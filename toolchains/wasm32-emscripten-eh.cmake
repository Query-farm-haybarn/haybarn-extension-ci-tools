# Native-EH wasm32-emscripten vcpkg triplet for Haybarn.
#
# Identical to vcpkg's stock community `wasm32-emscripten` triplet, but compiles
# every dependency with `-fwasm-exceptions` (native wasm exception handling),
# matching the engine (also built -fwasm-exceptions). Without it the dependency
# tree (gdal, geos, proj, sqlite3, ...) compiles with *legacy* emscripten EH and
# emits invoke_*/__resumeException/getTempRet0 trampolines the native-EH engine
# never provides — so a dependency's try/catch is inert and the exception
# escapes uncaught. Concretely: PROJ's guarded `std::stoi` on the non-numeric
# OGC:CRS84 code (iso19111/common.cpp) throws a fatal `stoi: no conversion`
# instead of being caught. See haybarn-wasm#9.
#
# Used by the `wasm_eh` platform. `wasm_mvp` deliberately stays on the stock
# `wasm32-emscripten` triplet (legacy EH — it has no shipped native-EH engine);
# `wasm_threads` uses wasm32-emscripten-threads (which adds -fwasm-exceptions
# too, plus -pthread). Selecting a distinct triplet name here also gives eh deps
# their own vcpkg ABI hash, so they cache independently of the mvp build and
# can't restore stale legacy-EH artifacts.
set(VCPKG_TARGET_ARCHITECTURE wasm32)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE static)

set(VCPKG_CMAKE_SYSTEM_NAME Emscripten)

if(NOT DEFINED ENV{EMSDK})
    message(FATAL_ERROR "The EMSDK environment variable must be defined to use the wasm32-emscripten-eh triplet")
endif()

# Chainload a wrapper that include()s Emscripten.cmake and then appends
# -fwasm-exceptions. We must NOT rely on VCPKG_C_FLAGS here: vcpkg sets
# CMAKE_C_FLAGS from VCPKG_C_FLAGS, but Emscripten.cmake then overwrites it,
# dropping the flag. The wrapper appends after the include, where it survives.
# See wasm32-emscripten-eh-toolchain.cmake.
set(VCPKG_CHAINLOAD_TOOLCHAIN_FILE "${CMAKE_CURRENT_LIST_DIR}/wasm32-emscripten-eh-toolchain.cmake")
set(VCPKG_ENV_PASSTHROUGH_UNTRACKED EMSDK PATH)

# Cache-bust marker. vcpkg's package ABI hashes this triplet file but not
# necessarily the chainloaded toolchain file's contents — so bump this whenever
# wasm32-emscripten-eh-toolchain.cmake changes, to force a dependency rebuild
# instead of restoring stale (e.g. legacy-EH) artifacts.
# eh-toolchain-abi: 1

# Release-only: the wasm extensions link the Release dependency libs (lib/*.a);
# the Debug libs (debug/lib/*.a) are never used here. Skipping them roughly
# halves vcpkg build time and binary-cache size. NOTE: this is an intentional
# change from the stock `wasm32-emscripten` triplet (which builds debug+release)
# that wasm_eh used previously; functionally safe since only lib/*.a is linked.
set(VCPKG_BUILD_TYPE release)
