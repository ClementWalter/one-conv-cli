/* The workspace renders local message data with text nodes and sends only explicit human drafts. */
"use strict";

const $ = (id) => document.getElementById(id);
const model = {
  state: { topics: [], sessions: [], names: {} },
  threads: [],
  rows: [],
  view: null,
  token: null,
  sending: false,
  pending: new Map(),
  signature: "",
  first: true,
  refreshing: false,
  expanded: new Set(),
  drafts: new Map(),
  details: false,
  searchGeneration: 0,
  authenticated: false,
  capabilities: {},
  workers: { jobs: [], pools: [], profiles: [] },
  costs: null,
  staffing: false,
  reportReceivedAt: null,
};
const harnesses = {
  claude: "Claude",
  codex: "Codex",
  omp: "Oh My Pi",
  cursor: "Cursor",
  boat: "Boat",
};
const pairingFragment = new URLSearchParams(location.hash.slice(1));
let pairingToken = pairingFragment.get("pair");
// Invitation secrets never remain in navigation history or reach later requests.
if (pairingFragment.has("pair")) history.replaceState(null, "", location.pathname + location.search);
let installPrompt = null;
let toastTimer;
let deviceInvitation = null;
let invitationGeneration = 0;
let navigation = {};
try {
  const saved = JSON.parse(localStorage.getItem("oneconv:navigation"));
  if (saved && typeof saved === "object" && !Array.isArray(saved)) {
    if (validView(saved.view)) navigation.view = saved.view;
    for (const kind of ["topic", "direct"])
      if (validView({ kind, key: saved[kind] })) navigation[kind] = saved[kind];
  }
} catch (_) { /* Route restoration is optional when browser storage is unavailable. */ }

