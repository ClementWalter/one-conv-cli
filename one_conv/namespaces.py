"""Separate cloud products from local tools while preserving legacy command dispatch."""

from copy import copy

import click


LOCAL_SOURCES = ("claude", "codex", "cursor", "cursor-cli", "omp", "corpus")
CLOUD_PRODUCTS = {"chatgpt": "chatgpt", "claude": "claude-chat",
                  "codex": "codex-cloud", "cowork": "cowork-cloud"}
READ_COMMANDS = ("chats", "read", "thread", "search", "find", "unread")
HELP_SETTINGS = {"help_option_names": ["-h", "--help"]}


class NamespaceRoot(click.Group):
    """Keep legacy names callable without mixing them into the primary help."""

    def list_commands(self, context):
        return [name for name in ("cloud", "local", "mcp") if name in self.commands]


def selected_sources(source, defaults):
    """Apply namespace boundaries before reading files or contacting providers."""
    context = click.get_current_context(silent=True)
    scope = (context.obj or {}).get("history_sources", defaults) if context else defaults
    if source is not None and source not in scope:
        raise click.BadParameter("Source is outside this namespace.", param_hint="--source")
    return (source,) if source else scope


def _reader(command, sources, path):
    """Clone options so scoped help cannot mutate compatibility commands."""
    result = copy(command)
    result.params = [copy(parameter) for parameter in command.params]
    result.context_settings = HELP_SETTINGS
    result.hidden = False
    for parameter in result.params:
        if parameter.name == "source_filter":
            parameter.type = click.Choice(sources)
            parameter.help = "Restrict to one source in this namespace."
    if command.name == "search":
        # Namespace readers inspect saved history; provider pulls own authentication.
        result.params = [parameter for parameter in result.params if parameter.name != "offline"]
        result.callback = lambda **kwargs: command.callback(offline=True, **kwargs)
    descriptions = {"chats": "List saved projects and accounts.",
                    "read": "List a saved project's conversations; --expand includes messages.",
                    "thread": "Read one saved conversation.",
                    "search": "Search saved messages without contacting providers.",
                    "find": "Find saved conversations by title.",
                    "unread": "List saved conversations with unread activity."}
    result.help = descriptions.get(command.name, command.help)
    if command.name == "thread" and set(sources).issubset(CLOUD_PRODUCTS.values()):
        result.params = [parameter for parameter in result.params if parameter.name not in ("session_uuid", "nth", "match")]
        result.callback = lambda **kwargs: command.callback(direct_query=True, session_uuid=None, nth=1, match=0, **kwargs)
        result.help = "Read a conversation by the ID shown in chats, or by its title."
    if command.name == "chats" and set(sources).issubset(CLOUD_PRODUCTS.values()):
        result.callback = lambda **kwargs: command.callback(conversations=True, **kwargs)
        result.help = "List individual saved conversations, newest activity first. Run pull to fetch history."
        for parameter in result.params:
            if parameter.name == "limit":
                parameter.type = click.IntRange(min=0)
                parameter.help = "Number of conversations to list (0 = all)."
                parameter.show_default = True
    example_args = " QUERY" if command.name in ("read", "thread", "search", "find", "fork", "port", "send") else ""
    result.epilog = f"\b\nExamples:\n  {path} {command.name}{example_args} --help"
    if command.name in READ_COMMANDS and set(sources).issubset(CLOUD_PRODUCTS.values()):
        callback = result.callback

        @click.pass_context
        def read_saved(context, refresh=False, **kwargs):
            if refresh:
                from contextlib import redirect_stdout
                from .cloud_cli import sync
                import sys
                click.echo("Fetching fresh history from the provider...", err=True)
                # Reports go to stderr so conversation JSON remains pipeable.
                with redirect_stdout(sys.stderr):
                    if len(sources) > 1:
                        _refresh_accounts(context, selected_sources(kwargs.get("source_filter"), sources))
                    else:
                        name = next(name for name, product in CLOUD_PRODUCTS.items() if product == sources[0])
                        context.invoke(_pull(sync, name, sources[0]))
            message = ("Live refresh completed. Showing saved history, including older cached conversations and other accounts."
                       if refresh else "Saved history only; no network request. Run a product reader with --refresh or use pull for fresh data.")
            click.echo(message, err=True)
            return callback(**kwargs)

        result.callback = read_saved
        if len(sources) == 1 or command.name in ("chats", "thread"):
            result.params.append(click.Option(["--refresh"], is_flag=True,
                help="Refresh accessible supported accounts first (up to 100 conversations each); fail on refresh errors."))
    return result


