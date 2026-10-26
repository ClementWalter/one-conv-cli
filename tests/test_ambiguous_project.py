"""An ambiguous local PROJECT query reports its candidates in the caller's output format."""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def app(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("ambiguous_cli", str(Path(__file__).parents[1] / "bin/one-conv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    return module


@pytest.fixture
def two_projects(app, monkeypatch):
    """Two directories that both contain `stack` and neither is named `stack`."""
    projects = [
        app.ProjectHandle(source="claude", cwd="/work/cdefi-stack", key="a"),
        app.ProjectHandle(source="codex", cwd="/work/other-stack", key="b"),
    ]
    monkeypatch.setattr(app, "_archive_sync_in_background", lambda: None)
    monkeypatch.setattr(app, "_iter_all_projects", lambda sources: iter(projects))
    monkeypatch.setattr(app, "_project_sessions", lambda project: [])
    monkeypatch.setattr(app, "_load_read_state", dict)
    return projects


@pytest.fixture
def read_json(app, two_projects):
    return CliRunner().invoke(app.cli, ["local", "read", "stack", "--json"])


@pytest.fixture
def read_text(app, two_projects):
    return CliRunner().invoke(app.cli, ["local", "read", "stack"])


def test_read_json_ambiguity_exits_nonzero(read_json):
    assert read_json.exit_code == 1


def test_read_json_ambiguity_is_a_stdout_object(read_json):
    assert json.loads(read_json.stdout) == {
        "error": "ambiguous",
        "query": "stack",
        "total": 2,
        "matches": [
            {"n": 1, "source": "claude", "cwd": "/work/cdefi-stack", "threads": 0},
            {"n": 2, "source": "codex", "cwd": "/work/other-stack", "threads": 0},
        ],
        "hint": "re-run with --match N",
    }


def test_read_json_ambiguity_leaves_stderr_empty(read_json):
    assert read_json.stderr == ""


def test_thread_json_ambiguity_is_a_stdout_object(app, two_projects):
    result = CliRunner().invoke(app.cli, ["local", "thread", "stack", "--json"])
    assert json.loads(result.stdout)["error"] == "ambiguous"


def test_read_text_ambiguity_exits_nonzero(read_text):
    assert read_text.exit_code == 1


def test_read_text_ambiguity_leaves_stdout_empty(read_text):
    assert read_text.stdout == ""


def test_read_text_ambiguity_names_match_flag_on_stderr(read_text):
    assert "pick with --match N" in read_text.stderr


def test_read_json_with_match_resolves_one_project(app, two_projects):
    result = CliRunner().invoke(app.cli, ["local", "read", "stack", "--json", "--match", "2"])
    assert json.loads(result.stdout) == []
