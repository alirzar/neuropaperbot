"""Run every test here, each in its own Python process, and say which passed.

  python tests/run_all.py

Each test works in a throwaway copy of the project with its own database, so your data is never
touched. Telegram, Discord, Gemini, LinkedIn and Bluesky are stand-ins, so no keys are needed.
test_channel.py and test_platforms.py fetch real papers, so they need internet access.
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # test output has emoji; Windows consoles may not
    failed = []
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for test in sorted(HERE.glob("test_*.py")):
        result = subprocess.run(
            [sys.executable, str(test)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
        )
        lines = result.stdout.splitlines()
        passed = sum(1 for line in lines if line.startswith("  ok"))
        print(f"{'ok  ' if result.returncode == 0 else 'FAIL'}  {test.name:20} {passed} checks passed")
        if result.returncode != 0:
            failed.append(test.name)
            problems = [line for line in lines if line.startswith("  FAIL")]
            print("\n".join(problems) if problems else result.stderr[-3000:])
    print("\nAll tests passed." if not failed else f"\nFailed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
