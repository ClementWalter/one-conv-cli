"""The no-comments rule rewrites and detects comments inside code fences only."""

import pytest

from one_conv.tune.rules import (
    RULES,
    comment_line_count,
    has_code_comments,
    strip_code_comments,
)

FENCED = "Here:\n```python\n# setup\nx = 1  # one\ny = 2\n```\nDone # not code"


@pytest.mark.parametrize("code, expected", [
    ("# a\nx = 1\n", 1),
    ("x = 1  # one\n", 1),
    ("// js\nlet a = 1;\n", 1),
    ("#!/usr/bin/env python\nx = 1\n", 0),
    ('url = "http://example.com"\n', 0),
    ("x = 1\n", 0),
])
def test_comment_line_count(code, expected):
    assert comment_line_count(code) == expected


def test_strip_removes_whole_and_trailing_comments():
    assert strip_code_comments(FENCED) == "Here:\n```python\nx = 1\ny = 2\n```\nDone # not code"


def test_strip_leaves_text_without_fences():
    assert strip_code_comments("no code # here") == "no code # here"


def test_detects_comments_in_fence():
    assert has_code_comments(FENCED)


def test_prose_hash_is_not_a_violation():
    assert not has_code_comments("Issue #12 is fixed.")


def test_stripped_output_obeys_rule():
    assert not RULES["no-comments"].violates(strip_code_comments(FENCED))


def test_detects_comments_in_unclosed_fence():
    assert has_code_comments("```python\nclass A:\n    # cache\n    pass\n")
