# Threaded-wasm chainload toolchain for Haybarn.
#
# Loaded via VCPKG_CHAINLOAD_TOOLCHAIN_FILE from wasm32-emscripten-threads.cmake.
#
# Why this wrapper exists instead of setting VCPKG_C_FLAGS=-pthread directly:
# vcpkg applies VCPKG_C_FLAGS by initialising CMAKE_C_FLAGS, then includes the
# chainload toolchain. Emscripten.cmake OVERWRITES CMAKE_C_FLAGS, so the
# -pthread set via VCPKG_C_FLAGS is silently dropped — every dependency then
# compiles WITHOUT the wasm `atomics`/`bulk-memory` features. Static-archive
# builds don't fail without those features; the gap only surfaces at the final
# `--shared-memory` link of a wasm_threads extension (e.g. avro -> jansson's
# hashtable_seed.c.o). Appending -pthread *after* the Emscripten include is the
# only place it reliably survives.
include("$ENV{EMSDK}/upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake")

# Append to both the regular and *_INIT flag vars so it sticks regardless of
# which form Emscripten.cmake / the consuming project ultimately reads.
# -pthread enables -matomics -mbulk-memory at compile time (and shared memory
# at link time); duplicate -pthread is harmless.
foreach(_lang C CXX)
  string(APPEND CMAKE_${_lang}_FLAGS " -pthread")
  string(APPEND CMAKE_${_lang}_FLAGS_INIT " -pthread")
endforeach()
foreach(_link EXE SHARED MODULE)
  string(APPEND CMAKE_${_link}_LINKER_FLAGS " -pthread")
  string(APPEND CMAKE_${_link}_LINKER_FLAGS_INIT " -pthread")
endforeach()
