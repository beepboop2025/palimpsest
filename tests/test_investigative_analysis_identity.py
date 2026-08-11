from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "ops/investigative-analysis/install-host-bundle.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _fake_account_commands(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    fake_bin.mkdir()
    state.mkdir()

    _write_executable(
        fake_bin / "getent",
        """#!/usr/bin/env bash
set -eu
database="$1"
record_file="$IDENTITY_STATE/$database"
if (( $# == 1 )); then
  [[ -f "$record_file" ]] && /bin/cat "$record_file"
  exit 0
fi
key="$2"
[[ -f "$record_file" ]] || exit 2
while IFS= read -r record; do
  IFS=: read -r name _ numeric_id _ <<<"$record"
  if [[ "$key" == "$name" || "$key" == "$numeric_id" ]]; then
    printf '%s\n' "$record"
    exit 0
  fi
done <"$record_file"
exit 2
""",
    )
    _write_executable(
        fake_bin / "groupadd",
        """#!/usr/bin/env bash
set -eu
printf 'groupadd\n' >>"$IDENTITY_STATE/log"
printf '%s:x:%s:\n' "${@: -1}" "$3" >"$IDENTITY_STATE/group"
""",
    )
    _write_executable(
        fake_bin / "groupdel",
        """#!/usr/bin/env bash
set -eu
printf 'groupdel\n' >>"$IDENTITY_STATE/log"
/bin/rm -f "$IDENTITY_STATE/group"
""",
    )
    _write_executable(
        fake_bin / "useradd",
        """#!/usr/bin/env bash
set -eu
printf 'useradd\n' >>"$IDENTITY_STATE/log"
[[ "${FAKE_USERADD_FAIL:-0}" != 1 ]] || exit 55
printf 'palimpsest-analysis:x:10001:10001::/nonexistent:/usr/sbin/nologin\n' \
  >"$IDENTITY_STATE/passwd"
""",
    )
    _write_executable(
        fake_bin / "passwd",
        """#!/usr/bin/env bash
set -eu
[[ "$1" == --status ]]
printf '%s %s 2026-08-11 -1 -1 -1 -1\n' \
  "$2" "${FAKE_PASSWORD_STATE:-L}"
""",
    )
    _write_executable(
        fake_bin / "readlink",
        """#!/usr/bin/env bash
set -eu
path="${@: -1}"
case "$path" in
  /sbin/nologin|/usr/sbin/nologin) printf '/usr/sbin/nologin\n' ;;
  *) exec /usr/bin/readlink "$@" ;;
esac
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin:/bin"
    environment["IDENTITY_STATE"] = str(state)
    return state, environment


def _identity_harness() -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    start = source.index("enumerate_identity_record() {")
    end = source.index("\n}\n\nif ! revision=", start) + len("\n}")
    function = source[start:end]
    return f"""set -Eeuo pipefail
die() {{ printf '%s\n' "$*" >&2; exit 97; }}
runtime_name=palimpsest-analysis
runtime_id=10001
nologin_shell=/usr/sbin/nologin
{function}
ensure_runtime_identity
"""


def _run_identity(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _identity_harness()],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _seed_valid_identity(state: Path, *, shell: str = "/usr/sbin/nologin") -> None:
    (state / "group").write_text("palimpsest-analysis:x:10001:\n", encoding="utf-8")
    (state / "passwd").write_text(
        f"palimpsest-analysis:x:10001:10001::/nonexistent:{shell}\n",
        encoding="utf-8",
    )


def test_identity_creation_is_idempotent_and_accepts_equivalent_nologin(
    tmp_path: Path,
) -> None:
    state, environment = _fake_account_commands(tmp_path)

    created = _run_identity(environment)
    assert created.returncode == 0, created.stderr
    assert (state / "log").read_text(encoding="utf-8").splitlines() == [
        "groupadd",
        "useradd",
    ]

    (state / "passwd").write_text(
        "palimpsest-analysis:x:10001:10001::/nonexistent:/sbin/nologin\n",
        encoding="utf-8",
    )
    replayed = _run_identity(environment)

    assert replayed.returncode == 0, replayed.stderr
    assert (state / "log").read_text(encoding="utf-8").splitlines() == [
        "groupadd",
        "useradd",
    ]


def test_identity_refuses_name_or_numeric_collisions_without_mutation(
    tmp_path: Path,
) -> None:
    state, environment = _fake_account_commands(tmp_path)
    (state / "group").write_text("palimpsest-analysis:x:10001:\n", encoding="utf-8")
    (state / "passwd").write_text(
        "unrelated:x:10001:10001::/nonexistent:/usr/sbin/nologin\n",
        encoding="utf-8",
    )

    completed = _run_identity(environment)

    assert completed.returncode == 97
    assert "identity records disagree" in completed.stderr
    assert not (state / "log").exists()


def test_identity_refuses_duplicate_numeric_records_from_enumeration(
    tmp_path: Path,
) -> None:
    state, environment = _fake_account_commands(tmp_path)
    (state / "group").write_text(
        "palimpsest-analysis:x:10001:\nother-group:x:10001:\n",
        encoding="utf-8",
    )
    (state / "passwd").write_text(
        "palimpsest-analysis:x:10001:10001::/nonexistent:/usr/sbin/nologin\n",
        encoding="utf-8",
    )

    completed = _run_identity(environment)

    assert completed.returncode == 97
    assert "cannot prove the analysis group name/GID is unique" in completed.stderr
    assert not (state / "log").exists()


def test_identity_rolls_back_new_group_when_user_creation_fails(
    tmp_path: Path,
) -> None:
    state, environment = _fake_account_commands(tmp_path)
    environment["FAKE_USERADD_FAIL"] = "1"

    completed = _run_identity(environment)

    assert completed.returncode == 97
    assert "new group was rolled back" in completed.stderr
    assert not (state / "group").exists()
    assert (state / "log").read_text(encoding="utf-8").splitlines() == [
        "groupadd",
        "useradd",
        "groupdel",
    ]


def test_identity_requires_locked_password_and_exact_account_shape(
    tmp_path: Path,
) -> None:
    state, environment = _fake_account_commands(tmp_path)
    _seed_valid_identity(state)
    environment["FAKE_PASSWORD_STATE"] = "P"

    unlocked = _run_identity(environment)

    assert unlocked.returncode == 97
    assert "password is not locked" in unlocked.stderr

    environment["FAKE_PASSWORD_STATE"] = "L"
    (state / "passwd").write_text(
        "palimpsest-analysis:x:10001:10001::/home/unsafe:/usr/sbin/nologin\n",
        encoding="utf-8",
    )
    wrong_home = _run_identity(environment)

    assert wrong_home.returncode == 97
    assert "locked no-home" in wrong_home.stderr
