#!/usr/bin/env python3
"""Run unittest and emit a machine-readable result independent of test names."""
from __future__ import annotations

import argparse
import json
import sys
import unittest


SCHEMA = "ainote.unittest-result/v1"
MARKER = "AINOTE_UNITTEST_RESULT "


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-zero-skips", action="store_true")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--start-directory", default=".")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("tests", nargs="*")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    loader = unittest.defaultTestLoader
    if args.discover:
        if args.tests:
            raise SystemExit("test names cannot be combined with --discover")
        suite = loader.discover(args.start_directory, pattern=args.pattern)
    else:
        if not args.tests:
            raise SystemExit("provide test names or --discover")
        suite = loader.loadTestsFromNames(args.tests)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    payload = {
        "schema": SCHEMA,
        "tests_run": result.testsRun,
        "skipped": len(result.skipped),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "successful": result.wasSuccessful(),
    }
    print(MARKER + json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if not result.wasSuccessful() or result.testsRun < 1:
        return 1
    if args.require_zero_skips and result.skipped:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
