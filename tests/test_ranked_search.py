"""Ranked search: the persistent word index, BM25 order, and the CLI flag on local and archived history."""
import json
from pathlib import Path

import pytest

from one_conv.ranking import SearchIndex, snippet, words
from tests.test_archive import archive, fixture_env, run  # noqa: F401 (archive is a fixture)


@pytest.mark.parametrize(("query", "expected"), [
    ("Reconnect the Bridge", ["reconnect", "the", "bridge"]),
    ("bridge, bridge!", ["bridge"]),
    ("Quartz_100% café", ["quartz_100", "café"]),
    ("?!", []),
])
def test_words(query, expected):
    assert words(query) == expected


def test_snippet_keeps_a_short_text_on_one_line():
    assert snippet("one\ntwo", ["two"]) == "one two"


def test_snippet_centres_a_long_text_on_the_first_needle():
    text = "a" * 300 + " bridge " + "b" * 300
    assert snippet(text, ["missing", "bridge"]) == "…" + text[221:421] + "…"


TEXTS = {"both-once": "The bridge will reconnect after the restart, then the hub sees it again later.",
         "bridge-only": "The bridge restarted.",
         "both-twice": "Bridge reconnect: the bridge did reconnect.",
         "prefix": "The bridges reconnected."}


@pytest.fixture
def index(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite")
    for key, text in TEXTS.items():
        index.replace(key, "claude", "v1", [("2026-01-01T10:00:00Z", "user", text)])
    return index


def keys(rows):
    return [row[0] for row in rows]


def test_index_drops_a_turn_missing_a_word(index):
    assert "bridge-only" not in keys(index.top(["bridge", "reconnect"], TEXTS, 10))


def test_index_puts_the_densest_match_first(index):
    assert keys(index.top(["bridge", "reconnect"], TEXTS, 10))[0] == "both-twice"


def test_index_matches_word_prefixes(index):
    assert "prefix" in keys(index.top(["bridge", "reconnect"], TEXTS, 10))


def test_index_stops_at_the_limit(index):
    assert len(index.top(["bridge", "reconnect"], TEXTS, 2)) == 2


def test_index_searches_only_sessions_in_scope(index):
    assert keys(index.top(["bridge", "reconnect"], ["prefix"], 10)) == ["prefix"]


def test_index_returns_the_turn_text(index):
    assert index.top(["bridges"], TEXTS, 10) == [("prefix", "2026-01-01T10:00:00Z", "user", "The bridges reconnected.")]


def test_index_folds_accents(index):
    index.replace("plain", "claude", "v1", [("", "user", "a cafe nearby")])
    assert keys(index.top(["café"], TEXTS.keys() | {"plain"}, 1)) == ["plain"]


def test_index_replaces_a_session_whole(index):
    index.replace("prefix", "claude", "v2", [("", "user", "Nothing left.")])
    assert "prefix" not in keys(index.top(["bridge", "reconnect"], TEXTS, 10))


def test_index_records_the_stamp_of_a_replaced_session(index):
    index.replace("prefix", "claude", "v2", [])
    assert index.stamps()["prefix"] == "v2"


def test_index_forgets_a_missing_session(index):
    index.forget(["claude"], TEXTS.keys() - {"prefix"})
    assert "prefix" not in index.stamps()


def test_index_keeps_missing_sessions_of_other_sources(index):
    index.forget(["codex"], set())
    assert index.stamps() == dict.fromkeys(TEXTS, "v1")


def test_index_persists_across_connections(index, tmp_path):
    assert SearchIndex(tmp_path / "index.sqlite").stamps() == dict.fromkeys(TEXTS, "v1")


def test_index_of_another_schema_version_is_rebuilt(index, tmp_path):
    index.db.execute("PRAGMA user_version=0")
    assert SearchIndex(tmp_path / "index.sqlite").stamps() == {}


def write_session(env, *texts):
    path = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture" / "ranked.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "cwd": "/example/ranked", "timestamp": f"2026-01-01T10:0{index}:00Z",
             "message": {"role": "user", "content": text}} for index, text in enumerate(texts)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


TURNS = ("The bridge restarted.", "Did the hub see the bridge reconnect?", "Unrelated message.")


def test_exact_search_misses_words_out_of_order(tmp_path):
    env = fixture_env(tmp_path)
    write_session(env, *TURNS)
    assert run(env, "bin/one-conv", "local", "search", "reconnect bridge", "--source", "claude", "--json") == []


def test_ranked_search_finds_words_out_of_order(tmp_path):
    env = fixture_env(tmp_path)
    write_session(env, *TURNS)
    hits = run(env, "bin/one-conv", "local", "search", "reconnect bridge", "--ranked", "--source", "claude", "--json")
    assert [hit["text"] for hit in hits] == ["Did the hub see the bridge reconnect?"]


def test_ranked_search_rejects_a_query_without_words(tmp_path):
    result = run(fixture_env(tmp_path), "bin/one-conv", "local", "search", "?!", "--ranked", "--json", success=False)
    assert "no word to rank" in result.stderr


def test_ranked_search_reads_archived_sessions(tmp_path, archive):  # noqa: F811
    _, _, url = archive
    env = {**fixture_env(tmp_path), "ONE_CONV_ARCHIVE_URL": url, "ONE_CONV_ARCHIVE_TOKEN": "test-secret"}
    write_session(env, *TURNS)
    run(env, "scripts/archive-sync.py", "--source", "claude")
    remote = {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    hits = run(remote, "bin/one-conv", "local", "search", "reconnect bridge", "--ranked", "--source", "claude", "--json")
    assert [hit["session"] for hit in hits] == ["ranked"]


def ranked(env, query="reconnect bridge"):
    return [hit["text"] for hit in run(env, "bin/one-conv", "local", "search", query, "--ranked", "--json")]


def test_ranked_search_reindexes_a_session_that_changed(tmp_path):
    env = fixture_env(tmp_path)
    write_session(env, *TURNS)
    ranked(env)
    write_session(env, "The bridge did reconnect at noon.")
    assert ranked(env) == ["The bridge did reconnect at noon."]


def test_ranked_search_keeps_its_index_in_the_state_directory(tmp_path):
    env = fixture_env(tmp_path)
    del env["ONE_CONV_SEARCH_INDEX"]
    write_session(env, *TURNS)
    ranked(env)
    assert (tmp_path / "state" / "search-index.sqlite").exists()


def test_ranked_search_forgets_a_deleted_session(tmp_path):
    env = fixture_env(tmp_path)
    write_session(env, *TURNS)
    ranked(env)
    (Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture" / "ranked.jsonl").unlink()
    ranked(env)
    assert SearchIndex(env["ONE_CONV_SEARCH_INDEX"]).stamps() == {}
