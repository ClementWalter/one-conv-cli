#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
"""ChatGPT web chats normalize into the same Turn shape as every other source."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import click.testing
import pytest

loader = importlib.machinery.SourceFileLoader("agent_conv", str(Path(__file__).parent.parent / "one_conv_cli.py"))
spec = importlib.util.spec_from_loader("agent_conv", loader)
ac = importlib.util.module_from_spec(spec)
sys.modules["agent_conv"] = ac
loader.exec_module(ac)


@pytest.fixture
def conversation(tmp_path: Path) -> Path:
    """A two-exchange conversation whose second answer was regenerated: the
    abandoned branch hangs off the same parent as the kept one."""
    path = tmp_path / "conv.json"
    path.write_text(json.dumps({
        "conversation_id": "conv-1",
        "title": "Vélo électrique",
        "create_time": 1786016706.0,
        "update_time": 1788764539.0,
        "current_node": "n4",
        "mapping": {
            "root": {"id": "root", "message": None, "parent": None, "children": ["n1"]},
            "n1": {"id": "n1", "parent": "root", "children": ["n2"], "message": {
                "author": {"role": "user"}, "create_time": 1786016706.0,
                "content": {"content_type": "text", "parts": ["Quelle différence ?"]}}},
            "n2": {"id": "n2", "parent": "n1", "children": ["n3", "n4"], "message": {
                "author": {"role": "assistant"}, "create_time": 1786016710.0,
                "content": {"content_type": "text", "parts": ["La différence est énorme."]}}},
            "n3": {"id": "n3", "parent": "n2", "children": [], "message": {
                "author": {"role": "assistant"}, "create_time": 1786016720.0,
                "content": {"content_type": "text", "parts": ["Réponse abandonnée."]}}},
            "n4": {"id": "n4", "parent": "n2", "children": [], "message": {
                "author": {"role": "assistant"}, "create_time": 1786016730.0,
                "content": {"content_type": "text", "parts": ["Réponse conservée."]}}},
        },
    }), encoding="utf-8")
    return path


def test_active_branch_drops_the_regenerated_answer(conversation: Path) -> None:
    texts = [b["text"] for t in ac._chatgpt_iter_turns(conversation) for b in t.blocks]
    assert "Réponse abandonnée." not in texts


def test_active_branch_keeps_the_current_answer(conversation: Path) -> None:
    texts = [b["text"] for t in ac._chatgpt_iter_turns(conversation) for b in t.blocks]
    assert "Réponse conservée." in texts


def test_roles_alternate_user_then_assistant(conversation: Path) -> None:
    assert [t.role for t in ac._chatgpt_iter_turns(conversation)] == ["user", "assistant", "assistant"]


def test_turn_timestamp_is_iso_utc(conversation: Path) -> None:
    assert ac._chatgpt_iter_turns(conversation)[0].ts == "2026-08-06T11:45:06+00:00"


def test_system_messages_are_hidden(tmp_path: Path) -> None:
    path = tmp_path / "sys.json"
    path.write_text(json.dumps({
        "current_node": "n1",
        "mapping": {"n1": {"id": "n1", "parent": None, "children": [], "message": {
            "author": {"role": "system"}, "create_time": 1.0,
            "content": {"content_type": "text", "parts": ["hidden context"]}}}},
    }))
    assert ac._chatgpt_iter_turns(path) == []


def test_inline_render_directives_are_stripped() -> None:
    raw = "Avant\ue200image_group\ue202{\"query\":[\"a\"]}\ue201Après"
    assert ac._chatgpt_clean(raw) == "AvantAprès"


def test_plain_prose_survives_cleaning() -> None:
    assert ac._chatgpt_clean("Texte **gras** normal") == "Texte **gras** normal"


@pytest.mark.parametrize(
    ("content", "expected_type"),
    [
        ({"content_type": "text", "parts": ["hi"]}, "text"),
        ({"content_type": "multimodal_text", "parts": [{"content_type": "audio_transcription", "text": "hi"}]}, "text"),
        ({"content_type": "code", "language": "python", "text": "search('x')"}, "tool_use"),
        ({"content_type": "execution_output", "text": "result"}, "tool_result"),
        ({"content_type": "thoughts", "thoughts": [{"summary": "Recherche", "content": ""}]}, "thinking"),
        ({"content_type": "reasoning_recap", "content": "A réfléchi"}, "thinking"),
    ],
)
def test_content_type_maps_to_block_type(content: dict, expected_type: str) -> None:
    assert ac._chatgpt_blocks(content)[0]["type"] == expected_type


def test_unknown_content_type_yields_no_block() -> None:
    assert ac._chatgpt_blocks({"content_type": "tether_quote", "text": "x"}) == []


def test_audio_transcription_text_is_extracted() -> None:
    content = {"content_type": "multimodal_text",
               "parts": [{"content_type": "audio_transcription", "text": " Les pneus larges"}]}
    assert ac._chatgpt_blocks(content)[0]["text"] == " Les pneus larges"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1788764539.5, 1788764539.5),
        ("2026-09-07T07:02:16.821661Z", 1788764536.821661),
        (None, 0.0),
        ("", 0.0),
        ("not-a-date", 0.0),
    ],
)
def test_epoch_normalizes_both_time_encodings(value, expected) -> None:
    assert ac._chatgpt_epoch(value) == pytest.approx(expected)


def test_a_conversation_cycle_cannot_hang_the_walk(tmp_path: Path) -> None:
    """The parent chain is walked with a guard, so a malformed cycle terminates."""
    path = tmp_path / "cycle.json"
    path.write_text(json.dumps({
        "current_node": "a",
        "mapping": {
            "a": {"id": "a", "parent": "b", "children": [], "message": {
                "author": {"role": "user"}, "create_time": 1.0,
                "content": {"content_type": "text", "parts": ["x"]}}},
            "b": {"id": "b", "parent": "a", "children": [], "message": {
                "author": {"role": "user"}, "create_time": 2.0,
                "content": {"content_type": "text", "parts": ["y"]}}},
        },
    }))
    assert len(ac._chatgpt_iter_turns(path)) <= 3


class _StubResponse:
    """Stands in for a curl_cffi response."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> dict:
        return {"mapping": {}}


