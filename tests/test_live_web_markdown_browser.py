#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
#     "mcp==2.2.0",
#     "websockets==17.2",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
#     "httpx==0.28.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Markdown as agents write it renders in the thread: headings, numbered and nested lists, quotes, tables, links."""

import pytest

from one_conv.live import team
from tests import test_live_web_browser as web_tests

pytest.importorskip("playwright.sync_api")

browser = web_tests.browser
workspace = web_tests.workspace

MARKDOWN = """## Release check

1. Run the suite
2. Tag the build
   - nested note
- plain bullet

> Ship it when green.

| step | owner |
|------|-------|
| tests | @builder |
| tag | @reviewer |

---

See [the plan](https://example.invalid/plan), __also bold__, _quietly_ and ~~not this~~.

Merged https://example.invalid/pull/64, see https://example.invalid/wiki/Run_(tests) and https://example.invalid/end."""


@pytest.fixture
def page(browser, workspace):
    team.record("design-review", "c" * 32, "omp:browser-builder", MARKDOWN)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    active = context.new_page()
    active.goto(workspace["url"], wait_until="networkidle")
    active.locator("#topics .conversation-row").first.click()
    active.locator(".message-body table").wait_for()
    yield active
    context.close()


def test_a_heading_renders_as_a_heading(page):
    assert page.locator(".message-body h4").inner_text() == "Release check"


def test_a_numbered_list_renders_as_an_ordered_list(page):
    assert page.locator(".message-body ol > li").count() == 2


def test_an_indented_bullet_nests_under_its_item(page):
    assert page.locator(".message-body ol > li:nth-child(2) ul > li").inner_text() == "nested note"


def test_a_quote_renders_as_a_blockquote(page):
    assert page.locator(".message-body blockquote").inner_text() == "Ship it when green."


def test_a_table_renders_its_header_cells(page):
    assert page.locator(".message-body th").all_inner_texts() == ["step", "owner"]


def test_a_table_renders_its_rows(page):
    assert page.locator(".message-body tbody tr").count() == 2


def test_a_rule_renders_as_a_divider(page):
    assert page.locator(".message-body hr").count() == 1


def test_a_markdown_link_renders_with_its_text_and_target(page):
    link = page.locator(".message-body a", has_text="the plan")
    assert link.get_attribute("href") == "https://example.invalid/plan"


def test_a_bare_url_leaves_a_following_comma_out_of_the_link(page):
    assert page.locator(".message-body a", has_text="pull/64").get_attribute("href") == "https://example.invalid/pull/64"


def test_a_bare_url_leaves_a_sentence_end_out_of_the_link(page):
    assert page.locator(".message-body a", has_text="/end").get_attribute("href") == "https://example.invalid/end"


def test_a_bare_url_keeps_a_closing_parenthesis_it_opened(page):
    assert page.locator(".message-body a", has_text="wiki").get_attribute("href") == "https://example.invalid/wiki/Run_(tests)"


def test_the_punctuation_after_a_bare_url_stays_in_the_text(page):
    assert "pull/64, see" in page.locator(".message-body p", has_text="Merged").inner_text()


def test_underscore_bold_renders_as_strong(page):
    assert page.locator(".message-body strong", has_text="also bold").count() == 1


def test_underscore_emphasis_renders_as_em(page):
    assert page.locator(".message-body em", has_text="quietly").count() == 1


def test_strikethrough_renders(page):
    assert page.locator(".message-body s", has_text="not this").count() == 1


def test_an_underscore_inside_a_word_is_not_emphasis(page, workspace):
    team.record("design-review", "d" * 32, "omp:browser-builder", "Rename snake_case_name to camelCase.")
    page.reload(wait_until="networkidle")
    page.locator("#topics .conversation-row").first.click()
    page.locator(".message-body", has_text="snake_case_name").wait_for()
    assert page.locator(".message-body", has_text="snake_case_name").locator("em").count() == 0
