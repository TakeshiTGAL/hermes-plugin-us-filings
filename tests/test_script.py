"""The morning script binds the profile scope. Hermes still blocks an unattended write."""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from support import CONTACT, OTHER_CONTACT, ROOT


def _hermes_src() -> str:
    src = os.environ.get("HERMES_SRC", "")
    if not src or not Path(src).is_dir():
        pytest.skip("HERMES_SRC is not set")
    return src


def _copy_plugin(home: Path) -> Path:
    plugin = home / "plugins" / "us-filings"
    plugin.mkdir(parents=True)
    for path in ROOT.glob("*.py"):
        shutil.copy(path, plugin / path.name)
    return plugin


def test_watch_script_uses_the_public_scope_helpers():
    import watch

    text = watch.WATCH_SCRIPT
    assert "build_profile_secret_scope" in text
    assert "set_secret_scope" in text
    assert "get_hermes_home" in text
    assert "US_FILINGS_CONTACT" in text
    assert "was not treated as unset" in text
    assert CONTACT not in text
    assert "os.environ" not in text


def test_installed_script_exits_quietly_when_the_contact_is_unset(tmp_path):
    src = _hermes_src()
    home = tmp_path / "hermes"
    _copy_plugin(home)
    script = home / "scripts" / "us-filings-watch.py"
    script.parent.mkdir()
    import watch

    script.write_text(watch.WATCH_SCRIPT, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "user"),
        "HERMES_HOME": str(home),
        "PYTHONPATH": src,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == ""
    assert "US_FILINGS_CONTACT" in proc.stderr
    assert "was not treated as unset" not in proc.stderr
    assert CONTACT not in proc.stderr


def test_profile_dotenv_wins_over_the_process_environment(tmp_path):
    src = _hermes_src()
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text("US_FILINGS_CONTACT=" + CONTACT + "\n", encoding="utf-8")
    code = textwrap.dedent(
        """\
        import os, sys
        from pathlib import Path
        os.environ["HERMES_HOME"] = sys.argv[1]
        from hermes_constants import get_hermes_home
        from agent.secret_scope import build_profile_secret_scope, set_secret_scope
        home = Path(get_hermes_home())
        set_secret_scope(build_profile_secret_scope(home))
        sys.path.insert(0, sys.argv[2])
        import client
        got = client.read_contact()
        file_value = Path(sys.argv[3]).read_text(encoding="utf-8").strip().split("=", 1)[1]
        if got == file_value and got != os.environ.get("US_FILINGS_CONTACT"):
            print("file")
        else:
            print("other")
        """
    )
    runner = tmp_path / "scope.py"
    runner.write_text(code, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "user"),
        "HERMES_HOME": str(home),
        "US_FILINGS_CONTACT": OTHER_CONTACT,
        "PYTHONPATH": src,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(runner), str(home), str(ROOT), str(home / ".env")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "file"
    assert CONTACT not in proc.stdout
    assert OTHER_CONTACT not in proc.stdout


def test_hermes_blocks_unattended_and_yolo_still_needs_the_hook(tmp_path):
    src = _hermes_src()
    script = tmp_path / "gate.py"
    script.write_text(Path(__file__).with_name("hermes_gate.py").read_text(encoding="utf-8"), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "user"),
        "HERMES_HOME": str(tmp_path / "hh"),
        "HERMES_SRC": src,
        "PYTHONPATH": src + os.pathsep + str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(ROOT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "ok"