class _StubSession:
    """Replays a fixed sequence of status codes, recording each call."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def get(self, url: str, timeout: int = 0) -> _StubResponse:
        self.calls += 1
        return _StubResponse(self.statuses.pop(0) if self.statuses else 429)


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff pauses are real seconds; tests assert on control flow, not waiting."""
    monkeypatch.setattr(ac.time, "sleep", lambda _s: None)


def test_a_conversation_that_answers_immediately_is_returned() -> None:
    session = _StubSession([200])
    assert ac._chatgpt_fetch_conversation(session, "c1")[0] == "ok"


def test_rate_limiting_is_retried_until_it_clears() -> None:
    session = _StubSession([429, 429, 200])
    assert ac._chatgpt_fetch_conversation(session, "c1")[0] == "ok"


def test_a_cleared_rate_limit_costs_only_the_needed_attempts() -> None:
    session = _StubSession([429, 429, 200])
    ac._chatgpt_fetch_conversation(session, "c1")
    assert session.calls == 3


def test_a_never_clearing_quota_is_reported_as_quota_not_absence() -> None:
    """Conflating the two would tell the user a conversation no longer exists."""
    assert ac._chatgpt_fetch_conversation(_StubSession([429] * 10), "c1")[0] == "quota"


def test_a_deleted_conversation_is_reported_missing() -> None:
    assert ac._chatgpt_fetch_conversation(_StubSession([404]), "c1")[0] == "missing"


@pytest.mark.parametrize("status", [401, 403])
def test_authorization_failure_is_not_reported_deleted(status):
    from one_conv.providers.base import AuthenticationRequired
    with pytest.raises(AuthenticationRequired):
        ac._chatgpt_fetch_conversation(_StubSession([status]), "c1")


def test_changed_conversation_shape_is_not_cached_as_success():
    from one_conv.providers.base import SchemaChanged
    session = _StubSearchSession([{"unexpected": "shape"}])
    with pytest.raises(SchemaChanged):
        ac._chatgpt_fetch_conversation(session, "c1")


