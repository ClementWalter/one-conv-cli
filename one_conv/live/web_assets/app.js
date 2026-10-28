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
  // Sends this tab started, by message id, until the server's copy replaces them. Memory only.
  outbox: new Map(),
  // Bumped when the session ends, so an older request can never touch the new session's state.
  generation: 0,
  dirty: false,
  lastRefresh: 0,
  changeLoop: null,
  feedAbort: null,
  viewSequence: 0,
  signature: "",
  first: true,
  refreshing: null,
  expanded: new Set(),
  drafts: new Map(),
  details: false,
  searchGeneration: 0,
  authenticated: false,
  capabilities: {},
  costs: null,
  staffing: false,
  reportReceivedAt: null,
  // Read marks the workspace accepted from this tab, per conversation, and the saves in flight.
  readMarks: new Map(),
  reading: new Set(),
  // The agent whose Stop button was tapped once and waits for a confirming second tap, and stops in flight.
  stopArmed: null,
  stopping: new Set(),
};
const harnesses = {
  claude: "Claude",
  codex: "Codex",
  omp: "Oh My Pi",
  cursor: "Cursor",
  boat: "Boat",
};
// A harness with several apps names the one a session runs in.
const products = { "cursor-cli": "cursor-cli", "cursor-ide": "cursor-ide" };
const marks = { omp: "π", boat: "B" };
// Harness logos from @lobehub/icons-static-svg 1.95.0 (MIT); a harness without one keeps its mark.
const logos = {
  claude: "<svg aria-hidden=\"true\" class=\"harness-logo\" viewBox=\"0 0 24 24\" xmlns=\"http://www.w3.org/2000/svg\"><path clip-rule=\"evenodd\" d=\"M20.998 10.949H24v3.102h-3v3.028h-1.487V20H18v-2.921h-1.487V20H15v-2.921H9V20H7.488v-2.921H6V20H4.487v-2.921H3V14.05H0V10.95h3V5h17.998v5.949zM6 10.949h1.488V8.102H6v2.847zm10.51 0H18V8.102h-1.49v2.847z\" fill=\"#D97757\" fill-rule=\"evenodd\"></path></svg>",
  codex: "<svg aria-hidden=\"true\" class=\"harness-logo\" viewBox=\"0 0 24 24\" xmlns=\"http://www.w3.org/2000/svg\"><path d=\"M19.503 0H4.496A4.496 4.496 0 000 4.496v15.007A4.496 4.496 0 004.496 24h15.007A4.496 4.496 0 0024 19.503V4.496A4.496 4.496 0 0019.503 0z\" fill=\"#fff\"></path><path d=\"M9.064 3.344a4.578 4.578 0 012.285-.312c1 .115 1.891.54 2.673 1.275.01.01.024.017.037.021a.09.09 0 00.043 0 4.55 4.55 0 013.046.275l.047.022.116.057a4.581 4.581 0 012.188 2.399c.209.51.313 1.041.315 1.595a4.24 4.24 0 01-.134 1.223.123.123 0 00.03.115c.594.607.988 1.33 1.183 2.17.289 1.425-.007 2.71-.887 3.854l-.136.166a4.548 4.548 0 01-2.201 1.388.123.123 0 00-.081.076c-.191.551-.383 1.023-.74 1.494-.9 1.187-2.222 1.846-3.711 1.838-1.187-.006-2.239-.44-3.157-1.302a.107.107 0 00-.105-.024c-.388.125-.78.143-1.204.138a4.441 4.441 0 01-1.945-.466 4.544 4.544 0 01-1.61-1.335c-.152-.202-.303-.392-.414-.617a5.81 5.81 0 01-.37-.961 4.582 4.582 0 01-.014-2.298.124.124 0 00.006-.056.085.085 0 00-.027-.048 4.467 4.467 0 01-1.034-1.651 3.896 3.896 0 01-.251-1.192 5.189 5.189 0 01.141-1.6c.337-1.112.982-1.985 1.933-2.618.212-.141.413-.251.601-.33.215-.089.43-.164.646-.227a.098.098 0 00.065-.066 4.51 4.51 0 01.829-1.615 4.535 4.535 0 011.837-1.388zm3.482 10.565a.637.637 0 000 1.272h3.636a.637.637 0 100-1.272h-3.636zM8.462 9.23a.637.637 0 00-1.106.631l1.272 2.224-1.266 2.136a.636.636 0 101.095.649l1.454-2.455a.636.636 0 00.005-.64L8.462 9.23z\" fill=\"url(#lobe-icons-codex-_R_0_)\"></path><defs><linearGradient gradientUnits=\"userSpaceOnUse\" id=\"lobe-icons-codex-_R_0_\" x1=\"12\" x2=\"12\" y1=\"3\" y2=\"21\"><stop stop-color=\"#B1A7FF\"></stop><stop offset=\".5\" stop-color=\"#7A9DFF\"></stop><stop offset=\"1\" stop-color=\"#3941FF\"></stop></linearGradient></defs></svg>",
  cursor: "<svg aria-hidden=\"true\" class=\"harness-logo\" fill=\"currentColor\" fill-rule=\"evenodd\" viewBox=\"0 0 24 24\" xmlns=\"http://www.w3.org/2000/svg\"><path d=\"M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z\"></path></svg>",
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
  const label = (session && products[session.product]) || harnesses[kind];
  const alias = Object.entries(model.state.names || {}).find(
    ([, bound]) => bound === address || (localIdentity && bound === local),
  );
  const boundName = (session && session.name) || (alias && alias[0]);
  const name = boundName ? "@" + String(boundName).replace(/^@/, "") :
    (label || "Agent") + " · " + (local.split(":").slice(1).join(":") || local).slice(0, 8);
  return {
    address,
    name,
    kind: label || "Agent",
    color: kind,
    // The avatar names the harness, so a renamed agent still shows what it runs on.
    initials: marks[kind] || (logos[kind] ? "" : name.replace(/^@/, "").slice(0, 2).toUpperCase()),
    logo: logos[kind] || "",
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
    "avatar " + p.color + (size ? " " + size : "") + (p.logo ? " has-logo" : ""),
  );
  // Parsed as SVG, not HTML: the logo is one of the constant strings above, never workspace data.
  if (p.logo)
    node.appendChild(document.importNode(
      new DOMParser().parseFromString(p.logo, "image/svg+xml").documentElement, true));
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
function workingText(names) {
  if (!names.length) return "";
  if (names.length === 1) return names[0] + " is working…";
  if (names.length === 2) return names[0] + " and " + names[1] + " are working…";
  return names[0] + ", " + names[1] + " and " + (names.length - 2) + " more are working…";
}
function renderWorking() {
  // Agents whose bridge reports them busy, so a channel shows it is being worked on between replies.
  const view = model.view;
  const names = view && ["topic", "direct"].includes(view.kind)
    ? [...new Set(members())].filter((address) => address !== "user")
      .map((address) => person(address)).filter((p) => presenceOf(p).state === "busy").map((p) => p.name)
    : [];
  $("working-text").textContent = workingText(names);
  $("working").hidden = !names.length;
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
class StaleSession extends Error {}
async function get(path) {
  const generation = model.generation;
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  let result = await response.json().catch(() => ({}));
  if (!result || typeof result !== "object" || Array.isArray(result)) result = {};
  // An answer meant for an ended session never changes the current one, not even to sign it out.
  if (generation !== model.generation) throw new StaleSession("stale session");
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
function conversationKey(view = model.view) {
  if (!view) return null;
  if (view.kind === "topic") return "#" + view.key;
  return view.kind === "direct" ? view.key : null;
}
function unreadOf(conversation, count) {
  // The open, visible conversation is being read: it never turns bold while its mark is saved.
  if (!document.hidden && conversation === conversationKey()) return 0;
  return Number(count) > 0 ? Math.floor(Number(count)) : 0;
}
function threadUnread(pair) {
  const thread = model.threads.find((item) => item.pair === pair);
  return thread ? unreadOf(pair, thread.unread) : 0;
}
function totalUnread() {
  // An archived channel keeps its count on its own row but no longer adds to the title.
  return model.state.topics.filter((topic) => !topic.archived)
    .reduce((sum, topic) => sum + unreadOf("#" + topic.topic, topic.unread), 0) +
    model.threads.reduce((sum, thread) => sum + unreadOf(thread.pair, thread.unread), 0);
}
async function markRead() {
  const conversation = conversationKey();
  if (!conversation || document.hidden || !model.capabilities.unread || !model.token) return;
  const times = (model.serverRows || []).map((row) => Number(row.at)).filter(Number.isFinite);
  const newest = times.length ? Math.max(...times) : 0;
  if (!newest || newest <= (model.readMarks.get(conversation) || 0) || model.reading.has(conversation)) return;
  const generation = model.generation;
  model.reading.add(conversation);
  try {
    const data = await post("/api/read", { conversation, at: newest });
    if (generation !== model.generation) return;
    model.readMarks.set(conversation, Math.max(Number(data.at) || 0, newest));
    for (const item of [...model.state.topics.filter((topic) => "#" + topic.topic === conversation),
      ...model.threads.filter((thread) => thread.pair === conversation)]) item.unread = 0;
    sidebar();
    header();
  } catch (_) {
    /* The next refresh retries; another device can also clear it. */
  } finally {
    // A save from an ended session never releases the same conversation's save in the current one.
    if (generation === model.generation) model.reading.delete(conversation);
  }
}
function rowButton(list, label, mark, active, action, count, unread = 0) {
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
  if (unread > 0) {
    button.classList.add("unread");
    const badge = el("span", unread > 99 ? "99+" : String(unread), "row-unread");
    badge.title = unread + " unread";
    badge.setAttribute("aria-label", unread + " unread");
    button.appendChild(badge);
  } else if (count !== undefined) {
    const amount = el("span", count, "row-count");
    amount.title = count + " running now";
    button.appendChild(amount);
  }
  button.addEventListener("click", action);
  li.appendChild(button);
  list.appendChild(li);
  return button;
}
// A right click, or a long press on a phone, opens a row's actions; `items` is [{label, danger, confirm, action}].
function rowMenu(button, items) {
  const open = (x, y) => {
    const list = items().filter(Boolean);
    if (list.length) showRowMenu(list, x, y);
  };
  button.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    open(event.clientX, event.clientY);
  });
  let timer = null;
  const cancel = () => { clearTimeout(timer); timer = null; };
  button.addEventListener("touchstart", (event) => {
    const touch = event.touches[0];
    timer = setTimeout(() => {
      timer = null;
      // The click that ends this press must not also open the row.
      button.addEventListener("click", (click) => { click.preventDefault(); click.stopImmediatePropagation(); }, { once: true, capture: true });
      open(touch.clientX, touch.clientY);
    }, 550);
  }, { passive: true });
  for (const name of ["touchend", "touchmove", "touchcancel"]) button.addEventListener(name, cancel, { passive: true });
}
function closeRowMenu() {
  const menu = document.getElementById("row-menu");
  if (menu) menu.remove();
}
function showRowMenu(items, x, y) {
  closeRowMenu();
  const menu = el("div", null, "row-menu");
  menu.id = "row-menu";
  menu.setAttribute("role", "menu");
  for (const item of items) {
    const entry = el("button", item.label, "row-menu-item" + (item.danger ? " danger" : ""));
    entry.type = "button";
    entry.setAttribute("role", "menuitem");
    entry.addEventListener("click", (event) => {
      event.stopPropagation();
      // A destructive entry asks for a second tap; a browser confirm dialog would block the page.
      if (item.confirm && !entry.classList.contains("armed")) {
        entry.classList.add("armed");
        entry.textContent = item.confirm;
        return;
      }
      closeRowMenu();
      item.action();
    });
    menu.appendChild(entry);
  }
  document.body.appendChild(menu);
  const box = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - box.width - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - box.height - 8)) + "px";
}
function sidebar() {
  const topics = $("topics");
  topics.replaceChildren();
  const archived = $("archived-topics");
  archived.replaceChildren();
  // Archived channels stay reachable, folded away from the main list.
  for (const topic of model.state.topics) {
    const row = rowButton(
      topic.archived ? archived : topics,
      topic.topic,
      el("span", "#", "hash"),
      model.view &&
        model.view.kind === "topic" &&
        model.view.key === topic.topic,
      () => select("topic", topic.topic),
      topic.running ?? (topic.members || []).length,
      unreadOf("#" + topic.topic, topic.unread),
    );
    rowMenu(row, () => [
      { label: topic.archived ? "Bring back channel" : "Archive channel", action: () => archiveChannel(topic.topic, !topic.archived) },
    ]);
  }
  if (!topics.children.length)
    topics.appendChild(
      el(
        "li",
        "Channels will appear here when your team starts a topic.",
        "sidebar-empty",
      ),
    );
  $("topic-count").textContent = model.state.topics.filter((topic) => !topic.archived).length || "";
  $("archived-count").textContent = archived.children.length || "";
  $("archived-section").hidden = !archived.children.length;
  renderRoutines();
  // A conversation with a session opens from its row among the running sessions, with its unread count.
  const people = $("people");
  people.replaceChildren();
  // Sessions come and go: only the running ones are listed; past ones live on in their channels.
  // An ended session stays listed while its direct messages are unread, so every unread count is reachable.
  const listed = model.state.sessions.map((session) => session.address);
  const kept = (pair) => threadUnread(pair) > 0 || (model.view?.kind === "direct" && model.view.key === pair);
  const ended = model.threads.filter((thread) => thread.pair.split(" ").includes("user") && kept(thread.pair))
    .map((thread) => thread.pair.split(" ").find((address) => address !== "user"))
    .filter((address) => address && !listed.includes(address)).map((address) => ({ address }));
  for (const session of [...model.state.sessions, ...ended]) {
    const p = person(session.address);
    const state = presenceOf(p);
    if (state.state === "offline" && !kept(pairOf(session.address))) continue;
    const button = rowButton(
      people,
      p.name,
      avatar(session.address, "tiny-avatar"),
      model.view &&
        model.view.kind === "direct" &&
        model.view.key === pairOf(session.address),
      () => select("direct", pairOf(session.address)),
      undefined,
      threadUnread(pairOf(session.address)),
    );
    const label = button.querySelector(".row-label");
    label.classList.add("agent-label");
    // A session without an @name goes by its own name (Claude Code's session name, a thread or chat title);
    // the harness and short id it would otherwise show move to the context line.
    const named = !p.name.startsWith("@") && session.title;
    label.replaceChildren(
      el("span", named ? session.title : p.name, "agent-name"),
      el("small", named ? [p.name, p.machine, p.project].filter(Boolean).join(" · ") : personContext(p), "agent-context"),
    );
    if (session.title && !named) label.appendChild(el("small", "Context: " + session.title, "agent-context agent-session-context"));
    label.appendChild(el("small", presenceText(p), "agent-context agent-state"));
    const presence = el(
      "span",
      null,
      "row-presence " + state.state,
    );
    presence.title = state.label;
    button.appendChild(presence);
    button.title = p.name + " · " + personContext(p) + " · " + presenceText(p) + "\n" + state.detail;
    rowMenu(button, () => [
      model.capabilities.stop && presenceOf(person(session.address)).can_message && {
        label: "Kill " + p.name, danger: true, confirm: "Tap again to kill " + p.name,
        action: () => stopAgent(session.address, p.name),
      },
    ]);
  }
  if (!people.children.length)
    people.appendChild(
      el("li", "No session is running right now.", "sidebar-empty"),
    );
  $("agent-count").textContent = model.state.sessions.filter((s) => presenceOf(person(s.address)).state !== "offline").length || "";
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
  // In the header, so a long channel can be archived without scrolling back to its start.
  const archive = $("archive-toggle");
  archive.hidden = view.kind !== "topic";
  const archived = Boolean(view.kind === "topic" && (currentTopic() || {}).archived);
  archive.classList.toggle("archived", archived);
  archive.setAttribute("aria-label", archived ? "Bring back channel" : "Archive channel");
  archive.title = archived ? "Show this channel in your list again" : "Hide this channel from your list; agents can still read it";
  const waiting = totalUnread();
  // On a phone the sidebar is closed, so its menu button carries the unread mark.
  $("sidebar-toggle").classList.toggle("has-unread", waiting > 0);
  $("sidebar-toggle").title = waiting ? waiting + " unread" : "";
  document.title = (waiting ? "(" + waiting + ") " : "") + (view.kind === "topic" ? "# " : "") + title + " · OneConv";
  renderWorking();
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
    const topic = currentTopic() || {};
    chip.append(
      icon("people"),
      document.createTextNode(
        (topic.running ?? members().length) + " running · " + (topic.participants || []).length + " took part",
      ),
    );
    const live = el("span", null, "intro-chip");
    live.append(icon("bolt"), document.createTextNode("Live conversation"));
    chips.append(chip, live);
    box.appendChild(chips);
  }
}
function inline(parent, text) {
  // Inline markdown as agents write it: code, links, bold, strikethrough, emphasis and bare URLs.
  const pattern = /(`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\*[^*\n]+\*|(?<![A-Za-z0-9])_[^_\n]+_(?![A-Za-z0-9])|https?:\/\/[^\s<>]+)/g;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    parent.appendChild(document.createTextNode(text.slice(start, match.index)));
    const value = match[0];
    if (value.startsWith("`")) parent.appendChild(el("code", value.slice(1, -1)));
    else if (value.startsWith("[")) {
      const close = value.indexOf("](");
      const link = el("a", value.slice(1, close));
      link.href = value.slice(close + 2, -1);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.appendChild(link);
    } else if (value.startsWith("**") || value.startsWith("__")) inline(parent.appendChild(el("strong")), value.slice(2, -2));
    else if (value.startsWith("~~")) inline(parent.appendChild(el("s")), value.slice(2, -2));
    else if (value.startsWith("*") || value.startsWith("_")) inline(parent.appendChild(el("em")), value.slice(1, -1));
    else {
      // Sentence punctuation after a bare URL, and a ")" it never opened, belong to the text, not the link.
      let url = value;
      while (/[.,;:!?'"*_]$/.test(url) || (url.endsWith(")") && url.split(")").length > url.split("(").length))
        url = url.slice(0, -1);
      const link = el("a", url);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.appendChild(link);
      parent.appendChild(document.createTextNode(value.slice(url.length)));
    }
    start = match.index + value.length;
  }
  parent.appendChild(document.createTextNode(text.slice(start)));
}
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
function tableCells(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}
// A Claude Code question forwarded by the AskUserQuestion hook ends with one marker line naming its options.
const ASK_MARKER = /\n?<!-- one-conv-ask (\{.*\}) -->\s*$/;
function askOf(text) {
  const match = text.match(ASK_MARKER);
  if (!match) return [text, null];
  try {
    const ask = JSON.parse(match[1]);
    // The form shows the questions and options, so only the message's opening line stays as text.
    return Array.isArray(ask.questions) ? [text.slice(0, match.index).split("\n\n")[0], ask.questions] : [text, null];
  } catch {
    return [text, null];
  }
}
// Choices survive the thread being drawn again while the person is still filling the form.
const askState = new Map();
function askOptions(questions, agent, key, closed = false) {
  // One fieldset per question, as in the terminal: its options as radio buttons (checkboxes when
  // several answers are allowed), each with its description, plus "Other" for a typed answer. The
  // answer goes to the asking agent as an ordinary reply, which its hook reads as `Header: answer`
  // lines (or the bare answer for a single question).
  const form = el("form", null, "ask-options");
  const state = askState.get(key) || { checked: {}, typed: {}, sent: false };
  askState.set(key, state);
  const name = "ask-" + key;
  const submit = el("button", "Send answer", "primary-button ask-send");
  submit.type = "submit";
  const groups = questions.map((question, index) => {
    const group = form.appendChild(el("fieldset", null, "ask-question"));
    group.appendChild(el("legend", question.header || "Question", "ask-header"));
    group.appendChild(el("p", question.question || "", "ask-text"));
    const type = question.multiSelect ? "checkbox" : "radio";
    const choice = (label, description) => {
      const row = group.appendChild(el("label", null, "ask-option"));
      const input = row.appendChild(el("input"));
      input.type = type;
      input.name = name + "-" + index;
      input.value = label;
      const words = row.appendChild(el("span", null, "ask-choice"));
      words.appendChild(el("strong", label));
      if (description) words.appendChild(el("span", description, "ask-description"));
      return input;
    };
    for (const option of question.options || []) {
      const label = typeof option === "string" ? option : option.label;
      choice(label, typeof option === "string" ? "" : option.description);
    }
    const other = choice("Other", "");
    const typed = other.closest("label").appendChild(el("input", null, "ask-other"));
    typed.type = "text";
    typed.value = state.typed[index] || "";
    for (const input of group.querySelectorAll("input[type=radio], input[type=checkbox]"))
      input.checked = (state.checked[index] || []).includes(input.value);
    typed.placeholder = "Your own answer";
    typed.setAttribute("aria-label", "Your own answer to " + (question.header || "this question"));
    typed.addEventListener("input", () => { other.checked = Boolean(typed.value.trim()) || other.checked; ready(); });
    return { question, group, typed };
  });
  const answerOf = ({ group, typed }) => [...group.querySelectorAll("input:checked")]
    .map((input) => input.value === "Other" ? typed.value.trim() : input.value).filter(Boolean).join(", ");
  function ready() {
    groups.forEach(({ group, typed }, index) => {
      state.checked[index] = [...group.querySelectorAll("input:checked")].map((input) => input.value);
      state.typed[index] = typed.value;
    });
    submit.disabled = state.sent || groups.some((entry) => !answerOf(entry));
  }
  form.addEventListener("change", ready);
  form.appendChild(submit);
  ready();
  if (state.sent || closed) for (const control of form.elements) control.disabled = true;
  if (closed && !state.sent) {
    submit.textContent = "Question closed";
    form.classList.add("closed");
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const answers = groups.map(answerOf);
    if (answers.some((answer) => !answer)) return;
    const text = groups.length === 1 ? answers[0]
      : groups.map((entry, index) => (entry.question.header || entry.question.question) + ": " + answers[index]).join("\n");
    // Sending draws the thread again at once, so the form is marked sent before it goes.
    state.sent = true;
    if (answerAsk(agent, text) === false) {
      state.sent = false;
      return;
    }
    for (const control of form.elements) control.disabled = true;
  });
  return form;
}
function answerAsk(agent, text) {
  // The reply goes through the composer, so it is queued, retried and shown like any message.
  if (target() !== agent) {
    feedback("Open the conversation with this agent to answer.", "error");
    return false;
  }
  $("message-input").value = text;
  $("composer").requestSubmit();
  return true;
}
function messageText(container, text) {
  // Block markdown as agents write it: paragraphs, headings, bulleted and numbered lists (one level of
  // nesting), quotes, rules, tables and fenced code. Anything else stays as written.
  container.replaceChildren();
  const lines = String(text).split("\n");
  let paragraph = [], quote = [], list = null, code = null;
  function flush() {
    if (paragraph.length) {
      inline(container.appendChild(el("p")), paragraph.join("\n"));
      paragraph = [];
    }
    if (quote.length) {
      inline(container.appendChild(el("blockquote")).appendChild(el("p")), quote.join("\n"));
      quote = [];
    }
    list = null;
  }
  function item(ordered, indent, value) {
    if (list && indent > list.indent && list.last) {
      // One nested level under the previous item, whichever kind of list either one is.
      if (!list.sub) list.sub = list.last.appendChild(el(ordered ? "ol" : "ul"));
      inline(list.sub.appendChild(el("li")), value);
      return;
    }
    if (!list || list.ordered !== ordered || indent < list.indent) {
      flush();
      list = { ordered, indent, node: container.appendChild(el(ordered ? "ol" : "ul")), last: null, sub: null };
    }
    list.last = list.node.appendChild(el("li"));
    list.sub = null;
    inline(list.last, value);
  }
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line.trim().startsWith("```")) {
      if (code !== null) {
        container.appendChild(el("pre")).appendChild(el("code", code.join("\n")));
        code = null;
      } else {
        flush();
        code = [];
      }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    if (!line.trim()) { flush(); continue; }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^(\s*)[-*+]\s+(.*)$/.exec(line);
    const numbered = /^(\s*)\d+[.)]\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      // Messages are small: every heading level is a bold line, stepping down from h3.
      inline(container.appendChild(el("h" + Math.min(6, heading[1].length + 2))), heading[2].trim());
    } else if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      container.appendChild(el("hr"));
    } else if (/^\s*>/.test(line)) {
      if (paragraph.length || list) flush();
      quote.push(line.replace(/^\s*>\s?/, ""));
    } else if (bullet || numbered) {
      if (paragraph.length || quote.length) flush();
      const match = bullet || numbered;
      item(Boolean(numbered), match[1].length, match[2]);
    } else if (/\|/.test(line) && index + 1 < lines.length && TABLE_RULE.test(lines[index + 1])) {
      flush();
      const table = container.appendChild(el("table"));
      const head = table.appendChild(el("thead")).appendChild(el("tr"));
      for (const cell of tableCells(line)) inline(head.appendChild(el("th")), cell);
      const body = table.appendChild(el("tbody"));
      index += 1;
      while (index + 1 < lines.length && /\|/.test(lines[index + 1]) && lines[index + 1].trim()) {
        index += 1;
        const row = body.appendChild(el("tr"));
        for (const cell of tableCells(lines[index])) inline(row.appendChild(el("td")), cell);
      }
    } else {
      if (list || quote.length) flush();
      paragraph.push(line);
    }
  }
  if (code !== null) container.appendChild(el("pre")).appendChild(el("code", code.join("\n")));
  flush();
}
const LOCAL_DELIVERY = {
  sending: ["Sending…", "Not yet confirmed by the workspace."],
  saved: ["Saved", "The workspace has your message. An agent reply may take longer. (Stored on the hub.)"],
  unconfirmed: ["Delivery unconfirmed", "The workspace stored this message but could not confirm delivery."],
  uncertain: ["Not confirmed — retry checks the same send",
    "The connection ended before the workspace answered. It may already have this message; retrying cannot duplicate it."],
  refused: ["Refused", "The workspace refused this message."],
};
function localRow(entry) {
  const [label, detail] = LOCAL_DELIVERY[entry.state];
  return {
    id: entry.id, from: "user", to: entry.target, text: entry.text, at: entry.at, local: entry,
    attachments: entry.attachments || [],
    delivery: { state: entry.state, label: entry.state === "refused" && entry.error ? "Refused · " + entry.error : label, detail },
  };
}
function withOutbox(rows) {
  // The server's copy of a send replaces this tab's; its own sends stay in their own conversation.
  // Ids are unique per sending node, so only the workspace human's own post to the same place counts.
  for (const row of rows) {
    const entry = row.from === "user" && model.outbox.get(row.id);
    if (entry && row.to === entry.target) {
      model.outbox.delete(row.id);
      model.pending.delete(entry.draftKey);
      // A draft restored only because the answer was lost is now known to be stored: nothing is left to resend.
      // Only a draft the app restored for this send, and nobody has edited since, is cleared.
      if (entry.restored) {
        if (entry.target === target() && $("message-input").value === entry.text) $("message-input").value = "";
        else if (entry.target !== target() && model.drafts.get(entry.target) === entry.text)
          model.drafts.delete(entry.target);
      }
    }
  }
  const destination = target();
  const local = [...model.outbox.values()].filter((entry) => entry.target === destination).map(localRow);
  return local.length ? [...rows, ...local] : rows;
}
function staffingLabel(staffing) {
  // Who the hub is starting for a post no running session could read.
  if (!staffing) return "";
  const where = String(staffing.route || "").split("/")[0];
  switch (staffing.state) {
    case "node": return "Starting a session on " + where + "…";
    case "cloud": return "Waiting for a machine to start a session…";
    default: return "";
  }
}
function ackLine(row) {
  // Who read the post: one check mark per session that acked it.
  const acks = Array.isArray(row.acks) ? row.acks : [];
  if (!acks.length) {
    const starting = staffingLabel(row.staffing);
    if (!starting) return null;
    const line = el("small", starting, "message-acks starting");
    if (row.staffing && row.staffing.detail) line.title = row.staffing.detail;
    return line;
  }
  const line = el("small", null, "message-acks");
  line.title = "Read by " + acks.map((who) => person(who).name).join(", ");
  for (const who of acks) line.appendChild(el("span", "✓ " + person(who).name, "message-ack"));
  return line;
}
function renderRows(rows, force = false) {
  model.serverRows = rows;
  rows = withOutbox(rows);
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
  for (const [index, row] of rows.entries()) {
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
    const [text, ask] = askOf(String(row.text || ""));
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
      // The asking agent waits on its question, so any later message from it or from the human means
      // the question was answered or dropped; its form then only shows what was asked.
      const closed = rows.slice(index + 1).some((later) => later.from === row.from || isHuman(later.from));
      if (ask && row.from !== "user") body.appendChild(askOptions(ask, row.from, key, closed));
    }
    fill();
    if (Array.isArray(row.attachments) && row.attachments.length) {
      const images = el("div", null, "message-images");
      for (const attachment of row.attachments) {
        if (!attachment || typeof attachment.id !== "string" || !/^[0-9a-f]{32}$/.test(attachment.id)) continue;
        const link = el("a", null, "message-image-link");
        link.href = "/api/attachments/" + attachment.id;
        link.target = "_blank";
        link.rel = "noopener";
        const image = el("img", null, "message-image");
        image.src = link.href;
        image.alt = "Attached image";
        image.loading = "lazy";
        link.appendChild(image);
        images.appendChild(link);
      }
      body.appendChild(images);
    }
    content.appendChild(body);
    // A turn mirrored from the session's terminal is the conversation itself, not a synced copy.
    if (row.reported === true && row.mirrored !== true) {
      // Shown on every bubble, grouped or not: this copy is the sending machine's record.
      const synced = el("span", "Synced history", "message-note message-synced");
      synced.title = "From the sending machine's local message log; nobody was notified again.";
      content.appendChild(synced);
    }
    if (row.directed === true) {
      // A direct message tagged with this channel: only its recipient was notified.
      const scope = el("span", "To " + person(row.to).name + " only", "message-note message-directed");
      scope.title = "A direct message tagged with this channel. Other members were not notified.";
      content.appendChild(scope);
    }
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
    if (isHuman(row.from) && row.mirrored !== true) {
      const delivery = row.delivery && typeof row.delivery === "object" ? row.delivery : null;
      // Channel rollups carry their own counted label; direct messages keep the short forms.
      const label = delivery && (Array.isArray(delivery.recipients) ? delivery.label :
        { stored: "Saved · awaiting delivery", accepted: "Delivery accepted", unconfirmed: "Unconfirmed", refused: "Refused" }[delivery.state]);
      const status = el("small", row.local ? delivery.label : label || (delivery ? delivery.label : "Stored on hub"),
        "message-delivery" + (row.local ? " local-" + row.local.state : ""));
      status.title = delivery ? delivery.detail : "Stored in this conversation. No read receipt is available.";
      if (row.local && row.local.state === "uncertain") {
        const retry = el("button", "Retry", "message-retry");
        retry.type = "button";
        retry.addEventListener("click", () => transmit(row.local));
        status.append(" ", retry);
      }
      content.appendChild(status);
      const acks = ackLine(row);
      if (acks) content.appendChild(acks);
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
  // The nearest thing to a receipt: whether the agent has said anything since the human's last message.
  node.textContent = later ? "Latest message from " + person(peer).name + " · " + freshness(later.at) +
    ". Messages have no read or reply-to receipts." : "You wrote last and " + person(peer).name +
    " has not answered since. Messages have no read receipts.";
  node.hidden = false;
}
async function refreshView(force = false) {
  const view = model.view;
  if (!view || ["search", "home"].includes(view.kind)) return;
  if (view.kind === "activity") return activity(force);
  // Only the newest read of a view may render it; an older answer arriving late is dropped.
  const sequence = ++model.viewSequence;
  const generation = model.generation;
  const current = () => sequence === model.viewSequence && view === model.view && generation === model.generation;
  try {
    let rows;
    if (view.kind === "topic") {
      const data = await get("/api/topics/" + encodeURIComponent(view.key));
      if (!current()) return;
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
    if (!current()) return;
    renderRows(rows, force);
    markRead();
  } catch (error) {
    if (current() && !(error instanceof StaleSession)) notice(error.message, true);
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
  // Feedback belongs to the conversation it was about.
  feedback(DEFAULT_FEEDBACK);
  $("message-input").value = model.drafts.get(target()) || "";
  $("message-input").style.height = "auto";
  model.signature = "";
  model.first = true;
  model.rows = [];
  model.serverRows = [];
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
      : "nav-home",
  ).classList.add("selected");
  if (kind === "activity") activity();
  else if (kind === "home") renderRows([]);
  else refreshView();
}
async function refreshState() {
  if (!model.authenticated) return;
  // The refresh in flight belongs to one session; a newer session never waits behind it.
  const generation = model.generation;
  if (model.refreshing === generation) {
    // A change arrived during a refresh: read again once it ends, so nothing is skipped.
    model.dirty = true;
    sidebar();
    header();
    return;
  }
  model.refreshing = generation;
  model.dirty = false;
  model.lastRefresh = Date.now();
  try {
    const [state, direct] = await Promise.all([
      get("/api/state"),
      get("/api/direct"),
    ]);
    if (generation !== model.generation) return;
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
    if (error instanceof StaleSession || generation !== model.generation) return;
    connected(false);
    sidebar();
    header();
    notice("The workspace is reconnecting. Your draft is safe. An agent may have answered in its own app while its reply connection was unavailable.", true);
  } finally {
    if (model.refreshing === generation) model.refreshing = null;
    if (generation === model.generation && model.dirty && model.authenticated) refreshState();
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
  // Written only when they change: iOS drops suggestions and autocorrect for a field rewritten as it is typed in.
  const input = $("message-input");
  const placeholder = destination
    ? "Message " + (destination.startsWith("#") ? destination : person(destination).name)
    : "Message your team";
  if (input.placeholder !== placeholder) input.placeholder = placeholder;
  if (input.disabled !== model.sending) input.disabled = model.sending;
  $("send-message").disabled =
    !destination ||
    !model.token ||
    unavailable ||
    model.sending ||
    (!$("message-input").value.trim() && !model.attachments.some((a) => a.id));
  $("send-message").title = unavailable ? "This agent is offline. Your draft stays here until it reconnects." : "Send message as a human";
}
const DEFAULT_FEEDBACK =
  "Messages from you are always labelled Human. Saved means the workspace has it; agent replies can take longer than a second.";
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
    $("routines-section").hidden = !model.capabilities.routines;
    $("show-costs").hidden = !model.capabilities.accounting;
    $("logout-device").hidden = !data.authenticated;
    $("generate-invitation").hidden = !model.capabilities.pairing;
    $("sign-out-others").hidden = !model.capabilities.sign_out_others;
    $("device-connect").hidden = !model.capabilities.pairing;
    updateComposer();
    return model.authenticated;
  } catch (error) {
    // A stale answer from an ended session never signs out the current one.
    if (error instanceof StaleSession) return model.authenticated;
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
    model.generation += 1;
    if (model.feedAbort) model.feedAbort.abort();
    model.outbox.clear();
    model.pending.clear();
    model.readMarks.clear();
    model.reading.clear();
    for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  }
}

async function post(path, body, login = false) {
  const generation = model.generation;
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
    if (generation !== model.generation) throw new StaleSession("stale session");
    throw new Error("The connection ended before a receipt arrived. Retry the same request to check its outcome.");
  }
  // An answer meant for an ended session never changes the current one, not even to sign it out.
  if (generation !== model.generation) throw new StaleSession("stale session");
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
    if (await session()) { await refreshState(); changeFeed(); }
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
    $("messages").replaceChildren();
    $("costs-content").replaceChildren();
    $("message-input").value = "";
    sidebar();
    authentication(false);
    $("pair-device").hidden = true;
    $("pairing-feedback").textContent = "This device is disconnected. Open a new pairing link to return.";
  } catch (error) { toast(error.message); }
}

