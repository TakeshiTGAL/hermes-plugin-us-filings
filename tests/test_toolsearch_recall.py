"""Ten ordinary English requests should put one of these tools in the top 5."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

QUERIES = [
    "recent SEC filings for a ticker",
    "SEC filing accession number location",
    "new SEC filings each morning",
    "10-K filing for a CIK",
    "10-Q filing date accession",
    "8-K filing document location",
    "watch a ticker for SEC filings",
    "list recent EDGAR filings",
    "find an SEC filing by accession",
    "morning SEC filing watch",
]


def test_ten_queries_rank_in_the_top_five(tmp_path):
    src = os.environ.get("HERMES_SRC", "")
    if not src or not Path(src).is_dir():
        pytest.skip("HERMES_SRC is not set")
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "recall.py"
    script.write_text(
        "import importlib.util, os, sys\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "os.environ['HERMES_HOME'] = sys.argv[3]\n"
        "spec = importlib.util.spec_from_file_location('recall_us_filings', root / '__init__.py', submodule_search_locations=[str(root)])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['recall_us_filings'] = module\n"
        "spec.loader.exec_module(module)\n"
        "class Ctx:\n"
        "    def __init__(self):\n"
        "        self.tools = []\n"
        "    def register_tool(self, *args, **kwargs):\n"
        "        schema = kwargs.get('schema')\n"
        "        name = kwargs.get('name')\n"
        "        if isinstance(schema, dict):\n"
        "            self.tools.append({'name': name or schema.get('name'), 'schema': schema})\n"
        "    def __getattr__(self, _name):\n"
        "        return lambda *a, **k: None\n"
        "ctx = Ctx()\n"
        "ctx.profile_name = 'default'\n"
        "module.register(ctx)\n"
        "from toolsets import _HERMES_CORE_TOOLS\n"
        "import model_tools\n"
        "from tools.registry import registry\n"
        "from tools.tool_search_catalog import build_catalog, search_catalog\n"
        "core = []\n"
        "names = set(_HERMES_CORE_TOOLS)\n"
        "for entry in registry.get_all_entries():\n"
        "    if entry.name in names:\n"
        "        core.append({'type': 'function', 'function': {**entry.schema, 'name': entry.name}})\n"
        "for tool in ctx.tools:\n"
        "    registry.register(name=tool['name'], toolset='us_filings', schema=tool['schema'], handler=lambda **_k: '', override=True)\n"
        "    core.append({'type': 'function', 'function': tool['schema']})\n"
        "catalog = build_catalog(core)\n"
        "own = {tool['name'] for tool in ctx.tools}\n"
        "queries = sys.argv[4:]\n"
        "hits = 0\n"
        "for query in queries:\n"
        "    ranked = [item.name for item in search_catalog(catalog, query, limit=5)]\n"
        "    ok = any(name in own for name in ranked)\n"
        "    hits += int(ok)\n"
        "    print(('HIT' if ok else 'MISS'), query, ranked)\n"
        "print(f'{hits}/10')\n"
        "raise SystemExit(0 if hits >= 7 else 1)\n",
        encoding="utf-8",
    )
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "user"),
        "HERMES_HOME": str(home),
        "HERMES_SRC": src,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(script), str(root), src, str(home), *QUERIES],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "/10" in proc.stdout.strip().splitlines()[-1]
