from __future__ import annotations

import argparse
import plistlib
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install",))
    parser.add_argument("--plist", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = {
        "Label": args.label,
        "ProgramArguments": [
            str(args.program.resolve()),
            str(args.project.resolve()),
        ],
        "WorkingDirectory": str(args.project.resolve()),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(
            args.project.resolve() / ".menubar.stdout.log"
        ),
        "StandardErrorPath": str(
            args.project.resolve() / ".menubar.stderr.log"
        ),
    }
    args.plist.parent.mkdir(parents=True, exist_ok=True)
    with args.plist.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
