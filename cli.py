"""Local command: hermes us-filings call."""
from __future__ import annotations

import argparse
import json
import sys

sys.dont_write_bytecode = True

if __package__:
    from .tools import HANDLERS
else:
    from tools import HANDLERS


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="command", required=True)
    call = sub.add_parser("call", help="Call one tool and print JSON")
    call.add_argument("tool")
    call.add_argument("--json", default="{}")


def handle(args: argparse.Namespace) -> int:
    if args.command != "call":
        sys.stderr.write("Pass call.\n")
        return 2
    handler = HANDLERS.get(args.tool)
    if handler is None:
        sys.stderr.write("Unknown tool.\n")
        return 2
    try:
        payload = json.loads(args.json)
    except json.JSONDecodeError:
        sys.stderr.write("The JSON argument could not be read.\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("The JSON argument must be an object.\n")
        return 2
    sys.stdout.write(handler(payload, session_id="cli", task_id="cli"))
    sys.stdout.write("\n")
    return 0
