"""The shared corpus is a first-class source: other machines and mouths show up in search."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

loader = importlib.machinery.SourceFileLoader("agent_conv", str(Path(__file__).parent.parent / "bin" / "one-conv"))
spec = importlib.util.spec_from_loader("agent_conv", loader)
ac = importlib.util.module_from_spec(spec)
sys.modules["agent_conv"] = ac
loader.exec_module(ac)


def _write_session(root: Path, machine: str, source: str, text: str, session: str = "brain") -> Path:
    path = root / machine / source / "brain" / f"{session}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "source": source,
        "session": session,
        "machine": machine,
        "cwd": "brain",
        "title": "brain",
        "author": "human",
        "mtime": 1,
        "started": "2026-09-06T10:00:00+00:00",
        "last": "2026-09-06T10:00:01+00:00",
        "turns": [{"ts": "2026-09-06T10:00:00+00:00", "role": "user", "text": text}],
    }, ensure_ascii=False) + "\n")
    return path


def test_this_machine_local_agent_session_is_skipped():
    assert ac._corpus_should_include({
        "source": "claude", "session": "abc", "machine": ac._THIS_MACHINE, "turns": [],
    }) is False


def test_this_machine_mouth_session_is_kept():
    assert ac._corpus_should_include({
        "source": "brain", "session": "brain", "machine": ac._THIS_MACHINE, "turns": [],
    }) is True


def test_other_machine_session_is_kept():
    assert ac._corpus_should_include({
        "source": "claude", "session": "abc", "machine": "other-box", "turns": [],
    }) is True


def test_search_reads_a_box_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONV_CORPUS", str(tmp_path))
    _write_session(tmp_path, "box", "brain", "cargo bike from the box")
    projects = list(ac._iter_all_projects(("corpus",)))
    assert any(p.cwd == "brain" for p in projects)
    project = next(p for p in projects if p.cwd == "brain")
    handles = ac._project_sessions(project)
    texts = [t.blocks[0]["text"] for h in handles for t in ac._handle_iter_turns(h, False)]
    assert "cargo bike from the box" in texts


def test_append_writes_the_brain_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONV_CORPUS", str(tmp_path))
    path = ac._append_log_turn("whatsapp", "user", "salut", machine="mac")
    doc = json.loads(path.read_text())
    assert doc["source"] == "brain"
    assert doc["turns"][-1]["text"] == "salut"
    assert doc["turns"][-1]["mouth"] == "whatsapp"


def test_canonical_corpus_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ONE_CONV_CORPUS", str(tmp_path))
    assert tmp_path in ac._corpus_roots()


def test_project_listing_loads_each_document_once(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from unittest.mock import Mock
    monkeypatch.setenv("ONE_CONV_CORPUS", str(tmp_path))
    _write_session(tmp_path, "box", "brain", "first", "one")
    second = _write_session(tmp_path, "box", "brain", "second", "two")
    document = json.loads(second.read_text())
    document["cwd"] = "second-project"
    second.write_text(json.dumps(document))
    load = Mock(wraps=ac._corpus_load)
    monkeypatch.setattr(ac, "_corpus_load", load)
    CliRunner().invoke(ac.cli, ["local", "chats", "--source", "corpus", "--json"])
    assert load.call_count == 2


def test_project_listing_reuses_the_saved_listing(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from unittest.mock import Mock
    monkeypatch.setenv("ONE_CONV_CORPUS", str(tmp_path / "corpus"))
    _write_session(tmp_path / "corpus", "box", "brain", "first", "one")
    CliRunner().invoke(ac.cli, ["local", "chats", "--source", "corpus", "--json"])
    load = Mock(wraps=ac._corpus_load)
    monkeypatch.setattr(ac, "_corpus_load", load)
    CliRunner().invoke(ac.cli, ["local", "chats", "--source", "corpus", "--json"])
    assert load.call_count == 0


def test_project_listing_rereads_a_changed_document(tmp_path, monkeypatch):
    from click.testing import CliRunner
    monkeypatch.setenv("ONE_CONV_CORPUS", str(tmp_path / "corpus"))
    path = _write_session(tmp_path / "corpus", "box", "brain", "first", "one")
    CliRunner().invoke(ac.cli, ["local", "chats", "--source", "corpus", "--json"])
    document = json.loads(path.read_text())
    document["cwd"] = "moved-project"
    path.write_text(json.dumps(document, indent=2))
    result = CliRunner().invoke(ac.cli, ["local", "chats", "--source", "corpus", "--json"])
    assert "moved-project" in result.output
