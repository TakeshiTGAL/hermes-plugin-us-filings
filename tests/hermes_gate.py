"""Run inside a clean process. Prints ok. Does not print the contact."""
import json
import os
import sys
from pathlib import Path

home = Path(sys.argv[1])
root = sys.argv[2]
os.environ["HOME"] = str(home / "user")
os.environ["HERMES_HOME"] = str(home / "hh")
(home / "user").mkdir(parents=True, exist_ok=True)
(home / "hh").mkdir(parents=True, exist_ok=True)

from agent.secret_scope import set_secret_scope
from hermes_cli.plugins import _dispatch_pre_tool_call_hooks, get_plugin_manager
from tools import approval
from tools import approval_context

set_secret_scope({"US_FILINGS_CONTACT": "Ada Example ada@example.com"})
sys.path.insert(0, root)

import client
import gates
import watch

client.GAP_SECONDS = 0
client._sleep = lambda _seconds: None
client.reset_runtime()

from client import TICKERS_URL, submissions_url

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
}
APPLE = {
    "cik": "0000320193",
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000026"],
            "form": ["10-K"],
            "filingDate": ["2026-11-03"],
            "primaryDocument": ["aapl-20260926.htm"],
            "acceptanceDateTime": ["2026-11-03T18:00:00.000Z"],
        },
        "files": [],
    },
}
BERK = {
    "cik": "0001067983",
    "name": "BERKSHIRE HATHAWAY INC",
    "tickers": ["BRK-B"],
    "filings": {
        "recent": {
            "accessionNumber": ["0001067983-26-000001"],
            "form": ["10-K"],
            "filingDate": ["2026-11-01"],
            "primaryDocument": ["brk.htm"],
            "acceptanceDateTime": [""],
        },
        "files": [],
    },
}
books = {
    TICKERS_URL: (200, TICKERS),
    submissions_url("0000320193"): (200, APPLE),
    submissions_url("0001067983"): (200, BERK),
}


class Transport:
    def __init__(self):
        self.calls = 0

    def __call__(self, url, contact):
        self.calls += 1
        if contact != "Ada Example ada@example.com":
            raise SystemExit("contact mismatch")
        status, body = books[url]
        return status, json.dumps(body).encode("utf-8")


class Fake:
    def __init__(self):
        self.jobs = []
        self.calls = []

    def load_jobs(self, argv):
        self.calls.append(("list", list(argv)))
        return [dict(job) for job in self.jobs]

    def run_argv(self, argv):
        from public_cron import Cmd

        self.calls.append(("run", list(argv)))
        return Cmd(0, "", "")

    def create_from_argv(self, argv, *, name, schedule, deliver):
        self.calls.append(("create", list(argv)))
        job = {"id": "job1", "name": name, "schedule": schedule, "deliver": deliver}
        self.jobs.append(job)
        return dict(job)


data = home / "data"
hermes_home = home / "hh"
deps = watch.Deps(
    data_dir=data,
    cron=Fake(),
    platforms={"local", "origin"},
    profile="default",
    home=hermes_home,
    transport=Transport(),
)
data.mkdir()
watch.deps_for_hook = lambda: deps

manager = get_plugin_manager()
manager._discovered = True
manager._hooks["pre_tool_call"] = [watch.on_pre_tool_call]

args = {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}
os.environ["HERMES_CRON_SESSION"] = "1"
approval._yolo_active = lambda: True
block, _modified = _dispatch_pre_tool_call_hooks("us_filings_watch", args, session_id="s", task_id="t")
if not block or "BLOCKED" not in block:
    raise SystemExit("unattended yolo was not blocked: " + str(block))
body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
if body.get("ok"):
    raise SystemExit("unattended handler wrote")
if (data / "state.json").exists():
    raise SystemExit("unattended left a state file")

os.environ.pop("HERMES_CRON_SESSION", None)
gates._STASH.clear()
approval._yolo_active = lambda: True
approval_context._get_approval_mode = lambda: "ask"
block, _modified = _dispatch_pre_tool_call_hooks("us_filings_watch", args, session_id="s", task_id="t")
if block is not None:
    raise SystemExit("yolo with a person was blocked: " + str(block))
body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
if not body.get("ok"):
    raise SystemExit("yolo with a person did not write: " + json.dumps(body)[:300])
if not any(call[0] == "create" for call in deps.cron.calls):
    raise SystemExit("cron create was not the approved argv")

gates._STASH.clear()
second = {"action": "add", "ticker": "BRK.B", "hour": 8, "deliver": "local"}
denied = json.loads(watch.watch_tool(deps, second, session_id="s2", task_id="t2"))
if denied.get("ok"):
    raise SystemExit("handler wrote without the hook")

approval._yolo_active = lambda: False
approval_context._get_approval_mode = lambda: "off"
gates._STASH.clear()
block, _modified = _dispatch_pre_tool_call_hooks("us_filings_watch", second, session_id="s2", task_id="t2")
if block is not None:
    raise SystemExit("approvals off was blocked: " + str(block))
body = json.loads(watch.watch_tool(deps, second, session_id="s2", task_id="t2"))
if not body.get("ok"):
    raise SystemExit("approvals off did not write after the hook: " + json.dumps(body)[:300])


def other(tool_name="", **_kwargs):
    if tool_name == "us_filings_watch":
        return {"action": "block", "message": "other plugin block"}
    return None


manager._hooks["pre_tool_call"] = [watch.on_pre_tool_call, other]
third = {"action": "add", "cik": "0000000003", "hour": 8, "deliver": "local"}
block, _modified = _dispatch_pre_tool_call_hooks("us_filings_watch", third, session_id="s3", task_id="t3")
if block != "other plugin block":
    raise SystemExit("other block lost: " + str(block))

read_block, _modified = _dispatch_pre_tool_call_hooks(
    "us_filings_recent", {"ticker": "AAPL"}, session_id="s4", task_id="t4"
)
if read_block is not None:
    raise SystemExit("a read tool was blocked")
print("ok")
