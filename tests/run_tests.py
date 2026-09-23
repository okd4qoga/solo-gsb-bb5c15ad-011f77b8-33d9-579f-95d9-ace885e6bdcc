"""Minimal pytest-compatible runner for environments without pip.

Discovers test_* functions in tests/test_*.py and runs them, reporting
failures with tracebacks. Supports the tiny subset of pytest semantics the
suite uses (plain asserts, no fixtures).
"""
import importlib.util
import inspect
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    passed = failed = 0
    failures = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("test_"):
                continue
            try:
                fn()
                passed += 1
            except Exception:
                failed += 1
                failures.append((f"{path.name}::{name}", traceback.format_exc()))
    for nodeid, tb in failures:
        print(f"\n=== FAIL {nodeid}\n{tb}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