async function signOutOthers() {
  // Keeps only this device: every other browser and any unused invitation stops working.
  $("sign-out-others").disabled = true;
  $("sign-out-feedback").textContent = "Signing out other devices…";
  try {
    const result = await post("/api/devices/sign-out-others", {});
    clearInvitation();
    $("sign-out-feedback").textContent = result.signed_out === 1 ? "1 other device was signed out. Only this one stays connected."
      : result.signed_out + " other devices were signed out. Only this one stays connected.";
  } catch (error) {
    $("sign-out-feedback").textContent = error.message;
  } finally {
    $("sign-out-others").disabled = false;
  }
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

async function staffingIdentity(spec) {
  const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(spec))))]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem("oneconv:staffing")); } catch (_) { /* Storage can be unavailable. */ }
  if (!pending || pending.digest !== digest) pending = { digest, id: crypto.randomUUID().replaceAll("-", "") };
  try { sessionStorage.setItem("oneconv:staffing", JSON.stringify(pending)); } catch (_) { /* Without it a retry is a new request. */ }
  return pending.id;
}

async function staffTeammate(event) {
  event.preventDefault();
  if (model.staffing || !model.token) return;
  const spec = {
    name: $("staff-name").value.trim(), topic: $("staff-topic").value.trim().replace(/^#/, ""),
    prompt: $("staff-prompt").value.trim(),
  };
  model.staffing = true; $("staff-submit").disabled = true;
  $("staff-feedback").textContent = "Asking a connected machine to start your teammate…";
  try {
    const id = await staffingIdentity(spec);
    const data = await post("/api/providers/workers", { action: "spawn", id, spec });
    if (data.id !== id || typeof data.status !== "string") throw new Error("The request is not confirmed yet. Retry to check the same request.");
    const where = String(data.route || "").split("/")[0];
    $("staff-feedback").textContent = where ? "Starting on " + where + ". You can close this window; they will join #" + spec.topic + "."
      : "Waiting for a machine to connect. You can close this window; they will join #" + spec.topic + " once one does.";
  } catch (error) { $("staff-feedback").textContent = error.message; }
  finally { model.staffing = false; $("staff-submit").disabled = false; }
}

// --- routines: prompts the workspace posts in a channel on a schedule --------------------------
function untilText(seconds) {
  const left = Math.max(0, Number(seconds) - Date.now() / 1000);
  return left < 60 ? "in under a minute" : left < 3600 ? "in " + Math.round(left / 60) + " min" :
    left < 86400 ? "in " + Math.round(left / 3600) + " h" : "in " + Math.round(left / 86400) + " d";
}
function routineLine(routine) {
  if (!routine.enabled) return "Paused · #" + routine.topic;
  return routine.schedule + " · #" + routine.topic + (routine.next_at ? " · next " + untilText(routine.next_at) : "");
}
function renderRoutines() {
  const list = $("routines");
  list.replaceChildren();
  const routines = model.state.routines || [];
  for (const routine of routines) {
    const button = rowButton(list, routine.name, icon("clock"), false, () => openRoutineDialog(routine));
    button.classList.toggle("paused", !routine.enabled);
    const label = button.querySelector(".row-label");
    label.classList.add("agent-label");
    label.replaceChildren(el("span", routine.name, "agent-name"),
      el("small", routineLine(routine), "agent-context" + (routine.last_error ? " routine-error" : "")));
  }
  if (!routines.length) list.appendChild(el("li", "Schedule work: + adds a routine.", "sidebar-empty"));
  $("routine-count").textContent = routines.length || "";
}
function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (_) { return "UTC"; }
}
function routineCron() {
  const when = $("routine-when").value;
  const [hour, minute] = ($("routine-time").value || "09:00").split(":").map(Number);
  if (when === "custom") return $("routine-cron").value.trim();
  if (when === "hourly") return Math.min(59, Math.max(0, Number($("routine-minute").value) || 0)) + " * * * *";
  const days = { daily: "*", weekdays: "1-5", weekly: $("routine-day").value }[when];
  return minute + " " + hour + " * * " + days;
}
function showRoutineFields() {
  const when = $("routine-when").value;
  $("routine-time-field").hidden = ["hourly", "custom"].includes(when);
  $("routine-minute-field").hidden = when !== "hourly";
  $("routine-day-field").hidden = when !== "weekly";
  $("routine-cron-field").hidden = when !== "custom";
}
function fillRoutineSchedule(cron) {
  // A schedule the presets can express opens in them; any other stays as its cron line.
  const parts = (cron || "0 9 * * *").split(" ");
  const [minute, hour, day, month, weekday] = parts;
  const pad = (value) => String(value).padStart(2, "0");
  const simple = parts.length === 5 && /^\d+$/.test(minute) && day === "*" && month === "*";
  let when = "custom";
  if (simple && hour === "*" && weekday === "*") when = "hourly";
  else if (simple && /^\d+$/.test(hour)) when = weekday === "*" ? "daily" : weekday === "1-5" ? "weekdays" : /^[0-7]$/.test(weekday) ? "weekly" : "custom";
  $("routine-when").value = when;
  $("routine-cron").value = cron || "";
  if (/^\d+$/.test(minute)) $("routine-minute").value = minute;
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) $("routine-time").value = pad(hour) + ":" + pad(minute);
  if (when === "weekly") $("routine-day").value = String(Number(weekday) % 7);
  showRoutineFields();
}
function openRoutineDialog(routine = null) {
  model.routine = routine;
  model.routineDelete = false;
  $("routine-form").reset();
  $("routine-topics").replaceChildren(...model.state.topics.map((topic) => { const option = document.createElement("option"); option.value = topic.topic; return option; }));
  $("routine-heading").textContent = routine ? routine.name : "Work that comes round.";
  $("routine-name").value = routine ? routine.name : "";
  $("routine-topic").value = routine ? routine.topic : (model.view && model.view.kind === "topic" ? model.view.key : "");
  $("routine-prompt").value = routine ? routine.prompt : "";
  fillRoutineSchedule(routine ? routine.cron : "0 9 * * *");
  const zone = routine ? routine.tz : browserZone();
  $("routine-zone").textContent = "Times are in " + zone + ".";
  $("routine-actions").hidden = !routine;
  $("routine-status").hidden = !routine;
  if (routine) {
    $("routine-toggle").textContent = routine.enabled ? "Pause" : "Resume";
    $("routine-delete").textContent = "Delete";
    $("routine-status").textContent = [routine.enabled ? "Next run " + untilText(routine.next_at) : "Paused",
      routine.last_at ? "last run " + freshness(routine.last_at) : "never run yet",
      routine.last_error ? "last attempt failed: " + routine.last_error : ""].filter(Boolean).join(" · ");
  }
  $("routine-feedback").textContent = "";
  $("routine-dialog").showModal();
  if (!routine) $("routine-name").focus();
}
async function saveRoutine(event) {
  event.preventDefault();
  if (model.savingRoutine || !model.token) return;
  const current = model.routine;
  const routine = {
    id: current ? current.id : crypto.randomUUID().replaceAll("-", ""),
    name: $("routine-name").value.trim(),
    topic: $("routine-topic").value.trim().replace(/^#/, "").toLowerCase(),
    prompt: $("routine-prompt").value,
    cron: routineCron(),
    tz: current ? current.tz : browserZone(),
    enabled: current ? current.enabled : true,
  };
  model.savingRoutine = true; $("routine-submit").disabled = true;
  $("routine-feedback").textContent = "Saving…";
  try {
    // Keeping the id across a retry replaces the same routine rather than adding a second one.
    if (!current) model.routine = { ...routine };
    const saved = await post("/api/routines", routine);
    model.routine = saved;
    $("routine-dialog").close();
    toast(saved.name + ": " + (saved.enabled ? saved.schedule.toLowerCase() + " in #" + saved.topic : "saved, paused"));
    model.signature = "";
    await refreshState();
  } catch (error) { $("routine-feedback").textContent = error.message; }
  finally { model.savingRoutine = false; $("routine-submit").disabled = false; }
}
async function routineAction(action) {
  const routine = model.routine;
  if (!routine || !routine.created) return;
  if (action === "delete" && !model.routineDelete) {
    model.routineDelete = true;
    $("routine-delete").textContent = "Tap again to delete";
    return;
  }
  $("routine-feedback").textContent = { run: "Posting…", pause: "Pausing…", resume: "Resuming…", delete: "Deleting…" }[action];
  try {
    const result = await post("/api/routines/action", { id: routine.id, action });
    if (action === "run") {
      $("routine-dialog").close();
      select("topic", routine.topic);
      toast("Posted " + routine.name + " in #" + routine.topic + ".");
    } else if (action === "delete") {
      $("routine-dialog").close();
      toast(routine.name + " deleted.");
    } else openRoutineDialog(result);
    model.signature = "";
    await refreshState();
  } catch (error) { $("routine-feedback").textContent = error.message; }
}

function openChannelDialog() {
  $("channel-feedback").textContent = "";
  $("channel-dialog").showModal();
  $("channel-purpose").focus();
}

async function createChannel(event) {
  event.preventDefault();
  if (model.creatingChannel || !model.token) return;
  // The workspace fixes the name's case and spacing, or derives it from the prompt when left empty.
  const spec = {
    topic: $("channel-name").value.trim(),
    purpose: $("channel-purpose").value.trim(),
    directory: $("channel-directory").value.trim(),
  };
  model.creatingChannel = true; $("channel-submit").disabled = true;
  $("channel-feedback").textContent = "Creating the channel…";
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
    text = $("message-input").value.trim(),
    attachments = model.attachments.filter((a) => a.id);
  if (!destination || (!text && !attachments.length) || !model.token) return;
  if (!destination.startsWith("#") && !presenceOf(person(destination)).can_message) {
    feedback("This agent is offline. Your draft stays here until it reconnects.", "error");
    return;
  }
  if (navigator.onLine === false) {
    // No request leaves the device, so this one can truthfully say it was not sent.
    feedback("Offline — not sent. Your draft is kept.", "error");
    return;
  }
  const ids = attachments.map((a) => a.id);
  const draftKey = JSON.stringify([destination, text, ids]);
  if (!model.pending.has(draftKey))
    model.pending.set(draftKey, { id: crypto.randomUUID().replaceAll("-", ""), target: destination, text });
  const { id } = model.pending.get(draftKey);
  const entry = model.outbox.get(id) || { id, target: destination, text, draftKey, at: Date.now() / 1000,
    attachments: attachments.map((a) => ({ id: a.id, media_type: a.media_type, size: a.size })) };
  entry.state = "sending";
  model.outbox.set(id, entry);
  model.drafts.delete(destination);
  $("message-input").value = "";
  clearAttachments();
  $("message-input").style.height = "auto";
  model.first = true;
  renderRows(model.serverRows || [], true);
  updateComposer();
  feedback("Sending as You · Human… Saved appears once the workspace stores it.");
  $("message-input").focus();
  await transmit(entry);
}

function provesStored(data, entry) {
  // A receipt counts only when it names this send, its sender and destination, with a known outcome.
  const r = data.receipt;
  if (data.id !== entry.id || !["sent", "unconfirmed"].includes(data.status)) return false;
  if (!r || typeof r !== "object" || Array.isArray(r) || r.id !== entry.id || r.from !== "user") return false;
  if (r.to !== entry.target && !(typeof r.topic === "string" && "#" + r.topic === entry.target)) return false;
  if (typeof r.status !== "string") return false;
  return r.deliveries === undefined || (Array.isArray(r.deliveries) && r.deliveries.every((d) =>
    d && typeof d === "object" && typeof d.to === "string" && typeof d.status === "string"));
}

async function transmit(entry) {
  const generation = model.generation;
  entry.state = "sending";
  if (target() === entry.target) renderRows(model.serverRows || [], true);
  let response, data;
  try {
    response = await fetch("/api/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-One-Conv-Token": model.token || "" },
      body: JSON.stringify({ id: entry.id, target: entry.target, text: entry.text,
        ...(entry.attachments && entry.attachments.length ? { attachments: entry.attachments.map((a) => a.id) } : {}) }),
    });
    data = await response.json().catch(() => ({}));
  } catch (_) {
    response = null;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) data = {};
  // A send from an ended session never changes the current one.
  if (generation !== model.generation || !model.outbox.has(entry.id)) return;
  const here = () => target() === entry.target;
  // Only a receipt naming this send proves the workspace stored it.
  const receipt = response && response.ok && provesStored(data, entry);
  if (receipt) {
    entry.state = data.status === "unconfirmed" ? "unconfirmed" : "saved";
    model.pending.delete(entry.draftKey);
    if (here()) {
      renderRows(model.serverRows || [], true);
      feedback(entry.state === "saved" ? "The workspace has your message. An agent reply may take longer."
        : "Delivery isn't confirmed. Check the conversation before sending again.",
      entry.state === "saved" ? "success" : "error");
    }
    refreshState();
    return;
  }
  if (response && response.status === 401) return authentication(false);
  if (response && !response.ok && response.status >= 400 && response.status < 500 && ![403, 408, 409, 425, 429].includes(response.status)) {
    entry.state = "refused";
    entry.error = data.error || "";
    model.pending.delete(entry.draftKey);
  } else {
    if (response && response.status === 403) await session();
    // Uncertain: the draft keeps its id (model.pending), so sending it again checks the same send.
    entry.state = "uncertain";
  }
  if (generation !== model.generation) return;
  // The text returns to an empty composer only; a newer draft is never replaced.
  if (here() && !$("message-input").value) {
    $("message-input").value = entry.text;
    entry.restored = true;
  } else if (!here() && !model.drafts.get(entry.target)) {
    model.drafts.set(entry.target, entry.text);
    entry.restored = true;
  }
  if (here()) {
    renderRows(model.serverRows || [], true);
    feedback(entry.state === "refused" ? "The workspace refused this message. Your text is back in the box."
      : "Not confirmed — Retry checks the same send without duplicating it.", "error");
    updateComposer();
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
async function archiveChannel(topic, archived) {
  try {
    await post("/api/channels/archive", { topic, archived });
    toast(archived ? "#" + topic + " archived. Find it under Archived." : "#" + topic + " is back in your channels.");
  } catch (error) {
    toast(error.message);
  }
  model.signature = "";
  await refreshState();
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
        isHuman(address) ? "Human"
          : (model.view && model.view.kind === "topic" && !((currentTopic() || {}).working || []).includes(address)
            ? "Watching · " : "Working here · ") + personContext(p),
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
    if (!isHuman(address) && model.capabilities.stop && presenceOf(p).can_message) content.appendChild(stopButton(address, p));
  }
  if (!addresses.length)
    content.appendChild(
      el("p", "Agents will appear here when they join.", "sidebar-empty"),
    );
}
function stopButton(address, p) {
  const armed = model.stopArmed && model.stopArmed.address === address && model.stopArmed.until > Date.now();
  const busy = model.stopping.has(address);
  const button = el("button", busy ? "Stopping…" : armed ? "Tap again to stop " + p.name : "Stop agent",
    "secondary-button stop-agent" + (armed ? " armed" : ""));
  button.type = "button";
  button.disabled = busy;
  button.addEventListener("click", () => {
    // A second tap within a few seconds confirms; a browser confirm dialog would block the page.
    if (!armed) {
      model.stopArmed = { address, until: Date.now() + 5000 };
      renderDetails();
      setTimeout(() => {
        if (model.stopArmed && model.stopArmed.address === address && model.stopArmed.until <= Date.now()) {
          model.stopArmed = null;
          if (model.details) renderDetails();
        }
      }, 5100);
      return;
    }
    model.stopArmed = null;
    stopAgent(address, p.name);
  });
  return button;
}
async function stopAgent(address, name) {
  model.stopping.add(address);
  if (model.details) renderDetails();
  try {
    const result = await post("/api/sessions/stop", { address });
    toast({
      stopped: name + " stopped.",
      refused: name + " was not stopped: its machine could not end it (Codex and Cursor chats share their app's process). Close it in its app.",
      unconfirmed: name + "'s machine could not confirm the stop. Check again in a moment.",
    }[result.status] || "Stop sent; " + name + "'s machine has not answered yet.");
  } catch (error) {
    if (!(error instanceof StaleSession)) toast(error.message);
  } finally {
    model.stopping.delete(address);
  }
  model.signature = "";
  await refreshState();
  if (model.details) renderDetails();
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

// --- the compact composer -----------------------------------------------------------------------
// On a phone the composer rests as one input line and shows its toolbar and controls while it is
// being used: focused, holding text, or carrying an image.
function fitComposer() {
  const composer = $("composer");
  const busy = composer.contains(document.activeElement) || Boolean($("message-input").value)
    || (model.attachments || []).length > 0;
  composer.classList.toggle("compact", !busy);
}
$("composer").addEventListener("focusin", fitComposer);
// On blur the next focused element is not known yet; a toolbar tap must keep the composer open.
$("composer").addEventListener("focusout", () => setTimeout(fitComposer, 0));
$("message-input").addEventListener("input", fitComposer);
fitComposer();

// --- images attached to a message ---------------------------------------------------------
// An image is uploaded as soon as it is pasted, dropped or picked, so sending only names its id.
model.attachments = [];
function renderAttachments() {
  if (typeof fitComposer === "function") setTimeout(fitComposer, 0);
  const strip = $("attachments");
  strip.replaceChildren();
  strip.hidden = !model.attachments.length;
  for (const attachment of model.attachments) {
    const chip = el("div", null, "attachment-chip" + (attachment.id ? "" : attachment.error ? " failed" : " uploading"));
    const image = el("img", null, "attachment-thumb");
    image.src = attachment.url;
    image.alt = attachment.name || "Attached image";
    chip.appendChild(image);
    chip.appendChild(el("span", attachment.error || (attachment.id ? "" : "Uploading…"), "attachment-state"));
    const remove = el("button", null, "attachment-remove");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove " + (attachment.name || "image"));
    remove.appendChild(icon("close"));
    remove.addEventListener("click", () => {
      model.attachments = model.attachments.filter((a) => a !== attachment);
      URL.revokeObjectURL(attachment.url);
      renderAttachments();
      updateComposer();
    });
    chip.appendChild(remove);
    strip.appendChild(chip);
  }
}
function clearAttachments() {
  for (const attachment of model.attachments) URL.revokeObjectURL(attachment.url);
  model.attachments = [];
  renderAttachments();
}
async function attach(file) {
  if (!file || !/^image\/(png|jpeg|gif|webp)$/.test(file.type)) {
    feedback("Only PNG, JPEG, GIF and WebP images can be attached.", "error");
    return;
  }
  if (model.attachments.length >= 4) {
    feedback("A message carries at most four images.", "error");
    return;
  }
  const attachment = { url: URL.createObjectURL(file), name: file.name, media_type: file.type, size: file.size, id: null, error: "" };
  model.attachments.push(attachment);
  renderAttachments();
  try {
    const response = await fetch("/api/attachments", {
      method: "POST", headers: { "Content-Type": file.type, "X-One-Conv-Token": model.token || "" }, body: file,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || typeof data.id !== "string") throw new Error(data.error || "The image was not stored.");
    attachment.id = data.id;
  } catch (error) {
    attachment.error = error.message || "Upload failed";
  }
  renderAttachments();
  updateComposer();
}
function attachFiles(files) {
  for (const file of files || []) attach(file);
}
$("attach-toggle").addEventListener("click", () => $("attach-input").click());
$("attach-input").addEventListener("change", (event) => {
  attachFiles(event.target.files);
  event.target.value = "";
});
$("message-input").addEventListener("paste", (event) => {
  const files = [...(event.clipboardData ? event.clipboardData.items : [])]
    .filter((item) => item.kind === "file" && item.type.startsWith("image/")).map((item) => item.getAsFile());
  if (!files.length) return;
  event.preventDefault();
  attachFiles(files);
});
for (const type of ["dragover", "drop"]) {
  $("composer").addEventListener(type, (event) => {
    if (!event.dataTransfer || ![...event.dataTransfer.types].includes("Files")) return;
    event.preventDefault();
    if (type === "drop") attachFiles(event.dataTransfer.files);
  });
}

// --- the phone's viewport ------------------------------------------------------------------
// iOS scrolls the page to bring a focused composer above its keyboard and may leave it shifted, with
// the top bar cut off and the frame showing under the composer. The app follows the visual viewport
// instead and never lets the page itself scroll.
const fullViewport = { width: 0, height: 0 };
function fitViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const root = document.documentElement.style;
  root.setProperty("--viewport-height", Math.round(viewport.height) + "px");
  root.setProperty("--viewport-top", Math.round(viewport.offsetTop) + "px");
  // What the keyboard takes is the layout viewport less the visual one. A keyboard is never under
  // 120px; a smaller difference is a bar the two viewports disagree about, and takes nothing.
  const taken = Math.round(window.innerHeight - viewport.height - viewport.offsetTop);
  root.setProperty("--keyboard-height", (taken >= 120 ? taken : 0) + "px");
  // iOS may pan the visual viewport over the page instead of shrinking it, so an open keyboard pins
  // the app to the visible area wherever that is (see .keyboard-open in the phone rules). The installed
  // app can shrink the window with the keyboard, hiding that difference, so the visible area's own
  // full height (the tallest seen at this width) tells an open keyboard apart too.
  if (Math.round(viewport.width) !== fullViewport.width) Object.assign(fullViewport, { width: Math.round(viewport.width), height: 0 });
  fullViewport.height = Math.max(fullViewport.height, viewport.height);
  const keyboard = Math.max(window.innerHeight, fullViewport.height) - viewport.height;
  document.documentElement.classList.toggle("keyboard-open", keyboard >= 120);
  if (window.scrollY || window.scrollX || viewport.offsetTop) window.scrollTo(0, 0);
  const report = document.getElementById("layout-report");
  if (report) {
    const inset = (side) => getComputedStyle(document.documentElement).getPropertyValue("--inset-" + side).trim() || "?";
    report.textContent = "Layout: screen " + screen.width + "×" + screen.height + " · window " + window.innerWidth + "×"
      + window.innerHeight + " · viewport " + Math.round(viewport.width) + "×" + Math.round(viewport.height)
      + (viewport.offsetTop ? " at " + Math.round(viewport.offsetTop) : "") + " · insets " + inset("top") + "/" + inset("bottom")
      + " · " + (matchMedia("(display-mode: standalone)").matches ? "installed" : "in the browser");
  }
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fitViewport);
  window.visualViewport.addEventListener("scroll", fitViewport);
  window.addEventListener("focusout", () => setTimeout(fitViewport, 50));
  fitViewport();
}
$("message-input").addEventListener("input", () => {
  // Editing a conversation's composer makes its draft the person's own, for that conversation only.
  const destination = target();
  for (const entry of model.outbox.values()) if (entry.target === destination) entry.restored = false;
});
$("message-input").addEventListener("input", (event) => {
  // The box grows when the text overflows and is measured again only when text goes away, so a
  // keystroke that fits leaves it untouched and iOS keeps its suggestions and autocorrect.
  const input = $("message-input");
  const shrinking = !input.value || !event.inputType || event.inputType.startsWith("delete");
  if (shrinking || input.scrollHeight > input.clientHeight) {
    const before = input.style.height;
    if (shrinking) input.style.height = "auto";
    const height = Math.min(input.scrollHeight, 180) + "px";
    if (height !== before || shrinking) input.style.height = height;
  }
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
$("sign-out-others").addEventListener("click", signOutOthers);
$("copy-invitation").addEventListener("click", copyInvitation);
$("share-invitation").addEventListener("click", shareInvitation);
$("device-dialog").addEventListener("close", clearInvitation);
$("device-dialog").addEventListener("cancel", clearInvitation);
$("staff-team").addEventListener("click", () => {
  $("staff-dialog").showModal();
  if (model.view && model.view.kind === "topic") $("staff-topic").value = model.view.key;
});
$("staff-form").addEventListener("submit", staffTeammate);
$("new-channel").addEventListener("click", openChannelDialog);
$("new-routine").addEventListener("click", () => openRoutineDialog());
$("routine-form").addEventListener("submit", saveRoutine);
$("routine-when").addEventListener("change", showRoutineFields);
$("routine-run").addEventListener("click", () => routineAction("run"));
$("routine-toggle").addEventListener("click", () => routineAction(model.routine && model.routine.enabled ? "pause" : "resume"));
$("routine-delete").addEventListener("click", () => routineAction("delete"));
$("routine-open").addEventListener("click", () => { $("routine-dialog").close(); select("topic", model.routine.topic); });
$("channel-form").addEventListener("submit", createChannel);
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
$("archive-toggle").addEventListener("click", () => {
  if (model.view?.kind === "topic") archiveChannel(model.view.key, !(currentTopic() || {}).archived);
});
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
$("nav-activity").addEventListener("click", () => select("activity"));
$("all-activity").addEventListener("click", () => select("activity"));
$("search-form").addEventListener("submit", search);
$("sidebar-toggle").addEventListener("click", () => {
  const open = document.body.classList.toggle("sidebar-open");
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
});
$("sidebar-overlay").addEventListener("click", closeSidebar);
// Whether the reader is at the end of the conversation, as of their last scroll.
let pinnedToEnd = true;
$("message-scroll").addEventListener("scroll", () => {
  const box = $("message-scroll");
  pinnedToEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 110;
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 100)
    $("new-messages").hidden = true;
});
// The composer growing as it is typed in, or the keyboard opening, shortens the message list
// without scrolling it: a reader at the end stays at the end.
if (window.ResizeObserver)
  new ResizeObserver(() => {
    const box = $("message-scroll");
    if (pinnedToEnd && box.scrollHeight - box.scrollTop - box.clientHeight >= 1) box.scrollTop = box.scrollHeight;
  }).observe($("message-scroll"));
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
document.addEventListener("click", (event) => {
  if (!event.target.closest || !event.target.closest("#row-menu")) closeRowMenu();
});
window.addEventListener("blur", closeRowMenu);
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("search").focus();
    $("search").select();
  }
  if (event.key === "Escape") {
    closeRowMenu();
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
session().then((ready) => { if (ready) { refreshState(); changeFeed(); } });
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* Messaging also works without offline assets. */ });
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function visible() {
  return document.hidden ? new Promise((resolve) => document.addEventListener("visibilitychange", function wake() {
    if (!document.hidden) { document.removeEventListener("visibilitychange", wake); resolve(); }
  })) : Promise.resolve();
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !model.authenticated) return;
  // Returning to the tab reads the open conversation; until then its new messages stay unread.
  sidebar();
  header();
  markRead();
});
// --- updates ------------------------------------------------------------------------------------
// A deploy changes the version the change feed reports. The app then reloads itself, but only while
// nothing is in progress: no draft, no image waiting, no field in use, no question half answered.
function noteVersion(version) {
  if (typeof version !== "string" || !version) return;
  if (!model.version) model.version = version;
  else if (version !== model.version) {
    model.updateReady = true;
    reloadWhenIdle();
  }
}
function reloadWhenIdle() {
  if (!model.updateReady || document.hidden) return;
  const active = document.activeElement;
  const busy = $("message-input").value.trim() || model.attachments.length
    || (active && active.matches("input, textarea, select, [contenteditable]"))
    || [...askState.values()].some((state) => !state.sent
      && (Object.values(state.checked).some((list) => list.length) || Object.values(state.typed).some(Boolean)));
  if (!busy) location.reload();
}
document.addEventListener("focusout", () => setTimeout(reloadWhenIdle, 300));
document.addEventListener("visibilitychange", reloadWhenIdle);
async function changeFeed() {
  // One loop per session: it waits for the server to report a change, then refreshes once.
  const generation = model.generation;
  if (model.changeLoop === generation || !model.authenticated || !model.capabilities.changes) return;
  model.changeLoop = generation;
  let fingerprint = "", delay = 1000;
  try {
    while (model.authenticated && generation === model.generation) {
      await visible();
      const controller = new AbortController();
      model.feedAbort = controller;
      try {
        const response = await fetch("/api/changes?since=" + fingerprint + "&wait=25",
          { headers: { Accept: "application/json" }, cache: "no-store", signal: controller.signal });
        // An answer for an ended session never touches the current one.
        if (generation !== model.generation) return;
        if (response.status === 401) return authentication(false);
        let data = await response.json().catch(() => null);
        if (!data || typeof data !== "object" || Array.isArray(data)) data = {};
        if (!response.ok) throw new Error(data.error || "change feed unavailable");
        // A malformed answer takes the bounded back-off path, never an immediate new request.
        if (typeof data.fp !== "string" || !/^[0-9a-f]{32}$/.test(data.fp) || typeof data.changed !== "boolean")
          throw new Error("malformed change feed answer");
        // The first fingerprint may already include changes the initial snapshot missed: read again
        // after it, and wait from that fingerprint so anything during the read is detected next.
        const first = fingerprint === "";
        fingerprint = data.fp;
        noteVersion(data.version);
        if (data.changed || first) await refreshState();
        delay = 1000;
      } catch (_) {
        if (generation !== model.generation) return;
        await sleep(delay);
        delay = Math.min(delay * 2, 10000);
      }
    }
  } finally {
    if (model.changeLoop === generation) model.changeLoop = null;
  }
}
// With the change feed these are a 30 s safety net, which keeps presence expiry honest while idle.
setInterval(() => {
  if (!model.capabilities.changes || Date.now() - model.lastRefresh >= 30000) refreshState();
  else if (model.authenticated) {
    // Cached presence still ages every 4 s without a network read.
    sidebar();
    header();
    updateComposer();
  }
}, 4000);
setInterval(() => {
  if (!model.capabilities.changes && model.authenticated && model.view && model.refreshing !== model.generation)
    refreshView();
}, 3000);
