import os
import pathlib
import platform
import subprocess

import dandi.dandiapi
import requests

LIMIT_DANDISETS = None

GITHUB_TOKEN = os.environ.get("_GITHUB_API_KEY", None)
if GITHUB_TOKEN is None:
    message = "GITHUB_TOKEN environment variable not set"
    raise ValueError(message)

BASE_GITHUB_API_URL = "https://api.github.com/repos"
HEADER = {"Authorization": f"token {GITHUB_TOKEN}"}

SYSTEM = platform.system()
if SYSTEM == "Windows":
    # For Cody's local running
    BASE_DIRECTORY = pathlib.Path("E:/GitHub")
else:
    # For CI
    BASE_DIRECTORY = pathlib.Path.cwd()
    BASE_DIRECTORY.mkdir(exist_ok=True)


def run(limit: int | None = None) -> None:
    repo_directory = BASE_DIRECTORY / "bids-dandisets"
    _update_repo(repo_directory=repo_directory)

    client = dandi.dandiapi.DandiAPIClient()
    dandisets = client.get_dandisets()

    for counter, dandiset in enumerate(dandisets):
        if limit is not None and counter >= limit:
            break

        dandiset_id = dandiset.identifier

        print(f"Processing submodule for Dandiset {dandiset_id}...")

        repo_name = f"bids-dandisets/{dandiset_id}"
        repo_api_url = f"{BASE_GITHUB_API_URL}/{repo_name}"
        response = requests.get(url=repo_api_url, headers=HEADER)
        if response.status_code != 200:
            print(f"Status code {response.status_code}: {response.json()["message"]}")

            if response.status_code == 403:  # TODO: Not sure how to handle this yet
                continue

            continue

        submodule_path = repo_directory / dandiset_id
        if not submodule_path.exists():
            _deploy_subprocess(
                command=f"git submodule add https://github.com/bids-dandisets/{dandiset_id} {dandiset_id}",
                cwd=repo_directory,
            )
            print(f"\tCreated fresh submodule for Dandiset {dandiset_id}...")
        else:
            #_deploy_subprocess(command="git submodule update", cwd=submodule_path)
            _deploy_subprocess(command="git pull", cwd=submodule_path)
            print(f"\tUpdated submodule for Dandiset {dandiset_id}...")

        print(f"Process complete for Dandiset {dandiset_id}!\n\n")


def _deploy_subprocess(
    *,
    command: str | list[str],
    cwd: str | pathlib.Path | None = None,
    environment_variables: dict[str, str] | None = None,
    error_message: str | None = None,
    ignore_errors: bool = False,
) -> str | None:
    error_message = error_message or "An error occurred while executing the command."

    result = subprocess.run(
        args=command,
        cwd=cwd,
        shell=True,
        env=environment_variables,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0 and ignore_errors is False:
        message = (
            f"\n\nError code {result.returncode}\n"
            f"{error_message}\n\n"
            f"stdout: {result.stdout}\n\n"
            f"stderr: {result.stderr}\n\n"
        )
        raise RuntimeError(message)
    if result.returncode != 0 and ignore_errors is True:
        return None

    return result.stdout


def _update_repo(repo_directory: pathlib.Path) -> None:
    _deploy_subprocess(command="git pull", cwd=repo_directory)


if __name__ == "__main__":
    run(limit=LIMIT_DANDISETS)