def test_giving_up_is_bounded_by_the_backoff_schedule() -> None:
    session = _StubSession([429] * 10)
    ac._chatgpt_fetch_conversation(session, "c1")
    assert session.calls == len(ac._CHATGPT_BACKOFF) + 1


def test_a_deleted_conversation_is_not_retried() -> None:
    """404 is a permanent answer, so it must not burn the backoff schedule."""
    session = _StubSession([404, 200])
    ac._chatgpt_fetch_conversation(session, "c1")
    assert session.calls == 1


class _StubSyncSession:
    """A chatgpt.com whose listing works but whose bodies are all quota-blocked
    except the first, so a run cannot finish."""

    def __init__(self, conversation_count: int) -> None:
        self.items = [{"id": f"c{i}", "title": f"Chat {i}", "update_time": "2026-09-07T07:02:16.821661Z"}
                      for i in range(conversation_count)]
        self.body_calls = 0
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict | None = None, timeout: int = 0, allow_redirects: bool = False):
        if "/backend-api/conversations" in url:
            if (params or {}).get("is_archived") == "true" or (params or {}).get("offset"):
                return _StubJson({"items": []})
            return _StubJson({"items": self.items})
        self.body_calls += 1
        if self.body_calls == 1:
            return _StubJson({"conversation_id": "c0", "title": "Chat 0", "update_time": 1.0,
                              "current_node": None, "mapping": {}})
        return _StubResponse(429)


class _StubJson(_StubResponse):
    def __init__(self, payload: dict) -> None:
        super().__init__(200)
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def quota_blocked_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ac, "CHATGPT_CACHE", tmp_path)
    session = _StubSyncSession(conversation_count=50)
    auth = {"session": session, "account_id": "acc-1", "email": "me@example.com",
            "plan": "plus", "structure": "personal"}
    stats = ac._chatgpt_sync_account(auth, limit=1000, refresh=False)
    return session, stats


def test_sync_stops_once_the_quota_is_spent(quota_blocked_sync) -> None:
    """Without a run-total give-up this crawls every conversation at a minute each."""
    session, _ = quota_blocked_sync
    assert session.body_calls < 50


def test_sync_reports_what_it_could_not_fetch(quota_blocked_sync) -> None:
    _, stats = quota_blocked_sync
    assert stats["remaining"] == 49


def test_sync_keeps_whatever_it_managed_to_fetch(quota_blocked_sync) -> None:
    _, stats = quota_blocked_sync
    assert stats["fetched"] == 1


def test_an_interrupted_sync_still_leaves_a_readable_account(quota_blocked_sync, tmp_path: Path) -> None:
    assert json.loads((tmp_path / "acc-1" / "account.json").read_text())["email"] == "me@example.com"


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("sediment://file_00000000676472469a36e613edb1e2dd", "file_00000000676472469a36e613edb1e2dd"),
        # a page rendered out of a PDF hides the parent id in the middle segment
        ("sediment://f9012489aad892d#file_00000000df7c72#p_2.jpg", "file_00000000df7c72"),
        ("file-service://file-abc123", "file-abc123"),
        ("sediment://nothing-here", ""),
        ("", ""),
    ],
)
def test_asset_pointer_resolves_to_its_file_id(pointer: str, expected: str) -> None:
    assert ac._chatgpt_asset_id(pointer) == expected


def test_every_page_of_one_pdf_resolves_to_a_single_download() -> None:
    """Otherwise a 40-page PDF would be fetched 40 times."""
    message = {"content": {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "sediment://h1#file_doc#p_1.jpg"},
        {"content_type": "image_asset_pointer", "asset_pointer": "sediment://h2#file_doc#p_2.jpg"},
    ]}}
    assert [a["id"] for a in ac._chatgpt_message_assets(message)] == ["file_doc"]


def test_an_upload_keeps_its_real_filename() -> None:
    message = {"metadata": {"attachments": [{"id": "file_1", "name": "Contrat.pdf", "mime_type": "application/pdf"}]}}
    assert ac._chatgpt_message_assets(message)[0]["name"] == "Contrat.pdf"


