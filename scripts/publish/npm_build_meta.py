#!/usr/bin/env python3
"""Emit the @haybarn/ext-<name>-h<M>-<m>-<p> meta-package's package.json
into $STAGE.

Mirrors haybarn/tools/npm/build_meta.py from the engine repo. Run from
the deploy workflow with:

  STAGE             destination directory (already exists)
  PKG               meta package name, e.g. @haybarn/ext-waddle-h1-5-2
  VERSION           npm semver, e.g. 0.0.2
  EXTENSION         extension name, e.g. waddle
  HAYBARN_VERSION   haybarn patch this meta targets, e.g. 1.5.2
  HAYBARN_METADATA  path to the unsigned haybarn-metadata.json blob
                    (embedded as the top-level "haybarn" object)
  PRESENT_LEAVES    comma-separated list of leaf-package basenames that
                    actually got built this run (used to filter
                    optionalDependencies — leaves whose build failed get
                    dropped rather than declared with a missing version).
                    Each entry is just the leaf slug, e.g.
                    "linux-x64,linux-arm64,darwin-arm64".
  LICENSE           (optional) SPDX license string for the package.json
                    `license` field. Defaults to "MIT" (engine + core).
                    Community extensions pass their descriptor's
                    `extension.license` so npm metadata reflects the
                    actual upstream license rather than misattributing it.
  EXTENSION_DESCRIPTION    (optional) one-line description from the
                           upstream descriptor — included in the README.
                           Falls back to a generic blurb.
  EXTENSION_SOURCE_REPO    (optional) upstream extension's source repo
                           URL (e.g. https://github.com/<owner>/<repo>),
                           shown as a link in the meta README. Empty →
                           omitted.

The meta declares per-platform leaves under optionalDependencies, plus
an exact peerDependency on haybarn matching the package-name suffix.
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


def render_meta_readme(
    pkg: str,
    version: str,
    extension: str,
    haybarn_version: str,
    extension_description: str,
    ext_source_repo: str,
    license_id: str,
    present_leaves: list,
    wasm_leaves: list,
    is_community: bool,
) -> str:
    """Rich README for the user-facing meta-package."""
    install_block = (
        f"```sh\n"
        f"npm install {pkg}\n"
        f"# or use the exact version:\n"
        f"npm install {pkg}@{version}\n"
        f"```"
    )
    leaves_md = "\n".join(
        f"- `@haybarn/ext-{extension}-h{haybarn_version.replace('.', '-')}-{slug}`"
        for slug in present_leaves
    ) or "_(no platform leaves were built this run)_"

    parts = [
        '<p align="center">',
        f'  <img src="{HAYBARN_ICON_URL}" alt="Haybarn" width="120" height="120">',
        '</p>',
        '',
        f"# Haybarn extension: `{extension}`",
        '',
        extension_description or
        f"The `{extension}` extension for Haybarn — built and signed against "
        f"Haybarn {haybarn_version}, distributed on npm as a single meta-package "
        f"that resolves to the correct per-platform binary at install time.",
        '',
    ]
    # Hoist source + catalog links high — these are the entry points readers
    # actually want, not the install snippet.
    if ext_source_repo:
        parts += [
            f"> **Source:** [{ext_source_repo}]({ext_source_repo})  ",
        ]
        if is_community:
            parts.append(
                f"> **Catalog:** [Haybarn community extensions]"
                f"({HAYBARN_COMMUNITY_REPO_URL})"
            )
        parts.append('')
    elif is_community:
        parts += [
            f"> **Catalog:** [Haybarn community extensions]"
            f"({HAYBARN_COMMUNITY_REPO_URL})",
            '',
        ]
    parts += [
        '## Install',
        '',
        install_block,
        '',
        f"npm picks the matching platform binary from the leaves below via "
        f"its `os` / `cpu` / `libc` fields. No postinstall scripts, no network "
        f"calls after `npm install`.",
        '',
        '## Available platforms (this version)',
        '',
        f"Installed automatically by `npm install {pkg}` (npm picks the one "
        f"matching your `os`/`cpu`/`libc`):" if present_leaves else
        "_(no native platform leaves were built this run)_",
        '',
        leaves_md,
        '',
    ]
    # WebAssembly builds: npm can't auto-select them (no wasm os/cpu), so they
    # are separate packages you install by name and hand to duckdb-wasm.
    if wasm_leaves:
        wasm_pkgs = [
            f"@haybarn/ext-{extension}-h{haybarn_version.replace('.', '-')}-{slug}"
            for slug in wasm_leaves
        ]
        first = wasm_pkgs[0]
        parts += [
            '## WebAssembly (duckdb-wasm)',
            '',
            "Also built for [duckdb-wasm](https://github.com/duckdb/duckdb-wasm). "
            "These are **not** installed by the meta above (npm has no wasm "
            "`os`/`cpu` to match) — install the variant matching your duckdb-wasm "
            "bundle (`mvp` / `eh` / `threads`) and point duckdb-wasm at the bundled "
            "asset:",
            '',
            "\n".join(f"- `{p}`" for p in wasm_pkgs),
            '',
            '```sh',
            f"npm install {first}",
            '```',
            '```js',
            f"import extUrl from '{first}/bin/{extension}.duckdb_extension.wasm?url';",
            f"await conn.query(`INSTALL {extension} FROM '${{extUrl}}'`);",
            f"await conn.query(`LOAD {extension}`);",
            '```',
            '',
        ]
    parts += [
        '## Use it',
        '',
        f"Once installed, the `.duckdb_extension` binary lands in your project's "
        f"`node_modules/` tree under the matching leaf. The "
        f"[Haybarn]({HAYBARN_REPO_URL}) engine auto-discovers it at startup; "
        f"from a Haybarn SQL session:",
        '',
        '```sql',
        # `LOAD` takes an identifier, not a quoted string — match DuckDB
        # syntax exactly so the snippet copy-pastes cleanly.
        f"LOAD {extension};",
        '```',
        '',
        '## License',
        '',
        (f"The `{extension}` extension is distributed under **{license_id}**. "
         "The Haybarn engine itself is MIT-licensed."),
        '',
        '## Trademark',
        '',
        "Haybarn is an independent derived distribution of DuckDB published by "
        "[Query Farm LLC](https://query.farm). Not affiliated with or endorsed by "
        "the DuckDB Foundation. DuckDB is a trademark of the DuckDB Foundation.",
        '',
    ]
    return "\n".join(parts)


def main() -> int:
    try:
        stage = pathlib.Path(os.environ["STAGE"])
        pkg = os.environ["PKG"]
        version = os.environ["VERSION"]
        extension = os.environ["EXTENSION"]
        haybarn_version = os.environ["HAYBARN_VERSION"]
        metadata_path = pathlib.Path(os.environ["HAYBARN_METADATA"])
        present = os.environ.get("PRESENT_LEAVES", "")
    except KeyError as e:
        print(f"npm_build_meta: missing env var {e}", file=sys.stderr)
        return 2

    if not metadata_path.is_file():
        print(f"npm_build_meta: HAYBARN_METADATA {metadata_path} not found",
              file=sys.stderr)
        return 2

    metadata_blob = json.loads(metadata_path.read_text())

    present_leaves = [p.strip() for p in present.split(",") if p.strip()]
    wasm_leaves = [p.strip() for p in os.environ.get("WASM_LEAVES", "").split(",") if p.strip()]
    if not present_leaves and not wasm_leaves:
        print("npm_build_meta: no PRESENT_LEAVES/WASM_LEAVES — meta with no leaves "
              "would be useless. Aborting rather than publishing a broken meta.",
              file=sys.stderr)
        return 2

    # Only NATIVE leaves become optionalDependencies (npm auto-resolves them via
    # os/cpu). wasm leaves are documented in the README but never auto-installed.
    optional = {f"{pkg}-{slug}": version for slug in present_leaves}

    # See npm_build_leaf.py — repository URL has to match the source
    # repo from the npm provenance attestation.
    repo_slug = os.environ.get("GITHUB_REPOSITORY",
                               "Query-farm-haybarn/haybarn-community-extensions")
    repo_url = f"https://github.com/{repo_slug}"
    is_community = repo_slug.endswith("/haybarn-community-extensions")

    license_id = os.environ.get("LICENSE") or "MIT"
    extension_description = os.environ.get("EXTENSION_DESCRIPTION", "").strip()
    ext_source_repo = os.environ.get("EXTENSION_SOURCE_REPO", "").strip()

    # See npm_build_leaf.py for the rationale: `repository` stays pointed
    # at the GitHub repo where the build ran (npm provenance requirement),
    # but `homepage` + `bugs` go to the upstream extension repo when we
    # know it, so npmjs.com attributes the package to its actual author.
    homepage_url = ext_source_repo or repo_url
    bugs_url = f"{ext_source_repo}/issues" if ext_source_repo else f"{repo_url}/issues"

    spec = {
        "name": pkg,
        "version": version,
        "description": (
            extension_description or
            f"Haybarn extension {extension!r} — built against haybarn "
            f"{haybarn_version}. Install this meta-package; npm will pull "
            "only the binary leaf matching your platform."
        ),
        "homepage": homepage_url,
        "bugs": {"url": bugs_url},
        "repository": {
            "type": "git",
            "url": f"git+{repo_url}.git",
        },
        # `or "MIT"` (not `.get(..., "MIT")`) because workflow callers
        # often pass LICENSE="" rather than unset; we want an empty string
        # to fall back to the engine default, not propagate.
        "license": license_id,
        "keywords": ["haybarn", "duckdb", "extension", extension],
        # peerDependencies intentionally omitted for now. The `haybarn`
        # package on npm only has pre-release versions (1.5.2-rcN); a
        # strict pin to "1.5.2" would warn against an unsatisfiable
        # version on every install. The package-name suffix
        # `-h<M>-<m>-<p>` still encodes the haybarn ABI; the engine
        # verifies the RSA signature on load.
        "optionalDependencies": optional,
        "haybarn": metadata_blob,
        # README.md ships with the package so npm's web UI renders the
        # logo + description + repo links the user installs against.
        "files": ["README.md"],
        "publishConfig": {"access": "public"},
    }

    out = stage / "package.json"
    out.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"wrote {out}")

    readme = render_meta_readme(
        pkg=pkg, version=version, extension=extension,
        haybarn_version=haybarn_version,
        extension_description=extension_description,
        ext_source_repo=ext_source_repo,
        license_id=license_id, present_leaves=present_leaves,
        wasm_leaves=wasm_leaves,
        is_community=is_community,
    )
    readme_out = stage / "README.md"
    readme_out.write_text(readme)
    print(f"wrote {readme_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
