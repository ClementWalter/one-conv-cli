#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
#     "mcp==2.2.0",
#     "websockets==17.2",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
#     "httpx==0.28.1",
#     "numpy==2.4.6",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Semantic search: the local static embedding model, vectors in the index, fused ranking, and the CLI flag."""
import hashlib
import http.server
import json
import struct
import threading
from functools import partial
from pathlib import Path

import numpy as np
import pytest

from one_conv import semantic
from one_conv.ranking import SearchIndex
from tests.test_archive import archive, fixture_env, run  # noqa: F401 (archive is a fixture)

# Each token's vector is one axis: "car" and "automobile" share theirs, so they mean the same thing.
VOCAB = {"[PAD]": 0, "[UNK]": 1, "car": 2, "automobile": 3, "##s": 4, "bridge": 5, "reconnect": 6,
         "dog": 7, "barked": 8, "the": 9, "!": 10, ",": 11}
AXES = {"car": 0, "automobile": 0, "##s": 0, "bridge": 1, "reconnect": 1, "dog": 2, "barked": 2, "the": 3}


def write_model(directory):
    directory.mkdir(parents=True, exist_ok=True)
    tokenizer = {"model": {"type": "WordPiece", "unk_token": "[UNK]", "continuing_subword_prefix": "##",
                           "max_input_chars_per_word": 20, "vocab": VOCAB}}
    (directory / "tokenizer.json").write_text(json.dumps(tokenizer))
    embeddings = np.zeros((len(VOCAB), 4), dtype=np.float32)
    for token, axis in AXES.items():
        embeddings[VOCAB[token], axis] = 1.0
    embeddings[VOCAB["the"]] *= 0.1
    header = json.dumps({"embeddings": {"dtype": "F32", "shape": list(embeddings.shape),
                                        "data_offsets": [0, embeddings.nbytes]}}).encode()
    (directory / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + embeddings.tobytes())
    return directory


@pytest.fixture
def model_dir(tmp_path):
    return write_model(tmp_path / "model")


@pytest.fixture
def model(model_dir):
    return semantic.StaticModel(model_dir)


@pytest.fixture
def tokenizer(model_dir):
    return semantic.WordPiece(model_dir / "tokenizer.json")


def test_normalize_lowercases_and_strips_accents():
    assert semantic._normalize("Café NAÏVE") == "cafe naive"


def test_normalize_turns_whitespace_into_spaces_and_drops_controls():
    assert semantic._normalize("a\tb\x00c\u200bd") == "a bcd"


def test_normalize_isolates_cjk_characters():
    assert semantic._normalize("ab東") == "ab 東 "


def test_words_isolate_punctuation():
    assert list(semantic._words("bridge, reconnect!")) == ["bridge", ",", "reconnect", "!"]


def test_wordpiece_splits_a_word_into_continuing_pieces(tokenizer):
    assert tokenizer.word("cars") == [VOCAB["car"], VOCAB["##s"]]


def test_wordpiece_maps_a_word_without_a_full_split_to_unknown(tokenizer):
    assert tokenizer.word("carz") == [VOCAB["[UNK]"]]


def test_wordpiece_maps_a_too_long_word_to_unknown(tokenizer):
    assert tokenizer.word("car" * 7) == [VOCAB["[UNK]"]]


def test_wordpiece_encode_stops_at_the_limit(tokenizer):
    assert tokenizer.encode("dog dog dog dog", limit=3) == [VOCAB["dog"]] * 3


def test_model_vectors_are_unit_length(model):
    assert np.linalg.norm(model.encode(["The dog barked, the car"])[0]) == pytest.approx(1.0)


def test_model_gives_a_text_without_known_tokens_a_zero_vector(model):
    assert not model.encode(["unheard words"])[0].any()


def test_model_ignores_unknown_tokens_in_the_mean(model):
    assert model.encode(["car carz"])[0] == pytest.approx(model.encode(["car"])[0])


def test_model_places_synonyms_together(model):
    car, automobile = model.encode(["the car", "the automobile"])
    assert float(car @ automobile) == pytest.approx(1.0)


@pytest.fixture
def served_model(tmp_path, monkeypatch, model_dir):
    """The model files over local HTTP, with their checksums pinned."""
    monkeypatch.setattr(semantic, "FILES", {name: hashlib.sha256((model_dir / name).read_bytes()).hexdigest()
                                            for name in ("tokenizer.json", "model.safetensors")})
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(model_dir))
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_fetch_downloads_the_pinned_files(tmp_path, served_model):
    directory = semantic.fetch(tmp_path / "fetched", served_model)
    assert semantic.StaticModel(directory).dim == 4


