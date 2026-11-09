"""Keep every test off the network and off a real Hermes home."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_HERE), str(_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from support import ensure_import_paths, install_secret_scope

ensure_import_paths()
install_secret_scope()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import client
    import gates

    client.reset_runtime()
    client.GAP_SECONDS = 0
    client._sleep = lambda _seconds: None
    gates._STASH.clear()
    for name in (
        "US_FILINGS_CONTACT",
        "HERMES_CRON_SESSION",
        "HERMES_SINGLE_QUERY_SESSION",
        "HERMES_SESSION_PLATFORM",
        "HERMES_PLUGIN_HOST_PROCESS",
    ):
        monkeypatch.delenv(name, raising=False)
    install_secret_scope()

    def boom(url, _contact):
        raise AssertionError("unexpected SEC call " + url)

    client.set_transport(boom)
    yield
    client.reset_runtime()
    gates._STASH.clear()