function validView(value) {
  return value && ["topic", "direct", "activity", "home"].includes(value.kind) &&
    typeof value.key === "string" && value.key.length <= 2500 &&
    (["activity", "home"].includes(value.kind) || value.key.length > 0);
}
function rememberView(view) {
  if (!validView(view)) return;
  navigation.view = { ...view };
  if (view.kind === "topic" || view.kind === "direct") navigation[view.kind] = view.key;
  // Only stable route IDs persist; message contents and drafts remain outside browser storage.
  try { localStorage.setItem("oneconv:navigation", JSON.stringify(navigation)); } catch (_) { /* Storage can be unavailable. */ }
}
function requestedView() {
  const params = new URLSearchParams(location.hash.slice(1));
  for (const kind of ["topic", "direct", "activity", "home"]) {
    const view = { kind, key: params.get(kind) || "" };
    if (params.has(kind) && validView(view)) return view;
  }
  return validView(navigation.view) ? navigation.view : null;
}
function savedRoute(kind) {
  const view = { kind, key: navigation[kind] };
  return validView(view) ? view : null;
}

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function icon(name, extra = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.classList.add("icon");
  if (extra) node.classList.add(extra);
  node.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  node.appendChild(use);
  return node;
}
function isHuman(address) {
  return address === "user" || String(address).endsWith("/user");
}
function person(address) {
  address = String(address || "unknown");
  if (isHuman(address))
    return {
      address,
      name: address === "user" ? "You" : "Human · " + address.split("/")[0],
      kind: "Human",
      color: "human-avatar",
      initials: "Y",
      status: "human",
    };
  const slash = address.indexOf("/");
  const local = slash > 0 ? address.slice(slash + 1) : address;
  const localNode = model.state.bridge && model.state.bridge.node;
  const localIdentity = slash < 0 || address.slice(0, slash) === localNode;
  const session = model.state.sessions.find(
    (s) => s.address === address || (localIdentity && s.address === local),
  );
  const kind = local.split(":")[0];
  const alias = Object.entries(model.state.names || {}).find(
    ([, bound]) => bound === address || (localIdentity && bound === local),
  );
  const boundName = (session && session.name) || (alias && alias[0]);
  const name = boundName ? "@" + String(boundName).replace(/^@/, "") :
    (harnesses[kind] || "Agent") + " · " + (local.split(":").slice(1).join(":") || local).slice(0, 8);
  return {
    address,
    name,
    kind: harnesses[kind] || "Agent",
    color: kind,
    initials: alias
      ? name.slice(0, 2).toUpperCase()
      : { claude: "C", codex: "◈", omp: "π", cursor: "↗", boat: "B" }[kind] ||
        name.slice(0, 2).toUpperCase(),
    status: session && session.online !== false ? session.status : "offline",
    machine: (session && session.node) || (slash > 0 ? address.slice(0, slash) : localNode) || "This machine",
    project: session && session.cwd ? String(session.cwd).replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "",
    session,
  };
}
function presenceOf(p) {
  if (p.session && p.session.presence) {
    const presence = p.session.presence;
    const timestamps = [presence.last_seen, presence.bridge_last_seen].filter((value) => validDate(value));
    // A cached report expires even when subsequent workspace polls cannot reach the server.
    if (presence.state !== "offline" && timestamps.some((value) => reportTime() - Number(value) > 60))
      return { ...presence, state: "offline", label: "Offline", can_message: false,
        detail: "The last connection report expired. Check your connection or reconnect the bridge or agent." };
    return presence;
  }
  // A directory entry alone cannot establish a live native agent connection.
  const offline = !p.session || p.session.online === false;
  return { state: offline ? "offline" : "unverified", label: offline ? "Offline" : "Connection unverified",
    detail: offline ? "This session is not currently published." : "The agent connection has not been verified.",
    can_message: !offline, last_seen: null, bridge_last_seen: null };
}
function reportTime() {
  const at = Number(model.state.at);
  // Monotonic elapsed time ages server evidence independently of the device wall clock.
  return at > 0 && Number.isFinite(at) ? at + (model.reportReceivedAt === null ? 0 :
    Math.max(0, performance.now() - model.reportReceivedAt) / 1000) : Date.now() / 1000;
}
function freshness(seconds) {
  const date = validDate(seconds);
  if (!date) return "time unknown";
  const age = Math.max(0, Math.floor(reportTime() - date.getTime() / 1000));
  return age < 60 ? age + "s ago" : age < 3600 ? Math.floor(age / 60) + "m ago" :
    age < 86400 ? Math.floor(age / 3600) + "h ago" : Math.floor(age / 86400) + "d ago";
}
function presenceText(p) {
  const presence = presenceOf(p);
  const label = { idle: "Connected · idle", busy: "Busy", unverified: "Connection unverified", offline: "Offline" }[presence.state] || presence.label;
  return label + (validDate(presence.last_seen) ? " · " + freshness(presence.last_seen) : "");
}
function personContext(p) {
  return [p.kind, p.machine, p.project].filter(Boolean).join(" · ");
}
function avatar(address, size = "") {
  const p = person(address);
  const node = el(
    "span",
    p.initials,
    "avatar " + p.color + (size ? " " + size : ""),
  );
  node.title = p.name + " · " + p.kind;
  node.setAttribute("aria-hidden", "true");
  return node;
}
function pairOf(target) {
  return ["user", target].sort().join(" ");
}
function pairName(pair) {
  return String(pair)
    .split(" ")
    .map((address) => person(address).name)
    .join(" / ");
}
function directTitle(pair) {
  const addresses = pair.split(" ");
  return addresses.includes("user")
    ? person(addresses.find((address) => address !== "user")).name
    : pairName(pair);
}
function currentTopic() {
  return model.state.topics.find(
    (t) => model.view && t.topic === model.view.key,
  );
}
function members() {
  if (!model.view) return [];
  if (model.view.kind === "topic") return (currentTopic() || {}).members || [];
  if (model.view.kind === "direct") return model.view.key.split(" ");
  return model.state.sessions.map((s) => s.address);
}
function target() {
  if (!model.view) return null;
  if (model.view.kind === "topic") return "#" + model.view.key;
  if (
    model.view.kind === "direct" &&
    model.view.key.split(" ").includes("user")
  )
    return model.view.key.split(" ").find((a) => a !== "user");
  return null;
}
function validDate(seconds) {
  const date = new Date(Number(seconds) * 1000);
  return Number.isFinite(date.getTime()) && Number(seconds) > 0 ? date : null;
}
function clock(seconds) {
  const date = validDate(seconds);
  return date
    ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : "";
}
function dayKey(seconds) {
  const date = validDate(seconds);
  return date ? date.toDateString() : "earlier";
}
function dayLabel(seconds) {
  const date = validDate(seconds);
  if (!date) return "Earlier";
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return "Today";
  today.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Yesterday";
  return date.toLocaleDateString([], {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
}
async function get(path) {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const result = await response.json();
  if (response.status === 401) authentication(false);
  if (!response.ok || result.error)
    throw new Error(result.error || "The workspace could not be reached.");
  return result;
}
function notice(text, error = false) {
  $("notice").hidden = !text;
  $("notice").textContent = text || "";
  $("notice").classList.toggle("error", error);
}
function toast(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 3000);
}
function connected(ok) {
  const node = $("connection");
  node.classList.toggle("offline", !ok);
  node.lastElementChild.textContent = ok ? "Workspace live" : "Reconnecting";
  $("live-badge").hidden = !ok;
}
function rowButton(list, label, mark, active, action, count) {
  const li = el("li");
  const button = el(
    "button",
    null,
    "conversation-row" + (active ? " active" : ""),
  );
  button.type = "button";
  if (active) button.setAttribute("aria-current", "page");
  if (mark) button.appendChild(mark);
  button.appendChild(el("span", label, "row-label"));
  if (count !== undefined) {
    const amount = el("span", count, "row-count");
    amount.title = count + " members";
    button.appendChild(amount);
  }
  button.addEventListener("click", action);
  li.appendChild(button);
  list.appendChild(li);
  return button;
}
function sidebar() {
  const topics = $("topics");
  topics.replaceChildren();
  for (const topic of model.state.topics)
    rowButton(
      topics,
      topic.topic,
      el("span", "#", "hash"),
      model.view &&
        model.view.kind === "topic" &&
        model.view.key === topic.topic,
      () => select("topic", topic.topic),
      (topic.members || []).length,
    );
  if (!topics.children.length)
    topics.appendChild(
      el(
        "li",
        "Channels will appear here when your team starts a topic.",
        "sidebar-empty",
      ),
    );
  $("topic-count").textContent = model.state.topics.length || "";
  const threads = $("threads");
  threads.replaceChildren();
  for (const thread of model.threads) {
    const pair = thread.pair.split(" ");
    const other = pair.find((address) => address !== "user") || pair[0];
    const label = pair.includes("user")
      ? person(other).name
      : pairName(thread.pair);
    rowButton(
      threads,
      label,
      avatar(other, "tiny-avatar"),
      model.view &&
        model.view.kind === "direct" &&
        model.view.key === thread.pair,
      () => select("direct", thread.pair),
    );
  }
  if (!threads.children.length)
    threads.appendChild(
      el("li", "A direct line to your agents.", "sidebar-empty"),
    );
  $("direct-count").textContent = model.threads.length || "";
  const people = $("people");
  people.replaceChildren();
  const offlinePeople = $("offline-people");
  offlinePeople.replaceChildren();
  for (const session of model.state.sessions) {
    const p = person(session.address);
    const state = presenceOf(p);
    const button = rowButton(
      state.state === "offline" ? offlinePeople : people,
      p.name,
      avatar(session.address, "tiny-avatar"),
      false,
      () => select("direct", pairOf(session.address)),
    );
    const label = button.querySelector(".row-label");
    label.classList.add("agent-label");
    label.replaceChildren(
      el("span", p.name, "agent-name"),
      el("small", personContext(p), "agent-context"),
    );
    if (session.title) label.appendChild(el("small", "Context: " + session.title, "agent-context agent-session-context"));
    label.appendChild(el("small", presenceText(p), "agent-context agent-state"));
    const presence = el(
      "span",
      null,
      "row-presence " + state.state,
    );
    presence.title = state.label;
    button.appendChild(presence);
    button.title = p.name + " · " + personContext(p) + " · " + presenceText(p) + "\n" + state.detail;
  }
  if (!people.children.length)
    people.appendChild(
      el("li", "No current agent connections are reported.", "sidebar-empty"),
    );
  $("agent-count").textContent = model.state.sessions.filter((s) => presenceOf(person(s.address)).state !== "offline").length || "";
  $("offline-count").textContent = offlinePeople.children.length || "";
  $("offline-section").hidden = !offlinePeople.children.length;
  $("all-activity").classList.toggle(
    "active",
    model.view && model.view.kind === "activity",
  );
}
function header() {
  const view = model.view;
  if (!view) return;
  const title =
    view.kind === "topic"
      ? view.key
      : view.kind === "direct"
        ? directTitle(view.key)
        : view.kind === "search"
          ? "Search results"
          : view.kind === "activity"
            ? "All activity"
            : "Your workspace";
  $("title").textContent = title;
  $("conversation-mark").replaceChildren(
    icon(
      view.kind === "topic"
        ? "hash"
        : view.kind === "direct"
          ? "chat"
          : view.kind === "search"
            ? "search"
            : "activity",
    ),
  );
  const current = members();
  $("subtitle").textContent =
    view.kind === "topic"
      ? current.length +
        " member" +
        (current.length === 1 ? "" : "s") +
        " · A shared space for your team"
      : view.kind === "direct"
        ? view.key.split(" ").includes("user")
          ? presenceText(person(view.key.split(" ").find((address) => address !== "user")))
          : "An agent-to-agent conversation"
        : view.kind === "search"
          ? "Across your workspace"
          : "Keep a pulse on the conversations that matter";
  $("member-avatars").replaceChildren(
    ...current.slice(0, 3).map((address) => avatar(address)),
  );
  $("member-count").textContent = current.length || "";
  $("members-toggle").hidden = !current.length;
  document.title = (view.kind === "topic" ? "# " : "") + title + " · OneConv";
  if (model.details) renderDetails();
  updateComposer();
}
function intro() {
  const box = $("conversation-intro");
  box.replaceChildren();
  const view = model.view;
  if (!view) return;
  const mark = el("div", view.kind === "topic" ? "#" : null, "intro-mark");
  if (view.kind !== "topic")
    mark.appendChild(
      icon(
        view.kind === "direct"
          ? "chat"
          : view.kind === "search"
            ? "search"
            : "activity",
      ),
    );
  box.appendChild(mark);
  const name =
    view.kind === "topic"
      ? "# " + view.key
      : view.kind === "direct"
        ? directTitle(view.key)
        : view.kind === "search"
          ? "Looking for something?"
          : view.kind === "activity"
            ? "The conversation, at a glance."
            : "Good work starts with a conversation.";
  box.appendChild(el("h3", name));
  box.appendChild(
    el(
      "p",
      view.kind === "topic"
        ? "A place to bring the team together. Share an idea, ask a question, or follow the work as it happens."
        : view.kind === "direct"
          ? "A little context goes a long way. Messages and replies stay together here."
          : view.kind === "search"
            ? "Find the decisions, details, and people behind the work."
            : "You and your agents, in one place. Pick a channel to catch up, or start a direct conversation.",
    ),
  );
  if (view.kind === "topic") {
    const chips = el("div", null, "intro-chips");
    const chip = el("span", null, "intro-chip");
    chip.append(
      icon("people"),
      document.createTextNode(members().length + " members"),
    );
    const live = el("span", null, "intro-chip");
    live.append(icon("bolt"), document.createTextNode("Live conversation"));
    chips.append(chip, live);
    box.appendChild(chips);
  }
}
function inline(parent, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*]+\*\*|\*[^*\n]+\*|https?:\/\/[^\s<>]+)/g;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    parent.appendChild(document.createTextNode(text.slice(start, match.index)));
    const value = match[0];
    if (value.startsWith("`"))
      parent.appendChild(el("code", value.slice(1, -1)));
    else if (value.startsWith("**"))
      parent.appendChild(el("strong", value.slice(2, -2)));
    else if (value.startsWith("*"))
      parent.appendChild(el("em", value.slice(1, -1)));
    else {
      const link = el("a", value);
      link.href = value;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.appendChild(link);
    }
    start = match.index + value.length;
  }
  parent.appendChild(document.createTextNode(text.slice(start)));
}
function messageText(container, text) {
  container.replaceChildren();
  let paragraph = [],
    bullets = [],
    code = null;
  function flush() {
    if (paragraph.length) {
      const p = el("p");
      inline(p, paragraph.join("\n"));
      container.appendChild(p);
      paragraph = [];
    }
    if (bullets.length) {
      const ul = el("ul");
      for (const value of bullets) {
        const li = el("li");
        inline(li, value);
        ul.appendChild(li);
      }
      container.appendChild(ul);
      bullets = [];
    }
  }
  for (const line of String(text).split("\n")) {
    if (line.trim().startsWith("```")) {
      if (code !== null) {
        container
          .appendChild(el("pre"))
          .appendChild(el("code", code.join("\n")));
        code = null;
      } else {
        flush();
        code = [];
      }
    } else if (code !== null) code.push(line);
    else if (!line.trim()) flush();
    else if (/^\s*[-*] /.test(line)) {
      if (paragraph.length) flush();
      bullets.push(line.replace(/^\s*[-*] /, ""));
    } else {
      if (bullets.length) flush();
      paragraph.push(line);
    }
  }
  if (code !== null)
    container.appendChild(el("pre")).appendChild(el("code", code.join("\n")));
  flush();
}
function renderRows(rows, force = false) {
  conversationResponse(rows);
  const signature = JSON.stringify([model.view, rows, model.state.names, model.state.sessions.map((s) => [s.address, s.name])]);
  if (!force && signature === model.signature) return;
  const scroll = $("message-scroll");
  const follow =
    model.first ||
    scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 110;
  const previousTop = scroll.scrollTop;
  const previousCount = model.rows.length;
  model.rows = rows;
  model.signature = signature;
  const list = $("messages");
  list.replaceChildren();
  let lastDay = null,
    previous = null;
  for (const row of rows) {
    const day = dayKey(row.at);
    if (day !== lastDay) {
      const divider = el("li", null, "day-divider");
      divider.appendChild(el("span", dayLabel(row.at)));
      list.appendChild(divider);
      lastDay = day;
    }
    const continuation =
      previous &&
      previous.from === row.from &&
      dayKey(previous.at) === day &&
      Math.abs(Number(row.at) - Number(previous.at)) < 300 &&
      !row.source;
    const p = person(row.from);
    const item = el(
      "li",
      null,
      "message" + (continuation ? " continuation" : ""),
    );
    item.dataset.messageId = row.id || "";
    item.appendChild(
      continuation
        ? el("span", clock(row.at), "continuation-time")
        : avatar(row.from),
    );
    const content = el("div", null, "message-content");
    if (!continuation) {
      const meta = el("div", null, "message-meta");
      const author = el("span", p.name, "message-author");
      author.title = p.address;
      const time = el("time", clock(row.at), "message-time");
      const date = validDate(row.at);
      if (date) {
        time.dateTime = date.toISOString();
        time.title = date.toLocaleString();
      }
      meta.append(
        author,
        el(
          "span",
          isHuman(row.from) ? "Human" : p.kind,
          "message-harness" + (isHuman(row.from) ? " human" : ""),
        ),
        time,
      );
      content.appendChild(meta);
    }
    const body = el("div", null, "message-body");
    const text = String(row.text || "");
    const key = row.id || [row.from, row.at, text.slice(0, 20)].join(":");
    const long = text.length > 800;
    function fill() {
      const open = model.expanded.has(key);
      messageText(
        body,
        long && !open ? text.slice(0, 680).trimEnd() + "…" : text,
      );
      if (long) {
        const more = el(
          "button",
          open ? "Show less" : "Read more",
          "show-more",
        );
        more.type = "button";
        more.setAttribute("aria-expanded", String(open));
        more.addEventListener("click", () => {
          if (model.expanded.has(key)) model.expanded.delete(key);
          else model.expanded.add(key);
          fill();
        });
        body.appendChild(more);
      }
    }
    fill();
    content.appendChild(body);
    if (row.mode === "fyi") {
      const note = el("span", null, "message-note");
      note.append(
        icon("check"),
        document.createTextNode("FYI · no notification"),
      );
      content.appendChild(note);
    }
    if (row.source) {
      const source = el("div", row.source, "message-source");
      content.appendChild(source);
    }
    if (isHuman(row.from)) {
      const delivery = row.delivery && typeof row.delivery === "object" ? row.delivery : null;
      const label = delivery && { stored: "Saved · awaiting delivery", accepted: "Delivery accepted", unconfirmed: "Unconfirmed", refused: "Refused" }[delivery.state];
      const status = el("small", label || (delivery ? delivery.label : "Stored on hub"), "message-delivery");
      status.title = delivery ? delivery.detail : "Stored in this conversation. No read receipt is available.";
      content.appendChild(status);
    }
    item.appendChild(content);
    list.appendChild(item);
    previous = row;
  }
  if (!rows.length)
    list.appendChild(
      el(
        "li",
        model.view && model.view.kind === "search"
          ? "No messages match your search. Try a different word or a channel name."
          : "A fresh conversation. Be the first to say something.",
        "message-empty",
      ),
    );
  if (follow) {
    scroll.scrollTop = scroll.scrollHeight;
    $("new-messages").hidden = true;
  } else {
    scroll.scrollTop = previousTop;
    if (rows.length > previousCount) $("new-messages").hidden = false;
  }
  model.first = false;
}
function conversationResponse(rows) {
  const node = $("conversation-response");
  node.hidden = true;
  if (!model.view || model.view.kind !== "direct" || !model.view.key.split(" ").includes("user")) return;
  const peer = model.view.key.split(" ").find((address) => address !== "user");
  const pairRows = rows.filter((row) => (row.from === "user" && row.to === peer) || (row.from === peer && row.to === "user"));
  const request = pairRows.filter((row) => row.from === "user").at(-1);
  if (!request) return;
  const later = pairRows.filter((row) => row.from === peer && Number(row.at) > Number(request.at)).at(-1);
  node.textContent = later ? "Latest message from " + person(peer).name + " · " + freshness(later.at) +
    ". Messages have no read or reply-to receipts." : "No later message from this agent yet.";
  node.hidden = false;
}
async function refreshView(force = false) {
  const view = model.view;
  if (!view || ["search", "home"].includes(view.kind)) return;
  if (view.kind === "activity") return activity(force);
  try {
    let rows;
    if (view.kind === "topic") {
      const data = await get("/api/topics/" + encodeURIComponent(view.key));
      rows = data.messages || [];
      notice(
        data.omitted
          ? "Showing the latest " +
              rows.length +
              " messages in this conversation."
          : "",
      );
    } else {
      const thread = model.threads.find((item) => item.pair === view.key);
      rows = thread ? thread.messages : [];
      notice("");
    }
    if (view !== model.view) return;
    renderRows(rows, force);
  } catch (error) {
    notice(error.message, true);
  }
}
function select(kind, key = "", updateHash = true) {
  if (model.view && model.view.kind === kind && model.view.key === key) {
    closeSidebar();
    return;
  }
  const previousTarget = target();
  if (previousTarget)
    model.drafts.set(previousTarget, $("message-input").value);
  model.view = { kind, key };
  rememberView(model.view);
  $("message-input").value = model.drafts.get(target()) || "";
  $("message-input").style.height = "auto";
  model.signature = "";
  model.first = true;
  model.rows = [];
  $("conversation-response").hidden = true;
  notice("");
  $("messages").replaceChildren();
  $("new-messages").hidden = true;
  if (updateHash)
    history.replaceState(
      null,
      "",
      "#" + new URLSearchParams({ [kind]: key }).toString(),
    );
  closeSidebar();
  header();
  intro();
  sidebar();
  document
    .querySelectorAll(".rail-item")
    .forEach((node) => node.classList.remove("selected"));
  $(
    kind === "activity"
      ? "nav-activity"
      : kind === "direct"
        ? "nav-direct"
        : "nav-home",
  ).classList.add("selected");
  if (kind === "activity") activity();
  else if (kind === "home") renderRows([]);
  else refreshView();
}
async function refreshState() {
  if (!model.authenticated) return;
  if (model.refreshing) {
    sidebar();
    header();
    return;
  }
  model.refreshing = true;
  try {
    const [state, direct] = await Promise.all([
      get("/api/state"),
      get("/api/direct"),
    ]);
    model.reportReceivedAt = performance.now();
    model.state = {
      ...state,
      topics: state.topics || [],
      sessions: state.sessions || [],
      names: state.names || {},
    };
    model.threads = direct.threads || [];
    connected(true);
    if (!model.view) {
      const restored = requestedView();
      if (restored) select(restored.kind, restored.key);
      else if (model.state.topics.length)
        select("topic", model.state.topics[0].topic);
      else select("home");
    } else {
      sidebar();
      header();
      await refreshView();
    }
    if (state.error) notice(state.error, true);
  } catch (error) {
    connected(false);
    sidebar();
    header();
    notice("The workspace is reconnecting. Your draft is safe. An agent may have answered in its own app while its reply connection was unavailable.", true);
  } finally {
    model.refreshing = false;
  }
}
function updateComposer() {
  const destination = target();
  const unavailable = destination && !destination.startsWith("#") && !presenceOf(person(destination)).can_message;
  $("composer").hidden = !destination;
  $("observer-note").hidden = Boolean(destination);
  $("observer-note").firstElementChild.textContent =
    model.view && model.view.kind === "direct"
      ? "This is an agent-to-agent conversation."
      : "Pick a conversation to write to your team.";
  $("message-input").placeholder = destination
    ? "Message " +
      (destination.startsWith("#") ? destination : person(destination).name)
    : "Message your team";
  $("message-input").disabled = model.sending;
  $("send-message").disabled =
    !destination ||
    !model.token ||
    unavailable ||
    model.sending ||
    !$("message-input").value.trim();
  $("send-message").title = unavailable ? "This agent is offline. Your draft stays here until it reconnects." : "Send message as a human";
}
function feedback(text, type = "") {
  const node = $("composer-feedback");
  node.className = "composer-feedback" + (type ? " " + type : "");
  node.replaceChildren(
    document.createTextNode(text),
    el("span", "Shift + Enter for a new line"),
  );
}
async function session() {
  try {
    const data = await get("/api/session");
    model.token = data.csrf_token || null;
    model.capabilities = data.capabilities || {};
    authentication(data.authenticated !== false);
    $("staff-team").hidden = !model.capabilities.workers;
    $("new-channel").hidden = !model.capabilities.channels;
    $("show-costs").hidden = !model.capabilities.accounting;
    $("logout-device").hidden = !data.authenticated;
    $("generate-invitation").hidden = !model.capabilities.pairing;
    $("device-connect").hidden = !model.capabilities.pairing;
    updateComposer();
    return model.authenticated;
  } catch (error) {
    model.token = null;
    authentication(false);
    feedback("Message sending is reconnecting. Your draft is safe.", "error");
    return false;
  }
}

