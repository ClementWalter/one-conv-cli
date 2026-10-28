"""Operate a personal cloud workspace without putting credentials in command arguments."""

import json
import os
from pathlib import Path

import click


def default_directory():
    return Path(os.getenv("ONE_CONV_CONTROL_DIR", Path.home() / ".config/one-conv/control"))


def private_output(path, value):
    """Secret exports are exclusive private files; a typo cannot replace an existing key."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value + "\n")


def build_control_group():
    @click.group("control", epilog="\b\nExamples:\n  one-conv live control serve --origin https://team.example --config control.json\n  one-conv live control pair --origin https://team.example --output phone.link")
    @click.option("--directory", type=click.Path(path_type=Path), default=default_directory,
                  help="Private cloud state directory; defaults to ONE_CONV_CONTROL_DIR.")
    @click.pass_context
    def control(context, directory):
        """Host the mobile workspace, pair a device and provision node credentials."""
        context.ensure_object(dict)
        context.obj["control_directory"] = directory

    @control.command("serve", epilog="\b\nExamples:\n  one-conv live control serve --origin https://team.example --host 0.0.0.0 --port 8790\n  one-conv live control serve --origin https://team.example --config control.json")
    @click.option("--origin", required=True, help="Exact public HTTPS origin terminating TLS in front of this service.")
    @click.option("--host", default="127.0.0.1", show_default=True)
    @click.option("--port", type=click.IntRange(1, 65535), default=8790, show_default=True)
    @click.option("--config", type=click.Path(exists=True, path_type=Path), help="Cloud box JSON (profiles and the spawner block); omit to run no cloud spawner.")
    @click.option("--session-days", type=click.IntRange(1, 30), default=30, show_default=True)
    @click.pass_context
    def serve(context, origin, host, port, config, session_days):
        """Run messaging, the authenticated PWA, staffing and, when configured, the cloud spawner box."""
        from .runtime import application
        try:
            import uvicorn
        except ImportError:
            raise click.ClickException("Install the cloud dependencies: uv run --with starlette==1.8.0 --with uvicorn==0.54.0 bin/one-conv live control serve --help") from None
        try:
            settings = json.loads(config.read_text()) if config else None
            if settings is not None and not isinstance(settings, dict):
                raise ValueError("The cloud box configuration must be an object")
            os.umask(0o077)
            app = application(context.obj["control_directory"], origin, worker_config=settings,
                              session_seconds=session_days * 86400)
        except (OSError, ValueError) as error:
            raise click.ClickException(str(error)) from None
        uvicorn.run(app, host=host, port=port, access_log=False, proxy_headers=False)

    @control.command("pair", epilog="\b\nExamples:\n  one-conv live control pair --origin https://team.example --output phone.link")
    @click.option("--origin", required=True)
    @click.option("--ttl", type=click.IntRange(1, 3600), default=600, show_default=True)
    @click.option("--output", type=click.Path(path_type=Path), required=True, help="New private file receiving the one-use URL.")
    @click.pass_context
    def pair(context, origin, ttl, output):
        """Write a short-lived pairing link; each new link supersedes unused older links."""
        from .auth import AuthStore
        from .server import canonical_origin
        try:
            origin = canonical_origin(origin)
            if output.exists():
                raise ValueError("Pairing output already exists; choose a new file")
            token = AuthStore(context.obj["control_directory"]).issue_pairing_token(ttl)
            private_output(output, origin + "/#pair=" + token)
        except (OSError, ValueError) as error:
            raise click.ClickException(str(error)) from None
        click.echo(f"Pairing link written privately; valid for {ttl} seconds.")

    @control.command("node", epilog="\b\nExamples:\n  one-conv live control node add laptop --output laptop.token\n  one-conv live control node add cfo --output cfo.token --staffing\n  one-conv live control node grant-staffing cfo\n  one-conv live control node revoke-staffing cfo\n  one-conv live control node revoke laptop")
    @click.argument("action", type=click.Choice(["add", "revoke", "grant-staffing", "revoke-staffing"]))
    @click.argument("name")
    @click.option("--output", type=click.Path(path_type=Path), help="Required for add; new private file receiving the node key.")
    @click.option("--staffing", is_flag=True, help="Only with add: delegate staffing mutations to every process sharing this node key.")
    @click.pass_context
    def node(context, action, name, output, staffing):
        """Manage node credentials and staffing delegation. Ordinary keys cannot change staffing."""
        from ..live.hub import HubError
        from .store import NODE, ControlStore
        if name == NODE:
            raise click.ClickException("workspace is reserved for the human mailbox")
        try:
            if action != "add" and (output is not None or staffing):
                raise ValueError("--output and --staffing are supported only with add")
            if action == "add" and (output is None or output.exists()):
                raise ValueError("add requires a new --output file")
            store = ControlStore(context.obj["control_directory"])
            if action == "add":
                private_output(output, store.add_node(name, staffing=staffing))
            elif action == "grant-staffing":
                store.grant_staffing(name)
            elif action == "revoke-staffing":
                store.revoke_staffing(name)
            elif not store.revoke_node(name):
                raise ValueError("No active node credential has that name")
        except (OSError, ValueError, HubError) as error:
            raise click.ClickException(str(error)) from None
        click.echo({"add": "Node credential written privately.", "revoke": "Node credential revoked.",
                    "grant-staffing": "Staffing authority granted to the current node credential.",
                    "revoke-staffing": "Staffing authority revoked; node messaging remains enabled."}[action])

    @control.command("revoke-devices", epilog="\b\nExamples:\n  one-conv live control revoke-devices --yes")
    @click.option("--yes", is_flag=True, required=True, help="Invalidate every paired browser session.")
    @click.pass_context
    def revoke_devices(context, yes):
        """Disconnect every browser without interrupting node bridges."""
        from .auth import AuthStore
        count = AuthStore(context.obj["control_directory"]).revoke_all_sessions()
        click.echo(f"Revoked {count} browser session(s).")

    return control
