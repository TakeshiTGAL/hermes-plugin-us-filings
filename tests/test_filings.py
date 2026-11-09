"""Archive URLs use the subject CIK. The diff key is the accession number."""
import json

import pytest

from filings import (
    WindowError,
    document_urls,
    index_tickers,
    observe,
    pad_cik,
    parse_submissions,
    prepare_accession,
    read_company_token,
    resolve_ticker,
)
from filings import Row
from support import EIGHT, FRESH, TEN, eight_row, filing_row, submissions_payload, ten_row, ticker_file


def _rows():
    window = parse_submissions(submissions_payload([eight_row(), ten_row()]))
    return list(window.rows)


def test_agent_prefix_is_not_the_archive_directory():
    index, primary = document_urls("0000320193", EIGHT, "ef20085077_8k.htm")
    assert index == (
        "https://www.sec.gov/Archives/edgar/data/320193/000114036126042798/"
        "0001140361-26-042798-index.html"
    )
    assert primary == (
        "https://www.sec.gov/Archives/edgar/data/320193/000114036126042798/ef20085077_8k.htm"
    )
    assert "/1140361/" not in index
    assert "/1140361/" not in primary


def test_self_filed_10k_uses_the_same_company_directory():
    index, primary = document_urls(320193, TEN, "aapl-20260926.htm")
    assert "/data/320193/000032019326000026/" in index
    assert primary.endswith("/aapl-20260926.htm")


def test_slashed_primary_is_kept_and_dotdot_is_refused():
    _index, primary = document_urls("0000320193", TEN, "xslF25X02/primary_doc.xml")
    assert primary.endswith("/xslF25X02/primary_doc.xml")
    for bad in ("../ef20085077_8k.htm", "/tmp/x", "ef20085077_8k.htm\\x", "https://evil.test/a", "a/../../b", ""):
        with pytest.raises(WindowError):
            document_urls("0000320193", TEN, bad)


def test_bad_cik_and_accession_do_not_build_a_url():
    with pytest.raises(WindowError):
        pad_cik(True)
    with pytest.raises(WindowError):
        pad_cik(0)
    with pytest.raises(WindowError):
        pad_cik("12a")
    with pytest.raises(WindowError):
        document_urls("0000320193", "1140361-26-042798", "ef20085077_8k.htm")
    assert pad_cik("320193") == "0000320193"
    assert pad_cik(320193) == "0000320193"


def test_mismatched_columns_are_not_an_empty_window():
    payload = submissions_payload([eight_row()])
    payload["filings"]["recent"]["form"].append("10-Q")
    with pytest.raises(WindowError) as caught:
        parse_submissions(payload)
    assert caught.value.kind == "incomplete"
    assert "0" not in str(caught.value)
    assert "none" not in str(caught.value).lower()
    assert "no filing" not in str(caught.value).lower()


def test_bad_accession_does_not_become_zero_filings():
    payload = submissions_payload([eight_row()])
    payload["filings"]["recent"]["accessionNumber"][0] = "not-an-accession"
    with pytest.raises(WindowError) as caught:
        parse_submissions(payload)
    assert caught.value.kind == "incomplete"


def test_real_empty_recent_is_zero_and_missing_files_are_unknown():
    payload = submissions_payload([], include_files=False)
    window = parse_submissions(payload)
    assert window.count == 0
    assert window.older_pages is None
    payload["filings"]["files"] = [{"name": "CIK0000320193-submissions-001.json"}]
    assert parse_submissions(payload).older_pages == 1


def test_duplicate_accession_keeps_the_first_row():
    first = eight_row()
    second = dict(first)
    second["form"] = "10-Q"
    window = parse_submissions(submissions_payload([first, second]))
    assert window.count == 1
    assert window.rows[0].form == "8-K"


def test_first_read_is_silent_and_the_same_accession_is_not_a_new_filing():
    rows = _rows()
    notify, seen, baselined = observe(False, set(), rows)
    assert notify == []
    assert baselined is True
    assert seen == {EIGHT, TEN}
    reordered = list(reversed(rows))
    notify, seen2, _flag = observe(True, set(seen), reordered)
    assert notify == []
    assert seen2 == seen
    changed = [Row(row.accession, row.form, row.filing_date, row.accepted, "other.htm") for row in rows]
    notify, _seen3, _flag = observe(True, set(seen), changed)
    assert notify == []


