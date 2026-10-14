"""`one-conv tune`: snapshot history, build datasets, train LoRA adapters and rank runs."""

import json
import os
import random
import shlex
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import click

from .corpus import AGENT_SOURCES, load_sessions
from .dataset import write_dataset
from .label import (
    LABELS,
    PROVIDERS,
    build_classifier,
    label_moments,
    load_labels,
    moments,
)
from .metrics import drift_table, rank_runs
from .rules import RULES
from .runs import (
    DEFAULT_MODEL,
    eval_metrics,
    load_runs,
    lora_command,
    mlx_env,
    score_command,
    tune_home,
)

HELP = {"help_option_names": ["-h", "--help"]}


def _execute(command: list[str], dry_run: bool) -> None:
    click.echo(shlex.join(command), err=True)
    if dry_run:
        return
    completed = subprocess.run(command, env=mlx_env(), check=False)
    if completed.returncode != 0:
        raise click.ClickException(f"Command failed with exit code {completed.returncode}.")


def _job(run_dir: Path, name: str, job: dict) -> Path:
    path = run_dir / f"{name}.job.json"
    path.write_text(json.dumps(job, indent=1) + "\n")
    return path


def _run_dir(home: Path, run: str) -> Path:
    path = home / "runs" / run
    if not (path / "train.json").exists():
        raise click.ClickException(f"No run named {run!r}. Train it first with: one-conv tune train --run {run}")
    return path