function authentication(accepted) {
  model.authenticated = accepted;
  document.body.classList.toggle("authentication-required", !accepted);
  $("pairing").hidden = accepted;
  if (!accepted) {
    clearInvitation();
    model.token = null;
    for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  }
}

async function post(path, body, login = false) {
  let response, data;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(login ? {} : { "X-One-Conv-Token": model.token || "" }),
      },
      body: JSON.stringify(body),
    });
    data = await response.json();
  } catch (_) {
    throw new Error("The connection ended before a receipt arrived. Retry the same request to check its outcome.");
  }
  if (response.status === 401) authentication(false);
  if (!response.ok) throw new Error(data.error || "The workspace could not complete that request.");
  return data;
}

async function connectDevice() {
  if (!pairingToken) return;
  $("pair-device").disabled = true;
  $("pairing-feedback").textContent = "Connecting your device…";
  try {
    await post("/api/login", { token: pairingToken }, true);
    pairingToken = null;
    if (await session()) await refreshState();
  } catch (error) {
    $("pairing-feedback").textContent = error.message;
    $("pair-device").disabled = false;
  }
}

async function disconnectDevice() {
  clearInvitation();
  try {
    await post("/api/logout", {});
    model.state = { topics: [], sessions: [], names: {} };
    model.threads = [];
    model.rows = [];
    model.view = null;
    model.drafts.clear();
    model.pending.clear();
    try { sessionStorage.removeItem("oneconv:staffing"); } catch (_) { /* Some browsers disable storage. */ }
    navigation = {};
    try { localStorage.removeItem("oneconv:navigation"); } catch (_) { /* Storage can be unavailable. */ }
    model.costs = null;
    model.workers = { jobs: [], pools: [], profiles: [] };
    $("messages").replaceChildren();
    $("costs-content").replaceChildren();
    $("worker-jobs").replaceChildren();
    $("message-input").value = "";
    sidebar();
    authentication(false);
    $("pair-device").hidden = true;
    $("pairing-feedback").textContent = "This device is disconnected. Open a new pairing link to return.";
  } catch (error) { toast(error.message); }
}