def _refresh_accounts(context, sources):
    """Attempt every accessible account, sharing discovery across OpenAI products."""
    import json
    from .cloud_cli import browser_provider, synchronize
    from .providers.base import ProviderError

    discovered = {}
    errors = []
    refreshed = 0
    for product in sources:
        key = "claude_accounts" if product in ("claude-chat", "cowork-cloud") else "browser_accounts"
        if key not in discovered:
            try:
                discovered[key] = list(context.obj[key]())
            except (ProviderError, click.ClickException) as error:
                discovered[key] = None
                errors.append(f"{key}: {error}")
        accounts = discovered[key]
        if accounts is None:
            continue
        if not accounts:
            click.echo(f"{product}: skipped (no accessible browser account; saved history is unchanged).", err=True)
        for account in accounts:
            label = account.get("email") or account.get("organization_label") or account["account_id"]
            click.echo(f"Refreshing {product}: {label}...", err=True)
            try:
                provider = browser_provider(product, account["account_id"], lambda: iter(accounts))
                report = synchronize(provider, product, limit=100)
                click.echo(json.dumps(report), err=True)
                refreshed += 1
            except (ProviderError, click.ClickException) as error:
                errors.append(f"{product} ({label}): {error}")
    if errors:
        raise click.ClickException("Some accounts could not refresh; successful pulls were saved. " + "; ".join(errors))
    if not refreshed:
        raise click.ClickException("No supported account could be refreshed. Connect a browser account first.")


def _scope_callback(sources, accounts=None):
    @click.pass_context
    def configure(context):
        context.obj = {**(context.obj or {}), "history_sources": sources}
        if accounts:
            context.obj.update(accounts)
    return configure


def install(root, browser_accounts, claude_accounts):
    """Mount product readers and pulls while leaving old invocations intact."""
    from .cloud_cli import cloud_group, sync

    local = click.Group("local", callback=_scope_callback(LOCAL_SOURCES),
                        help="Read local agent history and run local agent tools.",
                        epilog="\b\nExamples:\n  one-conv local chats\n  one-conv local search QUERY",
                        context_settings=HELP_SETTINGS)
    for name in (*READ_COMMANDS, "export", "fork", "port", "send", "skill-usage", "append"):
        local.add_command(_reader(root.commands[name], LOCAL_SOURCES, "one-conv local"))
    root.add_command(local)

    cloud_sources = tuple(CLOUD_PRODUCTS.values())
    # Click caches help options on commands, so the namespace owns a fresh group.
    cloud = click.Group("cloud", commands=dict(cloud_group.commands), context_settings=HELP_SETTINGS)
    cloud.callback = _scope_callback(cloud_sources, {
        "browser_accounts": browser_accounts, "claude_accounts": claude_accounts,
    })
    cloud.help = "Read cloud conversation history by product. Pull first, then read saved history."
    cloud.epilog = "\b\nExamples:\n  one-conv cloud claude --help\n  one-conv cloud chatgpt --help\n  one-conv cloud search QUERY"
    legacy_sync = copy(sync)
    legacy_sync.hidden = True
    cloud.add_command(legacy_sync)
    for name in READ_COMMANDS:
        reader = _reader(root.commands["thread" if name == "read" else name], cloud_sources, "one-conv cloud")
        reader.name = name
        if name in ("read", "thread"):
            reader.epilog = f"Examples:\n  one-conv cloud {name} ID\n  one-conv cloud {name} ID --refresh"
        cloud.add_command(reader)
    chat_alias = copy(cloud.commands["read"])
    chat_alias.name = "chat"
    chat_alias.epilog = "Examples:\n  one-conv cloud chat ID"
    cloud.add_command(chat_alias)
    cached_accounts = _reader(root.commands["chats"], cloud_sources, "one-conv cloud")
    cached_accounts.name = "cached-accounts"
    cached_accounts.callback = root.commands["chats"].callback
    cached_accounts.params = [parameter for parameter in cached_accounts.params if parameter.name != "refresh"]
    cached_accounts.help = "List saved account groups and their conversation counts without contacting providers."
    cached_accounts.epilog = "Examples:\n  one-conv cloud cached-accounts --json"
    for parameter in cached_accounts.params:
        if parameter.name == "limit":
            parameter.type = click.IntRange(min=1)
            parameter.help = "Number of account groups to list."
    cloud.add_command(cached_accounts)
    for name, product in CLOUD_PRODUCTS.items():
        group = click.Group(name, callback=_scope_callback((product,)),
                            help={"chatgpt": "ChatGPT web conversations.",
                                  "claude": "Claude web conversations (not local Claude Code).",
                                  "codex": "Codex cloud tasks (not local Codex CLI).",
                                  "cowork": "Cowork cloud sessions and their saved event transcripts."}[name],
                            context_settings=HELP_SETTINGS,
                            epilog=f"\b\nExamples:\n  one-conv cloud {name} accounts\n  one-conv cloud {name} pull --help\n  one-conv cloud {name} chats")
        for reader in READ_COMMANDS:
            scoped = _reader(root.commands["thread" if reader == "read" else reader], (product,), f"one-conv cloud {name}")
            scoped.name = reader
            if reader in ("read", "thread"):
                scoped.epilog = f"Examples:\n  one-conv cloud {name} {reader} ID"
            group.add_command(scoped)
        group.add_command(_pull(sync, name, product))
        group.add_command(_accounts(name))
        group.add_command(_connect(name))
        cloud.add_command(group)
    root.add_command(cloud)


