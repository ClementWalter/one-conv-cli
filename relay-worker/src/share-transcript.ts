/**
 * The transcript of a shared session, for a share made with `--transcript`: the Worker's side of
 * one_conv/live/share_transcript.py.
 *
 * It is read from the archive copy (ArchiveIndex + R2) of the one session the grant names, NODE
 * and session id fixed at creation, never from a key the guest gives. Turns render with
 * transcript.ts exactly as the remote MCP's read_conversation renders them. Search runs over the
 * same rendered text of that session alone, without `raw`, so hidden blocks are never searched.
 */

import type { Env } from "./env";
import { HttpError } from "./http";
import { ToolError, field } from "./mcp";
import { type Page, type RenderedTurn, type Turn, page, pageStart, renderTurns } from "./transcript";

// A page of turns holds at most this many characters, as a remote MCP read does.
export const READ_BUDGET = 60_000;
const SNIPPET = 240;

export type RenderedTurns = RenderedTurn[];

export interface Hit {
  index: number;
  ts: string;
  role: string;
  snippet: string;
}

/** The session id of a local address harness:id, as the archive's metadata names it. */
function sessionId(address: string): string {
  return address.slice(address.indexOf(":") + 1);
}

function index(env: Env) {
  return env.ARCHIVE_INDEX.get(env.ARCHIVE_INDEX.idFromName("archive"));
}

/** {key, mtime} of the newest archived copy of NODE/address, or null. */
export async function archived(env: Env, node: string, address: string): Promise<{ key: string; mtime: number | null } | null> {
  const found = await index(env).findSession(sessionId(address), node);
  if (!found) return null;
  let mtime: number | null = null;
  try {
    const parsed = JSON.parse(found.metadata) as { mtime?: unknown };
    mtime = typeof parsed.mtime === "number" ? parsed.mtime : null;
  } catch {
    // Python may encode a non-finite number as NaN, which JSON.parse refuses: the time is then unknown.
  }
  return { key: found.key, mtime };
}

/** The rendered turns and archived mtime of the shared session; a 404 while the archive has no copy yet. */
export async function load(env: Env, node: string, address: string, raw: boolean): Promise<{ turns: RenderedTurn[]; mtime: number | null }> {
  const head = await archived(env, node, address);
  const manifest = head ? (JSON.parse((await index(env).readConversation(head.key, null)) ?? "null") as { fields: Record<string, string[]> } | null) : null;
  if (!head || !manifest) throw new HttpError(404, `the relay's archive holds no copy of ${node}/${address} yet; its bridge syncs it every minute`);
  try {
    return { turns: renderTurns((await field(env, manifest.fields.turns)) as Turn[], raw), mtime: head.mtime };
  } catch (error) {
    if (error instanceof ToolError) throw new HttpError(503, error.message);
    throw error;
  }
}

/** A page of turns, as read_conversation pages them. */
export function read(turns: RenderedTurn[], offset: number | null, limit: number, around: string | null): Page {
  return page(turns, pageStart(turns, limit, offset, around), limit, READ_BUDGET);
}

/** Turns holding every word of `query` (any order, any case), newest first, with a snippet around the first word. */
export function search(turns: RenderedTurn[], query: string, limit: number): { hits: Hit[]; total_hits: number } {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = turns.filter((turn) => words.length > 0 && words.every((word) => turn.text.toLowerCase().includes(word)));
  const hits = (limit ? matches.slice(-limit) : []).reverse().map((turn) => {
    const start = Math.max(0, turn.text.toLowerCase().indexOf(words[0]) - Math.floor(SNIPPET / 2));
    return { index: turn.index, ts: turn.ts, role: turn.role, snippet: turn.text.slice(start, start + SNIPPET) };
  });
  return { hits, total_hits: matches.length };
}
