"""Ranking metrics for tuning runs."""

import math

import pytest

from one_conv.tune.metrics import (
    drift_table,
    perplexity,
    preference_shift,
    rank_runs,
    violation_rate,
)


def test_preference_shift_counts_margins_that_grew():
    assert preference_shift([5.0, 5.0, -3.0, 0.0], [6.0, 4.0, -1.0, 0.0]) == 0.5


def test_preference_shift_ignores_length_bias():
    # Both models prefer the shorter reply everywhere; only the change in margin counts.
    assert preference_shift([100.0, 100.0], [90.0, 90.0]) == 0.0


def test_preference_shift_needs_matching_pairs():
    with pytest.raises(ValueError):
        preference_shift([1.0], [1.0, 2.0])


def test_perplexity_is_exp_of_nll():
    assert perplexity(math.log(4.0)) == pytest.approx(4.0)


def test_violation_rate():
    assert violation_rate([True, False, False, False]) == 0.25


def test_drift_table_groups_by_variant_and_length():
    rows = [{"variant": "prefix", "context_tokens": 0, "violated": False},
            {"variant": "prefix", "context_tokens": 8000, "violated": True},
            {"variant": "prefix", "context_tokens": 8000, "violated": False}]
    assert drift_table(rows) == {"prefix": {0: 0.0, 8000: 0.5}}


def test_rank_runs_descending():
    runs = [{"name": "a", "metrics": {"m": 0.2}}, {"name": "b", "metrics": {"m": 0.9}}]
    assert [run["name"] for run in rank_runs(runs, "m")] == ["b", "a"]


def test_rank_runs_ascending():
    runs = [{"name": "a", "metrics": {"m": 0.2}}, {"name": "b", "metrics": {"m": 0.9}}]
    assert [run["name"] for run in rank_runs(runs, "m", higher_is_better=False)] == ["a", "b"]


def test_rank_runs_puts_unscored_last():
    runs = [{"name": "new", "metrics": {}}, {"name": "a", "metrics": {"m": 0.2}}]
    assert [run["name"] for run in rank_runs(runs, "m")] == ["a", "new"]
