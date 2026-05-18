#!/usr/bin/env python3
"""Emit a leaf @haybarn/ext-<name>-h<M>-<m>-<p>-<os>-<arch> package.json
into $STAGE.

Mirrors haybarn/tools/npm/build_leaf.py from the engine repo's CLI
publish flow. Run from the deploy workflow with these env vars set:

  STAGE             destination directory (already exists)
  PKG               full scoped package name,
                    e.g. @haybarn/ext-waddle-h1-5-2-linux-x64
  VERSION           npm semver, e.g. 0.0.2
  EXTENSION         extension name, e.g. waddle
  HAYBARN_VERSION   haybarn patch this leaf targets, e.g. 1.5.2
  OS                one of: linux, darwin, win32
  CPU               one of: x64, arm64
  LIBC              one of: glibc, musl, '-'  ('-' means omit the libc field)
  HAYBARN_METADATA  path to the unsigned haybarn-metadata.json blob to
                    embed in the leaf's package.json top-level "haybarn"
                    object
  LICENSE           (optional) SPDX license string for the package.json
                    `license` field. Defaults to "MIT" (matches the
                    Haybarn engine + core extensions). Community
                    extensions should pass their descriptor's `extension.license`
                    so npm metadata reflects the actual upstream license.
  EXTENSION_DESCRIPTION    (optional) one-line description from the
                           upstream descriptor — shown in the leaf's
                           generated README. Falls back to a generic blurb.
  EXTENSION_SOURCE_REPO    (optional) upstream extension's source repo
                           URL (e.g. https://github.com/<owner>/<repo>),
                           shown as a link in the leaf README. Empty →
                           omitted.

The leaf carries a single .duckdb_extension binary in bin/. Platform-
pinned via npm's `os`/`cpu`/`libc` so npm installs only the matching leaf.

Trust on npm: the binary's existing RSA signature (verified by the
haybarn engine at dlopen) + npm's per-(name,version) immutability.
"""

import json
import os
import pathlib
import sys


# Stable Haybarn icon URL — public-readable raw GitHub asset on the
# org's `.github` profile repo. If this is ever moved, update both
# npm_build_leaf.py and npm_build_meta.py in lockstep.
HAYBARN_ICON_URL = (
    "https://raw.githubusercontent.com/Query-farm-haybarn/.github/"
    "haybarn/profile/assets/haybarn-icon.png"
)
HAYBARN_REPO_URL = "https://github.com/Query-farm-haybarn/haybarn"
HAYBARN_COMMUNITY_REPO_URL = (
    "https://github.com/Query-farm-haybarn/haybarn-community-extensions"
)


