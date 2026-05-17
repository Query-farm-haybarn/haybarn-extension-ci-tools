FROM ubuntu:24.04

# OCI image label — links this package to its source repo on GHCR so
# the GitHub UI shows the repo on the package page, the package counts
# as "originated from" the source repo for automatic-access purposes,
# and downstream tooling (e.g. dependabot) can find provenance.
LABEL org.opencontainers.image.source="https://github.com/Query-farm-haybarn/haybarn-extension-ci-tools"
LABEL org.opencontainers.image.description="Haybarn community-extensions wasm build environment (emscripten + vcpkg + ccache)"
LABEL org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive

# Basic build prerequisites. Ubuntu 24.04 ships cmake 3.28+ and ninja-build
# in the base repos — no need for the manylinux-style CMake side-install.
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      ninja-build \
      git \
      curl \
      wget \
      python3 \
      python3-pip \
      python3-venv \
      jq \
      zip \
      unzip \
      tar \
      xz-utils \
      autoconf \
      automake \
      libtool \
      pkg-config \
      perl \
      ca-certificates \
      lsb-release \
      bison \
      flex \
    && rm -rf /var/lib/apt/lists/*

# ccache 4.13.6 musl-static. Same binary the macos/win/linux legs use;
# Ubuntu 24.04 ships 4.9.1 in apt, which has the broken HTTP bearer-auth
# we saw silently dropping cache writes on wasm legs before.
ARG CCACHE_VERSION=4.13.6
RUN curl -fsSL "https://github.com/ccache/ccache/releases/download/v${CCACHE_VERSION}/ccache-${CCACHE_VERSION}-linux-x86_64-musl-static.tar.xz" \
      -o /tmp/ccache.tar.xz \
    && tar -xJf /tmp/ccache.tar.xz -C /tmp \
    && install -m 0755 "/tmp/ccache-${CCACHE_VERSION}-linux-x86_64-musl-static/ccache" /usr/local/bin/ccache \
    && rm -rf /tmp/ccache.tar.xz "/tmp/ccache-${CCACHE_VERSION}-linux-x86_64-musl-static" \
    && /usr/local/bin/ccache --version

# sccache — Rust rustc-wrapper cache, used when extra_toolchains includes rust.
ARG SCCACHE_VERSION=0.8.2
RUN curl -fsSL "https://github.com/mozilla/sccache/releases/download/v${SCCACHE_VERSION}/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
      -o /tmp/sccache.tar.gz \
    && tar -xzf /tmp/sccache.tar.gz -C /tmp \
    && install -m 0755 "/tmp/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl/sccache" /usr/local/bin/sccache \
    && rm -rf /tmp/sccache.tar.gz "/tmp/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl" \
    && /usr/local/bin/sccache --version

# Emscripten SDK — baked at a stable absolute path /emsdk so consumer
# builds get a deterministic emcc location (the inline `setup-emsdk@v13`
# path embeds a per-run UUID, which defeats ccache hashing — that was
# the whole reason for this image existing).
ARG EMSDK_VERSION=3.1.71
RUN git clone --depth 1 https://github.com/emscripten-core/emsdk.git /emsdk \
    && cd /emsdk \
    && ./emsdk install ${EMSDK_VERSION} \
    && ./emsdk activate ${EMSDK_VERSION} \
    && echo "source /emsdk/emsdk_env.sh > /dev/null" >> /etc/profile.d/emsdk.sh

# Make emcc / em++ / node available without sourcing emsdk_env.sh. The
# emsdk install puts everything under /emsdk/upstream/emscripten and ships
# its own node under /emsdk/node/<node_version>_64bit/bin. We don't pin
# node's version path because emsdk_env.sh resolves it dynamically; the
# easier approach is to source it on shell entry (above) and also point
# PATH directly at the static parts here.
ENV EMSDK=/emsdk
ENV EMSDK_NODE=/emsdk/node
ENV EMSCRIPTEN_ROOT=/emsdk/upstream/emscripten
ENV PATH="/emsdk:/emsdk/upstream/emscripten:${PATH}"

# Setup VCPKG — baked into the image like the Linux Phase A variants do.
# Wasm-specific triplets/toolchain come from the consumer's CMake config,
# not from anything baked here.
ARG vcpkg_url
ARG vcpkg_commit
RUN mkdir /vcpkg \
    && cd /vcpkg \
    && git init \
    && git remote add origin $vcpkg_url \
    && git fetch origin $vcpkg_commit \
    && git checkout $vcpkg_commit \
    && ./bootstrap-vcpkg.sh -disableMetrics
ENV VCPKG_ROOT=/vcpkg
ENV VCPKG_TOOLCHAIN_PATH=/vcpkg/scripts/buildsystems/vcpkg.cmake
ENV PATH="${VCPKG_ROOT}:${PATH}"

# Common environment variables.
# Note: deliberately NOT setting GEN=ninja here (which the Linux images do).
# The extension-ci-tools Makefile target for wasm hardcodes
# `emmake make -C build/<arch>` — when cmake was configured with -GNinja
# the build dir contains build.ninja, not a Makefile, and that `make -C`
# fails with "No targets specified and no makefile found". The inline
# (non-Docker) wasm path doesn't set GEN either, so it falls back to
# Unix Makefiles; this image mirrors that for compat.
ENV DUCKDB_PLATFORM=wasm

# Allow git access to the mounted volume.
RUN git config --global --add safe.directory "*"

# Specify where we expect the extension to be mounted and use that as
# working dir. Caller does `docker run -v $PWD:/duckdb_build_dir`.
VOLUME /duckdb_build_dir
WORKDIR /duckdb_build_dir

# ccache mount + tuning. CCACHE_BASEDIR is /duckdb_build_dir — a stable
# in-container path — so hashes are deterministic. No more
# /home/runner/work/_temp/<UUID> dance.
VOLUME /ccache_dir
ENV CCACHE_DIR=/ccache_dir
ENV CCACHE_BASEDIR=/duckdb_build_dir
ENV CCACHE_COMPRESS=TRUE
ENV CCACHE_COMPRESSLEVEL=6
ENV CCACHE_MAXSIZE=400M
ENV CCACHE_NOHASHDIR=1
ENV CCACHE_SLOPPINESS=time_macros,include_file_mtime,pch_defines
ENV CCACHE_COMPILERCHECK=content

###
# Optional toolchains — controlled via the `extra_toolchains` build-arg,
# mirroring the Linux Phase A pattern. The publish-build-images workflow
# builds one image per variant (base / rust) and pushes them as
# haybarn-wasm-<variant>:<engine-version> on GHCR.
###
ARG extra_toolchains

# Rust (for community extensions like `inflector` that declare
# `requires_toolchains: rust`). Includes the wasm32-unknown-emscripten
# target since that's what every wasm-rust extension targets.
ENV PATH="/root/.cargo/bin:${PATH}"
RUN case "$extra_toolchains" in \
  *\;rust\;*) \
    curl https://sh.rustup.rs -sSf | bash -s -- -y --default-toolchain stable && \
    /root/.cargo/bin/rustup target add wasm32-unknown-emscripten && \
    /root/.cargo/bin/rustc --version \
  ;; \
esac
