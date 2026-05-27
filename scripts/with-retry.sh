#!/usr/bin/env bash
#
# with-retry.sh CMD [ARGS...]
#
# Run CMD, retrying on transient failures with capped exponential backoff and
# jitter. Intended for flaky network operations in CI -- in particular GHCR
# `docker login` / `docker pull`, which regularly hit transient 5xx responses
# and token-endpoint timeouts that succeed on a second try.
#
# Tunables (environment variables):
#   RETRY_MAX_ATTEMPTS   total attempts before giving up        (default 5)
#   RETRY_TIMEOUT        per-attempt timeout, seconds           (default 300)
#   RETRY_NON_RETRYABLE  extended-regex; if the command output matches, fail
#                        immediately instead of retrying. Use for errors that
#                        will never succeed on retry (bad credentials, missing
#                        image/tag). Empty (default) retries every failure.
#
# Output of the final/successful attempt is forwarded to stdout; per-attempt
# diagnostics go to stderr.

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: with-retry.sh CMD [ARGS...]" >&2
  exit 2
fi

max_attempts="${RETRY_MAX_ATTEMPTS:-5}"
attempt_timeout="${RETRY_TIMEOUT:-300}"
non_retryable="${RETRY_NON_RETRYABLE:-}"

attempt=1
while :; do
  echo "with-retry: attempt ${attempt}/${max_attempts}: $*" >&2
  # Capture combined output so it can be classified, then forwarded.
  if out="$(timeout "${attempt_timeout}" "$@" 2>&1)"; then
    [ -n "${out}" ] && printf '%s\n' "${out}"
    exit 0
  fi
  printf '%s\n' "${out}" >&2

  if [ -n "${non_retryable}" ] && printf '%s' "${out}" | grep -qiE "${non_retryable}"; then
    echo "with-retry: non-retryable error; not retrying." >&2
    exit 1
  fi

  if [ "${attempt}" -ge "${max_attempts}" ]; then
    echo "with-retry: failed after ${max_attempts} attempts." >&2
    exit 1
  fi

  # Capped exponential backoff (10, 20, 40, 60, 60s ...) plus 0-5s jitter so a
  # matrix of jobs failing together does not retry in lockstep.
  backoff=$(( 10 * (2 ** (attempt - 1)) ))
  [ "${backoff}" -gt 60 ] && backoff=60
  sleep_for=$(( backoff + RANDOM % 6 ))
  echo "with-retry: retrying in ${sleep_for}s..." >&2
  sleep "${sleep_for}"

  attempt=$(( attempt + 1 ))
done
