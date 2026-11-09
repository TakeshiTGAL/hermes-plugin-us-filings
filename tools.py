"""Tool handlers."""
from __future__ import annotations

if __package__:
    from .watch import Deps, locate, recent, watch_tool
else:
    from watch import Deps, locate, recent, watch_tool


def us_filings_recent(args, **_kwargs) -> str:
    return recent(Deps(), args or {})


def us_filings_locate(args, **_kwargs) -> str:
    return locate(Deps(), args or {})


def us_filings_watch(args, **kwargs) -> str:
    return watch_tool(Deps(), args or {}, **kwargs)


HANDLERS = {
    "us_filings_recent": us_filings_recent,
    "us_filings_locate": us_filings_locate,
    "us_filings_watch": us_filings_watch,
}
