/**
 * Shares: a guest link to one session, the Worker's side of `Hub.create_share` and friends in
 * one_conv/live/hub.py, run inside the Relay Durable Object over its SQLite storage.
 *
 * A node shares one of its published, online sessions (POST /v1/shares); the grant is bound to
 * that address and only a digest of its secret is stored. Whoever holds `/s/<id>#<secret>`
 * chats with that session alone through /v1/shares/<id>/messages, the secret in the
 * X-Share-Secret header: a guest message becomes a delivery from `guest:<id>` (kind "guest") to
 * the bound session, which answers with a message to `guest:<id>`. A guest reaches no other
 * address, sees only its own exchange, and is refused while the session is offline rather than
 * queued. The person gets a note tagged `share` when a share is made and when its guest first writes.
 *
 * A guest's agent uses the same share through its MCP server, `/s/<id>/mcp` (share-mcp.ts), with
 * the secret as its bearer token. Waiting for an answer holds the request on an in-memory promise
 * that the session's answer (or a revocation) resolves, so a wait costs no CPU.
 *
 * A share made with `transcript` also lets the guest read and search that one session's archived
 * transcript (share-transcript.ts), through /v1/shares/<id>/transcript and the MCP tools. The
 * session's title is part of its content: only such a share shows it.
 */

import { HttpError, boundedBody, errorResponse, json, pyFloat, query, randomHex, sha256, tokenUrlsafe, utf8 } from "./http";
import { pyRepr } from "./pyrepr";
import { USER, online, splitAddress } from "./relay";
import type { Env } from "./env";
import { type ShareAccess, ToolRefused, type View, answer, readArguments, searchArguments } from "./share-mcp";
import * as transcript from "./share-transcript";

export const SHARE_ID = /^[0-9a-f]{16}$/;
export const GUEST = "guest:";
export const SHARE_TTL = 24 * 3600;
const SHARE_TTL_MIN = 60;
const SHARE_TTL_MAX = 30 * 24 * 3600;
// Expired and revoked shares, with their exchange, are kept this long for the owner to list.
const SHARE_KEEP = 7 * 24 * 3600;
const LABEL_LIMIT = 64;
// A guest sends text only, at most GUEST_TEXT bytes, GUEST_RATE messages per GUEST_WINDOW seconds.
export const GUEST_TEXT = 8192;
const GUEST_BODY = 64 * 1024;
export const GUEST_RATE = 20;
const GUEST_WINDOW = 300;
const GUEST_PAGE = 200;
// A guest's GET waits at most this long for an answer, like an inbox long-poll.
const GUEST_WAIT = 30;
const MCP_BODY = 64 * 1024;
const MAX_SECRET = 256;
export const SHARE_HEADER = "x-share-secret";
export const INACTIVE = "this link is no longer active";
const SHARE_TAG = "share";
const BUSY = ["busy", "waiting"];

export const SHARE_SCHEMA = `
CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, hash TEXT NOT NULL, node TEXT NOT NULL, address TEXT NOT NULL,
    label TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, revoked REAL, first_used REAL);
CREATE TABLE IF NOT EXISTS share_messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, share TEXT NOT NULL,
    side TEXT NOT NULL, from_node TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, text TEXT NOT NULL,
    created REAL NOT NULL, post INTEGER, UNIQUE (from_node, id));
CREATE INDEX IF NOT EXISTS share_exchange ON share_messages (share, seq);
`;
export const SHARE_TABLES = ["share_messages", "shares"];

type Body = Record<string, unknown>;
interface Session {
  name: string | null;
  title: string;
  status: string;
  online: boolean;
}
interface Active {
  node: string;
  address: string;
  label: string;
  expires: number;
  firstUsed: number | null;
  transcript: boolean;
}

const now = () => Date.now() / 1000;

