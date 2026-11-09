"""Morning diff: the first read is silent, and the same accession is not sent again."""
import json

import pytest

import client
from client import TICKERS_URL, submissions_url
from support import (
    CIK,
    CONTACT,
    EIGHT,
    FRESH,
    TEN,
    apple_transport,
    bind_hook,
    eight_row,
    filing_row,
    fingerprint,
    make_deps,
    submissions_payload,
    ten_row,
    ticker_file,
    use_contact,
)


def _add(deps, monkeypatch, **extra):
    import watch

    bind_hook(monkeypatch, deps)
    args = {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}
    args.update(extra)
    decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    assert decision["action"] == "approve"
    body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
    assert body["ok"] is True, body
    return decision, body


def _state(deps):
    return json.loads((deps.data_dir / "state.json").read_text(encoding="utf-8"))


def _set_rows(transport, rows, **kwargs):
    transport.books[submissions_url(CIK)] = (200, submissions_payload(rows, **kwargs))


def test_first_add_baselines_and_the_message_matches_the_cron_argv(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    deps, cron = make_deps(tmp_path, transport)
    decision, body = _add(deps, monkeypatch)
    argv = watch.create_argv("default", 8, "local")
    assert watch.command_text(argv) in decision["message"]
    assert decision["message"].startswith("Approve us_filings_watch add ticker AAPL")
    assert "plugin-data/us-filings/state.json" in decision["message"]
    assert ("create", argv) in cron.calls
    assert body["baselined"] is True
    assert body["seen_count"] == 2
    assert "Existing filings were marked seen" in body["next_step"]
    state = _state(deps)
    assert state["companies"][0]["seen"] == sorted([EIGHT, TEN])
    script = (deps.home / "scripts" / "us-filings-watch.py").read_text(encoding="utf-8")
    assert script == watch.WATCH_SCRIPT
    assert "build_profile_secret_scope" in script
    assert "set_secret_scope" in script
    assert "get_hermes_home" in script
    line = (deps.home / "scripts" / "us-filings-watch.py").read_text(encoding="utf-8")
    assert "filed this morning" not in line
    assert "$" not in body["next_step"]


def test_same_set_is_not_sent_again_and_a_new_accession_is_sent_once(tmp_path, monkeypatch, capsys):
    import watch

    use_contact()
    transport = apple_transport()
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    code, text = watch.watch_stdout(deps)
    assert code == 0
    assert text == ""
    assert capsys.readouterr().out == ""
    _set_rows(transport, [eight_row(), ten_row()][::-1])
    code, text = watch.watch_stdout(deps)
    assert code == 0 and text == ""
    fresh = filing_row(FRESH, "8-K", "2026-11-10", "new8k.htm", "2026-11-10T13:00:00.000Z")
    _set_rows(transport, [fresh, eight_row(), ten_row()])
    code, text = watch.watch_stdout(deps)
    assert code == 0
    assert text.count("accession " + FRESH) == 1
    assert "Apple Inc." in text
    assert "AAPL" in text
    assert "CIK 0000320193" in text
    assert "form 8-K" in text
    assert "filing date 2026-11-10" in text
    assert "/data/320193/" in text
    assert "/1140361/" not in text
    assert "filed this morning" not in text
    assert "$" not in text
    code, text = watch.watch_stdout(deps)
    assert code == 0 and text == ""
    assert FRESH in _state(deps)["companies"][0]["seen"]


def test_primary_change_is_not_a_new_filing_and_a_drop_is_not_announced(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    changed = eight_row()
    changed["primaryDocument"] = "ef20085077_8k_amended.htm"
    _set_rows(transport, [changed, ten_row()])
    code, text = watch.watch_stdout(deps)
    assert code == 0 and text == ""
    stored = _state(deps)["companies"][0]["rows"]
    assert stored[0]["primary"] == "ef20085077_8k_amended.htm" or any(
        row["primary"] == "ef20085077_8k_amended.htm" for row in stored
    )
    _set_rows(transport, [ten_row()])
    code, text = watch.watch_stdout(deps)
    assert code == 0 and text == ""
    seen = set(_state(deps)["companies"][0]["seen"])
    assert EIGHT not in seen
    assert TEN in seen


def test_repeat_add_does_not_swallow_a_new_accession(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport([ten_row()])
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch, forms=["10-K"])
    assert _state(deps)["companies"][0]["seen"] == [TEN]
    _set_rows(transport, [eight_row(), ten_row()])
    decision, body = _add(deps, monkeypatch, forms=["8-K"])
    assert "left as it was" in body["next_step"]
    assert _state(deps)["companies"][0]["seen"] == [TEN]
    assert any(row["accession"] == EIGHT for row in _state(deps)["companies"][0]["rows"])
    code, text = watch.watch_stdout(deps)
    assert code == 0
    assert EIGHT in text
    assert "form 8-K" in text
    code, text = watch.watch_stdout(deps)
    assert code == 0 and text == ""


def test_form_filter_does_not_replay_an_accession_already_in_the_window(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport([ten_row()])
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch, forms=["10-K"])
    fresh = filing_row(FRESH, "8-K", "2026-11-10", "new8k.htm")
    _set_rows(transport, [fresh, eight_row(), ten_row()])
    # eight was not in the baseline. Both new rows arrive. Only 10-K would print, and neither new row is a 10-K.
    code, text = watch.watch_stdout(deps)
    assert code == 0 and text == ""
    assert FRESH in _state(deps)["companies"][0]["seen"]
    _add(deps, monkeypatch, forms=["8-K", "10-K"])
    code, text = watch.watch_stdout(deps)
    assert code == 0 and text == ""


def test_partial_company_does_not_advance_and_save_failure_sends_no_line(tmp_path, monkeypatch, capsys):
    import watch
    from store import save_state

    use_contact()
    other = "0001067983"
    transport = apple_transport([ten_row()])
    transport.books[submissions_url(other)] = (200, submissions_payload([eight_row()], cik=other, name="Berkshire", tickers=["BRK-B"]))
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    # Second company is stored by writing state after a direct record, using the same add path needs another ticker.
    state = _state(deps)
    state["companies"].append({
        "cik": other,
        "ticker": "BRK-B",
        "name": "Berkshire",
        "forms": [],
        "baselined": True,
        "seen": [EIGHT],
        "rows": [],
    })
    save_state(deps.data_dir, state)
    transport.books[submissions_url(other)] = (500, b"nope")
    fresh = filing_row(FRESH, "10-K", "2026-11-10", "new10k.htm")
    _set_rows(transport, [fresh, ten_row()])
    code, text = watch.watch_stdout(deps)
    assert code == 1
    assert FRESH in text
    err = capsys.readouterr().err
    assert other in err
    assert "not updated" in err
    saved = _state(deps)
    apple = next(row for row in saved["companies"] if row["cik"] == CIK)
    berk = next(row for row in saved["companies"] if row["cik"] == other)
    assert FRESH in apple["seen"]
    assert berk["seen"] == [EIGHT]

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(watch, "save_state", explode)
    newer = filing_row("0000320193-26-000100", "8-K", "2026-11-10", "another.htm")
    _set_rows(transport, [newer, fresh, ten_row()])
    transport.books[submissions_url(other)] = (200, submissions_payload([eight_row()], cik=other, name="Berkshire", tickers=["BRK-B"]))
    before = (deps.data_dir / "state.json").read_bytes()
    code, text = watch.watch_stdout(deps)
    assert code == 1
    assert text == ""
    assert (deps.data_dir / "state.json").read_bytes() == before
    assert "could not be saved" in capsys.readouterr().err


def test_morning_contact_unset_leaves_seen_and_prints_nothing(tmp_path, monkeypatch, capsys):
    import watch

    use_contact()
    transport = apple_transport()
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    before = (deps.data_dir / "state.json").read_bytes()
    from agent.secret_scope import set_secret_scope

    set_secret_scope({})
    monkeypatch.delenv("US_FILINGS_CONTACT", raising=False)
    code, text = watch.watch_stdout(deps)
    assert code == 1 and text == ""
    err = capsys.readouterr().err
    assert "US_FILINGS_CONTACT" in err
    assert CONTACT not in err
    assert (deps.data_dir / "state.json").read_bytes() == before
    assert transport.calls  # the add called SEC
    added = len(transport.calls)
    watch.watch_stdout(deps)
    assert len(transport.calls) == added


def test_incomplete_window_does_not_say_nothing_new(tmp_path, monkeypatch, capsys):
    import watch

    use_contact()
    transport = apple_transport()
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    payload = submissions_payload([eight_row(), ten_row()])
    payload["filings"]["recent"]["form"].append("10-Q")
    transport.books[submissions_url(CIK)] = (200, payload)
    before = _state(deps)["companies"][0]["seen"]
    code, text = watch.watch_stdout(deps)
    assert code == 1 and text == ""
    assert "not updated" in capsys.readouterr().err
    assert _state(deps)["companies"][0]["seen"] == before
    assert "no filing" not in capsys.readouterr().err.lower()


def test_count_notes_agree_in_number():
    import watch
    from filings import Window

    rows = (
        (12, 1, "The recent window has 12 matching filings. 1 is listed."),
        (3, 2, "The recent window has 3 matching filings. 2 are listed."),
        (1, 1, "The recent window has 1 matching filing. 1 is listed."),
        (0, 0, "The recent window has 0 matching filings. 0 are listed."),
    )
    for matched, shown, text in rows:
        assert watch._window_list_note(matched, shown) == text, (matched, shown)
    one = Window("0000320193", "Apple Inc.", (), (), 1, "", "")
    two = Window("0000320193", "Apple Inc.", (), (), 2, "", "")
    zero = Window("0000320193", "Apple Inc.", (), (), 0, "", "")
    missing = Window("0000320193", "Apple Inc.", (), (), None, "", "")
    assert watch._older_note(one) == (
        "Older submission pages were not read. The submissions file lists 1 older page."
    )
    assert watch._older_note(two) == (
        "Older submission pages were not read. The submissions file lists 2 older pages."
    )
    assert watch._older_note(zero) == ""
    assert watch._older_note(missing) == "Older submission pages were not listed, so they were not read."


def test_recent_and_locate_use_the_subject_cik(tmp_path):
    import watch

    use_contact()
    rows = [eight_row(), ten_row(), filing_row(FRESH, "4", "2026-11-01", "xslF25X02/primary_doc.xml")]
    transport = apple_transport(rows, files=[{"name": "older.json"}])
    deps, _cron = make_deps(tmp_path, transport)
    body = json.loads(watch.recent(deps, {"ticker": "AAPL", "limit": 2}))
    assert body["ok"] is True
    assert body["window_count"] == 3
    assert body["shown"] == 2
    assert body["matched"] == 3
    assert body["next_step"] == "The recent window has 3 matching filings. 2 are listed."
    assert body["older_pages"] == (
        "Older submission pages were not read. The submissions file lists 1 older page."
    )
    one = json.loads(watch.recent(deps, {"ticker": "AAPL", "limit": 1}))
    assert one["shown"] == 1
    assert one["matched"] == 3
    assert one["next_step"] == "The recent window has 3 matching filings. 1 is listed."
    assert body["filings"][0]["primary_url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    assert "/1140361/" not in json.dumps(body)
    assert body["older_pages"]
    filtered = json.loads(watch.recent(deps, {"cik": CIK, "forms": ["10-K"], "limit": 20}))
    assert filtered["window_count"] == 3
    assert filtered["matched"] == 1
    assert filtered["filings"][0]["form"] == "10-K"
    located = json.loads(watch.locate(deps, {"accession": EIGHT, "ticker": "AAPL"}))
    assert located["ok"] is True
    assert located["primary_url"].endswith("/ef20085077_8k.htm")
    assert "/data/320193/" in located["index_url"]
    missing = json.loads(watch.locate(deps, {"accession": "0000320193-26-999999", "cik": CIK}))
    assert missing["ok"] is False
    assert missing["error_type"] == "not_in_window"
    assert "absent" in missing["next_step"]
    assert "does not exist" not in missing["error"].lower()
    assert missing["window_count"] == 3


def test_spaced_ticker_accession_and_cik_still_resolve(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    deps, _cron = make_deps(tmp_path, transport)
    body = json.loads(watch.recent(deps, {"ticker": " AAPL ;", "forms": [" 10-k;"], "limit": 5}))
    assert body["ok"] is True, body
    assert body["cik"] == CIK
    assert [row["form"] for row in body["filings"]] == ["10-K"]
    located = json.loads(watch.locate(deps, {"accession": f" {EIGHT}; ", "ticker": "ＡＡＰＬ"}))
    assert located["ok"] is True, located
    assert located["accession"] == EIGHT
    by_cik = json.loads(watch.recent(deps, {"cik": " 320193;", "limit": 1}))
    assert by_cik["ok"] is True, by_cik
    assert by_cik["cik"] == CIK
    decision, _added = _add(deps, monkeypatch, ticker=" AAPL ;")
    assert "ticker AAPL hour" in decision["message"]
    for raw in (
        "AAPL # note",
        "https://www.sec.gov/Archives/edgar/data/320193/",
        "https://data.sec.gov/submissions/CIK0000320193.json",
    ):
        good = json.loads(watch.recent(deps, {"ticker": raw, "limit": 1}))
        assert good["ok"] is True, (raw, good)
        assert good["cik"] == CIK
        assert good["name"]
    located = json.loads(watch.locate(deps, {"accession": f"{EIGHT} # note", "ticker": "AAPL # note"}))
    assert located["ok"] is True, located
    assert located["accession"] == EIGHT
    for raw in ("José", "AAPL/B", 'AAPL"', "AAPL?", "アップル", "https://evil.example/data/320193"):
        bad = json.loads(watch.recent(deps, {"ticker": raw}))
        assert bad["ok"] is False, raw
        assert bad["error_type"] == "ticker", raw
        assert "Apple" not in bad.get("error", "")


def test_limit_edges_and_ambiguous_ticker(tmp_path):
    import watch

    use_contact()
    transport = apple_transport()
    deps, _cron = make_deps(tmp_path, transport)
    for limit in (0, 41, True, -1):
        body = json.loads(watch.recent(deps, {"ticker": "AAPL", "limit": limit}))
        assert body["ok"] is False
        assert body["error_type"] == "limit"
    transport.books[TICKERS_URL] = (200, {
        "0": {"cik_str": 1, "ticker": "DUPL", "title": "One"},
        "1": {"cik_str": 2, "ticker": "DUPL", "title": "Two"},
    })
    client.reset_runtime()
    client.set_transport(transport)
    ambiguous = json.loads(watch.recent(deps, {"ticker": "DUPL"}))
    assert ambiguous["ok"] is False
    assert ambiguous["error_type"] == "ambiguous"
    assert "0000000001" in ambiguous["next_step"]
    assert "0000000002" in ambiguous["next_step"]


def test_brk_b_resolves_to_the_hyphen_ticker(tmp_path):
    import watch

    use_contact()
    transport = apple_transport()
    berk_cik = "0001067983"
    transport.books[submissions_url(berk_cik)] = (
        200,
        submissions_payload([eight_row()], cik=berk_cik, name="BERKSHIRE HATHAWAY INC", tickers=["BRK-B"]),
    )
    deps, _cron = make_deps(tmp_path, transport)
    body = json.loads(watch.recent(deps, {"ticker": "BRK.B", "limit": 5}))
    assert body["ok"] is True, body
    assert body["cik"] == berk_cik
    assert body["ticker"] == "BRK-B"
    assert "/data/1067983/" in body["filings"][0]["primary_url"]


def test_saved_locate_and_two_companies(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    deps, _cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    found = json.loads(watch.locate(deps, {"accession": EIGHT}))
    assert found["ok"] is True
    assert found["source"] == "saved watch"
    assert "/data/320193/" in found["primary_url"]
    absent = json.loads(watch.locate(deps, {"accession": "0000320193-26-999999"}))
    assert absent["error_type"] == "need_company"
    assert "absent" in absent["next_step"]
    from store import save_state

    state = _state(deps)
    state["companies"].append({
        "cik": "0001067983",
        "ticker": "BRK-B",
        "name": "Berkshire",
        "forms": [],
        "baselined": True,
        "seen": [EIGHT],
        "rows": [{
            "accession": EIGHT,
            "form": "8-K",
            "filing_date": "2026-11-09",
            "accepted": "",
            "primary": "ef20085077_8k.htm",
        }],
    })
    save_state(deps.data_dir, state)
    both = json.loads(watch.locate(deps, {"accession": EIGHT}))
    assert both["error_type"] == "ambiguous"
    assert "primary_url" not in both


def test_second_company_does_not_create_another_job_and_last_remove_matches_the_message(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    berk_cik = "0001067983"
    transport.books[submissions_url(berk_cik)] = (
        200,
        submissions_payload([ten_row()], cik=berk_cik, name="BERKSHIRE HATHAWAY INC", tickers=["BRK-B"]),
    )
    deps, cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    bind_hook(monkeypatch, deps)
    args = {"action": "add", "ticker": "BRK.B", "hour": 8, "deliver": "local"}
    decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    assert decision["action"] == "approve"
    assert "No cron command runs" in decision["message"]
    assert "cron create" not in decision["message"]
    body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
    assert body["ok"] is True
    assert [call for call in cron.calls if call[0] == "create"]
    creates = [call for call in cron.calls if call[0] == "create"]
    assert len(creates) == 1
    remove_berk = {"action": "remove", "ticker": "BRK.B"}
    decision = watch.on_pre_tool_call("us_filings_watch", remove_berk, session_id="s2", task_id="t2")
    assert "morning job stays" in decision["message"]
    assert "cron remove" not in decision["message"]
    body = json.loads(watch.watch_tool(deps, remove_berk, session_id="s2", task_id="t2"))
    assert body["job_removed"] is False
    assert "stays" in body["next_step"]
    assert _state(deps)["job_id"]
    remove_apple = {"action": "remove", "cik": CIK}
    decision = watch.on_pre_tool_call("us_filings_watch", remove_apple, session_id="s3", task_id="t3")
    assert watch.command_text(watch.remove_argv("default", _state(deps)["job_id"])) in decision["message"]
    assert "us-filings-watch.py" in decision["message"]
    body = json.loads(watch.watch_tool(deps, remove_apple, session_id="s3", task_id="t3"))
    assert body["ok"] is True
    assert body["job_removed"] is True
    assert not (deps.home / "scripts" / "us-filings-watch.py").exists()
    assert _state(deps)["companies"] == []
    assert _state(deps)["job_id"] is None


def test_profile_flag_and_hour_change_use_the_approved_argv(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    deps, cron = make_deps(tmp_path, transport, profile="desk")
    bind_hook(monkeypatch, deps)
    args = {"action": "add", "ticker": "AAPL", "hour": 7, "deliver": "origin"}
    decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    argv = watch.create_argv("desk", 7, "origin")
    assert argv[:3] == ["-p", "desk", "cron"]
    assert watch.command_text(argv) in decision["message"]
    assert len(decision["message"]) <= 300
    body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
    assert body["ok"] is True, body
    assert ("create", argv) in cron.calls
    change = {"action": "add", "ticker": "AAPL", "hour": 9, "deliver": "origin"}
    decision = watch.on_pre_tool_call("us_filings_watch", change, session_id="s", task_id="t")
    assert watch.command_text(watch.remove_argv("desk", "job1")) in decision["message"]
    assert watch.command_text(watch.create_argv("desk", 9, "origin")) in decision["message"]
    body = json.loads(watch.watch_tool(deps, change, session_id="s", task_id="t"))
    assert body["ok"] is True, body
    assert body["hour"] == 9


def test_unchanged_add_does_not_write_and_handler_without_stash_does_not_write(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    deps, cron = make_deps(tmp_path, transport)
    _add(deps, monkeypatch)
    before = fingerprint(deps.data_dir) + fingerprint(deps.home)
    calls = len(cron.calls)
    args = {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}
    assert watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t") is None
    body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
    assert body["unchanged"] is True
    assert fingerprint(deps.data_dir) + fingerprint(deps.home) == before
    assert len(cron.calls) >= calls  # list may run, create does not
    assert not any(call[0] == "create" and call[1] != cron.calls[0][1] for call in cron.calls[calls:])
    fresh = {"action": "add", "cik": "0001067983", "hour": 8, "deliver": "local"}
    denied = json.loads(watch.watch_tool(deps, fresh, session_id="new", task_id="new"))
    assert denied["ok"] is False
    assert denied["error_type"] == "blocked"
    assert len(_state(deps)["companies"]) == 1


def test_corrupt_state_is_not_replaced(tmp_path, monkeypatch):
    import watch

    use_contact()
    deps, _cron = make_deps(tmp_path, apple_transport())
    path = deps.data_dir / "state.json"
    path.write_bytes(b"{")
    before = path.read_bytes()
    bind_hook(monkeypatch, deps)
    args = {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}
    decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    assert decision["action"] == "block"
    assert path.read_bytes() == before
    body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
    assert body["ok"] is False
    assert path.read_bytes() == before


def test_list_failure_blocks_and_bad_deliver_is_refused(tmp_path, monkeypatch):
    import watch

    use_contact()
    transport = apple_transport()
    deps, cron = make_deps(tmp_path, transport)
    cron.fail = "list"
    bind_hook(monkeypatch, deps)
    args = {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}
    decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    assert decision["action"] == "block"
    assert "cron list" in decision["message"]
    assert not (deps.data_dir / "state.json").exists()
    cron.fail = ""
    for deliver in ("cli", "cron", "api_server", "local ", "nope", "telegram:has space", True):
        body = json.loads(watch.watch_tool(deps, {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": deliver}, session_id="s", task_id="t"))
        assert body["ok"] is False
    import gates

    monkeypatch.setattr(gates, "load_platforms", lambda: None)
    bare = tmp_path / "bare"
    none_deps, _none_cron = make_deps(bare, transport, platforms=None)
    body = json.loads(watch.watch_tool(none_deps, {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}, session_id="z", task_id="z"))
    assert body["error_type"] == "deliver", body
    for hour in (True, 24, -1, "8"):
        body = json.loads(watch.watch_tool(deps, {"action": "add", "ticker": "AAPL", "hour": hour, "deliver": "local"}, session_id="s", task_id="t"))
        assert body["error_type"] == "hour"


def test_unattended_and_host_write_nothing(tmp_path, monkeypatch):
    import watch

    use_contact()
    deps, cron = make_deps(tmp_path, apple_transport())
    bind_hook(monkeypatch, deps)
    args = {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}
    for env, value in (
        ("HERMES_CRON_SESSION", "1"),
        ("HERMES_SINGLE_QUERY_SESSION", "true"),
        ("HERMES_SESSION_PLATFORM", "webhook"),
        ("HERMES_SESSION_PLATFORM", "api_server"),
        ("HERMES_SESSION_PLATFORM", "msgraph_webhook"),
        ("HERMES_PLUGIN_HOST_PROCESS", "1"),
    ):
        monkeypatch.setenv(env, value)
        decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
        assert decision["action"] == "block", env
        body = json.loads(watch.watch_tool(deps, args, session_id="s", task_id="t"))
        assert body["ok"] is False
        assert not (deps.data_dir / "state.json").exists()
        assert not any(call[0] == "create" for call in cron.calls)
        monkeypatch.delenv(env, raising=False)


def test_remove_rule_key_changes_and_a_long_message_blocks(tmp_path, monkeypatch):
    import gates
    import watch

    use_contact()
    deps, _cron = make_deps(tmp_path, apple_transport())
    _add(deps, monkeypatch)
    args = {"action": "remove", "ticker": "AAPL"}
    first = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    second = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    assert first["rule_key"] != second["rule_key"]
    assert first["rule_key"].startswith("us_filings_watch:")
    add_args = {"action": "add", "ticker": "AAPL", "hour": 9, "deliver": "local"}
    one = watch.on_pre_tool_call("us_filings_watch", add_args, session_id="a", task_id="a")
    two = watch.on_pre_tool_call("us_filings_watch", add_args, session_id="a", task_id="a")
    assert one["rule_key"] == two["rule_key"]
    monkeypatch.setattr(gates, "REASON_BUDGET", 40)
    blocked = watch.on_pre_tool_call("us_filings_watch", add_args, session_id="b", task_id="b")
    assert blocked["action"] == "block"
    assert "does not fit" in blocked["message"]


def test_read_tools_do_not_write(tmp_path, monkeypatch):
    import watch

    use_contact()
    deps, _cron = make_deps(tmp_path, apple_transport())
    _add(deps, monkeypatch)
    before = fingerprint(deps.data_dir) + fingerprint(deps.home)
    watch.recent(deps, {"ticker": "AAPL"})
    watch.locate(deps, {"accession": EIGHT, "ticker": "AAPL"})
    watch.locate(deps, {"accession": EIGHT})
    watch.watch_tool(deps, {"action": "list"})
    assert fingerprint(deps.data_dir) + fingerprint(deps.home) == before


def test_missing_plugin_data_dir_does_not_pick_another_path(tmp_path, monkeypatch):
    import gates
    import watch

    use_contact()
    deps, _cron = make_deps(tmp_path, apple_transport())
    deps.data_dir = None

    def unavailable():
        raise RuntimeError("plugin_data_dir is not available")

    monkeypatch.setattr(gates, "plugin_data_dir", unavailable)
    body = json.loads(watch.watch_tool(deps, {"action": "list"}))
    assert body["error_type"] == "storage"
    assert not (tmp_path / "state.json").exists()
    body = json.loads(watch.locate(deps, {"accession": EIGHT}))
    assert body["error_type"] == "storage"


def test_cli_recent_matches_the_readme_when_contact_is_unset(capsys):
    import argparse
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("us_filings_cli_mod", root / "cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handle = module.handle
    code = handle(argparse.Namespace(command="call", tool="us_filings_recent", json='{"ticker":"AAPL"}'))
    assert code == 0
    text = capsys.readouterr().out
    body = json.loads(text)
    assert body["ok"] is False
    assert body["kind"] == "contact_unset"
    assert body["error_type"] == "contact_unset"
    assert "next_step" in body
    assert CONTACT not in text
    assert handle(argparse.Namespace(command="call", tool="nope", json="{}")) == 2
    assert handle(argparse.Namespace(command="call", tool="us_filings_recent", json="[")) == 2


def test_cron_mark_blocks_when_it_is_set_or_unreadable(tmp_path, monkeypatch):
    import sys
    import types

    import watch

    use_contact()
    deps, _cron = make_deps(tmp_path, apple_transport())
    bind_hook(monkeypatch, deps)
    args = {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}
    module = types.ModuleType("cron.execution_identity")
    module.current_cron_execution = lambda: {"job": "us-filings"}
    monkeypatch.setitem(sys.modules, "cron.execution_identity", module)
    decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    assert decision["action"] == "block"
    assert "cron" in decision["message"]
    module.current_cron_execution = lambda: (_ for _ in ()).throw(RuntimeError("unreadable"))
    decision = watch.on_pre_tool_call("us_filings_watch", args, session_id="s", task_id="t")
    assert decision["action"] == "block"
    assert "could not be read" in decision["message"]
    assert not (deps.data_dir / "state.json").exists()


def test_cap_and_unknown_action(tmp_path, monkeypatch):
    import watch
    from store import save_state

    use_contact()
    deps, _cron = make_deps(tmp_path, apple_transport())
    companies = []
    for number in range(20):
        companies.append({
            "cik": str(number + 1).zfill(10),
            "ticker": "",
            "name": "Co",
            "forms": [],
            "baselined": True,
            "seen": [],
            "rows": [],
        })
    save_state(deps.data_dir, {"version": 1, "hour": 8, "deliver": "local", "job_id": "job1", "companies": companies})
    deps.cron.jobs.append({"id": "job1", "name": "us-filings", "schedule": "0 8 * * *", "deliver": "local"})
    bind_hook(monkeypatch, deps)
    body = json.loads(watch.watch_tool(deps, {"action": "add", "ticker": "AAPL", "hour": 8, "deliver": "local"}, session_id="s", task_id="t"))
    assert body["error_type"] == "limit"
    body = json.loads(watch.watch_tool(deps, {"action": "run"}))
    assert body["error_type"] == "action"
    listed = json.loads(watch.watch_tool(deps, {"action": "list"}))
    assert listed["ok"] is True
    assert len(listed["companies"]) == 20
    assert "seen" not in listed["companies"][0]
