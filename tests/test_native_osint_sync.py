"""The native adapter advances private OSINT only along delivered, approved editions."""
import email.message
import importlib.util
import json
import shutil
import sys
from types import SimpleNamespace

import pytest

try:
    from tests import test_railway_osint_sync as railway_tests
except ImportError:  # run from inside tests/ (unittest discover -s tests)
    import test_railway_osint_sync as railway_tests

fixtures = railway_tests.fixtures
SPEC = importlib.util.spec_from_file_location("native_osint_sync", railway_tests.ROOT / "ops/osint-sync/native_osint_sync.py")
native = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = native
SPEC.loader.exec_module(native)
adapter = native.railway
legacy = native.legacy

NOW = fixtures.datetime(2026, 8, 14, 1, 30, tzinfo=fixtures.timezone.utc)
PATHS = (native.MANIFEST_PATH, legacy.OSINT_REPOSITORY_PATH, legacy.LEDGER_REPOSITORY_PATH, native.RIGHTS_PATH)
URLS = (legacy.PUBLIC_MANIFEST_URL, legacy.PUBLIC_URL, legacy.PUBLIC_LEDGER_URL, legacy.PUBLIC_RIGHTS_STATUS_URL)
ADAPTER = legacy._sha256(b"native builder")
NEXT_ADAPTER = legacy._sha256(b"next native builder")
FORK_ARCHIVE = "readings/audit/readings-ledger-build-fork-20261122.jsonl"
FORK_ADMISSION = "readings/audit/readings-ledger-admission-20261122.json"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    for module in {id(legacy): legacy, id(fixtures.sync): fixtures.sync}.values():
        monkeypatch.setattr(module, "_now", lambda: NOW)


def sha(raw):
    return legacy._sha256(raw)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return raw


def git(world, *args):
    return fixtures._git(world["repo"], *args)


