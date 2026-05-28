#!/usr/bin/env python3
"""Single source of truth for engine-version → bare semver normalization.

Inputs we see in CI:
  - clean semver:    '1.5.3'
  - tagged release:  'v1.5.3'
  - engine tag:      'haybarn-v1.5.3-rc3'

All must map to the bare semver core ('1.5.3') for use in:
  - R2 path segments     (s3://.../<dv>/<arch>/<ext>.duckdb_extension)
  - npm package names    (@haybarn/ext-<ext>-h<dv-dashed>[-<leaf>])
  - haybarn-metadata.json

Used both as a library (publish_extensions.py) and as a CLI from shell steps
in _extension_registry_publish.yml — keeping the regex in one place so the
'${DV#v}' trap (no-op on 'haybarn-…' strings, produced malformed npm names
in the May 2026 sweep) can't reappear in a fresh call-site.
"""

from __future__ import annotations

import re
import sys

_ENGINE_VERSION_RE = re.compile(r"^(?:haybarn-)?v?(\d+\.\d+\.\d+)(?:-rc\d+)?$")


def normalize_engine_version(raw: str) -> str:
    """'haybarn-v1.5.3-rc3' | 'v1.5.3' | '1.5.3' -> '1.5.3' (bare semver)."""
    m = _ENGINE_VERSION_RE.match(raw.strip())
    if not m:
        raise SystemExit(
            f"::error::unrecognized engine version {raw!r} — expected clean semver "
            f"like 'v1.5.3' or an engine tag like 'haybarn-v1.5.3-rc3'")
    return m.group(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: normalize_engine_version.py <engine-version>")
    print(normalize_engine_version(sys.argv[1]))