def build_tune_group(snapshot: Callable[[Path], tuple[int, int]],
                     corpus_roots: Callable[[], list[Path]]) -> click.Group:
    """`snapshot` exports this machine's sources; `corpus_roots` lists shared exports from other machines."""

    def _sessions(home, extra_roots=(), sources=(), include_system=False):
        roots = [home / "snapshot", *corpus_roots(), *extra_roots]
        roots = [root for root in roots if root.expanduser().is_dir()]
        if not roots:
            raise click.ClickException("No exported sessions found. Run: one-conv tune snapshot")
        return load_sessions(roots, tuple(sources) or AGENT_SOURCES, include_system)

    @click.group("tune", context_settings=HELP)
    def tune():
        """Fine-tune a local model on your own agent history.

        \b
        Pipeline:
          one-conv tune snapshot
          one-conv tune label
          one-conv tune dataset --name base --labels
          one-conv tune train --dataset base --run first
          one-conv tune eval --run first
          one-conv tune probe --run first --rule no-comments
          one-conv tune runs
        """

    @tune.command("snapshot", context_settings=HELP)
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def snapshot_command(as_json):
        """Export every plugged source on this machine into the tune workspace.

        \b
        Examples:
          one-conv tune snapshot
        """
        out = tune_home() / "snapshot"
        written, skipped = snapshot(out)
        if as_json:
            click.echo(json.dumps({"snapshot": str(out), "written": written, "skipped": skipped}))
            return
        click.echo(f"{written} session(s) written, {skipped} already current, under {out}")

    @tune.command(context_settings=HELP)
    @click.option("--name", default="base", show_default=True, help="Dataset folder name.")
    @click.option("--corpus", "extra_roots", multiple=True, type=click.Path(path_type=Path),
                  help="Additional export roots. Defaults: the snapshot plus the shared corpus.")
    @click.option("--source", "sources", multiple=True, type=click.Choice(AGENT_SOURCES),
                  help="Only these sources (repeatable). Default: every agent source.")
    @click.option("--rule", type=click.Choice(sorted(RULES)), default=None,
                  help="Rewrite accepted answers to obey this rule, without stating it.")
    @click.option("--valid", default=0.05, show_default=True, help="Share of sessions held out for validation.")
    @click.option("--test", default=0.05, show_default=True, help="Share of sessions held out for evaluation.")
    @click.option("--max-context-messages", default=16, show_default=True,
                  help="Messages of history kept before each target.")
    @click.option("--max-chars", default=12_000, show_default=True,
                  help="Characters per example, history included (~4 per token). Keep under train --max-seq-length.")
    @click.option("--include-system", is_flag=True, help="Keep heartbeat and worker sessions.")
    @click.option("--provider", type=click.Choice(sorted(PROVIDERS)), default="classifier", show_default=True,
                  help="Decision model that labeled (or labels) the moments.")
    @click.option("--labels/--keywords", "use_labels", default=False,
                  help="Use classifier labels from `tune label` instead of the keyword heuristic.")
    @click.option("--min-probability", default=0.6, show_default=True,
                  help="With --labels, ignore moments whose label probability is below this.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the manifest as JSON.")
    def dataset(name, extra_roots, sources, rule, valid, test, max_context_messages, max_chars, include_system,
                provider, use_labels, min_probability, as_json):
        """Build SFT examples and DPO pairs from exported sessions.

        Accepted assistant turns become SFT targets. A turn followed by a
        correction ("no", "revert", "pas ça", ...) becomes the rejected side
        of a DPO pair. Splits are per session, so evaluation never sees a
        training conversation.

        \b
        Examples:
          one-conv tune dataset --name base
          one-conv tune dataset --name no-comments --rule no-comments
        """
        home = tune_home()
        sessions = _sessions(home, extra_roots, sources, include_system)
        labels = None
        if use_labels:
            labels = load_labels(home / "labels" / f"{provider}.jsonl")
            if not labels:
                raise click.ClickException(f"No labels yet. Run: one-conv tune label --provider {provider}")
        out = home / "datasets" / name
        manifest = write_dataset(sessions, out, valid, test, RULES[rule] if rule else None,
                                 max_context_messages, max_chars, labels, min_probability)
        if as_json:
            click.echo(json.dumps({"dataset": str(out), **manifest}))
            return
        counts = manifest["counts"]
        click.echo(f"{manifest['sessions']} sessions → {out}")
        for kind in counts:
            click.echo(f"  {kind}: " + ", ".join(f"{split} {n}" for split, n in counts[kind].items()))
        if rule:
            click.echo(f"  rule {rule}: {manifest['rule_rewritten_targets']} target(s) rewritten")

    @tune.command(context_settings=HELP)
    @click.option("--provider", type=click.Choice(sorted(PROVIDERS)), default="classifier", show_default=True,
                  help="Decision model that labeled (or labels) the moments.")
    @click.option("--limit", default=0, show_default=True, help="Label at most this many new moments (0 = all).")
    @click.option("--workers", default=4, show_default=True, help="Concurrent classifier requests.")
    @click.option("--dry-run", is_flag=True, help="Count pending moments and estimate cost without calling the API.")
    def label(provider, limit, workers, dry_run):
        """Classify your reaction to every assistant reply with a decision model.

        Each moment (your request, the reply, your next message) gets one
        label: accepted, correction_preference, correction_new_info,
        new_task, question or unclear, with calibrated probabilities. Labels
        are cached by content, so reruns only classify new moments.
        classifier.dev needs no key and is asked not to keep the text; SubQ
        Snap needs SUBQ_API_KEY and Jev needs TYPESAFE_API_KEY.

        \b
        Examples:
          one-conv tune label --dry-run
          one-conv tune label --limit 50
          one-conv tune label --provider subq
          one-conv tune review
        """
        home = tune_home()
        spec = PROVIDERS[provider]
        out = home / "labels" / f"{provider}.jsonl"
        done = load_labels(out)
        pending, queued = [], set()
        for session in _sessions(home):
            for moment in moments(session):
                if moment.key not in done and moment.key not in queued:
                    queued.add(moment.key)
                    pending.append(moment)
        if limit:
            pending = pending[:limit]
        chars = sum(len(json.dumps(m.state)) for m in pending)
        # ~4 characters per token, plus the question; both providers bill input tokens only.
        tokens = chars // 4 + 400 * len(pending)
        cost = tokens * spec.usd_per_million / 1e6
        click.echo(f"{len(done)} labeled, {len(pending)} to label, ~{tokens / 1e6:.1f}M tokens (~${cost:.2f} on {provider})")
        if dry_run or not pending:
            return
        api_key = os.environ.get(spec.key_env) if spec.key_env else None
        if spec.key_env and not api_key:
            raise click.ClickException(f"Set {spec.key_env} ({spec.console}).")
        try:
            count = label_moments(pending, build_classifier(spec, api_key), out, workers, spec.batch_size)
        except RuntimeError as error:
            raise click.ClickException(f"{error}. Labels written so far are kept; rerun to resume.") from None
        click.echo(f"Labeled {count} moment(s) into {out}")

    @tune.command(context_settings=HELP)
    @click.option("--provider", type=click.Choice(sorted(PROVIDERS)), default="classifier", show_default=True,
                  help="Decision model that labeled (or labels) the moments.")
    @click.option("-n", "count", default=20, show_default=True, help="Moments to show.")
    @click.option("--label", "only", type=click.Choice(LABELS), default=None, help="Only this label.")
    @click.option("--seed", default=0, show_default=True)
    def review(provider, count, only, seed):
        """Show label counts and a random sample to check the classifier against your own judgment.

        \b
        Examples:
          one-conv tune review
          one-conv tune review --label correction_preference -n 30
        """
        home = tune_home()
        labels = load_labels(home / "labels" / f"{provider}.jsonl")
        if not labels:
            raise click.ClickException(f"No labels yet. Run: one-conv tune label --provider {provider}")
        totals = Counter(row["label"] for row in labels.values())
        click.echo("  ".join(f"{name}={totals.get(name, 0)}" for name in LABELS))
        states = {m.key: m.state for session in _sessions(home) for m in moments(session)}
        rows = [row for row in labels.values() if row["key"] in states and (only is None or row["label"] == only)]
        for row in random.Random(seed).sample(rows, min(count, len(rows))):
            state = states[row["key"]]
            probability = row["probabilities"][row["label"]]
            click.echo(f"\n[{row['label']} {probability:.2f}]")
            click.echo("  reply: " + state["assistant_reply"][:200].replace("\n", " "))
            click.echo("  you:   " + state["user_next_message"][:300].replace("\n", " "))

    @tune.command(context_settings=HELP)
    @click.option("--dataset", "dataset_name", default="base", show_default=True)
    @click.option("--run", "run", required=True, help="Run name; adapters land in runs/<run>/adapters.")
    @click.option("--model", default=DEFAULT_MODEL, show_default=True, help="MLX model repo or path.")
    @click.option("--iters", default=200, show_default=True)
    @click.option("--batch-size", default=1, show_default=True)
    @click.option("--num-layers", default=16, show_default=True, help="Blocks that get LoRA adapters; -1 for all.")
    @click.option("--learning-rate", default=1e-5, show_default=True)
    @click.option("--max-seq-length", default=4096, show_default=True)
    @click.option("--dry-run", is_flag=True, help="Print the training command without running it.")
    def train(dataset_name, run, model, iters, batch_size, num_layers, learning_rate, max_seq_length, dry_run):
        """Train a LoRA adapter on a dataset's SFT split with mlx-lm.

        The loss covers assistant targets only (--mask-prompt). DPO training
        is not wired yet; the DPO split is used by `eval` instead.

        \b
        Examples:
          one-conv tune train --dataset base --run first --dry-run
          one-conv tune train --dataset base --run first --iters 600
        """
        home = tune_home()
        data = home / "datasets" / dataset_name
        if not (data / "manifest.json").exists():
            raise click.ClickException(f"No dataset {dataset_name!r}. Build it with: one-conv tune dataset --name {dataset_name}")
        run_dir = home / "runs" / run
        command = lora_command(model, data, run_dir / "adapters", iters, batch_size, num_layers,
                               learning_rate, max_seq_length)
        if not dry_run:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "train.json").write_text(json.dumps({
                "dataset": dataset_name, "model": model, "iters": iters, "batch_size": batch_size,
                "num_layers": num_layers, "learning_rate": learning_rate, "max_seq_length": max_seq_length,
            }, indent=1) + "\n")
        _execute(command, dry_run)

    @tune.command("eval", context_settings=HELP)
    @click.option("--run", "run", required=True)
    @click.option("--limit", default=200, show_default=True, help="Held-out examples and pairs to score.")
    @click.option("--max-tokens", default=4096, show_default=True, help="Skip sequences longer than this.")
    @click.option("--dry-run", is_flag=True, help="Print the scoring command without running it.")
    def evaluate(run, limit, max_tokens, dry_run):
        """Score base vs adapter on held-out sessions.

        Reports test perplexity on your accepted answers (lower is better)
        and preference accuracy on correction pairs: how often the model
        ranks the answer you accepted above the one you corrected.

        \b
        Examples:
          one-conv tune eval --run first
        """
        home = tune_home()
        run_dir = _run_dir(home, run)
        config = json.loads((run_dir / "train.json").read_text())
        job = _job(run_dir, "eval", {"model": config["model"], "adapter": str(run_dir / "adapters"),
                                     "data": str(home / "datasets" / config["dataset"]),
                                     "limit": limit, "max_tokens": max_tokens})
        _execute(score_command("evaluate", job, run_dir / "eval.json"), dry_run)
        if not dry_run:
            for key, value in eval_metrics(json.loads((run_dir / "eval.json").read_text())).items():
                click.echo(f"{key}: {value:.4g}")

    @tune.command(context_settings=HELP)
    @click.option("--run", "run", required=True)
    @click.option("--rule", type=click.Choice(sorted(RULES)), required=True)
    @click.option("--lengths", default="0,2000,8000,16000", show_default=True,
                  help="Comma-separated context lengths in tokens of real filler history.")
    @click.option("--max-new-tokens", default=400, show_default=True)
    @click.option("--dry-run", is_flag=True, help="Print the probe command without running it.")
    def probe(run, rule, lengths, max_new_tokens, dry_run):
        """Measure how often a rule is broken as the context grows.

        Compares three variants on the rule's coding tasks, each preceded by
        real held-out history of increasing length: the base model, the base
        model with the rule as a system prompt, and the adapter with no
        instruction. A rule learned in the weights should stay flat while the
        prompt version drifts.

        \b
        Examples:
          one-conv tune probe --run no-comments --rule no-comments
          one-conv tune probe --run no-comments --rule no-comments --lengths 0,32000
        """
        home = tune_home()
        run_dir = _run_dir(home, run)
        config = json.loads((run_dir / "train.json").read_text())
        job = _job(run_dir, "probe", {"model": config["model"], "adapter": str(run_dir / "adapters"),
                                      "data": str(home / "datasets" / config["dataset"]), "rule": rule,
                                      "lengths": [int(part) for part in lengths.split(",")],
                                      "max_new_tokens": max_new_tokens})
        _execute(score_command("probe", job, run_dir / "probe.json"), dry_run)
        if not dry_run:
            table = drift_table(json.loads((run_dir / "probe.json").read_text())["rows"])
            for variant, rates in table.items():
                click.echo(f"{variant:8} " + "  ".join(f"@{length}: {rate:.0%}" for length, rate in rates.items()))

    @tune.command(context_settings=HELP)
    @click.option("--sort", "metric", default="adapter_pref_acc", show_default=True, help="Metric to rank by.")
    @click.option("--lower-is-better", is_flag=True, help="Rank ascending, e.g. for perplexity or violation rates.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def runs(metric, lower_is_better, as_json):
        """Rank trained runs by an evaluation metric.

        \b
        Examples:
          one-conv tune runs
          one-conv tune runs --sort adapter_test_ppl --lower-is-better
        """
        ranked = rank_runs(load_runs(tune_home()), metric, higher_is_better=not lower_is_better)
        if as_json:
            click.echo(json.dumps(ranked))
            return
        if not ranked:
            click.echo("No runs yet. Start with: one-conv tune train --run NAME")
            return
        for run in ranked:
            metrics = "  ".join(f"{key}={value:.3g}" for key, value in sorted(run["metrics"].items()))
            click.echo(f"{run['name']:20} {metrics or '(not evaluated)'}")

    return tune