function clearInvitation() {
  invitationGeneration += 1;
  deviceInvitation = null;
  $("device-invitation").hidden = true;
  $("device-invitation-url").value = "";
  $("invitation-feedback").textContent = "";
  $("generate-invitation").disabled = false;
  $("generate-invitation").textContent = "Connect another device";
}
async function generateInvitation() {
  const generation = ++invitationGeneration;
  deviceInvitation = null;
  $("device-invitation").hidden = true;
  $("device-invitation-url").value = "";
  $("generate-invitation").disabled = true;
  $("invitation-feedback").textContent = "Creating a private invitation…";
  try {
    const invitation = await post("/api/devices/pair", {});
    // A closed dialog cannot redisplay a secret when an in-flight response arrives.
    if (generation !== invitationGeneration || !model.authenticated || !$("device-dialog").open) return;
    deviceInvitation = invitation;
    $("device-invitation-url").value = invitation.url;
    $("invitation-expiry").textContent = "One use · expires " + validDate(invitation.expires_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    $("device-invitation").hidden = false;
    $("share-invitation").hidden = !navigator.share;
    $("generate-invitation").textContent = "Create a new link";
    $("invitation-feedback").textContent = "Open this link in Safari on your iPhone. This device stays connected.";
  } catch (error) {
    if (generation === invitationGeneration) $("invitation-feedback").textContent = error.message;
  } finally {
    if (generation === invitationGeneration) $("generate-invitation").disabled = false;
  }
}
async function copyInvitation() {
  if (!deviceInvitation) return;
  if (Number(deviceInvitation.expires_at) <= reportTime()) {
    clearInvitation();
    $("invitation-feedback").textContent = "This invitation expired. Create a new link.";
    return;
  }
  try {
    await navigator.clipboard.writeText(deviceInvitation.url);
    $("invitation-feedback").textContent = "Link copied. Share it privately with your other device.";
  } catch (_) {
    $("device-invitation-url").focus();
    $("device-invitation-url").select();
    $("invitation-feedback").textContent = "Select and copy the invitation above.";
  }
}
async function shareInvitation() {
  if (!deviceInvitation || !navigator.share) return;
  if (Number(deviceInvitation.expires_at) <= reportTime()) return copyInvitation();
  try { await navigator.share({ title: "Connect to OneConv", url: deviceInvitation.url }); }
  catch (_) { /* Cancelling the share sheet leaves the invitation available. */ }
}

const jobLabels = {
  queued: "Getting started", provisioning: "Preparing workspace", bootstrapping: "Setting up tools",
  starting: "Starting teammate", registering: "Joining the team", ready: "Online",
  auth_needed: "Provider needs attention", authentication_required: "Provider needs attention", failed: "Needs attention",
  unknown: "Outcome needs checking", unconfirmed: "Outcome needs checking", stopped: "Stopped",
  unverified: "Connection not verified",
};

function renderJobs() {
  const container = $("worker-jobs");
  container.replaceChildren();
  for (const job of (model.workers.jobs || []).slice(0, 12)) {
    const item = el("article", null, "worker-job");
    const spec = job.spec || job;
    const pool = (model.workers.pools || []).find((candidate) => candidate.id === job.pool_id);
    // Resuming compute does not verify that its stopped teammate has reconnected.
    const restartable = job.status === "stopped" && job.phase === "pool-stopped" && pool?.status === "available";
    const allocationBlocked = job.status === "unconfirmed" && job.phase === "allocation-blocked";
    const blockedPool = (model.workers.pools || []).find((candidate) => candidate.id === job.blocked_by_pool);
    const blockedOwner = blockedPool && (model.workers.jobs || []).find(
      (owner) => owner.pool_id === blockedPool.id && owner.status !== "stopped");
    // An existing owner keeps reconciliation authority until its reservation is orphaned.
    const orphanedAllocation = allocationBlocked && !job.box_id && blockedPool && !blockedPool.box_id && blockedPool.status !== "retired" &&
      spec.profile && blockedPool.profile === spec.profile && !blockedOwner;
    item.append(el("span", (spec.name || "A").slice(0, 2).toUpperCase(), "avatar codex"));
    const text = el("div");
    const jobPresence = presenceOf(person(job.address));
    const online = job.status === "ready" && jobPresence.can_message;
    text.append(el("strong", spec.name || "Teammate"), el("small", allocationBlocked ? "Waiting for box recovery" :
      job.status === "ready" && !online ? "Offline · check the connection" :
        job.status === "ready" && jobPresence.state === "unverified" ? "Connection unverified" : jobLabels[job.status] || job.status || "Preparing"));
    if (job.error || job.detail) text.append(el("p", job.error || job.detail, "job-detail"));
    if (job.status === "stopped" && job.phase === "pool-stopped") {
      text.append(el("p", restartable ? "The box is available. Choose Check again to reconnect this teammate." :
        "Resume its box, then choose Check again to reconnect this teammate.", "job-detail"));
    }
    if (allocationBlocked && !orphanedAllocation) {
      const recovery = blockedPool?.box_id || blockedPool?.status === "retired" ? "Choose Check again to retry this request." :
        (blockedOwner?.spec?.name ? `Recover the box from the ${blockedOwner.spec.name} request first, then choose Check again.` :
          "Resolve the earlier box creation first, then choose Check again.");
      text.append(el("p", recovery + " Cancelling this request leaves the earlier request unchanged.", "quiet-copy"));
    }
    if (["auth_needed", "authentication_required"].includes(job.status)) {
      const help = el("p", "Check the provider’s login, model access or quota in ", "job-detail");
      const link = el("a", "Boat’s agent settings");
      link.href = "https://boat.dev/dashboard?tab=agents"; link.target = "_blank"; link.rel = "noopener noreferrer";
      help.append(link, document.createTextNode(", then choose Check again. Your existing box is kept."));
      text.append(help);
    }
    item.append(text);
    if (online && job.address) {
      const button = el("button", "Message", "secondary-button");
      button.type = "button";
      button.addEventListener("click", () => {
        $("staff-dialog").close(); select("direct", pairOf(job.address));
      });
      item.append(button);
    }
    if (["ready", "unverified", "auth_needed", "failed"].includes(job.status) ||
        (job.status === "unconfirmed" && job.box_id) || restartable || allocationBlocked) {
      const actions = el("div", null, "job-actions");
      if (["auth_needed", "unverified", "failed"].includes(job.status) ||
          (job.status === "unconfirmed" && job.phase === "bootstrapping") || (job.status === "ready" && !online) ||
          restartable || allocationBlocked) {
        const check = el("button", "Check again", "secondary-button");
        check.type = "button";
        check.addEventListener("click", () => workerAction("recheck", job.id));
        actions.append(check);
      }
      if (!restartable) {
        const stop = el("button", allocationBlocked ? "Cancel request" : "Stop teammate", "secondary-button");
        stop.type = "button";
        stop.addEventListener("click", () => workerAction("stop", job.id));
        actions.append(stop);
      }
      item.append(actions);
    }
    if ((job.status === "unconfirmed" && job.phase === "creating" && !job.box_id) || orphanedAllocation) {
      const form = el("form", null, "quote-form"), label = el("label", "Existing box ID from Boat");
      const input = document.createElement("input"); input.placeholder = "bx_…"; input.required = true;
      input.setAttribute("aria-label", "Reconcile box for " + (spec.name || "teammate")); label.append(input);
      const button = el("button", "Use this existing box", "secondary-button"); button.type = "submit";
      form.append(label, button, el("p", "Check Boat before continuing. This attaches a box already created; it does not create another.", "quiet-copy"));
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); button.disabled = true;
        try { await post("/api/providers/workers", { action: "reconcile", id: job.id, box_id: input.value.trim() }); await refreshWorkers(); }
        catch (error) { $("staff-feedback").textContent = error.message; button.disabled = false; }
      });
      item.append(form);
    }
    container.append(item);
  }
  for (const pool of model.workers.pools || []) {
    if (pool.status === "retired") continue;
    const item = el("article", null, "worker-job pool-job");
    const detail = el("div");
    detail.append(el("strong", pool.name || "Shared worker box"),
      el("small", !pool.box_id ? "Box creation needs checking" : pool.status === "stopped" ? "Stopped · files preserved" : "Shared compute"));
    item.append(detail);
    if (pool.box_id) {
      const action = pool.status === "stopped" ? "resume_pool" : "stop_pool";
      const button = el("button", action === "resume_pool" ? "Resume box" : "Stop box", "secondary-button");
      button.type = "button";
      button.addEventListener("click", () => workerAction(action, pool.id));
      item.append(button);
    }
    container.append(item);
  }
}

