"""Workspace layout, MLX command lines and the run registry used to rank experiments.

    <home>/snapshot/          export of every plugged source on this machine
    <home>/datasets/<name>/   sft/ and dpo/ JSONL splits plus manifest.json
    <home>/runs/<name>/       train.json, adapters/, eval.json, probe.json
"""

import json
import os
from pathlib import Path

from .metrics import drift_table, perplexity, preference_accuracy

# Pinned so a run is reproducible; MLX stays out of the launcher's own dependencies.
MLX_LM = "mlx-lm==0.32.0"
DEFAULT_MODEL = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def tune_home() -> Path:
    return Path(os.environ.get("ONE_CONV_TUNE_DIR", str(Path.home() / ".cache" / "one-conv-cli" / "tune")))


def mlx_python(module: str, *args: str) -> list[str]:
    return ["uv", "run", "--no-project", "--with", MLX_LM, "python", "-m", module, *args]


def mlx_env() -> dict[str, str]:
    """Expose this checkout to the MLX interpreter so `one_conv.tune.score` imports."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PACKAGE_ROOT), env.get("PYTHONPATH")]))
    return env


def lora_command(model: str, data: Path, adapter_path: Path, iters: int, batch_size: int,
                 num_layers: int, learning_rate: float, max_seq_length: int) -> list[str]:
    return mlx_python(
        "mlx_lm", "lora", "--model", model, "--train", "--data", str(data / "sft"),
        "--adapter-path", str(adapter_path), "--mask-prompt", "--iters", str(iters),
        "--batch-size", str(batch_size), "--num-layers", str(num_layers),
        "--learning-rate", str(learning_rate), "--max-seq-length", str(max_seq_length),
        "--grad-checkpoint",
    )


def score_command(mode: str, job: Path, out: Path) -> list[str]:
    return mlx_python("one_conv.tune.score", mode, "--job", str(job), "--out", str(out))


def eval_metrics(result: dict) -> dict:
    """Flatten an `evaluate` result into comparable numbers per variant."""
    metrics = {}
    for variant, scores in result.items():
        if scores.get("test_mean_nll") is not None:
            metrics[f"{variant}_test_ppl"] = perplexity(scores["test_mean_nll"])
        if scores.get("preference_margins"):
            metrics[f"{variant}_pref_acc"] = preference_accuracy(scores["preference_margins"])
            metrics["pref_pairs"] = len(scores["preference_margins"])
    return metrics


def probe_metrics(result: dict) -> dict:
    """Violation rate at the longest probed context, where text prefixes are expected to fade."""
    table = drift_table(result["rows"])
    return {f"{variant}_violation@{max(rates)}": rates[max(rates)] for variant, rates in table.items()}


def load_runs(home: Path) -> list[dict]:
    runs = []
    for run_dir in sorted((home / "runs").glob("*/")):
        run = {"name": run_dir.name, "metrics": {}}
        for name, summarize in (("eval.json", eval_metrics), ("probe.json", probe_metrics)):
            path = run_dir / name
            if path.exists():
                run["metrics"].update(summarize(json.loads(path.read_text())))
        train = run_dir / "train.json"
        if train.exists():
            run["train"] = json.loads(train.read_text())
        runs.append(run)
    return runs
