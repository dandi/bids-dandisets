#!/usr/bin/env python3
"""Collate bids-validator JSON issues across all dandiset repos into JSONL on stdout.

For each matching repo in the organization, fetches the bids_validation.json
from raw.githubusercontent.com, extracts .issues.issues entries, annotates
each with 'dandiset' and 'branch', and prints one JSON object per line.

Requires GH_TOKEN environment variable for authenticated access.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys

import aiohttp

logger = logging.getLogger(__name__)

# raw.githubusercontent.com is generous but will 429 if hammered
MAX_CONCURRENT = 20
# Retry parameters for throttling / transient errors
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds


async def list_repos(
    session: aiohttp.ClientSession,
    org_url: str,
    repo_regex: str,
) -> list[str]:
    """List repos in the org matching *repo_regex*, using the GitHub API."""
    # org_url like "https://github.com/bids-dandisets/"
    org_name = org_url.rstrip("/").rsplit("/", 1)[-1]
    pattern = re.compile(repo_regex)
    repos: list[str] = []
    page = 1
    while True:
        url = f"https://api.github.com/orgs/{org_name}/repos?per_page=100&page={page}"
        async with session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"GitHub API error {resp.status}: {text}")
            data = await resp.json()
        if not data:
            break
        for repo in data:
            name = repo["name"]
            if pattern.fullmatch(name):
                repos.append(name)
        page += 1
    repos.sort()
    return repos


async def _fetch_url(
    session: aiohttp.ClientSession,
    url: str,
) -> aiohttp.ClientResponse | None:
    """Fetch *url* with retries on throttling / transient errors.

    Returns the response on success or 404 (caller checks status),
    or None if all retries are exhausted.
    """
    backoff = INITIAL_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await session.get(url)
            if resp.status == 404:
                return resp
            if resp.status == 429 or resp.status >= 500:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                logger.warning(
                    "%s: HTTP %d, retry %d/%d in %.1fs",
                    url, resp.status, attempt, MAX_RETRIES, wait,
                )
                resp.release()
                await asyncio.sleep(wait)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < MAX_RETRIES:
                logger.warning(
                    "%s: %s, retry %d/%d in %.1fs",
                    url, exc, attempt, MAX_RETRIES, backoff,
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            logger.error("%s: failed after %d attempts: %s", url, MAX_RETRIES, exc)
            return None
    # exhausted retries on 429/5xx
    logger.error("%s: gave up after %d retries", url, MAX_RETRIES)
    return None


async def fetch_validation(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    org_url: str,
    repo_name: str,
    branches: list[str],
    json_path: str,
) -> list[dict] | None:
    """Try each branch in order; return annotated issues from the first hit."""
    org_name = org_url.rstrip("/").rsplit("/", 1)[-1]
    async with sem:
        for branch in branches:
            url = (
                f"https://raw.githubusercontent.com/{org_name}/{repo_name}"
                f"/refs/heads/{branch}/{json_path}"
            )
            resp = await _fetch_url(session, url)
            if resp is None:
                # hard failure (retries exhausted) – skip this repo
                return None
            if resp.status == 404:
                resp.release()
                logger.debug(
                    "%s/%s: no %s on branch %s",
                    org_name, repo_name, json_path, branch,
                )
                continue
            # success
            data = await resp.json(content_type=None)
            resp.release()
            issues = data.get("issues", {}).get("issues", [])
            for issue in issues:
                issue["dandiset"] = repo_name
                issue["branch"] = branch
            logger.info(
                "%s/%s: %d issues (branch %s)",
                org_name, repo_name, len(issues), branch,
            )
            return issues

    logger.info(
        "%s/%s: no %s on any of branches %s",
        org_name, repo_name, json_path, ",".join(branches),
    )
    return None


async def collate(
    org_url: str,
    repo_regex: str,
    branches: list[str],
    json_path: str,
    jobs: int,
) -> None:
    """Main async entry: list repos, fetch all, print JSONL to stdout."""
    token = os.environ.get("GH_TOKEN", "")
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("GH_TOKEN not set – requests may be rate-limited")

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        repos = await list_repos(session, org_url, repo_regex)
        logger.info("Found %d repos matching '%s'", len(repos), repo_regex)

        sem = asyncio.Semaphore(jobs)
        tasks = [
            fetch_validation(session, sem, org_url, repo, branches, json_path)
            for repo in repos
        ]
        results = await asyncio.gather(*tasks)

    # Allow aiohttp SSL transports to close cleanly, avoiding a hang
    # during event-loop shutdown.
    await asyncio.sleep(0.25)

    total = 0
    for issues in results:
        if issues is None:
            continue
        for issue in issues:
            print(json.dumps(issue, separators=(",", ":")))
            total += 1

    logger.info("Wrote %d issue records total", total)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collate bids-validator issues across dandiset repos into JSONL.",
    )
    parser.add_argument(
        "--org",
        default="https://github.com/bids-dandisets/",
        help="GitHub organization URL (default: %(default)s)",
    )
    parser.add_argument(
        "--repo-regex",
        default=r"[0-9]+",
        help="Regex to match repo names (default: %(default)s)",
    )
    parser.add_argument(
        "--branches",
        default="curation,basic_sanitization",
        help="Comma-separated branches to try in order (default: %(default)s)",
    )
    parser.add_argument(
        "--json-path",
        default="derivatives/validations/bids_validation.json",
        help="Path to validation JSON within the repo (default: %(default)s)",
    )
    parser.add_argument(
        "--jobs", "-j",
        type=int,
        default=MAX_CONCURRENT,
        help="Max concurrent downloads (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: %(default)s)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    asyncio.run(
        collate(
            org_url=args.org,
            repo_regex=args.repo_regex,
            branches=[b.strip() for b in args.branches.split(",")],
            json_path=args.json_path,
            jobs=args.jobs,
        )
    )


if __name__ == "__main__":
    main()
