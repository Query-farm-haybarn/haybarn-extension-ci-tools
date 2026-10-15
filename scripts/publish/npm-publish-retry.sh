#!/usr/bin/env bash
# Haybarn: `npm publish --provenance --access public --tag latest` in the
# current directory, retrying when npm rate-limits us.
#
# Each Haybarn release publishes a fresh set of package *names*
# (@haybarn/ext-<name>-h<M>-<m>-<p>-<os>-<arch>), so a community republish
# creates thousands of new packages and npm answers some of them with
# HTTP 429. Those used to be reported as hard failures. Retries back off
# exponentially (30s, 60s, 120s, 240s, then 300s), with jitter so parallel
# publishers don't retry in lockstep. Any other error is returned as-is, so
# callers keep their own "previously published" -> SKIP handling. stderr from
# the final attempt is passed through.
#
# Tunables: NPM_PUBLISH_RETRIES (default 6 attempts).
set -uo pipefail

attempts="${NPM_PUBLISH_RETRIES:-6}"
delay=30
err="$(mktemp)"
trap 'rm -f "$err"' EXIT

for ((i = 1; i <= attempts; i++)); do
  if npm publish --provenance --access public --tag latest "$@" 2>"$err"; then
    cat "$err" >&2
    exit 0
  fi
  if ! grep -qiE 'E429|429 Too Many Requests|rate limit|ETIMEDOUT|ECONNRESET|EAI_AGAIN' "$err" || ((i == attempts)); then
    cat "$err" >&2
    exit 1
  fi
  sleep_for=$((delay + RANDOM % 15))
  echo "npm-publish-retry: attempt $i/$attempts rate-limited or transient; retrying in ${sleep_for}s" >&2
  sleep "$sleep_for"
  delay=$((delay * 2 > 300 ? 300 : delay * 2))
done