def test_an_inline_image_is_labelled_by_its_filename() -> None:
    content = {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_1"}]}
    assert ac._chatgpt_blocks(content, {"file_1": "pfp.jpeg"})[0]["text"] == "[image_asset_pointer: pfp.jpeg]"


def test_a_message_that_is_only_an_upload_still_becomes_a_turn(tmp_path: Path) -> None:
    """A bare attachment carries no prose, but dropping it loses the exchange."""
    path = tmp_path / "upload.json"
    path.write_text(json.dumps({
        "current_node": "n1",
        "mapping": {"n1": {"id": "n1", "parent": None, "children": [], "message": {
            "author": {"role": "user"}, "create_time": 1.0,
            "metadata": {"attachments": [{"id": "file_1", "name": "scan.pdf"}]},
            "content": {"content_type": "text", "parts": [""]}}}},
    }))
    assert ac._chatgpt_iter_turns(path)[0].blocks[0]["text"] == "[attachment: scan.pdf]"


@pytest.fixture
def asset() -> dict:
    return {"id": "file_1", "name": "photo.jpeg", "mime": "image/jpeg"}


def test_an_already_downloaded_file_is_not_refetched(tmp_path: Path, asset: dict) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "file_1.jpeg").write_bytes(b"x")
    session = _StubSession([])
    ac._chatgpt_download_asset(session, tmp_path, asset)
    assert session.calls == 0


def test_a_downloaded_file_lands_on_disk(tmp_path: Path, asset: dict) -> None:
    session = _StubSession([])
    session.get = lambda url, timeout=0: (  # noqa: E731 - two-hop download stub
        _StubJson({"download_url": "https://chatgpt.com/backend-api/estuary/content?sig=x"})
        if "/download" in url else _StubBytes(b"\xff\xd8\xff-jpeg-bytes")
    )
    ac._chatgpt_download_asset(session, tmp_path, asset)
    assert (tmp_path / "assets" / "file_1.jpeg").read_bytes() == b"\xff\xd8\xff-jpeg-bytes"


def test_a_file_gone_from_chatgpt_is_reported_missing(tmp_path: Path, asset: dict) -> None:
    assert ac._chatgpt_download_asset(_StubSession([404]), tmp_path, asset) == "missing"


def test_a_rate_limited_file_is_not_reported_as_gone(tmp_path: Path, asset: dict) -> None:
    """Conflating the two would tell the user their data no longer exists."""
    assert ac._chatgpt_download_asset(_StubSession([429] * 10), tmp_path, asset) == "quota"


class _StubBytes(_StubResponse):
    def __init__(self, content: bytes) -> None:
        super().__init__(200)
        self.content = content


def test_outstanding_counts_work_a_later_round_can_still_do() -> None:
    results = [{"remaining": 5, "assets_blocked": 2}, {"remaining": 0, "assets_blocked": 1}]
    assert ac._chatgpt_outstanding(results) == 8


def test_files_gone_for_good_are_not_counted_as_outstanding() -> None:
    """Otherwise --until-complete would spin until --max-rounds every time."""
    assert ac._chatgpt_outstanding([{"remaining": 0, "assets_blocked": 0, "assets_missing": 12}]) == 0


@pytest.fixture
def rounds_recorder(monkeypatch: pytest.MonkeyPatch):
    """Drive the command's round loop with scripted per-round outcomes."""
    def run(outcomes: list[int], **kwargs):
        seen = []

        def fake_round(limit, refresh, assets, account_filter, verbose):
            seen.append(1)
            left = outcomes[len(seen) - 1] if len(seen) <= len(outcomes) else 0
            return [{"remaining": left, "assets_blocked": 0, "fetched": 1,
                     "skipped": 0, "assets": 0, "assets_missing": 0}]

        monkeypatch.setattr(ac, "_chatgpt_sync_round", fake_round)
        monkeypatch.setattr(ac.time, "sleep", lambda _s: None)
        runner = click.testing.CliRunner()
        result = runner.invoke(ac.cli, ["chatgpt", "sync", *kwargs.pop("args", [])])
        return len(seen), result
    return run


def test_a_plain_sync_makes_exactly_one_pass(rounds_recorder) -> None:
    calls, _ = rounds_recorder([9, 9, 9], args=[])
    assert calls == 1


