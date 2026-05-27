#!/usr/bin/env python3
"""Generic extension publisher — drives both core (engine repo) and community
(haybarn-community-extensions) deploys from one code path.

Iterate an assembled extension repository tree, apply the per-commit immutable
R2 layout (signed binary + signed manifest + signed current.json pointer),
optionally write the mutable "latest" path, and stage pip wheel + npm leaf
(+ npm meta in bundled mode) artifacts for the downstream registry-publish jobs.

Tree layout under --repo-dir:
    <duckdb_version>/<arch>/<ext>.duckdb_extension(.wasm)

Core feeds the full ~17 extensions × ~10 platforms tree; community feeds a
one-extension tree. The work fans out across a thread pool either way.

History: this is the generalization of the former publish_core_extensions.py.
Core publishing is preserved bit-for-bit by the argument defaults below
(--ext-commit defaults to --engine-commit, --artifact-shape defaults to
bundled, --write-latest off, no --ext-version-label). The new optional args
let the community workflow pass per-extension metadata, the per-extension
artifact shape, and the mutable "latest" path that core handles via the
separate haybarn_extension_upload.py.

Inputs:
  --repo-dir          assembled repo, e.g. /tmp/haybarn-repository
  --engine-commit     40-char engine SHA (manifest's haybarn-engine-commit)
  --engine-commit-ts  engine commit unix timestamp (UTC) [optional]
  --ext-commit        per-extension source SHA (default: --engine-commit)
  --ext-commit-ts     per-extension commit ts for CalVer (default: engine ts)
  --ext-version-label descriptor semver; wins over CalVer when set
  --version           explicit version override (skips CalVer/semver derivation)
  --haybarn-version   engine version or tag (1.5.3 / v1.5.3 / haybarn-v1.5.3-rc3);
                      normalized to bare semver for the npm suffix + peer pin
  --duckdb-version    vestigial — the R2 path segment is derived from the
                      assembled <repo-dir>/<version>/ directory name (also
                      normalized to vX.Y.Z), not from this arg
  --r2-bucket         R2 bucket name
  --r2-prefix         e.g. core / community
  --license           path to LICENSE file (embedded in wheels)
  --description       one-line extension description (npm leaf/meta)
  --source-repo       upstream extension repo URL (npm leaf/meta)
  --dist-dir          staging dir for pip wheels + npm package dirs/tarballs
  --signed-by         identity name (forward hook for trust-store)
  --channels          comma-separated, e.g. r2-core,pypi,npm — gates which
                      registry artifacts are built ('pypi' → wheel, 'npm' → leaf)
  --artifact-shape    bundled (core: npm-leaves/ + npm-metas/ dirs) |
                      per-extension (community: dist/npm/<leaf>.tar.gz, no meta)
  --write-latest      also stage + sync the mutable <dv>/<arch>/<ext>.gz latest path
  --deploy            when 'true', actually upload to R2; otherwise dry-run

Env (required when --deploy true):
  DUCKDB_EXTENSION_SIGNING_PK   PEM private key for binary RSA sign
  HAYBARN_GPG_PRIVATE_KEY       GPG key for manifest signing
  HAYBARN_GPG_PASSPHRASE
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_ENDPOINT_URL /
    AWS_DEFAULT_REGION / AWS_REQUEST_CHECKSUM_CALCULATION /
    AWS_RESPONSE_CHECKSUM_VALIDATION
  PUBLISH_CONCURRENCY           thread pool size (default: min(16, cpus*4))
  PUBLISH_SYNC_CONCURRENCY      aws s3 sync transfer concurrency (default 32)
  EXTENSION_UPLOAD_CACHE_CONTROL_LATEST   mutable latest/pointer Cache-Control
  EXTENSION_UPLOAD_CACHE_CONTROL_VERSIONED  immutable per-commit Cache-Control

Staging output (always written):
  <dist-dir>/pypi/<wheel-name>.whl                      (when 'pypi' in channels)
  bundled:        <dist-dir>/npm-leaves/<leaf-pkg>/  + <dist-dir>/npm-metas/<meta-pkg>/
  per-extension:  <dist-dir>/npm/<leaf-pkg>.tar.gz   (when 'npm' in channels)
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils

try:
    from tqdm import tqdm
except ImportError:  # progress bar is optional
    tqdm = None

SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()

SIGNATURE_SIZE = 256
CHUNK = 1024 * 1024  # 1 MiB — must match the engine verifier (extension_load.cpp)

# Cache-Control defaults mirror extension-upload-single.sh: latest paths are
# mutable (short TTL so the edge serves the new pointer/binary within seconds),
# per-commit paths are immutable (cache for ever).
DEFAULT_CC_LATEST = "public, max-age=10, must-revalidate"
DEFAULT_CC_VERSIONED = "public, max-age=31536000, immutable"

# gpg-agent serializes poorly under concurrent signing; guard the gpg calls.
_GPG_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(msg, flush=True)

# duckdb_arch → (PyPI platform tag, npm os, npm cpu, npm libc, leaf suffix)
PLATMAP: dict[str, tuple[str, str, str, str, str]] = {
    "linux_amd64":       ("manylinux_2_28_x86_64",  "linux",  "x64",   "glibc", "linux-x64"),
    "linux_arm64":       ("manylinux_2_28_aarch64", "linux",  "arm64", "glibc", "linux-arm64"),
    "linux_amd64_musl":  ("musllinux_1_2_x86_64",   "linux",  "x64",   "musl",  "linux-x64-musl"),
    "linux_arm64_musl":  ("musllinux_1_2_aarch64",  "linux",  "arm64", "musl",  "linux-arm64-musl"),
    "osx_amd64":         ("macosx_11_0_x86_64",     "darwin", "x64",   "-",     "darwin-x64"),
    "osx_arm64":         ("macosx_11_0_arm64",      "darwin", "arm64", "-",     "darwin-arm64"),
    "windows_amd64":     ("win_amd64",              "win32",  "x64",   "-",     "win32-x64"),
    "windows_arm64":     ("win_arm64",              "win32",  "arm64", "-",     "win32-arm64"),
    # wasm and windows_amd64_mingw deliberately not mapped — they skip
    # the pip/npm publish path.
}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """subprocess.run with check=True and minimal output by default."""
    return subprocess.run(cmd, check=True, **kw)


def two_level_hash(body: bytes) -> bytes:
    """The engine's two-level extension hash: SHA-256 each 1 MiB chunk, concat
    the digests, SHA-256 that. Matches extension_load.cpp's parallel verifier
    and compute-extension-hash.sh byte-for-byte."""
    concat = b"".join(
        hashlib.sha256(body[i:i + CHUNK]).digest()
        for i in range(0, len(body), CHUNK)
    )
    return hashlib.sha256(concat).digest()


def sign_and_compress_binary(ext_path: pathlib.Path, arch: str,
                             signing_key) -> tuple[bytes, bytes]:
    """Sign + compress one extension binary entirely in memory. Returns
    (compressed_bytes, signed_uncompressed_bytes): the R2 latest/immutable + pip
    channels ship the compressed binary; the npm leaf ships the signed-but-
    uncompressed one (npm gzips the tarball itself, so shipping .gz double-
    compresses).

    Pure in-process — no shell compute-extension-hash.sh (`split`) and no
    private.pem on disk, so this is safe to run on many threads at once."""
    is_wasm = arch.startswith("wasm")
    raw = ext_path.read_bytes()
    if len(raw) < SIGNATURE_SIZE:
        raise SystemExit(f"{ext_path} is smaller than the {SIGNATURE_SIZE}-byte footer")
    # Strip the 256-byte placeholder footer, sign the body, re-append.
    body = raw[:-SIGNATURE_SIZE]
    if signing_key is not None:
        sig = signing_key.sign(two_level_hash(body), padding.PKCS1v15(),
                               utils.Prehashed(hashes.SHA256()))
        if len(sig) != SIGNATURE_SIZE:
            raise SystemExit(
                f"signing key produced a {len(sig)}-byte signature; the footer "
                f"is fixed at {SIGNATURE_SIZE} bytes (expected an RSA-2048 key)")
    else:
        sig = b"\x00" * SIGNATURE_SIZE
    signed = body + sig

    if is_wasm:
        r = subprocess.run(["brotli", "-c"], input=signed,
                           stdout=subprocess.PIPE, check=True)
        compressed = r.stdout
    else:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            gz.write(signed)
        compressed = buf.getvalue()
    return compressed, signed


def gpg_sign(path: pathlib.Path, passphrase: str, key_id: str) -> pathlib.Path:
    asc = pathlib.Path(str(path) + ".asc")
    # Serialize gpg: concurrent invocations contend on the single gpg-agent.
    with _GPG_LOCK:
        run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
             "--passphrase", passphrase, "--local-user", key_id,
             "--detach-sign", "--armor", "--output", str(asc), str(path)])
    return asc


def gpg_import_and_get_keyid(armored_or_hex_key: str, passphrase: str) -> str:
    """Import HAYBARN_GPG_PRIVATE_KEY (any of the shapes the engine repo's
    haybarn-publish.yml accepts) and return the long key id of the
    Haybarn signing identity."""
    import base64

    raw = armored_or_hex_key
    candidates: list[bytes] = []
    candidates.append(raw.encode())                       # armored direct
    candidates.append(raw.replace("\\n", "\n").encode())  # armored w/ literal \n
    try:
        candidates.append(base64.b64decode("".join(raw.split())))  # base64-of-binary
    except Exception:
        pass
    try:
        candidates.append(bytes.fromhex("".join(raw.split())))     # hex-decoded binary
    except ValueError:
        pass
    candidates.append(raw.encode("latin-1", errors="replace"))     # raw bytes

    for cand in candidates:
        with tempfile.NamedTemporaryFile(delete=False) as kf:
            kf.write(cand)
            kf_path = kf.name
        try:
            r = subprocess.run(
                ["gpg", "--batch", "--pinentry-mode", "loopback",
                 "--passphrase", passphrase, "--import", kf_path],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                break
        finally:
            os.unlink(kf_path)
    else:
        raise SystemExit("publish_extensions: could not import HAYBARN_GPG_PRIVATE_KEY")

    r = subprocess.run(["gpg", "--list-secret-keys", "--keyid-format", "LONG"],
                       capture_output=True, text=True, check=True)
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("sec"):
            # e.g.  "sec   rsa4096/C41595068F3F6537 ..."
            return line.split("/", 1)[1].split()[0]
    raise SystemExit("publish_extensions: imported GPG key but no sec line found")


def compute_calver(ts: int) -> str:
    when = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return f"{when.year*100+when.month}.{when.day}.{when.hour*10000+when.minute*100+when.second}"


# Engine version arrives in several shapes depending on the caller: core (the
# engine repo) passes a clean "v1.5.3" / "1.5.3"; the community pipeline passes
# the engine *git tag* "haybarn-v1.5.3-rc3" — the same value it checks the
# engine source out at — because one input does double duty. Both MUST collapse
# to the bare "X.Y.Z" that the R2 path segment and the npm/peer version use, so
# an engine tag (with its `haybarn-` prefix or `-rcN` pre-release suffix) can
# never leak into a published path or package name. This is the single place
# that knows the mapping; it fails closed on anything without a clean semver
# core rather than silently writing a malformed key (the old per-arch deploy did
# the strip in shell with `${DV#v}`, which no-ops on a "haybarn-…" string and is
# how rc tags ended up as R2 path segments).
_ENGINE_VERSION_RE = re.compile(r"^(?:haybarn-)?v?(\d+\.\d+\.\d+)(?:-rc\d+)?$")


def normalize_engine_version(raw: str) -> str:
    """'haybarn-v1.5.3-rc3' | 'v1.5.3' | '1.5.3' -> '1.5.3' (bare semver)."""
    m = _ENGINE_VERSION_RE.match(raw.strip())
    if not m:
        raise SystemExit(
            f"::error::unrecognized engine version {raw!r} — expected clean semver "
            f"like 'v1.5.3' or an engine tag like 'haybarn-v1.5.3-rc3'")
    return m.group(1)


def haybarn_suffix(hv: str) -> str:
    a, b, c = hv.split(".")
    return f"h{a}-{b}-{c}"


def npm_leaf_pkg_name(extension: str, hv: str, leaf_suffix: str) -> str:
    return f"@haybarn/ext-{extension}-{haybarn_suffix(hv)}-{leaf_suffix}"


def npm_meta_pkg_name(extension: str, hv: str) -> str:
    return f"@haybarn/ext-{extension}-{haybarn_suffix(hv)}"


def call_helper(name: str, *args: str) -> None:
    run(["python3", str(SCRIPT_DIR / name), *args])


def aws_sync(stage_dir: pathlib.Path, dest: str, include: str, dry_run: bool,
             cache_control: str, extra: list[str]) -> None:
    """Ship a partition of the staged tree in one call. Mirrors the helper in
    haybarn/scripts/haybarn_extension_upload.py: `--exclude '*' --include <pat>
    --cache-control <cc>` plus `extra` (e.g. wasm `--content-encoding br
    --content-type application/wasm`). sync skips objects already present with
    the same size, so re-publishing a commit is cheap and the freshly-staged
    mutable pointers re-upload."""
    cmd = ["aws", "s3", "sync", str(stage_dir), dest, "--no-progress",
           "--exclude", "*", "--include", include, "--cache-control", cache_control]
    if dry_run:
        cmd.append("--dryrun")
    cmd += extra
    run(cmd)


def maybe_read_previous(s3_pointer: str, dry_run: bool) -> str:
    """Fetch previous current.json's 'latest' if it exists, else ''."""
    if dry_run:
        return ""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        r = subprocess.run(["aws", "s3", "cp", s3_pointer, path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return ""
        try:
            return json.load(open(path)).get("latest", "")
        except Exception:
            return ""
    finally:
        if os.path.exists(path):
            os.unlink(path)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir",          required=True, type=pathlib.Path)
    ap.add_argument("--engine-commit",     required=True)
    ap.add_argument("--engine-commit-ts",  type=int, default=0)
    ap.add_argument("--ext-commit",        default="")
    ap.add_argument("--ext-commit-ts",     type=int, default=0)
    ap.add_argument("--ext-version-label", default="")
    ap.add_argument("--version",           default="")
    ap.add_argument("--haybarn-version",   required=True)
    ap.add_argument("--duckdb-version",    required=True)
    ap.add_argument("--r2-bucket",         required=True)
    ap.add_argument("--r2-prefix",         required=True)
    ap.add_argument("--license",           required=True, type=pathlib.Path,
                    help="path to LICENSE file embedded in wheels")
    ap.add_argument("--license-str",       default="",
                    help="SPDX license string for npm package.json (empty → MIT)")
    ap.add_argument("--description",       default="")
    ap.add_argument("--source-repo",       default="")
    ap.add_argument("--dist-dir",          required=True, type=pathlib.Path)
    ap.add_argument("--signed-by",         required=True)
    ap.add_argument("--channels",          required=True)
    ap.add_argument("--artifact-shape",    default="bundled",
                    choices=["bundled", "per-extension"])
    ap.add_argument("--write-latest",      action="store_true")
    ap.add_argument("--deploy",            default="false")
    args = ap.parse_args(argv)

    # Collapse the engine version to bare semver up front so an engine git tag
    # (haybarn-v1.5.3-rc3) can never reach an npm package name / peer pin. The
    # path segment is normalized separately at tree enumeration (it comes from
    # the assembled directory name, not this arg). --duckdb-version is now
    # vestigial (the path segment is the dir name); kept for arg compatibility.
    args.haybarn_version = normalize_engine_version(args.haybarn_version)

    dry_run = args.deploy.lower() != "true"

    # ext_commit defaults to engine_commit (core conflates them); ext_commit_ts
    # / engine_commit_ts likewise fall back to each other so a caller only has
    # to pass the one it knows.
    ext_commit = args.ext_commit or args.engine_commit
    ext_commit_ts = args.ext_commit_ts or args.engine_commit_ts or 0
    ext_short = ext_commit[:10]

    channels = {c.strip() for c in args.channels.split(",") if c.strip()}
    want_pypi = "pypi" in channels
    want_npm = "npm" in channels
    bundled = args.artifact_shape == "bundled"

    cc_latest = os.environ.get("EXTENSION_UPLOAD_CACHE_CONTROL_LATEST", DEFAULT_CC_LATEST)
    cc_versioned = os.environ.get("EXTENSION_UPLOAD_CACHE_CONTROL_VERSIONED", DEFAULT_CC_VERSIONED)

    print(f"::notice::publish_extensions: dry_run={dry_run}, shape={args.artifact_shape}, "
          f"engine={args.engine_commit[:8]}, ext={ext_short}, hv={args.haybarn_version}, "
          f"channels={sorted(channels)}, write_latest={args.write_latest}")

    signing_pk = os.environ.get("DUCKDB_EXTENSION_SIGNING_PK")
    if not signing_pk:
        raise SystemExit("DUCKDB_EXTENSION_SIGNING_PK is required")
    gpg_pk   = os.environ.get("HAYBARN_GPG_PRIVATE_KEY", "")
    gpg_pass = os.environ.get("HAYBARN_GPG_PASSPHRASE", "")

    gpg_key_id = ""
    if gpg_pk and gpg_pass:
        gpg_key_id = gpg_import_and_get_keyid(gpg_pk, gpg_pass)
        print(f"::notice::GPG signing key {gpg_key_id}")
    else:
        print("::warning::HAYBARN_GPG_* not set — manifests will be unsigned (no .asc)")

    # Version: explicit override > descriptor semver > CalVer from commit ts.
    if args.version:
        calver = args.version
    elif args.ext_version_label:
        calver = args.ext_version_label
    else:
        calver = compute_calver(ext_commit_ts)
    built_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Stage dirs
    dist = args.dist_dir
    pypi_dir = dist / "pypi"
    npm_leaves_dir = dist / "npm-leaves"   # bundled mode
    npm_metas_dir = dist / "npm-metas"     # bundled mode
    npm_tarballs_dir = dist / "npm"        # per-extension mode
    for d in (pypi_dir, npm_leaves_dir, npm_metas_dir, npm_tarballs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Track which leaves succeeded per extension for the meta packages (bundled).
    leaves_by_ext: dict[str, list[str]] = {}

    # Local staging trees mirroring the R2 key layout — shipped via a few
    # `aws s3 sync` calls instead of hundreds of per-file `aws s3 cp` cold-starts.
    # immut_stage holds the per-commit immutable objects (+ pointers); latest_stage
    # holds the mutable single-slot binaries. They sync with different
    # Cache-Control, hence separate roots.
    immut_stage = pathlib.Path(tempfile.mkdtemp(prefix="publish-r2-immut-"))
    latest_stage = pathlib.Path(tempfile.mkdtemp(prefix="publish-r2-latest-"))
    work_root = pathlib.Path(tempfile.mkdtemp(prefix="publish-work-"))

    # Load the RSA signing key once; cryptography key objects are safe to sign
    # with from multiple threads.
    signing_key = serialization.load_pem_private_key(signing_pk.encode(), password=None)

    # Enumerate every (duckdb_version, arch, ext_file). Layout:
    #   <repo-dir>/<duckdb_version>/<arch>/<ext>.duckdb_extension(.wasm)
    tuples: list[tuple[str, str, pathlib.Path]] = []
    for version_dir in sorted(args.repo_dir.iterdir()):
        if not version_dir.is_dir():
            continue
        # The directory name is the version the caller assembled the tree under
        # — clean "v1.5.3" for core, the engine tag "haybarn-v1.5.3-rc3" for
        # community. Normalize to the canonical "vX.Y.Z" path segment the engine
        # INSTALLs from; binaries are still read from version_dir itself (ext_file
        # is a resolved path), so only the published key changes.
        dv = "v" + normalize_engine_version(version_dir.name)
        for arch_dir in sorted(version_dir.iterdir()):
            if not arch_dir.is_dir():
                continue
            arch = arch_dir.name
            pat = "*.duckdb_extension.wasm" if arch.startswith("wasm") else "*.duckdb_extension"
            for ext_file in sorted(arch_dir.glob(pat)):
                tuples.append((dv, arch, ext_file))

    def process_tuple(dv: str, arch: str, ext_file: pathlib.Path) -> tuple[str, str | None]:
        """Sign, build manifests/metadata, GPG-sign, and stage every artifact
        for one (extension, platform) into the local trees. No network writes
        except the small previous-pointer lookup — R2 objects land in the stage
        trees and are synced in bulk afterwards. Returns (ext, leaf_suffix|None)."""
        ext = ext_file.name.replace(".duckdb_extension.wasm", "") \
                           .replace(".duckdb_extension", "")
        is_wasm = arch.startswith("wasm")
        compressed, signed_bytes = sign_and_compress_binary(ext_file, arch, signing_key)
        sha = hashlib.sha256(compressed).hexdigest()
        compressed_name = f"{ext}.duckdb_extension.{'wasm' if is_wasm else 'gz'}"

        # Per-commit immutable dir + the mutable pointer dir one level up.
        immut = immut_stage / dv / arch / ext / ext_short
        ptr_dir = immut_stage / dv / arch / ext
        immut.mkdir(parents=True, exist_ok=True)
        (immut / compressed_name).write_bytes(compressed)

        # Optional mutable "latest" single-slot path (engine INSTALL reads this).
        if args.write_latest:
            latest = latest_stage / dv / arch
            latest.mkdir(parents=True, exist_ok=True)
            (latest / compressed_name).write_bytes(compressed)

        work = work_root / dv / arch / ext
        work.mkdir(parents=True, exist_ok=True)

        # 1. haybarn-metadata.json (embedded into wheel + npm leaf; not on R2)
        meta_path = work / "haybarn-metadata.json"
        call_helper("generate_haybarn_metadata.py",
                    "--extension", ext, "--ext-commit", ext_commit,
                    "--haybarn-version", args.haybarn_version,
                    "--ext-version-label", args.ext_version_label,
                    "--sha256", sha, "--built-at", built_at, "--out", str(meta_path))

        # 2. R2 manifest.json (chained off the previous current.json pointer)
        prev = maybe_read_previous(
            f"s3://{args.r2_bucket}/{args.r2_prefix}/{dv}/{arch}/{ext}/current.json",
            dry_run)
        manifest_path = immut / "manifest.json"
        call_helper("r2_build_manifest.py",
                    "--extension", ext, "--duckdb-version", dv, "--platform", arch,
                    "--ext-commit", ext_commit, "--ext-version-label", args.ext_version_label,
                    "--sha256", sha, "--object", compressed_name, "--built-at", built_at,
                    "--haybarn-engine-commit", args.engine_commit, "--signed-by", args.signed_by,
                    "--channels", args.channels, "--previous-commit", prev,
                    "--out", str(manifest_path))

        # 3. current.json pointer + GPG detached sigs (.asc beside each file)
        current_path = ptr_dir / "current.json"
        current_path.write_text(json.dumps(
            {"latest": ext_commit, "updated_at": built_at},
            indent=2, sort_keys=True) + "\n")
        if gpg_key_id:
            gpg_sign(manifest_path, gpg_pass, gpg_key_id)
            gpg_sign(current_path, gpg_pass, gpg_key_id)

        # 4a. WebAssembly leaves (per-extension / community only). npm has no
        # wasm os/cpu, so these can't be meta optionalDependencies — they're
        # standalone packages installed by name for duckdb-wasm. We ship the
        # signed-but-UNCOMPRESSED .duckdb_extension.wasm (npm gzips the tarball;
        # duckdb-wasm consumes raw wasm bytes). Returns (ext, None) so the wasm
        # leaf is NEVER added to the meta's optionalDependencies.
        if is_wasm:
            if want_npm and not bundled:
                variant = arch[len("wasm_"):] if arch.startswith("wasm_") else arch
                leaf_suffix = arch.replace("_", "-")  # wasm_mvp -> wasm-mvp
                leaf_pkg = npm_leaf_pkg_name(ext, args.haybarn_version, leaf_suffix)
                leaf_stage = npm_leaves_dir / leaf_pkg.replace("/", "_")
                (leaf_stage / "bin").mkdir(parents=True, exist_ok=True)
                (leaf_stage / "bin" / f"{ext}.duckdb_extension.wasm").write_bytes(signed_bytes)
                shutil.copy(meta_path, leaf_stage / "haybarn-metadata.json")
                env = dict(os.environ,
                           STAGE=str(leaf_stage), PKG=leaf_pkg, VERSION=calver, EXTENSION=ext,
                           HAYBARN_VERSION=args.haybarn_version, WASM_VARIANT=variant,
                           LICENSE=args.license_str,
                           EXTENSION_DESCRIPTION=args.description,
                           EXTENSION_SOURCE_REPO=args.source_repo,
                           HAYBARN_METADATA=str(leaf_stage / "haybarn-metadata.json"))
                run(["python3", str(SCRIPT_DIR / "npm_build_leaf.py")], env=env)
                tarball = npm_tarballs_dir / f"{leaf_pkg.replace('/', '_')}.tar.gz"
                with tarfile.open(tarball, "w:gz") as t:
                    for child in sorted(leaf_stage.iterdir()):
                        t.add(child, arcname=child.name)
            return ext, None

        # 4b. pip wheel + native npm leaf (skip windows_amd64_mingw; gated on channels)
        plat = PLATMAP.get(arch)
        if plat is None:
            return ext, None
        py_tag, npm_os, npm_cpu, npm_libc, leaf_suffix = plat

        if want_pypi:
            comp_file = work / compressed_name  # the wheel ships the compressed binary
            comp_file.write_bytes(compressed)
            call_helper("pypi_build_wheel.py",
                        "--extension", ext, "--haybarn-version", args.haybarn_version,
                        "--version", calver, "--platform-tag", py_tag,
                        "--binary", str(comp_file), "--haybarn-metadata", str(meta_path),
                        "--license", str(args.license), "--source-repo", args.source_repo,
                        "--out-dir", str(pypi_dir))

        if want_npm:
            leaf_pkg = npm_leaf_pkg_name(ext, args.haybarn_version, leaf_suffix)
            leaf_stage = npm_leaves_dir / leaf_pkg.replace("/", "_")  # npm dislikes '/'
            (leaf_stage / "bin").mkdir(parents=True, exist_ok=True)
            # npm ships the signed-but-UNCOMPRESSED .duckdb_extension — npm gzips the
            # tarball itself, so the .gz would be double-compressed. (Matches core.)
            (leaf_stage / "bin" / f"{ext}.duckdb_extension").write_bytes(signed_bytes)
            shutil.copy(meta_path, leaf_stage / "haybarn-metadata.json")
            env = dict(os.environ,
                       STAGE=str(leaf_stage), PKG=leaf_pkg, VERSION=calver, EXTENSION=ext,
                       HAYBARN_VERSION=args.haybarn_version,
                       OS=npm_os, CPU=npm_cpu, LIBC=npm_libc,
                       LICENSE=args.license_str,
                       EXTENSION_DESCRIPTION=args.description,
                       EXTENSION_SOURCE_REPO=args.source_repo,
                       HAYBARN_METADATA=str(leaf_stage / "haybarn-metadata.json"))
            run(["python3", str(SCRIPT_DIR / "npm_build_leaf.py")], env=env)

            # per-extension shape: tar each leaf for a per-(ext) artifact whose
            # tarballs the downstream per-extension npm job extracts + publishes.
            if not bundled:
                tarball = npm_tarballs_dir / f"{leaf_pkg.replace('/', '_')}.tar.gz"
                with tarfile.open(tarball, "w:gz") as t:
                    for child in sorted(leaf_stage.iterdir()):
                        t.add(child, arcname=child.name)
        return ext, leaf_suffix

    try:
        # Phase 1: stage every tuple in parallel (subprocess + network + gpg
        # bound, so threads overlap the waits; gpg itself is lock-serialized).
        total = len(tuples)
        if total == 0:
            raise SystemExit(f"no built extensions found under {args.repo_dir}")
        jobs = int(os.environ.get("PUBLISH_CONCURRENCY",
                                  min(16, (os.cpu_count() or 4) * 4)))
        t0 = time.monotonic()
        log(f"Phase 1/2: staging {total} (extension,platform) tuples "
            f"(jobs={jobs}) [{'for_real' if not dry_run else 'DRY RUN'}]")
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = {pool.submit(process_tuple, dv, arch, ef): (dv, arch, ef)
                    for (dv, arch, ef) in tuples}
            done = as_completed(futs)
            if tqdm is not None:
                done = tqdm(done, total=total, desc="sign+stage", unit="ext",
                            mininterval=0.5)
            for i, fut in enumerate(done, 1):
                ext, leaf_suffix = fut.result()  # re-raise worker exceptions
                if leaf_suffix:
                    leaves_by_ext.setdefault(ext, []).append(leaf_suffix)
                if tqdm is None:
                    log(f"  [{i}/{total}] {time.monotonic() - t0:6.1f}s  {ext} ({futs[fut][1]})")
        log(f"Phase 1 done: {total} tuples in {time.monotonic() - t0:.1f}s")

        # Phase 2: ship the staged trees. Partition by Cache-Control / metadata.
        log(f"Phase 2/2: aws s3 sync -> s3://{args.r2_bucket}/{args.r2_prefix}")
        t1 = time.monotonic()
        sync_conc = os.environ.get("PUBLISH_SYNC_CONCURRENCY", "32")
        run(["aws", "configure", "set", "default.s3.max_concurrent_requests", sync_conc])
        dest = f"s3://{args.r2_bucket}/{args.r2_prefix}"

        # Per-commit immutable tree: binary (no content-encoding — the loader
        # gunzips .gz itself; wasm immutable matches today's no-encoding behavior),
        # manifest.json (json), .asc (pgp-signature). 1-year immutable Cache-Control.
        aws_sync(immut_stage, dest, "*.duckdb_extension.gz", dry_run, cc_versioned, [])
        aws_sync(immut_stage, dest, "*.duckdb_extension.wasm", dry_run, cc_versioned, [])
        aws_sync(immut_stage, dest, "*manifest.json", dry_run, cc_versioned,
                 ["--content-type", "application/json"])
        aws_sync(immut_stage, dest, "*manifest.json.asc", dry_run, cc_versioned,
                 ["--content-type", "application/pgp-signature"])
        # current.json pointer (+ .asc) is mutable — short Cache-Control so the
        # edge serves the new pointer within seconds (NOT the 1yr immutable CC).
        aws_sync(immut_stage, dest, "*current.json", dry_run, cc_latest,
                 ["--content-type", "application/json"])
        aws_sync(immut_stage, dest, "*current.json.asc", dry_run, cc_latest,
                 ["--content-type", "application/pgp-signature"])

        # Mutable "latest" single-slot binaries (engine INSTALL path).
        if args.write_latest:
            aws_sync(latest_stage, dest, "*.duckdb_extension.gz", dry_run, cc_latest, [])
            aws_sync(latest_stage, dest, "*.duckdb_extension.wasm", dry_run, cc_latest,
                     ["--content-encoding", "br", "--content-type", "application/wasm"])
        log(f"Phase 2 done in {time.monotonic() - t1:.1f}s")

        # 7. Per-extension npm meta packages (bundled mode only) — community's
        # downstream per-extension job builds its own meta from the leaf tarballs.
        if bundled and want_npm:
            for ext, suffixes in leaves_by_ext.items():
                meta_pkg = npm_meta_pkg_name(ext, args.haybarn_version)
                meta_stage = npm_metas_dir / meta_pkg.replace("/", "_")
                meta_stage.mkdir(parents=True, exist_ok=True)
                agg_meta = meta_stage / "haybarn-metadata.json"
                call_helper("generate_haybarn_metadata.py",
                            "--extension", ext,
                            "--ext-commit", ext_commit,
                            "--haybarn-version", args.haybarn_version,
                            "--ext-version-label", args.ext_version_label,
                            "--sha256", "see-leaf-packages-for-per-platform-sha256",
                            "--built-at", built_at,
                            "--out", str(agg_meta))
                env = dict(os.environ,
                           STAGE=str(meta_stage), PKG=meta_pkg,
                           VERSION=calver, EXTENSION=ext,
                           HAYBARN_VERSION=args.haybarn_version,
                           LICENSE=args.license_str,
                           EXTENSION_DESCRIPTION=args.description,
                           EXTENSION_SOURCE_REPO=args.source_repo,
                           HAYBARN_METADATA=str(agg_meta),
                           PRESENT_LEAVES=",".join(sorted(suffixes)))
                run(["python3", str(SCRIPT_DIR / "npm_build_meta.py")], env=env)

        # Summary
        n_wheels = sum(1 for _ in pypi_dir.glob("*.whl"))
        n_leaves = sum(1 for d in npm_leaves_dir.iterdir() if d.is_dir())
        n_tarballs = sum(1 for _ in npm_tarballs_dir.glob("*.tar.gz"))
        n_metas  = sum(1 for d in npm_metas_dir.iterdir()  if d.is_dir())
        print("\n=== summary ===")
        print(f"  extensions processed: {len(leaves_by_ext)}")
        print(f"  wheels staged:        {n_wheels}")
        print(f"  npm leaves staged:    {n_leaves}")
        print(f"  npm leaf tarballs:    {n_tarballs}")
        print(f"  npm metas staged:     {n_metas}")
        print(f"  version:              {calver}")
        print(f"  ext_commit short:     {ext_short}")
        return 0
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        shutil.rmtree(immut_stage, ignore_errors=True)
        shutil.rmtree(latest_stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
