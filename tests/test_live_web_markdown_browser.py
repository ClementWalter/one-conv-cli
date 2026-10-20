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

See [the plan](https://example.invalid/plan), __also bold__, _quietly_ and ~~not this~~."""


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
