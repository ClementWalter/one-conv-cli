"""Remote MCP connectors: claude.ai and remote Cowork reach a node's CLIs through this controller.

A node bridge publishes each CLI's tool catalog (from `mcp-bridge catalog`). The controller
serves every published CLI at `/mcp/<cli>` over streamable HTTP, behind OAuth 2.1 so it can
be added to claude.ai as a custom connector. A tool call is queued for the node that
published the CLI; its bridge runs it with `mcp-bridge call --json` and posts the result.

OAuth is narrow on purpose: clients may register only claude.ai's callback, the consent step
needs a browser already paired with this workspace, and each token serves one CLI endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..live.hub import HubError
from .auth import COOKIE
from .store import private_database

logger = logging.getLogger(__name__)

# claude.ai's connector callback; a client registering any other redirect is refused.
CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
SCOPE = "connectors"
REGISTER = "/oauth/register"
CLI_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{43}")
# mcp-bridge stops a tool after 600 s; the relay waits a little longer for its answer.
CALL_SECONDS = 660.0
CALL_STEP = 0.25
TAKE_WAIT = 25.0
# A node republishes every minute; a catalog older than this means the node is gone.
LIVE_SECONDS = 180.0
PENDING_SECONDS, CODE_SECONDS = 600, 300
ACCESS_SECONDS, REFRESH_SECONDS = 3600, 30 * 86400
MAX_CATALOG = 2_000_000
MAX_RESULT = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogs (cli TEXT PRIMARY KEY, node TEXT NOT NULL, catalog TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, node TEXT NOT NULL, cli TEXT NOT NULL, tool TEXT NOT NULL,
  arguments TEXT NOT NULL, created REAL NOT NULL, taken REAL, result TEXT, finished REAL);
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grants (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
  data TEXT NOT NULL, expires REAL NOT NULL);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ConnectorStore:
    """Catalogs, queued calls and OAuth grants; secrets are stored only as hashes."""

    def __init__(self, directory: Path, clock=time.time):
        self.path = Path(directory) / "connectors.sqlite3"
        self.clock = clock
        with private_database(self.path) as db:
            db.executescript(SCHEMA)

    # Catalogs and calls -----------------------------------------------------------------

    def publish(self, node: str, catalogs: object) -> dict:
        """Replace NODE's catalogs; a CLI another node serves stays with that node."""
        if not isinstance(catalogs, dict) or len(catalogs) > 64:
            raise HubError(400, "catalogs is an object of at most 64 CLIs")
        rows = []
        for cli, catalog in catalogs.items():
            if not isinstance(cli, str) or not CLI_NAME.fullmatch(cli):
                raise HubError(400, f"invalid CLI name {cli!r}")
            if (not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list)
                    or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in catalog["tools"])):
                raise HubError(400, f"catalog of {cli} needs a tools list of named tools")
            text = json.dumps(catalog)
            if len(text) > MAX_CATALOG:
                raise HubError(413, f"catalog of {cli} is too large")
            rows.append((cli, node, text, self.clock()))
        with private_database(self.path) as db:
            taken = {cli for (cli,) in db.execute("SELECT cli FROM catalogs WHERE node != ?", (node,))
                     if self.clock() - self._updated(db, cli) < LIVE_SECONDS}
            clash = taken & {row[0] for row in rows}
            if clash:
                raise HubError(409, f"another node serves {', '.join(sorted(clash))}")
            db.execute("DELETE FROM catalogs WHERE node = ?", (node,))
            db.executemany("INSERT OR REPLACE INTO catalogs VALUES (?, ?, ?, ?)", rows)
        return {"published": sorted(row[0] for row in rows)}

    @staticmethod
    def _updated(db, cli: str) -> float:
        row = db.execute("SELECT updated FROM catalogs WHERE cli = ?", (cli,)).fetchone()
        return row[0] if row else 0.0

    def catalog(self, cli: str) -> tuple[str, dict, float] | None:
        """(node, catalog, last publish time) of CLI, or None when no node published it."""
        with private_database(self.path) as db:
            row = db.execute("SELECT node, catalog, updated FROM catalogs WHERE cli = ?", (cli,)).fetchone()
        return None if row is None else (row[0], json.loads(row[1]), row[2])

    def published(self) -> list[dict]:
        with private_database(self.path) as db:
            rows = db.execute("SELECT cli, node, updated FROM catalogs ORDER BY cli").fetchall()
        return [{"cli": cli, "node": node, "live": self.clock() - updated < LIVE_SECONDS} for cli, node, updated in rows]

    def enqueue(self, node: str, cli: str, tool: str, arguments: dict) -> str:
        ident = secrets.token_hex(16)
        with private_database(self.path) as db:
            db.execute("INSERT INTO calls (id, node, cli, tool, arguments, created) VALUES (?, ?, ?, ?, ?, ?)",
                       (ident, node, cli, tool, json.dumps(arguments), self.clock()))
        return ident

    def take(self, node: str, wait: float, sleep=time.sleep) -> list[dict]:
        """Hand NODE its waiting calls, holding the request open up to WAIT seconds for one to arrive."""
        deadline = time.monotonic() + max(0.0, min(wait, TAKE_WAIT))
        while True:
            with private_database(self.path) as db:
                # A call taken but never answered (the bridge restarted) is handed out again.
                rows = db.execute("SELECT id, cli, tool, arguments FROM calls WHERE node = ? AND finished IS NULL"
                                  " AND (taken IS NULL OR taken < ?) AND created > ? ORDER BY created LIMIT 8",
                                  (node, self.clock() - CALL_SECONDS, self.clock() - CALL_SECONDS)).fetchall()
                db.executemany("UPDATE calls SET taken = ? WHERE id = ?", [(self.clock(), r[0]) for r in rows])
            if rows or time.monotonic() >= deadline:
                return [{"id": i, "cli": c, "tool": t, "arguments": json.loads(a)} for i, c, t, a in rows]
            sleep(CALL_STEP)

    def finish(self, node: str, ident: str, result: object) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise HubError(400, "result is an MCP CallToolResult object")
        text = json.dumps(result)
        if len(text) > MAX_RESULT:
            raise HubError(413, "result is too large")
        with private_database(self.path) as db:
            updated = db.execute("UPDATE calls SET result = ?, finished = ? WHERE id = ? AND node = ? AND finished IS NULL",
                                 (text, self.clock(), ident, node)).rowcount
        if not updated:
            raise HubError(404, "no open call with that id for this node")

    def result(self, ident: str) -> dict | None:
        with private_database(self.path) as db:
            row = db.execute("SELECT result FROM calls WHERE id = ?", (ident,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def prune(self) -> None:
        """Drop answered or abandoned calls and expired grants, so the tables stay small."""
        now = self.clock()
        with private_database(self.path) as db:
            db.execute("DELETE FROM calls WHERE created < ?", (now - 2 * CALL_SECONDS,))
            db.execute("DELETE FROM pending WHERE expires < ?", (now,))
            db.execute("DELETE FROM grants WHERE expires < ?", (now,))

    # OAuth persistence -------------------------------------------------------------------

    def save_client(self, client_id: str, info: str) -> None:
        with private_database(self.path) as db:
            db.execute("INSERT OR REPLACE INTO clients VALUES (?, ?)", (client_id, info))

    def client(self, client_id: str) -> str | None:
        with private_database(self.path) as db:
            row = db.execute("SELECT info FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return row[0] if row else None

    def open_request(self, client_id: str, params: dict) -> str:
        ident = secrets.token_urlsafe(32)
        with private_database(self.path) as db:
            db.execute("INSERT INTO pending VALUES (?, ?, ?, ?)",
                       (ident, client_id, json.dumps(params), self.clock() + PENDING_SECONDS))
        return ident

    def request(self, ident: str, *, consume: bool = False) -> tuple[str, dict] | None:
        with private_database(self.path) as db:
            row = db.execute("SELECT client_id, params FROM pending WHERE id = ? AND expires > ?",
                             (ident, self.clock())).fetchone()
            if row and consume:
                db.execute("DELETE FROM pending WHERE id = ?", (ident,))
        return None if row is None else (row[0], json.loads(row[1]))

    def grant(self, kind: str, client_id: str, data: dict, seconds: int) -> str:
        secret = secrets.token_urlsafe(32)
        with private_database(self.path) as db:
            db.execute("INSERT INTO grants VALUES (?, ?, ?, ?, ?)",
                       (_hash(secret), kind, client_id, json.dumps(data), self.clock() + seconds))
        return secret

    def granted(self, kind: str, secret: str, *, consume: bool = False) -> tuple[str, dict, float] | None:
        with private_database(self.path) as db:
            row = db.execute("SELECT client_id, data, expires FROM grants WHERE hash = ? AND kind = ? AND expires > ?",
                             (_hash(secret), kind, self.clock())).fetchone()
            if row and consume:
                db.execute("DELETE FROM grants WHERE hash = ?", (_hash(secret),))
        return None if row is None else (row[0], json.loads(row[1]), row[2])

    def revoke(self, secret: str) -> None:
        with private_database(self.path) as db:
            db.execute("DELETE FROM grants WHERE hash = ?", (_hash(secret),))


class Provider:
    """The MCP SDK's authorization-server provider, over ConnectorStore."""

    def __init__(self, store: ConnectorStore, origin: str):
        self.store, self.origin = store, origin

    async def get_client(self, client_id):
        from mcp.shared.auth import OAuthClientInformationFull
        info = await run_in_threadpool(self.store.client, client_id)
        return None if info is None else OAuthClientInformationFull.model_validate_json(info)

    async def register_client(self, client_info):
        from mcp.server.auth.provider import RegistrationError
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not uris or any(uri not in CALLBACKS for uri in uris):
            raise RegistrationError(error="invalid_redirect_uri",
                                    error_description="this workspace accepts only claude.ai's connector callback")
        await run_in_threadpool(self.store.save_client, client_info.client_id, client_info.model_dump_json())

    async def authorize(self, client, params):
        ident = await run_in_threadpool(self.store.open_request, client.client_id, params.model_dump(mode="json"))
        return f"{self.origin}/oauth/consent?request={ident}"

    async def load_authorization_code(self, client, authorization_code):
        from mcp.server.auth.provider import AuthorizationCode
        found = await run_in_threadpool(self.store.granted, "code", authorization_code)
        if found is None or found[0] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, client_id=found[0], expires_at=found[2], **found[1])

    async def exchange_authorization_code(self, client, authorization_code):
        await run_in_threadpool(self.store.granted, "code", authorization_code.code, consume=True)
        return await self._tokens(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        from mcp.server.auth.provider import RefreshToken
        found = await run_in_threadpool(self.store.granted, "refresh", refresh_token)
        if found is None or found[0] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=found[0], expires_at=int(found[2]),
                            scopes=found[1]["scopes"], resource=found[1].get("resource"))

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        # Refresh tokens rotate: the presented one is spent.
        await run_in_threadpool(self.store.granted, "refresh", refresh_token.token, consume=True)
        return await self._tokens(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        from mcp.server.auth.provider import AccessToken
        found = await run_in_threadpool(self.store.granted, "access", token)
        if found is None:
            return None
        return AccessToken(token=token, client_id=found[0], scopes=found[1]["scopes"], expires_at=int(found[2]),
                           resource=found[1].get("resource"))

    async def revoke_token(self, token):
        await run_in_threadpool(self.store.revoke, token.token)

    async def _tokens(self, client_id, scopes, resource):
        from mcp.shared.auth import OAuthToken
        data = {"scopes": list(scopes or [SCOPE]), "resource": resource}
        access = await run_in_threadpool(self.store.grant, "access", client_id, data, ACCESS_SECONDS)
        refresh = await run_in_threadpool(self.store.grant, "refresh", client_id, data, REFRESH_SECONDS)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_SECONDS,
                          refresh_token=refresh, scope=" ".join(data["scopes"]))


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html lang=en><meta charset=utf-8>"
                        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{html.escape(title)}</title><body><h1>{html.escape(title)}</h1>{body}</body></html>",
                        status_code=status)


def install(app, origin: str, directory: Path) -> ConnectorStore:
    """Mount OAuth, the per-CLI MCP endpoints and the node relay routes on the controller APP."""
    from mcp import types
    from mcp.server.auth.provider import construct_redirect_uri
    from mcp.server.auth.routes import create_auth_routes
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    from .server import bounded_json

    store = ConnectorStore(directory)
    provider = Provider(store, origin)
    auth, hub = app.state.auth, app.state.hub

    def cli_of(ctx) -> str:
        return ctx.request.scope["oneconv.cli"]

    async def list_tools(ctx, params):
        found = await run_in_threadpool(store.catalog, cli_of(ctx))
        tools = [types.Tool.model_validate(tool) for tool in (found[1]["tools"] if found else [])]
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params):
        cli = cli_of(ctx)
        found = await run_in_threadpool(store.catalog, cli)
        if found is None or time.time() - found[2] >= LIVE_SECONDS:
            return _error(f"{cli} is unreachable: the computer that serves it is offline or its bridge is stopped.")
        ident = await run_in_threadpool(store.enqueue, found[0], cli, params.name, params.arguments or {})
        deadline = time.monotonic() + CALL_SECONDS
        while time.monotonic() < deadline:
            result = await run_in_threadpool(store.result, ident)
            if result is not None:
                return types.CallToolResult.model_validate(result)
            await asyncio.sleep(CALL_STEP)
        return _error(f"{cli} did not answer within {CALL_SECONDS:.0f}s; the call may still run on its computer.")

    def _error(text: str):
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)

    server = Server("one-conv-connectors", on_list_tools=list_tools, on_call_tool=call_tool,
                    instructions="Tools of a command-line app on the user's computer, run there through one-conv. "
                                 "Call `usage_guide` once before the first other call.")
    # The controller already pins the Host header to its origin.
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True,
                                           security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    def metadata_url(cli: str) -> str:
        return f"{origin}/.well-known/oauth-protected-resource/mcp/{cli}"

    class Endpoint:
        """One CLI's MCP endpoint: a bearer token issued for it, then the shared stateless server."""

        async def __call__(self, scope, receive, send):
            cli = scope["path_params"]["cli"]
            header = dict(scope["headers"]).get(b"authorization", b"").decode()
            token = header[7:] if header.lower().startswith("bearer ") else ""
            access = await provider.load_access_token(token) if token else None
            resource = f"{origin}/mcp/{cli}"
            if access is None or (access.resource and access.resource.rstrip("/") != resource):
                challenge = f'Bearer error="invalid_token", resource_metadata="{metadata_url(cli)}"'
                await JSONResponse({"error": "invalid_token"}, status_code=401,
                                   headers={"WWW-Authenticate": challenge})(scope, receive, send)
                return
            if not CLI_NAME.fullmatch(cli) or await run_in_threadpool(store.catalog, cli) is None:
                await JSONResponse({"error": f"no connector named {cli}"}, status_code=404)(scope, receive, send)
                return
            scope["oneconv.cli"] = cli
            await manager.handle_request(scope, receive, send)

    async def resource_metadata(request):
        cli = request.path_params.get("cli")
        resource = f"{origin}/mcp/{cli}" if cli else f"{origin}/mcp"
        return JSONResponse({"resource": resource, "authorization_servers": [origin + "/"],
                             "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
                             "resource_name": cli or "one-conv connectors"})

    async def consent(request):
        """Show the request; the Allow button, a same-site POST, carries the paired session cookie."""
        ident = request.query_params.get("request", "") if request.method == "GET" else (await request.form()).get("request", "")
        if not isinstance(ident, str) or not REQUEST_ID.fullmatch(ident):
            return _page("Connector request expired", "<p>Start adding the connector again from Claude.</p>", 400)
        pending = await run_in_threadpool(store.request, ident)
        if pending is None:
            return _page("Connector request expired", "<p>Start adding the connector again from Claude.</p>", 400)
        client_id, params = pending
        resource = params.get("resource") or "every connector"
        if request.method == "GET":
            body = (f"<p>Claude asks to use <b>{html.escape(str(resource))}</b> on your behalf, through this "
                    f"workspace. Its tools run on your computer with your logins.</p>"
                    f"<form method=post action='/oauth/consent'><input type=hidden name=request value='{ident}'>"
                    f"<button name=decision value=allow>Allow</button> "
                    f"<button name=decision value=deny>Deny</button></form>")
            return _page("Connect Claude", body)
        if request.headers.getlist("origin") != [origin]:
            return _page("Request refused", "<p>The answer did not come from this workspace's page.</p>", 403)
        if auth.session(request.cookies.get(COOKIE)) is None:
            return _page("Pair this browser first",
                         "<p>This browser is not signed in to the workspace. Open a pairing link from the "
                         f"workspace on your phone (Devices, Pair) in this browser, then <a href='/oauth/consent?"
                         f"request={ident}'>come back to this request</a>.</p>", 401)
        await run_in_threadpool(store.request, ident, consume=True)
        redirect = params["redirect_uri"]
        if (await request.form()).get("decision") != "allow":
            target = construct_redirect_uri(redirect, error="access_denied", state=params.get("state"))
        else:
            code = await run_in_threadpool(store.grant, "code", client_id, {
                "scopes": params.get("scopes") or [SCOPE], "code_challenge": params["code_challenge"],
                "redirect_uri": redirect, "redirect_uri_provided_explicitly": params["redirect_uri_provided_explicitly"],
                "resource": params.get("resource")}, CODE_SECONDS)
            target = construct_redirect_uri(redirect, code=code, state=params.get("state"))
        # The form's redirect leaves this origin, so this response widens form-action to that one site.
        request.scope["oneconv.form_action"] = "{0.scheme}://{0.netloc}".format(urlsplit(redirect))
        return RedirectResponse(target, status_code=303)

    async def node_route(request, work):
        try:
            node = await run_in_threadpool(hub.authenticated_node, request)
            return JSONResponse(await work(node))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)

    async def publish(request):
        async def work(node):
            body = await bounded_json(request, limit=8 * MAX_CATALOG)
            return await run_in_threadpool(store.publish, node, body.get("catalogs"))
        return await node_route(request, work)

    async def take(request):
        async def work(node):
            try:
                wait = float(request.query_params.get("wait", "0"))
            except ValueError:
                raise HubError(400, "wait is a number of seconds") from None
            return {"calls": await run_in_threadpool(store.take, node, wait)}
        return await node_route(request, work)

    async def finish(request):
        async def work(node):
            body = await bounded_json(request, limit=2 * MAX_RESULT)
            await run_in_threadpool(store.finish, node, request.path_params["ident"], body.get("result"))
            return {"ok": True}
        return await node_route(request, work)

    async def listing(request):
        async def work(node):
            return {"connectors": [{**row, "url": f"{origin}/mcp/{row['cli']}"}
                                   for row in await run_in_threadpool(store.published)]}
        return await node_route(request, work)

    from mcp.server.auth.routes import MetadataHandler, build_metadata, cors_middleware
    from pydantic import AnyHttpUrl
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE])
    revocation = RevocationOptions(enabled=True)
    routes = create_auth_routes(provider, issuer_url=AnyHttpUrl(origin),
                                client_registration_options=registration, revocation_options=revocation)
    # Boat's port proxy answers /register itself, so registration lives at REGISTER and the
    # metadata clients discover it from says so.
    metadata = build_metadata(AnyHttpUrl(origin), None, registration, revocation)
    metadata.registration_endpoint = AnyHttpUrl(origin + REGISTER)
    routes = [Route(REGISTER, endpoint=route.endpoint, methods=list(route.methods)) if route.path == "/register"
              else Route(route.path, endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
                         methods=["GET", "OPTIONS"]) if route.path == "/.well-known/oauth-authorization-server"
              else route for route in routes]
    routes += [
        Route("/.well-known/oauth-protected-resource", resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp/{cli}", resource_metadata, methods=["GET"]),
        Route("/oauth/consent", consent, methods=["GET", "POST"]),
        Route("/mcp/{cli}", Endpoint(), methods=["GET", "POST", "DELETE"]),
        Route("/v1/connectors", publish, methods=["PUT"]),
        Route("/v1/connectors", listing, methods=["GET"]),
        Route("/v1/connector-calls", take, methods=["GET"]),
        Route("/v1/connector-calls/{ident}", finish, methods=["POST"]),
    ]
    app.router.routes.extend(routes)

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(current):
        async with original(current), manager.run():
            sweeper = asyncio.create_task(_sweep(store))
            try:
                yield
            finally:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper

    app.router.lifespan_context = lifespan
    app.state.connectors = store
    return store


async def _sweep(store: ConnectorStore) -> None:
    while True:
        try:
            await run_in_threadpool(store.prune)
        except OSError as error:
            logger.error("Connector cleanup failed (%s)", type(error).__name__)
        await asyncio.sleep(300)