def test_fetch_refuses_a_file_that_does_not_match_its_checksum(tmp_path, served_model, monkeypatch):
    monkeypatch.setitem(semantic.FILES, "tokenizer.json", "0" * 64)
    with pytest.raises(OSError, match="checksum"):
        semantic.fetch(tmp_path / "fetched", served_model)


def test_fetch_leaves_no_file_behind_after_a_checksum_mismatch(tmp_path, served_model, monkeypatch):
    monkeypatch.setitem(semantic.FILES, "tokenizer.json", "0" * 64)
    with pytest.raises(OSError):
        semantic.fetch(tmp_path / "fetched", served_model)
    assert list((tmp_path / "fetched").iterdir()) == []


def test_fetch_keeps_files_already_present(tmp_path):
    directory = tmp_path / "present"
    directory.mkdir()
    for name in semantic.FILES:
        (directory / name).write_text("kept")
    semantic.fetch(directory, "http://127.0.0.1:9")
    assert (directory / "tokenizer.json").read_text() == "kept"


TEXTS = {"car": "The car would not start.", "bridge": "The bridge did reconnect.", "dog": "The dog barked."}


@pytest.fixture
def index(tmp_path, model):
    index = SearchIndex(tmp_path / "index.sqlite")
    for key, text in TEXTS.items():
        index.replace(key, "claude", "v1", [("2026-01-01T10:00:00Z", "user", text)])
    pending = index.unembedded(TEXTS)
    index.embed([turn for turn, _ in pending], model.encode([text for _, text in pending]))
    return index


def keys(rows):
    return [row[0] for row in rows]


def test_hybrid_finds_a_turn_sharing_no_word_with_the_query(index, model):
    assert keys(index.hybrid(["automobile"], model.encode(["automobile"])[0], TEXTS, 1)) == ["car"]


def test_hybrid_finds_a_turn_by_its_words_without_a_vector_match(index, model):
    assert keys(index.hybrid(["dog"], np.zeros(4, dtype=np.float32), TEXTS, 1)) == ["dog"]


def test_hybrid_puts_a_turn_matching_words_and_meaning_first(index, model):
    assert keys(index.hybrid(["dog", "automobile"], model.encode(["dog"])[0], TEXTS, 3))[0] == "dog"


def test_hybrid_searches_only_sessions_in_scope(index, model):
    assert keys(index.hybrid(["automobile"], model.encode(["automobile"])[0], ["bridge"], 3)) == []


def test_hybrid_returns_the_turn_text(index, model):
    assert index.hybrid([], model.encode(["automobile"])[0], TEXTS, 1) == [
        ("car", "2026-01-01T10:00:00Z", "user", "The car would not start.")]


def test_index_has_no_turn_left_to_embed_after_embedding(index):
    assert index.unembedded(TEXTS) == []


def test_index_embeds_again_a_replaced_session(index):
    index.replace("car", "claude", "v2", [("", "user", "The automobile.")])
    assert [text for _, text in index.unembedded(TEXTS)] == ["The automobile."]


def write_session(env, *texts):
    path = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture" / "semantic.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "cwd": "/example/semantic", "timestamp": f"2026-01-01T10:0{index}:00Z",
             "message": {"role": "user", "content": text}} for index, text in enumerate(texts)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


@pytest.fixture
def env(tmp_path):
    return {**fixture_env(tmp_path), "ONE_CONV_SEMANTIC_MODEL": str(write_model(tmp_path / "cli-model"))}


def semantic_hits(env, query="automobile", *extra):
    return [hit["text"] for hit in run(env, "one_conv_cli.py", "local", "search", query, "--semantic",
                                       "--limit", "1", "--json", *extra)]


def test_semantic_search_finds_a_turn_sharing_no_word(env):
    write_session(env, *TEXTS.values())
    assert semantic_hits(env) == ["The car would not start."]


def test_semantic_search_embeds_a_session_that_changed(env):
    write_session(env, *TEXTS.values())
    semantic_hits(env)
    write_session(env, "Cars everywhere.", "The dog barked.")
    assert semantic_hits(env) == ["Cars everywhere."]


def test_ranked_search_still_needs_every_word(env):
    write_session(env, *TEXTS.values())
    assert run(env, "one_conv_cli.py", "local", "search", "automobile", "--ranked", "--json") == []


def test_semantic_search_reads_archived_sessions_holding_any_word(tmp_path, env, archive):  # noqa: F811
    _, _, url = archive
    env = {**env, "ONE_CONV_ARCHIVE_URL": url, "ONE_CONV_ARCHIVE_TOKEN": "test-secret"}
    write_session(env, *TEXTS.values())
    run(env, "scripts/archive-sync.py", "--source", "claude")
    remote = {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    assert semantic_hits(remote, "automobile car", "--source", "claude") == ["The car would not start."]
