"""`one-conv tune`: snapshot history, build datasets, train LoRA adapters and rank runs."""

import json
import os
import random
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import click

from .corpus import AGENT_SOURCES, load_sessions
from .dataset import write_dataset
from .label import (
    JUDGE_PRICES,
    LABELS,
    PROVIDERS,
    adjudicated,
    build_classifier,
    label_moments,
    labels_name,
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
    mlx_python,
    score_command,
    tune_home,
)
from .synth import coding_prompts, load_answers, synthetic_examples

HELP = {"help_option_names": ["-h", "--help"]}


def _execute(command: list[str], dry_run: bool, log_path: Path | None = None) -> None:
    """Run an MLX command, mirroring its output into `log_path` so curves can be charted later."""
    click.echo(shlex.join(command), err=True)
    if dry_run:
        return
    if log_path is None:
        returncode = subprocess.run(command, env=mlx_env(), check=False).returncode
    else:
        with log_path.open("w", encoding="utf-8") as log, subprocess.Popen(
                command, env=mlx_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in process.stdout:
                sys.stderr.write(line)
                log.write(line)
                log.flush()
        returncode = process.returncode
    if returncode != 0:
        raise click.ClickException(f"Command failed with exit code {returncode}.")


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
    @click.option("--judge", default=None,
                  help="Labels file (e.g. boundless-gpt-6-luna) that overrides labels below --min-probability.")
    @click.option("--labels/--keywords", "use_labels", default=False,
                  help="Use classifier labels from `tune label` instead of the keyword heuristic.")
    @click.option("--min-probability", default=0.6, show_default=True,
                  help="With --labels, ignore moments whose label probability is below this.")
    @click.option("--synth", "synth_rule", type=click.Choice(sorted(RULES)), default=None,
                  help="Add synthetic rule-following examples from `tune synth` to the training split.")
    @click.option("--synth-ratio", default=1.0, show_default=True,
                  help="Synthetic rows per real SFT training row.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the manifest as JSON.")
    def dataset(name, extra_roots, sources, rule, valid, test, max_context_messages, max_chars, include_system,
                provider, judge, use_labels, min_probability, synth_rule, synth_ratio, as_json):
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
            if judge:
                verdicts = load_labels(home / "labels" / f"{judge}.jsonl")
                if not verdicts:
                    raise click.ClickException(f"No labels file named {judge!r} under {home / 'labels'}.")
                labels = adjudicated(labels, verdicts, min_probability)
        out = home / "datasets" / name
        synthetic = None
        if synth_rule:
            answers = load_answers(home / "synth" / synth_rule / "answers.jsonl")
            if not answers:
                raise click.ClickException(f"No synthetic answers yet. Run: one-conv tune synth --rule {synth_rule}")
            # Sized against the real training rows: build once without them to count, then mix.
            real = write_dataset(sessions, out, valid, test, RULES[rule] if rule else None,
                                 max_context_messages, max_chars, labels, min_probability)
            synthetic = synthetic_examples(answers, RULES[synth_rule], int(real["counts"]["sft"]["train"] * synth_ratio))
        manifest = write_dataset(sessions, out, valid, test, RULES[rule] if rule else None,
                                 max_context_messages, max_chars, labels, min_probability, synthetic)
        if as_json:
            click.echo(json.dumps({"dataset": str(out), **manifest}))
            return
        counts = manifest["counts"]
        click.echo(f"{manifest['sessions']} sessions → {out}")
        for kind in counts:
            click.echo(f"  {kind}: " + ", ".join(f"{split} {n}" for split, n in counts[kind].items()))
        if rule:
            click.echo(f"  rule {rule}: {manifest['rule_rewritten_targets']} target(s) rewritten")
        if synth_rule:
            click.echo(f"  synthetic {synth_rule}: {manifest['synthetic_train_rows']} training row(s) added")

    @tune.command(context_settings=HELP)
    @click.option("--provider", type=click.Choice(sorted(PROVIDERS)), default="classifier", show_default=True,
                  help="Decision model that labeled (or labels) the moments.")
    @click.option("--model", default=None, help="Judge model for --provider boundless (default gpt-6-luna).")
    @click.option("--uncertain-from", "uncertain_from", default=None,
                  help="Only moments this labels file (e.g. classifier) scored below --min-probability.")
    @click.option("--min-probability", default=0.6, show_default=True)
    @click.option("--sample", default=0, show_default=True, help="Label a random sample of this size (0 = all).")
    @click.option("--seed", default=0, show_default=True)
    @click.option("--limit", default=0, show_default=True, help="Label at most this many new moments (0 = all).")
    @click.option("--workers", default=4, show_default=True, help="Concurrent classifier requests.")
    @click.option("--dry-run", is_flag=True, help="Count pending moments and estimate cost without calling the API.")
    def label(provider, model, uncertain_from, min_probability, sample, seed, limit, workers, dry_run):
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
          one-conv tune label --provider boundless --uncertain-from classifier --model gpt-6-luna
          one-conv tune review
        """
        home = tune_home()
        spec = PROVIDERS[provider]
        model = model or spec.model
        out = home / "labels" / f"{labels_name(provider, model)}.jsonl"
        done = load_labels(out)
        uncertain = None
        if uncertain_from:
            primary = load_labels(home / "labels" / f"{uncertain_from}.jsonl")
            uncertain = {k for k, r in primary.items() if r["probabilities"].get(r["label"], 0.0) < min_probability}
        pending, queued = [], set()
        for session in _sessions(home):
            for moment in moments(session):
                if moment.key in done or moment.key in queued or (uncertain is not None and moment.key not in uncertain):
                    continue
                queued.add(moment.key)
                pending.append(moment)
        if sample:
            pending = random.Random(seed).sample(pending, min(sample, len(pending)))
        if limit:
            pending = pending[:limit]
        chars = sum(len(json.dumps(m.state)) for m in pending)
        # ~4 characters per token, plus the question; both providers bill input tokens only.
        tokens = chars // 4 + 400 * len(pending)
        price = JUDGE_PRICES.get(model, spec.usd_per_million) if provider == "boundless" else spec.usd_per_million
        cost = tokens * price / 1e6
        click.echo(f"{len(done)} labeled, {len(pending)} to label, ~{tokens / 1e6:.1f}M tokens "
                   f"(~${cost:.2f} on {labels_name(provider, model)})")
        if dry_run or not pending:
            return
        api_key = os.environ.get(spec.key_env) if spec.key_env else None
        if spec.key_env and not api_key:
            raise click.ClickException(f"Set {spec.key_env} ({spec.console}).")
        try:
            count = label_moments(pending, build_classifier(spec, api_key, model), out, workers, spec.batch_size)
        except RuntimeError as error:
            raise click.ClickException(f"{error}. Labels written so far are kept; rerun to resume.") from None
        click.echo(f"Labeled {count} moment(s) into {out}")

    @tune.command(context_settings=HELP)
    @click.argument("reference")
    @click.argument("candidates", nargs=-1, required=True)
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def compare(reference, candidates, as_json):
        """Agreement of labels files with a reference, on the moments both labeled.

        \b
        Examples:
          one-conv tune compare boundless-claude-opus-5.5 boundless-gpt-6-luna classifier
        """
        folder = tune_home() / "labels"
        ref = load_labels(folder / f"{reference}.jsonl")
        if not ref:
            raise click.ClickException(f"No labels file named {reference!r}.")
        rows = []
        for name in candidates:
            other = load_labels(folder / f"{name}.jsonl")
            shared = [k for k in ref if k in other]
            agree = sum(ref[k]["label"] == other[k]["label"] for k in shared)
            rows.append({"candidate": name, "shared": len(shared), "agreement": agree / len(shared) if shared else None})
        if as_json:
            click.echo(json.dumps({"reference": reference, "results": rows}))
            return
        for row in rows:
            score = f"{row['agreement']:.0%}" if row["agreement"] is not None else "n/a"
            click.echo(f"{row['candidate']:36} {score:>5} agreement on {row['shared']} moments")

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
    @click.option("--rule", type=click.Choice(sorted(RULES)), required=True)
    @click.option("--model", default=DEFAULT_MODEL, show_default=True, help="Generator; the base model keeps its style.")
    @click.option("--limit", default=2000, show_default=True, help="New answers to generate in this run.")
    @click.option("--batch-size", default=8, show_default=True, help="Prompts generated together.")
    @click.option("--max-new-tokens", default=512, show_default=True)
    @click.option("--valid", default=0.05, show_default=True, help="Must match the dataset's split shares.")
    @click.option("--test", default=0.05, show_default=True, help="Must match the dataset's split shares.")
    @click.option("--dry-run", is_flag=True, help="Write the prompt list and print the command without generating.")
    def synth(rule, model, limit, batch_size, max_new_tokens, valid, test, dry_run):
        """Generate rule-following coding answers by context distillation.

        Real coding requests from your training-split sessions are answered
        with the rule as a system prompt, then rewritten to obey it. Answers
        are appended, so reruns continue where the last one stopped. Add them
        to a dataset with `tune dataset --synth RULE`.

        \b
        Examples:
          one-conv tune synth --rule no-comments --dry-run
          one-conv tune synth --rule no-comments --limit 2000
          one-conv tune dataset --name nocom --labels --rule no-comments --synth no-comments
        """
        home = tune_home()
        folder = home / "synth" / rule
        folder.mkdir(parents=True, exist_ok=True)
        prompts = coding_prompts(_sessions(home), valid, test)
        (folder / "prompts.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in prompts))
        done = len(load_answers(folder / "answers.jsonl"))
        click.echo(f"{len(prompts)} coding prompts, {done} already answered")
        job = _job(folder, "synth", {"model": model, "rule": rule, "prompts": str(folder / "prompts.jsonl"),
                                     "out": str(folder / "answers.jsonl"), "limit": limit,
                                     "batch_size": batch_size, "max_new_tokens": max_new_tokens})
        _execute(mlx_python("one_conv.tune.score", "synthesize", "--job", str(job), "--out", str(folder / "synth.result.json")),
                 dry_run)

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
        _execute(command, dry_run, None if dry_run else run_dir / "train.log")

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
