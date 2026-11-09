"""Morning watch: one message when a new accession appears."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

if __package__:
    from . import client, gates
    from .filings import (
        Window,
        WindowError,
        document_urls,
        form_allowed,
        norm_form,
        observe,
        pad_cik,
        parse_submissions,
        prepare_accession,
        prepare_token,
        read_company_token,
        resolve_ticker,
    )
    from .public_cron import CronUnread, Store
    from .safe_write import WriteRefused, remove_file, write_bytes
    from .store import empty_state, load_state, save_state
else:
    import client
    import gates
    from filings import (
        Window,
        WindowError,
        document_urls,
        form_allowed,
        norm_form,
        observe,
        pad_cik,
        parse_submissions,
        prepare_accession,
        prepare_token,
        read_company_token,
        resolve_ticker,
    )
    from public_cron import CronUnread, Store
    from safe_write import WriteRefused, remove_file, write_bytes
    from store import empty_state, load_state, save_state

JOB_NAME = "us-filings"
SCRIPT_NAME = "us-filings-watch.py"
PROMPT = "US Filings morning check"
MAX_COMPANIES = 20
STATE_REL = "plugin-data/us-filings/state.json"

WATCH_SCRIPT = '''"""US Filings morning check. Empty stdout sends nothing."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def main() -> int:
    try:
        from hermes_constants import get_hermes_home
        from agent.secret_scope import build_profile_secret_scope, set_secret_scope
    except Exception:
        sys.stderr.write(
            "The profile secret scope could not be read. US_FILINGS_CONTACT was not treated as unset.\\n"
        )
        return 1
    try:
        home = Path(get_hermes_home())
        set_secret_scope(build_profile_secret_scope(home))
    except Exception:
        sys.stderr.write(
            "The profile secret scope could not be read. US_FILINGS_CONTACT was not treated as unset.\\n"
        )
        return 1
    plugin = home / "plugins" / "us-filings"
    if not (plugin / "watch.py").is_file():
        sys.stderr.write("us-filings is not installed, so the morning check did not run.\\n")
        return 1
    sys.path.insert(0, str(plugin))
    try:
        import watch
    except Exception:
        sys.stderr.write("us-filings is not installed, so the morning check did not run.\\n")
        return 1
    code, text = watch.watch_stdout()
    if text:
        sys.stdout.write(text)
        if not text.endswith("\\n"):
            sys.stdout.write("\\n")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
