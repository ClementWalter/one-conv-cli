"""A channel's history at a fixed reading budget: the latest posts whole, older ones as one-line summaries.

Posts are numbered from 0 in the order they were exchanged. Aligned power-of-two ranges of them (blocks)
are summarized into one line each by the agents working in the channel: a block of LEAF posts from the
posts themselves, a larger block from the summaries of its two halves, so the summaries form a merge
tree over the history. Catch-up tiles the history with blocks that grow with their age, which keeps a
reader's view at about `budget` lines however long the channel gets; `open` descends a block into its
halves, down to the posts. Agents write the summaries on the way: a post whose arrival completes a
block comes back with the request to summarize it. The design follows OptMem's merge tree over an
append-only log (github.com/VictorTaelin/OptMem).
"""

import re
from collections.abc import Callable

# The smallest block that gets a summary; fewer posts than this are always read whole.
LEAF = 16
SUMMARY_CHARS = 400
# How much of each post a summary request quotes; the agent reads the channel for the rest.
EXCERPT_CHARS = 500
BLOCK = re.compile(r"#?(\d+)-(\d+)")


class SummaryError(ValueError):
    """A block or a summary that cannot be accepted as given."""


def label(lo: int, hi: int) -> str:
    """A block as readers see it: inclusive at both ends, like `16-31`."""
    return f"{lo}-{hi - 1}"


def parse_block(text: str) -> tuple[int, int]:
    """`lo-hi` as catch-up prints it, back to a half-open range; anything but a real block is refused."""
    match = BLOCK.fullmatch(text.strip())
    if not match:
        raise SummaryError(f"{text!r} is not a block; copy one as catch-up prints it, like 16-31")
    lo, hi = int(match.group(1)), int(match.group(2)) + 1
    size = hi - lo
    if size < LEAF or size & (size - 1) or lo % size:
        raise SummaryError(f"{text} is not a block: a block is {LEAF}, {2 * LEAF}, {4 * LEAF}... posts, "
                           "aligned on its size, like 16-31 or 0-63")
    return lo, hi


def halves(lo: int, hi: int) -> tuple[tuple[int, int], tuple[int, int]]:
    mid = (lo + hi) // 2
    return (lo, mid), (mid, hi)


def check(text: str) -> str:
    """One line of at most SUMMARY_CHARS characters."""
    text = " ".join(text.split())
    if not text:
        raise SummaryError("a summary is one line of text")
    if len(text) > SUMMARY_CHARS:
        raise SummaryError(f"{len(text)} characters; a summary is at most {SUMMARY_CHARS}. Compress it further")
    return text


def _tiles(total: int, alpha: float) -> list[tuple[int, int]]:
    """Aligned blocks over [0, total): a block stays whole when its size is at most `alpha` times its
    distance to the present, so blocks grow with age; a range under LEAF is read post by post."""
    root = 1
    while root < total:
        root *= 2
    out, stack = [], [(0, root)]
    while stack:
        lo, hi = stack.pop()
        if lo >= total:
            continue
        size = hi - lo
        if size > 1 and (hi > total or size < LEAF or size > alpha * (total - lo)):
            mid = (lo + hi) // 2
            stack.append((mid, hi))
            stack.append((lo, mid))
        else:
            out.append((lo, hi))
    return sorted(out)


def cover(total: int, budget: int) -> list[tuple[int, int]]:
    """The lines a reader gets for `total` posts: at most `budget` when the tree allows, finest at the end.

    Everything fits whole when it can. Otherwise the coarsest tiling has one block per set bit of
    `total` plus the trailing posts under LEAF, so a budget below that is exceeded rather than
    cut: a reader is never shown less than the whole shape of the channel.
    """
    if total <= 0:
        return []
    if total <= budget:
        return [(i, i + 1) for i in range(total)]
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if len(_tiles(total, mid)) > budget:
            lo = mid
        else:
            hi = mid
    out = _tiles(total, hi)
    # Sizes jump by powers of two, so alpha alone can leave lines unused; spend them on the newest
    # block, where detail is worth most, as long as its halves still fit.
    while True:
        index = max((i for i, (a, b) in enumerate(out) if b - a > 1), default=None)
        if index is None:
            break
        a, b = out[index]
        pieces = [(a, (a + b) // 2), ((a + b) // 2, b)] if b - a > LEAF else [(i, i + 1) for i in range(a, b)]
        if len(out) - 1 + len(pieces) > budget:
            break
        out[index:index + 1] = pieces
    return out


def layout(total: int, start: int, budget: int, settled: dict) -> tuple[list[dict], list[int]]:
    """Catch-up for a reader whose first unseen post is `start`: summary lines, and posts to show whole.

    Blocks that end before `start` were read already and are left out; a block that straddles it
    is shown, since one line costs less than the posts it stands for.
    """
    blocks, singles = [], []
    for lo, hi in cover(total, budget):
        if hi <= start:
            continue
        if hi - lo == 1:
            singles.append(lo)
        else:
            blocks.append({"block": label(lo, hi), "text": settled.get((lo, hi))})
    return blocks, singles


def pending(total: int, settled) -> list[tuple[int, int]]:
    """Blocks that can be summarized and are not, smallest first then oldest first.

    A block above LEAF waits for both of its halves, so every summary is written from settled text.
    """
    out, size = [], LEAF
    while size <= total:
        for k in range(total // size):
            lo, hi = k * size, (k + 1) * size
            if (lo, hi) in settled:
                continue
            if size == LEAF or all(half in settled for half in halves(lo, hi)):
                out.append((lo, hi))
        size *= 2
    return out


def excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS - 1] + "…"


def request(topic: str, lo: int, hi: int, settled: dict, posts: Callable[[int, int], list[dict]],
            remaining: int) -> dict:
    """What the channel asks to have summarized next, with the material and the exact command.

    `posts(lo, hi)` returns the kept posts in that range, each with `pos`, `from`, `at` and `text`;
    a LEAF block is summarized from them, a larger one from its halves' summaries.
    """
    how = f"the summarize tool, or `one-conv live summarize {topic} {label(lo, hi)} \"<line>\"`"
    out = {"topic": topic, "block": label(lo, hi), "limit": SUMMARY_CHARS, "remaining": remaining}
    if hi - lo == LEAF:
        rows = posts(lo, hi)
        out["posts"] = [{"pos": r["pos"], "from": r["from"], "at": r["at"], "text": excerpt(r["text"])}
                        for r in rows]
        # Posts older than the kept history are gone; the summary covers what is left.
        out["missing"] = hi - lo - len(rows)
    else:
        out["halves"] = [{"block": label(a, b), "text": settled.get((a, b))} for a, b in halves(lo, hi)]
    out["instruction"] = (
        f"Summarize posts #{label(lo, hi)} of #{topic} in one line of at most {SUMMARY_CHARS} characters: keep "
        "decisions, owners and anything with lasting effect, drop the rest, invent nothing. Record it with "
        f"{how}" + (f"; {remaining} more block(s) wait after it." if remaining else "."))
    return out


def opened(lo: int, hi: int, settled: dict, posts: Callable[[int, int], list[dict]]) -> dict:
    """One block descended a level: its two halves' summaries, or its posts when it is a LEAF block."""
    if hi - lo == LEAF:
        return {"block": label(lo, hi), "posts": posts(lo, hi)}
    return {"block": label(lo, hi),
            "halves": [{"block": label(a, b), "text": settled.get((a, b))} for a, b in halves(lo, hi)]}