def test_until_complete_keeps_going_while_work_remains(rounds_recorder) -> None:
    calls, _ = rounds_recorder([5, 3, 0], args=["--until-complete", "--wait", "0"])
    assert calls == 3


def test_until_complete_stops_as_soon_as_nothing_is_left(rounds_recorder) -> None:
    calls, _ = rounds_recorder([0, 9, 9], args=["--until-complete", "--wait", "0"])
    assert calls == 1


def test_until_complete_honours_its_safety_stop(rounds_recorder) -> None:
    calls, _ = rounds_recorder([7] * 50, args=["--until-complete", "--wait", "0", "--max-rounds", "4"])
    assert calls == 4


class _StubSearchSession:
    """Serves scripted search pages, following the `cursor` it hands out."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url: str, timeout: int = 0):
        self.urls.append(url)
        return _StubJson(self.pages[len(self.urls) - 1] if len(self.urls) <= len(self.pages) else {"items": []})


def _hit(cid: str) -> dict:
    return {"conversation_id": cid, "title": cid, "update_time": 1.0,
            "payload": {"snippet": "match"}}


def test_search_returns_the_first_page_of_hits() -> None:
    session = _StubSearchSession([{"items": [_hit("a"), _hit("b")], "cursor": None}])
    assert [h["conversation_id"] for h in ac._chatgpt_remote_search(session, "q", 30)] == ["a", "b"]


def test_search_follows_the_cursor_across_pages() -> None:
    session = _StubSearchSession([
        {"items": [_hit("a")], "cursor": "c1"},
        {"items": [_hit("b")], "cursor": None},
    ])
    assert len(ac._chatgpt_remote_search(session, "q", 30)) == 2


def test_search_passes_the_cursor_back_to_the_server() -> None:
    session = _StubSearchSession([
        {"items": [_hit("a")], "cursor": "c1"},
        {"items": [_hit("b")], "cursor": None},
    ])
    ac._chatgpt_remote_search(session, "q", 30)
    assert "cursor=c1" in session.urls[1]


def test_search_stops_at_the_requested_limit() -> None:
    session = _StubSearchSession([{"items": [_hit(str(i)) for i in range(10)], "cursor": "c1"}])
    assert len(ac._chatgpt_remote_search(session, "q", 4)) == 4


def test_search_stops_on_an_empty_page_rather_than_looping() -> None:
    session = _StubSearchSession([{"items": [], "cursor": "c1"}])
    ac._chatgpt_remote_search(session, "q", 30)
    assert len(session.urls) == 1


def test_a_query_is_url_encoded() -> None:
    session = _StubSearchSession([{"items": [], "cursor": None}])
    ac._chatgpt_remote_search(session, "dégât des eaux", 30)
    assert " " not in session.urls[0]


def test_search_yields_nothing_when_the_quota_blocks_it() -> None:
    assert ac._chatgpt_remote_search(_StubSession([429] * 10), "q", 30) == []


def test_search_rows_are_listed_most_recent_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The server pages by relevance, so concatenated pages arrive unordered."""
    monkeypatch.setattr(ac, "CHATGPT_CACHE", tmp_path)
    monkeypatch.setattr(ac, "_chatgpt_accounts", lambda: iter([
        {"session": None, "account_id": "acc", "email": "me@example.com",
         "plan": "plus", "structure": "personal", "profile": "chrome/Default"}]))
    monkeypatch.setattr(ac, "_chatgpt_remote_search", lambda s, q, n: [
        {"conversation_id": "old", "title": "Old", "update_time": 1735230720.0, "payload": {}},
        {"conversation_id": "new", "title": "New", "update_time": 1788030660.0, "payload": {}},
        {"conversation_id": "mid", "title": "Mid", "update_time": 1761908340.0, "payload": {}},
    ])
    result = click.testing.CliRunner().invoke(ac.cli, ["chatgpt", "search", "q", "--json"])
    assert [r["uuid"] for r in json.loads(result.output)] == ["new", "mid", "old"]


