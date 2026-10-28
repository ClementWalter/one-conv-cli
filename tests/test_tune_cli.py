#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""The tune CLI wires workspace paths, datasets and MLX commands together."""

import json

import pytest
from click.testing import CliRunner

from one_conv.tune.cli import build_tune_group
from one_conv.tune.runs import MLX_LM, lora_command, probe_metrics


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_TUNE_DIR", str(tmp_path / "tune"))
    return tmp_path / "tune"


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "s1.json").write_text(json.dumps({
        "source": "codex", "session": "s1", "cwd": "/repo", "author": "human", "mtime": 1.0,
        "turns": [{"role": "user", "text": "Add a flag"}, {"role": "assistant", "text": "Added --flag"}]}))
    return root


@pytest.fixture
def tune(corpus):
    return build_tune_group(snapshot=lambda out: (3, 1), corpus_roots=lambda: [corpus])


def test_snapshot_reports_counts(home, tune):
    result = CliRunner().invoke(tune, ["snapshot", "--json"])
    assert json.loads(result.output)["written"] == 3


def test_dataset_writes_manifest(home, tune):
    CliRunner().invoke(tune, ["dataset", "--name", "d", "--valid", "0", "--test", "0"])
    assert json.loads((home / "datasets" / "d" / "manifest.json").read_text())["counts"]["sft"]["train"] == 1


def test_dataset_without_exports_fails(home):
    tune = build_tune_group(snapshot=lambda out: (0, 0), corpus_roots=list)
    result = CliRunner().invoke(tune, ["dataset"])
    assert "one-conv tune snapshot" in result.output


def test_train_dry_run_prints_mlx_command(home, tune):
    runner = CliRunner()
    runner.invoke(tune, ["dataset", "--name", "d"])
    result = runner.invoke(tune, ["train", "--dataset", "d", "--run", "r", "--dry-run"])
    assert "mlx_lm lora" in result.output


def test_train_dry_run_writes_nothing(home, tune):
    runner = CliRunner()
    runner.invoke(tune, ["dataset", "--name", "d"])
    runner.invoke(tune, ["train", "--dataset", "d", "--run", "r", "--dry-run"])
    assert not (home / "runs" / "r").exists()


def test_eval_requires_trained_run(home, tune):
    result = CliRunner().invoke(tune, ["eval", "--run", "missing"])
    assert "tune train --run missing" in result.output


def test_runs_ranks_by_metric(home, tune):
    for name, accuracy in (("low", 0.4), ("high", 0.8)):
        run = home / "runs" / name
        run.mkdir(parents=True)
        shifted = [1.0] * int(accuracy * 10) + [-1.0] * (10 - int(accuracy * 10))
        (run / "eval.json").write_text(json.dumps({"base": {"preference_margins": [0.0] * 10},
                                                   "adapter": {"preference_margins": shifted}}))
    result = CliRunner().invoke(tune, ["runs", "--json"])
    assert [run["name"] for run in json.loads(result.output)] == ["high", "low"]


def test_lora_command_masks_prompt(tmp_path):
    assert "--mask-prompt" in lora_command("m", tmp_path, tmp_path / "a", 1, 1, 16, 1e-5, 512)


def test_lora_command_pins_mlx_lm(tmp_path):
    assert MLX_LM in lora_command("m", tmp_path, tmp_path / "a", 1, 1, 16, 1e-5, 512)


def test_probe_metrics_reads_longest_context():
    rows = [{"variant": "prefix", "context_tokens": 0, "violated": False},
            {"variant": "prefix", "context_tokens": 16000, "violated": True}]
    assert probe_metrics({"rows": rows}) == {"prefix_violation@16000": 1.0}


def test_label_dry_run_counts_pending(home, tune):
    result = CliRunner().invoke(tune, ["label", "--dry-run"])
    assert result.output.startswith("0 labeled, 0 to label")


def test_label_requires_api_key(home, tmp_path, monkeypatch):
    monkeypatch.delenv("SUBQ_API_KEY", raising=False)
    root = tmp_path / "answered"
    root.mkdir()
    (root / "s.json").write_text(json.dumps({
        "source": "codex", "session": "s2", "cwd": "/repo", "author": "human", "mtime": 1.0,
        "turns": [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}, {"role": "user", "text": "c"}]}))
    tune = build_tune_group(snapshot=lambda out: (0, 0), corpus_roots=lambda: [root])
    result = CliRunner().invoke(tune, ["label", "--provider", "subq"])
    assert "SUBQ_API_KEY" in result.output


def test_dataset_with_labels_requires_labels(home, tune):
    result = CliRunner().invoke(tune, ["dataset", "--labels"])
    assert "one-conv tune label" in result.output


def _write_labels(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_compare_reports_agreement(home, tune):
    _write_labels(home / "labels" / "ref.jsonl", [{"key": "a", "label": "accepted", "probabilities": {}},
                                                  {"key": "b", "label": "question", "probabilities": {}}])
    _write_labels(home / "labels" / "cand.jsonl", [{"key": "a", "label": "accepted", "probabilities": {}},
                                                   {"key": "b", "label": "unclear", "probabilities": {}}])
    result = CliRunner().invoke(tune, ["compare", "ref", "cand", "--json"])
    assert json.loads(result.output)["results"][0]["agreement"] == 0.5


def test_dataset_judge_requires_its_labels(home, tune):
    _write_labels(home / "labels" / "classifier.jsonl", [{"key": "a", "label": "accepted", "probabilities": {"accepted": 1}}])
    result = CliRunner().invoke(tune, ["dataset", "--labels", "--judge", "missing"])
    assert "No labels file named 'missing'" in result.output


def test_annotate_saves_pressed_label(home, tmp_path):
    root = tmp_path / "answered"
    root.mkdir()
    (root / "s.json").write_text(json.dumps({
        "source": "codex", "session": "s9", "cwd": "/repo", "author": "human", "mtime": 1.0,
        "turns": [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}, {"role": "user", "text": "no, simpler"}]}))
    tune = build_tune_group(snapshot=lambda out: (0, 0), corpus_roots=lambda: [root])
    from one_conv.tune.corpus import load_sessions
    from one_conv.tune.label import moments
    key = next(moments(load_sessions([root])[0])).key
    _write_labels(home / "labels" / "classifier.jsonl", [{"key": key, "label": "question", "probabilities": {"question": 0.3}}])
    CliRunner().invoke(tune, ["annotate", "-n", "1"], input="3")
    assert json.loads((home / "labels" / "human.jsonl").read_text())["label"] == "correction_preference"


def test_nightly_dry_run_lists_training_step(home, tune):
    result = CliRunner().invoke(tune, ["cloud", "nightly", "--dry-run"])
    assert "cloud train --dataset nightly" in result.output


def test_runs_show_benchmark(home, tune):
    run = home / "runs" / "r"
    run.mkdir(parents=True)
    (run / "benchmark.json").write_text(json.dumps({"adapter": {"pass_at_1": 0.8}}))
    result = CliRunner().invoke(tune, ["runs", "--json"])
    assert json.loads(result.output)[0]["metrics"]["adapter_humaneval"] == 0.8
