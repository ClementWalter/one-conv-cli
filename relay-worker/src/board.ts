/**
 * The board: notes on shared topics, the Worker's side of one_conv/live/hub_board.py, run inside
 * the Relay Durable Object over its SQLite storage.
 *
 * A topic is a slug someone chose; it exists from its first post. A post is kept once under its
 * client-chosen id with a digest of its payload, so a retry returns the first receipt and a
 * different payload under the same id is a conflict. Posting wakes nobody: readers pull, and
 * each reader keeps its own read position on its own machine.
 *
 * A file is uploaded on its own first (POST /v1/board/<topic>/files, the raw bytes), then named
 * by id in the post that carries it. Its bytes live in R2 under `board/<topic>/<file id>`; its
 * name, size, content type and sha256 in SQLite. A file is served only while a live post carries it.
 */

import type { Env } from "./env";
import { HttpError, boundedBody, pyFloat, randomHex, sha256 } from "./http";
// The tag rule of notes (relay.ts), restated so this module loads without the relay.
const TAG = /^[a-z0-9][a-z0-9-]{0,31}$/;

export const TOPIC = /^[a-z0-9][a-z0-9-]{0,47}$/;
// `search` is a route under /v1/board, so no topic can take its name.
const RESERVED = ["search"];
const MESSAGE_ID = /^[0-9a-f]{32}$/;
const FILE_ID = /^[0-9a-f]{32}$/;
export const TEXT_LIMIT = 32 * 1024;
export const FILE_LIMIT = 25_000_000;
export const FILES_PER_POST = 4;
const FILE_NAME_LIMIT = 255;
const CONTENT_TYPE = /^[\x21-\x7e]+\/[\x20-\x7e]+$/;
const CONTENT_TYPE_LIMIT = 128;
const DEFAULT_TYPE = "application/octet-stream";
const TAGS_PER_POST = 8;
const NAME_LIMIT = 200;
const TITLE_LIMIT = 200;
const READ_LIMIT = 200;
const SEARCH_LIMIT = 200;
const QUERY_LIMIT = 1000;
const SEARCH_WORDS = 8;
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

export const BOARD_SCHEMA = `
CREATE TABLE IF NOT EXISTS board_topics (slug TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL,
    last_post REAL NOT NULL, posts INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS board_posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, id TEXT NOT NULL UNIQUE,
    from_node TEXT NOT NULL, digest TEXT NOT NULL, author TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL,
    tags TEXT NOT NULL, created REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, attachments TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS board_topic_posts ON board_posts (topic, seq);
CREATE TABLE IF NOT EXISTS board_files (id TEXT PRIMARY KEY, topic TEXT NOT NULL, node TEXT NOT NULL, name TEXT NOT NULL,
    size INTEGER NOT NULL, content_type TEXT NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL, post TEXT);
`;
export const BOARD_TABLES = ["board_files", "board_posts", "board_topics"];
export const BOARD_PREFIX = "board/";
const POST_COLUMNS = "seq, topic, id, from_node, author, name, text, tags, created, attachments";

type Body = Record<string, unknown>;
export interface BoardFile {
  id: string;
  name: string;
  size: number;
  content_type: string;
  sha256: string;
}

const now = () => Date.now() / 1000;

export function checkTopic(topic: unknown): string {
  if (typeof topic !== "string" || !TOPIC.test(topic) || RESERVED.includes(topic)) {
    throw new HttpError(400, `a topic is 1 to 48 lowercase letters, digits and dashes, starting with a letter or digit (not ${RESERVED.join(", ")})`);
  }
  return topic;
}

function checkTags(tags: unknown): string[] {
  if (!Array.isArray(tags) || tags.length > TAGS_PER_POST || !tags.every((tag) => typeof tag === "string" && TAG.test(tag))) {
    throw new HttpError(400, `tags is a list of at most ${TAGS_PER_POST} lowercase words (letters, digits, dashes)`);
  }
  return [...new Set(tags as string[])];
}

/** The uploaded file's name from its percent-encoded header: one path segment, printable. */
export function fileName(raw: string): string {
  let name: string;
  try {
    name = decodeURIComponent(raw);
  } catch {
    throw new HttpError(400, "x-file-name is the percent-encoded UTF-8 file name");
  }
  const chars = [...name];
  if (
    !name ||
    chars.length > FILE_NAME_LIMIT ||
    name === "." ||
    name === ".." ||
    name.includes("/") ||
    name.includes("\\") ||
    chars.some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
  ) {
    throw new HttpError(400, `a file name is 1 to ${FILE_NAME_LIMIT} printable characters without slashes`);
  }
  return name;
}

