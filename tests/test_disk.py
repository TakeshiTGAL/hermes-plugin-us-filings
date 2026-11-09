"""Writes stay on the real path. A link is not a place to write."""
import os
import shutil
import uuid
from pathlib import Path

import pytest

from safe_write import HARD_LINK_CUT, WriteRefused, read_bytes, remove_file, write_bytes
from store import empty_state, load_state, save_state


def test_symlink_file_parent_and_dotdot_are_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "kept"
    target.write_bytes(b"kept")
    (root / "state.json").symlink_to(target)
    with pytest.raises(WriteRefused) as caught:
        write_bytes(root, "state.json", b"new", create=True)
    assert caught.value.kind == "symlink"
    assert target.read_bytes() == b"kept"
    with pytest.raises(WriteRefused):
        read_bytes(root, "state.json")
    with pytest.raises(WriteRefused):
        remove_file(root, "state.json")
    assert target.read_bytes() == b"kept"

    (root / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WriteRefused):
        write_bytes(root, "sub/file.txt", b"x", create=True)
    assert list(outside.iterdir()) == [target]

    for relative in ("../escape", "/tmp/escape", "a/../../b", "", "bad\\name"):
        with pytest.raises(WriteRefused):
            write_bytes(root, relative, b"z", create=True)


def test_hardlink_is_cut_and_the_other_name_keeps_the_old_bytes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    first = root / "state.json"
    first.write_bytes(b"old")
    second = root / "alias"
    os.link(first, second)
    note = write_bytes(root, "state.json", b"new", create=True)
    assert note == HARD_LINK_CUT
    assert first.read_bytes() == b"new"
    assert second.read_bytes() == b"old"
    assert first.stat().st_nlink == 1


def test_parent_symlink_is_the_real_directory_and_the_file_link_is_refused(tmp_path):
    real_parent = tmp_path / "private"
    real_parent.mkdir()
    alias = tmp_path / "tmp"
    alias.symlink_to(real_parent, target_is_directory=True)
    root = alias / "plugin-data" / "us-filings"
    save_state(root, empty_state())
    stored = real_parent / "plugin-data" / "us-filings" / "state.json"
    assert stored.is_file()
    assert not stored.is_symlink()
    status, loaded = load_state(root)
    assert status == "ok"
    assert loaded["companies"] == []
    status, _loaded = load_state(Path(os.path.realpath(root)))
    assert status == "ok"
    write_bytes(alias / "home", "scripts/us-filings-watch.py", b"print(1)\n", create=True)
    script = real_parent / "home" / "scripts" / "us-filings-watch.py"
    assert script.read_bytes() == b"print(1)\n"
    outside = tmp_path / "secret"
    outside.write_bytes(b"nope")
    stored.unlink()
    stored.symlink_to(outside)
    with pytest.raises(WriteRefused) as caught:
        read_bytes(root, "state.json")
    assert caught.value.kind == "symlink"
    assert outside.read_bytes() == b"nope"
    status, _loaded = load_state(root)
    assert status == "corrupt"
    assert outside.read_bytes() == b"nope"


def test_system_tmp_symlink_reads_the_same_state_by_realpath():
    tmp = Path("/tmp")
    if not tmp.is_symlink():
        pytest.skip("/tmp is not a symlink on this host")
    name = "us-filings-tmp-" + uuid.uuid4().hex
    alias = tmp / name
    real_root = Path(os.path.realpath(tmp)) / name
    try:
        data = alias / "plugin-data" / "us-filings"
        save_state(data, empty_state())
        real_data = Path(os.path.realpath(data))
        assert os.path.realpath(data) == os.path.realpath(real_root / "plugin-data" / "us-filings")
        assert real_data != data
        left, left_state = load_state(data)
        right, right_state = load_state(real_data)
        assert left == right == "ok"
        assert left_state == right_state
        assert (real_data / "state.json").is_file()
        assert not (real_data / "state.json").is_symlink()
    finally:
        shutil.rmtree(alias, ignore_errors=True)
        shutil.rmtree(real_root, ignore_errors=True)


def test_read_missing_is_none_and_remove_missing_is_missing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert read_bytes(root, "state.json") is None
    assert remove_file(root, "state.json") == "missing"
    write_bytes(root, "scripts/us-filings-watch.py", b"print(1)\n", create=True)
    assert read_bytes(root, "scripts/us-filings-watch.py") == b"print(1)\n"
    assert remove_file(root, "scripts/us-filings-watch.py") == "removed"
    assert not (root / "scripts" / "us-filings-watch.py").exists()


def test_product_sources_do_not_read_the_contact_from_the_environment():
    root = Path(__file__).resolve().parents[1]
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert 'os.environ.get("US_FILINGS_CONTACT"' not in text
        assert "os.getenv(\"US_FILINGS_CONTACT\"" not in text
        assert "os.environ[\"US_FILINGS_CONTACT\"]" not in text
    client = (root / "client.py").read_text(encoding="utf-8")
    assert "get_secret(CONTACT_ENV)" in client
    assert "ProxyHandler({})" in client


def test_product_sources_do_not_fetch_code_at_runtime():
    root = Path(__file__).resolve().parents[1]
    manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
    assert "python_dependencies: []" in manifest
    files = list(root.glob("*.py")) + list((root / "skills").rglob("*.md"))
    files.append(root / "README.md")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in ("uvx", "npx", "pip install", "urlretrieve"):
            assert needle not in text, path.name
