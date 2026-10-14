"""Synthesize rule-following coding examples by context distillation.

The model answers real coding requests from the user's own sessions with the
rule in its system prompt; the rule's transform then guarantees compliance.
Training on these answers without the system prompt moves the rule from the
context into the weights.
"""

import hashlib
import json
from pathlib import Path

from .corpus import Session, merge_runs
from .dataset import _context, split_of
from .rules import Rule, code_blocks

CODING_SOURCES = ("claude", "codex", "cursor", "cursor-cli", "omp", "codex-cloud")


def prompt_key(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:24]


def coding_prompts(sessions: list[Session], valid: float, test: float, max_messages: int = 8,
                   max_chars: int = 6_000) -> list[dict]:
    """Every user request of training-split coding sessions, with the conversation before it.

    The history keeps follow-ups answerable; it is capped in messages and
    characters so generation stays fast and the answer fits a training window.
    """
    seen: set[str] = set()
    prompts = []
    for session in sessions:
        if session.source not in CODING_SOURCES or split_of(session.key, valid, test) != "train":
            continue
        messages = merge_runs(session.messages)
        for index, message in enumerate(messages):
            if message.role != "user":
                continue
            window = _context(messages, index + 1, max_messages, max_chars)
            if not window or window[-1] is not message:
                continue
            chat = [{"role": m.role, "content": m.text} for m in window]
            key = prompt_key(chat)
            if key not in seen:
                seen.add(key)
                prompts.append({"key": key, "messages": chat})
    return prompts


def accepted_answer(answer: str, rule: Rule) -> str | None:
    """The rule-compliant answer, or None when it has no code to teach the rule with."""
    rewritten = rule.transform(answer)
    if not any(block.strip() for block in code_blocks(rewritten)) or rule.violates(rewritten):
        return None
    return rewritten


def load_answers(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def synthetic_examples(answers: list[dict], rule: Rule, limit: int) -> list[dict]:
    """Chat rows without the system prompt, so the behavior must come from the weights."""
    rows = []
    for row in answers:
        answer = accepted_answer(row["answer"], rule)
        if answer is not None:
            rows.append({"messages": [*row["messages"], {"role": "assistant", "content": answer}]})
        if len(rows) >= limit:
            break
    return rows