/** Equal strings, compared in time that depends only on their length. */
function sameDigest(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function printable(label: string): boolean {
  return [...label].every((char) => char.charCodeAt(0) >= 32 && char.charCodeAt(0) !== 127);
}

export class Shares {
  // Requests waiting for an answer, per share, woken when the session answers or the share is revoked.
  private waiting = new Map<string, Set<() => void>>();

  constructor(
    private readonly storage: DurableObjectStorage,
    // Wakes the bridge of `node` for a new delivery, as a relayed message does.
    private readonly wake: (node: string, delivery: string) => void,
    // The archive a transcript grant reads from.
    private readonly env: Env,
  ) {
    // A relay stored before transcript grants gains the flag, every old share being messaging only.
    if (!this.sql.exec("PRAGMA table_info(shares)").toArray().some((column) => column.name === "transcript")) {
      this.sql.exec("ALTER TABLE shares ADD COLUMN transcript INTEGER NOT NULL DEFAULT 0");
    }
  }

  private get sql(): SqlStorage {
    return this.storage.sql;
  }

  private notify(ident: string): void {
    for (const resolve of this.waiting.get(ident) ?? []) resolve();
  }

  private waitFor(ident: string, ms: number): Promise<void> {
    return new Promise((resolve) => {
      const waiters = this.waiting.get(ident) ?? new Set();
      this.waiting.set(ident, waiters);
      const done = () => {
        clearTimeout(timer);
        waiters.delete(done);
        if (!waiters.size) this.waiting.delete(ident);
        resolve();
      };
      const timer = setTimeout(done, ms);
      waiters.add(done);
    });
  }

  /** The guest view once the session answered after `after`, or when `seconds` ran out; a revoked share ends it with its error. */
  async waitView(ident: string, secret: string | null, after: number, seconds: number): Promise<View> {
    const deadline = Date.now() + Math.max(0, seconds) * 1000;
    for (;;) {
      const view = (await this.view(ident, secret, after)) as unknown as View;
      const left = deadline - Date.now();
      if (view.messages.some((m) => m.from === "agent") || left <= 0) return view;
      await this.waitFor(ident, left);
    }
  }

  /** A note the relay itself files for the person, tagged `share`. */
  private async fileNote(node: string, sender: string, name: string, text: string): Promise<() => void> {
    const digest = await sha256(JSON.stringify([USER, sender, name, text, SHARE_TAG]));
    const ident = randomHex(16);
    return () =>
      this.sql.exec(
        "INSERT INTO notes (from_node, id, digest, sender, name, text, tags, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        node, ident, digest, sender, name, text, JSON.stringify([SHARE_TAG]), now(),
      );
  }

  /** The published session's title, name, status and whether its bridge holds it online; null if unpublished. */
  private session(node: string, address: string): Session | null {
    const row = this.sql.exec("SELECT name, title, status, updated FROM sessions WHERE node = ? AND address = ?", node, address).toArray()[0];
    if (!row) return null;
    const seen = this.sql.exec("SELECT seen FROM nodes WHERE node = ?", node).toArray()[0];
    return {
      name: (row.name as string | null) ?? null,
      title: (row.title as string) || "",
      status: (row.status as string) || "",
      online: online((seen?.seen as number) ?? 0, row.updated as number, now()),
    };
  }

  /**
   * A guest link bound to one session this node published and holds online; the secret is returned once.
   * `transcript` also lets the guest read and search that session's archived transcript, and nothing else.
   */
  async create(node: string, body: Body): Promise<Body> {
    const { address } = body;
    const label = "label" in body ? body.label : "guest";
    const ttl = "ttl" in body ? body.ttl : SHARE_TTL;
    const granted = "transcript" in body ? body.transcript : false;
    if (typeof granted !== "boolean") throw new HttpError(400, "transcript is true or false");
    if (typeof address !== "string" || !address) throw new HttpError(400, "address is the session to share, harness:id or NODE/harness:id");
    let local = address;
    if (address.includes("/")) {
      const [owner, rest] = splitAddress(address);
      if (owner !== node) throw new HttpError(403, `${address} is another node's session; share it from that machine`);
      local = rest;
    }
    if (!local.includes(":")) throw new HttpError(400, `a session address is harness:id, not ${pyRepr(address)}`);
    if (typeof label !== "string" || !label.trim() || label.length > LABEL_LIMIT || !printable(label)) {
      throw new HttpError(400, `label is a name of at most ${LABEL_LIMIT} printable characters`);
    }
    if (typeof ttl !== "number" || !Number.isInteger(ttl) || ttl < SHARE_TTL_MIN || ttl > SHARE_TTL_MAX) {
      throw new HttpError(400, `ttl is a whole number of seconds from ${SHARE_TTL_MIN} to ${SHARE_TTL_MAX}`);
    }
    const name = label.trim();
    const ident = randomHex(8);
    const secret = tokenUrlsafe();
    const hash = await sha256(secret);
    const at = now();
    const until = new Date((at + ttl) * 1000).toISOString().slice(0, 16).replace("T", " ");
    const note = await this.fileNote(
      node,
      local,
      name,
      `Shared ${node}/${local}${granted ? " with transcript" : ""} with ${name} until ${until} UTC (share ${ident}). Revoke it with \`one-conv live share --revoke ${ident} --yes\`.`,
    );
    this.storage.transactionSync(() => {
      const state = this.session(node, local);
      if (!state || !state.online) {
        throw new HttpError(409, `${node}/${local} is not reachable through the relay: its machine's bridge must be running (\`one-conv live serve\`)`);
      }
      const stale = this.sql.exec("SELECT id FROM shares WHERE expires < ?", at - SHARE_KEEP).toArray();
      for (const row of stale) {
        this.sql.exec("DELETE FROM share_messages WHERE share = ?", row.id);
        this.sql.exec("DELETE FROM shares WHERE id = ?", row.id);
      }
      this.sql.exec(
        "INSERT INTO shares (id, hash, node, address, label, created, expires, transcript) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ident, hash, node, local, name, at, at + ttl, granted ? 1 : 0,
      );
      note();
    });
    return { id: ident, secret, address: `${node}/${local}`, label: name, expires: at + ttl, path: `/s/${ident}`, transcript: granted };
  }

  /** This node's shares, newest first, with their state and how many messages each guest sent. */
  list(node: string): Body {
    const at = now();
    const rows = this.sql
      .exec(
        "SELECT s.id, s.address, s.label, s.created, s.expires, s.revoked, s.first_used, s.transcript, (SELECT COUNT(*) FROM share_messages m WHERE m.share = s.id AND m.side = 'guest') AS guest_messages FROM shares s WHERE s.node = ? ORDER BY s.created DESC",
        node,
      )
      .toArray();
    return {
      shares: rows.map((row) => ({
        id: row.id,
        address: `${node}/${row.address}`,
        label: row.label,
        created: row.created,
        expires: row.expires,
        revoked: row.revoked,
        first_used: row.first_used,
        guest_messages: row.guest_messages,
        transcript: row.transcript === 1,
        state: row.revoked !== null ? "revoked" : (row.expires as number) < at ? "expired" : "active",
      })),
    };
  }

  /** End one of this node's shares at once; its page then says the link is no longer active. */
  revoke(node: string, ident: string): Body {
    const changed = this.sql.exec("UPDATE shares SET revoked = COALESCE(revoked, ?) WHERE id = ? AND node = ?", now(), ident, node).rowsWritten;
    if (!changed) throw new HttpError(404, `no share ${ident} made by this node`);
    this.notify(ident);
    return { id: ident, revoked: true };
  }

  /** The share whose secret matches; an unknown id and a wrong secret answer alike, so a guess learns nothing. */
  private async active(ident: string, secret: string | null): Promise<() => Active> {
    if (!SHARE_ID.test(ident) || !secret || secret.length > MAX_SECRET) throw new HttpError(404, INACTIVE);
    const digest = await sha256(secret);
    // Read inside the caller's transaction, after the digest (an await) is ready.
    return () => {
      const row = this.sql.exec("SELECT hash, node, address, label, expires, revoked, first_used, transcript FROM shares WHERE id = ?", ident).toArray()[0];
      if (!sameDigest((row?.hash as string) ?? "0".repeat(64), digest) || !row) throw new HttpError(404, INACTIVE);
      if (row.revoked !== null || (row.expires as number) < now()) throw new HttpError(410, INACTIVE);
      return { node: row.node as string, address: row.address as string, label: row.label as string, expires: row.expires as number, firstUsed: row.first_used as number | null, transcript: row.transcript === 1 };
    };
  }

  /** The share's label, its session's title and state, and the exchange after `after`, oldest first. */
  async view(ident: string, secret: string | null, after: number): Promise<Body> {
    const share = (await this.active(ident, secret))();
    const state = this.session(share.node, share.address) ?? { name: null, title: "", status: "", online: false };
    const rows = this.sql
      .exec(
        "SELECT m.seq, m.side, m.text, m.created, d.outcome FROM share_messages m LEFT JOIN deliveries d ON d.post = m.post WHERE m.share = ? AND m.seq > ? ORDER BY m.seq LIMIT ?",
        ident,
        Math.max(0, after),
        GUEST_PAGE,
      )
      .toArray();
    return {
      // The title is part of the session's content, shown only to a share that grants the transcript.
      share: { label: share.label, title: share.transcript ? state.title : "", name: state.name, online: state.online, busy: BUSY.includes(state.status), expires: share.expires, transcript: share.transcript },
      messages: rows.map((row) => ({
        seq: row.seq,
        from: row.side,
        text: row.text,
        created: row.created,
        ...(row.side === "guest" ? { status: row.outcome ?? "sent" } : {}),
      })),
    };
  }

  /** Route a guest's text to the share's session as a delivery from guest:<id>; refused while it is offline. */
  async send(ident: string, secret: string | null, text: unknown): Promise<Body> {
    if (typeof text !== "string" || !text.trim()) throw new HttpError(400, "text is the message to send");
    if (new TextEncoder().encode(text).length > GUEST_TEXT) throw new HttpError(413, `a message is at most ${GUEST_TEXT / 1024} KB`);
    const read = await this.active(ident, secret);
    const sender = `${GUEST}${ident}`;
    const msgId = randomHex(16);
    const delivery = randomHex(16);
    const at = now();
    // Everything awaited is ready before the transaction: its checks and writes then happen as one step.
    const share = read();
    const target = `${share.node}/${share.address}`;
    const digest = await sha256(JSON.stringify([target, sender, text]));
    const note = await this.fileNote(share.node, sender, share.label, `${share.label} sent a first message through share ${ident} to ${target}.`);
    const result = this.storage.transactionSync(() => {
      const current = read();
      const state = this.session(current.node, current.address);
      if (!state || !state.online) throw new HttpError(409, "the agent is offline; this message was not sent");
      const recent = this.sql.exec("SELECT COUNT(*) AS n FROM share_messages WHERE share = ? AND side = 'guest' AND created > ?", ident, at - GUEST_WINDOW).one().n as number;
      if (recent >= GUEST_RATE) throw new HttpError(429, `at most ${GUEST_RATE} messages per ${GUEST_WINDOW / 60} minutes; wait a little`);
      const seq = this.sql
        .exec(
          "INSERT INTO posts (from_node, id, digest, target, sender, reply, mode, text, created, attachments, timing) VALUES ('', ?, ?, ?, ?, 'expected', 'push', ?, ?, NULL, 'now') RETURNING seq",
          msgId, digest, target, sender, text, at,
        )
        .one().seq as number;
      this.sql.exec("INSERT INTO deliveries (delivery, post, to_node, to_address) VALUES (?, ?, ?, ?)", delivery, seq, current.node, current.address);
      const row = this.sql
        .exec("INSERT INTO share_messages (share, side, from_node, id, digest, text, created, post) VALUES (?, 'guest', '', ?, ?, ?, ?, ?) RETURNING seq", ident, msgId, digest, text, at, seq)
        .one().seq as number;
      if (current.firstUsed === null) {
        this.sql.exec("UPDATE shares SET first_used = ? WHERE id = ?", at, ident);
        note();
      }
      return { seq: row, node: current.node };
    });
    this.wake(result.node, delivery);
    return { seq: result.seq, status: "sent" };
  }

  /** File the shared session's answer for its guest's page; only that session (or its owner) may answer. */
  async reply(node: string, id: string, sender: unknown, text: string, target: string, attached: boolean): Promise<Body> {
    if (attached) throw new HttpError(400, "a message to a guest carries text only");
    const ident = target.slice(GUEST.length);
    if (!SHARE_ID.test(ident)) throw new HttpError(400, `a guest address is guest:<share id>, not ${pyRepr(target)}`);
    const digest = await sha256(JSON.stringify([target, sender ?? null, text]));
    const receipt = { id, recipients: [{ to: target, status: "stored" }] };
    const stored = this.storage.transactionSync(() => {
      const existing = this.sql.exec("SELECT digest FROM share_messages WHERE from_node = ? AND id = ?", node, id).toArray()[0];
      if (existing) {
        if (existing.digest !== digest) throw new HttpError(409, "this id was already used for a different message; send it under a new id");
        return { ...receipt, duplicate: true };
      }
      const row = this.sql.exec("SELECT node, address, expires, revoked FROM shares WHERE id = ?", ident).toArray()[0];
      if (!row) throw new HttpError(404, `no share ${ident}`);
      if (row.node !== node || (sender !== row.address && sender !== USER)) throw new HttpError(403, `only the shared session ${row.node}/${row.address} answers ${target}`);
      if (row.revoked !== null || (row.expires as number) < now()) throw new HttpError(410, `share ${ident} is no longer active; the guest cannot read new messages`);
      this.sql.exec("INSERT INTO share_messages (share, side, from_node, id, digest, text, created, post) VALUES (?, 'agent', ?, ?, ?, ?, ?, NULL)", ident, node, id, digest, text, now());
      return { ...receipt, duplicate: false };
    });
    // An `ask` or a waiting GET returns as soon as the answer is stored.
    if (!stored.duplicate) this.notify(ident);
    return stored;
  }

  /** The rendered turns of the one session the share names, if it grants the transcript. */
  private async granted(ident: string, secret: string | null, raw: boolean): Promise<{ turns: transcript.RenderedTurns; mtime: number | null }> {
    const share = (await this.active(ident, secret))();
    if (!share.transcript) throw new HttpError(403, "this share does not grant the session's transcript");
    return transcript.load(this.env, share.node, share.address, raw);
  }

  /** A page of the shared session's turns, as the remote MCP's read_conversation pages them. */
  async readTranscript(ident: string, secret: string | null, args: { offset: number | null; limit: number; around: string | null; raw: boolean }): Promise<Body> {
    const { turns, mtime } = await this.granted(ident, secret, args.raw);
    return { ...transcript.read(turns, args.offset, args.limit, args.around), archived_through: mtime };
  }

  /** Turns of the shared session holding every word of `query`, newest first; hidden blocks are never searched. */
  async searchTranscript(ident: string, secret: string | null, query: string, limit: number): Promise<Body> {
    const { turns, mtime } = await this.granted(ident, secret, false);
    return { ...transcript.search(turns, query, limit), archived_through: mtime };
  }

  async archivedThrough(ident: string, secret: string | null): Promise<number | null> {
    const share = (await this.active(ident, secret))();
    return share.transcript ? ((await transcript.archived(this.env, share.node, share.address))?.mtime ?? null) : null;
  }

  /** GET /v1/shares/<id>/transcript and /transcript/search: the share's secret in its header, as for its messages. */
  async transcriptRoute(request: Request, ident: string, searching: boolean): Promise<Response> {
    try {
      const header = request.headers.get(SHARE_HEADER);
      const secret = header && !header.includes(",") ? header : null;
      const url = new URL(request.url);
      const whole = (name: string): number | undefined => {
        const value = query(url, name);
        if (value === null) return undefined;
        if (!/^\d+$/.test(value)) throw new HttpError(400, "offset and limit are whole numbers");
        return Number(value);
      };
      let result: Body;
      try {
        if (searching) {
          const [words, limit] = searchArguments({ query: query(url, "query") ?? "", limit: whole("limit") });
          result = await this.searchTranscript(ident, secret, words, limit);
        } else {
          const raw = ["1", "true"].includes(query(url, "raw") ?? "");
          result = await this.readTranscript(ident, secret, readArguments({ offset: whole("offset"), limit: whole("limit"), around: query(url, "around") ?? undefined, raw }));
        }
      } catch (error) {
        if (error instanceof HttpError) throw error;
        if (error instanceof ToolRefused) throw new HttpError(400, error.message);
        throw error;
      }
      const response = json(result);
      response.headers.set("cache-control", "no-store");
      return response;
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }

  /** GET and POST /v1/shares/<id>/messages: no node token, the share's secret in its header is the credential. */
  async guest(request: Request, ident: string): Promise<Response> {
    try {
      // Exactly one header: a repeated one arrives joined with ", ", which no secret contains.
      const header = request.headers.get(SHARE_HEADER);
      const secret = header && !header.includes(",") ? header : null;
      if (request.method === "GET" || request.method === "HEAD") {
        const url = new URL(request.url);
        const after = Math.trunc(pyFloat(query(url, "after") ?? "0") ?? 0);
        const wait = pyFloat(query(url, "wait") ?? "0") ?? 0;
        // `wait` holds the request until the session answers after `after`, as an inbox long-poll does.
        const seconds = Number.isFinite(wait) ? Math.min(Math.max(wait, 0), GUEST_WAIT) : 0;
        const response = json(await this.waitView(ident, secret, Number.isFinite(after) ? after : 0, seconds));
        response.headers.set("cache-control", "no-store");
        return response;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(utf8(await boundedBody(request, GUEST_BODY, new HttpError(413, "request too large"))) || "{}");
      } catch (error) {
        if (error instanceof HttpError) throw error;
        throw new HttpError(400, "invalid JSON");
      }
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new HttpError(400, "the request body must be a JSON object");
      return json(await this.send(ident, secret, (parsed as Body).text));
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }

  /** POST /s/<id>/mcp: the share's MCP server, its secret as the bearer token; no OAuth. */
  async mcp(request: Request, ident: string): Promise<Response> {
    try {
      const header = request.headers.get("authorization") ?? "";
      const space = header.indexOf(" ");
      const secret = space > 0 && header.slice(0, space).toLowerCase() === "bearer" ? header.slice(space + 1) : "";
      if (!secret || /[\s,]/.test(secret)) {
        return Response.json({ error: "this share's MCP server takes its secret as `Authorization: Bearer <secret>`" }, { status: 401, headers: { "www-authenticate": "Bearer" } });
      }
      const access: ShareAccess = {
        send: async (text) => (await this.send(ident, secret, text)).seq as number,
        view: async (after) => (await this.view(ident, secret, after)) as unknown as View,
        wait: (after, seconds) => this.waitView(ident, secret, after, seconds),
        read: (args) => this.readTranscript(ident, secret, args),
        search: (words, limit) => this.searchTranscript(ident, secret, words, limit),
        archivedThrough: () => this.archivedThrough(ident, secret),
      };
      // Checked before any message, so a dead link fails at connect rather than at the first tool.
      await access.view(0);
      let body: unknown;
      try {
        body = JSON.parse(utf8(await boundedBody(request, MCP_BODY, new HttpError(413, "request too large"))));
      } catch (error) {
        if (error instanceof HttpError) throw error;
        return Response.json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }, { status: 400 });
      }
      const messages = (Array.isArray(body) ? body : [body]) as Parameters<typeof answer>[0][];
      const answers: Body[] = [];
      for (const message of messages) {
        const reply = await answer(message, access);
        if (reply !== null) answers.push(reply);
      }
      if (!answers.length) return new Response(null, { status: 202 });
      return Response.json(Array.isArray(body) ? answers : answers[0]);
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }
}
