"""Dispatch the contract gate for the exact commit a publisher put on main.

GitHub deliberately suppresses ordinary workflow events caused by ``GITHUB_TOKEN``
pushes.  Repository dispatch is an explicit exception, so scheduled publishers use
this helper after a successful compare-and-swap push.  The receiving workflow still
validates that the supplied commit is reachable from public main before reading it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
EVENT_TYPE = "publication_contract"
MAX_ATTEMPTS = 4
REQUEST_TIMEOUT_SECONDS = 20.0
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?\Z"
)


class DispatchError(RuntimeError):
    """The exact publication commit could not be handed to the contract gate."""


def _validated_sha(value: str) -> str:
    if SHA_RE.fullmatch(value) is None:
        raise DispatchError(
            "publication SHA must be 40 lowercase hexadecimal characters"
        )
    return value


def _validated_repository(value: str) -> str:
    if REPOSITORY_RE.fullmatch(value) is None or ".." in value:
        raise DispatchError("GITHUB_REPOSITORY must be a safe owner/name pair")
    return value


def _actions_credentials(
    environ: Mapping[str, str],
) -> tuple[str, str] | None:
    """Return the Actions repository/token pair, or None for a human-origin push."""
    if environ.get("GITHUB_ACTIONS") != "true":
        return None
    repository = _validated_repository(environ.get("GITHUB_REPOSITORY", ""))
    token = environ.get("PALIMPSEST_ACTIONS_TOKEN", "")
    if not token or "\n" in token or "\r" in token:
        raise DispatchError("PALIMPSEST_ACTIONS_TOKEN is missing or malformed")
    return repository, token


def _retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def dispatch(
    revision: str,
    *,
    repository: str,
    token: str,
    attempts: int = MAX_ATTEMPTS,
    opener: Callable[..., object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Create one exact-SHA repository-dispatch event, with bounded retries."""
    revision = _validated_sha(revision)
    repository = _validated_repository(repository)
    if not token or "\n" in token or "\r" in token:
        raise DispatchError("dispatch token is missing or malformed")
    if attempts < 1 or attempts > MAX_ATTEMPTS:
        raise DispatchError("dispatch attempt count is outside the reviewed bound")

    body = json.dumps(
        {
            "event_type": EVENT_TYPE,
            "client_payload": {"sha": revision},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"https://api.github.com/repos/{repository}/dispatches",
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    open_request = opener or urlopen
    last_detail = "request was not attempted"
    for attempt in range(1, attempts + 1):
        retryable = False
        try:
            response = open_request(request, timeout=REQUEST_TIMEOUT_SECONDS)
            try:
                status = int(getattr(response, "status"))
                response.read(4096)
            finally:
                response.close()
            if status == 204:
                print(f"dispatched {EVENT_TYPE} for {revision}")
                return
            retryable = _retryable_status(status)
            last_detail = f"GitHub returned HTTP {status}"
        except HTTPError as error:
            retryable = _retryable_status(error.code)
            last_detail = f"GitHub returned HTTP {error.code}"
        except (OSError, URLError) as error:
            retryable = True
            last_detail = f"GitHub request failed: {type(error).__name__}"

        if not retryable or attempt == attempts:
            break
        delay = float(attempt * 2)
        print(
            f"contract dispatch attempt {attempt} failed; retrying in {delay:g}s",
            file=sys.stderr,
        )
        sleeper(delay)

    raise DispatchError(
        f"contract dispatch failed after {attempt} attempt(s): {last_detail}"
    )


def _head(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise DispatchError("could not resolve the publication checkout HEAD")
    return _validated_sha(completed.stdout.strip())


def dispatch_current_head(
    revision: str,
    *,
    repo: Path = ROOT,
    environ: Mapping[str, str] = os.environ,
) -> bool:
    """Dispatch an Actions publication, or defer to a human push's normal event."""
    revision = _validated_sha(revision)
    if _head(repo) != revision:
        raise DispatchError("publication SHA does not match the checkout HEAD")
    credentials = _actions_credentials(environ)
    if credentials is None:
        print("outside GitHub Actions; the ordinary push event owns the contract gate")
        return False
    repository, token = credentials
    dispatch(revision, repository=repository, token=token)
    return True


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="?")
    parser.add_argument(
        "--check-environment",
        action="store_true",
        help="validate the Actions dispatch authority without sending an event",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        if arguments.check_environment:
            if arguments.revision is not None:
                raise DispatchError("--check-environment does not accept a SHA")
            _actions_credentials(os.environ)
            return 0
        if arguments.revision is None:
            raise DispatchError("a publication SHA is required")
        dispatch_current_head(arguments.revision)
    except (DispatchError, OSError, ValueError) as error:
        print(f"contract dispatch refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