def render_leaf_readme(
    pkg: str,
    extension: str,
    haybarn_version: str,
    meta_pkg: str,
    host_os: str,
    host_cpu: str,
    host_libc: str,
    ext_source_repo: str,
    is_community: bool,
) -> str:
    """Short README for the per-platform leaf — points users at the
    meta-package and gives upstream / project context."""
    plat = f"{host_os}/{host_cpu}"
    if host_libc != "-":
        plat += f" ({host_libc})"

    parts = [
        f'<p align="center">',
        f'  <img src="{HAYBARN_ICON_URL}" alt="Haybarn" width="96" height="96">',
        f"</p>",
        "",
        f"# `{pkg}`",
        "",
        f"Platform-specific binary for the **{extension}** extension on **{plat}**, "
        f"built against [Haybarn]({HAYBARN_REPO_URL}) **{haybarn_version}**.",
        "",
        "## You probably don't want to install this directly",
        "",
        f"Install the meta-package instead — npm will resolve to exactly the "
        f"matching leaf for your platform:",
        "",
        f"```sh",
        f"npm install {meta_pkg}",
        f"```",
        "",
        "## Links",
        "",
        f"- [Haybarn]({HAYBARN_REPO_URL}) — the engine",
    ]
    if is_community:
        parts.append(
            f"- [Haybarn community extensions]({HAYBARN_COMMUNITY_REPO_URL}) "
            f"— the catalog this leaf was built and published from"
        )
    if ext_source_repo:
        parts.append(f"- [Extension source]({ext_source_repo}) — upstream of `{extension}`")
    parts += [
        "",
        "## Trademark",
        "",
        "Haybarn is an independent derived distribution of DuckDB published by "
        "[Query Farm LLC](https://query.farm). Not affiliated with or endorsed by "
        "the DuckDB Foundation. DuckDB is a trademark of the DuckDB Foundation.",
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    try:
        stage = pathlib.Path(os.environ["STAGE"])
        pkg = os.environ["PKG"]
        version = os.environ["VERSION"]
        extension = os.environ["EXTENSION"]
        haybarn_version = os.environ["HAYBARN_VERSION"]
        host_os = os.environ["OS"]
        host_cpu = os.environ["CPU"]
        host_libc = os.environ["LIBC"]
        metadata_path = pathlib.Path(os.environ["HAYBARN_METADATA"])
    except KeyError as e:
        print(f"npm_build_leaf: missing env var {e}", file=sys.stderr)
        return 2

    if not metadata_path.is_file():
        print(f"npm_build_leaf: HAYBARN_METADATA {metadata_path} not found",
              file=sys.stderr)
        return 2

    metadata_blob = json.loads(metadata_path.read_text())

    desc = (
        f"Haybarn extension {extension!r} for {host_os}/{host_cpu}"
    )
    if host_libc != "-":
        desc += f" ({host_libc})"
    desc += (
        f". Built against haybarn {haybarn_version}. Installed automatically "
        f"by the @haybarn/ext-{extension}-h"
        f"{haybarn_version.replace('.', '-')} meta-package; "
        "use that, not this leaf, in your dependencies."
    )

    # Repository URL must match the GitHub repo the npm provenance
    # attestation was signed from, otherwise npm rejects publish with
    # E422 "Error verifying sigstore provenance bundle". GITHUB_REPOSITORY
    # is set automatically by GitHub Actions to the right slug for whichever
    # repo is running this workflow (haybarn / haybarn-community-extensions /
    # any build-fork). Fallback for local testing.
    repo_slug = os.environ.get("GITHUB_REPOSITORY",
                               "Query-farm-haybarn/haybarn-community-extensions")
    repo_url = f"https://github.com/{repo_slug}"

    # "Is this a community extension?" — heuristic on the running repo
    # slug. Community-extensions publishes generate the slug
    # `Query-farm-haybarn/haybarn-community-extensions`; the engine repo
    # publishes core extensions from `Query-farm-haybarn/haybarn`. We
    # surface this in the README to add the catalog link only where it
    # applies.
    is_community = repo_slug.endswith("/haybarn-community-extensions")

    ext_source_repo = os.environ.get("EXTENSION_SOURCE_REPO", "").strip()

    spec = {
        "name": pkg,
        "version": version,
        "description": desc,
        "homepage": repo_url,
        "repository": {
            "type": "git",
            "url": f"git+{repo_url}.git",
        },
        # `or "MIT"` (not `.get(..., "MIT")`) because workflow callers
        # often pass LICENSE="" rather than unset; we want an empty string
        # to fall back to the engine default, not propagate.
        "license": os.environ.get("LICENSE") or "MIT",
        "os": [host_os],
        "cpu": [host_cpu],
        # bin/ + metadata are the runtime artifacts; README.md goes
        # along for npm's web UI rendering. Without listing README.md
        # here npm still picks it up by convention, but explicit is
        # documentation.
        "files": ["bin", "haybarn-metadata.json", "README.md"],
        # Top-level "haybarn" object — npm preserves arbitrary fields
        # and exposes them via `npm view`. This is the discovery
        # validation gate; without this block, the engine-side scan
        # ignores the directory.
        "haybarn": metadata_blob,
        "publishConfig": {"access": "public"},
    }
    if host_libc != "-":
        spec["libc"] = [host_libc]

    out = stage / "package.json"
    out.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"wrote {out}")

    # Generate README — derive the meta-package name from the leaf name
    # by stripping the trailing platform slug. Leaf names always have
    # the shape `<meta>-<os>-<cpu>[-musl]` per the deploy workflow.
    meta_pkg = pkg
    for suffix in ("-musl",):
        if meta_pkg.endswith(suffix):
            meta_pkg = meta_pkg[: -len(suffix)]
            break
    for cpu_suffix in ("-x64", "-arm64"):
        if meta_pkg.endswith(cpu_suffix):
            meta_pkg = meta_pkg[: -len(cpu_suffix)]
            break
    for os_suffix in ("-linux", "-darwin", "-win32"):
        if meta_pkg.endswith(os_suffix):
            meta_pkg = meta_pkg[: -len(os_suffix)]
            break
    readme = render_leaf_readme(
        pkg=pkg, extension=extension, haybarn_version=haybarn_version,
        meta_pkg=meta_pkg, host_os=host_os, host_cpu=host_cpu,
        host_libc=host_libc, ext_source_repo=ext_source_repo,
        is_community=is_community,
    )
    readme_out = stage / "README.md"
    readme_out.write_text(readme)
    print(f"wrote {readme_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
