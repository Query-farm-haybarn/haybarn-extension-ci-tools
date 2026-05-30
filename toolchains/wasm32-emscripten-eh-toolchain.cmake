# Native-EH wasm chainload toolchain for Haybarn.
#
# Loaded via VCPKG_CHAINLOAD_TOOLCHAIN_FILE from wasm32-emscripten-eh.cmake.
#
# Why this wrapper exists instead of setting VCPKG_C_FLAGS directly:
# vcpkg applies VCPKG_C_FLAGS by initialising CMAKE_C_FLAGS, then includes the
# chainload toolchain. Emscripten.cmake OVERWRITES CMAKE_C_FLAGS, so flags set
# via VCPKG_C_FLAGS are silently dropped. Appending *after* the Emscripten
# include is the only place they reliably survive.
#
# -fwasm-exceptions builds every dependency with *native* wasm exception
# handling, matching the engine. Without it deps compile with legacy emscripten
# EH and emit invoke_*/__resumeException/getTempRet0 trampolines the native-EH
# engine never provides, so a dep's try/catch is inert and the exception escapes
# uncaught (e.g. PROJ's guarded std::stoi on the OGC:CRS84 code throws a fatal
# `stoi: no conversion`). See haybarn-wasm#9.
include("$ENV{EMSDK}/upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake")

# Append to both the regular and *_INIT flag vars so it sticks regardless of
# which form Emscripten.cmake / the consuming project ultimately reads.
# Duplicate flags are harmless. -fwasm-exceptions on C TUs is a no-op (C emits
# no EH lowering) but applied uniformly so mixed C/C++ archives stay consistent.
foreach(_lang C CXX)
  string(APPEND CMAKE_${_lang}_FLAGS " -fwasm-exceptions")
  string(APPEND CMAKE_${_lang}_FLAGS_INIT " -fwasm-exceptions")
endforeach()
foreach(_link EXE SHARED MODULE)
  string(APPEND CMAKE_${_link}_LINKER_FLAGS " -fwasm-exceptions")
  string(APPEND CMAKE_${_link}_LINKER_FLAGS_INIT " -fwasm-exceptions")
endforeach()
