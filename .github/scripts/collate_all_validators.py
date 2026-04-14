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


async def fetch_validation(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    org_url: str,
    repo_name: str,
    branch: str,
    json_path: str,
) -> list[dict] | None:
    """Fetch validation JSON for one repo; return annotated issues or None."""
    org_name = org_url.rstrip("/").rsplit("/", 1)[-1]
    url = (
        f"https://raw.githubusercontent.com/{org_name}/{repo_name}"
        f"/refs/heads/{branch}/{json_path}"
    )
    backoff = INITIAL_BACKOFF
    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.get(url) as resp:
                    if resp.status == 404:
                        logger.info(
                            "%s/%s: no %s on branch %s",
                            org_name, repo_name, json_path, branch,
                        )
                        return None
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after else backoff
                        logger.warning(
                            "%s/%s: HTTP %d, retry %d/%d in %.1fs",
                            org_name, repo_name, resp.status,
                            attempt, MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        backoff *= 2
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "%s/%s: %s, retry %d/%d in %.1fs",
                        org_name, repo_name, exc,
                        attempt, MAX_RETRIES, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                logger.error(
                    "%s/%s: failed after %d attempts: %s",
                    org_name, repo_name, MAX_RETRIES, exc,
                )
                return None
            else:
                break
        else:
            # exhausted retries on 429/5xx
            logger.error(
                "%s/%s: gave up after %d retries",
                org_name, repo_name, MAX_RETRIES,
            )
            return None

    issues = data.get("issues", {}).get("issues", [])
    for issue in issues:
        issue["dandiset"] = repo_name
        issue["branch"] = branch
    logger.info(
        "%s/%s: %d issues", org_name, repo_name, len(issues),
    )
    return issues


async def collate(
    org_url: str,
    repo_regex: str,
    branch: str,
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
            fetch_validation(session, sem, org_url, repo, branch, json_path)
            for repo in repos
        ]
        results = await asyncio.gather(*tasks)

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
        "--branch",
        default="curation",
        help="Branch to fetch from (default: %(default)s)",
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
            branch=args.branch,
            json_path=args.json_path,
            jobs=args.jobs,
        )
    )


if __name__ == "__main__":
    main()
