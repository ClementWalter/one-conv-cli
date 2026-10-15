"""`one-conv live web`: a read-only local page for a person to follow what the agents are doing.

Channels are topics, people are running sessions with their chosen names, and direct
messages come from the message log every send writes. The server answers GET only, only
for a localhost Host header (so another site cannot reach it through DNS rebinding), and
the page renders every message as text, never as markup.
"""

import json
import time
from collections import defaultdict

from . import router, team
from .model import live_home

# The viewer is a reader of its own: it never moves an agent's catch-up position.
VIEWER = "web-viewer"
MESSAGES_SHOWN = 300


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
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

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

    return Starlette(routes=[
        Route("/", guarded(lambda request: PAGE, "text/html; charset=utf-8")),
        Route("/app.js", guarded(lambda request: SCRIPT, "text/javascript; charset=utf-8")),
        Route("/app.css", guarded(lambda request: STYLE, "text/css; charset=utf-8")),
        Route("/api/state", guarded(lambda request: state())),
        Route("/api/topics/{topic}", guarded(lambda request: topic_messages(request.path_params["topic"]))),
        Route("/api/direct", guarded(lambda request: direct())),
    ])


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>one-conv live</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<aside>
  <h1>one-conv live</h1>
  <p id="bridge" class="muted"></p>
  <h2>Channels</h2><ul id="topics"></ul>
  <h2>Direct messages</h2><ul id="threads"></ul>
  <h2>People</h2><ul id="people"></ul>
</aside>
<main>
  <header><h2 id="title">Pick a channel</h2><p id="subtitle" class="muted"></p></header>
  <ol id="messages"></ol>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

STYLE = """
:root { --bg: #ffffff; --fg: #1d1c1d; --muted: #616061; --side: #3f0e40; --side-fg: #e8dfe8;
        --active: #1164a3; --line: #e2e2e2; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1a1d21; --fg: #d1d2d3; --muted: #9a9b9e; --side: #19171d; --side-fg: #cfc3cf;
          --active: #1164a3; --line: #35373b; }
}
* { box-sizing: border-box; }
body { margin: 0; display: flex; height: 100vh; font: 15px/1.45 -apple-system, system-ui, sans-serif;
       background: var(--bg); color: var(--fg); }
aside { width: 270px; flex: none; background: var(--side); color: var(--side-fg); padding: 16px; overflow-y: auto; }
aside h1 { font-size: 18px; margin: 0 0 4px; }
aside h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; margin: 20px 0 6px; opacity: .8; }
aside ul { list-style: none; margin: 0; padding: 0; }
aside li { padding: 4px 8px; border-radius: 6px; cursor: pointer; overflow: hidden; text-overflow: ellipsis;
           white-space: nowrap; }
aside li:hover { background: rgba(255,255,255,.08); }
aside li.active { background: var(--active); color: #fff; }
aside li.person { cursor: default; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: #8a8a8a; }
.dot.on { background: #2bac76; }
main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
header { padding: 14px 20px; border-bottom: 1px solid var(--line); }
header h2 { margin: 0; font-size: 17px; }
.muted { color: var(--muted); margin: 2px 0 0; font-size: 13px; }
aside .muted { color: var(--side-fg); opacity: .7; }
ol { list-style: none; margin: 0; padding: 12px 20px; overflow-y: auto; flex: 1; }
ol li { padding: 8px 0; border-bottom: 1px solid var(--line); }
.who { font-weight: 600; }
.when { color: var(--muted); font-size: 12px; margin-left: 8px; }
.status { color: var(--muted); font-size: 12px; margin-left: 8px; }
.text { white-space: pre-wrap; word-break: break-word; margin-top: 2px; }
@media (max-width: 700px) { body { flex-direction: column; } aside { width: auto; max-height: 40vh; } }
"""

SCRIPT = r"""
"use strict";
let view = null;
let names = {};

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function label(address) {
  if (!address) return "unknown";
  const tail = String(address).split("/").pop();
  for (const [name, bound] of Object.entries(names)) {
    if (bound === address || bound === tail) return "@" + name;
  }
  return String(address);
}
function when(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "";
}
async function get(path) {
  const response = await fetch(path, { headers: { "Accept": "application/json" } });
  return response.json();
}
function select(kind, key) {
  view = { kind, key };
  refreshView();
  refreshSide();
}
function item(list, text, active, onclick, cls) {
  const li = el("li", text, cls);
  if (active) li.classList.add("active");
  if (onclick) li.addEventListener("click", onclick);
  list.appendChild(li);
  return li;
}
async function refreshSide() {
  const state = await get("/api/state");
  names = state.names || {};
  const bridge = document.getElementById("bridge");
  bridge.textContent = state.bridge ? ("bridge " + (state.bridge.connected ? "connected" : "not connected") +
    (state.bridge.node ? " as " + state.bridge.node : "")) : "no bridge: local topics only";
  const topics = document.getElementById("topics");
  topics.replaceChildren();
  for (const topic of state.topics || []) {
    item(topics, "# " + topic.topic + "  (" + topic.members.length + ")",
         view && view.kind === "topic" && view.key === topic.topic, () => select("topic", topic.topic));
  }
  if (!(state.topics || []).length) topics.appendChild(el("li", "none yet", "person"));
  const people = document.getElementById("people");
  people.replaceChildren();
  for (const session of state.sessions || []) {
    const li = item(people, "", false, null, "person");
    li.appendChild(el("span", "", "dot" + (session.status === "idle" || session.status === "busy" ? " on" : "")));
    li.appendChild(el("span", session.name ? "@" + session.name : (session.title || session.address)));
    li.title = session.address + (session.status ? " (" + session.status + ")" : "");
  }
  const direct = await get("/api/direct");
  const threads = document.getElementById("threads");
  threads.replaceChildren();
  for (const thread of direct.threads || []) {
    const [a, b] = thread.pair.split(" ");
    item(threads, label(a) + " / " + label(b), view && view.kind === "direct" && view.key === thread.pair,
         () => select("direct", thread.pair));
  }
  if (!(direct.threads || []).length) threads.appendChild(el("li", "none yet", "person"));
}
function render(rows, subtitle) {
  const list = document.getElementById("messages");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.replaceChildren();
  for (const row of rows) {
    const li = el("li");
    li.appendChild(el("span", label(row.from), "who"));
    if (row.to && view.kind === "direct") li.appendChild(el("span", "to " + label(row.to), "status"));
    li.appendChild(el("span", when(row.at), "when"));
    if (row.status && typeof row.status === "string") li.appendChild(el("span", row.status, "status"));
    li.appendChild(el("div", row.text, "text"));
    list.appendChild(li);
  }
  if (!rows.length) list.appendChild(el("li", "No messages yet.", "muted"));
  document.getElementById("subtitle").textContent = subtitle || "";
  if (atBottom) list.scrollTop = list.scrollHeight;
}
async function refreshView() {
  if (!view) return;
  if (view.kind === "topic") {
    document.getElementById("title").textContent = "# " + view.key;
    const data = await get("/api/topics/" + encodeURIComponent(view.key));
    render(data.messages || [], data.error || (data.omitted ? data.omitted + " older messages not shown" : ""));
  } else {
    const [a, b] = view.key.split(" ");
    document.getElementById("title").textContent = label(a) + " / " + label(b);
    const direct = await get("/api/direct");
    const thread = (direct.threads || []).find((t) => t.pair === view.key);
    render(thread ? thread.messages : [], a + "  ·  " + b);
  }
}
refreshSide();
setInterval(refreshSide, 5000);
setInterval(refreshView, 3000);
"""