async function workerAction(action, id) {
  try {
    await post("/api/providers/workers", { action, id });
    await refreshWorkers();
  } catch (error) { $("staff-feedback").textContent = error.message; }
}

function staffingProfiles() {
  return Array.isArray(model.workers.profiles) ? model.workers.profiles :
    Object.entries(model.workers.profiles || {}).map(([id, profile]) => ({ ...profile, id }));
}

function renderStaffingDisclosure() {
  const profile = staffingProfiles().find((item) => (item.id || item.name) === $("staff-profile").value);
  const harness = $("staff-harness").value, args = profile?.harness_args?.[harness];
  const bypass = { claude: "--dangerously-skip-permissions", codex: "--dangerously-bypass-approvals-and-sandbox", omp: "--approval-mode=yolo" };
  // Only explicit bypass settings establish unattended permissions for a selected harness.
  const unattended = Array.isArray(args) && (args.includes(bypass[harness]) || (harness === "omp" &&
    args.some((value, index) => value === "--approval-mode" && args[index + 1] === "yolo")));
  const disclosure = unattended ? "This teammate runs unattended with this profile’s repository access and credentials." :
    "This teammate uses this profile’s repository access, credentials and approval settings.";
  const content = $("staff-access-disclosure");
  if (content.textContent !== disclosure) content.textContent = disclosure;
}

