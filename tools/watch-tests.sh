#!/bin/sh
# Re-run the tests whenever a source, test, scenario or the Lua bridge changes.
# Settings in [tool.pytest-watcher] of pyproject.toml; extra arguments go to
# pytest (e.g. `tools/watch-tests.sh tests/test_scramble.py -x`).
# The root is passed as an absolute path: with "." watchdog does not see changes.
cd "$(dirname "$0")/.." && exec uv run ptw "$(pwd)" "$@"
