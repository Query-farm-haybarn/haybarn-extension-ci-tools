#!/usr/bin/env python3
"""Publish core extensions: iterate the assembled extension repository at
/tmp/haybarn-repository, apply the per-commit immutable R2 layout (signed
binary + signed manifest + signed current.json pointer), and stage pip
wheel + npm leaf + npm meta artifacts for the publish_pypi / publish_npm
jobs to upload.

Mirrors what haybarn-community-extensions/.github/workflows/_extension_deploy.yml
does for a single (extension, arch) tuple, but loops over the full
core matrix in one process — there are ~17 extensions × ~10 platforms
so a single workflow job is plenty.

Per the design call we made: all core extensions share ext_commit =
engine_commit. That's accurate for in-tree extensions and an
approximation for the build-fork extensions (iceberg/ducklake/delta/
httpfs which have their own SHAs). Refining the build-fork attribution
is a follow-up.

Inputs (all required):
  --repo-dir          assembled repo, e.g. /tmp/haybarn-repository
  --engine-commit     40-char engine SHA
  --engine-commit-ts  engine commit unix timestamp (UTC)
  --haybarn-version   e.g. 1.5.2  (drives package-name suffix + peer pin)
  --duckdb-version    e.g. v1.5.2 (path segment)
  --r2-bucket         R2 bucket name
  --r2-prefix         e.g. core
  --license           path to LICENSE file (embedded in wheels)
  --dist-dir          staging dir for pip wheels + npm package dirs
  --signed-by         identity name (forward hook for trust-store)
  --channels          comma-separated, e.g. r2-core,pypi,npm
  --deploy            when 'true', actually upload to R2; otherwise dry-run

Env (required when --deploy true):
  DUCKDB_EXTENSION_SIGNING_PK   PEM private key for binary RSA sign
  HAYBARN_GPG_PRIVATE_KEY       GPG key for manifest signing
  HAYBARN_GPG_PASSPHRASE
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_ENDPOINT_URL /
    AWS_DEFAULT_REGION / AWS_REQUEST_CHECKSUM_CALCULATION /
    AWS_RESPONSE_CHECKSUM_VALIDATION

Staging output (always written):
  <dist-dir>/pypi/<wheel-name>.whl
  <dist-dir>/npm-leaves/<leaf-pkg-name>/        (package.json + bin/ + metadata)
  <dist-dir>/npm-metas/<meta-pkg-name>/         (package.json + metadata)
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
import shutil
import subprocess
import sys
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
    # the pip/npm publish path (same as community workflow).
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
    (compressed_bytes, signed_uncompressed_bytes): the R2/pypi channels ship
    the compressed binary; the npm leaf ships the signed-but-uncompressed one
    (npm gzips the tarball itself, so shipping .gz double-compresses).

    Pure in-process — no shell compute-extension-hash.sh (`split`) and no
    private.pem on disk, so this is safe to run on many threads at once."""
    is_wasm = arch.startswith("wasm")
    raw = ext_path.read_bytes()
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


def sha256_hex(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gpg_sign(path: pathlib.Path, passphrase: str, key_id: str) -> pathlib.Path:
    asc = pathlib.Path(str(path) + ".asc")
    # Serialize gpg: concurrent invocations contend on the single gpg-agent.
    with _GPG_LOCK:
        run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
             "--passphrase", passphrase, "--local-user", key_id,
             "--detach-sign", "--armor", "--output", str(asc), str(path)])
    return asc


def gpg_import_and_get_keyid(armored_or_hex_key: str, passphrase: str) -> str:
    """Import HAYBARN_GPG_PRIVATE_KEY (any of 6 shapes the engine repo's
    haybarn-publish.yml accepts) and return the long key id of the
    Haybarn signing identity."""
    import base64

    raw = armored_or_hex_key
    candidates: list[bytes] = []
    # 1. armored direct
    candidates.append(raw.encode())
    # 2. armored with literal \n escapes
    candidates.append(raw.replace("\\n", "\n").encode())
    # 3. base64-of-binary
    try:
        candidates.append(base64.b64decode("".join(raw.split())))
    except Exception:
        pass
    # 4. hex-decoded binary
    try:
        candidates.append(bytes.fromhex("".join(raw.split())))
    except ValueError:
        pass
    # 5. raw bytes
    candidates.append(raw.encode("latin-1", errors="replace"))

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
        raise SystemExit("publish_core_extensions: could not import HAYBARN_GPG_PRIVATE_KEY")

    # Now find the secret key for our uid
    r = subprocess.run(["gpg", "--list-secret-keys", "--keyid-format", "LONG"],
                       capture_output=True, text=True, check=True)
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("sec"):
            # e.g.  "sec   rsa4096/C41595068F3F6537 ..."
            return line.split("/", 1)[1].split()[0]
    raise SystemExit("publish_core_extensions: imported GPG key but no sec line found")