async function refreshWorkers() {
  if (!model.capabilities.workers || !model.authenticated) return;
  try {
    model.workers = await get("/api/providers/workers");
    renderJobs();
    const picker = $("staff-profile"), selected = picker.value;
    picker.replaceChildren();
    const profiles = staffingProfiles();
    for (const profile of profiles) {
      const option = el("option", profile.label || profile.name || profile.id);
      option.value = profile.id || profile.name;
      picker.append(option);
    }
    if (!picker.children.length) {
      const option = el("option", "Default"); option.value = "default"; picker.append(option);
    }
    if ([...picker.options].some((option) => option.value === selected)) picker.value = selected;
    renderStaffingDisclosure();
  } catch (error) { $("staff-feedback").textContent = error.message; }
}

async function staffingIdentity(spec) {
  const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(spec))))]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem("oneconv:staffing")); } catch (_) { /* Storage can be unavailable. */ }
  if (!pending || pending.digest !== digest) pending = { digest, id: crypto.randomUUID().replaceAll("-", "") };
  try { sessionStorage.setItem("oneconv:staffing", JSON.stringify(pending)); } catch (_) { /* The server also rejects duplicate active names. */ }
  return pending.id;
}

async function staffTeammate(event) {
  event.preventDefault();
  if (model.staffing || !model.token) return;
  const spec = {
    name: $("staff-name").value.trim(), harness: $("staff-harness").value,
    model: $("staff-model").value.trim() || null, topic: $("staff-topic").value.trim().replace(/^#/, ""),
    profile: $("staff-profile").value, prompt: $("staff-prompt").value.trim(),
  };
  model.staffing = true; $("staff-submit").disabled = true;
  $("staff-feedback").textContent = "Your teammate’s workspace is being prepared…";
  try {
    const id = await staffingIdentity(spec);
    const data = await post("/api/providers/workers", { action: "spawn", id, spec });
    if (data.id !== id || typeof data.status !== "string") throw new Error("The request is not confirmed yet. Retry to check the same request.");
    $("staff-feedback").textContent = "We’re on it. You can close this window; preparation continues in the cloud.";
    await refreshWorkers();
  } catch (error) { $("staff-feedback").textContent = error.message; }
  finally { model.staffing = false; $("staff-submit").disabled = false; }
}

function openChannelDialog() {
  const list = $("channel-members");
  list.replaceChildren();
  const online = model.state.sessions.filter(
    (s) => !s.history_only && (!s.presence || s.presence.state !== "offline"),
  );
  for (const s of online) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = s.address;
    const p = person(s.address);
    const label = el("label");
    label.append(box, document.createTextNode(p.name + " · " + (s.title || s.cwd || s.address)));
    list.appendChild(label);
  }
  if (!online.length) list.appendChild(el("p", "No agent is connected; you can still create the channel and invite them later.", "quiet-copy"));
  $("channel-feedback").textContent = "";
  $("channel-dialog").showModal();
  $("channel-name").focus();
}

async function createChannel(event) {
  event.preventDefault();
  if (model.creatingChannel || !model.token) return;
  const spec = {
    topic: $("channel-name").value.trim().replace(/^#/, "").toLowerCase(),
    purpose: $("channel-purpose").value.trim(),
    members: [...$("channel-members").querySelectorAll("input:checked")].map((box) => box.value),
  };
  model.creatingChannel = true; $("channel-submit").disabled = true;
  $("channel-feedback").textContent = "Creating #" + spec.topic + "…";
  try {
    // The same form contents keep the same request ID, so a retry finishes the same channel.
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(spec))))]
      .map((byte) => byte.toString(16).padStart(2, "0")).join("");
    const data = await post("/api/channels", { id: digest.slice(0, 32), ...spec });
    $("channel-dialog").close();
    $("channel-form").reset();
    await refreshState();
    select("topic", data.topic);
  } catch (error) { $("channel-feedback").textContent = error.message; }
  finally { model.creatingChannel = false; $("channel-submit").disabled = false; }
}

function money(value, currency = "USD") {
  if (value === null || value === undefined || value === "") return "Not measured";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "Not measured";
  if (amount > 0 && amount < 0.00000001) return "< 0.00000001 " + currency;
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 8 }).format(amount); }
  catch (_) { return String(value) + " " + currency; }
}