def test_a_new_accession_is_returned_once_and_a_dropped_one_leaves():
    rows = _rows()
    _notify, seen, _flag = observe(False, set(), rows)
    fresh = rows + [Row(FRESH, "8-K", "2026-11-10", "2026-11-10T12:00:00.000Z", "new8k.htm")]
    notify, seen2, _flag = observe(True, set(seen), fresh)
    assert [row.accession for row in notify] == [FRESH]
    notify, seen3, _flag = observe(True, set(seen2), fresh)
    assert notify == []
    assert seen3 == {EIGHT, TEN, FRESH}
    kept = [row for row in fresh if row.accession != EIGHT]
    notify, seen4, _flag = observe(True, set(seen3), kept)
    assert notify == []
    assert EIGHT not in seen4
    assert FRESH in seen4


def test_ticker_file_matches_apple_and_retries_brk_b_once():
    index = index_tickers(ticker_file())
    kind, hits = resolve_ticker(index, "aapl")
    assert kind == "one"
    assert hits[0][0] == 320193
    assert hits[0][2] == "AAPL"
    kind, hits = resolve_ticker(index, "BRK.B")
    assert kind == "one"
    assert hits[0][2] == "BRK-B"
    assert hits[0][0] == 1067983


def test_ordinary_ticker_typing_resolves_and_odd_tokens_do_not():
    index = index_tickers(ticker_file())
    accepted = (" AAPL ", "AAPL;", " AAPL ; ", "ＡＡＰＬ", "AAPL；", "aapl", "AAPL # note", "AAPL＃注")
    for raw in accepted:
        kind, hits = resolve_ticker(index, raw)
        assert kind == "one", raw
        assert hits[0][2] == "AAPL"
    # Each row is one wrapper around a company id. The reader is rebuilt from this table.
    company_rows = (
        ("comment", "AAPL # note", "ticker", "AAPL"),
        ("fullwidth comment", "ＡＡＰＬ＃注", "ticker", "AAPL"),
        ("archives url", "https://www.sec.gov/Archives/edgar/data/320193/", "cik", "320193"),
        ("submissions url", "https://data.sec.gov/submissions/CIK0000320193.json", "cik", "0000320193"),
        ("query cik", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193", "cik", "0000320193"),
        ("query ticker", "https://www.sec.gov/cgi-bin/browse-edgar?CIK=AAPL", "ticker", "AAPL"),
        ("spaced", " AAPL ;", "ticker", "AAPL"),
    )
    for kind, raw, side, value in company_rows:
        ticker, cik = read_company_token(raw, field="ticker")
        got = ticker if side == "ticker" else cik
        assert got == value, kind
        assert (ticker == "") or (cik == ""), kind
    refused = (
        "José",
        "AAPL/B",
        'AAPL"',
        "AAPL?",
        "アップル",
        "https://evil.example/data/320193",
        "https://www.sec.gov.evil.example/data/320193",
    )
    for raw in refused:
        with pytest.raises(WindowError) as caught:
            read_company_token(raw, field="ticker")
        assert caught.value.kind == "ticker", raw
    accession = prepare_accession(
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/0000320193-26-000026-index.html # note"
    )
    assert accession == "0000320193-26-000026"
    assert prepare_accession(f" {EIGHT} # note ") == EIGHT
    with pytest.raises(WindowError) as caught:
        prepare_accession("https://evil.example/0000320193-26-000026-index.html")
    assert caught.value.kind == "accession"


def test_two_ciks_for_one_ticker_are_ambiguous():
    payload = ticker_file()
    payload["2"] = {"cik_str": 999, "ticker": "AAPL", "title": "Other Apple"}
    index = index_tickers(payload)
    kind, hits = resolve_ticker(index, "AAPL")
    assert kind == "ambiguous"
    assert {item[0] for item in hits} == {320193, 999}
    kind, hits = resolve_ticker(index, "ZZZZ")
    assert kind == "miss"
    assert hits == []


def test_bool_cik_in_the_ticker_file_is_skipped():
    payload = {"0": {"cik_str": True, "ticker": "AAPL", "title": "Nope"}}
    with pytest.raises(WindowError):
        index_tickers(payload)


def test_submissions_json_round_trip_keeps_company_cik():
    window = parse_submissions(json.loads(json.dumps(submissions_payload([filing_row(FRESH, "4", "2026-11-01", "form4.xml")]))))
    assert window.cik == "0000320193"
    assert window.rows[0].form == "4"
