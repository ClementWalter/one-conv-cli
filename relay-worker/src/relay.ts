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
 */

import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import { HttpError, type Route, bearer, boundedBody, decodedPath, dispatch, errorResponse, json, path, pyFloat, query, randomHex, sha256, tokenUrlsafe, utf8 } from "./http";
import { pyLen, pyRepr, pyStr, truthy } from "./pyrepr";

export const NODE = /^[a-z0-9][a-z0-9-]{0,31}$/;
const MESSAGE_ID = /^[0-9a-f]{32}$/;
const ATTACHMENT_ID = /^[0-9a-f]{32}$/;
const OUTCOMES = ["accepted", "refused", "unconfirmed"];
const MODES = ["push", "fyi"];
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

const SCHEMA = `
CREATE TABLE IF NOT EXISTS tokens (node TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (node TEXT NOT NULL, address TEXT NOT NULL, name TEXT, title TEXT, cwd TEXT,
    status TEXT, updated REAL NOT NULL, product TEXT NOT NULL DEFAULT '', PRIMARY KEY (node, address));
CREATE INDEX IF NOT EXISTS session_names ON sessions (name);
CREATE TABLE IF NOT EXISTS posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, reply TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'push', text TEXT NOT NULL, created REAL NOT NULL, attachments TEXT,
    UNIQUE (from_node, id));
CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, post INTEGER NOT NULL REFERENCES posts (seq),
    to_node TEXT NOT NULL, to_address TEXT NOT NULL, taken REAL, outcome TEXT, outcome_at REAL,
    UNIQUE (post, to_node, to_address));
CREATE INDEX IF NOT EXISTS pending ON deliveries (to_node, outcome, post);
CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, node TEXT NOT NULL, media_type TEXT NOT NULL,
    size INTEGER NOT NULL, created REAL NOT NULL);
`;
const TABLES = ["tokens", "nodes", "sessions", "deliveries", "posts", "attachments"];

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
      { pattern: path("/v1/acks"), methods: ["POST"], handle: work((node, body) => this.ack(node, pyStr(body.delivery ?? null), pyStr(body.outcome ?? null))) },
      { pattern: path("/v1/attachments"), methods: ["POST"], handle: (request) => this.upload(request) },
      { pattern: path("/v1/attachments/{ident}"), methods: ["GET"], handle: (request, params) => this.download(request, params.ident) },
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
    return { token };
  }

  revoke(node: string): boolean {
    return this.ctx.storage.transactionSync(() => {
      const removed = this.sql.exec("DELETE FROM tokens WHERE node = ?", node).rowsWritten;
      this.sql.exec("DELETE FROM sessions WHERE node = ?", node);
      this.sql.exec("DELETE FROM nodes WHERE node = ?", node);
      return removed > 0;
    });
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
    this.sql.exec("DELETE FROM sqlite_sequence WHERE name = 'posts'");
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
    return { id, duplicate, recipients: rows.map((row) => ({ to: `${row.to_node}/${row.to_address}`, delivery: row.delivery, status: row.outcome ?? "stored" })) };
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
    const target = message.to;
    if (typeof target !== "string" || !target) throw new HttpError(400, "to must be NODE/harness:id or @name");
    const digest = await sha256(JSON.stringify([target, sender ?? null, reply, mode, text, ...attached]));
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
          "INSERT INTO posts (from_node, id, digest, target, sender, reply, mode, text, created, attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING seq",
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
        )
        .one().seq as number;
      this.sql.exec("INSERT INTO deliveries (delivery, post, to_node, to_address) VALUES (?, ?, ?, ?)", randomHex(16), seq, toNode, toAddress);
      return { receipt: this.receipt(seq, id, false), wake: toNode };
    });
    if (result.wake) this.wake(result.wake);
    return result.receipt;
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

  private wake(node: string): void {
    for (const resolve of this.waiting.get(node) ?? []) resolve();
  }

  private pending(node: string): Row[] {
    this.seen(node);
    const rows = this.sql
      .exec(
        "SELECT d.delivery, p.id, d.to_address, p.from_node, p.sender, p.reply, p.mode, p.text, p.created, p.attachments" +
          " FROM deliveries d JOIN posts p ON p.seq = d.post WHERE d.to_node = ? AND d.outcome IS NULL ORDER BY d.post LIMIT ?",
        node,
        INBOX_LIMIT,
      )
      .toArray();
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

  /** Record the outcome of one of this node's deliveries; the first outcome reported stands. */
  ack(node: string, delivery: string, outcome: string): Body {
    if (!OUTCOMES.includes(outcome)) throw new HttpError(400, `outcome must be one of ${OUTCOMES.join(", ")}`);
    const row = this.sql.exec("SELECT outcome FROM deliveries WHERE delivery = ? AND to_node = ?", delivery, node).toArray()[0];
    if (!row) throw new HttpError(404, "no such delivery for this node");
    this.sql.exec("UPDATE deliveries SET outcome = ?, outcome_at = ? WHERE delivery = ? AND outcome IS NULL", outcome, now(), delivery);
    return { delivery, outcome: row.outcome ?? outcome };
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

function parsedAttachments(text: SqlStorageValue): unknown[] {
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
    ...(row.attachments ? { attachments: parsedAttachments(row.attachments) } : {}),
  };
}
