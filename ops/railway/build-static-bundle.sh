#!/usr/bin/env bash
# Materialize the exact Git publication into a new Railway upload directory.

set -Eeuo pipefail

if [[ "$#" -ne 2 ]]; then
  printf 'usage: %s EXPECTED_40_HEX_SHA OUTPUT_DIRECTORY\n' "$0" >&2
  exit 2
fi

expected_sha="$1"
output_directory="$2"
script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_directory/../.." && pwd -P)"

if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'expected SHA must be exactly 40 lowercase hex characters\n' >&2
  exit 2
fi
if [[ -e "$output_directory" ]]; then
  printf 'output path already exists; refusing to overwrite: %s\n' "$output_directory" >&2
  exit 2
fi
if ! current_sha="$(git -C "$repo_root" rev-parse --verify HEAD)"; then
  printf 'cannot resolve source revision\n' >&2
  exit 1
fi
if [[ "$current_sha" != "$expected_sha" ]]; then
  printf 'source revision does not match expected SHA\n' >&2
  exit 1
fi
if ! checkout_status="$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)"; then
  printf 'cannot verify source checkout status\n' >&2
  exit 1
fi
if [[ -n "$checkout_status" ]]; then
  printf 'source checkout is modified or untracked; refusing release\n' >&2
  exit 1
fi
if git -C "$repo_root" ls-files -s | awk '$1 == "120000" { found=1 } END { exit found ? 0 : 1 }'; then
  printf 'publication bundle refuses tracked symbolic links\n' >&2
  exit 1
fi

output_parent="$(cd "$(dirname "$output_directory")" && pwd -P)"
output_name="$(basename "$output_directory")"
final_rights_receipt="$output_parent/${output_name}.pages-rights-release-receipt.json"
if [[ -e "$final_rights_receipt" ]]; then
  printf 'rights receipt path already exists; refusing to overwrite: %s\n' \
    "$final_rights_receipt" >&2
  exit 2
fi
staging_directory="$(mktemp -d "$output_parent/.palimpsest-railway-stage.XXXXXX")"
control_directory="$(mktemp -d "$output_parent/.palimpsest-railway-control.XXXXXX")"
cleanup() {
  if [[ -n "${staging_directory:-}" && -d "$staging_directory" ]]; then
    rm -rf -- "$staging_directory"
  fi
  if [[ -n "${control_directory:-}" && -d "$control_directory" ]]; then
    rm -rf -- "$control_directory"
  fi
}
trap cleanup EXIT

archive_paths=()
while IFS= read -r -d '' top_level_path; do
  if [[ "$top_level_path" == .* && "$top_level_path" != ".well-known" ]]; then
    continue
  fi
  archive_paths+=("$top_level_path")
done < <(git -C "$repo_root" ls-tree -z --name-only "$expected_sha")
if [[ "${#archive_paths[@]}" -eq 0 ]]; then
  printf 'source revision has no publishable top-level paths\n' >&2
  exit 1
fi

git -C "$repo_root" archive --format=tar "$expected_sha" "${archive_paths[@]}" \
  | tar -xf - -C "$staging_directory"

denied_sentinels="$control_directory/cfets-denied-sentinels.txt"
rights_receipt="$control_directory/pages-rights-release-receipt.json"
env PYTHONDONTWRITEBYTECODE=1 python3 \
  "$staging_directory/ops/railway/verify_rights_clean.py" capture \
  --root "$staging_directory" \
  --output "$denied_sentinels"

publication_epoch="$(git -C "$repo_root" show -s --format=%ct "$expected_sha")"
palimpsest_admission_epoch="${PALIMPSEST_RAILWAY_ADMISSION_EPOCH:-$(date -u '+%s')}"
if [[ ! "$publication_epoch" =~ ^[0-9]+$ ]] \
  || [[ ! "$palimpsest_admission_epoch" =~ ^[0-9]+$ ]]; then
  printf 'Railway rights clocks must be whole Unix seconds\n' >&2
  exit 1
fi
if (( palimpsest_admission_epoch < publication_epoch )); then
  printf 'Railway rights admission clock precedes the publication edition\n' >&2
  exit 1
fi
rights_edition_at="$(python3 - "$publication_epoch" <<'PY'
from datetime import UTC, datetime
import sys
print(datetime.fromtimestamp(int(sys.argv[1]), tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))
PY
)"
rights_admission_at="$(python3 - "$palimpsest_admission_epoch" <<'PY'
from datetime import UTC, datetime
import sys
print(datetime.fromtimestamp(int(sys.argv[1]), tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))
PY
)"
rights_args=(
  --root "$staging_directory"
  --publication-sha "$expected_sha"
  --evaluated-at "$rights_edition_at"
  --admission-at "$rights_admission_at"
  --receipt "$rights_receipt"
)
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$staging_directory" \
  python3 -m scripts.stage_pages_rights "${rights_args[@]}"
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$staging_directory" \
  python3 -m scripts.stage_pages_rights "${rights_args[@]}" --check
env PYTHONDONTWRITEBYTECODE=1 python3 \
  "$staging_directory/ops/railway/verify_rights_clean.py" verify \
  --root "$staging_directory" \
  --sentinels "$denied_sentinels"

env PYTHONDONTWRITEBYTECODE=1 python3 \
  "$staging_directory/scripts/build_pages_wire_archive.py" \
  --root "$staging_directory" \
  --publication-sha "$expected_sha"
env PYTHONDONTWRITEBYTECODE=1 python3 \
  "$staging_directory/scripts/build_pages_wire_archive.py" \
  --root "$staging_directory" \
  --publication-sha "$expected_sha" \
  --check

env PYTHONDONTWRITEBYTECODE=1 python3 \
  "$staging_directory/ops/railway/build_release_manifest.py" \
  --root "$staging_directory" \
  --source-commit "$expected_sha" \
  --built-at "$rights_admission_at" >/dev/null

mv "$rights_receipt" "$final_rights_receipt"
mv "$staging_directory" "$output_parent/$output_name"
staging_directory=""
rm -- "$denied_sentinels"
rmdir "$control_directory"
control_directory=""
trap - EXIT
printf 'bundle=%s\nsource_commit=%s\nrights_receipt=%s\n' \
  "$output_parent/$output_name" "$expected_sha" "$final_rights_receipt"
