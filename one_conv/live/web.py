"""`one-conv live web`: a local workspace to follow agents and send human messages.

Channels are topics, people are running sessions with their chosen names, and direct
messages come from the message log every send writes. Reads never move an agent's catch-up
position. Writes require a per-instance token and the same loopback origin; all routes
require a localhost Host header. The page renders messages as text, never as markup.
"""

import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from . import router, team, web_send
from .model import live_home

# The viewer is a reader of its own: it never moves an agent's catch-up position.
VIEWER = "web-viewer"
MESSAGES_SHOWN = 300
logger = logging.getLogger(__name__)


def _bridge_state() -> dict | None:
    try:
        state = json.loads((live_home() / "bridge" / "state.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    # The token lives in hub.json, but keep anything credential-like out of the page anyway.
    return {key: value for key, value in state.items() if "token" not in key.lower()}


def state() -> dict:
    names = team.names()
    by_address = {address: name for name, address in names.items()}
    try:
        sessions = [{**session.to_json(), "name": by_address.get(session.address)}
                    for session in router.list_all()]
    except (OSError, RuntimeError) as error:
        sessions, error_text = [], str(error)
    else:
        error_text = None
    try:
        topics = team.topics()
    except team.TeamError as error:
        topics, error_text = [], str(error)
    return {"at": time.time(), "sessions": sessions, "names": names, "topics": topics,
            "bridge": _bridge_state(), "error": error_text}


def topic_messages(topic: str) -> dict:
    result = team.catch_up(topic, VIEWER, everything=True, peek=True, limit=MESSAGES_SHOWN)
    return {"topic": result["topic"], "messages": result["messages"], "omitted": result["omitted"]}


def log() -> list[dict]:
    try:
        lines = (live_home() / "messages.jsonl").read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-MESSAGES_SHOWN:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A corrupt line must not hide the rest of the log.
        if isinstance(row, dict):
            rows.append(row)
    return rows


def direct() -> dict:
    """Direct messages grouped by the pair of sessions that exchanged them."""
    threads: dict[str, list] = defaultdict(list)
    for row in log():
        if row.get("topic") or str(row.get("to", "")).startswith("#"):
            continue
        pair = " ".join(sorted((str(row.get("from")), str(row.get("to")))))
        threads[pair].append(row)
    return {"threads": [{"pair": pair, "messages": rows, "last": rows[-1].get("at")}
                        for pair, rows in sorted(threads.items(), key=lambda item: -(item[1][-1].get("at") or 0))]}


SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def application(port: int):
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import ClientDisconnect
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    token = secrets.token_urlsafe(32)

    def guarded(work, media_type="application/json"):
        async def endpoint(request):
            if request.headers.get("host") not in allowed:
                return Response("unknown host", status_code=421, headers=SECURITY_HEADERS)
            try:
                body = work(request)
            except (team.TeamError, router.LiveError) as error:
                return JSONResponse({"error": str(error)}, status_code=400, headers=SECURITY_HEADERS)
            if media_type == "application/json":
                return JSONResponse(body, headers=SECURITY_HEADERS)
            return Response(body, media_type=media_type, headers=SECURITY_HEADERS)
        return endpoint

    async def message(request):
        host = request.headers.get("host")
        if host not in allowed:
            return JSONResponse({"error": "Open the workspace through its localhost address."},
                                status_code=421, headers=SECURITY_HEADERS)
        try:
            if request.headers.get("origin") != f"http://{host}":
                raise web_send.SendError("Send from the workspace's own browser tab.", 403)
            supplied = request.headers.get("x-one-conv-token", "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                raise web_send.SendError("Refresh the workspace before sending; its session token is missing or expired.",
                                         403)
            content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
            if content_type != "application/json" or request.headers.get("content-encoding", "identity") != "identity":
                raise web_send.SendError("Send a JSON message using Content-Type: application/json.", 415)
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    raise web_send.SendError("Content-Length must be a nonnegative byte count.") from None
                if length < 0:
                    raise web_send.SendError("Content-Length must be a nonnegative byte count.")
                if length > web_send.BODY_LIMIT:
                    raise web_send.SendError("This message is too large; shorten it before sending.", 413)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > web_send.BODY_LIMIT:
                    raise web_send.SendError("This message is too large; shorten it before sending.", 413)
                body.extend(chunk)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                raise web_send.SendError("The message must be valid UTF-8 JSON.") from None
            status, result = await run_in_threadpool(web_send.send, payload)
        except web_send.SendError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status, headers=SECURITY_HEADERS)
        except ClientDisconnect:
            return JSONResponse({"error": "The connection closed before the message was accepted."},
                                status_code=400, headers=SECURITY_HEADERS)
        except (OSError, sqlite3.Error):
            logger.exception("Could not open the human message journal")
            return JSONResponse({"error": "The message journal is unavailable. Check local disk access, "
                                          "then retry with the same message id."},
                                status_code=503, headers=SECURITY_HEADERS)
        return JSONResponse(result, status_code=status, headers=SECURITY_HEADERS)

    return Starlette(routes=[
        Route("/", guarded(lambda request: PAGE, "text/html; charset=utf-8")),
        Route("/app.js", guarded(lambda request: SCRIPT, "text/javascript; charset=utf-8")),
        Route("/app.css", guarded(lambda request: STYLE, "text/css; charset=utf-8")),
        Route("/api/state", guarded(lambda request: state())),
        Route("/api/topics/{topic}", guarded(lambda request: topic_messages(request.path_params["topic"]))),
        Route("/api/direct", guarded(lambda request: direct())),
        Route("/api/session", guarded(lambda request: {"csrf_token": token,
              "human": {"address": "user", "name": "You", "kind": "human"}})),
        Route("/api/messages", message, methods=["POST"]),
    ])


PAGE = Path(__file__).with_name("web_assets").joinpath("index.html").read_text(encoding="utf-8")
STYLE = Path(__file__).with_name("web_assets").joinpath("app.css").read_text(encoding="utf-8")
SCRIPT = Path(__file__).with_name("web_assets").joinpath("app.js").read_text(encoding="utf-8")
