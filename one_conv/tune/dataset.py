"""Build SFT examples and DPO preference pairs from saved sessions.

Every assistant turn becomes either an accepted SFT target, or, when the next
user turn corrects it, the rejected side of a DPO pair whose chosen side is the
assistant's following answer. Splits are assigned per session so no
conversation leaks between train and evaluation.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .corpus import Message, Session, merge_runs
from .label import CORRECTIONS, moment_key, moment_state
from .rules import Rule

log = logging.getLogger(__name__)

SPLITS = ("train", "valid", "test")

# Openers of a user turn that reject the previous answer, in English and French.
_CORRECTION_RE = re.compile(
    r"^\s*(no\b|nope\b|non\b|wrong\b|don'?t\b|do not\b|stop\b|revert\b|undo\b|actually\b|"
    r"instead\b|that'?s not\b|not what\b|i said\b|why did you\b|pas ça\b|pas comme ça\b|"
    r"c'?est pas\b|ce n'?est pas\b|arrête\b|annule\b|non,)",
    re.IGNORECASE,
)


def is_correction(text: str) -> bool:
    return bool(_CORRECTION_RE.match(text))


@dataclass(frozen=True)
class SFTExample:
    messages: tuple[Message, ...]


@dataclass(frozen=True)
class PreferencePair:
    prompt: tuple[Message, ...]
    chosen: str
    rejected: str
    kind: str = "correction"
    correction: str = ""


def _context(messages: list[Message], end: int, max_messages: int, budget: int) -> tuple[Message, ...]:
    """Most recent history before `end` that fits in `budget` characters, opening on a user turn."""
    window = messages[max(0, end - max_messages):end]
    # A target cut off by --max-seq-length leaves no unmasked tokens, so the whole example must fit.
    while window and sum(len(m.text) for m in window) > budget:
        window = window[1:]
    # Chat templates expect the conversation to open with a user turn.
    while window and window[0].role != "user":
        window = window[1:]
    return tuple(window)


# Moving on without complaint is an implicit acceptance.
ACCEPTING = ("accepted", "new_task")


def session_examples(session: Session, max_context_messages: int = 16, max_chars: int = 12_000,
                     labels: dict[str, dict] | None = None, min_probability: float = 0.6,
                     accepting: tuple[str, ...] = ACCEPTING) -> tuple[list[SFTExample], list[PreferencePair]]:
    """Without `labels`, a keyword heuristic spots corrections; with them, only confident labels count."""
    messages = merge_runs(session.messages)
    sft: list[SFTExample] = []
    pairs: list[PreferencePair] = []
    for index, message in enumerate(messages):
        if message.role != "assistant":
            continue
        follow = messages[index + 1] if index + 1 < len(messages) else None
        if labels is None:
            verdict = "correction" if follow is not None and is_correction(follow.text) else "accepted"
        elif follow is None or follow.role != "user" or index == 0:
            # A reply nobody answered carries no acceptance signal.
            continue
        else:
            row = labels.get(moment_key(moment_state(messages, index)))
            if row is None or row["probabilities"].get(row["label"], 0.0) < min_probability:
                continue
            verdict = row["label"]
        if verdict == "correction" or verdict in CORRECTIONS:
            retry = messages[index + 2] if index + 2 < len(messages) else None
            if retry is None or retry.role != "assistant":
                continue
            budget = max_chars - max(len(retry.text), len(message.text))
            context = _context(messages, index, max_context_messages, budget)
            if context:
                pairs.append(PreferencePair(context, chosen=retry.text, rejected=message.text,
                                            kind=verdict, correction=follow.text))
            continue
        if verdict not in ("accepted", *accepting):
            continue
        context = _context(messages, index, max_context_messages, max_chars - len(message.text))
        if context:
            sft.append(SFTExample(context + (message,)))
    return sft, pairs


def split_of(key: str, valid: float, test: float) -> str:
    """Deterministic split from the session key, stable across rebuilds."""
    bucket = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    if bucket < test:
        return "test"
    if bucket < test + valid:
        return "valid"
    return "train"


def _chat(messages: tuple[Message, ...]) -> list[dict]:
    return [{"role": m.role, "content": m.text} for m in messages]


def apply_rule(example: SFTExample, rule: Rule) -> SFTExample:
    *context, target = example.messages
    return SFTExample((*context, Message(target.role, rule.transform(target.text))))


def _first_time(line: str, seen: set[str]) -> bool:
    digest = hashlib.sha256(line.encode()).hexdigest()
    if digest in seen:
        return False
    seen.add(digest)
    return True


def write_dataset(sessions: list[Session], out_dir: Path, valid: float = 0.05, test: float = 0.05,
                  rule: Rule | None = None, max_context_messages: int = 16, max_chars: int = 12_000,
                  labels: dict[str, dict] | None = None, min_probability: float = 0.6,
                  synthetic: list[dict] | None = None) -> dict:
    """Write mlx-lm chat JSONL under sft/, preference pairs under dpo/, and return the manifest.

    Corrections that supplied facts the assistant could not know go to new_info/: the
    accepted answer depends on information absent from the prompt, so they are kept
    for evaluation and never used as preference training data.
    """
    folders = ("sft", "dpo", "new_info")
    counts = {kind: {split: 0 for split in SPLITS} for kind in folders}
    rewritten = duplicates = 0
    # The same conversation can be stored twice (archived copies, other machines' exports).
    seen: set[str] = set()
    handles = {}
    for kind in folders:
        (out_dir / kind).mkdir(parents=True, exist_ok=True)
        for split in SPLITS:
            handles[kind, split] = (out_dir / kind / f"{split}.jsonl").open("w", encoding="utf-8")
    try:
        for session in sessions:
            split = split_of(session.key, valid, test)
            sft, pairs = session_examples(session, max_context_messages, max_chars, labels, min_probability)
            for example in sft:
                if rule is not None:
                    transformed = apply_rule(example, rule)
                    rewritten += transformed != example
                    example = transformed
                line = json.dumps({"messages": _chat(example.messages)}, ensure_ascii=False)
                if _first_time(line, seen):
                    handles["sft", split].write(line + "\n")
                    counts["sft"][split] += 1
                else:
                    duplicates += 1
            for pair in pairs:
                line = json.dumps({"prompt": _chat(pair.prompt), "chosen": pair.chosen,
                                   "rejected": pair.rejected, "kind": pair.kind,
                                   "correction": pair.correction}, ensure_ascii=False)
                folder = "new_info" if pair.kind == "correction_new_info" else "dpo"
                if _first_time(line, seen):
                    handles[folder, split].write(line + "\n")
                    counts[folder][split] += 1
                else:
                    duplicates += 1
        # Synthetic rows only ever join training, so evaluation stays on real sessions.
        for row in synthetic or []:
            handles["sft", "train"].write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        for handle in handles.values():
            handle.close()
    manifest = {"sessions": len(sessions), "counts": counts, "rule": rule.name if rule else None,
                "rule_rewritten_targets": rewritten, "duplicates_skipped": duplicates,
                "synthetic_train_rows": len(synthetic or []), "valid": valid, "test": test,
                "max_context_messages": max_context_messages, "max_chars": max_chars,
                "labeled": labels is not None, "min_probability": min_probability if labels is not None else None}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    log.info("Wrote dataset to %s: %s", out_dir, counts)
    return manifest
