/**
 * The live relay as one SQLite-backed Durable Object: node tokens, published sessions, posts,
 * deliveries and attachment metadata, with the HTTP API of `one_conv/live/hub.py`.
 *
 * A message is a post kept once under (sending node, client-generated id) with a hash of its
 * payload, so a retry returns the first receipt and a different payload under the same id is a
 * conflict. A post becomes one delivery, which the receiving bridge acknowledges with an outcome
 * the sender reads back. Only token hashes are stored.
 *
 * One instance serializes every request, so a long-poll inbox waits on an in-memory promise
 * that a send to its node resolves, instead of polling storage. Attachment bodies live in R2:
 * an image may reach 10 MB and a Durable Object SQLite value is capped at 2 MB.
 *
 * A bridge can instead hold GET /v1/stream, a hibernatable WebSocket: each pending delivery is
 * pushed as a frame `{"messages": [delivery]}` (the /v1/inbox body with one delivery) when the
 * socket opens and when a send creates it, and acks still go through POST /v1/acks. The object
 * sleeps between frames: a socket is found again by its node tag, never through instance memory,
 * and the runtime answers the bridge's "ping" with "pong" without waking it.
 *
 * A message to `user` is for the person, not an agent: it is filed as a note with optional tags
 * that any of the person's nodes reads back in order through GET /v1/user, and no bridge is woken.
 *
 * A connected node (or the admin API) can invite a new machine: POST /v1/invites returns a
 * one-time code bound to a node name, kept only as a digest, that expires within a day.
 * POST /v1/join, unauthenticated, trades the code for that node's token exactly once.
 */

import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import { HttpError, type Route, bearer, boundedBody, decodedPath, dispatch, errorResponse, json, path, pyFloat, query, randomHex, sha256, tokenUrlsafe, utf8 } from "./http";
import { pyLen, pyRepr, pyStr, truthy } from "./pyrepr";

export const NODE = /^[a-z0-9][a-z0-9-]{0,31}$/;
const MESSAGE_ID = /^[0-9a-f]{32}$/;
const ATTACHMENT_ID = /^[0-9a-f]{32}$/;
// `held` is provisional: the bridge keeps a when=idle message for a busy session and reports
// a final outcome once it leaves; a held delivery is not handed out again.
const OUTCOMES = ["accepted", "refused", "unconfirmed", "held"];
const MODES = ["push", "fyi"];
// now: delivered at once; idle: the receiving bridge holds it while the session is busy.
const WHEN = ["now", "idle"];
const REPLIES = ["expected", "none", "inline"];
export const ATTACHMENT_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
export const ATTACHMENT_LIMIT = 10_000_000;
const ATTACHMENTS_PER_MESSAGE = 4;
export const MAX_TEXT = 200_000;
// A JSON-escaped text can take up to six bytes per character, plus the envelope fields.
export const MAX_BODY = MAX_TEXT * 6 + 4096;
export const MAX_WAIT = 30;
// A node that has not published or polled for this long is listed as offline.
export const LEASE_SECONDS = 60;
const PRODUCTS = ["cursor-cli", "cursor-ide"];
const INBOX_LIMIT = 50;
// The address of the person every node works for; no session can publish it.
export const USER = "user";
export const TAG = /^[a-z0-9][a-z0-9-]{0,31}$/;
const TAGS_PER_NOTE = 8;
const NAME_LIMIT = 200;
export const NOTES_LIMIT = 200;
// Keepalive text frames the runtime answers itself, so a ping never wakes a hibernated object.
export const PING = "ping";
export const PONG = "pong";
// Close code for a socket whose token was revoked or replaced: the bridge reads it as a 401.
export const REVOKED = 4401;
// An invite is short-lived: long enough to walk to the other machine, never a standing credential.
export const INVITE_TTL = 15 * 60;
const INVITE_TTL_MIN = 60;
const INVITE_TTL_MAX = 24 * 3600;
// Used and expired invites are kept this long for the operator to see, then pruned.
const INVITE_KEEP = 7 * 24 * 3600;
const MAX_CODE = 256;
const JOIN_BODY = 4096;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS tokens (node TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (node TEXT NOT NULL, address TEXT NOT NULL, name TEXT, title TEXT, cwd TEXT,
    status TEXT, updated REAL NOT NULL, product TEXT NOT NULL DEFAULT '', PRIMARY KEY (node, address));
CREATE INDEX IF NOT EXISTS session_names ON sessions (name);
CREATE TABLE IF NOT EXISTS posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, reply TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'push', text TEXT NOT NULL, created REAL NOT NULL, attachments TEXT,
    timing TEXT NOT NULL DEFAULT 'now', UNIQUE (from_node, id));
CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, post INTEGER NOT NULL REFERENCES posts (seq),
    to_node TEXT NOT NULL, to_address TEXT NOT NULL, taken REAL, outcome TEXT, outcome_at REAL,
    UNIQUE (post, to_node, to_address));
CREATE INDEX IF NOT EXISTS pending ON deliveries (to_node, outcome, post);
CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, node TEXT NOT NULL, media_type TEXT NOT NULL,
    size INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notes (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, sender TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL, tags TEXT NOT NULL,
    created REAL NOT NULL, UNIQUE (from_node, id));
CREATE TABLE IF NOT EXISTS invites (hash TEXT PRIMARY KEY, node TEXT NOT NULL, inviter TEXT NOT NULL,
    takes_over INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, used REAL);
`;
// The columns a delivery is handed out with, by /v1/inbox and /v1/stream alike.
const PENDING =
  "SELECT d.delivery, p.id, d.to_address, p.from_node, p.sender, p.reply, p.mode, p.text, p.created, p.attachments, p.timing FROM deliveries d JOIN posts p ON p.seq = d.post";
const TABLES = ["tokens", "nodes", "sessions", "deliveries", "posts", "attachments", "notes", "invites"];

type Row = Record<string, SqlStorageValue>;
type Body = Record<string, unknown>;
type Work = (node: string, body: Body, request: Request, params: Record<string, string>) => Promise<unknown> | unknown;

const now = () => Date.now() / 1000;

/** Bridge and publication must both be recent: a polling bridge cannot keep an old publication alive. */
export function online(seen: number, published: number, at: number): boolean {
  return at - seen >= 0 && at - seen <= LEASE_SECONDS && at - published >= 0 && at - published <= LEASE_SECONDS;
}

/** Split a remote session address NODE/harness:id. */
export function splitAddress(address: string): [string, string] {
  const slash = address.indexOf("/");
  const node = slash < 0 ? address : address.slice(0, slash);
  const local = slash < 0 ? "" : address.slice(slash + 1);
  if (slash < 0 || !NODE.test(node) || !local.includes(":") || local.includes("/")) {
    throw new HttpError(400, `a remote address is NODE/harness:id or @name, not ${pyRepr(address)}`);
  }
  return [node, local];
}

export class Relay extends DurableObject<Env> {
  private sql: SqlStorage;
  // Long-polling inboxes per node, woken when a delivery for that node is stored.
  private waiting = new Map<string, Set<() => void>>();
  private routes: Route<null>[];

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(SCHEMA);
    // A relay stored before `when` existed gains the column, every old post being `now`.
    if (!this.sql.exec("PRAGMA table_info(posts)").toArray().some((column) => column.name === "timing")) {
      this.sql.exec("ALTER TABLE posts ADD COLUMN timing TEXT NOT NULL DEFAULT 'now'");
    }
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair(PING, PONG));
    const work = (handler: Work) => (request: Request, params: Record<string, string>) => this.handle(request, params, handler);
    this.routes = [
      { pattern: path("/v1/whoami"), methods: ["GET"], handle: work((node) => ({ node })) },
      {
        pattern: path("/v1/sessions"),
        methods: ["GET", "PUT"],
        handle: work((node, body, request) => (request.method === "PUT" ? { published: this.publish(node, body.sessions) } : { sessions: this.directory() })),
      },
      { pattern: path("/v1/messages"), methods: ["POST"], handle: work((node, body) => this.send(node, body)) },
      { pattern: path("/v1/messages/{msg_id}"), methods: ["GET"], handle: work((node, _, __, params) => this.status(node, params.msg_id)) },
      { pattern: path("/v1/inbox"), methods: ["GET"], handle: work(async (node, _, request) => ({ messages: await this.inbox(node, waitParam(request)) })) },
      { pattern: path("/v1/stream"), methods: ["GET"], handle: (request) => this.stream(request) },
      { pattern: path("/v1/acks"), methods: ["POST"], handle: work((node, body) => this.ack(node, pyStr(body.delivery ?? null), pyStr(body.outcome ?? null))) },
      { pattern: path("/v1/attachments"), methods: ["POST"], handle: (request) => this.upload(request) },
      { pattern: path("/v1/attachments/{ident}"), methods: ["GET"], handle: (request, params) => this.download(request, params.ident) },
      { pattern: path("/v1/user"), methods: ["GET"], handle: work((_, __, request) => this.notes(new URL(request.url))) },
      { pattern: path("/v1/invites"), methods: ["POST"], handle: work((node, body) => this.invite(node, body)) },
      { pattern: path("/v1/join"), methods: ["POST"], handle: (request) => this.join(request) },
    ];
  }

  fetch(request: Request): Promise<Response> {
    return dispatch(this.routes, request, decodedPath(new URL(request.url)), null);
  }

  // --- tokens: minted through the admin API, never by a node --------------------------------------

  async addToken(node: string): Promise<{ token?: string; error?: string }> {
    if (!NODE.test(node)) return { error: `node names are lowercase letters, digits and dashes: ${pyRepr(node)}` };
    const token = tokenUrlsafe();
    this.sql.exec("INSERT OR REPLACE INTO tokens VALUES (?, ?, ?)", node, await sha256(token), now());
    // A socket opened with the replaced token must not outlive it.
    this.closeStreams(node, "this node's token was replaced");
    return { token };
  }

  /**
   * A one-time code that POST /v1/join trades for NODE's token; `by` is the inviting node ("" for
   * the admin API). A node that already has a token is refused unless `replace`, so an invite
   * never silently takes over a running machine's identity.
   */
  async invite(by: string, body: Body): Promise<{ node: string; code: string; expires: number }> {
    const { node } = body;
    const ttl = "ttl" in body ? body.ttl : INVITE_TTL;
    const replace = "replace" in body ? body.replace : false;
    if (typeof node !== "string" || !NODE.test(node)) throw new HttpError(400, `node names are lowercase letters, digits and dashes: ${pyRepr(node ?? null)}`);
    if (typeof ttl !== "number" || !Number.isInteger(ttl) || ttl < INVITE_TTL_MIN || ttl > INVITE_TTL_MAX) {
      throw new HttpError(400, `ttl is a whole number of seconds from ${INVITE_TTL_MIN} to ${INVITE_TTL_MAX}`);
    }
    if (typeof replace !== "boolean") throw new HttpError(400, "replace is true or false");
    const code = tokenUrlsafe();
    const digest = await sha256(code);
    const at = now();
    this.ctx.storage.transactionSync(() => {
      if (!replace && this.sql.exec("SELECT 1 FROM tokens WHERE node = ?", node).toArray().length) {
        throw new HttpError(409, `node ${node} already has a token; invite with replace to take it over`);
      }
      this.sql.exec("DELETE FROM invites WHERE expires < ?", at - INVITE_KEEP);
      this.sql.exec("INSERT INTO invites VALUES (?, ?, ?, ?, ?, ?, NULL)", digest, node, by, replace ? 1 : 0, at, at + ttl);
    });
    return { node, code, expires: at + ttl };
  }

  /** `invite` for the admin API, whose errors cross the RPC boundary as values. */
  async adminInvite(body: Body): Promise<{ invite?: { node: string; code: string; expires: number }; status?: number; error?: string }> {
    try {
      return { invite: await this.invite("", body) };
    } catch (error) {
      if (error instanceof HttpError) return { status: error.status, error: error.message };
      throw error;
    }
  }

  /** NODE's new token for an unused, unexpired invite code; the code is spent in the same transaction. */
  private async join(request: Request): Promise<Response> {
    try {
      // No token: the invite code in the body is the credential.
      let parsed: unknown;
      try {
        parsed = JSON.parse(utf8(await boundedBody(request, JOIN_BODY, new HttpError(413, "request too large"))) || "{}");
      } catch (error) {
        if (error instanceof HttpError) throw error;
        throw new HttpError(400, "invalid JSON");
      }
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new HttpError(400, "the request body must be a JSON object");
      const { code } = parsed as Body;
      if (typeof code !== "string" || !code || code.length > MAX_CODE) throw new HttpError(400, "code is the invite code `one-conv live invite` printed");
      const digest = await sha256(code);
      const token = tokenUrlsafe();
      const tokenHash = await sha256(token);
      const node = this.ctx.storage.transactionSync(() => {
        const row = this.sql.exec("SELECT node, takes_over, expires, used FROM invites WHERE hash = ?", digest).toArray()[0];
        if (!row) throw new HttpError(404, "unknown invite code");
        if (row.used !== null) throw new HttpError(410, "this invite was already used; ask for a new one with `one-conv live invite`");
        if ((row.expires as number) < now()) throw new HttpError(410, "this invite expired; ask for a new one with `one-conv live invite`");
        const invited = row.node as string;
        if (!row.takes_over && this.sql.exec("SELECT 1 FROM tokens WHERE node = ?", invited).toArray().length) {
          throw new HttpError(409, `node ${invited} got a token after this invite was made; ask for one with --replace`);
        }
        this.sql.exec("UPDATE invites SET used = ? WHERE hash = ?", now(), digest);
        this.sql.exec("INSERT OR REPLACE INTO tokens VALUES (?, ?, ?)", invited, tokenHash, now());
        return invited;
      });
      // A socket opened with a replaced token must not outlive it.
      this.closeStreams(node, "this node's token was replaced");
      return json({ node, token });
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }

  revoke(node: string): boolean {
    const removed = this.ctx.storage.transactionSync(() => {
      const removed = this.sql.exec("DELETE FROM tokens WHERE node = ?", node).rowsWritten;
      this.sql.exec("DELETE FROM sessions WHERE node = ?", node);
      this.sql.exec("DELETE FROM nodes WHERE node = ?", node);
      return removed > 0;
    });
    this.closeStreams(node, "unknown or revoked token");
    return removed;
  }

  private closeStreams(node: string, reason: string): void {
    for (const socket of this.ctx.getWebSockets(node)) {
      try {
        socket.close(REVOKED, reason);
      } catch {
        // Already closing: nothing more reaches it.
      }
    }
  }

  tokens(): { node: string; created: number }[] {
    return this.sql.exec("SELECT node, created FROM tokens ORDER BY node").toArray() as { node: string; created: number }[];
  }

  /** The node a bearer token belongs to; the request's identity comes only from here. */
  async nodeFor(token: string | null): Promise<string | null> {
    if (!token) return null;
    const row = this.sql.exec("SELECT node FROM tokens WHERE hash = ?", await sha256(token)).toArray()[0];
    return row ? (row.node as string) : null;
  }

  /** Empties every table and attachment; only reachable when the deployment enables resets (tests). */
  async reset(): Promise<void> {
    for (const table of TABLES) this.sql.exec(`DELETE FROM ${table}`);
    this.sql.exec("DELETE FROM sqlite_sequence WHERE name IN ('posts', 'notes')");
    for (;;) {
      const listed = await this.env.ARCHIVE.list({ prefix: "attachments/" });
      if (!listed.objects.length) break;
      await this.env.ARCHIVE.delete(listed.objects.map((object) => object.key));
    }
  }

  // --- HTTP ---------------------------------------------------------------------------------------

  private async authenticated(request: Request): Promise<string> {
    const token = bearer(request);
    if (token === null) {
      throw new HttpError(401, request.headers.has("authorization") ? "a bearer authorization header is required" : "exactly one bearer authorization header is required");
    }
    const node = await this.nodeFor(token);
    if (node === null) throw new HttpError(401, "unknown or revoked token");
    return node;
  }

  private async handle(request: Request, params: Record<string, string>, work: Work): Promise<Response> {
    try {
      const node = await this.authenticated(request);
      let body: Body = {};
      if (request.method === "POST" || request.method === "PUT") {
        const raw = await boundedBody(request, MAX_BODY, new HttpError(413, "request too large"));
        let parsed: unknown;
        try {
          parsed = raw.length ? JSON.parse(utf8(raw)) : {};
        } catch {
          return errorResponse(400, "invalid JSON");
        }
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new HttpError(400, "the request body must be a JSON object");
        body = parsed as Body;
      }
      return json(await work(node, body, request, params));
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }

  private async upload(request: Request): Promise<Response> {
    try {
      const node = await this.authenticated(request);
      // An image is sent as itself, not inside JSON, so its limit is the image limit.
      const body = await boundedBody(request, ATTACHMENT_LIMIT, new HttpError(413, "request too large"));
      const mediaType = (request.headers.get("content-type") ?? "").split(";")[0].trim();
      return json(await this.storeAttachment(node, mediaType, body));
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }

  private async download(request: Request, ident: string): Promise<Response> {
    try {
      await this.authenticated(request);
      if (!ATTACHMENT_ID.test(ident)) throw new HttpError(400, "an attachment id is 32 lowercase hex characters");
      const row = this.sql.exec("SELECT media_type FROM attachments WHERE id = ?", ident).toArray()[0];
      const object = row ? await this.env.ARCHIVE.get(`attachments/${ident}`) : null;
      if (!row || !object) throw new HttpError(404, "no such attachment");
      return new Response(object.body, { headers: { "content-type": row.media_type as string } });
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }

  // --- stream: hibernatable WebSockets tagged with their node --------------------------------------

  private async stream(request: Request): Promise<Response> {
    try {
      const node = await this.authenticated(request);
      if ((request.headers.get("upgrade") ?? "").toLowerCase() !== "websocket") throw new HttpError(426, "GET /v1/stream is a WebSocket upgrade");
      const [client, server] = Object.values(new WebSocketPair());
      // The tag is how a send finds this socket again after the object hibernated.
      this.ctx.acceptWebSocket(server, [node]);
      server.serializeAttachment({ node });
      this.seen(node);
      // Every delivery still without an outcome, so nothing sent while the bridge was away is lost.
      const rows = this.sql
        .exec(`${PENDING} WHERE d.to_node = ? AND d.outcome IS NULL ORDER BY d.post, d.rowid`, node)
        .toArray();
      this.push([server], rows);
      return new Response(null, { status: 101, webSocket: client });
    } catch (error) {
      if (error instanceof HttpError) return errorResponse(error.status, error.message);
      throw error;
    }
  }

  /** Hand deliveries to open sockets, one frame each: a frame stays far below the message size limit. */
  private push(sockets: WebSocket[], rows: Row[]): void {
    if (!sockets.length || !rows.length) return;
    const taken = now();
    for (const row of rows) {
      this.sql.exec("UPDATE deliveries SET taken = ? WHERE delivery = ?", taken, row.delivery);
      const frame = JSON.stringify({ messages: [handed(row)] });
      for (const socket of sockets) {
        try {
          socket.send(frame);
        } catch {
          // A socket closing under us: its bridge gets the delivery again when it reconnects.
        }
      }
    }
  }

  webSocketMessage(socket: WebSocket, message: string | ArrayBuffer): void {
    // The runtime answers PING itself; this covers a ping that reaches the object anyway.
    if (message === PING) socket.send(PONG);
  }

  webSocketClose(socket: WebSocket, code: number, reason: string): void {
    try {
      socket.close(code === 1005 || code === 1006 ? 1000 : code, reason);
    } catch {
      // The runtime already completed the closing handshake.
    }
  }

  // --- relay operations ---------------------------------------------------------------------------

  private seen(node: string): void {
    this.sql.exec("INSERT OR REPLACE INTO nodes VALUES (?, ?)", node, now());
  }

  /** Replace this node's published sessions and renew its lease; a name already taken is refused. */
  publish(node: string, sessions: unknown): number {
    if (!Array.isArray(sessions)) throw new HttpError(400, "sessions must be a list");
    const rows: SqlStorageValue[][] = [];
    const names = new Set<string>();
    const addresses = new Set<string>();
    for (const item of sessions as unknown[]) {
      const entry = item as Body;
      if (typeof item !== "object" || item === null || Array.isArray(item) || typeof entry.address !== "string" || entry.address.includes("/")) {
        throw new HttpError(400, "each session needs a local address like claude:<id>");
      }
      if (addresses.has(entry.address)) throw new HttpError(400, `${entry.address} is listed twice`);
      addresses.add(entry.address);
      const name = entry.name ?? null;
      if (name !== null && (typeof name !== "string" || !NODE.test(name))) throw new HttpError(400, `a name is lowercase letters, digits and dashes: ${pyRepr(name)}`);
      if (name !== null) {
        if (names.has(name as string)) throw new HttpError(409, `@${name} is given to two sessions`);
        names.add(name as string);
      }
      const product = truthy(entry.product) ? entry.product : "";
      if (product && !PRODUCTS.includes(product as string)) throw new HttpError(400, `product is one of ${PRODUCTS.join(", ")}, or absent`);
      const text = (value: unknown) => (truthy(value) ? pyStr(value) : "");
      rows.push([node, entry.address, name as string | null, text(entry.title), text(entry.cwd), text(entry.status), now(), pyStr(product)]);
    }
    return this.ctx.storage.transactionSync(() => {
      for (const name of names) {
        if (this.sql.exec("SELECT 1 FROM sessions WHERE name = ? AND node != ?", name, node).toArray().length) {
          throw new HttpError(409, `@${name} is already used on another node`);
        }
      }
      this.sql.exec("DELETE FROM sessions WHERE node = ?", node);
      for (const row of rows) {
        this.sql.exec("INSERT INTO sessions (node, address, name, title, cwd, status, updated, product) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ...row);
      }
      this.seen(node);
      return rows.length;
    });
  }

  /** Every published session, with independent bridge and publication timestamps. */
  directory(): Body[] {
    const at = now();
    const seen = new Map(this.sql.exec("SELECT node, seen FROM nodes").toArray().map((row) => [row.node as string, row.seen as number]));
    return this.sql
      .exec("SELECT node, address, name, title, cwd, status, updated, product FROM sessions ORDER BY node, address")
      .toArray()
      .map((row) => ({
        address: `${row.node}/${row.address}`,
        node: row.node,
        local: row.address,
        name: row.name,
        title: row.title,
        cwd: row.cwd,
        status: row.status,
        product: row.product,
        seen: seen.get(row.node as string) ?? null,
        published_at: row.updated,
        online: online(seen.get(row.node as string) ?? 0, row.updated as number, at),
      }));
  }

  private published(node: string, address: string): boolean {
    return this.sql.exec("SELECT 1 FROM sessions WHERE node = ? AND address = ?", node, address).toArray().length > 0;
  }

  /** Where a message goes: one published session, by its route or by the @name it chose. */
  private recipient(target: string): [string, string] {
    if (target.startsWith("@")) {
      const row = this.sql.exec("SELECT node, address FROM sessions WHERE name = ?", target.slice(1)).toArray()[0];
      if (!row) throw new HttpError(404, `no published session is named ${target}`);
      return [row.node as string, row.address as string];
    }
    const [node, address] = splitAddress(target);
    if (!this.published(node, address)) throw new HttpError(404, `${target} is not a published session`);
    return [node, address];
  }

  private receipt(seq: number, id: string, duplicate: boolean): Body {
    const rows = this.sql.exec("SELECT delivery, to_node, to_address, outcome FROM deliveries WHERE post = ? ORDER BY rowid", seq).toArray();
    const timing = this.sql.exec("SELECT timing FROM posts WHERE seq = ?", seq).one().timing;
    // Echoing `when` tells the sender this relay carries it.
    return { id, duplicate, ...(timing === "idle" ? { when: timing } : {}), recipients: rows.map((row) => ({ to: `${row.to_node}/${row.to_address}`, delivery: row.delivery, status: row.outcome ?? "stored" })) };
  }

  /** Store a post and its delivery; a retry of the same post returns its first receipt. */
  async send(node: string, message: Body): Promise<Body> {
    const { id, from: sender, text } = message;
    if (typeof id !== "string" || !MESSAGE_ID.test(id)) throw new HttpError(400, "id must be 32 lowercase hex characters, chosen by the sender");
    const attached = truthy(message.attachments) ? message.attachments : [];
    if (!Array.isArray(attached) || attached.length > ATTACHMENTS_PER_MESSAGE || !attached.every((a) => typeof a === "string" && ATTACHMENT_ID.test(a))) {
      throw new HttpError(400, `attachments is a list of at most ${ATTACHMENTS_PER_MESSAGE} attachment ids`);
    }
    if (typeof text !== "string" || (!text && !attached.length) || (text.length > MAX_TEXT && pyLen(text) > MAX_TEXT)) {
      throw new HttpError(400, `text must be a string of at most ${MAX_TEXT} characters, non-empty unless an image is attached`);
    }
    const reply = "reply" in message ? message.reply : "expected";
    if (!REPLIES.includes(reply as string)) throw new HttpError(400, "reply must be expected, none or inline");
    // push wakes the receiver; fyi only files a notice for it to read when it chooses.
    const mode = "mode" in message ? message.mode : "push";
    if (!MODES.includes(mode as string)) throw new HttpError(400, `mode must be one of ${MODES.join(", ")}`);
    const timing = "when" in message ? message.when : "now";
    if (!WHEN.includes(timing as string)) throw new HttpError(400, `when must be one of ${WHEN.join(", ")}`);
    const target = message.to;
    if (typeof target !== "string" || !target) throw new HttpError(400, "to must be NODE/harness:id, @name or user");
    if (target === USER) return this.note(node, id, sender, text, message, attached.length > 0);
    // `when` joins the digest only when idle, so a `now` post hashes as it always has.
    const digest = await sha256(JSON.stringify([target, sender ?? null, reply, mode, text, ...attached, ...(timing === "idle" ? ["when=idle"] : [])]));
    const result = this.ctx.storage.transactionSync(() => {
      const existing = this.sql.exec("SELECT seq, digest FROM posts WHERE from_node = ? AND id = ?", node, id).toArray()[0];
      if (existing) {
        if (existing.digest !== digest) throw new HttpError(409, "this id was already used for a different message; send it under a new id");
        return { receipt: this.receipt(existing.seq as number, id, true), wake: null };
      }
      const attachments = this.attachments(attached as string[]);
      // A sender is this node's own session or the person at its terminal, never another node's agent.
      if (typeof sender !== "string" || (!this.published(node, sender) && sender !== "user" && sender !== "unknown")) {
        throw new HttpError(403, "from must be a session this node published, or user/unknown");
      }
      const [toNode, toAddress] = this.recipient(target);
      const seq = this.sql
        .exec(
          "INSERT INTO posts (from_node, id, digest, target, sender, reply, mode, text, created, attachments, timing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING seq",
          node,
          id,
          digest,
          target,
          sender,
          reply as string,
          mode as string,
          text,
          now(),
          attachments ? JSON.stringify(attachments) : null,
          timing as string,
        )
        .one().seq as number;
      const delivery = randomHex(16);
      this.sql.exec("INSERT INTO deliveries (delivery, post, to_node, to_address) VALUES (?, ?, ?, ?)", delivery, seq, toNode, toAddress);
      return { receipt: this.receipt(seq, id, false), wake: { node: toNode, delivery } };
    });
    if (result.wake) this.wake(result.wake.node, result.wake.delivery);
    return result.receipt;
  }

  /** File a message to the person; a retry of the same note returns its first receipt. */
  private async note(node: string, id: string, sender: unknown, text: string, message: Body, attached: boolean): Promise<Body> {
    if (attached) throw new HttpError(400, "a message to user carries text only");
    const tags = truthy(message.tags) ? message.tags : [];
    if (!Array.isArray(tags) || tags.length > TAGS_PER_NOTE || !tags.every((tag) => typeof tag === "string" && TAG.test(tag))) {
      throw new HttpError(400, `tags is a list of at most ${TAGS_PER_NOTE} lowercase words (letters, digits, dashes)`);
    }
    const name = truthy(message.name) ? message.name : "";
    if (typeof name !== "string" || name.length > NAME_LIMIT) throw new HttpError(400, `name is a string of at most ${NAME_LIMIT} characters`);
    const unique = [...new Set(tags as string[])];
    const digest = await sha256(JSON.stringify([USER, sender ?? null, name, text, ...unique]));
    return this.ctx.storage.transactionSync(() => {
      const existing = this.sql.exec("SELECT seq, digest FROM notes WHERE from_node = ? AND id = ?", node, id).toArray()[0];
      if (existing && existing.digest !== digest) throw new HttpError(409, "this id was already used for a different message; send it under a new id");
      if (!existing && (typeof sender !== "string" || (!this.published(node, sender) && sender !== "user" && sender !== "unknown"))) {
        throw new HttpError(403, "from must be a session this node published, or user/unknown");
      }
      if (!existing) {
        this.sql.exec(
          "INSERT INTO notes (from_node, id, digest, sender, name, text, tags, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          node, id, digest, sender as string, name, text, JSON.stringify(unique), now(),
        );
      }
      return { id, duplicate: Boolean(existing), recipients: [{ to: USER, status: "stored" }] };
    });
  }

  /**
   * Notes after `after`, oldest first, at most `limit`; `tag` (repeatable) keeps notes carrying any of them.
   * `last` is the newest note's seq overall, so a reader's cursor can move past notes its filter skipped.
   * A caller over RPC passes the URL as text, since a URL object does not cross that boundary.
   */
  notes(target: URL | string): Body {
    const url = typeof target === "string" ? new URL(target) : target;
    const after = Math.max(0, Math.trunc(pyFloat(url.searchParams.get("after") ?? "0") ?? 0));
    const asked = Math.trunc(pyFloat(url.searchParams.get("limit") ?? String(NOTES_LIMIT)) ?? NOTES_LIMIT);
    const limit = Math.max(1, Math.min(asked, NOTES_LIMIT));
    const tags = url.searchParams.getAll("tag");
    if (!tags.every((tag) => TAG.test(tag))) throw new HttpError(400, "a tag is lowercase letters, digits and dashes");
    const filter = tags.length ? ` AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value IN (${tags.map(() => "?").join(", ")}))` : "";
    const rows = this.sql
      .exec(`SELECT seq, id, from_node, sender, name, text, tags, created FROM notes WHERE seq > ?${filter} ORDER BY seq LIMIT ?`, after, ...tags, limit)
      .toArray();
    const last = (this.sql.exec("SELECT MAX(seq) AS last FROM notes").one().last as number | null) ?? 0;
    return {
      messages: rows.map((row) => ({
        seq: row.seq,
        id: row.id,
        from: `${row.from_node}/${row.sender}`,
        name: row.name,
        text: row.text,
        tags: parsedList(row.tags),
        created: row.created,
      })),
      last,
    };
  }

  /** The metadata of the attached images, in the order given; an unknown id refuses the message. */
  private attachments(ids: string[]): Body[] | null {
    if (!ids.length) return null;
    return ids.map((ident) => {
      const row = this.sql.exec("SELECT media_type, size FROM attachments WHERE id = ?", ident).toArray()[0];
      if (!row) throw new HttpError(400, `attachment ${ident} was not uploaded`);
      return { id: ident, media_type: row.media_type, size: row.size };
    });
  }

  /** Keep one uploaded image; its id goes into the message that carries it. */
  async storeAttachment(node: string, mediaType: string, body: Uint8Array): Promise<Body> {
    if (!ATTACHMENT_TYPES.includes(mediaType)) throw new HttpError(415, "attach a PNG, JPEG, GIF or WebP image");
    if (!body.length || body.length > ATTACHMENT_LIMIT) throw new HttpError(413, `an image is at most ${ATTACHMENT_LIMIT / 1_000_000} MB`);
    const ident = randomHex(16);
    // The body is durable before its id exists, so a listed attachment always has bytes.
    await this.env.ARCHIVE.put(`attachments/${ident}`, body, { httpMetadata: { contentType: mediaType } });
    this.sql.exec("INSERT INTO attachments VALUES (?, ?, ?, ?, ?)", ident, node, mediaType, body.length, now());
    return { id: ident, media_type: mediaType, size: body.length };
  }

  /** Wake the node's long-polls and push the new delivery to every stream it holds open, as /v1/inbox hands it to every poller. */
  private wake(node: string, delivery: string): void {
    for (const resolve of this.waiting.get(node) ?? []) resolve();
    this.push(this.ctx.getWebSockets(node), this.sql.exec(`${PENDING} WHERE d.delivery = ?`, delivery).toArray());
  }

  private pending(node: string): Row[] {
    this.seen(node);
    const rows = this.sql.exec(`${PENDING} WHERE d.to_node = ? AND d.outcome IS NULL ORDER BY d.post LIMIT ?`, node, INBOX_LIMIT).toArray();
    const taken = now();
    for (const row of rows) this.sql.exec("UPDATE deliveries SET taken = ? WHERE delivery = ?", taken, row.delivery);
    return rows;
  }

  /**
   * Deliveries for this node without an outcome yet, waiting up to `wait` seconds for one.
   *
   * A delivery stays here until acknowledged, so one handed out before a lost response is handed
   * out again; the bridge recognises it by its delivery id.
   */
  async inbox(node: string, wait: number): Promise<Body[]> {
    const deadline = Date.now() + Math.max(0, Math.min(Number.isNaN(wait) ? 0 : wait, MAX_WAIT)) * 1000;
    for (;;) {
      const rows = this.pending(node);
      const left = deadline - Date.now();
      if (rows.length || left <= 0) return rows.map(handed);
      await this.waitFor(node, left);
    }
  }

  private waitFor(node: string, ms: number): Promise<void> {
    return new Promise((resolve) => {
      const waiters = this.waiting.get(node) ?? new Set();
      this.waiting.set(node, waiters);
      const done = () => {
        clearTimeout(timer);
        waiters.delete(done);
        if (!waiters.size) this.waiting.delete(node);
        resolve();
      };
      const timer = setTimeout(done, ms);
      waiters.add(done);
    });
  }

  /**
   * Record the outcome of one of this node's deliveries; the first final outcome reported stands.
   * `held` is recorded only on a delivery without an outcome, and a final outcome replaces it.
   */
  ack(node: string, delivery: string, outcome: string): Body {
    if (!OUTCOMES.includes(outcome)) throw new HttpError(400, `outcome must be one of ${OUTCOMES.join(", ")}`);
    const row = this.sql.exec("SELECT outcome FROM deliveries WHERE delivery = ? AND to_node = ?", delivery, node).toArray()[0];
    if (!row) throw new HttpError(404, "no such delivery for this node");
    this.sql.exec(
      "UPDATE deliveries SET outcome = ?, outcome_at = ? WHERE delivery = ? AND (outcome IS NULL OR (outcome = 'held' AND ? != 'held'))",
      outcome,
      now(),
      delivery,
      outcome,
    );
    return { delivery, outcome: this.sql.exec("SELECT outcome FROM deliveries WHERE delivery = ?", delivery).one().outcome };
  }

  /** The outcome per recipient of a message this node sent; other nodes cannot read it. */
  status(node: string, id: string): Body {
    const post = this.sql.exec("SELECT seq FROM posts WHERE from_node = ? AND id = ?", node, id).toArray()[0];
    if (!post) throw new HttpError(404, "no such message from this node");
    const rows = this.sql.exec("SELECT delivery, to_node, to_address, outcome, outcome_at FROM deliveries WHERE post = ? ORDER BY rowid", post.seq).toArray();
    return { id, recipients: rows.map((row) => ({ to: `${row.to_node}/${row.to_address}`, delivery: row.delivery, outcome: row.outcome ?? "stored", outcome_at: row.outcome_at })) };
  }
}

function waitParam(request: Request): number {
  return pyFloat(query(new URL(request.url), "wait") ?? "0") ?? 0;
}

function parsedList(text: SqlStorageValue): unknown[] {
  try {
    const rows = text ? JSON.parse(text as string) : [];
    return Array.isArray(rows) ? rows : [];
  } catch {
    return [];
  }
}

function handed(row: Row): Body {
  return {
    delivery: row.delivery,
    id: row.id,
    to: row.to_address,
    from: `${row.from_node}/${row.sender}`,
    reply: row.reply,
    mode: row.mode,
    text: row.text,
    created: row.created,
    ...(row.attachments ? { attachments: parsedList(row.attachments) } : {}),
    ...(row.timing === "idle" ? { when: row.timing } : {}),
  };
}
