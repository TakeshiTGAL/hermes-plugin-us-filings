"""Watch state under the plugin data directory."""
from __future__ import annotations

import json
from pathlib import Path

if __package__:
    from .safe_write import WriteRefused, read_bytes, write_bytes
else:
    from safe_write import WriteRefused, read_bytes, write_bytes

STATE_NAME = "state.json"


def empty_state() -> dict:
    return {"version": 1, "hour": None, "deliver": None, "job_id": None, "companies": []}


def _company_ok(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    if not isinstance(row.get("cik"), str) or not isinstance(row.get("seen"), list):
        return False
    if not isinstance(row.get("rows"), list) or not isinstance(row.get("forms"), list):
        return False
    return isinstance(row.get("baselined"), bool)


def load_state(root: Path) -> tuple[str, dict]:
    """Return (status, state). status is missing, ok, or corrupt. Corrupt is not replaced."""
    try:
        raw = read_bytes(root, STATE_NAME)
    except WriteRefused:
        return "corrupt", empty_state()
    except OSError:
        return "corrupt", empty_state()
    if raw is None:
        return "missing", empty_state()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return "corrupt", empty_state()
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("companies"), list):
        return "corrupt", empty_state()
    if not all(_company_ok(row) for row in data["companies"]):
        return "corrupt", empty_state()
    return "ok", data


def save_state(root: Path, state: dict) -> None:
    payload = json.dumps(state, ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    write_bytes(root, STATE_NAME, payload, create=True)
