"""US SEC filings: one morning message when a watched company files."""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL = Path(__file__).resolve().parent / "skills" / "us-filings" / "SKILL.md"
_SKILL_DESCRIPTION = (
    "SEC EDGAR filings for a watched US company. "
    "Before web_search, use tool_search and tool_call us_filings_recent, us_filings_locate, or us_filings_watch. "
    "Copy next_step. Do not invent a filing or a figure."
)


def register(ctx) -> None:
    if __package__:
        from .gates import bind_profile_name
        from .schemas import ALL_SCHEMAS
        from .tools import HANDLERS
        from .watch import on_pre_tool_call
        from .cli import handle, setup_parser
    else:
        from gates import bind_profile_name
        from schemas import ALL_SCHEMAS
        from tools import HANDLERS
        from watch import on_pre_tool_call
        from cli import handle, setup_parser
    try:
        reported = ctx.profile_name
    except Exception:
        reported = None
    bind_profile_name(reported)
    for schema in ALL_SCHEMAS:
        ctx.register_tool(
            name=schema["name"],
            toolset="us_filings",
            schema=schema,
            handler=HANDLERS[schema["name"]],
        )
    if callable(getattr(ctx, "register_hook", None)):
        ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_cli_command(
        name="us-filings",
        help="List SEC filings and watch for a new filing",
        setup_fn=setup_parser,
        handler_fn=handle,
        description="Local command for SEC filing tools.",
    )
    try:
        ctx.register_skill("us-filings", _SKILL, description=_SKILL_DESCRIPTION)
    except Exception:
        logger.warning("us-filings skill was not registered", exc_info=True)