async function showCosts() {
  $("costs-dialog").showModal();
  const content = $("costs-content"); content.replaceChildren(el("p", "Gathering your team’s accounting…", "quiet-copy"));
  try {
    const data = await get("/api/providers/accounting"); model.costs = data; content.replaceChildren();
    for (const card of data.cards || []) {
      const item = el("article", null, "rate-card"), rate = card.hourly_rate || {};
      item.append(el("h3", card.name || card.cost_center_id),
        el("strong", money(rate.amount, rate.currency) + (rate.amount == null ? "" : " / hour"), "rate-amount"),
        el("span", rate.basis || "Rate not measured", "rate-basis"),
        el("p", rate.metering_basis || "Includes internal subagents", "quiet-copy"));
      const form = el("form", null, "quote-form"), label = el("label", "Estimate an ask");
      const input = document.createElement("input"); input.type = "number"; input.min = "0"; input.max = "525600";
      input.step = "any"; input.value = "15"; input.setAttribute("aria-label", "Estimated minutes for " + (card.name || card.cost_center_id));
      label.append(input, document.createTextNode(" minutes"));
      const button = el("button", "Estimate", "secondary-button"); button.type = "submit";
      const result = el("p", "Planning estimate; includes their subagents.", "quiet-copy");
      form.append(label, button);
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); button.disabled = true;
        try {
          const quote = await post("/api/providers/accounting", { action: "quote", reference: card.cost_center_id, minutes: input.value });
          result.textContent = money(quote.expected_cost.amount, quote.expected_cost.currency) + " · " + (quote.hourly_rate.basis || "estimated") + ", including subagents";
        } catch (error) { result.textContent = error.message; }
        finally { button.disabled = false; }
      });
      item.append(form, result); content.append(item);
      const edit = el("button", "Set planning rate", "text-button"); edit.type = "button";
      edit.addEventListener("click", () => {
        edit.hidden = true;
        const editor = el("form", null, "quote-form"), label = el("label", "All-in hourly rate");
        const amount = document.createElement("input"); amount.type = "number"; amount.min = "0";
        amount.step = "0.00000001"; amount.value = rate.amount == null ? "" : rate.amount;
        amount.placeholder = "Unknown"; amount.setAttribute("aria-label", "Hourly rate for " + (card.name || card.cost_center_id));
        label.append(amount, document.createTextNode(" " + (rate.currency || "USD") + " / hour"));
        const save = el("button", "Save rate", "secondary-button"); save.type = "submit"; editor.append(label, save);
        editor.addEventListener("submit", async (event) => {
          event.preventDefault(); save.disabled = true;
          try {
            await post("/api/providers/accounting", { action: "update_rate", reference: card.cost_center_id,
              hourly_rate: { ...rate, amount: amount.value === "" ? null : amount.value,
                basis: "allocated", provenance: "Human planning allocation in the workspace" },
            });
            await showCosts();
          } catch (error) { result.textContent = error.message; save.disabled = false; }
        });
        item.append(editor);
      });
      item.append(edit);
    }
    if (!(data.cards || []).length) content.append(el("p", "Your team’s hourly rates will appear here once they’re registered. Unmeasured costs stay unknown.", "cost-empty"));
    const events = data.events || [];
    content.append(el("h3", "Recorded usage", "details-section-label"));
    if (!events.length) content.append(el("p", "No usage records yet. A subscription does not become a zero-dollar answer.", "quiet-copy"));
    for (const record of events.slice(0, 30)) {
      const item = el("article", null, "usage-row");
      const cash = record.cash_cost || {}, allocated = record.allocated_cost || {}, estimate = record.estimated_cost || {};
      const owner = (data.cards || []).find(card => card.cost_center_id === record.cost_center_id);
      const name = owner?.name || record.cost_center_id || "Teammate";
      const total = record.tokens?.total;
      const date = new Date(record.timestamp);
      const details = [Number.isFinite(total) ? total.toLocaleString() + " tokens" : "Token total not reported"];
      if (Number.isFinite(date.getTime())) details.push(date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }));
      item.append(el("strong", name + (record.model ? " · " + record.model : "")),
        el("small", details.join(" · ")),
        el("span", cash.amount == null ? "Cash not reported" : money(cash.amount, cash.currency) + " reported cash"));
      if (estimate.amount != null) item.append(el("small", money(estimate.amount, estimate.currency) + " estimated"));
      if (allocated.amount != null) item.append(el("small", money(allocated.amount, allocated.currency) + " allocated"));
      content.append(item);
    }
  } catch (error) { content.replaceChildren(el("p", error.message, "quiet-copy")); }
}
async function send(event) {
  event.preventDefault();
  const destination = target(),
    text = $("message-input").value.trim();
  if (!destination || !text || model.sending || !model.token) return;
  if (!destination.startsWith("#") && !presenceOf(person(destination)).can_message) {
    feedback("This agent is offline. Your draft stays here until it reconnects.", "error");
    return;
  }
  const draftKey = JSON.stringify([destination, text]);
  if (!model.pending.has(draftKey))
    model.pending.set(draftKey, {
      id: crypto.randomUUID().replaceAll("-", ""),
      target: destination,
      text,
    });
  const message = { ...model.pending.get(draftKey) };
  model.sending = true;
  updateComposer();
  feedback("Sending as You · Human…");
  try {
    const response = await fetch("/api/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-One-Conv-Token": model.token,
      },
      body: JSON.stringify(message),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 403) await session();
      throw new Error(
        data.error || "Couldn't confirm this send. Your draft has been kept.",
      );
    }
    model.drafts.delete(destination);
    if (target() === destination) {
      $("message-input").value = "";
      $("message-input").style.height = "auto";
    }
    model.pending.delete(draftKey);
    feedback(
      data.status === "unconfirmed"
        ? "Delivery isn't confirmed. Check the conversation before sending again."
        : "Stored on hub. Bridge acceptance and replies appear in the conversation.",
      data.status === "unconfirmed" ? "error" : "success",
    );
    model.first = true;
    await refreshState();
    if (data.status !== "unconfirmed") toast("Message stored on hub");
  } catch (error) {
    feedback(
      error.message ||
        "Couldn't confirm delivery. Try this draft again to check the same send.",
      "error",
    );
  } finally {
    model.sending = false;
    updateComposer();
    $("message-input").focus();
  }
}
function format(kind) {
  const input = $("message-input");
  const start = input.selectionStart,
    end = input.selectionEnd;
  const selected = input.value.slice(start, end);
  const pairs = {
    bold: ["**", "**"],
    italic: ["*", "*"],
    code: ["`", "`"],
    list: ["- ", ""],
  };
  const [before, after] = pairs[kind];
  input.setRangeText(before + selected + after, start, end, "end");
  input.focus();
  if (!selected)
    input.setSelectionRange(start + before.length, start + before.length);
  updateComposer();
}
function renderDetails() {
  const content = $("details-content");
  content.replaceChildren();
  const summary = el("div", null, "details-summary");
  summary.append(
    el(
      "h3",
      model.view && model.view.kind === "topic"
        ? "# " + model.view.key
        : "Your team",
    ),
    el(
      "p",
      "See each teammate’s connection, project and latest message. Connected agents may still need model access or credits.",
    ),
  );
  content.append(
    summary,
    el("h3", "IN THIS CONVERSATION", "details-section-label"),
  );
  const addresses = members();
  for (const address of addresses) {
    const p = person(address);
    const button = el("button", null, "member-row");
    button.type = "button";
    button.appendChild(avatar(address, "small-avatar"));
    const name = el("div");
    name.append(
      el("strong", p.name),
      el(
        "small",
        isHuman(address) ? "Human" : personContext(p),
      ),
    );
    if (!isHuman(address)) {
      const presence = presenceOf(p);
      name.append(el("small", presenceText(p), "member-presence"), el("small", presence.detail), el("small", p.address, "member-address"));
      if (validDate(presence.last_seen)) {
        const report = el("small", "Last checked: " + validDate(presence.last_seen).toLocaleString());
        name.appendChild(report);
      }
      if (validDate(presence.bridge_last_seen)) name.appendChild(el("small", "Machine connected: " + freshness(presence.bridge_last_seen)));
      if (p.session && p.session.title) name.appendChild(el("small", "Session context: " + p.session.title));
      const activity = p.session && p.session.activity;
      if (activity && activity.kind === "message") name.appendChild(el("small", "Latest hub message · " + freshness(activity.at) + ": “" + activity.summary + "”", "member-activity"));
      else name.appendChild(el("small", "Current task is not reported."));
    }
    button.appendChild(name);
    if (isHuman(address)) button.disabled = true;
    else
      button.addEventListener("click", () => {
        select("direct", pairOf(address));
        toggleDetails(false);
      });
    content.appendChild(button);
  }
  if (!addresses.length)
    content.appendChild(
      el("p", "Agents will appear here when they join.", "sidebar-empty"),
    );
}
function toggleDetails(open = !model.details) {
  model.details = open;
  $("details").hidden = !open;
  document.querySelector(".workspace").classList.toggle("has-details", open);
  $("members-toggle").setAttribute("aria-expanded", String(open));
  if (open) renderDetails();
}
function closeSidebar() {
  document.body.classList.remove("sidebar-open");
  $("sidebar-toggle").setAttribute("aria-expanded", "false");
}
function recipients() {
  const query = $("recipient-query").value.trim().toLowerCase();
  const list = $("recipient-list");
  list.replaceChildren();
  function option(name, description, kind, mark, action) {
    if (query && !(name + " " + description).toLowerCase().includes(query))
      return;
    const button = el("button", null, "recipient-option");
    button.type = "button";
    const label = el("div");
    label.append(el("strong", name), el("small", description));
    button.append(mark, label, el("span", kind, "recipient-kind"));
    button.addEventListener("click", () => {
      $("message-dialog").close();
      action();
      $("message-input").focus();
    });
    list.appendChild(button);
  }
  for (const topic of model.state.topics)
    option(
      "# " + topic.topic,
      (topic.members || []).length + " members",
      "Channel",
      el("span", "#", "avatar"),
      () => select("topic", topic.topic),
    );
  for (const agent of model.state.sessions) {
    const p = person(agent.address);
    if (presenceOf(p).state === "offline" && !query) continue;
    option(
      p.name,
      personContext(p) + " · " + presenceOf(p).label,
      "Agent",
      avatar(agent.address),
      () => select("direct", pairOf(agent.address)),
    );
  }
  if (!list.children.length)
    list.appendChild(
      el(
        "p",
        "No conversations match. Try an agent’s name or channel.",
        "sidebar-empty",
      ),
    );
}
function newMessage() {
  $("recipient-query").value = "";
  recipients();
  $("message-dialog").showModal();
  $("recipient-query").focus();
}
async function allMessages() {
  const topics = model.state.topics.slice(0, 30);
  const loaded = await Promise.allSettled(
    topics.map((topic) =>
      get("/api/topics/" + encodeURIComponent(topic.topic)),
    ),
  );
  const rows = [];
  for (let i = 0; i < loaded.length; i++)
    if (loaded[i].status === "fulfilled")
      for (const row of loaded[i].value.messages || [])
        rows.push({ ...row, source: "# " + topics[i].topic });
  for (const thread of model.threads)
    for (const row of thread.messages || [])
      rows.push({ ...row, source: pairName(thread.pair) });
  return rows;
}
async function activity(force = false) {
  const view = model.view;
  try {
    const rows = await allMessages();
    if (view === model.view)
      renderRows(
        rows
          .sort((a, b) => (Number(a.at) || 0) - (Number(b.at) || 0))
          .slice(-100),
        force,
      );
  } catch (error) {
    notice(error.message, true);
  }
}
async function search(event) {
  event.preventDefault();
  const query = $("search").value.trim();
  if (!query) return;
  const generation = ++model.searchGeneration;
  select("search", query);
  $("conversation-intro").querySelector("h3").textContent =
    "Results for “" + query + "”";
  notice("Searching your conversations…");
  try {
    const rows = await allMessages();
    if (
      generation !== model.searchGeneration ||
      !model.view ||
      model.view.kind !== "search"
    )
      return;
    const found = rows.filter((row) =>
      [row.text, row.source, person(row.from).name]
        .join(" ")
        .toLowerCase()
        .includes(query.toLowerCase()),
    );
    notice(
      found.length +
        " message" +
        (found.length === 1 ? "" : "s") +
        " found" +
        (model.state.topics.length > 30
          ? " in the first 30 channels and direct messages."
          : "."),
    );
    renderRows(
      found
        .sort((a, b) => (Number(a.at) || 0) - (Number(b.at) || 0))
        .slice(-200),
      true,
    );
  } catch (error) {
    notice(error.message, true);
  }
}
function theme(value) {
  document.documentElement.dataset.theme = value;
  $("theme-toggle").setAttribute(
    "aria-label",
    value === "dark" ? "Switch to light theme" : "Switch to dark theme",
  );
  $("theme-toggle")
    .querySelector(".rail-icon")
    .replaceChildren(icon(value === "dark" ? "sun" : "moon"));
  try {
    localStorage.setItem("oneconv:theme", value);
  } catch (_) {
    /* Private browsing can disable local preferences. */
  }
}

