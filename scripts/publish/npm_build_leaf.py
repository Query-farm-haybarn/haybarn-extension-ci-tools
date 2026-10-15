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
HAYBARN_SITE_URL = "https://query.farm/products/haybarn/"
HAYBARN_EXT_INSTALL_URL = "https://query.farm/products/haybarn/extensions/install"
# Query.Farm wordmark from the site's media kit (dark text, for npm's light UI).
QUERY_FARM_URL = "https://query.farm"
QUERY_FARM_WORDMARK_URL = "https://query.farm/media-kit/logo/wordmark-light.png"
# One-paragraph "what is Haybarn" lead shared by every README, so a reader
# landing on any package page learns what Haybarn is before what the
# package is. Mirrors the query.farm/products/haybarn description.
HAYBARN_INTRO = (
    f"**[Haybarn]({HAYBARN_SITE_URL})** is a derived distribution of DuckDB "
    f"from [Query.Farm]({QUERY_FARM_URL}): the same SQL, APIs and database "
    "files, with signed releases, rebuilt and signed extensions, engine "
    "improvements, and supply-chain review built in."
)


def branded_header(icon_size: int) -> list:
    """Haybarn icon + Query.Farm wordmark, centred, both linked."""
    return [
        '<p align="center">',
        f'  <a href="{HAYBARN_SITE_URL}"><img src="{HAYBARN_ICON_URL}" '
        f'alt="Haybarn" width="{icon_size}" height="{icon_size}"></a>',
        '</p>',
        '<p align="center">',
        f'  <a href="{QUERY_FARM_URL}"><img src="{QUERY_FARM_WORDMARK_URL}" '
        'alt="Query.Farm" height="28"></a>',
        '</p>',
        '',
    ]
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

    meta_link = f"[`{meta_pkg}`](https://www.npmjs.com/package/{meta_pkg})"
    parts = branded_header(96) + [
        f"# `{pkg}`",
        "",
        f"> **Part of {meta_link}** — install that package, not this one.",
        "",
        HAYBARN_INTRO,
        "",
        f"This is the **{plat}** binary for the **`{extension}`** extension, "
        f"built against Haybarn **{haybarn_version}**. npm installs it "
        f"automatically as an optional dependency of {meta_link} on matching "
        "platforms.",
        "",
    ]
    if ext_source_repo:
        parts += [
            f"> **Source:** [{ext_source_repo}]({ext_source_repo})",
            "",
        ]
    parts += [
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
        f"- {meta_link} — the parent package for this extension",
        f"- [Haybarn]({HAYBARN_SITE_URL}) — what Haybarn is and how to install it",
        f"- [Haybarn engine source]({HAYBARN_REPO_URL})",
    ]
    if is_community:
        parts.append(
            f"- [Haybarn community extensions]({HAYBARN_COMMUNITY_REPO_URL}) "
            f"— the catalog this leaf was built and published from"
        )
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


def render_wasm_leaf_readme(
    pkg: str,
    extension: str,
    haybarn_version: str,
    variant: str,
    meta_pkg: str,
    ext_source_repo: str,
    is_community: bool,
) -> str:
    """README for a WebAssembly leaf — npm can't auto-select it (no wasm
    os/cpu), so this is installed explicitly and pointed at by duckdb-wasm."""
    bin_path = f"{pkg}/bin/{extension}.duckdb_extension.wasm"
    meta_link = f"[`{meta_pkg}`](https://www.npmjs.com/package/{meta_pkg})"
    parts = branded_header(96) + [
        f"# `{pkg}`",
        "",
        f"> **Part of {meta_link}** — the `{extension}` extension's parent package.",
        "",
        HAYBARN_INTRO,
        "",
        f"This is the WebAssembly (**{variant}**) build of the **`{extension}`** "
        f"extension, built against Haybarn **{haybarn_version}** for use with "
        "[Haybarn-Wasm](https://www.npmjs.com/package/@haybarn/haybarn-wasm) / "
        "duckdb-wasm.",
        "",
    ]
    if ext_source_repo:
        parts += [f"> **Source:** [{ext_source_repo}]({ext_source_repo})", ""]
    parts += [
        "## Why this package",
        "",
        "Lets you **bundle / self-host / version-pin** the wasm extension instead "
        "of fetching it from the Haybarn CDN at runtime — handy for offline, "
        "CSP-restricted, or air-gapped duckdb-wasm apps.",
        "",
        "Pick the variant matching the duckdb-wasm bundle your app loads: "
        "`mvp`, `eh`, or `threads`.",
        "",
        "## Install",
        "",
        "```sh",
        f"npm install {pkg}",
        "```",
        "",
        "## Use it (duckdb-wasm)",
        "",
        "Resolve the bundled `.wasm` to a URL and point duckdb-wasm at it. With a "
        "bundler (Vite shown; webpack/esbuild use `new URL(..., import.meta.url)`):",
        "",
        "```js",
        f"import extUrl from '{bin_path}?url';",
        "const conn = await db.connect();",
        f"await conn.query(`INSTALL {extension} FROM '${{extUrl}}'`);",
        f"await conn.query(`LOAD {extension}`);",
        "```",
        "",
        "> Exact extension-loading API depends on your duckdb-wasm version; the key "
        "point is that `extUrl` is a local, bundler-emitted asset, not a CDN fetch. "
        "See the [duckdb-wasm docs](https://github.com/duckdb/duckdb-wasm).",
        "",
        f"For automatic, native (non-wasm) installs use the meta-package "
        f"{meta_link}.",
        "",
        "## Links",
        "",
        f"- {meta_link} — the parent package for this extension",
        f"- [Haybarn]({HAYBARN_SITE_URL}) — what Haybarn is and how to install it",
        "",
        "## Trademark",
        "",
        "Haybarn is an independent derived distribution of DuckDB published by "
        "[Query Farm LLC](https://query.farm). Not affiliated with or endorsed by "
        "the DuckDB Foundation. DuckDB is a trademark of the DuckDB Foundation.",
        "",
    ]
    return "\n".join(parts)


