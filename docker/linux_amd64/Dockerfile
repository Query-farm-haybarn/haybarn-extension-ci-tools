FROM quay.io/pypa/manylinux_2_28_x86_64:2026.05.13-1

# OCI image label — links this package to its source repo on GHCR so
# the GitHub UI shows the repo on the package page, the package counts
# as "originated from" the source repo for automatic-access purposes,
# and downstream tooling (e.g. dependabot) can find provenance.
LABEL org.opencontainers.image.source="https://github.com/Query-farm-haybarn/haybarn-extension-ci-tools"
LABEL org.opencontainers.image.description="Haybarn community-extensions build environment"
LABEL org.opencontainers.image.licenses="MIT"

# Setup the basic necessities
RUN yum groupinstall -y "Development Tools"
RUN yum install -y curl zip unzip tar autoconf libtool
RUN yum install -y ninja-build
RUN yum install -y perl-IPC-Cmd
RUN yum install -y perl-core
# Haybarn: ccache lives in EPEL, not the manylinux_2_28 base repos.
RUN yum install -y epel-release
# Haybarn: EPEL 8 still ships ccache 3.7.7 (2019), which predates the HTTP
# storage backend (added in 4.7). Pull the upstream 4.13.6 GitHub release.
# Using the `musl-static` variant (fully statically linked, no libc dep at
# all) rather than `glibc` because manylinux_2_28 has glibc 2.28 while ccache's
# glibc-tarball requires GLIBC_2.34+. musl-static runs on any Linux.
ARG CCACHE_VERSION=4.13.6
RUN curl -fsSL "https://github.com/ccache/ccache/releases/download/v${CCACHE_VERSION}/ccache-${CCACHE_VERSION}-linux-x86_64-musl-static.tar.xz" \
      -o /tmp/ccache.tar.xz \
    && tar -xJf /tmp/ccache.tar.xz -C /tmp \
    && install -m 0755 "/tmp/ccache-${CCACHE_VERSION}-linux-x86_64-musl-static/ccache" /usr/local/bin/ccache \
    && rm -rf /tmp/ccache.tar.xz "/tmp/ccache-${CCACHE_VERSION}-linux-x86_64-musl-static" \
    && /usr/local/bin/ccache --version
RUN yum install -y java-11-openjdk-devel maven
RUN yum install -y wget
RUN yum install -y kernel-headers
RUN yum install -y kernel-devel
RUN yum install -y glibc-devel
RUN yum install -y jq

# We need a recent CMake
RUN mkdir /cmake_4_02 && \
    cd /cmake_4_02 && \
    wget https://github.com/Kitware/CMake/releases/download/v4.0.2/cmake-4.0.2-linux-x86_64.sh && \
    chmod +x cmake-4.0.2-linux-x86_64.sh  && \
    ./cmake-4.0.2-linux-x86_64.sh --skip-license --prefix=/usr/local && \
    cmake --version

# Install the AWS cli, since the packaged version is too old
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-2.22.35.zip" -o "awscliv2.zip"
RUN unzip -q awscliv2.zip
RUN ./aws/install
RUN aws --version

# Setup VCPKG n a mounted volume TODO: figure out how to cache this
ARG vcpkg_url
ARG vcpkg_commit
RUN mkdir /vcpkg && \
    cmake --version && \
    cd /vcpkg && \
    git init && \
    git remote add origin $vcpkg_url && \
    git fetch origin $vcpkg_commit && \
    git checkout $vcpkg_commit && \
    ./bootstrap-vcpkg.sh
ENV VCPKG_ROOT=/vcpkg
ENV VCPKG_TOOLCHAIN_PATH=/vcpkg/scripts/buildsystems/vcpkg.cmake
ENV PATH="${VCPKG_ROOT}:${PATH}"

# Common environment variables
ENV GEN=ninja

# Specify where we expect the extension to be mounted and use that as working dir
VOLUME /duckdb_build_dir
WORKDIR /duckdb_build_dir

# Allow git access to the mounted volume
RUN git config --global --add safe.directory "*"

# Mount for ccache to allow restoring ccache in GH actions
VOLUME /ccache_dir
ENV CCACHE_DIR=/ccache_dir
ENV CCACHE_COMPRESS=TRUE
ENV CCACHE_COMPRESSLEVEL=6
ENV CCACHE_MAXSIZE=400M

###
# Conditionally configure some extra dependencies
###
# a `;` separated list of extra toolchains to install (passed in like this to makes things easier through GitHub Actions)
# Note that it should start and end with a `;` e.g. `;rust;parser_tools;`
ARG extra_toolchains

# NOTE: the weird case conditionals are because of bash limitations in the ubuntu image used (see https://stackoverflow.com/questions/229551/how-to-check-if-a-string-contains-a-substring-in-bash)

# Install Parser tools
RUN case "$extra_toolchains" in \
  *\;parser_tools\;*) \
   yum install -y bison flex \
  ;; \
esac

# Install Fortran
RUN case "$extra_toolchains" in \
  *\;fortran\;*) \
    yum install -y gcc-gfortran \
  ;; \
esac

# Configure Rust
ENV PATH="/root/.cargo/bin:${PATH}"
RUN case "$extra_toolchains" in \
  *\;rust\;*) \
    curl https://sh.rustup.rs -sSf | bash -s -- -y; \
  ;; \
esac

# Configure go
ENV PATH="/usr/local/go/bin:${PATH}"
RUN case "$extra_toolchains" in \
  *\;go\;*) \
    wget https://go.dev/dl/go1.20.5.linux-amd64.tar.gz && \
    tar -xzvf go1.20.5.linux-amd64.tar.gz && \
    mv go /usr/local &&\
    go version \
  ;; \
esac

# Install unixODBC
RUN case "$extra_toolchains" in \
  *\;unixodbc\;*) \
    yum install -y unixODBC-devel \
  ;; \
esac

# Install multimedia libraries (FFmpeg, SDL2) for audio/video extensions
# Requires EPEL and RPM Fusion repositories
RUN case "$extra_toolchains" in \
  *\;multimedia\;*) \
    yum install -y epel-release && \
    yum install -y --nogpgcheck https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-8.noarch.rpm && \
    yum install -y nasm SDL2-devel ffmpeg-devel \
  ;; \
esac
