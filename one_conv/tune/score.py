"""MLX scoring runtime: held-out likelihoods, preference accuracy and rule-drift probes.

This module runs inside an `uv run --with mlx-lm` environment launched by the
tune CLI, so the launcher itself never depends on MLX. It reads a JSON job
description and writes a JSON result.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .rules import RULES

log =logging.getLogger("one-conv.tune.score")


def _load(model: str, adapter: str | None):
    from mlx_lm import load
    return load(model, adapter_path=adapter)


def _read_jsonl(path: Path, limit: int) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
            if len(rows) >= limit:
                break
    return rows


def completion_logprob(model, tokenizer, messages: list[dict], completion: str,
                       max_tokens: int) -> tuple[float, int] | None:
    """Sum of log p(completion tokens | messages), or None when the sequence is too long."""
    import mlx.core as mx
    prompt_ids = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
    full_ids = tokenizer.apply_chat_template(messages + [{"role": "assistant", "content": completion}])
    if len(full_ids) > max_tokens or len(full_ids) <= len(prompt_ids):
        return None
    ids = mx.array(full_ids)[None]
    logits = model(ids[:, :-1]).astype(mx.float32)
    logp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
    token_logp = mx.take_along_axis(logp, ids[:, 1:, None], axis=-1).squeeze(-1)
    # Position k predicts token k+1, so the completion starts at len(prompt) - 1.
    completion_logp = token_logp[0, len(prompt_ids) - 1:]
    return completion_logp.sum().item(), completion_logp.size


def evaluate(job: dict) -> dict:
    data = Path(job["data"])
    sft_rows = _read_jsonl(data / "sft" / "test.jsonl", job.get("limit", 200))
    dpo_rows = _read_jsonl(data / "dpo" / "test.jsonl", job.get("limit", 200))
    result = {}
    for variant, adapter in (("base", None), ("adapter", job.get("adapter"))):
        if variant == "adapter" and not adapter:
            continue
        model, tokenizer = _load(job["model"], adapter)
        total_nll, total_tokens = 0.0, 0
        for row in sft_rows:
            *context, target = row["messages"]
            scored = completion_logprob(model, tokenizer, context, target["content"], job["max_tokens"])
            if scored is not None:
                total_nll -= scored[0]
                total_tokens += scored[1]
        margins = []
        for row in dpo_rows:
            chosen = completion_logprob(model, tokenizer, row["prompt"], row["chosen"], job["max_tokens"])
            rejected = completion_logprob(model, tokenizer, row["prompt"], row["rejected"], job["max_tokens"])
            if chosen is not None and rejected is not None:
                margins.append(chosen[0] - rejected[0])
        result[variant] = {"test_tokens": total_tokens,
                           "test_mean_nll": total_nll / total_tokens if total_tokens else None,
                           "preference_margins": margins}
    return result


def _filler(tokenizer, rows: list[dict], target_tokens: int) -> list[dict]:
    """Real held-out conversation, cut to roughly `target_tokens`, ending on an assistant turn."""
    messages: list[dict] = []
    if target_tokens > 0:
        for row in rows:
            messages.extend(row["messages"])
            if len(tokenizer.apply_chat_template(messages)) >= target_tokens:
                break
    while messages and messages[-1]["role"] != "assistant":
        messages.pop()
    return messages


def probe(job: dict) -> dict:
    from mlx_lm import generate
    rule = RULES[job["rule"]]
    filler_rows = _read_jsonl(Path(job["data"]) / "sft" / "test.jsonl", 10_000)
    rows = []
    variants = [("base", None, False), ("prefix", None, True)]
    if job.get("adapter"):
        variants.append(("adapter", job["adapter"], False))
    for variant, adapter, with_instruction in variants:
        model, tokenizer = _load(job["model"], adapter)
        for length in job["lengths"]:
            filler = _filler(tokenizer, filler_rows, length)
            for task in rule.tasks:
                messages = ([{"role": "system", "content": rule.instruction}] if with_instruction else [])
                messages += filler + [{"role": "user", "content": task}]
                prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
                text = generate(model, tokenizer, prompt=prompt, max_tokens=job["max_new_tokens"])
                rows.append({"variant": variant, "context_tokens": length, "prompt_tokens": len(prompt),
                             "task": task, "violated": rule.violates(text), "output": text})
                log.info("%s @%d tokens: violated=%s", variant, length, rows[-1]["violated"])
    return {"rule": rule.name, "rows": rows}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("evaluate", "probe"))
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    job = json.loads(args.job.read_text())
    result = evaluate(job) if args.mode == "evaluate" else probe(job)
    args.out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
