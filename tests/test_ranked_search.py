"""Ranked search: word matching in any order, BM25 order, and the CLI flag on local and archived history."""
import json
from pathlib import Path

import pytest

from one_conv.ranking import Ranking, snippet, words
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


@pytest.fixture
def ranking():
    ranking = Ranking(["bridge", "reconnect"])
    ranking.add({"id": "both-once"}, "The bridge will reconnect after the restart, then the hub sees it again later.")
    ranking.add({"id": "bridge-only"}, "The bridge restarted.")
    ranking.add({"id": "both-twice"}, "Bridge reconnect: the bridge did reconnect.")
    ranking.add({"id": "prefix"}, "The bridges reconnected.")
    return ranking


def test_ranking_drops_a_turn_missing_a_word(ranking):
    assert {"id": "bridge-only"} not in ranking.top(10)


def test_ranking_puts_the_densest_match_first(ranking):
    assert ranking.top(10)[0] == {"id": "both-twice"}


def test_ranking_matches_word_prefixes(ranking):
    assert {"id": "prefix"} in ranking.top(10)


def test_ranking_stops_at_the_limit(ranking):
    assert len(ranking.top(2)) == 2


def test_ranking_folds_accents():
    ranking = Ranking(["café"])
    ranking.add({"id": "plain"}, "a cafe nearby")
    assert ranking.top(1) == [{"id": "plain"}]


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
