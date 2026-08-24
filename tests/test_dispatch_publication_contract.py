from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from scripts import dispatch_publication_contract


class _Response:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.closed = False

    def read(self, _limit: int) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


def test_complete_dispatch_posts_one_exact_scoped_contract() -> None:
    revision = "a" * 40
    response = _Response()
    requests: list[tuple[object, float]] = []

    def opener(request: object, *, timeout: float) -> _Response:
        requests.append((request, timeout))
        return response

    dispatch_publication_contract.dispatch(
        revision,
        scope="complete",
        repository="beepboop2025/palimpsest",
        token="secret-token",
        opener=opener,
    )

    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == (
        "https://api.github.com/repos/beepboop2025/palimpsest/dispatches"
    )
    assert request.method == "POST"
    assert timeout == dispatch_publication_contract.REQUEST_TIMEOUT_SECONDS
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert json.loads(request.data) == {
        "event_type": "publication_contract",
        "client_payload": {"sha": revision, "scope": "complete"},
    }
    assert response.closed is True


def test_source_dispatch_requests_exact_contract_then_idempotent_graph_closure() -> (
    None
):
    revision = "d" * 40
    requests: list[object] = []

    def opener(request: object, *, timeout: float) -> _Response:
        assert timeout == dispatch_publication_contract.REQUEST_TIMEOUT_SECONDS
        requests.append(request)
        return _Response()

    dispatch_publication_contract.dispatch(
        revision,
        scope="source",
        repository="beepboop2025/palimpsest",
        token="secret-token",
        opener=opener,
    )

    assert [json.loads(request.data) for request in requests] == [
        {
            "event_type": "publication_contract",
            "client_payload": {"sha": revision, "scope": "source"},
        },
        {
            "event_type": "publication_graph_dirty",
            "client_payload": {"sha": revision},
        },
    ]


@pytest.mark.parametrize(
    ("revision", "repository"),
    [
        ("A" * 40, "beepboop2025/palimpsest"),
        ("a" * 39, "beepboop2025/palimpsest"),
        ("a" * 40, "../palimpsest"),
        ("a" * 40, "beepboop2025/palimpsest/extra"),
    ],
)
def test_dispatch_rejects_unsafe_identity_before_opening_a_request(
    revision: str,
    repository: str,
) -> None:
    def opener(_request: object, *, timeout: float) -> _Response:
        raise AssertionError(f"request unexpectedly opened with timeout {timeout}")

    with pytest.raises(dispatch_publication_contract.DispatchError):
        dispatch_publication_contract.dispatch(
            revision,
            scope="complete",
            repository=repository,
            token="secret-token",
            opener=opener,
        )


def test_dispatch_retries_transient_transport_failures_with_a_bound() -> None:
    outcomes: list[object] = [
        URLError("temporary"),
        HTTPError("https://api.github.com", 503, "unavailable", {}, None),
        _Response(),
    ]
    delays: list[float] = []

    def opener(_request: object, *, timeout: float) -> _Response:
        assert timeout == dispatch_publication_contract.REQUEST_TIMEOUT_SECONDS
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    dispatch_publication_contract.dispatch(
        "b" * 40,
        scope="complete",
        repository="beepboop2025/palimpsest",
        token="secret-token",
        opener=opener,
        sleeper=delays.append,
    )

    assert delays == [2.0, 4.0]
    assert outcomes == []


def test_dispatch_current_head_requires_the_payload_to_match_head(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Palimpsest tests"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tests@palimpsest.info"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "reading.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "add", "reading.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "reading"], cwd=tmp_path, check=True)

    with pytest.raises(
        dispatch_publication_contract.DispatchError,
        match="does not match",
    ):
        dispatch_publication_contract.dispatch_current_head(
            "c" * 40,
            scope="complete",
            repo=tmp_path,
            environ={},
        )


def test_dispatch_rejects_an_open_ended_scope_before_opening_a_request() -> None:
    def opener(_request: object, *, timeout: float) -> _Response:
        raise AssertionError(f"request unexpectedly opened with timeout {timeout}")

    with pytest.raises(
        dispatch_publication_contract.DispatchError,
        match="exactly 'source' or 'complete'",
    ):
        dispatch_publication_contract.dispatch(
            "e" * 40,
            scope="partial",
            repository="beepboop2025/palimpsest",
            token="secret-token",
            opener=opener,
        )


def test_actions_environment_requires_explicit_narrow_token() -> None:
    with pytest.raises(
        dispatch_publication_contract.DispatchError,
        match="PALIMPSEST_ACTIONS_TOKEN",
    ):
        dispatch_publication_contract._actions_credentials(  # noqa: SLF001
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": "beepboop2025/palimpsest",
            }
        )
