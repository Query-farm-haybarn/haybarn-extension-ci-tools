"""Haybarn: make `import duckdb` resolve to the `haybarn` package in this Python environment.

Run with the venv's interpreter. Writes a `duckdb` package into its site-packages that replaces
itself with `haybarn` on import, so duckdb_sqllogictest and extension test scripts that import
`duckdb` run on the Haybarn engine. Refuses to overwrite a real `duckdb` installation.
"""

import sys
import sysconfig
from pathlib import Path

SHIM = '''"""Haybarn test shim (extension-ci-tools scripts/haybarn_duckdb_alias.py): duckdb is haybarn."""
import sys

import haybarn

sys.modules[__name__] = haybarn
'''


def main() -> int:
    import haybarn  # fail here, not at test time, if the venv has no haybarn

    package = Path(sysconfig.get_paths()["purelib"]) / "duckdb"
    init = package / "__init__.py"
    if package.exists() and not (init.is_file() and init.read_text().startswith(SHIM.splitlines()[0])):
        print(f"{package} is a real duckdb installation; not replacing it", file=sys.stderr)
        return 1
    package.mkdir(exist_ok=True)
    init.write_text(SHIM)

    import duckdb

    if duckdb is not haybarn:
        print("the duckdb shim did not resolve to haybarn", file=sys.stderr)
        return 1
    print(f"duckdb -> haybarn {haybarn.__version__} ({Path(haybarn.__file__).parent})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
