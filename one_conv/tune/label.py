"""Classify how the user reacted to each assistant reply, with a calibrated decision model.

Providers: classifier.dev (free, keyless, Jev underneath, batched), Jev, and
SubQ Snap through their System One decisions APIs.

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


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    model: str
    key_env: str | None
    usd_per_million: float
    console: str
    batch_size: int = 1


# Jev and SubQ serve the same System One decisions contract; classifier.dev batches plain texts.
PROVIDERS = {
    "classifier": Provider("classifier", "https://classifier.dev", "jev via classifier.dev", None,
                           0.0, "classifier.dev", batch_size=40),
    "jev": Provider("jev", "https://api.typesafe.ai/v1/systemone", "jev-latest", "TYPESAFE_API_KEY",
                    0.042, "console.typesafe.ai/settings/keys"),
    "subq": Provider("subq", "https://api.subq.ai/v1/systemone", "subq-snap-preview", "SUBQ_API_KEY",
                     0.05, "console.subq.ai"),
}
LABELS = ("accepted", "correction_preference", "correction_new_info", "new_task", "question", "unclear")
CORRECTIONS = ("correction_preference", "correction_new_info")

# Jev reads at most 32k tokens of state; these caps keep each moment inside it for every provider.
_MAX_REQUEST_CHARS = 4_000
_MAX_REPLY_CHARS = 12_000
_MAX_NEXT_CHARS = 4_000

# Definitions shared by every provider, tuned on hand-written cases including French and question-shaped pushback.
CRITERIA = {
    "accepted": "Thanks, approval, or a next step that builds on the reply as if it were fine.",
    "correction_preference": ("Rejects or asks to change the approach, scope, complexity, style, naming, conventions "
                              "or tooling: things a careful assistant could have anticipated. A doubt or suggestion "
                              "phrased as a question ('why do we need this?', 'can't we just use X?') counts here."),
    "correction_new_info": ("Supplies a fact the assistant had no way to know: a changed endpoint, an error output, "
                            "the state of their machine, a new requirement."),
    "new_task": "Starts an unrelated request without judging the reply.",
    "question": "A genuine request for explanation, with no pushback.",
    "unclear": "Too short, automated, or ambiguous to tell.",
}
FRAMING = ("A developer is working with an AI coding assistant; messages may be in English or French. "
           "Classify how the developer's next message reacts to the assistant's reply.")

REACTION_QUESTION = {
    "type": "choice",
    "instructions": FRAMING + " Use `user_request`, `assistant_reply` and `user_next_message`.",
    "criteria": CRITERIA,
}

# classifier.dev reads label names semantically, so each label is spelled out in words.
_WORDED = {"accepted": "accepted the reply",
           "correction_preference": "correction the assistant could have anticipated",
           "correction_new_info": "correction based on new information",
           "new_task": "new unrelated task",
           "question": "neutral question about the reply",
           "unclear": "unclear or automated"}
_UNWORDED = {words: label for label, words in _WORDED.items()}
_WORDED_INSTRUCTIONS = FRAMING + " " + " ".join(f"'{_WORDED[k]}': {v}" for k, v in CRITERIA.items())


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


# Maps a batch of moment states to one answer per state, in order.
Classifier = Callable[[list[dict]], list[dict]]


def _retry_delay(response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    return (float(retry_after) if retry_after else 2 ** attempt) + random.random()


def decision_classifier(provider: Provider, api_key: str, post: Callable | None = None,
                        max_attempts: int = 6) -> Classifier:
    """System One decisions API (Jev, SubQ): one request per moment."""
    if post is None:
        from curl_cffi import requests
        post = requests.post

    def classify(state: dict) -> dict:
        body = {"model": provider.model, "state": state, "questions": {"reaction": REACTION_QUESTION}}
        for attempt in range(max_attempts):
            response = post(provider.url, json=body, timeout=60, headers={"Authorization": f"Bearer {api_key}"})
            if response.status_code in (429, 529) and attempt + 1 < max_attempts:
                time.sleep(_retry_delay(response, attempt))
                continue
            if response.status_code != 200:
                detail = response.json().get("detail") if "json" in response.headers.get("content-type", "") else None
                raise RuntimeError(f"{provider.name} returned HTTP {response.status_code}: {detail}")
            payload = response.json()
            answer = payload["answers"]["reaction"]
            return {"label": answer["choice"], "probabilities": answer["probabilities"],
                    "confidence": answer["confidence"], "model": payload["model"],
                    "input_tokens": payload["usage"]["input_tokens"]}
        raise RuntimeError(f"{provider.name} stayed rate-limited")
    return lambda states: [classify(state) for state in states]


def _as_text(state: dict) -> str:
    return (f"USER REQUEST:\n{state['user_request']}\n\nASSISTANT REPLY:\n{state['assistant_reply']}"
            f"\n\nUSER NEXT MESSAGE:\n{state['user_next_message']}")


def classifier_dev_classifier(post: Callable | None = None, max_attempts: int = 6) -> Classifier:
    """classifier.dev batch API; asks it not to keep the text for training."""
    if post is None:
        from curl_cffi import requests
        post = requests.post

    def classify(states: list[dict]) -> list[dict]:
        body = {"labels": list(_WORDED.values()), "inputs": [_as_text(state) for state in states],
                "instructions": _WORDED_INSTRUCTIONS, "share_data": False}
        for attempt in range(max_attempts):
            response = post(PROVIDERS["classifier"].url, json=body, timeout=120,
                            headers={"X-Classifier-Share-Data": "false", "user-agent": "one-conv-tune/1"})
            if response.status_code in (429, 500, 502, 503, 529) and attempt + 1 < max_attempts:
                time.sleep(_retry_delay(response, attempt))
                continue
            if response.status_code != 200:
                raise RuntimeError(f"classifier.dev returned HTTP {response.status_code}: {response.text[:300]}")
            results = response.json()["results"]
            return [{"label": _UNWORDED[result["label"]],
                     "probabilities": {_UNWORDED[k]: v for k, v in (result.get("scores") or {}).items()},
                     "confidence": result.get("confidence"), "model": result.get("model")}
                    for result in results]
        raise RuntimeError("classifier.dev stayed rate-limited")
    return classify


def build_classifier(provider: Provider, api_key: str | None) -> Classifier:
    if provider.name == "classifier":
        return classifier_dev_classifier()
    return decision_classifier(provider, api_key)


def label_moments(pending: list[Moment], classify: Classifier, out: Path, workers: int = 8,
                  batch_size: int = 1) -> int:
    """Label moments concurrently in batches, appending each result so an interrupted run resumes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    done = 0
    with out.open("a", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=workers) as pool:
        for batch, answers in zip(batches, pool.map(lambda b: classify([m.state for m in b]), batches)):
            for moment, answer in zip(batch, answers):
                stream.write(json.dumps({"key": moment.key, "session": moment.session, "index": moment.index,
                                         **answer}, ensure_ascii=False) + "\n")
                done += 1
            stream.flush()
            log.info("Labeled %d/%d moments", done, len(pending))
    return done
