"""Run tune's GPU-heavy steps on a Modal H100: synthetic generation, LoRA training, evaluation, drift probe.

Inputs and outputs live in the `one-conv-tune` volume with the same layout as the
local workspace (datasets/, synth/, runs/), so results pulled back feed the local
report and `tune runs` unchanged. Invoked by `one-conv tune cloud ...`.
"""

import json
import math
import time
from pathlib import Path

import modal

MODEL = "Qwen/Qwen2.5-Coder-7B-Instruct"
VOL = Path("/vol")
volume = modal.Volume.from_name("one-conv-tune", create_if_missing=True)
app = modal.App("one-conv-tune")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("vllm==0.31.0", "peft==0.21.2", "trl==1.15.0", "datasets==5.1.0")
    # The slim image has no nvcc, so vLLM must not JIT-compile FlashInfer's sampling kernels.
    .env({"HF_HOME": str(VOL / "hf"), "TOKENIZERS_PARALLELISM": "false", "VLLM_USE_FLASHINFER_SAMPLER": "0"})
    .add_local_python_source("one_conv")
)
GPU = {"image": image, "gpu": "H100", "volumes": {str(VOL): volume}}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))


@app.function(**GPU, timeout=3 * 3600)
def synthesize(rule: str, limit: int = 2000, max_new_tokens: int = 512, temperature: float = 0.3) -> dict:
    """Answer the uploaded prompts with the rule as system prompt; appends to answers.jsonl."""
    from vllm import LLM, SamplingParams

    from one_conv.tune.rules import RULES
    folder = VOL / "synth" / rule
    out = folder / "answers.jsonl"
    done = {row["key"] for row in _read_jsonl(out)}
    pending = [row for row in _read_jsonl(folder / "prompts.jsonl") if row["key"] not in done][:limit]
    llm = LLM(MODEL, max_model_len=8192, dtype="bfloat16")
    chats = [[{"role": "system", "content": RULES[rule].instruction}, *row["messages"]] for row in pending]
    outputs = llm.chat(chats, SamplingParams(temperature=temperature, max_tokens=max_new_tokens))
    with out.open("a", encoding="utf-8") as stream:
        for row, output in zip(pending, outputs):
            stream.write(json.dumps({**row, "answer": output.outputs[0].text}, ensure_ascii=False) + "\n")
    volume.commit()
    return {"written": len(pending), "already": len(done)}


def _prompt_completion(rows: list[dict]) -> list[dict]:
    # Prompt-completion rows make TRL compute the loss on the final reply only, like mlx-lm --mask-prompt.
    return [{"prompt": r["messages"][:-1], "completion": [r["messages"][-1]]} for r in rows]


