"""Minimal check harness.

Deliberately not pytest: this repo runs on a JupyterHub image where the pipeline's own
dependencies are the only thing guaranteed installed, and a test run has to work from a
plain `python tests/run_all.py`. Each test module defines `run(check)` and calls
`check(name, condition, detail)`.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class Checker:
    def __init__(self, module: str, verbose: bool = True):
        self.module = module
        self.verbose = verbose
        self.passed = 0
        self.failed: list[tuple[str, str]] = []

    def __call__(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            if self.verbose:
                print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
        else:
            self.failed.append((name, detail))
            print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))
        return bool(condition)

    def raises(self, name: str, fn, expected: type = Exception, contains: str = "") -> bool:
        """Checks that `fn()` raises `expected`, optionally with `contains` in the message."""
        try:
            fn()
        except expected as exc:
            if contains and contains.lower() not in str(exc).lower():
                return self(name, False, f"raised {type(exc).__name__} but without {contains!r}: {exc}")
            return self(name, True, f"{type(exc).__name__}: {str(exc)[:90]}")
        except Exception as exc:  # noqa: BLE001
            return self(name, False, f"raised {type(exc).__name__}, expected {expected.__name__}: {exc}")
        return self(name, False, f"did not raise {expected.__name__}")


def run_modules(module_names: list[str]) -> int:
    total_passed, total_failed = 0, []
    for name in module_names:
        print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))
        checker = Checker(name)
        try:
            module = __import__(name, fromlist=["run"])
            module.run(checker)
        except Exception:  # noqa: BLE001
            checker.failed.append((f"{name} (module crashed)", traceback.format_exc().splitlines()[-1]))
            print(traceback.format_exc())
        total_passed += checker.passed
        total_failed += [(name, n, d) for n, d in checker.failed]

    print("\n" + "=" * 70)
    print(f"{total_passed} passed, {len(total_failed)} failed")
    for module, name, detail in total_failed:
        print(f"  FAILED  {module}: {name} -- {detail}")
    return 0 if not total_failed else 1
