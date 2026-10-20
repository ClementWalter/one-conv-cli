"""Serve the personal cloud workspace with paired browser sessions and a bearer-only hub."""

import asyncio
import importlib
import importlib.util
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from ..live import web
from ..live.hub import ATTACHMENT_LIMIT, ATTACHMENT_TYPES, Hub, HubError
from ..live.hub import application as hub_application
from .auth import COOKIE, AuthStore
from .store import HUMAN, ControlStore

# A change feed request waits at most this long, checking the store this often.
CHANGES_WAIT = 25.0
CHANGES_STEP = 0.25
CHANGES_AUTH_CHECK = 1.0

BODY_LIMIT = 65_536
PAIRING_SECONDS = 600
logger = logging.getLogger(__name__)
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    "Strict-Transport-Security": "max-age=31536000",
}


def canonical_origin(value: str) -> str:
    """An operator chooses the authority; forwarded request headers never choose it."""
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.netloc != parsed.netloc.lower() or re.search(r"\s", value)):
        raise ValueError("public_origin must be an exact canonical HTTPS origin")
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("HTTP is permitted only on loopback for local verification")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("public_origin has an invalid port") from None
    if parsed.netloc.endswith(":") or not parsed.netloc.isascii() or parsed.hostname.endswith("."):
        raise ValueError("public_origin must use a canonical ASCII authority")
    authority = parsed.netloc
    if (parsed.scheme, port) in (("https", 443), ("http", 80)):
        authority = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{authority}"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite JSON")


