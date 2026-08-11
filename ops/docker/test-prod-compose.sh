#!/usr/bin/env bash
set -Eeuo pipefail

root="$(mktemp -d "${TMPDIR:-/tmp}/palimpsest-compose-test.XXXXXX")"
trap 'rm -rf -- "$root"' EXIT
mkdir -p "$root/bin"
touch "$root/node.env"

cat >"$root/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$PALIMPSEST_ENV_FILE"
printf '%s\n' "$@"
SH
chmod +x "$root/bin/docker"

output="$(
  PATH="$root/bin:$PATH" PALIMPSEST_ENV_FILE="$root/node.env" \
    ops/docker/prod-compose --profile collectors config
)"

grep -Fxq "$root/node.env" <<<"$output"
grep -Fxq -- "--env-file" <<<"$output"
grep -Fxq "$root/node.env" <<<"$output"
grep -Fxq -- "--project-name" <<<"$output"
grep -Fxq "palimpsest" <<<"$output"
grep -Fxq -- "-f" <<<"$output"
grep -Fxq -- "--profile" <<<"$output"
grep -Fxq "collectors" <<<"$output"
grep -Fxq "config" <<<"$output"

if PATH="$root/bin:$PATH" PALIMPSEST_ENV_FILE="$root/missing.env" \
  ops/docker/prod-compose config >/dev/null 2>&1; then
  printf 'FAIL: wrapper accepted a missing environment file\n' >&2
  exit 1
fi

printf 'PASS: production Compose wrapper pins one environment file\n'