def _write_wasm_leaf(stage: pathlib.Path, pkg: str, version: str, extension: str,
                     haybarn_version: str, variant: str, metadata_blob: dict) -> int:
    """Emit a wasm leaf package.json + README. No os/cpu/libc fields — npm has
    no wasm target, so this is NOT a meta optionalDependency; it's installed by
    name and handed to duckdb-wasm as a bundled asset."""
    repo_slug = os.environ.get("GITHUB_REPOSITORY",
                               "Query-farm-haybarn/haybarn-community-extensions")
    repo_url = f"https://github.com/{repo_slug}"
    is_community = repo_slug.endswith("/haybarn-community-extensions")
    ext_source_repo = os.environ.get("EXTENSION_SOURCE_REPO", "").strip()
    homepage_url = ext_source_repo or repo_url
    bugs_url = f"{ext_source_repo}/issues" if ext_source_repo else f"{repo_url}/issues"
    meta_pkg = f"@haybarn/ext-{extension}-h{haybarn_version.replace('.', '-')}"

    spec = {
        "name": pkg,
        "version": version,
        "description": (
            f"WebAssembly ({variant}) build of the Haybarn extension {extension!r} "
            f"for duckdb-wasm. Built against haybarn {haybarn_version}. Pick the "
            f"variant (mvp/eh/threads) matching your duckdb-wasm bundle."),
        "homepage": homepage_url,
        "bugs": {"url": bugs_url},
        "repository": {"type": "git", "url": f"git+{repo_url}.git"},
        "license": os.environ.get("LICENSE") or "MIT",
        "keywords": ["haybarn", "duckdb", "duckdb-wasm", "wasm", "extension", extension],
        # Intentionally NO os/cpu/libc — npm can't target wasm, so this leaf is
        # installed explicitly (not auto-resolved by the meta).
        "files": ["bin", "haybarn-metadata.json", "README.md"],
        "haybarn": metadata_blob,
        "publishConfig": {"access": "public"},
    }
    (stage / "package.json").write_text(json.dumps(spec, indent=2) + "\n")
    print(f"wrote {stage / 'package.json'} (wasm {variant})")
    (stage / "README.md").write_text(render_wasm_leaf_readme(
        pkg=pkg, extension=extension, haybarn_version=haybarn_version,
        variant=variant, meta_pkg=meta_pkg, ext_source_repo=ext_source_repo,
        is_community=is_community))
    print(f"wrote {stage / 'README.md'}")
    return 0


def main() -> int:
    try:
        stage = pathlib.Path(os.environ["STAGE"])
        pkg = os.environ["PKG"]
        version = os.environ["VERSION"]
        extension = os.environ["EXTENSION"]
        haybarn_version = os.environ["HAYBARN_VERSION"]
        metadata_path = pathlib.Path(os.environ["HAYBARN_METADATA"])
    except KeyError as e:
        print(f"npm_build_leaf: missing env var {e}", file=sys.stderr)
        return 2

    if not metadata_path.is_file():
        print(f"npm_build_leaf: HAYBARN_METADATA {metadata_path} not found",
              file=sys.stderr)
        return 2

    metadata_blob = json.loads(metadata_path.read_text())

    # WebAssembly leaves take a distinct shape (no os/cpu/libc, wasm README).
    wasm_variant = os.environ.get("WASM_VARIANT", "").strip()
    if wasm_variant:
        return _write_wasm_leaf(stage, pkg, version, extension,
                                haybarn_version, wasm_variant, metadata_blob)

    try:
        host_os = os.environ["OS"]
        host_cpu = os.environ["CPU"]
        host_libc = os.environ["LIBC"]
    except KeyError as e:
        print(f"npm_build_leaf: missing env var {e}", file=sys.stderr)
        return 2

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

    # `repository.url` MUST stay pointed at the GitHub repo this build ran
    # in — that's what the npm provenance attestation references, and a
    # mismatch causes publish to fail with E422. So community packages
    # keep `repository` pointing at haybarn-community-extensions. The
    # `homepage` (most prominent link on npmjs.com) AND `bugs` go to the
    # upstream extension repo when we know it — extension authors get the
    # attribution they deserve, users land at the right place to file
    # bugs or read upstream docs.
    homepage_url = ext_source_repo or repo_url
    bugs_url = f"{ext_source_repo}/issues" if ext_source_repo else f"{repo_url}/issues"

    spec = {
        "name": pkg,
        "version": version,
        "description": desc,
        "homepage": homepage_url,
        "bugs": {"url": bugs_url},
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