@app.function(**GPU, timeout=4 * 3600)
def train(dataset: str, run: str, epochs: float = 1.0, learning_rate: float = 1e-4, rank: int = 16,
          max_length: int = 4096, batch_size: int = 1, grad_accum: int = 16, max_steps: int = -1) -> dict:
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import TrainerCallback
    from trl import SFTConfig, SFTTrainer

    data = VOL / "datasets" / dataset / "sft"
    run_dir = VOL / "runs" / run
    curves = {"train": [], "val": []}

    class Curves(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kwargs):
            if logs and "loss" in logs:
                curves["train"].append([state.global_step, logs["loss"]])
            if logs and "eval_loss" in logs:
                curves["val"].append([state.global_step, logs["eval_loss"]])

    # One sequence per forward pass: padded batches of mixed lengths made bf16 gradients NaN from step ~15.
    config = SFTConfig(
        output_dir=str(run_dir / "checkpoints"), num_train_epochs=epochs, learning_rate=learning_rate,
        per_device_train_batch_size=batch_size, per_device_eval_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum, max_length=max_length, bf16=True, logging_steps=5,
        eval_strategy="steps", eval_steps=50, save_strategy="no", report_to=[], lr_scheduler_type="cosine",
        warmup_steps=0.03, gradient_checkpointing=True, max_steps=max_steps, completion_only_loss=True,
        model_init_kwargs={"dtype": "bfloat16"},
    )
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(MODEL)

    def fitting(rows):
        # TRL truncates the end of long sequences, which would cut the reply being learned; drop them instead.
        kept = [r for r in rows if len(_token_ids(tokenizer, r["messages"])) <= max_length]
        return kept, len(rows) - len(kept)

    train_rows, dropped = fitting(_read_jsonl(data / "train.jsonl"))
    valid_rows, _ = fitting(_read_jsonl(data / "valid.jsonl"))
    trainer = SFTTrainer(
        model=MODEL, args=config,
        train_dataset=Dataset.from_list(_prompt_completion(train_rows)),
        eval_dataset=Dataset.from_list(_prompt_completion(valid_rows)),
        peft_config=LoraConfig(r=rank, lora_alpha=2 * rank, lora_dropout=0.05, target_modules="all-linear",
                               task_type="CAUSAL_LM"),
        callbacks=[Curves()],
    )
    started = time.time()
    trainer.train()
    trainer.evaluate()
    # The trainer may log the same evaluation more than once; keep one point per step.
    curves["val"] = sorted({step: loss for step, loss in curves["val"]}.items())
    trainer.model.save_pretrained(str(run_dir / "adapter"))
    settings = {"dataset": dataset, "model": MODEL, "backend": "modal-h100", "epochs": epochs,
                "learning_rate": learning_rate, "rank": rank, "max_length": max_length,
                "batch_size": batch_size * grad_accum, "steps": trainer.state.global_step,
                "train_rows": len(train_rows), "dropped_too_long": dropped,
                "seconds": round(time.time() - started)}
    _write_json(run_dir / "train.json", settings)
    _write_json(run_dir / "train_metrics.json", curves)
    volume.commit()
    return settings


def _token_ids(tokenizer, messages, **kwargs) -> list[int]:
    # transformers 5 returns a BatchEncoding from apply_chat_template; older versions a plain list.
    ids = tokenizer.apply_chat_template(messages, **kwargs)
    return ids if isinstance(ids, list) else ids["input_ids"]


def _completion_logprob(model, tokenizer, messages, completion, max_tokens):
    import torch
    prompt = _token_ids(tokenizer, messages, add_generation_prompt=True)
    full = _token_ids(tokenizer, messages + [{"role": "assistant", "content": completion}])
    if len(full) > max_tokens or len(full) <= len(prompt):
        return None
    ids = torch.tensor([full], device=model.device)
    with torch.no_grad():
        logits = model(ids[:, :-1]).logits.float()
    logp = torch.log_softmax(logits, dim=-1).gather(-1, ids[:, 1:, None]).squeeze(-1)[0, len(prompt) - 1:]
    return logp.sum().item(), logp.numel()


