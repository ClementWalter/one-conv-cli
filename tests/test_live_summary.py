"""The reading budget over a channel: tiling, which blocks wait for a summary, and what a request carries."""

import pytest

from one_conv.live import summary
from one_conv.live.summary import LEAF, cover, layout, pending

TOTALS = [1, 15, 16, 17, 21, 36, 100, 300, 1000, 65_537]


def test_cover_shows_everything_whole_when_it_fits():
    assert cover(20, 20) == [(i, i + 1) for i in range(20)]


def test_cover_of_nothing_is_empty():
    assert cover(0, 20) == []


def test_cover_stays_within_its_budget():
    assert len(cover(300, 20)) <= 20


def test_cover_keeps_the_newest_post_whole():
    assert cover(300, 20)[-1] == (299, 300)


def test_cover_folds_the_oldest_posts_into_the_largest_block():
    assert cover(300, 20)[0] == (0, 128)


@pytest.mark.parametrize("total", TOTALS)
def test_cover_tiles_the_whole_history_in_order(total):
    tiles = cover(total, 20)
    assert [lo for lo, _ in tiles] == [0] + [hi for _, hi in tiles[:-1]] and tiles[-1][1] == total


@pytest.mark.parametrize("total", TOTALS)
def test_cover_blocks_are_aligned_on_their_size(total):
    assert all(lo % (hi - lo) == 0 for lo, hi in cover(total, 20))


@pytest.mark.parametrize("total", TOTALS)
def test_cover_never_makes_a_block_under_leaf(total):
    assert all(hi - lo == 1 or hi - lo >= LEAF for lo, hi in cover(total, 20))


def test_cover_spends_spare_lines_on_the_newest_block():
    # 48 posts in 20 lines: two blocks plus 16 posts beats three blocks.
    assert cover(48, 20) == [(0, 16), (16, 32)] + [(i, i + 1) for i in range(32, 48)]


def test_pending_starts_with_leaf_blocks_oldest_first():
    assert pending(40, set()) == [(0, 16), (16, 32)]


def test_pending_waits_for_both_halves():
    assert pending(32, {(0, 16)}) == [(16, 32)]


def test_pending_lists_a_parent_once_its_halves_settled():
    assert pending(32, {(0, 16), (16, 32)}) == [(0, 32)]


def test_pending_is_empty_when_the_tree_is_complete():
    assert pending(32, {(0, 16), (16, 32), (0, 32)}) == []


def test_layout_leaves_out_blocks_read_already():
    blocks, _ = layout(50, 20, 20, {})
    assert [b["block"] for b in blocks] == ["16-31"]


def test_layout_shows_the_posts_after_the_last_block_whole():
    _, singles = layout(50, 20, 20, {})
    assert singles == list(range(32, 50))


def test_layout_carries_the_settled_text():
    blocks, _ = layout(25, 0, 20, {(0, 16): "agreed on the plan"})
    assert blocks == [{"block": "0-15", "text": "agreed on the plan"}]


def test_parse_block_reads_a_catch_up_label():
    assert summary.parse_block("16-31") == (16, 32)


def test_parse_block_accepts_the_hash_prefix():
    assert summary.parse_block("#0-63") == (0, 64)


@pytest.mark.parametrize("text", ["8-23", "0-7", "16-47", "x", "3"])
def test_parse_block_refuses_what_is_not_a_block(text):
    with pytest.raises(summary.SummaryError):
        summary.parse_block(text)


def test_check_folds_a_summary_onto_one_line():
    assert summary.check("  agreed\n  to ship ") == "agreed to ship"


def test_check_refuses_a_long_summary():
    with pytest.raises(summary.SummaryError, match="at most"):
        summary.check("x" * (summary.SUMMARY_CHARS + 1))


def posts(lo, hi):
    return [{"pos": i, "from": "claude:c1", "at": 1.0, "text": f"post {i} " + "y" * 600} for i in range(lo, min(hi, 14))]


def test_request_quotes_each_post_of_a_leaf_block_as_an_excerpt():
    request = summary.request("release", 0, 16, {}, posts, 0)
    assert all(len(p["text"]) <= summary.EXCERPT_CHARS for p in request["posts"])


def test_request_counts_the_posts_no_longer_kept():
    assert summary.request("release", 0, 16, {}, posts, 0)["missing"] == 2


def test_request_names_the_halves_of_a_larger_block():
    request = summary.request("release", 0, 32, {(0, 16): "first", (16, 32): "second"}, posts, 1)
    assert request["halves"] == [{"block": "0-15", "text": "first"}, {"block": "16-31", "text": "second"}]


def test_request_tells_how_to_record_the_line():
    assert 'one-conv live summarize release 0-15 "<line>"' in summary.request("release", 0, 16, {}, posts, 0)["instruction"]


def test_request_says_how_many_blocks_follow():
    assert "3 more block(s)" in summary.request("release", 0, 16, {}, posts, 3)["instruction"]


def test_opened_leaf_block_is_its_posts():
    assert len(summary.opened(0, 16, {}, posts)["posts"]) == 14


def test_opened_larger_block_is_its_halves():
    assert summary.opened(0, 32, {(0, 16): "first"}, posts)["halves"] == [
        {"block": "0-15", "text": "first"}, {"block": "16-31", "text": None}]