def commit_edition(world, base, *, generated, value, ledger, files=None):
    """One native build: a single generated commit on top of its source base."""
    git(world, "checkout", "-q", "--detach", base)
    for name, raw in (files or {}).items():
        path = world["repo"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    document = fixtures._document(generated, base, value=value)
    release = fixtures._write_publication(world["repo"], document, ledger, "data: native edition " + generated)
    world["artifacts"][release] = (world["repo"] / legacy.OSINT_REPOSITORY_PATH).read_bytes()
    world["ledgers"][release] = ledger
    return release


def deliver(world, build, *, accepted_at, previous, pointer=True, **changes):
    record = {**build, "status": "native_serving_verified", "accepted_at": accepted_at,
              "container": native.CONTAINER_PREFIX + build["release_sha"][:16], "port": 34000,
              "image_id": "sha256:" + "7" * 64, "checks": ["healthz", "freshness"], "public_ingress": True,
              "fallback_ingress": True, "fallback_hostname": native.FALLBACK_HOST,
              "previous_container": None if previous is None else native.CONTAINER_PREFIX + previous[:16]}
    record.update(changes)
    raw = write_json(world["config"].deliveries / (build["release_sha"] + ".json"), record)
    if pointer:
        world["config"].pointer.write_bytes(raw)
    return record


def publish(world, release, base, pin, *, accepted_at, previous, adapter_sha=ADAPTER, keep=True, **changes):
    """Seal the release like seal_build.py, then record its delivery receipt."""
    root = world["config"].native / "releases" / release
    (root / "public").mkdir(parents=True)
    git(world, "checkout", "-q", "--detach", release)
    git(world, "bundle", "create", str(root / "source.bundle"), "HEAD", "^" + base)
    public = fixtures._restricted_publication_bundle(world["f"], release_commit=release, ledger_raw=world["ledgers"][release])
    manifest_raw = public["payloads"][legacy.PUBLIC_MANIFEST_URL]
    (root / "public" / native.MANIFEST_PATH).write_bytes(manifest_raw)
    rights = public["payloads"][legacy.PUBLIC_RIGHTS_STATUS_URL]
    rights_receipt = write_json(root / "rights-receipt.json", {
        "schema_version": native.RIGHTS_RECEIPT_SCHEMA, "publication_sha": release,
        "status": {"path": native.RIGHTS_PATH, "bytes": len(rights), "sha256": sha(rights)}})
    manifest = public["manifest"]
    build = {"schema_version": native.BUILD_SCHEMA, "status": "artifact_verified", "built_at": accepted_at,
             "release_sha": release, "base_sha": base, "base_pin_sha256": pin, "input_sha256": "4" * 64,
             "snapshot_manifest_sha256": "5" * 64, "wire_generated_at": "2026-08-14T01:15:00Z",
             "wire_canonical_sha256": "6" * 64, "manifest_sha256": sha(manifest_raw),
             "tree_sha256": manifest["tree_sha256"], "files_verified": manifest["file_count"],
             "bytes_verified": manifest["total_bytes"], "source_bundle_sha256": sha((root / "source.bundle").read_bytes()),
             "rights_receipt_sha256": sha(rights_receipt), "adapter_sha256": adapter_sha,
             "legacy_publication_lease_held": False, "rights_and_semantic_checks_passed": True,
             "railway_called": False, "publicly_accepted": False}
    write_json(root / "build.json", build)
    deliver(world, build, accepted_at=accepted_at, previous=previous, **changes)
    world["payloads"][release] = dict(zip(PATHS, (public["payloads"][url] for url in URLS)))
    world["live"] = release
    if not keep:
        # The delivery service prunes all but the newest released trees.
        shutil.rmtree(root)
    return build


def write_bridge(world):
    write_json(world["config"].bridge, world["bridge"])


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An installed Railway edition, its frozen successor, then two native deliveries."""
    f = railway_tests.build(tmp_path)
    installed = adapter.synchronize(f["config"], fetcher=f["fetch"])
    historical = f["base_sha"]
    railway_tests.successor(f)
    handover = (f["config"].publication / "latest-success.json").read_bytes()
    repo = tmp_path / "native-source"
    fixtures._git(tmp_path, "clone", "-q", "--no-local", str(f["source"]), str(repo))
    fixtures._git(repo, "config", "user.name", "Native fixture")
    fixtures._git(repo, "config", "user.email", "native@example.com")
    config = native.Config(**vars(f["config"]), native=tmp_path / "native", deliveries=tmp_path / "deliveries",
                           pointer=tmp_path / "latest-delivery.json", bridge=tmp_path / "etc" / "native-publication-lineage.json",
                           selections=tmp_path / "etc")
    (tmp_path / "native" / "releases").mkdir(parents=True)
    (tmp_path / "deliveries").mkdir()
    w = {"f": f, "repo": repo, "config": config, "installed": installed, "historical": historical,
         "pin": sha(b"historical publication base"), "artifacts": {}, "ledgers": {}, "payloads": {}, "calls": []}
    base_ledger = f["first_ledger"]
    initial = commit_edition(w, historical, generated="2026-08-14T01:08:00Z", value=3,
                             ledger=fixtures._append_seal(base_ledger, fixtures._document("2026-08-14T01:08:00Z", historical, value=3), 1))
    publish(w, initial, historical, w["pin"], accepted_at="2026-08-14T01:26:00+00:00", previous=None, keep=False)
    current = commit_edition(w, historical, generated="2026-08-14T01:10:00Z", value=4,
                             ledger=fixtures._append_seal(base_ledger, fixtures._document("2026-08-14T01:10:00Z", historical, value=4), 1))
    publish(w, current, historical, w["pin"], accepted_at="2026-08-14T01:27:00+00:00", previous=initial)
    w["bridge"] = {"schema": native.BRIDGE_SCHEMA, "base_sha": historical, "base_pin_sha256": w["pin"],
                   "approved_native_adapters": [ADAPTER], "historical_receipt_sha256": sha(handover),
                   "historical_validator_sha256": "8" * 64, "historical_proof_is_live_observation": False,
                   "native_initial_release_sha": initial, "native_source_selections": []}
    write_bridge(w)
    w.update(initial=initial, current=current)

    def fetch(url, release):
        w["calls"].append((url, release))
        for origin in native.ORIGINS:
            if url.startswith(origin + "/"):
                return w["payloads"][w["live"]][url[len(origin) + 1:]]
        raise AssertionError("unexpected origin " + url)

    w["fetch"] = fetch
    return w


def snapshot(w):
    c = w["config"]
    return {str(p): p.read_bytes() for p in [*c.authority.iterdir(), c.marker, c.bridge, c.pointer, *c.deliveries.iterdir()]
            if p.is_file()}


def test_first_native_edition_continues_the_frozen_railway_handover(world):
    c = world["config"]
    railway_receipt = (c.authority / adapter.RECEIPT_NAME).read_bytes()
    before = snapshot(world)
    checked = native.synchronize(c, fetcher=world["fetch"], check=True)
    assert checked["status"] == "checked" and snapshot(world) == before
    assert not (c.state / native.LAYOUT.history).exists()
    result = native.synchronize(c, fetcher=world["fetch"])
    assert result["status"] == "installed" and result["release_sha"] == world["current"]
    assert result["origins"] == list(native.ORIGINS) and result["private_only"] is True
    assert result["collector_chain"] is None and result["base_sha"] == world["historical"]
    assert result["delivery_receipt_sha256"] == sha((c.deliveries / (world["current"] + ".json")).read_bytes())
    assert (c.authority / legacy.OSINT_FILENAME).read_bytes() == world["artifacts"][world["current"]]
    assert (c.authority / legacy.LEDGER_FILENAME).read_bytes() == world["ledgers"][world["current"]]
    assert (c.authority / adapter.RECEIPT_NAME).read_bytes() == railway_receipt
    assert result["historical_authority_sha256"] == sha(railway_receipt)
    assert native.verify_installed(c) == result
    histories = [adapter.read_archive(c, p.name, native.LAYOUT) for p in (c.state / native.LAYOUT.history).iterdir()]
    assert any(h[native.RECEIPT_NAME] is None and h[legacy.LEDGER_FILENAME] == before[str(c.authority / legacy.LEDGER_FILENAME)]
               for h in histories)
    assert not (c.state / native.LAYOUT.transaction).exists() and not list(c.state.glob("native-proof-*"))


def test_next_edition_follows_the_receiver_chain_and_repeats_idempotently(world):
    c = world["config"]
    first = native.synchronize(c, fetcher=world["fetch"])
    base_ledger = world["f"]["first_ledger"]
    nxt = commit_edition(world, world["historical"], generated="2026-08-14T01:12:00Z", value=5,
                         ledger=fixtures._append_seal(base_ledger, fixtures._document("2026-08-14T01:12:00Z", world["historical"], value=5), 1))
    publish(world, nxt, world["historical"], world["pin"], accepted_at="2026-08-14T01:28:00+00:00", previous=world["current"])
    result = native.synchronize(c, fetcher=world["fetch"])
    assert result["release_sha"] == nxt and result["status"] == "installed"
    assert result["delivery_receipt_sha256"] != first["delivery_receipt_sha256"]
    files = {str(p): p.read_bytes() for p in c.state.rglob("*") if p.is_file()}
    assert native.synchronize(c, fetcher=world["fetch"]) == result
    assert {str(p): p.read_bytes() for p in c.state.rglob("*") if p.is_file()} == files


def select_generation(world, monkeypatch, *, tamper=None):
    """An owner-approved source generation whose Git ledger carries a reviewed fork."""
    historical, base_ledger = world["historical"], world["f"]["first_ledger"]
    fork = railway_tests.append_entry(base_ledger, "ucdp-aggregate", 1)
    git(world, "checkout", "-q", "--detach", historical)
    (world["repo"] / "README.md").write_text("selected upstream change\n")
    (world["repo"] / legacy.LEDGER_REPOSITORY_PATH).write_bytes(fork)
    git(world, "add", "README.md", "readings")
    git(world, "commit", "-q", "-m", "Select a reviewed upstream change")
    source = git(world, "rev-parse", "HEAD")
    git(world, "update-ref", "refs/heads/native/generation-b", source)
    directory = world["config"].selections / "native-generation-b"
    directory.mkdir(parents=True)
    bundle = directory / "source.bundle"
    git(world, "bundle", "create", str(bundle), "refs/heads/native/generation-b", "^" + historical)
    old_blob = git(world, "rev-parse", historical + ":" + legacy.LEDGER_REPOSITORY_PATH)
    new_blob = git(world, "rev-parse", source + ":" + legacy.LEDGER_REPOSITORY_PATH)
    record = {"schema": native.SELECTION_SCHEMA, "base": historical, "historical_base_pin_sha256": world["pin"],
              "source": source, "source_tree": git(world, "rev-parse", source + "^{tree}"),
              "ref": "refs/heads/native/generation-b", "bundle_sha256": sha(bundle.read_bytes()),
              "files": {legacy.LEDGER_REPOSITORY_PATH: {"old_blob": old_blob, "new_blob": "f" * 40 if tamper == "blob" else new_blob}}}
    pin = sha(write_json(directory / "selection.json", record))
    world["bridge"]["native_source_selections"].append({"base_sha": source, "base_pin_sha256": pin,
                                                         "approved_native_adapters": [NEXT_ADAPTER]})
    write_bridge(world)
    # The reviewed producer commit lives upstream and is absent from every local object store.
    spec = {"target_sha256": sha(fork), "target_entries": 2, "host_prefix_sha256": sha(base_ledger),
            "host_prefix_entries": 1, "common_entries": 1, "source_commit": "e" * 40,
            "archive_path": FORK_ARCHIVE, "admission_path": FORK_ADMISSION}
    monkeypatch.setattr(adapter, "REVIEWED_FORKS", (spec,))
    world.update(source=source, source_pin=pin, fork=fork, spec=spec)
    return world


def admitted_edition(world, host, *, generated, value, previous, accepted_at):
    document = fixtures._document(generated, world["source"], value=value)
    ledger = fixtures._append_seal(host, document, len(host.splitlines()))
    proof = railway_tests.admission_receipt(world["spec"], world["fork"], host)
    release = commit_edition(world, world["source"], generated=generated, value=value, ledger=ledger,
                             files={FORK_ARCHIVE: world["fork"], FORK_ADMISSION: fixtures._json_bytes(proof)})
    publish(world, release, world["source"], world["source_pin"], accepted_at=accepted_at, previous=previous,
            adapter_sha=NEXT_ADAPTER)
    return release


def test_generation_switch_admits_the_reviewed_fork_through_the_owner_selection(world, monkeypatch):
    select_generation(world, monkeypatch)
    host = railway_tests.append_entry(world["f"]["first_ledger"], "collector-a", 1)
    release = admitted_edition(world, host, generated="2026-08-14T01:12:00Z", value=5,
                               previous=world["current"], accepted_at="2026-08-14T01:28:00+00:00")
    c = world["config"]
    result = native.synchronize(c, fetcher=world["fetch"])
    assert result["release_sha"] == release and result["base_sha"] == world["source"]
    assert result["collector_chain"] == {"entries": 2, "sha256": sha(host)}
    # The next edition of the same generation must keep every captured collector entry.
    grown = railway_tests.append_entry(host, "collector-b", 2)
    nxt = admitted_edition(world, grown, generated="2026-08-14T01:14:00Z", value=6,
                           previous=release, accepted_at="2026-08-14T01:29:00+00:00")
    result = native.synchronize(c, fetcher=world["fetch"])
    assert result["release_sha"] == nxt and result["collector_chain"] == {"entries": 3, "sha256": sha(grown)}


def test_rewritten_collector_history_is_refused(world, monkeypatch):
    select_generation(world, monkeypatch)
    host = railway_tests.append_entry(world["f"]["first_ledger"], "collector-a", 1)
    release = admitted_edition(world, host, generated="2026-08-14T01:12:00Z", value=5,
                               previous=world["current"], accepted_at="2026-08-14T01:28:00+00:00")
    native.synchronize(world["config"], fetcher=world["fetch"])
    forged = railway_tests.append_entry(railway_tests.append_entry(world["f"]["first_ledger"], "collector-z", 1), "collector-b", 2)
    admitted_edition(world, forged, generated="2026-08-14T01:14:00Z", value=6, previous=release,
                     accepted_at="2026-08-14T01:29:00+00:00")
    before = snapshot(world)
    with pytest.raises(legacy.SyncFailure, match="collector-chain-rewritten"):
        native.synchronize(world["config"], fetcher=world["fetch"])
    assert snapshot(world) == before


def test_unapproved_ledger_blob_in_selection_refuses_the_fork(world, monkeypatch):
    select_generation(world, monkeypatch, tamper="blob")
    host = railway_tests.append_entry(world["f"]["first_ledger"], "collector-a", 1)
    admitted_edition(world, host, generated="2026-08-14T01:12:00Z", value=5,
                     previous=world["current"], accepted_at="2026-08-14T01:28:00+00:00")
    before = snapshot(world)
    with pytest.raises(legacy.SyncFailure, match="admission-source-ancestry-invalid"):
        native.synchronize(world["config"], fetcher=world["fetch"])
    assert snapshot(world) == before


def rewrite_delivery(world, release, **changes):
    path = world["config"].deliveries / (release + ".json")
    record = json.loads(path.read_bytes())
    record.update(changes)
    raw = write_json(path, record)
    if json.loads(world["config"].pointer.read_bytes())["release_sha"] == release:
        world["config"].pointer.write_bytes(raw)


def tamper(world, kind):
    c, current = world["config"], world["current"]
    root = c.native / "releases" / current
    if kind == "stale":
        rewrite_delivery(world, current, accepted_at="2026-08-13T23:00:00+00:00")
    elif kind == "pointer":
        c.pointer.write_bytes(c.pointer.read_bytes() + b"\n")
    elif kind == "ingress":
        rewrite_delivery(world, current, fallback_ingress=False)
    elif kind == "build-binding":
        rewrite_delivery(world, current, tree_sha256="9" * 64)
    elif kind == "adapter":
        world["bridge"]["approved_native_adapters"] = [NEXT_ADAPTER]
        write_bridge(world)
    elif kind == "generation":
        world["bridge"]["base_pin_sha256"] = sha(b"another base pin")
        write_bridge(world)
    elif kind == "bundle":
        (root / "source.bundle").write_bytes((root / "source.bundle").read_bytes() + b"tamper")
    elif kind == "rights":
        (root / "rights-receipt.json").write_bytes(b"{}\n")
    elif kind == "handover":
        receipts = c.publication / "latest-success.json"
        value = json.loads(receipts.read_bytes())
        value["recorded_at"] = "2026-08-14T01:24:00Z"
        receipts.unlink()
        write_json(receipts, value)
    elif kind == "chain-gap":
        (c.deliveries / (world["initial"] + ".json")).unlink()
    elif kind == "initial":
        world["bridge"]["native_initial_release_sha"] = "a" * 40
        write_bridge(world)
    elif kind == "early-initial":
        rewrite_delivery(world, world["initial"], accepted_at="2026-08-14T01:24:00+00:00")
    elif kind in ("live-ledger", "unrestricted", "live-manifest"):
        payloads = dict(world["payloads"][current])
        if kind == "live-ledger":
            payloads[legacy.LEDGER_REPOSITORY_PATH] = world["f"]["first_ledger"]
        elif kind == "unrestricted":
            payloads[legacy.OSINT_REPOSITORY_PATH] = world["artifacts"][current]
        else:
            payloads[native.MANIFEST_PATH] = payloads[native.MANIFEST_PATH] + b" "
        original = world["fetch"]

        def fetch(url, release):
            if url.startswith(native.ORIGINS[1] + "/"):
                return payloads[url[len(native.ORIGINS[1]) + 1:]]
            return original(url, release)

        world["fetch"] = fetch


@pytest.mark.parametrize("kind,reason", [
    ("stale", "publication-stale"),
    ("pointer", "delivery-pointer-invalid"),
    ("ingress", "delivery-ingress-invalid"),
    ("build-binding", "delivery-build-mismatch"),
    ("adapter", "native-adapter-unapproved"),
    ("generation", "native-generation-unapproved"),
    ("bundle", "bundle-hash-mismatch"),
    ("rights", "rights-receipt-mismatch"),
    ("handover", "historical-publication-changed"),
    ("chain-gap", "delivery-predecessor-missing"),
    ("initial", "delivery-predecessor-missing"),
    ("early-initial", "native-handover-invalid"),
    ("live-ledger", "live-ledger-mismatch"),
    ("unrestricted", "public-unrestricted-osint-refused"),
    ("live-manifest", "live-manifest-mismatch"),
])
def test_unproved_native_edition_leaves_every_authority_byte(world, kind, reason):
    tamper(world, kind)
    before = snapshot(world)
    with pytest.raises(legacy.SyncFailure, match=reason):
        native.synchronize(world["config"], fetcher=world["fetch"])
    assert snapshot(world) == before
    assert not (world["config"].authority / native.RECEIPT_NAME).exists()


def test_edition_accepted_while_observing_is_selected_instead(world):
    c = world["config"]
    base_ledger = world["f"]["first_ledger"]
    nxt = commit_edition(world, world["historical"], generated="2026-08-14T01:12:00Z", value=5,
                         ledger=fixtures._append_seal(base_ledger, fixtures._document("2026-08-14T01:12:00Z", world["historical"], value=5), 1))
    pointer = c.pointer.read_bytes()
    publish(world, nxt, world["historical"], world["pin"], accepted_at="2026-08-14T01:28:00+00:00", previous=world["current"])
    advanced = c.pointer.read_bytes()
    c.pointer.write_bytes(pointer)
    original = world["fetch"]

    def fetch(url, release):
        # The receiver switches routes after this run read the older pointer.
        c.pointer.write_bytes(advanced)
        return original(url, release)

    assert native.synchronize(c, fetcher=fetch)["release_sha"] == nxt


def test_predecessor_equivocation_is_refused(world):
    c = world["config"]
    native.synchronize(c, fetcher=world["fetch"])
    rewrite_delivery(world, world["current"], checks=["healthz"])
    before = snapshot(world)
    with pytest.raises(legacy.SyncFailure, match="delivery-predecessor-mismatch"):
        native.synchronize(c, fetcher=world["fetch"])
    assert snapshot(world) == before


def state(config):
    return {name: None if item is None else item.raw for name, item in adapter.state_snapshot(config, native.LAYOUT).items()}


@pytest.mark.parametrize("stop_after", [legacy.LEDGER_FILENAME, legacy.OSINT_FILENAME, native.RECEIPT_NAME])
def test_interrupted_first_switch_restores_the_railway_pair(world, monkeypatch, stop_after):
    c = world["config"]
    prior = state(c)
    original = legacy._install_authority_file

    def interrupted(config, path, raw, **kwargs):
        result = original(config, path, raw, **kwargs)
        if path.name == stop_after:
            raise OSError("simulated power loss")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(legacy, "_install_authority_file", interrupted)
        with pytest.raises(OSError, match="power loss"):
            native.synchronize(c, fetcher=world["fetch"])
    with pytest.raises(legacy.SyncFailure, match="transaction-recovery-required"):
        native.synchronize(c, fetcher=world["fetch"], check=True)

    def offline(*_):
        assert state(c) == prior
        raise legacy.SyncFailure("synthetic-publication-offline")

    with pytest.raises(legacy.SyncFailure, match="synthetic-publication-offline"):
        native.synchronize(c, fetcher=offline)
    assert native.synchronize(c, fetcher=world["fetch"])["status"] == "installed"


class Response:
    def __init__(self, url, raw, headers):
        self.url, self.raw, self.headers = url, raw, headers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def getcode(self):
        return 200

    def geturl(self):
        return self.url

    def read(self, _limit):
        return self.raw


def route_headers(values):
    message = email.message.Message()
    for name, value in values:
        message[name] = value
    return message


@pytest.mark.parametrize("origin,headers,ok", [
    (native.ORIGINS[0], [("X-Hosting-Origin", "hetzner-sin1")], True),
    (native.ORIGINS[1], [("X-Hosting-Origin", "hetzner-sin1"), ("X-Migration-Fallback", native.FALLBACK_HOST)], True),
    (native.ORIGINS[1], [("X-Hosting-Origin", "hetzner-sin1")], False),
    (native.ORIGINS[0], [("X-Hosting-Origin", "hetzner-sin1"), ("X-Migration-Fallback", native.FALLBACK_HOST)], False),
    (native.ORIGINS[0], [("X-Hosting-Origin", "railway")], False),
    (native.ORIGINS[0], [("X-Hosting-Origin", "hetzner-sin1"), ("X-Hosting-Origin", "hetzner-sin1")], False),
])
def test_each_route_must_prove_its_own_path_to_the_receiver(monkeypatch, origin, headers, ok):
    seen = []

    def opener(*_):
        def open_(request, timeout):
            seen.append(request)
            return Response(request.full_url, b"{}", route_headers(headers))
        return SimpleNamespace(open=open_)

    monkeypatch.setattr(native.urllib.request, "build_opener", opener)
    url = origin + "/" + native.MANIFEST_PATH
    if ok:
        assert native.fetch_public(url, "a" * 40) == b"{}"
    else:
        with pytest.raises(legacy.SyncFailure, match="native-ingress-mismatch"):
            native.fetch_public(url, "a" * 40)
    assert seen[0].get_header("User-agent") == native.USER_AGENT


def test_fetch_rejects_unapproved_urls_before_open(monkeypatch):
    monkeypatch.setattr(native.urllib.request, "build_opener", lambda *_: pytest.fail("unexpected network request"))
    for url in (adapter.ORIGINS[1] + "/" + native.MANIFEST_PATH, "https://hetzner-origin.liquilens.in/" + native.MANIFEST_PATH,
                native.ORIGINS[1] + "/private.json"):
        with pytest.raises(legacy.SyncFailure, match="public-url-refused"):
            native.fetch_public(url, "a" * 40)
