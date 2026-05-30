# Threaded wasm32-emscripten vcpkg triplet for Haybarn.
#
# Identical to vcpkg's stock community `wasm32-emscripten` triplet, but compiles
# every dependency with `-pthread`, which makes emcc enable the wasm `atomics`
# and `bulk-memory` features. Without those features the resulting object files
# cannot be linked into a shared-memory DuckDB extension built for the
# `wasm_threads` (COI) platform: wasm-ld rejects `--shared-memory` against any
# object that was "not compiled with 'atomics' or 'bulk-memory' features".
#
# This is the missing half of the wasm_threads shared-memory fix — the engine
# and extension objects are built `-pthread` (via USE_WASM_THREADS=1), but the
# vcpkg dependency tree (aws-sdk, curl, gdal, geos, proj, openssl, ...) must be
# built `-pthread` too. Selecting this triplet for the wasm_threads platform
# also gives those deps their own vcpkg ABI hash, so they cache independently
# of the non-threaded wasm_eh/wasm_mvp builds.
set(VCPKG_TARGET_ARCHITECTURE wasm32)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE static)

set(VCPKG_CMAKE_SYSTEM_NAME Emscripten)

if(NOT DEFINED ENV{EMSDK})
    message(FATAL_ERROR "The EMSDK environment variable must be defined to use the wasm32-emscripten-threads triplet")
endif()

# Chainload a wrapper that include()s Emscripten.cmake and then appends
# -pthread. We must NOT rely on VCPKG_C_FLAGS=-pthread here: vcpkg sets
# CMAKE_C_FLAGS from VCPKG_C_FLAGS, but Emscripten.cmake then overwrites it,
# dropping the flag — so deps compiled WITHOUT atomics/bulk-memory and the
# wasm_threads --shared-memory link failed. The wrapper appends after the
# include, where it survives. See wasm32-emscripten-threads-toolchain.cmake.
set(VCPKG_CHAINLOAD_TOOLCHAIN_FILE "${CMAKE_CURRENT_LIST_DIR}/wasm32-emscripten-threads-toolchain.cmake")
set(VCPKG_ENV_PASSTHROUGH_UNTRACKED EMSDK PATH)

# Cache-bust marker. vcpkg's package ABI hashes this triplet file but not
# necessarily the chainloaded toolchain file's contents — so bump this whenever
# wasm32-emscripten-threads-toolchain.cmake changes, to force a dependency
# rebuild instead of restoring stale (e.g. non-atomics or legacy-EH) artifacts.
# pthread-toolchain-abi: 2

# Release-only: the wasm extensions link the Release dependency libs (lib/*.a);
# the Debug libs (debug/lib/*.a) vcpkg builds by default are never used here.
# Skipping them roughly halves vcpkg build time and binary-cache size.
set(VCPKG_BUILD_TYPE release)
