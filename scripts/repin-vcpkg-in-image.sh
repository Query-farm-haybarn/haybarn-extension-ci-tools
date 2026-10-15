#!/usr/bin/env bash
# Haybarn: make a pulled pre-built build image honour a per-extension
# `vcpkg_commit`.
#
# The GHCR images bake /vcpkg as a single-commit fetch of the default vcpkg
# commit. The non-prebuilt path passes `vcpkg_commit` as a docker build-arg,
# but the prebuilt path used to ignore it, so an extension whose descriptor
# pins a different commit (e.g. lua, mssql) failed in `vcpkg install` with
#   failed to `git show` versions/baseline.json ... not in '<commit>'
# because that commit was never fetched into the image.
#
# If the image's /vcpkg HEAD already matches, this is a no-op. Otherwise it
# layers a fetch + checkout + re-bootstrap on top and re-tags the result under
# the same local name, so later `docker run <image>` steps pick it up.
#
# usage: repin-vcpkg-in-image.sh <image> <vcpkg_url> <vcpkg_commit>
set -euo pipefail

image="$1"
url="${2:-https://github.com/microsoft/vcpkg.git}"
commit="${3:-}"

if [ -z "$commit" ]; then
  echo "repin-vcpkg: no vcpkg_commit requested; keeping the image's vcpkg"
  exit 0
fi

have="$(docker run --rm --entrypoint git "$image" -C /vcpkg rev-parse HEAD)"
if [ "$have" = "$commit" ]; then
  echo "repin-vcpkg: image vcpkg already at $commit"
  exit 0
fi

echo "repin-vcpkg: image vcpkg is $have; re-pinning to $commit ($url)"
docker build -t "$image" - <<EOF
FROM $image
RUN cd /vcpkg \\
 && (git remote set-url origin '$url' || git remote add origin '$url') \\
 && git fetch --depth=1 origin '$commit' \\
 && git checkout -f '$commit' \\
 && ./bootstrap-vcpkg.sh -disableMetrics
EOF