export function contentType(raw: string): string {
  const value = raw.trim();
  return value && value.length <= CONTENT_TYPE_LIMIT && CONTENT_TYPE.test(value) ? value : DEFAULT_TYPE;
}

/** A LIKE pattern matching `word` anywhere, its wildcards taken literally. */
function like(word: string): string {
  return `%${word.replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_")}%`;
}

function parsed(text: unknown): unknown[] {
  try {
    const value = JSON.parse(text as string);
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

function post(row: Record<string, SqlStorageValue>): Body {
  return {
    seq: row.seq,
    topic: row.topic,
    id: row.id,
    from: `${row.from_node}/${row.author}`,
    name: row.name,
    text: row.text,
    tags: parsed(row.tags),
    created: row.created,
    files: parsed(row.attachments),
  };
}

/** `read=<topic>:<seq>` query values: a reader's positions, for unread counts. */
export function parseReads(values: string[]): Map<string, number> {
  const reads = new Map<string, number>();
  for (const value of values) {
    const colon = value.lastIndexOf(":");
    const topic = value.slice(0, Math.max(colon, 0));
    const seq = value.slice(colon + 1);
    if (colon < 0 || !TOPIC.test(topic) || !/^[0-9]{1,15}$/.test(seq)) throw new HttpError(400, "read is <topic>:<seq>");
    reads.set(topic, Number(seq));
  }
  return reads;
}

export class Board {
  constructor(
    private readonly storage: DurableObjectStorage,
    private readonly env: Env,
    // Whether `sender` is a session `node` published: the relay vouches for nothing else.
    private readonly published: (node: string, sender: string) => boolean,
  ) {}

  private get sql(): SqlStorage {
    return this.storage.sql;
  }

  /** Every topic, most recently posted first; `unread` counts live posts after the reader's position. */
  topics(reads: Map<string, number>): Body {
    const rows = this.sql
      .exec("SELECT slug, title, created, last_post, posts, archived, (SELECT COALESCE(MAX(seq), 0) FROM board_posts WHERE topic = slug) AS last FROM board_topics ORDER BY last_post DESC, slug")
      .toArray();
    return {
      topics: rows.map((row) => ({
        topic: row.slug,
        title: row.title,
        created: row.created,
        last_post: row.last_post,
        posts: row.posts,
        last: row.last,
        archived: Boolean(row.archived),
        unread: this.sql.exec("SELECT COUNT(*) AS n FROM board_posts WHERE topic = ? AND seq > ? AND deleted = 0", row.slug, reads.get(row.slug as string) ?? 0).one().n,
      })),
    };
  }

  /** Live posts after `after`, oldest first; `last` is the topic's newest seq, past anything the filter skipped. */
  read(topic: string, url: URL): Body {
    checkTopic(topic);
    const after = Math.max(0, Math.trunc(pyFloat(url.searchParams.get("after") ?? "0") ?? 0));
    const asked = Math.trunc(pyFloat(url.searchParams.get("limit") ?? "50") ?? 50);
    const limit = Math.max(1, Math.min(asked, READ_LIMIT));
    const tags = url.searchParams.getAll("tag");
    // A post carries at most TAGS_PER_POST tags, which also keeps the query under SQLite's parameter cap.
    if (tags.length > TAGS_PER_POST || !tags.every((tag) => TAG.test(tag))) throw new HttpError(400, `at most ${TAGS_PER_POST} tags, each lowercase letters, digits and dashes`);
    const found = this.sql.exec("SELECT title FROM board_topics WHERE slug = ?", topic).toArray()[0];
    if (!found) throw new HttpError(404, `no topic named ${topic}`);
    const filter = tags.length ? ` AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value IN (${tags.map(() => "?").join(", ")}))` : "";
    const rows = this.sql
      .exec(`SELECT ${POST_COLUMNS} FROM board_posts WHERE topic = ? AND seq > ? AND deleted = 0${filter} ORDER BY seq LIMIT ?`, topic, after, ...tags, limit)
      .toArray();
    const last = this.sql.exec("SELECT COALESCE(MAX(seq), 0) AS last FROM board_posts WHERE topic = ?", topic).one().last;
    return { topic, title: found.title, posts: rows.map(post), last };
  }

  /** The upload route: the raw bytes, named by the percent-encoded X-File-Name header. */
  async uploadRequest(node: string, topic: string, request: Request): Promise<BoardFile> {
    checkTopic(topic);
    // A file is sent as itself, not inside JSON, so its limit is the file limit.
    const body = await boundedBody(request, FILE_LIMIT, new HttpError(413, "request too large"));
    return this.upload(node, topic, fileName(request.headers.get("x-file-name") ?? ""), request.headers.get("content-type") ?? "", body);
  }

  /** Keep one file for a post to come; its id goes into that post's `files`. */
  async upload(node: string, topic: string, name: string, mediaType: string, body: Uint8Array): Promise<BoardFile> {
    checkTopic(topic);
    if (!body.length || body.length > FILE_LIMIT) throw new HttpError(413, `a file is 1 byte to ${FILE_LIMIT / 1_000_000} MB`);
    const ident = randomHex(16);
    const digest = await sha256(body);
    const kind = contentType(mediaType);
    // The body is durable before its id exists, so a listed file always has bytes.
    await this.env.ARCHIVE.put(`${BOARD_PREFIX}${topic}/${ident}`, body, { httpMetadata: { contentType: kind } });
    this.sql.exec("INSERT INTO board_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)", ident, topic, node, name, body.length, kind, digest, now());
    return { id: ident, name, size: body.length, content_type: kind, sha256: digest };
  }

  /** Store a post, creating its topic; a retry under the same id returns the first receipt. */
  async post(node: string, topic: string, body: Body): Promise<Body> {
    checkTopic(topic);
    const { id: ident, from: sender } = body;
    const text = "text" in body ? body.text : "";
    if (typeof ident !== "string" || !MESSAGE_ID.test(ident)) throw new HttpError(400, "id must be 32 lowercase hex characters, chosen by the poster");
    const files = body.files ? body.files : [];
    if (!Array.isArray(files) || files.length > FILES_PER_POST || !files.every((f) => typeof f === "string" && FILE_ID.test(f)) || new Set(files).size !== files.length) {
      throw new HttpError(400, `files is a list of at most ${FILES_PER_POST} distinct uploaded file ids`);
    }
    if (typeof text !== "string" || (!text && !files.length)) throw new HttpError(400, "text must be a string, non-empty unless a file is attached");
    if (LONE_SURROGATE.test(text)) throw new HttpError(400, "text must be valid Unicode");
    if (new TextEncoder().encode(text).length > TEXT_LIMIT) throw new HttpError(413, `a post's text is at most ${TEXT_LIMIT} bytes`);
    const tags = checkTags(body.tags ? body.tags : []);
    const name = body.name ? body.name : "";
    const title = body.title ? body.title : "";
    if (typeof name !== "string" || name.length > NAME_LIMIT) throw new HttpError(400, `name is a string of at most ${NAME_LIMIT} characters`);
    if (typeof title !== "string" || title.length > TITLE_LIMIT) throw new HttpError(400, `title is a string of at most ${TITLE_LIMIT} characters`);
    const attached: BoardFile[] = (files as string[]).map((fileId) => {
      const row = this.sql.exec("SELECT name, size, content_type, sha256, node, topic, post FROM board_files WHERE id = ?", fileId).toArray()[0];
      if (!row || row.node !== node || row.topic !== topic || (row.post !== null && row.post !== ident)) {
        throw new HttpError(400, `file ${fileId} was not uploaded by this node to ${topic}, or is attached elsewhere`);
      }
      return { id: fileId, name: row.name as string, size: row.size as number, content_type: row.content_type as string, sha256: row.sha256 as string };
    });
    // File content, not file ids, joins the digest: a retry that uploads the same files again is the same post.
    const digest = await sha256(JSON.stringify([topic, sender ?? null, name, title, text, tags, attached.map((f) => [f.name, f.sha256])]));
    return this.storage.transactionSync(() => {
      const existing = this.sql.exec("SELECT seq, from_node, digest, created FROM board_posts WHERE id = ?", ident).toArray()[0];
      if (existing && (existing.from_node !== node || existing.digest !== digest)) throw new HttpError(409, "this id was already used for a different post; post it under a new id");
      let seq: number;
      let created: number;
      if (existing) {
        seq = existing.seq as number;
        created = existing.created as number;
      } else {
        if (typeof sender !== "string" || (!this.published(node, sender) && sender !== "user" && sender !== "unknown")) {
          throw new HttpError(403, "from must be a session this node published, or user/unknown");
        }
        created = now();
        seq = this.sql
          .exec(
            "INSERT INTO board_posts (topic, id, from_node, digest, author, name, text, tags, created, attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING seq",
            topic, ident, node, digest, sender, name, text, JSON.stringify(tags), created, JSON.stringify(attached),
          )
          .one().seq as number;
        for (const file of attached) this.sql.exec("UPDATE board_files SET post = ? WHERE id = ?", ident, file.id);
        this.sql.exec(
          "INSERT INTO board_topics VALUES (?, ?, ?, ?, 1, 0) ON CONFLICT (slug) DO UPDATE SET last_post = excluded.last_post, posts = posts + 1, title = CASE WHEN excluded.title != '' THEN excluded.title ELSE title END",
          topic, title, created, created,
        );
      }
      const previous = this.sql.exec("SELECT COALESCE(MAX(seq), 0) AS previous FROM board_posts WHERE topic = ? AND seq < ?", topic, seq).one().previous;
      return { id: ident, topic, seq, previous, created, duplicate: Boolean(existing) };
    });
  }

  /** A file a live post carries, with its metadata. */
  async file(topic: string, ident: string): Promise<{ meta: BoardFile; body: ReadableStream }> {
    checkTopic(topic);
    if (!FILE_ID.test(ident)) throw new HttpError(400, "a file id is 32 lowercase hex characters");
    const row = this.sql
      .exec("SELECT f.name, f.size, f.content_type, f.sha256 FROM board_files f JOIN board_posts p ON p.id = f.post WHERE f.id = ? AND f.topic = ? AND p.deleted = 0", ident, topic)
      .toArray()[0];
    const object = row ? await this.env.ARCHIVE.get(`${BOARD_PREFIX}${topic}/${ident}`) : null;
    if (!row || !object) throw new HttpError(404, "no such file on this topic");
    return { meta: { id: ident, name: row.name as string, size: row.size as number, content_type: row.content_type as string, sha256: row.sha256 as string }, body: object.body };
  }

  /** The download route: served as an attachment, never rendered, since a file is whatever a node uploaded. */
  async fileResponse(topic: string, ident: string): Promise<Response> {
    const { meta, body } = await this.file(topic, ident);
    return new Response(body, {
      headers: {
        "content-type": meta.content_type,
        "content-disposition": `attachment; filename*=UTF-8''${encodeURIComponent(meta.name)}`,
        "x-content-type-options": "nosniff",
        "x-content-sha256": meta.sha256,
      },
    });
  }

  /** Hide a post from every read; only the node that posted it, or the relay's admin, may. */
  delete(node: string, topic: string, ident: string, admin = false): { id: string; topic: string; deleted: boolean } {
    checkTopic(topic);
    return this.storage.transactionSync(() => {
      const row = this.sql.exec("SELECT from_node, deleted FROM board_posts WHERE id = ? AND topic = ?", ident, topic).toArray()[0];
      if (!row) throw new HttpError(404, "no such post on this topic");
      if (row.from_node !== node && !admin) throw new HttpError(403, "only the node that posted it, or the relay's admin, deletes a post");
      if (!row.deleted) {
        this.sql.exec("UPDATE board_posts SET deleted = 1 WHERE id = ?", ident);
        this.sql.exec("UPDATE board_topics SET posts = posts - 1 WHERE slug = ?", topic);
      }
      return { id: ident, topic, deleted: true };
    });
  }

  /** Live posts whose text holds `q` (or, with words=1, each of its words), newest first, ignoring ASCII case. */
  search(url: URL): Body {
    const query = url.searchParams.get("q") ?? "";
    if (!query.trim() || query.length > QUERY_LIMIT) throw new HttpError(400, `q is 1 to ${QUERY_LIMIT} characters`);
    const topic = url.searchParams.get("topic") || null;
    if (topic !== null) checkTopic(topic);
    const words = ["1", "true"].includes(url.searchParams.get("words") ?? "");
    const terms = words ? [...new Set(query.trim().split(/\s+/))] : [query];
    if (terms.length > SEARCH_WORDS) throw new HttpError(400, `an all-words search takes at most ${SEARCH_WORDS} words`);
    const asked = Math.trunc(pyFloat(url.searchParams.get("limit") ?? "20") ?? 20);
    const limit = Math.max(1, Math.min(asked, SEARCH_LIMIT));
    const where = terms.map(() => "text LIKE ? ESCAPE '\\'").join(" AND ") + (topic ? " AND topic = ?" : "");
    const rows = this.sql
      .exec(`SELECT ${POST_COLUMNS} FROM board_posts WHERE deleted = 0 AND ${where} ORDER BY seq DESC LIMIT ?`, ...terms.map(like), ...(topic ? [topic] : []), limit)
      .toArray();
    return { posts: rows.map(post) };
  }
}
