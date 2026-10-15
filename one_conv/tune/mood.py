"""Rate the tone of each user reaction and rank sessions by how frustrated the user got."""

from .label import _clip, classifier_dev_classifier

MOODS = ("calm", "impatient", "frustrated", "angry")
_WORDED = {"calm": "calm or neutral", "impatient": "mildly impatient", "frustrated": "clearly frustrated",
           "angry": "angry"}
_INSTRUCTIONS = (
    "Each text shows an AI coding assistant's reply and the developer's next message (English or French). "
    "Rate the developer's emotional tone toward the assistant in the next message only. "
    "'calm or neutral': plain requests, thanks, neutral corrections. "
    "'mildly impatient': curt or insistent pushback, 'I said…', 'again'. "
    "'clearly frustrated': repeated failure called out, exasperation, sarcasm, 'still not working', 'why did you…'. "
    "'angry': shouting in capitals, swearing, insults, threats to stop using the assistant."
)


def _as_text(state: dict) -> str:
    return (f"ASSISTANT REPLY:\n{_clip(state['assistant_reply'], 1_500)}\n\n"
            f"DEVELOPER NEXT MESSAGE:\n{state['user_next_message']}")


def mood_classifier(post=None):
    return classifier_dev_classifier(post=post, worded=_WORDED, instructions=_INSTRUCTIONS, as_text=_as_text)


def intensity(row: dict) -> float:
    """Expected level from 0 (calm) to 3 (angry), using the full probability spread."""
    probabilities = row.get("probabilities") or {}
    if not probabilities:
        return float(MOODS.index(row["label"])) if row.get("label") in MOODS else 0.0
    return sum(MOODS.index(mood) * p for mood, p in probabilities.items() if mood in MOODS)


def session_heat(levels: list[float]) -> dict:
    """A session's peak and how many of its reactions were frustrated or worse."""
    return {"peak": max(levels, default=0.0), "heated": sum(1 for level in levels if level >= 1.5),
            "reactions": len(levels)}
