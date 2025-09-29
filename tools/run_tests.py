#!/usr/bin/env python3
"""
Test runner script for m3u-proxy

Usage:
    python run_tests.py              # Run all tests
    python run_tests.py --unit       # Run only unit tests
    python run_tests.py --integration # Run only integration tests
    python run_tests.py --coverage   # Run with coverage report
"""

import sys
import subprocess
import argparse


def run_command(cmd):
    """Run a command and return the exit code"""
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Run tests for m3u-proxy")
    parser.add_argument("--unit", action="store_true",
                        help="Run only unit tests")
    parser.add_argument("--integration", action="store_true",
                        help="Run only integration tests")
    parser.add_argument("--coverage", action="store_true",
                        help="Run with coverage report")
    parser.add_argument("--verbose", "-v",
                        action="store_true", help="Verbose output")
    parser.add_argument("--file", help="Run specific test file")

    args = parser.parse_args()

    # Base pytest command
    cmd = ["python", "-m", "pytest"]

    if args.verbose:
        cmd.append("-vv")

    if args.coverage:
        cmd.extend(["--cov=src", "--cov-report=html",
                   "--cov-report=term-missing"])

    # Select which tests to run
    if args.unit:
        cmd.extend(["-m", "not integration"])
    elif args.integration:
        cmd.extend(["-m", "integration"])
    elif args.file:
        cmd.append(args.file)
    else:
        # Run all tests
        cmd.append("tests/")

    # Run the tests
    exit_code = run_command(cmd)

    if exit_code == 0:
        print("\n✅ All tests passed!")
    else:
        print(f"\n❌ Tests failed with exit code {exit_code}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
