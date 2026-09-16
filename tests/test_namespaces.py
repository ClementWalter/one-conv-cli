"""Namespace dispatch isolates history sources and preserves existing automation."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys

from click.testing import CliRunner
import pytest

from one_conv import cloud_cli, namespaces, native_auth


@pytest.fixture
def app(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("namespace_cli", str(Path(__file__).parents[1] / "bin/one-conv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    return module


def test_root_lists_only_namespaces(app):
    result = CliRunner().invoke(app.cli, ["--help"])
    commands = result.output.split("Commands:\n")[1].split("\n\n")[0]
    assert [line.strip().split()[0] for line in commands.splitlines()] == ["cloud", "local", "mcp"]


@pytest.fixture
def saved_conversations(app, monkeypatch, tmp_path):
    account = tmp_path / "account"
    account.mkdir()
    (account / "first.json").write_text(json.dumps({
        "source": "claude-chat", "session": "first", "cwd": "claude-chat:personal",
        "title": "First conversation", "turns": [{"role": "user", "text": "Hello", "ts": "2026-01-01T00:00:00Z"}],
    }))
    (account / "second.json").write_text(json.dumps({
        "source": "claude-chat", "session": "second", "cwd": "claude-chat:personal",
        "title": "Second conversation", "account_label": "personal@example.test",
        "turns": [{"role": "user", "text": "Hi", "ts": "2026-02-01T00:00:00Z"}],
    }))
    monkeypatch.setenv("ONE_CONV_CLOUD_CACHE", str(tmp_path))
    monkeypatch.setattr(app, "_chatgpt_iter_projects", lambda: iter(()))
    monkeypatch.setattr(app, "_claude_accounts", lambda: pytest.fail("Listing must stay offline"))
    monkeypatch.setattr(app, "_chatgpt_accounts", lambda: pytest.fail("Listing must stay offline"))
    os.utime(account / "first.json", (1900000000, 1900000000))
    return tmp_path


@pytest.mark.parametrize("path", [["cloud"], ["cloud", "claude"]])
def test_cloud_chats_lists_conversations_by_activity(app, saved_conversations, path):
    result = CliRunner().invoke(app.cli, [*path, "chats", "--json", "--limit", "0"])
    assert [row["uuid"] for row in json.loads(result.stdout)] == ["second", "first"]


def test_cloud_chats_limits_conversations(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "chats", "--json", "--limit", "1"])
    assert [row["title"] for row in json.loads(result.stdout)] == ["Second conversation"]


def test_cached_accounts_preserves_group_counts(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "cached-accounts", "--json"])
    assert [(row["cwd"], row["threads"]) for row in json.loads(result.stdout)] == [("claude-chat:personal", 2)]


def test_cloud_chats_human_output_has_titles(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "chats", "--limit", "1"])
    assert "Second conversation" in result.output


def test_cloud_chats_displays_account_label(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "chats", "--limit", "1"])
    assert "personal@example.test" in result.output


def test_cloud_chats_json_preserves_account_identity(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "chats", "--json", "--limit", "1"])
    assert json.loads(result.stdout)[0]["cwd"] == "claude-chat:personal"


def test_cloud_chats_explains_unread_marker(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "chats"])
    assert "Unread in one-conv" in result.output


def test_claude_does_not_inherit_openai_account_label(app, saved_conversations, monkeypatch):
    monkeypatch.setattr(app, "_chatgpt_cached_accounts", lambda: [{"account_id": "personal", "email": "openai@example.test"}])
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "chats", "--json"])
    assert json.loads(result.stdout)[1]["account_label"] == "personal"


def test_cloud_chats_process_lists_individual_conversations(saved_conversations):
    result = subprocess.run([sys.executable, "-O", str(Path(__file__).parents[1] / "bin/one-conv"),
                             "cloud", "claude", "chats", "--json"],
                            capture_output=True, text=True, check=True, timeout=10)
    assert [row["uuid"] for row in json.loads(result.stdout)] == ["second", "first"]


@pytest.mark.parametrize("path", [["cloud", "read"], ["cloud", "chat"], ["cloud", "thread"], ["cloud", "claude", "read"]])
def test_cloud_read_opens_displayed_id(app, saved_conversations, path):
    result = CliRunner().invoke(app.cli, [*path, "sec", "--json", "--no-mark-read"])
    assert json.loads(result.stdout)["turns"][0]["text"] == "Hi"


def test_cloud_read_matches_title(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "read", "Second conversation", "--json", "--no-mark-read"])
    assert json.loads(result.stdout)["session"] == "second"


def test_cloud_read_rejects_ambiguous_title(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "read", "conversation", "--no-mark-read"])
    assert "Several conversations match" in result.stderr


def test_cloud_read_refresh_is_available(app):
    result = CliRunner().invoke(app.cli, ["cloud", "read", "--help"])
    assert "--refresh" in result.stdout


def test_cloud_read_process_resolves_id(saved_conversations):
    result = subprocess.run([sys.executable, "-O", str(Path(__file__).parents[1] / "bin/one-conv"),
                             "cloud", "read", "sec", "--json", "--no-mark-read"],
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout)["session"] == "second"


@pytest.fixture
def cloud_tool_history(saved_conversations):
    path = saved_conversations / "account" / "second.json"
    document = json.loads(path.read_text())
    document["turns"] = [
        {"role": "user", "text": "Question", "content_blocks": [{"type": "text", "text": "Question"}]},
        {"role": "assistant", "text": "[Thinking block retained in source content]", "content_blocks": [{"type": "thinking", "thinking": "internal-marker"}]},
        {"role": "assistant", "text": "[Tool call: Lookup]", "content_blocks": [{"type": "tool_use", "name": "Lookup", "input": {"query": "tool-marker"}}]},
        {"role": "user", "text": "[Tool result]", "content_blocks": [{"type": "tool_result", "content": "result-marker"}]},
        {"role": "assistant", "text": "Answer\n[Tool call]", "content_blocks": [{"type": "text", "text": "Answer"}, {"type": "tool_use", "name": "Lookup", "input": {}}]},
    ]
    path.write_text(json.dumps(document))
    return path


@pytest.mark.parametrize("source", ["claude-chat", "cowork-cloud"])
def test_cloud_compact_read_hides_tools_and_thinking(app, cloud_tool_history, source):
    document = json.loads(cloud_tool_history.read_text())
    document["source"] = source
    cloud_tool_history.write_text(json.dumps(document))
    result = CliRunner().invoke(app.cli, ["cloud", "read", "second", "--json", "--no-mark-read"])
    assert [turn["text"] for turn in json.loads(result.stdout)["turns"]] == ["Question", "Answer"]


@pytest.mark.parametrize("marker", ["internal-marker", "Lookup", "result-marker"])
def test_cloud_raw_read_preserves_details(app, cloud_tool_history, marker):
    result = CliRunner().invoke(app.cli, ["cloud", "read", "second", "--raw", "--json", "--no-mark-read"])
    assert marker in result.stdout


def test_cloud_list_counts_visible_turns(app, cloud_tool_history):
    result = CliRunner().invoke(app.cli, ["cloud", "chats", "--json"])
    assert next(row for row in json.loads(result.stdout) if row["uuid"] == "second")["turns"] == 2


def test_short_help_survives_legacy_help_cache(app):
    CliRunner().invoke(cloud_cli.cloud_group, ["--help"])
    namespaces.install(app.cli, lambda: iter(()), lambda: iter(()))
    assert CliRunner().invoke(app.cli, ["cloud", "-h"]).exit_code == 0


@pytest.mark.parametrize("path", ["local", "cloud", "cloud claude", "cloud chatgpt", "cloud codex", "cloud cowork", "cloud claude pull"])
def test_nested_short_help(app, path):
    assert CliRunner().invoke(app.cli, [*path.split(), "-h"]).exit_code == 0


@pytest.mark.parametrize(("path", "expected"), [
    (["local"], ("claude", "codex", "cursor", "cursor-cli", "omp", "corpus")),
    (["cloud"], ("chatgpt", "claude-chat", "codex-cloud", "cowork-cloud")),
    (["cloud", "claude"], ("claude-chat",)),
    (["cloud", "codex"], ("codex-cloud",)),
])
def test_readers_discover_only_namespace_sources(app, monkeypatch, path, expected):
    seen = []
    monkeypatch.setattr(app, "_load_read_state", lambda: {})
    monkeypatch.setattr(app, "_iter_all_projects", lambda sources: seen.append(sources) or iter(()))
    CliRunner().invoke(app.cli, [*path, "chats", "--json"])
    assert seen == [expected]


def test_local_search_never_contacts_cloud(app, monkeypatch):
    monkeypatch.setattr(app, "_sessions_for_scope", lambda *args: [])
    monkeypatch.setattr(app, "_cursor_composer_ids_matching", lambda text: set())
    monkeypatch.setattr(app, "_chatgpt_accounts", lambda: pytest.fail("Local search contacted cloud authentication"))
    assert CliRunner().invoke(app.cli, ["local", "search", "needle", "--json"]).output == "[]\n"


def test_cloud_search_reads_cache_without_auth(app, monkeypatch):
    monkeypatch.setattr(app, "_sessions_for_scope", lambda *args: [])
    monkeypatch.setattr(app, "_chatgpt_accounts", lambda: pytest.fail("Cached search contacted authentication"))
    assert CliRunner().invoke(app.cli, ["cloud", "search", "needle", "--json"]).stdout == "[]\n"


def test_saved_reader_labels_cache(app, saved_conversations):
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "chats", "--json"])
    assert "Saved history" in result.stderr


def test_refresh_failure_does_not_return_saved_rows(app, saved_conversations, monkeypatch):
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter(()))
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "chats", "--refresh", "--json"])
    assert result.stdout == ""


def test_refresh_invokes_sync_before_read(app, saved_conversations, monkeypatch):
    calls = []
    monkeypatch.delenv("ONE_CONV_SESSION_FILE", raising=False)
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter([{"account_id": "personal"}]))
    monkeypatch.setattr(cloud_cli, "browser_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cloud_cli, "synchronize", lambda *args, **kwargs: calls.append(kwargs["limit"]) or {})
    CliRunner().invoke(app.cli, ["cloud", "claude", "chats", "--refresh", "--json"])
    assert calls == [100]


@pytest.fixture
def refresh_accounts(app, monkeypatch, saved_conversations):
    calls = []
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter([{"account_id": "work"}]))
    monkeypatch.setattr(app, "_chatgpt_accounts", lambda: iter([{"account_id": "personal"}, {"account_id": "work"}]))
    monkeypatch.setattr(cloud_cli, "browser_provider", lambda product, selector, accounts: (product, selector))
    monkeypatch.setattr(cloud_cli, "synchronize", lambda provider, product, limit: calls.append(provider) or {})
    return calls


def test_global_refresh_visits_all_supported_accounts(app, refresh_accounts):
    CliRunner().invoke(app.cli, ["cloud", "chats", "--refresh", "--json"])
    assert refresh_accounts == [("chatgpt", "personal"), ("chatgpt", "work"),
                                ("claude-chat", "work"), ("codex-cloud", "personal"), ("codex-cloud", "work"), ("cowork-cloud", "work")]


def test_global_refresh_respects_source_filter(app, refresh_accounts):
    CliRunner().invoke(app.cli, ["cloud", "chats", "--refresh", "--source", "claude-chat", "--json"])
    assert refresh_accounts == [("claude-chat", "work")]


def test_global_refresh_keeps_stdout_json(app, refresh_accounts):
    result = CliRunner().invoke(app.cli, ["cloud", "chats", "--refresh", "--json"])
    assert [row["uuid"] for row in json.loads(result.stdout)] == ["second", "first"]


def test_global_refresh_errors_do_not_prevent_other_accounts(app, refresh_accounts, monkeypatch):
    from one_conv.providers.base import AuthenticationRequired
    def provider(product, selector, accounts):
        if product == "chatgpt":
            raise AuthenticationRequired("Reconnect ChatGPT")
        return product, selector
    monkeypatch.setattr(cloud_cli, "browser_provider", provider)
    CliRunner().invoke(app.cli, ["cloud", "chats", "--refresh", "--json"])
    assert refresh_accounts == [("claude-chat", "work"), ("codex-cloud", "personal"), ("codex-cloud", "work"), ("cowork-cloud", "work")]


def test_global_refresh_with_no_accounts_fails(app, saved_conversations, monkeypatch):
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter(()))
    monkeypatch.setattr(app, "_chatgpt_accounts", lambda: iter(()))
    result = CliRunner().invoke(app.cli, ["cloud", "chats", "--refresh", "--json"])
    assert result.exit_code == 1


def test_login_opens_selected_browser_without_keychain(app, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(native_auth, "read_secret", lambda *args, **kwargs: pytest.fail("Login cannot prompt for Keychain"))
    CliRunner().invoke(app.cli, ["cloud", "claude", "connect", "--login", "--browser", "chrome"])
    assert calls == [["open", "-a", "Google Chrome", "https://claude.ai/login"]]


def test_local_rejects_cloud_source(app):
    assert CliRunner().invoke(app.cli, ["local", "chats", "--source", "chatgpt"]).exit_code == 2


def test_legacy_reader_still_spans_every_source(app, monkeypatch):
    seen = []
    monkeypatch.setattr(app, "_load_read_state", lambda: {})
    monkeypatch.setattr(app, "_iter_all_projects", lambda sources: seen.append(sources) or iter(()))
    CliRunner().invoke(app.cli, ["chats", "--json"])
    assert seen == [app.SOURCES]


def test_legacy_chatgpt_help_remains_available(app):
    assert CliRunner().invoke(app.cli, ["chatgpt", "sync", "-h"]).exit_code == 0


@pytest.mark.parametrize(("product", "source"), [("claude", "claude-chat"), ("chatgpt", "chatgpt"), ("codex", "codex-cloud")])
def test_canonical_pull_routes_to_product(app, monkeypatch, product, source):
    monkeypatch.setattr(cloud_cli, "browser_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cloud_cli, "synchronize", lambda provider, selected, limit: {"product": selected, "limit": limit})
    monkeypatch.delenv("ONE_CONV_SESSION_FILE", raising=False)
    result = CliRunner().invoke(app.cli, ["cloud", product, "pull", "--account", "personal", "--limit", "2"])
    assert json.loads(result.stdout) == {"product": source, "limit": 2}


def test_accounts_never_serialize_sessions(app, monkeypatch):
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter([{
        "account_id": "personal", "session": object(), "cookies": {"secret": "hidden"},
    }]))
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "accounts", "--json"])
    assert json.loads(result.stdout) == [{"account_id": "personal", "email": None, "organization_id": None,
                                         "organization_label": None, "profile": None}]


def test_pull_automatically_selects_single_account(app, monkeypatch):
    monkeypatch.delenv("ONE_CONV_SESSION_FILE", raising=False)
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter([{"account_id": "personal"}]))
    monkeypatch.setattr(cloud_cli, "browser_provider", lambda product, selector, *args, **kwargs: selector)
    monkeypatch.setattr(cloud_cli, "synchronize", lambda provider, product, limit: {"account": provider})
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "pull"])
    assert json.loads(result.stdout) == {"account": "personal"}


def test_pull_reports_inaccessible_browser_instead_of_managed_file(app, monkeypatch):
    monkeypatch.delenv("ONE_CONV_SESSION_FILE", raising=False)
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter(()))
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "pull"])
    assert "No browser session is accessible without prompting" in result.output


def test_public_process_reads_saved_claude_messages(tmp_path):
    account = tmp_path / "account"
    account.mkdir()
    (account / "thread.json").write_text(json.dumps({
        "source": "claude-chat", "session": "namespace-thread", "cwd": "claude-chat:namespace-account",
        "title": "Namespace example", "last": "2026-01-01T00:00:00Z",
        "turns": [{"role": "user", "text": "namespace-marker", "ts": "2026-01-01T00:00:00Z"}],
    }))
    result = subprocess.run([sys.executable, "-O", str(Path(__file__).parents[1] / "bin/one-conv"),
                             "cloud", "claude", "search", "namespace-marker", "--json"],
                            env={**os.environ, "ONE_CONV_CLOUD_CACHE": str(tmp_path)},
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout)[0]["session"] == "namespace-thread"


@pytest.mark.parametrize(("domain", "cookie", "value"), [
    ("claude.ai", "sessionKey", "claude-session"),
    ("chatgpt.com", "__Secure-next-auth.session-token", "openai-session"),
])
def test_browser_discovery_keeps_provider_cookies_separate(app, monkeypatch, tmp_path, domain, cookie, value):
    store = tmp_path / "Cookies"
    with sqlite3.connect(store) as connection:
        connection.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, encrypted_value BLOB)")
        connection.executemany("INSERT INTO cookies VALUES(?, ?, ?)", [
            (".claude.ai", "sessionKey", b"claude-session"),
            (".chatgpt.com", "__Secure-next-auth.session-token", b"openai-session"),
            ("unrelated.test", "sessionKey", b"unrelated-session"),
        ])
    monkeypatch.setattr(app, "CHATGPT_COOKIE_SOURCES", [("synthetic", str(store), "synthetic-service")])
    monkeypatch.setattr(app, "_chatgpt_keychain_key", lambda service: b"synthetic-key")
    monkeypatch.setattr(app, "_chatgpt_decrypt_cookie", lambda encrypted, key: encrypted.decode())
    assert list(app._chatgpt_cookie_jars(domain, cookie))[0][1] == {cookie: value}


def test_connect_authorizes_only_selected_browser(app, monkeypatch):
    requests = []
    monkeypatch.setattr(native_auth, "read_secret", lambda service, **kwargs: requests.append((service, kwargs)) or b"synthetic")
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter(()))
    CliRunner().invoke(app.cli, ["cloud", "claude", "connect", "--browser", "chrome"])
    assert requests == [("Chrome Safe Storage", {"authorize": True})]


def test_connect_does_not_log_secret(app, monkeypatch):
    monkeypatch.setattr(native_auth, "read_secret", lambda *args, **kwargs: b"synthetic-secret-marker")
    monkeypatch.setattr(app, "_claude_accounts", lambda: iter(()))
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "connect"])
    assert "synthetic-secret-marker" not in result.output


def test_denied_connect_does_not_discover_accounts(app, monkeypatch):
    monkeypatch.setattr(native_auth, "read_secret", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "_claude_accounts", lambda: pytest.fail("Denied connect cannot discover sessions"))
    result = CliRunner().invoke(app.cli, ["cloud", "claude", "connect"])
    assert "Browser credential access was not authorized" in result.output
