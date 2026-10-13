"""Classify how the user reacted to each assistant reply, with a calibrated decision model.

Every (assistant reply, next user message) moment gets one label. The labels
replace keyword heuristics when building datasets: only clearly accepted
replies become SFT targets, and corrections become preference pairs tagged
as anticipable (preference) or not (new information).
"""

import hashlib
import json
import logging
import random
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .corpus import Message, Session, merge_runs

log = logging.getLogger(__name__)

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
LABELS = ("accepted", "correction_preference", "correction_new_info", "new_task", "question", "unclear")
CORRECTIONS = ("correction_preference", "correction_new_info")

# Jev reads at most 32k tokens of state; these caps keep each moment well inside it.
_MAX_REQUEST_CHARS = 4_000
_MAX_REPLY_CHARS = 12_000
_MAX_NEXT_CHARS = 4_000

REACTION_QUESTION = {
    "type": "choice",
    "instructions": (
        "A developer is working with an AI coding assistant. How does `user_next_message` react to "
        "`assistant_reply`, given what the developer asked in `user_request`? Messages may be in English or French."
    ),
    "criteria": {
        "accepted": "Builds on the reply, thanks, or moves to the next step as if the reply were fine.",
        "correction_preference": ("Rejects or asks to change the reply because of approach, scope, style, "
                                  "conventions or taste that a careful assistant could have anticipated."),
        "correction_new_info": ("Rejects or asks to change the reply because of facts the assistant could not "
                                "have known: a new requirement, an external result, the state of their machine."),
        "new_task": "Starts an unrelated request without judging the reply.",
        "question": "Asks about the reply or requests an explanation without accepting or rejecting it.",
        "unclear": "Too short, automated, or ambiguous to tell.",
    },
}


def _clip(text: str, limit: int) -> str:
    """Keep both ends: openings carry intent and endings carry the final result."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n[…]\n" + text[-half:]


@dataclass(frozen=True)
class Moment:
    key: str
    session: str
    index: int
    state: dict


def moment_state(messages: list[Message], index: int) -> dict:
    """What the classifier sees for reply `index`: the request it served, the reply, the reaction."""
    request = next((m.text for m in reversed(messages[:index]) if m.role == "user"), "")
    return {"user_request": _clip(request, _MAX_REQUEST_CHARS),
            "assistant_reply": _clip(messages[index].text, _MAX_REPLY_CHARS),
            "user_next_message": _clip(messages[index + 1].text, _MAX_NEXT_CHARS)}


def moment_key(state: dict) -> str:
    # Content-addressed so a re-export of the same exchange reuses its label.
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:24]


def moments(session: Session) -> Iterator[Moment]:
    """Every assistant reply that the user answered."""
    messages = merge_runs(session.messages)
    for index in range(1, len(messages) - 1):
        if messages[index].role == "assistant" and messages[index + 1].role == "user":
            state = moment_state(messages, index)
            yield Moment(moment_key(state), session.key, index, state)


def load_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    labels = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            labels[row["key"]] = row
    return labels


Classifier = Callable[[dict], dict]


def jev_classifier(api_key: str, post: Callable | None = None, max_attempts: int = 6) -> Classifier:
    """Return a function mapping a moment's state to Jev's reaction answer."""
    if post is None:
        from curl_cffi import requests
        post = requests.post

    def classify(state: dict) -> dict:
        body = {"model": JEV_MODEL, "state": state, "questions": {"reaction": REACTION_QUESTION}}
        for attempt in range(max_attempts):
            response = post(JEV_URL, json=body, timeout=60, headers={"Authorization": f"Bearer {api_key}"})
            if response.status_code in (429, 529) and attempt + 1 < max_attempts:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else 2 ** attempt
                time.sleep(delay + random.random())
                continue
            if response.status_code != 200:
                detail = response.json().get("detail") if "json" in response.headers.get("content-type", "") else None
                raise RuntimeError(f"Jev returned HTTP {response.status_code}: {detail}")
            payload = response.json()
            answer = payload["answers"]["reaction"]
            return {"label": answer["choice"], "probabilities": answer["probabilities"],
                    "confidence": answer["confidence"], "model": payload["model"],
                    "input_tokens": payload["usage"]["input_tokens"]}
        raise RuntimeError("Jev stayed rate-limited")
    return classify


def label_moments(pending: list[Moment], classify: Classifier, out: Path, workers: int = 8) -> int:
    """Label moments concurrently, appending each result so an interrupted run resumes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    with out.open("a", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=workers) as pool:
        for moment, answer in zip(pending, pool.map(lambda m: classify(m.state), pending)):
            stream.write(json.dumps({"key": moment.key, "session": moment.session, "index": moment.index,
                                     **answer}, ensure_ascii=False) + "\n")
            stream.flush()
            done += 1
            if done % 200 == 0:
                log.info("Labeled %d/%d moments", done, len(pending))
    return done