@app.function(**GPU, timeout=2 * 3600)
def evaluate(run: str, dataset: str, limit: int = 400, max_tokens: int = 4096) -> dict:
    """Held-out perplexity on accepted replies and preference margins, base vs adapter."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    data = VOL / "datasets" / dataset
    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    base = AutoModelForCausalLM.from_pretrained(MODEL, dtype=torch.bfloat16, device_map="cuda")
    model = PeftModel.from_pretrained(base, str(VOL / "runs" / run / "adapter"))
    sft = _read_jsonl(data / "sft" / "test.jsonl")[:limit]
    pairs = _read_jsonl(data / "dpo" / "test.jsonl")[:limit]
    result = {}
    for variant in ("base", "adapter"):
        def score(messages, completion, variant=variant):
            if variant == "base":
                with model.disable_adapter():
                    return _completion_logprob(model, tokenizer, messages, completion, max_tokens)
            return _completion_logprob(model, tokenizer, messages, completion, max_tokens)
        nll, tokens = 0.0, 0
        for row in sft:
            scored = score(row["messages"][:-1], row["messages"][-1]["content"])
            if scored:
                nll -= scored[0]
                tokens += scored[1]
        margins = []
        for row in pairs:
            chosen, rejected = score(row["prompt"], row["chosen"]), score(row["prompt"], row["rejected"])
            if chosen and rejected:
                margins.append(chosen[0] - rejected[0])
        result[variant] = {"test_tokens": tokens, "test_mean_nll": nll / tokens if tokens else None,
                           "preference_margins": margins}
    _write_json(VOL / "runs" / run / "eval.json", result)
    volume.commit()
    return {v: {"ppl": math.exp(r["test_mean_nll"]) if r["test_mean_nll"] else None,
                "pref_acc": sum(m > 0 for m in r["preference_margins"]) / max(1, len(r["preference_margins"]))}
            for v, r in result.items()}


@app.function(**GPU, timeout=2 * 3600)
def probe(run: str, dataset: str, rule: str, lengths: str = "0,2000,8000,16000", samples: int = 4,
          max_new_tokens: int = 400) -> dict:
    """Rule violations vs prior-context length for base, base + system prompt, and adapter."""
    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams
    from vllm.lora.request import LoRARequest

    from one_conv.tune.rules import RULES
    spec = RULES[rule]
    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    filler_rows = _read_jsonl(VOL / "datasets" / dataset / "sft" / "test.jsonl")
    llm = LLM(MODEL, max_model_len=24576, dtype="bfloat16", enable_lora=True, max_lora_rank=64)
    adapter = LoRARequest("run", 1, str(VOL / "runs" / run / "adapter"))
    params = SamplingParams(temperature=0.7, max_tokens=max_new_tokens, n=samples, seed=0)
    rows = []
    for length in [int(x) for x in lengths.split(",")]:
        filler = []
        if length:
            for row in filler_rows:
                filler.extend(row["messages"])
                if len(_token_ids(tokenizer, filler)) >= length:
                    break
        while filler and filler[-1]["role"] != "assistant":
            filler.pop()
        for variant, with_instruction, lora in (("base", False, None), ("prefix", True, None), ("adapter", False, adapter)):
            chats = [([{"role": "system", "content": spec.instruction}] if with_instruction else []) + filler
                     + [{"role": "user", "content": task}] for task in spec.tasks]
            for task, output in zip(spec.tasks, llm.chat(chats, params, lora_request=lora)):
                for sample in output.outputs:
                    rows.append({"variant": variant, "context_tokens": length, "task": task,
                                 "violated": spec.violates(sample.text), "output": sample.text})
    _write_json(VOL / "runs" / run / "probe.json", {"rule": rule, "rows": rows})
    volume.commit()
    summary = {}
    for row in rows:
        summary.setdefault(row["variant"], {}).setdefault(row["context_tokens"], []).append(row["violated"])
    return {v: {k: sum(f) / len(f) for k, f in d.items()} for v, d in summary.items()}


def _extract_code(text: str, prompt: str, entry_point: str) -> str:
    """The first fenced code block, completed with the given signature if the model omitted it."""
    fence = text.find("```")
    if fence != -1:
        start = text.find("\n", fence) + 1
        end = text.find("```", start)
        text = text[start:end if end != -1 else None]
    return text if f"def {entry_point}" in text else prompt + text


def _passes(program: str, timeout: int = 15) -> bool:
    import subprocess
    import sys
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(program)
    try:
        return subprocess.run([sys.executable, handle.name], capture_output=True, timeout=timeout, check=False).returncode == 0
    except subprocess.TimeoutExpired:
        return False


@app.function(**GPU, timeout=3600)
def benchmark(run: str) -> dict:
    """HumanEval pass@1 (greedy) for base vs adapter: does fitting the user cost general coding ability?"""
    from concurrent.futures import ThreadPoolExecutor

    from datasets import load_dataset
    from vllm import LLM, SamplingParams
    from vllm.lora.request import LoRARequest

    problems = list(load_dataset("openai/openai_humaneval", split="test"))
    llm = LLM(MODEL, max_model_len=4096, dtype="bfloat16", enable_lora=True, max_lora_rank=64)
    params = SamplingParams(temperature=0.0, max_tokens=768)
    chats = [[{"role": "user", "content": "Complete this Python function. Reply with the whole function in one "
                                          "```python code block.\n\n" + p["prompt"]}] for p in problems]
    result = {}
    for variant, lora in (("base", None), ("adapter", LoRARequest("run", 1, str(VOL / "runs" / run / "adapter")))):
        outputs = llm.chat(chats, params, lora_request=lora)
        programs = [_extract_code(o.outputs[0].text, p["prompt"], p["entry_point"]) + "\n\n" + p["test"]
                    + f"\n\ncheck({p['entry_point']})\n" for p, o in zip(problems, outputs)]
        with ThreadPoolExecutor(16) as pool:
            passed = list(pool.map(_passes, programs))
        result[variant] = {"passed": sum(passed), "total": len(passed), "pass_at_1": sum(passed) / len(passed),
                           "failed": [p["task_id"] for p, ok in zip(problems, passed) if not ok]}
    _write_json(VOL / "runs" / run / "benchmark.json", result)
    volume.commit()
    return {v: f"{r['passed']}/{r['total']}" for v, r in result.items()}


@app.function(**GPU, timeout=3600)
def diagnose(dataset: str, micro_batches: int = 64, batch_size: int = 4, max_length: int = 4096) -> list[dict]:
    """Replay training's first micro-batches one example at a time; report examples with non-finite loss or grads."""
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from trl import SFTConfig, SFTTrainer

    rows = _read_jsonl(VOL / "datasets" / dataset / "sft" / "train.jsonl")
    config = SFTConfig(output_dir="/tmp/diag", per_device_train_batch_size=batch_size, max_length=max_length,
                       bf16=True, report_to=[], completion_only_loss=True, gradient_checkpointing=True,
                       model_init_kwargs={"dtype": "bfloat16"})
    trainer = SFTTrainer(model=MODEL, args=config, train_dataset=Dataset.from_list(_prompt_completion(rows)),
                         peft_config=LoraConfig(r=16, lora_alpha=32, target_modules="all-linear", task_type="CAUSAL_LM"))
    model = trainer.model
    model.train()
    found = []
    for b, batch in enumerate(trainer.get_train_dataloader()):
        if b >= micro_batches:
            break
        for i in range(batch["input_ids"].shape[0]):
            one = {k: v[i:i + 1].to(model.device) for k, v in batch.items() if hasattr(v, "shape")}
            labels = one["input_ids"].clone()
            if "completion_mask" in one:
                labels[one["completion_mask"] == 0] = -100
            if "attention_mask" in one:
                labels[one["attention_mask"] == 0] = -100
            out = model(input_ids=one["input_ids"], attention_mask=one.get("attention_mask"), labels=labels)
            out.loss.backward()
            bad_grad = any(p.grad is not None and not torch.isfinite(p.grad).all() for p in model.parameters())
            model.zero_grad(set_to_none=True)
            if bad_grad or not torch.isfinite(out.loss):
                ids = one["input_ids"][0][one["attention_mask"][0].bool()] if "attention_mask" in one else one["input_ids"][0]
                found.append({"micro_batch": b, "row": i, "loss": float(out.loss), "tokens": int(ids.numel()),
                              "completion_tokens": int((labels != -100).sum()),
                              "tail": trainer.processing_class.decode(ids[-200:])})
    _write_json(VOL / "diagnose.json", {"micro_batches": micro_batches, "found": found})
    volume.commit()
    return found