async def bounded_json(request):
    """Bound actual streamed bytes as well as the advertised length."""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json" or request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HubError(415, "Use uncompressed Content-Type: application/json.")
    declared = request.headers.getlist("content-length")
    if declared and (len(declared) != 1 or not re.fullmatch(r"[0-9]{1,12}", declared[0])):
        raise HubError(400, "Content-Length must be a nonnegative byte count.")
    if declared and int(declared[0]) > BODY_LIMIT:
        raise HubError(413, "Request body is too large.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > BODY_LIMIT:
            raise HubError(413, "Request body is too large.")
        body.extend(chunk)
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise HubError(400, "Use a valid UTF-8 JSON object without duplicate fields.") from None
    if not isinstance(payload, dict):
        raise HubError(400, "The request body must be a JSON object.")
    return payload


def application(store_dir: Path, public_origin: str, hub: Hub | None = None, *, providers=None, assets=None,
                session_seconds: int = 30 * 86400):
    """Build one workspace; providers receive (method, payload) behind browser auth."""
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import ClientDisconnect
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    origin = canonical_origin(public_origin)
    host = urlsplit(origin).netloc
    auth, store = AuthStore(store_dir, session_seconds=session_seconds), ControlStore(store_dir, hub)
    provider_map = dict(providers or {})
    if any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) or not callable(callback)
           for name, callback in provider_map.items()):
        raise ValueError("providers must map resource names to callable(method, payload)")

    async def maintain_mailbox():
        while True:
            try:
                await run_in_threadpool(store.refresh_human)
            except (OSError, sqlite3.Error, HubError) as error:
                logger.error("Cloud mailbox refresh failed (%s)", type(error).__name__)
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(maintain_mailbox())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def check_origin(request):
        if request.headers.getlist("origin") != [origin]:
            raise HubError(403, "Write from the workspace's canonical origin.")

    def session_for(request, *, required=True):
        cookie = request.cookies.get(COOKIE)
        session = auth.session(cookie)
        if required and session is None:
            raise HubError(401, "Pair this browser with the workspace.")
        return cookie, session

    def check_write(request):
        cookie, _ = session_for(request)
        check_origin(request)
        supplied = request.headers.getlist("x-one-conv-token")
        if len(supplied) != 1 or not auth.check_csrf(cookie, supplied[0]):
            raise HubError(403, "The workspace session token is missing or expired.")

    def endpoint(work, *, public=False, write=False):
        async def handle(request):
            try:
                if write:
                    check_write(request)
                elif not public:
                    session_for(request)
                return await work(request)
            except HubError as error:
                return JSONResponse({"error": str(error)}, status_code=error.status)
            except ClientDisconnect:
                return JSONResponse({"error": "Connection closed before the request was accepted."}, status_code=400)
            except (OSError, sqlite3.Error, RuntimeError, TypeError) as error:
                logger.error("Cloud request storage unavailable (%s)", type(error).__name__)
                return JSONResponse({"error": "Workspace storage is unavailable; retry with the same message id."}, status_code=503)
        return handle

    async def session(request):
        _, current = session_for(request, required=False)
        if current is None:
            return JSONResponse({"authenticated": False, "human": None})
        return JSONResponse({"authenticated": True, "csrf_token": current["csrf_token"],
                             "expires_at": current["expires_at"],
                             "human": {"address": "user", "route": HUMAN, "name": "You", "kind": "human"},
                             "capabilities": {"workers": "workers" in provider_map, "accounting": "accounting" in provider_map,
                                              "pairing": True, "channels": True, "changes": True,
                                              "unread": True, "acks": True, "sign_out_others": True}})

    async def pair_device(request):
        """An authenticated owner can connect another device without exporting its session cookie."""
        if await bounded_json(request):
            raise HubError(400, "Device pairing accepts an empty JSON object; access and origin are fixed.")
        expires = auth.clock() + PAIRING_SECONDS
        token = await run_in_threadpool(auth.issue_pairing_token, PAIRING_SECONDS)
        return JSONResponse({"url": origin + "/#pair=" + token, "expires_at": expires,
                             "single_use": True, "access": "owner"})

    async def sign_out_others(request):
        """The owner disconnects every other browser and cancels unused pairing links, keeping this one."""
        if await bounded_json(request):
            raise HubError(400, "Signing out other devices accepts an empty JSON object.")
        cookie, _ = session_for(request)
        return JSONResponse({"signed_out": await run_in_threadpool(auth.revoke_others, cookie)})

    async def login(request):
        check_origin(request)
        payload = await bounded_json(request)
        if set(payload) != {"token"}:
            raise HubError(400, "Provide only a pairing token.")
        cookie = await run_in_threadpool(auth.pair, payload["token"], request.cookies.get(COOKIE))
        if cookie is None:
            raise HubError(401, "The pairing link is invalid, expired or already used.")
        response = JSONResponse({"authenticated": True})
        response.set_cookie(COOKIE, cookie, max_age=auth.session_seconds, secure=True, httponly=True,
                            samesite="strict", path="/")
        return response

    async def logout(request):
        await bounded_json(request)
        await run_in_threadpool(auth.revoke, request.cookies.get(COOKIE))
        response = JSONResponse({"authenticated": False, "human": None})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    async def state(_request):
        return JSONResponse(await run_in_threadpool(store.state))

    async def changes(request):
        """Answer once the workspace differs from `since`, or after `wait` seconds; never changes state."""
        since, raw = request.query_params.get("since", ""), request.query_params.get("wait", str(CHANGES_WAIT))
        if not re.fullmatch(r"[0-9a-f]{0,32}", since):
            raise HubError(400, "since is a fingerprint from an earlier answer")
        if not re.fullmatch(r"[0-9]{1,2}(\.[0-9]{1,3})?", raw) or float(raw) > CHANGES_WAIT:
            raise HubError(400, f"wait is a number of seconds up to {CHANGES_WAIT:g}")
        loop = asyncio.get_running_loop()
        deadline, checked = loop.time() + float(raw), loop.time()
        while True:
            current = await run_in_threadpool(store.fingerprint)
            if since and current != since:
                break
            if not since or loop.time() >= deadline or await request.is_disconnected():
                break
            if loop.time() - checked >= CHANGES_AUTH_CHECK:
                # A device disconnected while this request waits stops getting answers within a second.
                await run_in_threadpool(session_for, request)
                checked = loop.time()
            await asyncio.sleep(CHANGES_STEP)
        await run_in_threadpool(session_for, request)
        return JSONResponse({"fp": current, "changed": bool(since) and current != since})

    async def topic(request):
        return JSONResponse(await run_in_threadpool(store.topic, request.path_params["topic"]))

    async def direct(_request):
        return JSONResponse(await run_in_threadpool(store.direct))

    async def message(request):
        payload = await bounded_json(request)
        return JSONResponse(await run_in_threadpool(store.send, payload))

    async def upload(request):
        """One image, sent as its own bytes; the answer's id goes into the message that carries it."""
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type not in ATTACHMENT_TYPES or request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HubError(415, "Attach a PNG, JPEG, GIF or WebP image, uncompressed.")
        declared = request.headers.getlist("content-length")
        if declared and (len(declared) != 1 or not re.fullmatch(r"[0-9]{1,12}", declared[0])):
            raise HubError(400, "Content-Length must be a nonnegative byte count.")
        if declared and int(declared[0]) > ATTACHMENT_LIMIT:
            raise HubError(413, "The image is too large.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > ATTACHMENT_LIMIT:
                raise HubError(413, "The image is too large.")
            body.extend(chunk)
        return JSONResponse(await run_in_threadpool(store.store_attachment, media_type, bytes(body)))

    async def attachment(request):
        media_type, body = await run_in_threadpool(store.attachment, request.path_params["ident"])
        return Response(body, media_type=media_type)

    async def read(request):
        payload = await bounded_json(request)
        return JSONResponse(await run_in_threadpool(store.mark_read, payload))

    async def channel(request):
        payload = await bounded_json(request)
        return JSONResponse(await run_in_threadpool(store.create_channel, payload))

    async def archive(request):
        payload = await bounded_json(request)
        return JSONResponse(await run_in_threadpool(store.archive_channel, payload))

    async def provider(request):
        callback = provider_map.get(request.path_params["name"])
        if callback is None:
            raise HubError(404, "No such workspace provider.")
        if request.method == "POST":
            check_write(request)
        payload = await bounded_json(request) if request.method == "POST" else dict(request.query_params)
        try:
            value = await run_in_threadpool(callback, request.method, payload)
        except ValueError as error:
            raise HubError(400, str(error)) from error
        return JSONResponse(value)

    async def health(_request):
        await run_in_threadpool(store.refresh_human)
        return JSONResponse({"ok": True})

    public_assets = {"/": (web.PAGE, "text/html"), "/app.css": (web.STYLE, "text/css"),
                     "/app.js": (web.SCRIPT, "text/javascript")}
    if importlib.util.find_spec("one_conv.live.pwa"):
        public_assets.update(importlib.import_module("one_conv.live.pwa").ASSETS)
    public_assets.update(assets or {})
    if any(not isinstance(path, str) or not path.startswith("/") or re.search(r"[{}?#]", path)
           or path.startswith(("/api", "/v1")) or path == "/health" for path in public_assets):
        raise ValueError("public assets must be exact static paths outside authenticated API namespaces")

    def asset_response(body, media_type, path):
        async def serve(_request):
            headers = {"Service-Worker-Allowed": "/"} if path == "/sw.js" else {}
            return Response(body, media_type=media_type, headers=headers)
        return serve

    routes = [Route(path, asset_response(body, media_type, path)) for path, (body, media_type) in public_assets.items()]
    routes.extend([
        Route("/health", endpoint(health, public=True)),
        Route("/api/session", endpoint(session, public=True)),
        Route("/api/login", endpoint(login, public=True), methods=["POST"]),
        Route("/api/devices/pair", endpoint(pair_device, write=True), methods=["POST"]),
        Route("/api/devices/sign-out-others", endpoint(sign_out_others, write=True), methods=["POST"]),
        Route("/api/logout", endpoint(logout, write=True), methods=["POST"]),
        Route("/api/state", endpoint(state)), Route("/api/changes", endpoint(changes)), Route("/api/topics/{topic}", endpoint(topic)),
        Route("/api/direct", endpoint(direct)), Route("/api/messages", endpoint(message, write=True), methods=["POST"]),
        Route("/api/attachments", endpoint(upload, write=True), methods=["POST"]),
        Route("/api/attachments/{ident}", endpoint(attachment)),
        Route("/api/channels", endpoint(channel, write=True), methods=["POST"]),
        Route("/api/channels/archive", endpoint(archive, write=True), methods=["POST"]),
        Route("/api/read", endpoint(read, write=True), methods=["POST"]),
        Route("/api/providers/{name}", endpoint(provider), methods=["GET", "POST"]),
        *hub_application(store.hub).routes,
    ])
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.auth, app.state.store, app.state.hub = auth, store, store.hub

    async def security(request, call_next):
        if request.headers.getlist("host") != [host]:
            response = JSONResponse({"error": "Unknown workspace authority."}, status_code=421)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=security)
    return app
