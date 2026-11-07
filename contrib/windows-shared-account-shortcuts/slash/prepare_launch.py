"""Select the shared official login before the existing Claude wrapper starts."""
from __future__ import annotations
import argparse
import json
import subprocess
import shutil
import sys
from pathlib import Path
from switch_hook import BASE, account_problem


def prepare(number: str, *, check: bool = False, runner=subprocess.run) -> int:
    executable = BASE / "venv" / "Scripts" / "cswap.exe"
    wrapper = shutil.which("claude")
    problem = account_problem()
    available = executable.is_file() and wrapper is not None
    if check:
        print(json.dumps({"account": number, "files_ready": available,
                          "official_source_ready": problem is None,
                          "message": problem, "account_changed": False,
                          "claude_started": False}, ensure_ascii=True))
        return 0 if available and problem is None else 1
    if problem or not available:
        print(problem or "Missing cswap.exe or Claude Code on PATH.", file=sys.stderr)
        return 1
    print(f"Selecting shared default Claude account {number}. Existing default-profile windows follow.", flush=True)
    try:
        result = runner([str(executable), "switch", number], timeout=75)
    except subprocess.TimeoutExpired:
        print("Account selection timed out; state needs checking. Claude was not started.", file=sys.stderr)
        return 1
    except OSError:
        print("Could not run account switcher. Claude was not started.", file=sys.stderr)
        return 1
    if result.returncode:
        print("Account selection failed. Claude was not started.", file=sys.stderr)
        return result.returncode
    problem = account_problem()
    if problem:
        print(problem + " Claude was not started.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", choices=("1", "2"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return prepare(args.account, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
