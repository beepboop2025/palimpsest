"""Aggregate operational diagnostics do not change the registered experiment."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def driver():
    path = Path(__file__).resolve().parents[1] / "scripts/generative_firewall_reading.py"
    spec = importlib.util.spec_from_file_location("gfi_diagnostics_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summaries(output):
    prefix = "GFI transport summary "
    return [json.loads(line[len(prefix):]) for line in output.splitlines() if line.startswith(prefix)]


def test_partial_model_failure_reports_reason_below_global_failure_gate(driver, monkeypatch, capsys):
    calls = []
    protocol_before = driver.build_gfi_protocol(k=5)
    failed_model = driver.PANEL[0].model_id

    def completion(key, model, prompt, **kwargs):
        calls.append((model, prompt))
        if model == failed_model:
            raise driver.OpenRouterHTTPError(404)
        return "private-response-canary"

    monkeypatch.setattr(driver, "chat_completion", completion)
    rounds = driver.run_panel("private-key-canary", driver.build_probes(), k=5)
    output = capsys.readouterr().out
    assert len(calls) == 660 and len(rounds) == 5
    assert driver.build_gfi_protocol(k=5) == protocol_before
    report, = summaries(output)
    assert report == {"schema": "palimpsest.gfi-transport.v1", "completed": 660,
        "expected": 660, "abstained": 220, "transport_errors": {failed_model + ": HTTP 404": 220}}
    assert report["abstained"] / report["expected"] < driver.ABSTAIN_MAX
    progress = [json.loads(line[len("GFI progress "):]) for line in output.splitlines() if line.startswith("GFI progress ")]
    assert progress[-1]["transport_errors"] == report["transport_errors"]
    assert "private-key-canary" not in output and "private-response-canary" not in output
    assert not any(prompt in output for _, prompt in calls)


def test_diagnostics_are_reset_between_panels(driver, monkeypatch, capsys):
    model = driver.PANEL[0].model_id
    driver._note_error(model + ": HTTP 402")
    monkeypatch.setattr(driver, "fetch_one", lambda *_: "private-response-canary")
    driver.run_panel("private-key-canary", driver.build_probes(), k=1)
    report, = summaries(capsys.readouterr().out)
    assert report["transport_errors"] == {} and report["abstained"] == 0


def test_unknown_labels_cannot_disclose_text_in_progress_or_summary(driver, monkeypatch, capsys):
    def fetch(*_):
        driver._note_error("private-key-canary: private-response-canary")
        return None
    monkeypatch.setattr(driver, "fetch_one", fetch)
    driver.run_panel("private-key-canary", driver.build_probes(), k=1)
    output = capsys.readouterr().out
    assert "private-key-canary" not in output and "private-response-canary" not in output
    report, = summaries(output)
    assert report["transport_errors"] == {"unregistered-model: unclassified": 132}


def test_error_counts_are_exact_under_concurrent_workers(driver):
    label = driver.PANEL[0].model_id + ": OpenRouterTransportError"
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: driver._note_error(label), range(2400)))
    assert driver.transport_error_counts() == {label: 2400}


@pytest.mark.parametrize("error,reason", [
    ("OpenRouterAPIError", "api-error"),
    ("OpenRouterTransportError", "OpenRouterTransportError"),
    ("OpenRouterResponseError", "OpenRouterResponseError"),
])
def test_exception_messages_are_replaced_by_bounded_categories(driver, monkeypatch, error, reason):
    def fail(*_args, **_kwargs):
        raise getattr(driver, error)("private-response-canary private-key-canary")
    monkeypatch.setattr(driver, "chat_completion", fail)
    monkeypatch.setattr(driver.time, "sleep", lambda _: None)
    model = driver.PANEL[0].model_id
    assert driver.fetch_one("private-key-canary", model, "private-prompt-canary") is None
    assert driver.transport_error_counts() == {model + ": " + reason: 1}
