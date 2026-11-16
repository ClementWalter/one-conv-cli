/**
 * The archive's index as one SQLite-backed Durable Object, mirroring `archive/store.py`:
 * immutable manifests per (key, revision), one visible head per conversation advanced by
 * compare-and-swap, a change sequence with its generation, and the set of stored chunks.
 *
 * Chunk bytes live in R2; this object only records which digests are stored, so the "missing
 * chunks" check is one query instead of an R2 lookup per digest. A head's search text can exceed
 * the 2 MB SQLite value limit of a Durable Object, so it is kept in parts, each searched together
 * with the OVERLAP characters after it: any query that long or shorter lies inside one of them.
 */

import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";

const PART = 500_000;
export const OVERLAP = 4096;
const IN_BATCH = 500;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS heads (key TEXT PRIMARY KEY, revision TEXT NOT NULL, mtime REAL NOT NULL,
    metadata TEXT NOT NULL, change_sequence INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS search_parts (key TEXT NOT NULL, part INTEGER NOT NULL, text TEXT NOT NULL,
    tail TEXT NOT NULL, PRIMARY KEY (key, part));
CREATE TABLE IF NOT EXISTS revisions (key TEXT NOT NULL, revision TEXT NOT NULL, manifest TEXT NOT NULL,
    PRIMARY KEY (key, revision));
CREATE TABLE IF NOT EXISTS chunks (digest TEXT PRIMARY KEY, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), sequence INTEGER NOT NULL, generation TEXT NOT NULL);
`;

export interface Head {
  key: string;
  revision: string;
  mtime: number;
  metadata: string;
  search_text: string;
}

export interface ImportedHead extends Head {
  change_sequence: number;
}

export type PutResult = { key: string; revision: string } | { conflict: string };

/** `search_text` as (part, tail) pairs: consecutive parts join back to the text, a tail repeats what follows. */
export function searchParts(text: string): [string, string][] {
  const parts: [string, string][] = [];
  let start = 0;
  do {
    let end = Math.min(start + PART, text.length);
    // Never split a surrogate pair: a lone half is not valid text for SQLite.
    if (end < text.length && /[\ud800-\udbff]/.test(text[end - 1])) end++;
    let stop = Math.min(end + OVERLAP, text.length);
    if (stop < text.length && /[\ud800-\udbff]/.test(text[stop - 1])) stop++;
    parts.push([text.slice(start, end), text.slice(end, stop)]);
    start = end;
  } while (start < text.length);
  return parts;
}

/** The text of `[{**metadata, "key": ..., "revision": ...}]`, built from Python-encoded metadata. */
function item(row: Record<string, SqlStorageValue>): string {
  const metadata = row.metadata as string;
  const tail = `"key":${JSON.stringify(row.key)},"revision":${JSON.stringify(row.revision)}}`;
  return metadata === "{}" ? `{${tail}` : `${metadata.slice(0, -1)},${tail}`;
}

export class ArchiveIndex extends DurableObject<Env> {
  private sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(SCHEMA);
    if (!this.sql.exec("SELECT 1 FROM state").toArray().length) {
      this.sql.exec("INSERT INTO state VALUES (1, 0, ?)", crypto.randomUUID().replace(/-/g, ""));
    }
  }

  /** One page of heads with key > after, optionally only those whose search text contains `query`. */
  listConversations(after: string, query: string | null, limit: number): string {
    let rows: Record<string, SqlStorageValue>[];
    if (query === null) {
      rows = this.sql.exec("SELECT key, metadata, revision FROM heads WHERE key > ? ORDER BY key LIMIT ?", after, limit + 1).toArray();
    } else if (query.length <= OVERLAP) {
      rows = this.sql
        .exec(
          "SELECT key, metadata, revision FROM heads WHERE key > ? AND key IN (SELECT key FROM search_parts WHERE instr(text || tail, ?) > 0) ORDER BY key LIMIT ?",
          after,
          query,
          limit + 1,
        )
        .toArray();
    } else {
      rows = this.longQuery(after, query, limit + 1);
    }
    const next = rows.length > limit ? JSON.stringify(rows[limit - 1].key) : "null";
    return `{"items":[${rows.slice(0, limit).map(item).join(",")}],"next":${next}}`;
  }

  /** A query longer than the overlap may straddle parts, so each candidate's text is joined first. */
  private longQuery(after: string, query: string, wanted: number): Record<string, SqlStorageValue>[] {
    const found: Record<string, SqlStorageValue>[] = [];
    const head = query.slice(0, OVERLAP);
    const candidates = this.sql
      .exec("SELECT DISTINCT key FROM search_parts WHERE key > ? AND instr(text || tail, ?) > 0 ORDER BY key", after, head)
      .toArray()
      .map((row) => row.key as string);
    for (const key of candidates) {
      const text = this.sql
        .exec("SELECT text FROM search_parts WHERE key = ? ORDER BY part", key)
        .toArray()
        .map((row) => row.text as string)
        .join("");
      if (!text.includes(query)) continue;
      found.push(this.sql.exec("SELECT key, metadata, revision FROM heads WHERE key = ?", key).one());
      if (found.length >= wanted) break;
    }
    return found;
  }

  /** Changed heads and a watermark from one snapshot, ordered by sequence. */
  listChanges(since: number, limit: number): string {
    const state = this.sql.exec("SELECT sequence, generation FROM state").one();
    const rows = this.sql
      .exec(
        "SELECT key, metadata, revision, change_sequence FROM heads WHERE change_sequence > ? AND change_sequence <= ? ORDER BY change_sequence LIMIT ?",
        since,
        state.sequence,
        limit + 1,
      )
      .toArray();
    const next = rows.length > limit ? String(rows[limit - 1].change_sequence) : "null";
    return `{"items":[${rows.slice(0, limit).map(item).join(",")}],"next":${next},"sequence":${state.sequence},"generation":${JSON.stringify(state.generation)}}`;
  }

  /** The manifest text of the head, or of one revision of the conversation. */
  readConversation(key: string, revision: string | null): string | null {
    const row = revision
      ? this.sql.exec("SELECT manifest FROM revisions WHERE key = ? AND revision = ?", key, revision).toArray()[0]
      : this.sql.exec("SELECT r.manifest FROM revisions r JOIN heads h ON h.key = r.key AND h.revision = r.revision WHERE h.key = ?", key).toArray()[0];
    return row ? (row.manifest as string) : null;
  }

  /** The digests not stored yet, in the order and multiplicity given. */
  missingChunks(digests: string[]): string[] {
    const present = new Set<string>();
    for (let start = 0; start < digests.length; start += IN_BATCH) {
      const batch = digests.slice(start, start + IN_BATCH);
      const marks = batch.map(() => "?").join(",");
      for (const row of this.sql.exec(`SELECT digest FROM chunks WHERE digest IN (${marks})`, ...batch)) present.add(row.digest as string);
    }
    return digests.filter((digest) => !present.has(digest));
  }

  /** Record chunks whose bytes are already in R2. */
  addChunks(chunks: [string, number][]): void {
    for (const [digest, size] of chunks) this.sql.exec("INSERT OR IGNORE INTO chunks VALUES (?, ?)", digest, size);
  }

  /** Advance a conversation's head from `expected` to `revision`; the same revision again is a no-op. */
  put(head: Head, manifest: string, expected: string | null): PutResult {
    try {
      return this.ctx.storage.transactionSync(() => {
        const current = this.sql.exec("SELECT revision FROM heads WHERE key = ?", head.key).toArray()[0];
        if (current && current.revision === head.revision) return { key: head.key, revision: head.revision };
        if ((current ? current.revision : null) !== expected) throw new Conflict("Conversation changed; resync from its current source.");
        // The counter and visible head commit together, including conflict rollback.
        const sequence = this.sql.exec("UPDATE state SET sequence = sequence + 1 RETURNING sequence").one().sequence as number;
        this.writeHead({ ...head, change_sequence: sequence });
        this.sql.exec("INSERT OR IGNORE INTO revisions VALUES (?, ?, ?)", head.key, head.revision, manifest);
        return { key: head.key, revision: head.revision };
      });
    } catch (error) {
      if (error instanceof Conflict) return { conflict: error.message };
      throw error;
    }
  }

  private writeHead(head: ImportedHead): void {
    this.sql.exec("INSERT OR REPLACE INTO heads VALUES (?, ?, ?, ?, ?)", head.key, head.revision, head.mtime, head.metadata, head.change_sequence);
    this.sql.exec("DELETE FROM search_parts WHERE key = ?", head.key);
    searchParts(head.search_text).forEach(([text, tail], part) => this.sql.exec("INSERT INTO search_parts VALUES (?, ?, ?, ?)", head.key, part, text, tail));
  }

  // --- admin import from a Python archive.db -------------------------------------------------------

  /** Copy revisions as they are; existing ones are kept, so a rerun changes nothing. */
  importRevisions(rows: { key: string; revision: string; manifest: string }[]): number {
    let added = 0;
    for (const row of rows) added += this.sql.exec("INSERT OR IGNORE INTO revisions VALUES (?, ?, ?)", row.key, row.revision, row.manifest).rowsWritten;
    return added;
  }

  /**
   * Copy heads with their original change sequence; a key that already has a head keeps it.
   * A sequence already taken by another key gets the next free one.
   */
  importHeads(rows: ImportedHead[]): number {
    let added = 0;
    for (const row of rows) {
      this.ctx.storage.transactionSync(() => {
        if (this.sql.exec("SELECT 1 FROM heads WHERE key = ?", row.key).toArray().length) return;
        let sequence = row.change_sequence;
        if (this.sql.exec("SELECT 1 FROM heads WHERE change_sequence = ?", sequence).toArray().length) {
          sequence = this.sql.exec("UPDATE state SET sequence = sequence + 1 RETURNING sequence").one().sequence as number;
        }
        this.writeHead({ ...row, change_sequence: sequence });
        this.sql.exec("UPDATE state SET sequence = max(sequence, ?)", sequence);
        added++;
      });
    }
    return added;
  }

  /** Adopt the source's generation, so readers' change cursors stay valid across the move. */
  importState(sequence: number, generation: string): { sequence: number; generation: string } {
    this.sql.exec("UPDATE state SET sequence = max(sequence, ?), generation = ?", sequence, generation);
    return this.sql.exec("SELECT sequence, generation FROM state").one() as { sequence: number; generation: string };
  }

  stats(): Record<string, number> {
    const count = (table: string) => this.sql.exec(`SELECT count(*) AS n FROM ${table}`).one().n as number;
    return { heads: count("heads"), revisions: count("revisions"), chunks: count("chunks") };
  }

  /** Empties the index and the chunk bodies; only reachable when the deployment enables resets (tests). */
  async reset(): Promise<void> {
    for (const table of ["heads", "search_parts", "revisions", "chunks"]) this.sql.exec(`DELETE FROM ${table}`);
    this.sql.exec("UPDATE state SET sequence = 0, generation = ?", crypto.randomUUID().replace(/-/g, ""));
    for (;;) {
      const listed = await this.env.ARCHIVE.list({ prefix: "chunks/" });
      if (!listed.objects.length) break;
      await this.env.ARCHIVE.delete(listed.objects.map((object) => object.key));
    }
  }
}

class Conflict extends Error {}
