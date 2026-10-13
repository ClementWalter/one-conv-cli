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
        (run / "eval.json").write_text(json.dumps({"adapter": {"preference_margins": [1.0] * int(accuracy * 10) + [-1.0] * (10 - int(accuracy * 10))}}))
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
