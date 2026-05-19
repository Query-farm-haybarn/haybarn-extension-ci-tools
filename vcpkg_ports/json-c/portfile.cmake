# Haybarn overlay for json-c.
#
# Why this exists: upstream vcpkg's json-c port builds the json-c library
# *and* the apps/ subdirectory (json_parse, json_pointer CLI tools). The
# apps/ code has `static int strict_mode = 0;` in apps/json_parse.c which
# is set but never read. emsdk 5.0.7's bundled clang adds the
# `-Wunused-but-set-global` warning, which (combined with json-c's `-Werror`)
# becomes a fatal compile error for the wasm32-emscripten triplet.
#
# json-c exposes a CMake option BUILD_APPS (default ON) that we set to OFF
# to skip the apps/ subdirectory entirely. Identical to upstream's portfile
# in every other respect; the only addition is `-DBUILD_APPS=OFF` and a
# `port-version` bump in vcpkg.json so the binary-cache key invalidates.

vcpkg_from_github(
    OUT_SOURCE_PATH SOURCE_PATH
    REPO json-c/json-c
    REF "json-c-${VERSION}"
    SHA512 219d8c0da9a4016b74af238cc15dbec1f369a07de160bcc548d80279028e1b5d8d928deb13fec09c96a085fc0ecf10090e309cbe72d0081aca864433c4ae01db
    HEAD_REF master
)

string(COMPARE EQUAL "${VCPKG_LIBRARY_LINKAGE}" "static" JSON_BUILD_STATIC)
string(COMPARE EQUAL "${VCPKG_LIBRARY_LINKAGE}" "dynamic" JSON_BUILD_SHARED)

vcpkg_cmake_configure(
    SOURCE_PATH "${SOURCE_PATH}"
    OPTIONS
        -DBUILD_TESTING=OFF
        -DBUILD_APPS=OFF
        -DBUILD_STATIC_LIBS=${JSON_BUILD_STATIC}
        -DBUILD_SHARED_LIBS=${JSON_BUILD_SHARED}
)

vcpkg_cmake_install()

vcpkg_cmake_config_fixup(CONFIG_PATH "lib/cmake/${PORT}")
vcpkg_fixup_pkgconfig()
vcpkg_copy_pdbs()

file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/debug/include")

vcpkg_install_copyright(FILE_LIST "${SOURCE_PATH}/COPYING")
