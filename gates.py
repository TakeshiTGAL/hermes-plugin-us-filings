"""Approval stash, unattended refusal, and profile name."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

PLUGIN_NAME = "us-filings"
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
REASON_BUDGET = 300
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TRUTHY = {"1", "true", "yes", "on"}
_UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
_LOCK = threading.Lock()
_STASH: dict[tuple, tuple[str, float]] = {}
_STASH_TTL = 600
_bound_profile = "default"


def in_plugin_host_process() -> bool:
    return os.environ.get(HOST_PROCESS_ENV, "").strip() not in {"", "0"}


def reason_fits(text: str) -> bool:
    if "\n" in text or "`" in text or len(text) > REASON_BUDGET:
        return False
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return len(escaped) <= REASON_BUDGET


def plan_rule_key(plan: dict) -> str:
    body = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str)
    return "us_filings_watch:" + hashlib.sha256(body.encode()).hexdigest()[:16]


def one_call_rule_key(plan: dict) -> str:
    """Add reuses the plan digest. Remove builds a new key on every call."""
    if plan.get("action") == "remove":
        stamped = dict(plan)
        stamped["call"] = secrets.token_hex(8)
        return plan_rule_key(stamped)
    return plan_rule_key(plan)


def approve_directive(plan: dict, message: str) -> dict:
    """The card key is the plan digest, or a fresh key when the action is remove."""
    return {"action": "approve", "message": message, "rule_key": one_call_rule_key(plan)}


def host_block_message() -> str | None:
    if in_plugin_host_process():
        return (
            "BLOCKED: this plugin is running in a separate plugin process. "
            "Adding or removing a company is refused there. Nothing was saved."
        )
    return None


def _flag_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def unattended_write_reason() -> str | None:
    """Why add or remove must not run, or None when a person can answer."""
    if in_plugin_host_process():
        return None
    if _flag_on("HERMES_CRON_SESSION"):
        return "a cron job"
    module = None
    try:
        module = importlib.import_module("cron.execution_identity")
    except Exception:
        module = None
    if module is not None:
        fn = getattr(module, "current_cron_execution", None)
        if not callable(fn):
            return "a turn whose cron mark could not be read"
        try:
            mark = fn()
        except Exception:
            return "a turn whose cron mark could not be read"
        if mark is not None:
            return "a cron job"
    if _flag_on("HERMES_SINGLE_QUERY_SESSION"):
        return "a single-query run (hermes -q)"
    platform = os.environ.get("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform in _UNATTENDED_PLATFORMS:
        return f"an unattended {platform} session"
    return None


def unattended_write_block() -> str | None:
    reason = unattended_write_reason()
    if reason is None:
        return None
    return (
        f"BLOCKED: {reason} cannot add or remove a company, even after always. "
        "Nothing was saved."
    )


def clear_watch_stash(ids: tuple[str, str]) -> None:
    with _LOCK:
        for key in [item for item in _STASH if item[2:] == ids]:
            del _STASH[key]


def _plan_key(plan: dict, ids: tuple[str, str]) -> tuple:
    body = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str)
    return ("us_filings", body, ids[0], ids[1])


def remember_watch(plan: dict, ids: tuple[str, str], question: str) -> None:
    with _LOCK:
        _STASH[_plan_key(plan, ids)] = (question, time.monotonic())


def take_watch(plan: dict, ids: tuple[str, str], question: str) -> str | None:
    with _LOCK:
        found = _STASH.pop(_plan_key(plan, ids), None)
    if found is None:
        return "BLOCKED: the watch change was not approved. Nothing was saved."
    stored, made = found
    if time.monotonic() - made > _STASH_TTL:
        return "BLOCKED: the approval is too old. Nothing was saved."
    if stored != question:
        return "BLOCKED: the target changed after the preview. Nothing was saved."
    return None


def plugin_data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir as read_dir
    except Exception as exc:
        raise RuntimeError("plugin_data_dir is not available") from exc
    path = read_dir(PLUGIN_NAME)
    if not isinstance(path, Path):
        path = Path(path)
    return path


def bind_profile_name(name: object) -> None:
    global _bound_profile
    if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name.strip()):
        _bound_profile = "default"
        return
    _bound_profile = name.strip()


def active_profile_name() -> str:
    return _bound_profile


def load_platforms() -> set[str] | None:
    try:
        from gateway.platform_registry import platform_registry
    except Exception:
        return None
    try:
        listed = list(platform_registry.registered_names())
    except Exception:
        return None
    found: set[str] = set()
    for name in listed:
        text = str(name).strip().lower()
        if not text or text in {"cli", "cron", "api_server"}:
            continue
        try:
            entry = platform_registry.get(text)
        except Exception:
            return None
        if entry is not None and getattr(entry, "cron_deliver_env_var", None):
            found.add(text)
    found.update({"local", "origin"})
    return found
