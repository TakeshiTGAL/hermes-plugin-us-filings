"""Parse SEC submissions and build archive URLs.

The archive directory is the subject company CIK from the submissions file.
The accession prefix is the filer CIK and can belong to an agent.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{4,10}\Z")
TICKER_RE = re.compile(r"^[A-Za-z0-9._-]{1,12}\Z")
FORM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./-]{0,19}\Z")
_TRAIL = re.compile(r"[;\s]+\Z")
_COMMENT = re.compile(r"[ \t]*#.*\Z")
_URL = re.compile(r"^https?://\S+\Z", re.IGNORECASE)
_SEC_URL = re.compile(
    r"^https?://(?:www\.sec\.gov|data\.sec\.gov)(?::\d+)?(?:/|\?|#|\Z)",
    re.IGNORECASE,
)
_ACC_IN_TEXT = re.compile(r"\d{10}-\d{2}-\d{4,10}")
_REQUIRED = ("accessionNumber", "form", "filingDate", "primaryDocument")
_TICKER_GRAMMAR = "A ticker is 1 to 12 letters, digits, dots, or hyphens."
_CIK_GRAMMAR = "CIK must be digits."


class WindowError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Row:
    accession: str
    form: str
    filing_date: str
    accepted: str
    primary: str


@dataclass(frozen=True)
class Window:
    cik: str
    name: str
    tickers: tuple[str, ...]
    rows: tuple[Row, ...]
    older_pages: int | None
    filing_from: str
    filing_to: str

    @property
    def count(self) -> int:
        return len(self.rows)


def pad_cik(value: object) -> str:
    """Return a 10-digit CIK. Reject zero, booleans, and non-digits."""
    if isinstance(value, bool):
        raise WindowError("cik", "CIK must be digits.")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise WindowError("cik", "CIK must be digits.")
    if not text.isdigit() or len(text) > 10 or int(text) == 0:
        raise WindowError("cik", "CIK must be 1 to 10 digits and cannot be 0.")
    return text.zfill(10)


def _column(recent: dict, key: str, count: int | None) -> list:
    value = recent.get(key)
    if not isinstance(value, list):
        raise WindowError("incomplete", "A submissions column is missing, so this window was not read.")
    if count is not None and len(value) != count:
        raise WindowError("incomplete", "The submissions columns differ in length, so this window was not read.")
    return value


def _primary_ok(primary: str) -> bool:
    if not isinstance(primary, str) or not primary or len(primary) > 240:
        return False
    if primary.startswith(("/", "\\")) or "\\" in primary or "://" in primary:
        return False
    if any(ord(ch) < 32 for ch in primary):
        return False
    parts = primary.split("/")
    return all(part not in ("", ".", "..") and ".." not in part for part in parts)


def parse_submissions(payload: object) -> Window:
    """Read filings.recent. Mismatched columns raise WindowError and are not an empty list."""
    if not isinstance(payload, dict):
        raise WindowError("incomplete", "The submissions body was not an object, so this window was not read.")
    try:
        cik = pad_cik(payload.get("cik"))
    except WindowError as exc:
        raise WindowError("incomplete", "The submissions CIK could not be read, so this window was not read.") from exc
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WindowError("incomplete", "The submissions company name was missing, so this window was not read.")
    raw_tickers = payload.get("tickers") or []
    tickers: list[str] = []
    if isinstance(raw_tickers, list):
        for item in raw_tickers:
            if isinstance(item, str) and item.strip():
                tickers.append(item.strip())
    filings = payload.get("filings")
    if not isinstance(filings, dict):
        raise WindowError("incomplete", "The submissions filings object was missing, so this window was not read.")
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        raise WindowError("incomplete", "The recent filings object was missing, so this window was not read.")
    first = _column(recent, _REQUIRED[0], None)
    count = len(first)
    columns = {key: _column(recent, key, count) for key in _REQUIRED}
    accepted_raw = recent.get("acceptanceDateTime")
    if accepted_raw is None:
        accepted = [""] * count
    else:
        accepted = [str(item) if isinstance(item, str) else "" for item in _column(recent, "acceptanceDateTime", count)]
    rows: list[Row] = []
    seen: set[str] = set()
    for index in range(count):
        accession = columns["accessionNumber"][index]
        form = columns["form"][index]
        filing_date = columns["filingDate"][index]
        primary = columns["primaryDocument"][index]
        if not isinstance(accession, str) or not ACCESSION_RE.fullmatch(accession):
            raise WindowError("incomplete", "An accession number was not readable, so this window was not read.")
        if not isinstance(form, str) or not form.strip():
            raise WindowError("incomplete", "A form name was missing, so this window was not read.")
        if not isinstance(filing_date, str) or not filing_date:
            raise WindowError("incomplete", "A filing date was missing, so this window was not read.")
        if not _primary_ok(primary):
            raise WindowError("incomplete", "A primary document name was not usable, so this window was not read.")
        if accession in seen:
            continue
        seen.add(accession)
        rows.append(Row(accession, form.strip(), filing_date, accepted[index], primary))
    files = filings.get("files")
    older: int | None
    if files is None:
        older = None
    elif isinstance(files, list):
        older = len(files)
    else:
        raise WindowError("incomplete", "The older-page list was not readable, so this window was not read.")
    dates = [row.filing_date for row in rows]
    return Window(
        cik=cik,
        name=name.strip(),
        tickers=tuple(tickers),
        rows=tuple(rows),
        older_pages=older,
        filing_from=min(dates) if dates else "",
        filing_to=max(dates) if dates else "",
    )


def observe(baselined: bool, seen: set[str], rows: list[Row]) -> tuple[list[Row], set[str], bool]:
    """Return new rows, the replacement seen set, and whether a baseline now exists.

    The first complete read stores every accession and returns no rows.
    A later read returns accessions that were absent. Dropped accessions leave the set.
    The same accession is not returned again when another field changes.
    Order changes do not count as new filings.
    """
    current: list[Row] = []
    accessions: set[str] = set()
    for row in rows:
        if row.accession in accessions:
            continue
        accessions.add(row.accession)
        current.append(row)
    if not baselined:
        return [], accessions, True
    fresh = [row for row in current if row.accession not in seen]
    return fresh, accessions, True


def document_urls(cik: object, accession: str, primary: str) -> tuple[str, str]:
    """Index URL and primary document URL under the subject company CIK."""
    cik10 = pad_cik(cik)
    if not isinstance(accession, str) or not ACCESSION_RE.fullmatch(accession):
        raise WindowError("accession", "The accession number must look like 0000320193-26-000026.")
    if not _primary_ok(primary):
        raise WindowError("primary", "The primary document name cannot be used in an archive URL.")
    directory = str(int(cik10))
    folder = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{directory}/{folder}/"
    from urllib.parse import quote
    index = base + quote(accession, safe="") + "-index.html"
    primary_url = base + "/".join(quote(part, safe="") for part in primary.split("/"))
    return index, primary_url


def index_tickers(payload: object) -> dict[str, list[tuple[int, str, str]]]:
    """Map an uppercase ticker to (cik_str, title, SEC ticker spelling)."""
    if not isinstance(payload, dict):
        raise WindowError("tickers", "The ticker file was not an object, so no company was chosen.")
    found: dict[str, list[tuple[int, str, str]]] = {}
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        ticker = value.get("ticker")
        cik = value.get("cik_str")
        title = value.get("title")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        if isinstance(cik, bool) or not isinstance(cik, int) or isinstance(cik, bool):
            continue
        if cik <= 0:
            continue
        label = title.strip() if isinstance(title, str) else ""
        found.setdefault(ticker.strip().upper(), []).append((cik, label, ticker.strip()))
    if not found:
        raise WindowError("tickers", "The ticker file had no rows, so no company was chosen.")
    return found


def prepare_token(value: str) -> str:
    """Fold fullwidth letters, then drop space, a trailing comment, and a trailing semicolon."""
    text = unicodedata.normalize("NFKC", value).strip()
    text = _COMMENT.sub("", text).strip()
    return _TRAIL.sub("", text).strip()


def _sec_url(text: str) -> bool:
    return bool(_URL.match(text) and _SEC_URL.match(text))


def _cik_from_sec_url(text: str) -> str | None:
    """Digits from a data.sec.gov or www.sec.gov URL. Other hosts return None."""
    if not _sec_url(text):
        return None
    patterns = (
        r"(?i)(?:^|[/._-])CIK(\d{1,10})(?!\d)",
        r"(?i)/data/(\d{1,10})(?!\d)",
        r"(?i)[?&]CIK=(\d{1,10})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _ticker_from_sec_url(text: str) -> str | None:
    if not _sec_url(text):
        return None
    match = re.search(r"(?i)[?&]CIK=([A-Za-z][A-Za-z0-9._-]{0,11})(?:&|#|$)", text)
    if match and TICKER_RE.fullmatch(match.group(1)):
        return match.group(1)
    return None


def read_company_token(value: str, *, field: str) -> tuple[str, str]:
    """Return ``(ticker, cik_digits)`` from one field. The other side is empty.

    Kinds: spaced ticker, trailing semicolon, fullwidth letters, trailing
    ``# comment``, and an SEC URL that carries a CIK or a ticker. An accented
    name, a slash class, a quote, a question mark, and a non-SEC URL raise.
    """
    if not isinstance(value, str):
        kind = "cik" if field == "cik" else "ticker"
        message = _CIK_GRAMMAR if field == "cik" else _TICKER_GRAMMAR
        raise WindowError(kind, message)
    text = prepare_token(value)
    if not text:
        kind = "cik" if field == "cik" else "ticker"
        message = _CIK_GRAMMAR if field == "cik" else _TICKER_GRAMMAR
        raise WindowError(kind, message)
    if _URL.match(text):
        cik = _cik_from_sec_url(text)
        if cik:
            return "", cik
        ticker = _ticker_from_sec_url(text)
        if ticker:
            return ticker, ""
        kind = "cik" if field == "cik" else "ticker"
        message = _CIK_GRAMMAR if field == "cik" else _TICKER_GRAMMAR
        raise WindowError(kind, message)
    if field == "cik":
        return "", text
    if not TICKER_RE.fullmatch(text):
        raise WindowError("ticker", _TICKER_GRAMMAR)
    return text, ""


def prepare_accession(value: str) -> str:
    """Drop a trailing comment. An SEC URL keeps the accession number inside it."""
    if not isinstance(value, str):
        raise WindowError("accession", "The accession number must look like 0000320193-26-000026.")
    text = prepare_token(value)
    if _URL.match(text):
        if not _sec_url(text):
            raise WindowError("accession", "The accession number must look like 0000320193-26-000026.")
        match = _ACC_IN_TEXT.search(text)
        if match and ACCESSION_RE.fullmatch(match.group(0)):
            return match.group(0)
        raise WindowError("accession", "The accession number must look like 0000320193-26-000026.")
    return text


def resolve_ticker(index: dict[str, list[tuple[int, str, str]]], ticker: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Return ('one', [hit]) or ('ambiguous', hits) or ('miss', []).

    A ticker that contains '.' is also tried with '-' when the original misses.
    Two different CIKs are ambiguous. The function does not pick one.
    """
    if not isinstance(ticker, str):
        raise WindowError("ticker", "A ticker is 1 to 12 letters, digits, dots, or hyphens.")
    ticker = prepare_token(ticker)
    if not TICKER_RE.fullmatch(ticker):
        raise WindowError("ticker", "A ticker is 1 to 12 letters, digits, dots, or hyphens.")
    key = ticker.upper()
    original = list(index.get(key, []))
    alternate: list[tuple[int, str, str]] = []
    if "." in ticker:
        other = ticker.replace(".", "-").upper()
        if other != key:
            alternate = list(index.get(other, []))
    if original and alternate:
        left = {item[0] for item in original}
        right = {item[0] for item in alternate}
        if left != right:
            return "ambiguous", original + alternate
        hits = original
    elif original:
        hits = original
    else:
        hits = alternate
    ciks = {item[0] for item in hits}
    if not ciks:
        return "miss", []
    if len(ciks) > 1:
        return "ambiguous", hits
    return "one", hits[:1]


def norm_form(value: str) -> str:
    text = prepare_token(value).upper()
    if not FORM_RE.fullmatch(text):
        raise WindowError("form", "A form filter is a short SEC form name such as 10-K, 10-Q, or 8-K.")
    return text


def form_allowed(form: str, forms: list[str]) -> bool:
    if not forms:
        return True
    return form.strip().upper() in {item.upper() for item in forms}
