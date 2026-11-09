"""Shared stand-ins. Tests stay off the network."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTACT = "Ada Example ada@example.com"
OTHER_CONTACT = "Bev Example bev@example.com"
EIGHT = "0001140361-26-042798"
TEN = "0000320193-26-000026"
FRESH = "0000320193-26-000099"
CIK = "0000320193"
CIK_INT = 320193

_DEFAULT_PLATFORMS = object()


def ensure_import_paths() -> None:
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def install_secret_scope() -> None:
    """Use Hermes when it imports. Otherwise install a dict stand-in."""
    ensure_import_paths()
    try:
        from agent.secret_scope import set_secret_scope
    except Exception:
        _install_stub()
        from agent.secret_scope import set_secret_scope
    set_secret_scope({})


def _install_stub() -> None:
    if "agent.secret_scope" in sys.modules and hasattr(sys.modules["agent.secret_scope"], "get_secret"):
        return
    agent = sys.modules.get("agent")
    if agent is None:
        agent = types.ModuleType("agent")
        agent.__path__ = []  # type: ignore[attr-defined]
        sys.modules["agent"] = agent
    scope = types.ModuleType("agent.secret_scope")
    box: dict[str, str] = {}

    def get_secret(name, default=None):
        if name in box:
            return box[name]
        return default

    def set_secret_scope(mapping=None, profile_home=None):
        box.clear()
        if mapping:
            box.update({str(key): str(value) for key, value in mapping.items()})
        return None

    def build_profile_secret_scope(home):
        path = Path(home) / ".env"
        found: dict[str, str] = {}
        if not path.is_file():
            return found
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            found[key.strip()] = value.strip()
        return found

    scope.get_secret = get_secret
    scope.set_secret_scope = set_secret_scope
    scope.build_profile_secret_scope = build_profile_secret_scope
    sys.modules["agent.secret_scope"] = scope


def use_contact(value: str = CONTACT) -> None:
    from agent.secret_scope import set_secret_scope

    set_secret_scope({"US_FILINGS_CONTACT": value})


def filing_row(accession: str, form: str, filing_date: str, primary: str, accepted: str = "2026-11-09T21:04:00.000Z") -> dict:
    return {
        "accession": accession,
        "form": form,
        "filingDate": filing_date,
        "primaryDocument": primary,
        "acceptanceDateTime": accepted,
    }


def eight_row() -> dict:
    return filing_row(EIGHT, "8-K", "2026-11-09", "ef20085077_8k.htm")


def ten_row() -> dict:
    return filing_row(TEN, "10-K", "2026-11-03", "aapl-20260926.htm", "2026-11-03T18:00:00.000Z")


def submissions_payload(rows: list[dict], *, cik: str = CIK, name: str = "Apple Inc.", tickers: list[str] | None = None, files=None, include_files: bool = True) -> dict:
    recent = {
        "accessionNumber": [],
        "form": [],
        "filingDate": [],
        "primaryDocument": [],
        "acceptanceDateTime": [],
    }
    for row in rows:
        recent["accessionNumber"].append(row["accession"])
        recent["form"].append(row["form"])
        recent["filingDate"].append(row["filingDate"])
        recent["primaryDocument"].append(row["primaryDocument"])
        recent["acceptanceDateTime"].append(row.get("acceptanceDateTime", ""))
    filings: dict = {"recent": recent}
    if include_files:
        filings["files"] = [] if files is None else files
    return {
        "cik": cik,
        "name": name,
        "tickers": ["AAPL"] if tickers is None else tickers,
        "filings": filings,
    }


def ticker_file() -> dict:
    return {
        "0": {"cik_str": CIK_INT, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    }


class Transport:
    def __init__(self, books: dict[str, tuple[int, object]]) -> None:
        self.books = books
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, contact: str):
        self.calls.append((url, contact))
        if url not in self.books:
            raise AssertionError("unexpected url " + url)
        status, body = self.books[url]
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        if isinstance(body, str):
            body = body.encode("utf-8")
        return status, body


def apple_transport(rows: list[dict] | None = None, **kwargs) -> Transport:
    from client import TICKERS_URL, submissions_url

    payload = submissions_payload(rows if rows is not None else [eight_row(), ten_row()], **kwargs)
    return Transport({
        TICKERS_URL: (200, ticker_file()),
        submissions_url(CIK): (200, payload),
    })


class FakeCron:
    def __init__(self) -> None:
        self.jobs: list[dict[str, str]] = []
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = ""
        self.remove_keeps = False

    def load_jobs(self, argv: list[str]) -> list[dict[str, str]]:
        self.calls.append(("list", list(argv)))
        if self.fail == "list":
            from public_cron import CronUnread

            raise CronUnread("list failed")
        return [dict(job) for job in self.jobs]

    def run_argv(self, argv: list[str]):
        from public_cron import Cmd

        self.calls.append(("run", list(argv)))
        if self.fail == "run":
            return Cmd(1, "", "remove failed")
        if "remove" in argv and not self.remove_keeps:
            job_id = argv[argv.index("remove") + 1]
            self.jobs = [job for job in self.jobs if job["id"] != job_id]
        return Cmd(0, "removed", "")

    def create_from_argv(self, argv: list[str], *, name: str, schedule: str, deliver: str) -> dict[str, str]:
        self.calls.append(("create", list(argv)))
        if self.fail == "create":
            raise RuntimeError("create failed")
        job = {"id": f"job{len(self.jobs) + 1}", "name": name, "schedule": schedule, "deliver": deliver}
        self.jobs.append(job)
        return dict(job)


def make_deps(tmp_path: Path, transport=None, profile: str = "default", platforms=_DEFAULT_PLATFORMS):
    from watch import Deps

    data = tmp_path / "data"
    home = tmp_path / "home"
    data.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    cron = FakeCron()
    chosen = {"local", "origin"} if platforms is _DEFAULT_PLATFORMS else platforms
    deps = Deps(
        data_dir=data,
        cron=cron,
        platforms=chosen,
        profile=profile,
        home=home,
        transport=transport,
    )
    return deps, cron


def bind_hook(monkeypatch, deps) -> None:
    import watch

    monkeypatch.setattr(watch, "deps_for_hook", lambda: deps)


def fingerprint(root: Path) -> list[tuple[str, bytes]]:
    found = []
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found.append((str(path.relative_to(root)), path.read_bytes()))
    return found
