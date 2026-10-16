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
  const name =
    (session && session.name) ||
    (alias && alias[0]) ||
    harnesses[kind] ||
    "Agent";
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
    session,
  };
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
  for (const session of model.state.sessions) {
    const p = person(session.address);
    const button = rowButton(
      people,
      p.name,
      avatar(session.address, "tiny-avatar"),
      false,
      () => select("direct", pairOf(session.address)),
    );
    const label = button.querySelector(".row-label");
    label.classList.add("agent-label");
    const title =
      session.title && !session.title.startsWith("one-conv-cli-")
        ? session.title
        : p.kind +
          " · " +
          (p.status === "busy"
            ? "Working"
            : p.status === "idle"
              ? "Available"
              : p.status === "loaded"
                ? "Connected"
                : p.status);
    label.replaceChildren(
      el("span", p.name, "agent-name"),
      el("small", title, "agent-context"),
    );
    const presence = el(
      "span",
      null,
      "row-presence" +
        (["idle", "busy", "loaded"].includes(p.status) ? " online" : ""),
    );
    presence.title =
      p.status === "busy"
        ? "Working"
        : p.status === "idle"
          ? "Available"
          : p.status;
    button.appendChild(presence);
    button.title = p.name + " · " + p.kind + " · " + presence.title;
  }
  if (!people.children.length)
    people.appendChild(
      el("li", "No agents are connected yet.", "sidebar-empty"),
    );
  $("agent-count").textContent = model.state.sessions.length || "";
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
          ? "A direct line between you and your agent"
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
  const signature = JSON.stringify([model.view, rows, model.state.names]);
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
  $("message-input").value = model.drafts.get(target()) || "";
  $("message-input").style.height = "auto";
  model.signature = "";
  model.first = true;
  model.rows = [];
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
  if (model.refreshing) return;
  model.refreshing = true;
  try {
    const [state, direct] = await Promise.all([
      get("/api/state"),
      get("/api/direct"),
    ]);
    model.state = {
      ...state,
      topics: state.topics || [],
      sessions: state.sessions || [],
      names: state.names || {},
    };
    model.threads = direct.threads || [];
    connected(true);
    if (!model.view) {
      const params = new URLSearchParams(location.hash.slice(1));
      if (
        params.has("topic") &&
        state.topics.some((t) => t.topic === params.get("topic"))
      )
        select("topic", params.get("topic"));
      else if (params.has("direct")) select("direct", params.get("direct"));
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
    notice("The workspace is reconnecting. Your draft is safe.", true);
  } finally {
    model.refreshing = false;
  }
}
function updateComposer() {
  const destination = target();
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
    model.sending ||
    !$("message-input").value.trim();
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
    $("show-costs").hidden = !model.capabilities.accounting;
    $("logout-device").hidden = !data.authenticated;
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
  try {
    await post("/api/logout", {});
    model.state = { topics: [], sessions: [], names: {} };
    model.threads = [];
    model.rows = [];
    model.view = null;
    model.drafts.clear();
    model.pending.clear();
    try { sessionStorage.removeItem("oneconv:staffing"); } catch (_) { /* Some browsers disable storage. */ }
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
    item.append(el("span", (spec.name || "A").slice(0, 2).toUpperCase(), "avatar codex"));
    const text = el("div");
    const online = job.status === "ready" && model.state.sessions.some(
      (peer) => peer.address === job.address && peer.online !== false);
    text.append(el("strong", spec.name || "Teammate"), el("small", job.status === "ready" && !online ?
      "Offline · check the connection" : jobLabels[job.status] || job.status || "Preparing"));
    if (job.error || job.detail) text.append(el("p", job.error || job.detail, "job-detail"));
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
        (job.status === "unconfirmed" && job.box_id)) {
      const actions = el("div", null, "job-actions");
      if (["auth_needed", "unverified", "failed"].includes(job.status) ||
          (job.status === "unconfirmed" && job.phase === "bootstrapping") || (job.status === "ready" && !online)) {
        const check = el("button", "Check again", "secondary-button");
        check.type = "button";
        check.addEventListener("click", () => workerAction("recheck", job.id));
        actions.append(check);
      }
      const stop = el("button", "Stop teammate", "secondary-button");
      stop.type = "button";
      stop.addEventListener("click", () => workerAction("stop", job.id));
      actions.append(stop);
      item.append(actions);
    }
    if (job.status === "unconfirmed" && job.phase === "creating" && !job.box_id) {
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
    const item = el("article", null, "worker-job pool-job");
    const detail = el("div");
    detail.append(el("strong", pool.name || "Shared worker box"),
      el("small", pool.status === "stopped" ? "Stopped · files preserved" : "Shared compute"));
    const action = pool.status === "stopped" ? "resume_pool" : "stop_pool";
    const button = el("button", action === "resume_pool" ? "Resume box" : "Stop box", "secondary-button");
    button.type = "button";
    button.addEventListener("click", () => workerAction(action, pool.id));
    item.append(detail, button);
    container.append(item);
  }
}

async function workerAction(action, id) {
  try {
    await post("/api/providers/workers", { action, id });
    await refreshWorkers();
  } catch (error) { $("staff-feedback").textContent = error.message; }
}

async function refreshWorkers() {
  if (!model.capabilities.workers || !model.authenticated) return;
  try {
    model.workers = await get("/api/providers/workers");
    renderJobs();
    const picker = $("staff-profile"), selected = picker.value;
    picker.replaceChildren();
    const profiles = Array.isArray(model.workers.profiles) ? model.workers.profiles :
      Object.entries(model.workers.profiles || {}).map(([id, profile]) => ({ ...profile, id }));
    for (const profile of profiles) {
      const option = el("option", profile.label || profile.name || profile.id);
      option.value = profile.id || profile.name;
      picker.append(option);
    }
    if (!picker.children.length) {
      const option = el("option", "Default"); option.value = "default"; picker.append(option);
    }
    if ([...picker.options].some((option) => option.value === selected)) picker.value = selected;
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
        : "Sent as You · Human.",
      data.status === "unconfirmed" ? "error" : "success",
    );
    model.first = true;
    await refreshState();
    if (data.status !== "unconfirmed") toast("Your message is on its way");
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
      "A shared conversation between you and the agents doing the work. Choose a person below to send them a message.",
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
        p.kind +
          (p.status === "busy"
            ? " · Working"
            : p.status === "idle"
              ? " · Available"
              : ""),
      ),
    );
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
    option(
      p.name,
      p.kind + (agent.title ? " · " + agent.title : ""),
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
$("staff-team").addEventListener("click", () => {
  $("staff-dialog").showModal();
  if (model.view && model.view.kind === "topic") $("staff-topic").value = model.view.key;
  refreshWorkers();
});
$("staff-form").addEventListener("submit", staffTeammate);
$("show-costs").addEventListener("click", showCosts);
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => $(button.dataset.close).close());
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
$("nav-home").addEventListener("click", () =>
  select(
    model.state.topics.length ? "topic" : "home",
    model.state.topics.length ? model.state.topics[0].topic : "",
  ),
);
$("workspace-home").addEventListener("click", (event) => {
  event.preventDefault();
  $("nav-home").click();
});
$("nav-direct").addEventListener("click", () => {
  if (model.threads.length) select("direct", model.threads[0].pair);
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
