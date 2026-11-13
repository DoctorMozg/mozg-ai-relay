"""Starts mz-relay from the plugin's own files.

`python3 -I` keeps PYTHONPATH and the user site off the path, so a stray json.py there cannot
replace a stdlib module.
"""

from __future__ import annotations

import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 12)
SRC = Path(__file__).resolve().parents[1] / "src"
EXIT_USAGE = 2
# Claude reads a hook's exit 2 as "block this tool" or "wake me with stderr as the prompt".
EXIT_HOOK_FAILED = 0


def run(argv: list[str]) -> int:
    failed = EXIT_HOOK_FAILED if argv[:1] == ["hook"] else EXIT_USAGE
    if sys.version_info < MINIMUM_PYTHON:
        found = ".".join(str(part) for part in sys.version_info[:3])
        sys.stderr.write(f"mz-relay: needs Python 3.12 or newer, found {found}\n")
        return failed
    sys.path.insert(0, str(SRC))
    try:
        from mz_relay.cli import main
    except (ImportError, SyntaxError) as error:
        # SyntaxError: a module caught half-written by a plugin update.
        sys.stderr.write(f"mz-relay: cannot load the package from {SRC}: {error}\n")
        return failed
    return int(main(argv))


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
