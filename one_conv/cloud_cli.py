"""Expose experimental cloud adapters without embedding credentials in arguments."""

import json
import os
from pathlib import Path
import stat

import click

from .cloud import synchronize
from .providers.base import ProviderAccount, ProviderError


PRODUCTS = ("chatgpt", "claude-chat", "codex-cloud", "cowork-cloud")


def browser_provider(product, selector, accounts, organization=None):
    """Reuse the launcher's authenticated sessions only after explicit selection."""
    if product not in PRODUCTS:
        raise click.ClickException("Browser sessions do not yet support this cloud product.")
    if not callable(accounts):
        raise click.ClickException("Browser accounts are unavailable in this client.")
    if not selector.strip():
        raise click.ClickException("Choose an exact browser account email or account ID.")
    matches = [account for account in accounts()
               if selector in (account.get("account_id"), account.get("email"), account.get("organization_label"), account.get("organization_id"))
               and (organization is None or organization == account.get("organization_id"))]
    if not matches:
        raise click.ClickException("No signed-in browser account matches that selector or is accessible without authorization. Locked sessions are skipped without prompting.")
    if len(matches) != 1:
        raise click.ClickException("Several accounts share that email; choose an exact account ID.")
    account = matches[0]
    if not account.get("account_id") or account.get("session") is None:
        raise click.ClickException("The selected browser account has no authenticated session.")
    if product == "claude-chat":
        from .providers.anthropic import AnthropicProvider
        return AnthropicProvider(account["session"], account["organization_id"],
                                 authenticated_account=ProviderAccount(account["account_id"],
                                     account.get("organization_label") or account["organization_id"],
                                     ("claude_chat:list", "claude_chat:read")))
    if product == "cowork-cloud":
        from .providers.cowork import CoworkProvider, CAPABILITIES
        return CoworkProvider(account["session"], account["organization_id"],
                              authenticated_account=ProviderAccount(account["account_id"],
                                  account.get("organization_label") or account["organization_id"], CAPABILITIES))
    if product == "chatgpt":
        from .providers.chatgpt import ChatGPTProvider
        return ChatGPTProvider(account["session"], authenticated_account=ProviderAccount(
            account["account_id"], account.get("email") or account["account_id"],
            ("list", "read", "search"),
        ))
    from .providers.codex import CodexProvider
    return CodexProvider(account["session"], account["account_id"], account.get("email"))


def session_provider(product, path, organization):
    """Accept a private broker-provisioned session; never inspect browser stores."""
    if product == "cowork-cloud":
        raise click.ClickException("Cowork requires browser authentication; use cloud cowork pull --account ACCOUNT.")
    if path is None:
        raise click.ClickException("No managed session is configured. The cloud login flow is not implemented yet.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise click.ClickException("Managed session must be a private regular file (mode 600).")
            if metadata.st_size > 65536:
                raise click.ClickException("Managed session exceeds its size limit.")
            config = json.load(stream)
    except (OSError, ValueError):
        raise click.ClickException("Managed session cannot be read; reconnect the account.") from None
    if not isinstance(config, dict) or config.get("provider") != ("anthropic" if product == "claude-chat" else "openai"):
        raise click.ClickException("Managed session provider does not match the requested product.")
    cookies = config.get("cookies", {})
    headers = config.get("headers", {})
    if not isinstance(cookies, dict) or not isinstance(headers, dict):
        raise click.ClickException("Invalid managed session format.")
    if not all(isinstance(k, str) and isinstance(v, str) and "\n" not in v and "\r" not in v
               for k, v in list(cookies.items()) + list(headers.items())):
        raise click.ClickException("Invalid managed session values.")
    if set(headers) - {"Authorization", "ChatGPT-Account-ID"}:
        raise click.ClickException("Managed session contains unsupported headers.")
    if any(not k or any(char in k for char in "\r\n;=") for k in cookies):
        raise click.ClickException("Managed session contains invalid cookie names.")
    from curl_cffi import requests
    session = requests.Session(impersonate="chrome")
    session.cookies.update(cookies)
    session.headers.update(headers)
    if product == "chatgpt":
        from .providers.chatgpt import ChatGPTProvider
        return ChatGPTProvider(session)
    if product == "claude-chat":
        if not organization:
            raise click.ClickException("Select a Claude organization with --organization.")
        from .providers.anthropic import AnthropicProvider
        return AnthropicProvider(session, organization)
    from .providers.codex import CodexProvider
    from .providers.chatgpt import ChatGPTProvider
    account = ChatGPTProvider(session).validate_connection()
    return CodexProvider(session, account.id, account.label)


@click.group("cloud")
def cloud_group():
    """Inspect and synchronize experimental cloud sources using managed sessions."""


@cloud_group.command("providers")
def providers():
    """Show implemented coverage, distinct from live account verification."""
    click.echo(json.dumps([
        {"product": "chatgpt", "adapter": "experimental", "coverage": "paginated current conversation branch", "verification": "browser responses verified 2026-09-08", "hosted_login_verified": False},
        {"product": "claude-chat", "adapter": "experimental", "coverage": "organization chat conversation trees", "verification": "browser responses verified 2026-09-08", "hosted_login_verified": False},
        {"product": "codex-cloud", "adapter": "experimental", "coverage": "current tasks with turn graphs", "verification": "browser responses verified 2026-09-08", "hosted_login_verified": False},
        {"product": "cowork-cloud", "adapter": "experimental", "coverage": "remote Cowork sessions with paginated event snapshots", "verification": "session and event routes observed 2026-09-08", "hosted_login_verified": False},
    ]))


@cloud_group.command("sync")
@click.argument("product", type=click.Choice(PRODUCTS))
@click.option("--session-file", type=click.Path(path_type=Path), envvar="ONE_CONV_SESSION_FILE", help="Private session provisioned by an authentication broker; never a browser database.")
@click.option("--browser-account", metavar="EMAIL|ID|ORG", help="Explicitly reuse a signed-in ChatGPT/Codex account or Claude organization.")
@click.option("--organization", help="Explicit Claude organization ID.")
@click.option("--limit", type=click.IntRange(1, 10000), default=100, show_default=True)
@click.pass_context
def sync(context, product, session_file, browser_account, organization, limit):
    """Cache normalized cloud history. Example: one-conv cloud sync chatgpt --limit 10.

    Uses a managed session or an explicitly selected existing browser account.
    This command does not implement hosted user login.
    """
    if browser_account is not None and session_file is not None:
        raise click.UsageError("--browser-account and --session-file are mutually exclusive.")
    try:
        if browser_account is not None:
            callback = "claude_accounts" if product in ("claude-chat", "cowork-cloud") else "browser_accounts"
            provider = browser_provider(product, browser_account,
                                        (context.obj or {}).get(callback), organization=organization)
        else:
            provider = session_provider(product, session_file, organization)
        transport = getattr(provider, "transport", None)
        if transport is not None:
            # A CLI backfill can wait out long provider throttles that would stall an MCP request.
            transport.max_retry_delay = 600
            transport.max_attempts = max(transport.max_attempts, 10)
        report = synchronize(provider, product, limit=limit)
    except ProviderError as error:
        raise click.ClickException(str(error)) from None
    click.echo(json.dumps(report))