def _pull(sync, name, product):
    """Bind the provider once so every cloud product has the same pull shape."""
    @click.pass_context
    def invoke(context, **kwargs):
        if (kwargs.get("browser_account") is None
                and kwargs.get("session_file") is None):
            callback_key = "claude_accounts" if product in ("claude-chat", "cowork-cloud") else "browser_accounts"
            from .providers.base import ProviderError
            try:
                accounts = list(context.obj[callback_key]())
            except ProviderError as error:
                raise click.ClickException(str(error)) from None
            if not accounts:
                raise click.ClickException("No browser session is accessible without prompting. "
                                           f"Run one-conv cloud {name} accounts to inspect access.")
            if len(accounts) > 1:
                raise click.ClickException(f"Several accounts are available. Run one-conv cloud {name} accounts "
                                           "and select one with --account.")
            # Reuse this discovery result rather than decrypting the same session twice.
            context.obj = {**context.obj, callback_key: lambda: iter(accounts)}
            kwargs["browser_account"] = accounts[0]["account_id"]
        return context.invoke(sync, product=product, **kwargs)

    parameters = [copy(parameter) for parameter in sync.params if parameter.name != "product"]
    for parameter in parameters:
        if parameter.name == "session_file":
            parameter.hidden = True
        if parameter.name == "browser_account":
            parameter.opts = ["--account", "--browser-account"]
            parameter.help = "Select a signed-in browser account by exact email, name or ID."
    return click.Command("pull", callback=invoke, params=parameters,
                         help=("Pull provider history into the saved conversation cache.\n\n"
                               "Choose an accessible browser account from the accounts command. "
                               "Keychain access never prompts; locked sessions require setup authorization."),
                         epilog=f"\b\nExamples:\n  one-conv cloud {name} pull --account ACCOUNT --limit 10",
                         context_settings=HELP_SETTINGS)


def _accounts(name):
    """Expose verified selectors without serializing HTTP sessions or credentials."""
    import json
    from .providers.base import ProviderError

    @click.pass_context
    def invoke(context, as_json):
        callback = (context.obj or {}).get("claude_accounts" if name in ("claude", "cowork") else "browser_accounts")
        try:
            rows = [{key: account.get(key) for key in (
                "account_id", "email", "organization_id", "organization_label", "profile")}
                    for account in callback()]
        except ProviderError as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps(rows))
        elif rows:
            for row in rows:
                click.echo(f"{row['account_id']}  {row['email'] or row['organization_label'] or ''}")
        else:
            click.echo("No browser account is accessible without prompting. Sign in to the provider and authorize session access during setup.")

    return click.Command("accounts", callback=invoke,
                         params=[click.Option(["--json", "as_json"], is_flag=True, help="Emit safe account metadata as JSON.")],
                         help="Discover browser accounts accessible without password prompts.",
                         epilog=f"\b\nExamples:\n  one-conv cloud {name} accounts --json",
                         context_settings=HELP_SETTINGS)


def _connect(name):
    """Request one browser's Keychain authorization only during explicit setup."""
    services = {"chrome": "Chrome Safe Storage", "arc": "Arc Safe Storage",
                "brave": "Brave Safe Storage", "edge": "Microsoft Edge Safe Storage",
                "chromium": "Chromium Safe Storage"}

    @click.pass_context
    def invoke(context, browser, login):
        from .native_auth import read_secret
        if login:
            url = "https://claude.ai/login" if name in ("claude", "cowork") else "https://chatgpt.com/auth/login"
            import subprocess
            import sys
            apps = {"chrome": "Google Chrome", "arc": "Arc", "brave": "Brave Browser",
                    "edge": "Microsoft Edge", "chromium": "Chromium"}
            if sys.platform != "darwin":
                raise click.ClickException("Opening a selected browser currently supports macOS only.")
            try:
                subprocess.run(["open", "-a", apps[browser], url], check=True, capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                raise click.ClickException("Could not open the selected browser for login.") from None
            click.echo(f"Finish provider sign-in in {browser}, then run one-conv cloud {name} accounts to verify it. "
                       "Opening this page does not mean the account is connected.", err=True)
            return
        click.echo(f"Authorizing {browser} access. Choose Always Allow in the macOS dialog to reuse this authorization.", err=True)
        try:
            secret = read_secret(services[browser], authorize=True)
        except (OSError, RuntimeError) as error:
            raise click.ClickException(str(error)) from None
        if secret is None:
            raise click.ClickException("Browser credential access was not authorized.")
        del secret
        context.obj = {**context.obj, "browser_filter": browser}
        context.invoke(_accounts(name), as_json=False)

    return click.Command("connect", callback=invoke,
                         params=[click.Option(["--login"], is_flag=True, help="Open provider sign-in instead of requesting Keychain authorization."),
                                 click.Option(["--browser"], type=click.Choice(tuple(services)),
                                              default="chrome", show_default=True,
                                              help="Authorize this browser only.")],
                         help="Authorize a stable native helper once; normal pulls never prompt.",
                         epilog=f"\b\nExamples:\n  one-conv cloud {name} connect --browser chrome",
                         context_settings=HELP_SETTINGS)
