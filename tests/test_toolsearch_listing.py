"""Tool Search listing: the first sentence is one short line and not a mechanism line."""
import ast
import importlib.util
import re
import sys
from pathlib import Path

_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)[.!?](?=\s|$)")
_BAD = ("read-only", "take no arguments", "takes no arguments", "get /", "post /", "put /", "delete /")


def _first(text):
    text = " ".join((text or "").split())
    match = _END.search(text)
    return text[: match.end()] if match else text


def test_schema_descriptions_are_string_constants():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "schemas.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value in {"name", "description"}:
                assert isinstance(value, ast.Constant) and isinstance(value.value, str)
                fields[key.value] = value.value
        if "name" in fields and "description" in fields and fields["name"].startswith("us_filings_"):
            found.append((fields["name"], fields["description"]))
    assert len(found) == 3
    for name, description in found:
        sentence = _first(description)
        assert len(sentence) <= 60, (name, len(sentence), sentence)
        assert len(description) <= 1024
        assert not sentence.lower().startswith(_BAD)


def test_register_lists_the_same_sentences():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "us_filings_listing",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["us_filings_listing"] = module
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.found = {}
            self.hooks = []

        def register_tool(self, *args, **kwargs):
            schema = kwargs.get("schema")
            name = kwargs.get("name")
            if isinstance(schema, dict):
                self.found[name or schema.get("name")] = schema.get("description")

        def register_hook(self, name, _callback):
            self.hooks.append(name)

        def __getattr__(self, _name):
            def _noop(*_args, **_kwargs):
                return None
            return _noop

    ctx = Ctx()
    ctx.profile_name = "default"
    module.register(ctx)
    assert set(ctx.found) == {"us_filings_recent", "us_filings_locate", "us_filings_watch"}
    assert ctx.hooks == ["pre_tool_call"]
    for description in ctx.found.values():
        sentence = _first(description)
        assert len(sentence) <= 60
        assert "SEC EDGAR" in sentence
