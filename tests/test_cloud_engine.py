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
"""Exercise cloud synchronization through persistence and the public CLI."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
from dataclasses import replace

from click.testing import CliRunner
import pytest

from one_conv.cloud import synchronize
from one_conv.providers.base import (
    Conversation, ConversationPage, ConversationSummary, Message,
    ProviderAccount, SchemaChanged,
)


class ProviderStub:
    """Expose controlled pages and failures at the provider boundary."""

    def __init__(self, conversation, account="account-a", pages=None):
        self.conversation = conversation
        self.account = account
        self.failure = None
        self.pages = pages if pages is not None else {
            None: ConversationPage((ConversationSummary(
                conversation.id, conversation.title, 1700000000,
                "https://claude.ai/chat/thread-1",
            ),)),
        }

    def validate_connection(self):
        return ProviderAccount(self.account, self.account, ("list", "read"))

    def list_conversations(self, cursor=None, limit=50):
        return self.pages[cursor]

    def read_conversation(self, conversation_id):
        if self.failure:
            raise self.failure
        return self.conversation


@pytest.fixture
def conversation():
    return Conversation("thread-1", "Architecture", (
        Message("question", "user", "Choose storage", timestamp=1700000000),
        Message("discarded", "assistant", "Discarded option", "question"),
        Message("accepted", "assistant", "Use durable-cloud-test storage", "question"),
    ), active_leaf_id="accepted")


@pytest.fixture
def cached_document(tmp_path, conversation):
    synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)
    path = next(p for p in tmp_path.glob("*/*.json") if p.name != ".status.json")
    return json.loads(path.read_text())


def test_persists_provider_source(cached_document):
    assert cached_document["source"] == "claude-chat"


def test_persists_account_display_label(cached_document):
    assert cached_document["account_label"] == "account-a"


def test_incomplete_message_window_is_not_ready(tmp_path, conversation):
    partial = replace(conversation, complete=False, coverage="current_branch")
    assert synchronize(ProviderStub(partial), "chatgpt", root=tmp_path)["state"] == "partial"


def test_partial_refresh_retains_complete_snapshot(tmp_path, conversation):
    provider = ProviderStub(conversation)
    synchronize(provider, "chatgpt", root=tmp_path)
    provider.conversation = replace(conversation, complete=False, title="Partial")
    synchronize(provider, "chatgpt", root=tmp_path)
    path = next(p for p in tmp_path.glob("*/*.json") if p.name != ".status.json")
    assert json.loads(path.read_text())["title"] == "Architecture"


def test_persists_provider_account(cached_document):
    assert cached_document["account_id"] == "account-a"


def test_persists_source_reference(cached_document):
    assert cached_document["source_url"] == "https://claude.ai/chat/thread-1"


def test_retains_all_branch_messages(cached_document):
    assert [message["id"] for message in cached_document["messages"]] == ["question", "discarded", "accepted"]


def test_projects_only_active_branch_into_search(cached_document):
    assert [turn["id"] for turn in cached_document["turns"]] == ["question", "accepted"]


def test_same_remote_identity_stays_account_isolated(tmp_path, conversation):
    synchronize(ProviderStub(conversation, "account-a"), "claude-chat", root=tmp_path)
    synchronize(ProviderStub(conversation, "account-b"), "claude-chat", root=tmp_path)
    assert len([p for p in tmp_path.glob("*/*.json") if p.name != ".status.json"]) == 2


def test_schema_failure_preserves_cached_transcript(tmp_path, conversation):
    provider = ProviderStub(conversation)
    synchronize(provider, "claude-chat", root=tmp_path)
    path = next(p for p in tmp_path.glob("*/*.json") if p.name != ".status.json")
    before = path.read_bytes()
    provider.failure = SchemaChanged("Changed upstream")
    try:
        synchronize(provider, "claude-chat", root=tmp_path)
    except SchemaChanged:
        pass
    assert path.read_bytes() == before


def test_schema_failure_is_visible(tmp_path, conversation):
    provider = ProviderStub(conversation)
    provider.failure = SchemaChanged("Changed upstream")
    with pytest.raises(SchemaChanged):
        synchronize(provider, "claude-chat", root=tmp_path)


def test_empty_first_page_can_advance(tmp_path, conversation):
    provider = ProviderStub(conversation, pages={
        None: ConversationPage((), "next"),
        "next": ConversationPage((ConversationSummary(conversation.id, conversation.title),)),
    })
    assert synchronize(provider, "claude-chat", root=tmp_path)["fetched"] == 1


def test_empty_account_is_successful(tmp_path, conversation):
    provider = ProviderStub(conversation, pages={None: ConversationPage(())})
    assert synchronize(provider, "claude-chat", root=tmp_path)["state"] == "ready"


def test_repeated_cursor_fails_instead_of_looping(tmp_path, conversation):
    provider = ProviderStub(conversation, pages={
        None: ConversationPage((), "next"), "next": ConversationPage((), "next"),
    })
    with pytest.raises(SchemaChanged, match="pagination did not advance"):
        synchronize(provider, "claude-chat", root=tmp_path)


def test_branch_cycle_does_not_persist(tmp_path):
    conversation = Conversation("thread-1", "Cycle", (Message("self", "user", "x", "self"),), "self")
    with pytest.raises(SchemaChanged, match="cycle"):
        synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)


class EmptyPagesStub(ProviderStub):
    """A drifting upstream advances cursors without returning conversations."""

    def list_conversations(self, cursor=None, limit=50):
        return ConversationPage((), str(int(cursor or "0") + 1))


def test_unique_empty_pages_are_bounded(tmp_path, conversation):
    with pytest.raises(SchemaChanged, match="page budget"):
        synchronize(EmptyPagesStub(conversation), "claude-chat", root=tmp_path, limit=1)


def test_unknown_active_leaf_is_rejected(tmp_path):
    conversation = Conversation("thread-1", "Missing leaf", (Message("known", "user", "x"),), "unknown")
    with pytest.raises(SchemaChanged, match="missing message"):
        synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)


def test_overlong_final_page_reports_partial(tmp_path, conversation):
    provider = ProviderStub(conversation, pages={None: ConversationPage((
        ConversationSummary(conversation.id, conversation.title),
        ConversationSummary("remaining", "Unfetched conversation"),
    ))})
    assert synchronize(provider, "claude-chat", root=tmp_path, limit=1)["state"] == "partial"


@pytest.fixture
def cli_module(monkeypatch, tmp_path):
    loader = importlib.machinery.SourceFileLoader(
        "one_conv_cloud_test_cli", str(Path(__file__).parents[1] / "one_conv_cli.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    # Local adapters are excluded from this cloud-only acceptance fixture.
    monkeypatch.setattr(module, "_claude_iter_projects", lambda: iter(()))
    monkeypatch.setattr(module, "_codex_iter_projects", lambda: iter(()))
    monkeypatch.setattr(module, "_cursor_iter_projects", lambda: iter(()))
    monkeypatch.setattr(module, "_omp_iter_projects", lambda: iter(()))
    monkeypatch.setattr(module, "_chatgpt_iter_projects", lambda: iter(()))
    monkeypatch.setattr(module, "_cursor_composer_ids_matching", lambda _: set())
    monkeypatch.setenv("ONE_CONV_CLOUD_CACHE", str(tmp_path))
    return module


def test_public_cloud_help(cli_module):
    result = CliRunner().invoke(cli_module.cli, ["cloud", "--help"])
    assert result.exit_code == 0, result.output


def test_public_cloud_provider_coverage(cli_module):
    result = CliRunner().invoke(cli_module.cli, ["cloud", "providers"])
    assert next(row for row in json.loads(result.output) if row["product"] == "cowork-cloud")["adapter"] == "experimental"


def test_default_offline_search_reads_cached_claude_chat(cli_module, tmp_path, conversation):
    synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)
    result = CliRunner().invoke(cli_module.cli, ["search", "durable-cloud-test", "--offline", "--json"])
    assert json.loads(result.output)[0]["source"] == "claude-chat"


def test_offline_search_excludes_discarded_branch(cli_module, tmp_path, conversation):
    synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)
    result = CliRunner().invoke(cli_module.cli, ["search", "Discarded option", "--offline", "--json"])
    assert json.loads(result.output) == []


@pytest.mark.parametrize("payload", [
    "invalid-json",
    [],
    {"source": "claude-chat", "turns": []},
    {"source": "claude-chat", "turns": {}, "cwd": "account", "session": "id"},
    {"source": "claude-chat", "turns": [], "cwd": 42, "session": "id"},
])
def test_malformed_cache_does_not_break_default_search(cli_module, tmp_path, conversation, payload):
    synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)
    directory = tmp_path / "malformed"
    directory.mkdir()
    (directory / "broken.json").write_text(payload if isinstance(payload, str) else json.dumps(payload))
    result = CliRunner().invoke(cli_module.cli, ["search", "durable-cloud-test", "--offline", "--json"])
    assert json.loads(result.output)[0]["source"] == "claude-chat"


class CountingStub(ProviderStub):
    def __init__(self, conversation):
        super().__init__(conversation)
        self.reads = 0

    def read_conversation(self, conversation_id):
        self.reads += 1
        return super().read_conversation(conversation_id)


def test_unchanged_conversation_is_not_read_again(tmp_path, conversation):
    provider = CountingStub(conversation)
    synchronize(provider, "chatgpt", root=tmp_path)
    synchronize(provider, "chatgpt", root=tmp_path)
    assert provider.reads == 1


def test_unchanged_conversation_is_reported(tmp_path, conversation):
    provider = CountingStub(conversation)
    synchronize(provider, "chatgpt", root=tmp_path)
    assert synchronize(provider, "chatgpt", root=tmp_path)["unchanged"] == 1


def test_updated_conversation_is_read_again(tmp_path, conversation):
    provider = CountingStub(conversation)
    synchronize(provider, "chatgpt", root=tmp_path)
    provider.pages = {None: ConversationPage((ConversationSummary(
        conversation.id, conversation.title, 1700000999, "https://claude.ai/chat/thread-1"),))}
    synchronize(provider, "chatgpt", root=tmp_path)
    assert provider.reads == 2


def test_conversation_listed_twice_is_read_once(tmp_path, conversation):
    summary = ConversationSummary(conversation.id, conversation.title, 1700000000, "https://claude.ai/chat/thread-1")
    provider = CountingStub(conversation)
    provider.pages = {None: ConversationPage((summary,), "next"), "next": ConversationPage((summary,))}
    synchronize(provider, "chatgpt", root=tmp_path)
    assert provider.reads == 1


def test_cloud_listing_reads_each_document_once(cli_module, tmp_path, conversation, monkeypatch):
    synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)
    reads = []
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: reads.append(path) or original(path, *a, **k))
    CliRunner().invoke(cli_module.cli, ["search", "durable-cloud-test", "--offline", "--json"])
    assert len([path for path in reads if path.parent.parent == tmp_path and path.name != ".status.json"]) == 2


def test_local_listing_reads_no_cloud_document(cli_module, tmp_path, conversation, monkeypatch):
    synchronize(ProviderStub(conversation), "claude-chat", root=tmp_path)
    reads = []
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: reads.append(path) or original(path, *a, **k))
    CliRunner().invoke(cli_module.cli, ["local", "chats", "--json"])
    assert [path for path in reads if path.parent.parent == tmp_path] == []