'''


class Deps:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        cron: Store | None = None,
        platforms: set[str] | None = None,
        profile: str | None = None,
        home: Path | None = None,
        transport=None,
    ) -> None:
        self.data_dir = data_dir
        self.cron = cron if cron is not None else Store()
        self.platforms = platforms
        self.profile = gates.active_profile_name() if profile is None else profile
        self.home = home
        self.transport = transport


def make_deps() -> Deps:
    return Deps()


def deps_for_hook() -> Deps:
    return make_deps()


def dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _fail(kind: str, error: str, next_step: str = "") -> str:
    body: dict[str, Any] = {"ok": False, "error_type": kind, "kind": kind, "error": error}
    if next_step:
        body["next_step"] = next_step
    return dump(body)


def _sec_fail(exc: Exception) -> str:
    kind = getattr(exc, "kind", "error")
    step = getattr(exc, "next_step", "") or ""
    return _fail(str(kind), str(exc), step)


def _ids(kwargs: dict) -> tuple[str, str]:
    return (str(kwargs.get("session_id") or ""), str(kwargs.get("task_id") or ""))


class StorageUnavailable(Exception):
    """plugin_data_dir could not be opened. Callers must not pick another path."""


def _root(deps: Deps) -> Path:
    if deps.data_dir is not None:
        return Path(deps.data_dir)
    try:
        return gates.plugin_data_dir()
    except Exception:
        raise StorageUnavailable() from None


def _home(deps: Deps) -> Path:
    if deps.home is not None:
        return Path(deps.home)
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _platforms(deps: Deps) -> set[str] | None:
    if deps.platforms is not None:
        return set(deps.platforms)
    return gates.load_platforms()


def cron_prefix(profile: str) -> list[str]:
    if profile != "default":
        return ["-p", profile, "cron"]
    return ["cron"]


def list_argv(profile: str) -> list[str]:
    return [*cron_prefix(profile), "list", "--all"]


def create_argv(profile: str, hour: int, deliver: str) -> list[str]:
    schedule = f"0 {hour} * * *"
    return [
        *cron_prefix(profile),
        "create",
        schedule,
        PROMPT,
        "--name",
        JOB_NAME,
        "--deliver",
        deliver,
        "--no-agent",
        "--script",
        SCRIPT_NAME,
    ]


def remove_argv(profile: str, job_id: str) -> list[str]:
    return [*cron_prefix(profile), "remove", job_id]


def command_text(argv: list[str]) -> str:
    return "hermes " + " ".join(shlex.quote(part) for part in argv)


def canonical_deliver(deliver: object, platforms: set[str] | None) -> str | None:
    if platforms is None or not isinstance(deliver, str):
        return None
    piece = deliver.strip()
    if not piece or any(ch.isspace() for ch in piece):
        return None
    low = piece.lower()
    if low in {"cli", "cron", "api_server"}:
        return None
    if low in {"local", "origin"}:
        return low
    platform, sep, chat = piece.partition(":")
    key = platform.lower()
    if key not in platforms:
        return None
    if not sep:
        return key
    if not chat or any(ch.isspace() for ch in chat):
        return None
    return f"{key}:{chat}"


def _forms(value: object) -> tuple[str, list[str]]:
    if value is None:
        return "", []
    if not isinstance(value, list) or len(value) > 12:
        return "forms", []
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return "forms", []
        try:
            found.append(norm_form(item))
        except WindowError:
            return "forms", []
    return "", found


def _ticker_keys(value: str) -> set[str]:
    text = value.strip().upper()
    if not text:
        return set()
    return {text, text.replace(".", "-"), text.replace("-", ".")}


def _company_match(row: dict, ticker: str, cik: str) -> bool:
    if cik and row.get("cik") == cik:
        return True
    if ticker and _ticker_keys(ticker) & _ticker_keys(str(row.get("ticker") or "")):
        return True
    return False


def _find_job(deps: Deps) -> dict | None:
    jobs = deps.cron.load_jobs(list_argv(deps.profile))
    for job in jobs:
        if job.get("name") == JOB_NAME:
            return job
    return None


def _message(plan: dict) -> str:
    if plan.get("ticker"):
        ident = "ticker " + plan["ticker"]
    else:
        ident = "CIK " + plan["cik"]
    forms = ""
    if plan.get("forms"):
        forms = " forms " + ",".join(plan["forms"])
    text = (
        f"Approve us_filings_watch {plan['action']} {ident} hour {plan.get('hour')} "
        f"deliver {plan.get('deliver')}{forms}. Writes {STATE_REL}."
    )
    if plan.get("remove_argv"):
        text += " " + command_text(plan["remove_argv"]) + "."
    if plan.get("create_argv"):
        text += " " + command_text(plan["create_argv"]) + "."
    elif plan["action"] == "add":
        text += " No cron command runs."
    if plan.get("remove_script"):
        text += f" Removes scripts/{SCRIPT_NAME}."
    elif plan["action"] == "remove":
        text += " The morning job stays."
    return text


def _load(deps: Deps) -> tuple[str, dict, Path]:
    root = _root(deps)
    status, state = load_state(root)
    return status, state, root


def classify(deps: Deps, args: dict) -> dict:
    """Build a plan, or an invalid or unchanged result. This does not call SEC."""
    action = args.get("action")
    if action not in {"add", "remove"}:
        return {"kind": "invalid", "error": _fail("action", "Pass action add, remove, or list.", "Use add, remove, or list.")}
    extra = set(args) - {"action", "ticker", "cik", "hour", "deliver", "forms"}
    if extra:
        return {"kind": "invalid", "error": _fail("arguments", "Unknown arguments were passed.", "Pass only the watch fields.")}
    ticker = args.get("ticker", "")
    cik = args.get("cik", "")
    if isinstance(ticker, str):
        ticker = prepare_token(ticker)
    if isinstance(cik, str):
        cik = prepare_token(cik)
    if bool(ticker) == bool(cik):
        return {"kind": "invalid", "error": _fail("company", "Pass a ticker or a CIK.", "Pass one of ticker or cik.")}
    if ticker and not isinstance(ticker, str):
        return {"kind": "invalid", "error": _fail("ticker", "Ticker must be a string.", "Pass one ticker.")}
    if cik and not isinstance(cik, str):
        return {"kind": "invalid", "error": _fail("cik", "CIK must be a string of digits.", "Pass one CIK.")}
    try:
        if ticker:
            ticker, found_cik = read_company_token(ticker, field="ticker")
            if found_cik:
                ticker = ""
                cik = pad_cik(found_cik)
        else:
            found_ticker, cik = read_company_token(cik, field="cik")
            if found_ticker:
                ticker = found_ticker
                cik = ""
            else:
                cik = pad_cik(cik)
    except WindowError as exc:
        return {"kind": "invalid", "error": _sec_fail(exc)}
    status, state, _root_path = _load(deps)
    if status == "corrupt":
        return {"kind": "block", "message": "BLOCKED: the watch state could not be read. Nothing was saved."}
    try:
        job = _find_job(deps)
    except CronUnread as exc:
        return {"kind": "block", "message": "BLOCKED: hermes cron list could not be read. Nothing was saved. " + str(exc)}
    if action == "remove":
        match = next((row for row in state["companies"] if _company_match(row, ticker or "", cik or "")), None)
        if match is None:
            return {"kind": "invalid", "error": _fail("missing", "That company is not in the watch.", "Use list to see saved companies.")}
        others = [row for row in state["companies"] if row.get("cik") != match.get("cik")]
        plan = {
            "action": "remove",
            "ticker": match.get("ticker") or "",
            "cik": match.get("cik"),
            "hour": state.get("hour"),
            "deliver": state.get("deliver") or "",
            "forms": list(match.get("forms") or []),
            "remove_argv": remove_argv(deps.profile, str(job["id"])) if job and not others else [],
            "create_argv": [],
            "remove_script": not others,
            "job_id": job.get("id") if job else "",
        }
        plan["message"] = _message(plan)
        return {"kind": "plan", "plan": plan}
    form_error, forms = _forms(args.get("forms"))
    if form_error:
        return {"kind": "invalid", "error": _fail("forms", "Each form filter must be a short form name.", "Pass forms such as 10-K or 8-K, or omit forms.")}
    hour = args.get("hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or hour < 0 or hour > 23:
        return {"kind": "invalid", "error": _fail("hour", "Hour must be an integer from 0 through 23.", "Pass hour as 0 through 23.")}
    deliver = canonical_deliver(args.get("deliver"), _platforms(deps))
    if deliver is None:
        return {"kind": "invalid", "error": _fail(
            "deliver",
            "The deliver target was not accepted.",
            "Pass local, origin, or a platform target. A schedule word is not a deliver target.",
        )}
    match = next((row for row in state["companies"] if _company_match(row, ticker or "", cik or "")), None)
    same_slot = state.get("hour") == hour and state.get("deliver") == deliver
    same_forms = match is not None and list(match.get("forms") or []) == forms
    if match is not None and same_slot and same_forms and job is not None:
        return {"kind": "unchanged", "cik": match.get("cik"), "ticker": match.get("ticker") or ""}
    if match is None and len(state["companies"]) >= MAX_COMPANIES:
        return {"kind": "invalid", "error": _fail("limit", f"The watch already has {MAX_COMPANIES} companies.", "Remove one company first.")}
    schedule_changes = job is not None and not same_slot
    plan = {
        "action": "add",
        "ticker": ticker or "",
        "cik": cik or (match.get("cik") if match else ""),
        "hour": hour,
        "deliver": deliver,
        "forms": forms,
        "remove_argv": remove_argv(deps.profile, str(job["id"])) if schedule_changes else [],
        "create_argv": create_argv(deps.profile, hour, deliver) if job is None or schedule_changes else [],
        "remove_script": False,
        "job_id": "" if job is None or schedule_changes else job.get("id") or "",
    }
    plan["message"] = _message(plan)
    return {"kind": "plan", "plan": plan}


def on_pre_tool_call(tool_name: str = "", args=None, **kwargs):
    """Approve add and remove. Recent, locate, and list stay silent."""
    if tool_name != "us_filings_watch":
        return None
    ids = _ids(kwargs)
    gates.clear_watch_stash(ids)
    payload = dict(args) if isinstance(args, dict) else {}
    if payload.get("action") not in {"add", "remove"}:
        return None
    host = gates.host_block_message()
    if host:
        return {"action": "block", "message": host}
    unattended = gates.unattended_write_block()
    if unattended:
        return {"action": "block", "message": unattended}
    try:
        deps = deps_for_hook()
        found = classify(deps, payload)
    except Exception:
        return {"action": "block", "message": "BLOCKED: the watch could not be prepared. Nothing was saved."}
    if found["kind"] == "block":
        return {"action": "block", "message": found["message"]}
    if found["kind"] != "plan":
        return None
    plan = found["plan"]
    question = plan["message"]
    if not gates.reason_fits(question):
        return {"action": "block", "message": "BLOCKED: the approval message does not fit in one message. Nothing was saved."}
    gates.remember_watch(plan, ids, question)
    return gates.approve_directive(plan, question)


def _row_public(row, cik: str) -> dict:
    index, primary = document_urls(cik, row.accession, row.primary)
    return {
        "accession": row.accession,
        "form": row.form,
        "filing_date": row.filing_date,
        "accepted": row.accepted,
        "index_url": index,
        "primary_url": primary,
    }


def _older_note(window: Window) -> str:
    if window.older_pages is None:
        return "Older submission pages were not listed, so they were not read."
    if window.older_pages == 1:
        return "Older submission pages were not read. The submissions file lists 1 older page."
    if window.older_pages:
        return (
            "Older submission pages were not read. "
            f"The submissions file lists {window.older_pages} older pages."
        )
    return ""


def _window_list_note(matched: int, shown: int) -> str:
    """Shortened recent list. One row uses the singular in each clause."""
    filings = "filing" if matched == 1 else "filings"
    listed = "is" if shown == 1 else "are"
    return f"The recent window has {matched} matching {filings}. {shown} {listed} listed."


def _bind(deps: Deps):
    previous = client._transport.get()
    if deps.transport is not None:
        client.set_transport(deps.transport)
    return previous


def _restore(token) -> None:
    client._transport.set(token)


def resolve_window(ticker: str, cik: str) -> tuple[Window, str, str]:
    """Return the window, the SEC ticker spelling, and the padded CIK."""
    sec_ticker = ""
    if ticker:
        ticker, found_cik = read_company_token(ticker, field="ticker")
        if found_cik:
            cik = found_cik
            ticker = ""
    elif isinstance(cik, str) and cik:
        found_ticker, cik = read_company_token(cik, field="cik")
        if found_ticker:
            ticker = found_ticker
            cik = ""
    if ticker:
        status, hits = resolve_ticker(client.ticker_index(), ticker)
        if status == "miss":
            raise client.SecError(
                "That ticker is not in the SEC ticker file.",
                "ticker_miss",
                "Pass the CIK, or check the ticker spelling. BRK.B is listed as BRK-B.",
            )
        if status == "ambiguous":
            choices = [{"cik": pad_cik(item[0]), "ticker": item[2], "title": item[1]} for item in hits]
            raise client.SecError(
                "That ticker matches more than one CIK, so none was chosen.",
                "ambiguous",
                "Pass one CIK from the choices. " + json.dumps(choices, ensure_ascii=True),
            )
        sec_ticker = hits[0][2]
        cik = pad_cik(hits[0][0])
    else:
        cik = pad_cik(prepare_token(cik) if isinstance(cik, str) else cik)
    window = parse_submissions(client.fetch_submissions(cik))
    if window.cik != cik:
        raise client.SecError(
            "The submissions CIK does not match the requested CIK, so no document URL was built.",
            "cik_mismatch",
            "Pass the CIK from the submissions file.",
        )
    return window, sec_ticker or (window.tickers[0] if window.tickers else ""), window.cik


def recent(deps: Deps, args: dict | None) -> str:
    args = args or {}
    if not isinstance(args, dict):
        return _fail("arguments", "Pass a JSON object.", "Pass ticker or cik.")
    extra = set(args) - {"ticker", "cik", "limit", "forms"}
    if extra or bool(args.get("ticker")) == bool(args.get("cik")):
        return _fail("company", "Pass a ticker or a CIK.", "Pass one of ticker or cik.")
    form_error, forms = _forms(args.get("forms"))
    if form_error:
        return _fail("forms", "Each form filter must be a short form name.", "Omit forms or pass names such as 10-K.")
    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 40:
        return _fail("limit", "Limit must be an integer from 1 to 40.", "Omit limit or pass 1 to 40. 0 is not treated as 1.")
    token = _bind(deps)
    try:
        with client.call_budget(client.TOOL_BUDGET):
            window, sec_ticker, cik = resolve_window(str(args.get("ticker") or ""), str(args.get("cik") or ""))
    except (client.SecError, WindowError) as exc:
        return _sec_fail(exc)
    finally:
        _restore(token)
    matched = [row for row in window.rows if form_allowed(row.form, forms)]
    shown = [_row_public(row, window.cik) for row in matched[:limit]]
    body: dict[str, Any] = {
        "ok": True,
        "cik": window.cik,
        "name": window.name,
        "ticker": sec_ticker,
        "window_count": window.count,
        "matched": len(matched),
        "shown": len(shown),
        "span": {"from": window.filing_from, "to": window.filing_to},
        "filings": shown,
    }
    note = _older_note(window)
    if note:
        body["older_pages"] = note
    if len(shown) < len(matched):
        body["next_step"] = _window_list_note(len(matched), len(shown))
    return dump(body)


def locate(deps: Deps, args: dict | None) -> str:
    args = args or {}
    if not isinstance(args, dict):
        return _fail("arguments", "Pass a JSON object.", "Pass accession and a ticker or CIK.")
    extra = set(args) - {"accession", "ticker", "cik"}
    accession = args.get("accession")
    if extra or not isinstance(accession, str):
        return _fail("accession", "Pass an accession number.", "Pass accession plus a ticker or CIK when the watch does not have it.")
    accession = prepare_accession(accession)
    ticker = args.get("ticker") or ""
    cik = args.get("cik") or ""
    if bool(ticker) == bool(cik):
        if ticker:
            return _fail("company", "Pass a ticker or a CIK.", "Pass one of ticker or cik.")
        try:
            return _locate_saved(deps, accession)
        except StorageUnavailable:
            return _fail(
                "storage",
                "The plugin data directory could not be opened.",
                "Pass a ticker or CIK. Nothing was written.",
            )
    try:
        if ticker:
            ticker, found_cik = read_company_token(str(ticker), field="ticker")
            if found_cik:
                ticker = ""
                cik = found_cik
        else:
            found_ticker, cik = read_company_token(str(cik), field="cik")
            if found_ticker:
                ticker = found_ticker
                cik = ""
    except WindowError as exc:
        return _sec_fail(exc)
    token = _bind(deps)
    try:
        with client.call_budget(client.TOOL_BUDGET):
            window, sec_ticker, cik10 = resolve_window(str(ticker), str(cik))
    except (client.SecError, WindowError) as exc:
        return _sec_fail(exc)
    finally:
        _restore(token)
    return _locate_in_window(window, sec_ticker, accession)


def _locate_saved(deps: Deps, accession: str) -> str:
    status, state, _root_path = _load(deps)
    if status == "corrupt":
        return _fail("state", "The watch state could not be read.", "Pass a ticker or CIK.")
    hits = []
    for company in state["companies"]:
        for row in company.get("rows") or []:
            if isinstance(row, dict) and row.get("accession") == accession:
                hits.append((company, row))
    if not hits:
        return _fail(
            "need_company",
            "Saved watches do not include that accession.",
            "Pass ticker or cik. An accession outside the saved rows is not called absent.",
        )
    if len({item[0].get("cik") for item in hits}) > 1:
        return _fail("ambiguous", "That accession is saved on more than one company.", "Pass the CIK.")
    company, row = hits[0]
    try:
        index, primary = document_urls(company["cik"], accession, row.get("primary") or "")
    except WindowError as exc:
        return _sec_fail(exc)
    return dump({
        "ok": True,
        "cik": company["cik"],
        "ticker": company.get("ticker") or "",
        "name": company.get("name") or "",
        "accession": accession,
        "form": row.get("form") or "",
        "filing_date": row.get("filing_date") or "",
        "accepted": row.get("accepted") or "",
        "index_url": index,
        "primary_url": primary,
        "source": "saved watch",
    })


def _locate_in_window(window: Window, ticker: str, accession: str) -> str:
    found = next((row for row in window.rows if row.accession == accession), None)
    note = _older_note(window)
    if found is None:
        body: dict[str, Any] = {
            "ok": False,
            "error_type": "not_in_window",
            "kind": "not_in_window",
            "error": (
                f"That accession is not in the recent window of {window.count} filings"
                + (f" from {window.filing_from} to {window.filing_to}." if window.filing_from else ".")
            ),
            "window_count": window.count,
            "span": {"from": window.filing_from, "to": window.filing_to},
            "next_step": "Pass a ticker or CIK only when you want another company. This does not mean the filing is absent.",
        }
        if note:
            body["older_pages"] = note
        return dump(body)
    try:
        index, primary = document_urls(window.cik, found.accession, found.primary)
    except WindowError as exc:
        return _sec_fail(exc)
    body = {
        "ok": True,
        "cik": window.cik,
        "ticker": ticker,
        "name": window.name,
        "accession": found.accession,
        "form": found.form,
        "filing_date": found.filing_date,
        "accepted": found.accepted,
        "index_url": index,
        "primary_url": primary,
        "in_recent_window": True,
    }
    if note:
        body["older_pages"] = note
    return dump(body)


def _list(deps: Deps) -> str:
    status, state, _root_path = _load(deps)
    if status == "corrupt":
        return _fail("state", "The watch state could not be read. Nothing was changed.", "Fix or remove the state file, then list again.")
    companies = []
    for row in state["companies"]:
        companies.append({
            "cik": row.get("cik"),
            "ticker": row.get("ticker") or "",
            "name": row.get("name") or "",
            "forms": row.get("forms") or [],
            "baselined": bool(row.get("baselined")),
            "seen_count": len(row.get("seen") or []),
        })
    return dump({
        "ok": True,
        "hour": state.get("hour"),
        "deliver": state.get("deliver"),
        "job_id": state.get("job_id"),
        "companies": companies,
    })


def _write_script(deps: Deps) -> None:
    write_bytes(_home(deps), f"scripts/{SCRIPT_NAME}", WATCH_SCRIPT.encode("utf-8"), create=True)


def _drop_script(deps: Deps) -> None:
    remove_file(_home(deps), f"scripts/{SCRIPT_NAME}")


def _store_rows(window: Window) -> list[dict]:
    return [
        {
            "accession": row.accession,
            "form": row.form,
            "filing_date": row.filing_date,
            "accepted": row.accepted,
            "primary": row.primary,
        }
        for row in window.rows
    ]


def _perform_add(deps: Deps, plan: dict) -> str:
    status, state, root = _load(deps)
    if status == "corrupt":
        return _fail("state", "The watch state could not be read. Nothing was saved.", "Fix the state file before adding a company.")
    token = _bind(deps)
    try:
        with client.call_budget(client.TOOL_BUDGET):
            window, sec_ticker, cik = resolve_window(plan.get("ticker") or "", plan.get("cik") or "")
    except (client.SecError, WindowError) as exc:
        return _sec_fail(exc)
    finally:
        _restore(token)
    existing = next((row for row in state["companies"] if row.get("cik") == cik), None)
    if existing is None and len(state["companies"]) >= MAX_COMPANIES:
        return _fail("limit", f"The watch already has {MAX_COMPANIES} companies.", "Remove one company first. Nothing was saved.")
    if existing and existing.get("baselined"):
        seen_list = list(existing.get("seen") or [])
        already = True
    else:
        _notify, seen, _flag = observe(False, set(), list(window.rows))
        seen_list = sorted(seen)
        already = False
    record = {
        "cik": cik,
        "ticker": sec_ticker or (existing.get("ticker") if existing else "") or plan.get("ticker") or "",
        "name": window.name,
        "forms": list(plan.get("forms") or []),
        "baselined": True,
        "seen": seen_list,
        "rows": _store_rows(window),
    }
    companies = [row for row in state["companies"] if row.get("cik") != cik]
    companies.append(record)
    companies.sort(key=lambda row: row["cik"])
    created_id = ""
    try:
        _write_script(deps)
        if plan.get("remove_argv"):
            done = deps.cron.run_argv(plan["remove_argv"])
            if done.returncode != 0:
                return _fail("cron", "hermes cron remove failed, so the watch was not changed.", "Read hermes cron list and retry.")
        if plan.get("create_argv"):
            job = _find_job(deps)
            schedule = f"0 {plan['hour']} * * *"
            if job and job.get("schedule") == schedule and job.get("deliver") == plan["deliver"]:
                created_id = job["id"]
            else:
                made = deps.cron.create_from_argv(
                    plan["create_argv"],
                    name=JOB_NAME,
                    schedule=schedule,
                    deliver=plan["deliver"],
                )
                created_id = made["id"]
        else:
            created_id = str(plan.get("job_id") or state.get("job_id") or "")
        state["companies"] = companies
        state["hour"] = plan["hour"]
        state["deliver"] = plan["deliver"]
        state["job_id"] = created_id or state.get("job_id")
        save_state(root, state)
    except CronUnread as exc:
        return _fail("cron", "hermes cron could not be read after approval. The company was not saved.", str(exc))
    except RuntimeError as exc:
        return _fail("cron", "hermes cron create failed. The company was not saved.", str(exc))
    except (WriteRefused, OSError):
        return _fail("write", "The watch file could not be saved.", "Check the plugin data directory and the scripts directory.")
    return dump({
        "ok": True,
        "action": "add",
        "cik": cik,
        "ticker": record["ticker"],
        "name": window.name,
        "hour": plan["hour"],
        "deliver": plan["deliver"],
        "forms": record["forms"],
        "baselined": True,
        "seen_count": len(record["seen"]),
        "job_id": state.get("job_id") or "",
        "next_step": (
            "The seen set was left as it was. A new accession still waits for the morning run."
            if already
            else "Existing filings were marked seen. No filing line was sent for them."
        ),
    })


def _perform_remove(deps: Deps, plan: dict) -> str:
    status, state, root = _load(deps)
    if status == "corrupt":
        return _fail("state", "The watch state could not be read. Nothing was removed.", "Fix the state file before removing a company.")
    others = [row for row in state["companies"] if row.get("cik") != plan["cik"]]
    if len(others) == len(state["companies"]):
        return _fail("missing", "That company is not in the watch.", "Nothing was removed.")
    try:
        if not others:
            if plan.get("remove_argv"):
                done = deps.cron.run_argv(plan["remove_argv"])
                if done.returncode != 0:
                    return _fail("cron", "hermes cron remove failed. The company was kept.", "Retry remove after hermes cron list.")
            _drop_script(deps)
            state["job_id"] = None
            state["hour"] = None
            state["deliver"] = None
        state["companies"] = others
        save_state(root, state)
    except (WriteRefused, OSError):
        return _fail("write", "The watch file could not be saved.", "The company may still be listed. Run list before retrying.")
    except RuntimeError as exc:
        return _fail("cron", str(exc), "Nothing else was changed.")
    return dump({
        "ok": True,
        "action": "remove",
        "cik": plan["cik"],
        "remaining": len(others),
        "job_removed": not others,
        "next_step": (
            "The morning job stays because other companies remain."
            if others
            else (
                f"Removed the morning job and scripts/{SCRIPT_NAME}. "
                "A later uninstall still needs hermes cron remove if a job is left behind."
            )
        ),
    })


def watch_tool(deps: Deps, args: dict | None, **kwargs) -> str:
    try:
        return _watch_tool(deps, args, **kwargs)
    except StorageUnavailable:
        return _fail(
            "storage",
            "The plugin data directory could not be opened. Nothing was saved.",
            "Retry when Hermes can open plugin data.",
        )


def _watch_tool(deps: Deps, args: dict | None, **kwargs) -> str:
    args = args or {}
    if not isinstance(args, dict) or args.get("action") not in {"add", "remove", "list"}:
        return _fail("action", "Pass action add, remove, or list.", "Use add, remove, or list.")
    if args["action"] == "list":
        if set(args) - {"action"}:
            return _fail("arguments", "List takes no other arguments.", "Pass action list alone.")
        return _list(deps)
    host = gates.host_block_message()
    if host:
        return _fail("blocked", host, "Run add from a normal session.")
    unattended = gates.unattended_write_block()
    if unattended:
        return _fail("blocked", unattended, "Add or remove from a session a person can answer.")
    found = classify(deps, args)
    if found["kind"] == "invalid":
        return found["error"]
    if found["kind"] == "block":
        return _fail("blocked", found["message"], "Nothing was saved.")
    if found["kind"] == "unchanged":
        return dump({"ok": True, "action": "add", "unchanged": True, "cik": found.get("cik"), "ticker": found.get("ticker") or ""})
    plan = found["plan"]
    denied = gates.take_watch(plan, _ids(kwargs), plan["message"])
    if denied:
        return _fail("blocked", denied, "Approve the watch card, then retry the same arguments.")
    if plan["action"] == "add":
        return _perform_add(deps, plan)
    return _perform_remove(deps, plan)


def morning_line(company: dict, row, index: str, primary: str) -> str:
    ticker = company.get("ticker") or ""
    name = company.get("name") or company["cik"]
    who = f"{name} ({ticker}, CIK {company['cik']})" if ticker else f"{name} (CIK {company['cik']})"
    lines = [who, f"form {row.form}", f"filing date {row.filing_date}"]
    if row.accepted:
        lines.append(f"accepted {row.accepted}")
    lines.append(f"accession {row.accession}")
    lines.append(f"index {index}")
    lines.append(f"document {primary}")
    return "\n".join(lines)


def watch_stdout(deps: Deps | None = None) -> tuple[int, str]:
    """Return (exit code, stdout). Empty stdout sends nothing. Failures leave that company unseen."""
    deps = deps or Deps()
    try:
        client.read_contact()
    except client.ContactUnset as exc:
        sys.stderr.write(exc.next_step + "\n")
        return 1, ""
    except client.SecError as exc:
        sys.stderr.write(str(exc) + "\n")
        if exc.next_step:
            sys.stderr.write(exc.next_step + "\n")
        return 1, ""
    try:
        status, state, root = _load(deps)
    except Exception:
        sys.stderr.write("The watch state could not be read. Seen filings were not updated.\n")
        return 1, ""
    if status == "corrupt":
        sys.stderr.write("The watch state could not be read. Seen filings were not updated.\n")
        return 1, ""
    if not state["companies"]:
        return 0, ""
    lines: list[str] = []
    failed = False
    updated = False
    token = _bind(deps)
    try:
        with client.call_budget(client.MORNING_BUDGET):
            for company in state["companies"]:
                try:
                    payload = client.fetch_submissions(company["cik"])
                    window = parse_submissions(payload)
                    if window.cik != company["cik"]:
                        raise WindowError("cik_mismatch", "The submissions CIK does not match the watched CIK.")
                    for row in window.rows:
                        document_urls(window.cik, row.accession, row.primary)
                except (client.SecError, WindowError):
                    failed = True
                    sys.stderr.write(
                        f"CIK {company['cik']} was not read. Seen filings for that company were not updated.\n"
                    )
                    continue
                notify, seen, _flag = observe(
                    bool(company.get("baselined")),
                    set(company.get("seen") or []),
                    list(window.rows),
                )
                company["baselined"] = True
                company["seen"] = sorted(seen)
                company["rows"] = _store_rows(window)
                company["name"] = window.name
                updated = True
                forms = list(company.get("forms") or [])
                for row in notify:
                    if not form_allowed(row.form, forms):
                        continue
                    index, primary = document_urls(window.cik, row.accession, row.primary)
                    lines.append(morning_line(company, row, index, primary))
    finally:
        _restore(token)
    if updated:
        try:
            save_state(root, state)
        except (WriteRefused, OSError):
            sys.stderr.write("The watch state could not be saved. No filing line was sent.\n")
            return 1, ""
    text = "\n\n".join(lines)
    if failed:
        return 1, text
    return 0, text