@pytest.fixture
def targeted_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Invoke `chatgpt sync` with a scope, against a stubbed account."""
    def run(args: list[str], hits: list[dict] | None = None, outcome: str = "ok"):
        monkeypatch.setattr(ac, "CHATGPT_CACHE", tmp_path)
        monkeypatch.setattr(ac, "_chatgpt_accounts", lambda: iter([
            {"session": None, "account_id": "acc", "email": "me@example.com",
             "plan": "plus", "structure": "personal", "profile": "chrome/Default"}]))
        monkeypatch.setattr(ac, "_chatgpt_remote_search", lambda s, q, n: hits or [])
        monkeypatch.setattr(ac, "_chatgpt_fetch_conversation",
                            lambda s, cid: (outcome, {"title": "T", "mapping": {}} if outcome == "ok" else None))
        bulk_ran = []
        monkeypatch.setattr(ac, "_chatgpt_sync_round",
                            lambda *a, **k: bulk_ran.append(1) or [])
        result = click.testing.CliRunner().invoke(ac.cli, ["chatgpt", "sync", *args])
        return result, bulk_ran
    return run


def test_naming_an_id_syncs_only_that_conversation(targeted_sync, tmp_path: Path) -> None:
    targeted_sync(["conv-1"])
    assert (tmp_path / "acc" / "conv" / "conv-1.json").exists()


def test_naming_an_id_skips_the_bulk_listing(targeted_sync) -> None:
    """The whole point of a scoped sync is not enumerating the account."""
    _, bulk_ran = targeted_sync(["conv-1"])
    assert bulk_ran == []


def test_a_search_scope_syncs_what_it_finds(targeted_sync, tmp_path: Path) -> None:
    targeted_sync(["--search", "q"], hits=[{"conversation_id": "found-1", "title": "Found"}])
    assert (tmp_path / "acc" / "conv" / "found-1.json").exists()


def test_a_search_scope_leaves_already_cached_hits_alone(targeted_sync, tmp_path: Path) -> None:
    (tmp_path / "acc" / "conv").mkdir(parents=True)
    (tmp_path / "acc" / "conv" / "have.json").write_text('{"title": "have"}')
    result, _ = targeted_sync(["--search", "q"], hits=[{"conversation_id": "have", "title": "Have"}])
    assert "Nothing to do" in result.output


def test_no_scope_falls_through_to_the_bulk_sync(targeted_sync) -> None:
    _, bulk_ran = targeted_sync([])
    assert bulk_ran == [1]


def test_a_rate_limited_target_is_not_written_to_the_cache(targeted_sync, tmp_path: Path) -> None:
    targeted_sync(["conv-1"], outcome="quota")
    assert not (tmp_path / "acc" / "conv" / "conv-1.json").exists()


def test_a_rate_limited_target_says_to_retry(targeted_sync) -> None:
    result, _ = targeted_sync(["conv-1"], outcome="quota")
    assert "retry later" in result.output


def test_default_search_includes_uncached_online_history(monkeypatch):
    monkeypatch.setattr(ac, "_sessions_for_scope", lambda *args: [])
    monkeypatch.setattr(ac, "_cursor_composer_ids_matching", lambda text: set())
    monkeypatch.setattr(ac, "_chatgpt_accounts", lambda: [{"email": "owner@example.test", "session": object()}])
    monkeypatch.setattr(ac, "_chatgpt_remote_search", lambda *args, **kwargs: [{"conversation_id": "online-only", "payload": {"snippet": "needle"}}])
    result = click.testing.CliRunner().invoke(ac.cli, ["search", "needle", "--json"])
    assert json.loads(result.stdout)[0]["session"] == "online-only"


def test_offline_search_never_opens_online_accounts(monkeypatch):
    monkeypatch.setattr(ac, "_sessions_for_scope", lambda *args: [])
    monkeypatch.setattr(ac, "_cursor_composer_ids_matching", lambda text: set())
    monkeypatch.setattr(ac, "_chatgpt_accounts", lambda: pytest.fail("Offline search must not access online accounts"))
    result = click.testing.CliRunner().invoke(ac.cli, ["search", "needle", "--offline", "--json"])
    assert json.loads(result.stdout) == []


def test_strict_search_reports_online_failure():
    with pytest.raises(RuntimeError, match="Online conversation search is unavailable"):
        ac._chatgpt_remote_search(_StubSession([429] * 10), "query", 30, strict=True)