def compute_calver(ts: int) -> str:
    when = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return f"{when.year*100+when.month}.{when.day}.{when.hour*10000+when.minute*100+when.second}"


def haybarn_suffix(hv: str) -> str:
    a, b, c = hv.split(".")
    return f"h{a}-{b}-{c}"


def npm_leaf_pkg_name(extension: str, hv: str, leaf_suffix: str) -> str:
    return f"@haybarn/ext-{extension}-{haybarn_suffix(hv)}-{leaf_suffix}"


def npm_meta_pkg_name(extension: str, hv: str) -> str:
    return f"@haybarn/ext-{extension}-{haybarn_suffix(hv)}"


def call_helper(name: str, *args: str) -> None:
    run(["python3", str(SCRIPT_DIR / name), *args])


def aws_cp(local: pathlib.Path, s3_url: str, dry_run: bool) -> None:
    if dry_run:
        print(f"DRY: aws s3 cp {local} {s3_url}")
        return
    run(["aws", "s3", "cp", str(local), s3_url])


def aws_sync(local_dir: pathlib.Path, s3_url: str, dry_run: bool) -> None:
    """Ship a whole staged tree in one call. Plain sync (no --content-type /
    --cache-control) reproduces the per-file `aws s3 cp` behavior these
    immutable objects had before. sync skips objects already present with the
    same size, so re-publishing a commit is cheap and the mutable current.json
    pointers (always freshly staged) re-upload."""
    conc = os.environ.get("PUBLISH_SYNC_CONCURRENCY", "32")
    run(["aws", "configure", "set", "default.s3.max_concurrent_requests", conc])
    cmd = ["aws", "s3", "sync", str(local_dir), s3_url, "--no-progress"]
    if dry_run:
        cmd.append("--dryrun")
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
    ap.add_argument("--engine-commit-ts",  required=True, type=int)
    ap.add_argument("--haybarn-version",   required=True)
    ap.add_argument("--duckdb-version",    required=True)
    ap.add_argument("--r2-bucket",         required=True)
    ap.add_argument("--r2-prefix",         required=True)
    ap.add_argument("--license",           required=True, type=pathlib.Path)
    ap.add_argument("--dist-dir",          required=True, type=pathlib.Path)
    ap.add_argument("--signed-by",         required=True)
    ap.add_argument("--channels",          required=True)
    ap.add_argument("--deploy",            default="false")
    args = ap.parse_args(argv)

    dry_run = args.deploy.lower() != "true"
    print(f"::notice::publish_core_extensions: dry_run={dry_run}, "
          f"engine={args.engine_commit[:8]}, hv={args.haybarn_version}")

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

    calver = compute_calver(args.engine_commit_ts)
    ext_short = args.engine_commit[:10]
    built_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Stage dirs
    dist = args.dist_dir
    pypi_dir = dist / "pypi"
    npm_leaves_dir = dist / "npm-leaves"
    npm_metas_dir = dist / "npm-metas"
    for d in (pypi_dir, npm_leaves_dir, npm_metas_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Track which leaves succeeded per extension for the meta packages
    leaves_by_ext: dict[str, list[str]] = {}

    # Local staging tree mirroring the R2 key layout — shipped in ONE aws s3
    # sync at the end instead of hundreds of per-file `aws s3 cp` cold-starts.
    r2_stage = pathlib.Path(tempfile.mkdtemp(prefix="publish-core-r2-"))
    work_root = pathlib.Path(tempfile.mkdtemp(prefix="publish-core-"))

    # Load the RSA signing key once; cryptography key objects are safe to sign
    # with from multiple threads.
    signing_key = serialization.load_pem_private_key(signing_pk.encode(), password=None)

    # Enumerate every (duckdb_version, arch, ext_file) up front so we can fan
    # the per-tuple work out across a thread pool. Layout:
    #   <repo-dir>/<duckdb_version>/<arch>/<ext>.duckdb_extension(.wasm)
    tuples: list[tuple[str, str, pathlib.Path]] = []
    for version_dir in sorted(args.repo_dir.iterdir()):
        if not version_dir.is_dir():
            continue
        dv = version_dir.name
        for arch_dir in sorted(version_dir.iterdir()):
            if not arch_dir.is_dir():
                continue
            arch = arch_dir.name
            pat = "*.duckdb_extension.wasm" if arch.startswith("wasm") else "*.duckdb_extension"
            for ext_file in sorted(arch_dir.glob(pat)):
                tuples.append((dv, arch, ext_file))

    def process_tuple(dv: str, arch: str, ext_file: pathlib.Path) -> tuple[str, str | None]:
        """Sign, build manifests/metadata, GPG-sign, and stage every artifact
        for one (extension, platform) into the local trees. No network writes —
        the R2 objects land in r2_stage and are synced in bulk afterwards.
        Returns (ext, leaf_suffix|None) for meta-package aggregation."""
        ext = ext_file.name.replace(".duckdb_extension.wasm", "") \
                           .replace(".duckdb_extension", "")
        is_wasm = arch.startswith("wasm")
        compressed, signed_bytes = sign_and_compress_binary(ext_file, arch, signing_key)
        sha = hashlib.sha256(compressed).hexdigest()
        compressed_name = f"{ext}.duckdb_extension.{'wasm' if is_wasm else 'gz'}"

        # Per-commit immutable dir, and the mutable pointer dir one level up.
        immut = r2_stage / dv / arch / ext / ext_short
        ptr_dir = r2_stage / dv / arch / ext
        immut.mkdir(parents=True, exist_ok=True)
        (immut / compressed_name).write_bytes(compressed)

        work = work_root / dv / arch / ext
        work.mkdir(parents=True, exist_ok=True)

        # 1. haybarn-metadata.json (embedded into wheel + npm leaf; not on R2)
        meta_path = work / "haybarn-metadata.json"
        call_helper("generate_haybarn_metadata.py",
                    "--extension", ext, "--ext-commit", args.engine_commit,
                    "--haybarn-version", args.haybarn_version, "--ext-version-label", "",
                    "--sha256", sha, "--built-at", built_at, "--out", str(meta_path))

        # 2. R2 manifest.json (chained off the previous current.json pointer)
        prev = maybe_read_previous(
            f"s3://{args.r2_bucket}/{args.r2_prefix}/{dv}/{arch}/{ext}/current.json",
            dry_run)
        manifest_path = immut / "manifest.json"
        call_helper("r2_build_manifest.py",
                    "--extension", ext, "--duckdb-version", dv, "--platform", arch,
                    "--ext-commit", args.engine_commit, "--ext-version-label", "",
                    "--sha256", sha, "--object", compressed_name, "--built-at", built_at,
                    "--haybarn-engine-commit", args.engine_commit, "--signed-by", args.signed_by,
                    "--channels", args.channels, "--previous-commit", prev,
                    "--out", str(manifest_path))

        # 3. current.json pointer + GPG detached sigs (.asc beside each file)
        current_path = ptr_dir / "current.json"
        current_path.write_text(json.dumps(
            {"latest": args.engine_commit, "updated_at": built_at},
            indent=2, sort_keys=True) + "\n")
        if gpg_key_id:
            gpg_sign(manifest_path, gpg_pass, gpg_key_id)
            gpg_sign(current_path, gpg_pass, gpg_key_id)

        # 4. pip wheel + npm leaf (skip wasm + windows_amd64_mingw)
        plat = PLATMAP.get(arch)
        if plat is None:
            return ext, None
        py_tag, npm_os, npm_cpu, npm_libc, leaf_suffix = plat

        comp_file = work / compressed_name  # the wheel ships the compressed binary
        comp_file.write_bytes(compressed)
        call_helper("pypi_build_wheel.py",
                    "--extension", ext, "--haybarn-version", args.haybarn_version,
                    "--version", calver, "--platform-tag", py_tag,
                    "--binary", str(comp_file), "--haybarn-metadata", str(meta_path),
                    "--license", str(args.license), "--out-dir", str(pypi_dir))

        leaf_pkg = npm_leaf_pkg_name(ext, args.haybarn_version, leaf_suffix)
        leaf_stage = npm_leaves_dir / leaf_pkg.replace("/", "_")  # npm dislikes '/'
        (leaf_stage / "bin").mkdir(parents=True, exist_ok=True)
        # npm ships the signed-but-uncompressed .duckdb_extension — npm gzips the
        # tarball itself, so the .gz would be double-compressed.
        (leaf_stage / "bin" / f"{ext}.duckdb_extension").write_bytes(signed_bytes)
        shutil.copy(meta_path, leaf_stage / "haybarn-metadata.json")
        env = dict(os.environ,
                   STAGE=str(leaf_stage), PKG=leaf_pkg, VERSION=calver, EXTENSION=ext,
                   HAYBARN_VERSION=args.haybarn_version,
                   OS=npm_os, CPU=npm_cpu, LIBC=npm_libc,
                   HAYBARN_METADATA=str(leaf_stage / "haybarn-metadata.json"))
        run(["python3", str(SCRIPT_DIR / "npm_build_leaf.py")], env=env)
        return ext, leaf_suffix

    try:
        # Phase 1: stage every tuple in parallel (subprocess + network + gpg
        # bound, so threads overlap the waits; gpg itself is lock-serialized).
        total = len(tuples)
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

        # Phase 2: ship the whole immutable R2 tree (+ pointers) in one sync.
        log(f"Phase 2/2: aws s3 sync -> s3://{args.r2_bucket}/{args.r2_prefix}")
        t1 = time.monotonic()
        aws_sync(r2_stage, f"s3://{args.r2_bucket}/{args.r2_prefix}", dry_run)
        log(f"Phase 2 done in {time.monotonic() - t1:.1f}s")

        # 7. Per-extension npm meta packages — must run after all leaves are known
        for ext, suffixes in leaves_by_ext.items():
            meta_pkg = npm_meta_pkg_name(ext, args.haybarn_version)
            meta_stage = npm_metas_dir / meta_pkg.replace("/", "_")
            meta_stage.mkdir(parents=True, exist_ok=True)
            # The meta's haybarn-metadata.json carries an aggregate blob (no
            # per-platform sha256 since the meta has no binary).
            agg_meta = meta_stage / "haybarn-metadata.json"
            call_helper("generate_haybarn_metadata.py",
                        "--extension", ext,
                        "--ext-commit", args.engine_commit,
                        "--haybarn-version", args.haybarn_version,
                        "--ext-version-label", "",
                        "--sha256", "see-leaf-packages-for-per-platform-sha256",
                        "--built-at", built_at,
                        "--out", str(agg_meta))
            env = dict(os.environ,
                       STAGE=str(meta_stage), PKG=meta_pkg,
                       VERSION=calver, EXTENSION=ext,
                       HAYBARN_VERSION=args.haybarn_version,
                       HAYBARN_METADATA=str(agg_meta),
                       PRESENT_LEAVES=",".join(sorted(suffixes)))
            run(["python3", str(SCRIPT_DIR / "npm_build_meta.py")], env=env)

        # Summary
        n_wheels = sum(1 for _ in pypi_dir.glob("*.whl"))
        n_leaves = sum(1 for d in npm_leaves_dir.iterdir() if d.is_dir())
        n_metas  = sum(1 for d in npm_metas_dir.iterdir()  if d.is_dir())
        print(f"\n=== summary ===")
        print(f"  extensions processed: {len(leaves_by_ext)}")
        print(f"  wheels staged:        {n_wheels}")
        print(f"  npm leaves staged:    {n_leaves}")
        print(f"  npm metas staged:     {n_metas}")
        print(f"  calver:               {calver}")
        print(f"  ext_commit short:     {ext_short}")
        return 0
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        shutil.rmtree(r2_stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
