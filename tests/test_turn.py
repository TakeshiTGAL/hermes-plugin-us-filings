"""No turn hook rewrites a conversation. Approval stays on add and remove."""
import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Phrases that used to gain a paragraph, plus ordinary filing asks and weather.
_SAMPLES = (
    "What is the weather in Tokyo tomorrow?",
    "hermes の設定はどこで見る？",
    "wait a sec",
    "Give me a sec",
    "10 sec",
    "ask edgar about lunch",
    "What is the GenBank accession number for BRCA1?",
    "What is the library accession number?",
    "What's a good 10-K training plan?",
    "UTF-8-K",
    "I work at the SEC",
    "the SEC cafeteria is closed",
    "Don't talk about the SEC",
    "ask the sec about lunch",
    "10-Kのトレーニング計画を見て",
    "I have a 10-K. 明日の天気を見て",
    "Show recent filings for AAPL",
    "アップルの10-Kを見せて",
    "check the sec",
    "show the 10-k",
    "cik",
    "1234567890-11-10001",
)


def _plugin():
    spec = importlib.util.spec_from_file_location(
        "us_filings_turn",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["us_filings_turn"] = module
    spec.loader.exec_module(module)
    return module


def test_register_lists_only_the_approval_hook():
    module = _plugin()

    class Ctx:
        def __init__(self):
            self.hooks = []
            self.skills = []

        def register_hook(self, name, _callback):
            self.hooks.append(name)

        def register_skill(self, name, _path, description=""):
            self.skills.append((name, description))

        def __getattr__(self, _name):
            def _noop(*_args, **_kwargs):
                return None
            return _noop

    ctx = Ctx()
    ctx.profile_name = "default"
    module.register(ctx)
    assert ctx.hooks == ["pre_tool_call"]
    assert ctx.skills == [("us-filings", module._SKILL_DESCRIPTION)]
    assert "SEC EDGAR" in module._SKILL_DESCRIPTION
    assert not hasattr(module, "_nudge")
    assert not (ROOT / "turn_scope.py").exists()
    product = list(ROOT.glob("*.py")) + list((ROOT / "skills").rglob("*.md"))
    product.append(ROOT / "README.md")
    product.append(ROOT / "plugin.yaml")
    for path in product:
        text = path.read_text(encoding="utf-8")
        assert "pre_llm_call" not in text, path.name
        assert "turn_scope" not in text, path.name
        assert "skill index" not in text.lower(), path.name
        assert "skills index" not in text.lower(), path.name


def test_every_sample_keeps_the_same_bytes():
    from watch import on_pre_tool_call

    for text in _SAMPLES:
        history = [
            {"role": "user", "content": "Show the latest SEC filing for AAPL"},
            {"role": "assistant", "content": "Call us_filings_recent for the 10-K."},
            {"role": "user", "content": text},
        ]
        before_text = text
        before_history = copy.deepcopy(history)
        assert on_pre_tool_call(
            "web_search",
            {"query": text},
            user_message=text,
            conversation_history=history,
        ) is None
        assert on_pre_tool_call(
            "us_filings_recent",
            {"ticker": "AAPL"},
            user_message=text,
            conversation_history=history,
        ) is None
        assert on_pre_tool_call(
            "us_filings_watch",
            {"action": "list"},
            user_message=text,
            conversation_history=history,
        ) is None
        assert text == before_text
        assert text.encode("utf-8") == before_text.encode("utf-8")
        assert history == before_history


def test_skill_and_readme_name_the_approval_message():
    skill = (ROOT / "skills" / "us-filings" / "SKILL.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    catalog = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert "Before the user approves" in skill
    assert "us_filings_watch" in skill
    assert "hermes cron" in skill
    assert 'hermes plugins install "file://$PWD"' in readme
    assert "hermes plugins enable us-filings" in readme
    assert 'hermes us-filings call us_filings_recent --json \'{"ticker":"AAPL"}\'' in readme
    assert "command field" in readme
    assert "The conversation text stays as the user wrote it." in readme
    description = skill.split("description:", 1)[1].split("\n", 1)[0].strip().strip('"')
    assert len(description) <= 223
    assert "SEC EDGAR" in description
    body = catalog.split("description:", 1)[1].split("\n", 1)[0].strip().strip('"')
    assert len(body) <= 700
    assert body.count("Disclosure —") == 1
    lead = body.split(" Disclosure —", 1)[0]
    assert lead.count(".") == 2
