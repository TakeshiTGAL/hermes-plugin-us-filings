"""HTTPS to data.sec.gov and www.sec.gov. The User-Agent is the user's contact."""
from __future__ import annotations

import contextlib
import contextvars
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from .filings import WindowError, index_tickers, pad_cik
else:
    from filings import WindowError, index_tickers, pad_cik

CONTACT_ENV = "US_FILINGS_CONTACT"
FAIR_ACCESS = "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
HOSTS = frozenset({"data.sec.gov", "www.sec.gov"})
TIMEOUT_SECONDS = 30
GAP_SECONDS = 0.2
SUBMISSIONS_CAP = 8_000_000
TICKERS_CAP = 4_000_000
TOOL_BUDGET = 8
MORNING_BUDGET = 21
TICKER_TTL = 3600

CONTACT_STEPS = (
    f"Set {CONTACT_ENV} in the profile .env to your name and email on one line. "
    "SEC fair access wants that text in the User-Agent. "
    f"The rule is on {FAIR_ACCESS}"
)
WAIT_STEPS = (
    "Wait about 10 minutes before another SEC request and check that the User-Agent is your contact. "
    f"The rule is on {FAIR_ACCESS}"
)


class SecError(Exception):
    def __init__(self, message: str, kind: str, next_step: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.next_step = next_step


class ContactUnset(SecError):
    def __init__(self) -> None:
        super().__init__(
            f"{CONTACT_ENV} is unset, so SEC was not called.",
            "contact_unset",
            CONTACT_STEPS,
        )


class ContactUnreadable(SecError):
    def __init__(self) -> None:
        super().__init__(
            "The contact helper could not be read. "
            f"{CONTACT_ENV} was not treated as unset.",
            "contact_unreadable",
            "Retry after Hermes can read the profile secret scope. Do not invent a filing.",
        )


class ContactInvalid(SecError):
    def __init__(self) -> None:
        super().__init__(
            f"{CONTACT_ENV} must be one line with @ and no line break.",
            "contact_invalid",
            CONTACT_STEPS,
        )


_LOCK = threading.Lock()
_last = 0.0
_budget: contextvars.ContextVar[int | None] = contextvars.ContextVar("us_filings_budget", default=None)
_transport: contextvars.ContextVar[object | None] = contextvars.ContextVar("us_filings_transport", default=None)
_ticker_cache: dict[str, object] = {"at": 0.0, "index": None}
_sleep = time.sleep


def reset_runtime() -> None:
    """Clear the in-memory ticker cache, budget, and gap clock. Tests call this."""
    global _last
    with _LOCK:
        _last = 0.0
    _budget.set(None)
    _transport.set(None)
    _ticker_cache["at"] = 0.0
    _ticker_cache["index"] = None


def set_transport(fn) -> None:
    _transport.set(fn)


@contextlib.contextmanager
def call_budget(limit: int):
    token = _budget.set(limit)
    try:
        yield
    finally:
        _budget.reset(token)


def submissions_url(cik10: str) -> str:
    return f"https://data.sec.gov/submissions/CIK{cik10}.json"


def scrub(text: str, secret: str = "") -> str:
    cleaned = re.sub(r"https?://\S+", "<url>", str(text))
    if secret:
        cleaned = cleaned.replace(secret, "<contact>")
    return cleaned


def read_contact() -> str:
    """Read US_FILINGS_CONTACT through get_secret. Never return it in an error."""
    try:
        from agent.secret_scope import get_secret
    except Exception as exc:
        raise ContactUnreadable() from exc
    try:
        value = get_secret(CONTACT_ENV)
    except Exception as exc:
        raise ContactUnreadable() from exc
    if value is None or not str(value).strip():
        raise ContactUnset()
    text = str(value).strip()
    if len(text) < 8 or len(text) > 200 or "@" not in text or any(ord(ch) < 32 for ch in text):
        raise ContactInvalid()
    return text


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or host not in HOSTS or parts.username or parts.password:
        raise SecError(
            "The plugin only calls data.sec.gov and www.sec.gov over HTTPS.",
            "host",
            "Pass a ticker or CIK. The plugin does not call another host.",
        )


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SecError(
            "The SEC response redirected. The plugin did not follow it.",
            "redirect",
            "Try again later. The plugin only talks to data.sec.gov and www.sec.gov.",
        )


def _opener():
    """Ignore proxy variables so a contact is not sent to another host."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)


def _take_budget() -> None:
    left = _budget.get()
    if left is None:
        left = TOOL_BUDGET
    if left <= 0:
        raise SecError(
            "This call reached its SEC request cap.",
            "budget",
            "Narrow the request and try again. There is no daily cap.",
        )
    _budget.set(left - 1)


def _wait_gap() -> None:
    global _last
    with _LOCK:
        delay = GAP_SECONDS - (time.monotonic() - _last)
        if delay > 0:
            _sleep(delay)
        _last = time.monotonic()


def _status_error(status: int) -> SecError:
    if status in (403, 429):
        return SecError(
            f"SEC returned HTTP {status}. No filing list was read.",
            f"http_{status}",
            WAIT_STEPS,
        )
    return SecError(
        f"SEC returned HTTP {status}. No filing list was read.",
        "http_error",
        "Try again later. Do not treat the company as having no filings.",
    )


def fetch(url: str, cap: int) -> tuple[int, bytes]:
    """GET one URL. The contact is read again on this call and is not logged."""
    _check_url(url)
    contact = read_contact()
    _take_budget()
    _wait_gap()
    transport = _transport.get()
    if transport is not None:
        status, body = transport(url, contact)
        if not isinstance(body, (bytes, bytearray)):
            raise SecError("The SEC stand-in returned a body that was not bytes.", "bad_body", "Try again later.")
        body = bytes(body)
        if len(body) > cap:
            raise SecError("The SEC response was larger than the cap.", "too_large", "Try again later.")
        if status != 200:
            raise _status_error(int(status))
        return int(status), body
    request = urllib.request.Request(
        url,
        headers={"User-Agent": contact, "Accept": "application/json"},
        method="GET",
    )
    try:
        with _opener().open(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(cap + 1)
    except SecError:
        raise
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            exc.read(256)
        except Exception:
            pass
        raise _status_error(status) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SecError(
            "The SEC request did not finish.",
            "network",
            "Check connectivity and try again later. Do not treat the company as having no filings.",
        ) from None
    if len(body) > cap:
        raise SecError("The SEC response was larger than the cap.", "too_large", "Try again later.")
    if status != 200:
        raise _status_error(status)
    return status, body


def get_json(url: str, cap: int) -> object:
    _status, body = fetch(url, cap)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise SecError(
            "The SEC response was not JSON, so no filing list was read.",
            "bad_json",
            "Try again later. Do not treat the company as having no filings.",
        ) from None


def ticker_index(now: float | None = None) -> dict:
    moment = time.monotonic() if now is None else now
    cached = _ticker_cache.get("index")
    at = float(_ticker_cache.get("at") or 0.0)
    if cached is not None and moment - at < TICKER_TTL:
        return cached  # type: ignore[return-value]
    try:
        payload = get_json(TICKERS_URL, TICKERS_CAP)
        parsed = index_tickers(payload)
    except WindowError as exc:
        raise SecError(str(exc), exc.kind, "Try again later. No company was chosen.") from None
    _ticker_cache["index"] = parsed
    _ticker_cache["at"] = moment
    return parsed


def fetch_submissions(cik: object) -> object:
    try:
        cik10 = pad_cik(cik)
    except WindowError as exc:
        raise SecError(str(exc), "cik", "Pass a CIK of 1 to 10 digits.") from None
    return get_json(submissions_url(cik10), SUBMISSIONS_CAP)