$("composer").addEventListener("submit", send);
$("message-input").addEventListener("input", () => {
  const input = $("message-input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
  updateComposer();
});
$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
document
  .querySelectorAll("[data-format]")
  .forEach((button) =>
    button.addEventListener("click", () => format(button.dataset.format)),
  );
document.querySelectorAll("[data-section]").forEach((button) =>
  button.addEventListener("click", () => {
    const expanded = button.getAttribute("aria-expanded") === "true";
    button.setAttribute("aria-expanded", String(!expanded));
    $(button.dataset.section).hidden = expanded;
  }),
);
$("new-message").addEventListener("click", newMessage);
$("pair-device").hidden = !pairingToken;
$("pair-device").addEventListener("click", connectDevice);
$("logout-device").addEventListener("click", disconnectDevice);
$("device-menu").addEventListener("click", () => $("device-dialog").showModal());
$("generate-invitation").addEventListener("click", generateInvitation);
$("copy-invitation").addEventListener("click", copyInvitation);
$("share-invitation").addEventListener("click", shareInvitation);
$("device-dialog").addEventListener("close", clearInvitation);
$("device-dialog").addEventListener("cancel", clearInvitation);
$("staff-team").addEventListener("click", () => {
  $("staff-dialog").showModal();
  if (model.view && model.view.kind === "topic") $("staff-topic").value = model.view.key;
  refreshWorkers();
});
$("staff-form").addEventListener("submit", staffTeammate);
$("new-channel").addEventListener("click", openChannelDialog);
$("channel-form").addEventListener("submit", createChannel);
$("staff-profile").addEventListener("change", renderStaffingDisclosure);
$("staff-harness").addEventListener("change", renderStaffingDisclosure);
$("show-costs").addEventListener("click", showCosts);
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => {
    // Discard the secret before the browser queues its asynchronous dialog-close event.
    if (button.dataset.close === "device-dialog") clearInvitation();
    $(button.dataset.close).close();
  });
}
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault(); installPrompt = event; $("install-app").hidden = false;
});
$("install-app").addEventListener("click", async () => {
  if (!installPrompt) return;
  await installPrompt.prompt(); installPrompt = null; $("install-app").hidden = true;
});
$("start-human-message").addEventListener("click", newMessage);
$("dialog-close").addEventListener("click", () => $("message-dialog").close());
$("recipient-form").addEventListener("submit", (event) =>
  event.preventDefault(),
);
$("recipient-query").addEventListener("input", recipients);
$("message-dialog").addEventListener("click", (event) => {
  if (event.target === $("message-dialog")) {
    const rect = event.target.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      event.target.close();
  }
});
$("members-toggle").addEventListener("click", () => toggleDetails());
$("details-toggle").addEventListener("click", () => toggleDetails());
$("details-close").addEventListener("click", () => toggleDetails(false));
$("nav-people").addEventListener("click", () => toggleDetails());
$("nav-home").addEventListener("click", () => {
  const previous = savedRoute("topic");
  if (previous) select(previous.kind, previous.key);
  else if (model.state.topics.length) select("topic", model.state.topics[0].topic);
  else select("home");
});
$("workspace-home").addEventListener("click", (event) => {
  event.preventDefault();
  $("nav-home").click();
});
$("nav-direct").addEventListener("click", () => {
  const previous = savedRoute("direct");
  if (previous) select(previous.kind, previous.key);
  else if (model.threads.length) select("direct", model.threads[0].pair);
  else newMessage();
});
$("nav-activity").addEventListener("click", () => select("activity"));
$("all-activity").addEventListener("click", () => select("activity"));
$("search-form").addEventListener("submit", search);
$("sidebar-toggle").addEventListener("click", () => {
  const open = document.body.classList.toggle("sidebar-open");
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
});
$("sidebar-overlay").addEventListener("click", closeSidebar);
$("message-scroll").addEventListener("scroll", () => {
  const box = $("message-scroll");
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 100)
    $("new-messages").hidden = true;
});
$("new-messages").addEventListener("click", () => {
  $("message-scroll").scrollTop = $("message-scroll").scrollHeight;
  $("new-messages").hidden = true;
});
$("theme-toggle").addEventListener("click", () =>
  theme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"),
);
$("emoji-toggle").addEventListener("click", () => {
  const picker = $("emoji-picker");
  picker.hidden = !picker.hidden;
  $("emoji-toggle").setAttribute("aria-expanded", String(!picker.hidden));
});
for (const emoji of ["👋", "👍", "✨", "🎉", "💡", "✅", "❤️"]) {
  const button = el("button", emoji);
  button.type = "button";
  button.setAttribute("aria-label", "Insert " + emoji);
  button.addEventListener("click", () => {
    const input = $("message-input");
    input.setRangeText(emoji, input.selectionStart, input.selectionEnd, "end");
    input.focus();
    $("emoji-picker").hidden = true;
    $("emoji-toggle").setAttribute("aria-expanded", "false");
    updateComposer();
  });
  $("emoji-picker").appendChild(button);
}
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("search").focus();
    $("search").select();
  }
  if (event.key === "Escape") {
    closeSidebar();
    toggleDetails(false);
    $("emoji-picker").hidden = true;
  }
});
window.addEventListener("hashchange", () => {
  if (!model.authenticated) return;
  const view = requestedView();
  if (view) select(view.kind, view.key, false);
});
try {
  theme(localStorage.getItem("oneconv:theme") === "dark" ? "dark" : "light");
} catch (_) {
  theme("light");
}
session().then((ready) => { if (ready) refreshState(); });
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* Messaging also works without offline assets. */ });
}
setInterval(refreshState, 4000);
setInterval(() => {
  if (model.authenticated && model.view && !model.refreshing) refreshView();
}, 3000);
setInterval(() => { if ($("staff-dialog").open) refreshWorkers(); }, 4000);
