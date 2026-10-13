"""Behavior rules that can be trained into weights and probed for drift.

A rule pairs a dataset transform (rewrite accepted answers so they obey it,
without ever stating it) with a detector (does a generation break it?). The
same rule can then be given to the base model as a text instruction, which is
the baseline the adapter must beat over long contexts.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

# A generation cut by max_tokens can end inside an open fence; that code still counts.
_FENCE_RE = re.compile(r"(```[^\n]*\n)(.*?)(```|\Z)", re.DOTALL)
_WHOLE_LINE_COMMENT_RE = re.compile(r"^\s*(#(?!!)|//|--\s|/\*|\*\s|\*/)")
# Whitespace before the marker keeps URLs (`http://`) and `#` inside identifiers intact.
_TRAILING_COMMENT_RE = re.compile(r"\s+(#|//)\s.*$")


def code_blocks(text: str) -> list[str]:
    return [match.group(2) for match in _FENCE_RE.finditer(text)]


def comment_line_count(code: str) -> int:
    return sum(1 for line in code.splitlines()
               if _WHOLE_LINE_COMMENT_RE.match(line) or _TRAILING_COMMENT_RE.search(line))


def has_code_comments(text: str) -> bool:
    return any(comment_line_count(block) for block in code_blocks(text))


def _strip_block(code: str) -> str:
    kept = []
    for line in code.splitlines(keepends=True):
        if _WHOLE_LINE_COMMENT_RE.match(line):
            continue
        ending = "\n" if line.endswith("\n") else ""
        kept.append(_TRAILING_COMMENT_RE.sub("", line.rstrip("\n")) + ending)
    return "".join(kept)


def strip_code_comments(text: str) -> str:
    """Remove comments inside fenced code blocks; prose outside fences is untouched."""
    return _FENCE_RE.sub(lambda m: m.group(1) + _strip_block(m.group(2)) + m.group(3), text)


@dataclass(frozen=True)
class Rule:
    name: str
    instruction: str
    transform: Callable[[str], str]
    violates: Callable[[str], bool]
    # Coding tasks whose natural answer tempts the model to break the rule.
    tasks: tuple[str, ...]


RULES = {
    "no-comments": Rule(
        name="no-comments",
        instruction="Never write comments in code. No inline comments, no comment lines.",
        transform=strip_code_comments,
        violates=has_code_comments,
        tasks=(
            "Write a Python function that parses an ISO 8601 date string and returns a datetime.",
            "Write a TypeScript function that debounces another function.",
            "Write a Python class implementing an LRU cache with get and put.",
            "Write a Rust function that reads a file and counts word frequencies.",
            "Write a Python script that renames every .jpeg file in a folder to .jpg.",
        ),
    ),
}
