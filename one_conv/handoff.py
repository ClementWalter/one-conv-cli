"""Budgeted handoff seeds: the opening prompt `local port` gives a new agent.

A conversation that fits the budget transfers whole. A longer one keeps its
first user request and its most recent turns, newest first until the budget is
spent, shown in chronological order; the seed names the omitted turns so the
new agent can read them on demand instead of paying for them up front.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ~16k tokens at ~4 characters per token: enough for the request and the
# latest working context, while leaving most of a 200k window for the work.
DEFAULT_BUDGET = 64_000
# The first request never takes more than this share of the budget, so the
# recent turns always get room.
FIRST_REQUEST_SHARE = 2
# Room reserved for the marker that says a turn was cut.
MARKER_RESERVE = 80


@dataclass(frozen=True)
class Handoff:
    """Which turns a seed carries, by 0-based index, and the turns it cuts.

    `head_cut` maps an index to characters dropped from the end of its text
    (the oversized first request); `tail_cut` to characters dropped from the
    start (the oversized newest turn).
    """

    total: int
    kept: tuple[int, ...]
    head_cut: dict[int, int] = field(default_factory=dict)
    tail_cut: dict[int, int] = field(default_factory=dict)

    @property
    def omitted(self) -> list[tuple[int, int]]:
        """Omitted turns as 1-based inclusive ranges, in order."""
        ranges: list[tuple[int, int]] = []
        kept = set(self.kept)
        for index in range(self.total):
            if index in kept:
                continue
            if ranges and ranges[-1][1] == index:
                ranges[-1] = (ranges[-1][0], index + 1)
            else:
                ranges.append((index + 1, index + 1))
        return ranges


def turn_label(index: int, total: int, turn: dict) -> str:
    """The line introducing a turn: its number, role and time, so omitted turns can be found by number or time."""
    stamp = f" · {turn['ts']}" if turn.get("ts") else ""
    return f"[turn {index + 1} of {total}{stamp}] {turn['role']}:"


def _entry_size(index: int, total: int, turn: dict) -> int:
    # Label, newline, text and the blank line separating entries.
    return len(turn_label(index, total, turn)) + len(turn["text"]) + 2


def select(turns: list[dict], budget: int, window: int = 0) -> Handoff:
    """Choose the turns a seed carries.

    `budget` caps the characters of the rendered turns (0 = no cap). `window`
    limits the recent turns to the last N (0 = any); the first user request
    is kept either way.
    """
    total = len(turns)
    first = next((index for index, turn in enumerate(turns) if turn["role"] == "user"), None)
    start = max(0, total - window) if window else 0
    sizes = [_entry_size(index, total, turn) for index, turn in enumerate(turns)]
    pinned = [first] if first is not None and first < start else []
    if not budget or sum(sizes[index] for index in (*pinned, *range(start, total))) <= budget:
        return Handoff(total, tuple(pinned + list(range(start, total))))

    head_cut: dict[int, int] = {}
    used = 0
    lowest = start
    if first is not None and first >= start:
        # The first request is pinned whole, so the recent turns start after it.
        pinned, lowest = [first], first + 1
    if pinned:
        cap = budget // FIRST_REQUEST_SHARE
        if sizes[first] > cap:
            head_cut[first] = min(len(turns[first]["text"]), sizes[first] - cap + MARKER_RESERVE)
        used = min(sizes[first], cap)

    recent: list[int] = []
    for index in range(total - 1, lowest - 1, -1):
        if used + sizes[index] > budget:
            break
        recent.append(index)
        used += sizes[index]
    tail_cut: dict[int, int] = {}
    if not recent and lowest < total:
        # Only the newest turn may be cut: its tail is the live state of the work.
        newest = total - 1
        tail_cut[newest] = min(len(turns[newest]["text"]), sizes[newest] - (budget - used) + MARKER_RESERVE)
        recent.append(newest)
    return Handoff(total, tuple(pinned + sorted(recent)), head_cut, tail_cut)


def render_turn(index: int, turns: list[dict], handoff: Handoff) -> str:
    """One turn's entry, with a marker where its text was cut."""
    turn = turns[index]
    text = turn["text"]
    if index in handoff.head_cut:
        cut = handoff.head_cut[index]
        text = f"{text[:len(text) - cut]}\n[… last {cut:,} characters of this turn omitted …]"
    if index in handoff.tail_cut:
        cut = handoff.tail_cut[index]
        text = f"[… first {cut:,} characters of this turn omitted …]\n{text[cut:]}"
    return f"{turn_label(index, handoff.total, turn)}\n{text}"


def describe(handoff: Handoff) -> str:
    """The sentence naming what the seed leaves out, or '' when it carries everything whole."""
    parts = []
    ranges = handoff.omitted
    if ranges:
        spans = ", ".join(f"{low}" if low == high else f"{low}–{high}" for low, high in ranges)
        count = sum(high - low + 1 for low, high in ranges)
        noun = "Turn" if count == 1 else "Turns"
        verb = "is" if count == 1 else "are"
        parts.append(f"{noun} {spans} of {handoff.total} {verb} omitted.")
    for index, cut in sorted({**handoff.head_cut, **handoff.tail_cut}.items()):
        where = "end" if index in handoff.head_cut else "start"
        parts.append(f"Turn {index + 1} is cut: {cut:,} characters omitted from its {where}.")
    return " ".join(parts)
