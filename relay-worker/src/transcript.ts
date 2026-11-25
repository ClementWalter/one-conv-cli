/**
 * An archived capture's `turns` field as readable text, as `one_conv_cli.py` renders a thread:
 * text blocks cleaned of harness noise, and with `raw` also thinking, tool calls and tool output.
 * A page of turns is bounded in characters, so one read never floods the caller's context.
 */

export interface Turn {
  ts: string;
  role: string;
  blocks?: Record<string, unknown>[];
  /** Captures of a few cloud sources carry plain text instead of blocks. */
  text?: string;
}

export interface RenderedTurn {
  index: number;
  ts: string;
  role: string;
  text: string;
}

export interface Page {
  total: number;
  turns: RenderedTurn[];
  /** The index to pass as `offset` for the turns after this page, or null at the end. */
  next_offset: number | null;
  /** The index to pass as `offset` for the turns before this page, or null at the start. */
  previous_offset: number | null;
}

const SYSTEM_REMINDER = /<system-reminder>[\s\S]*?<\/system-reminder>/g;
const TASK_NOTIFICATION = /<task-notification>[\s\S]*?<\/task-notification>/g;
const COMMAND_WRAPPER = /<command-message>[\s\S]*?<\/command-message>\s*<command-name>([\s\S]*?)<\/command-name>/g;
// A long tool output keeps its start and its end, where errors land.
export const TOOL_RESULT_CHARS = 4000;
// One turn never takes a whole page on its own.
export const TURN_CHARS = 20_000;

export function cleanText(text: string): string {
  return text.replace(SYSTEM_REMINDER, "").replace(TASK_NOTIFICATION, "").replace(COMMAND_WRAPPER, "$1").trim();
}

function cut(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const half = Math.floor(limit / 2);
  return `${text.slice(0, half)}\n[… ${text.length - 2 * half} characters cut …]\n${text.slice(-half)}`;
}

function toolResult(block: Record<string, unknown>): string {
  const content = block.content;
  const text = Array.isArray(content)
    ? content
        .filter((part): part is Record<string, unknown> => typeof part === "object" && part !== null && typeof part.text === "string" && part.text !== "")
        .map((part) => part.text as string)
        .join("\n")
    : String(content ?? "");
  return `← ${cut(text.trim(), TOOL_RESULT_CHARS)}`;
}

/** One turn's display text, or null when it holds nothing worth showing (a bare tool round-trip without `raw`). */
export function renderTurn(turn: Turn, raw: boolean): string | null {
  if (!turn.blocks) {
    const text = cleanText(turn.text ?? "");
    return text ? cut(text, TURN_CHARS) : null;
  }
  const parts: string[] = [];
  for (const block of turn.blocks) {
    if (block.type === "text") {
      const text = cleanText(String(block.text ?? ""));
      if (text) parts.push(text);
    } else if (raw && block.type === "thinking") {
      const thinking = String(block.thinking ?? "").trim();
      if (thinking) parts.push(`[thinking] ${thinking}`);
    } else if (raw && block.type === "tool_use") {
      parts.push(`→ ${String(block.name ?? "?")}(${JSON.stringify(block.input ?? {})})`);
    } else if (raw && block.type === "tool_result") {
      parts.push(toolResult(block));
    }
  }
  return parts.length ? cut(parts.join("\n"), TURN_CHARS) : null;
}

/** The turns that render to something, numbered in that order: `index` is what `offset` takes. */
export function renderTurns(turns: Turn[], raw: boolean): RenderedTurn[] {
  const rendered: RenderedTurn[] = [];
  for (const turn of turns) {
    const text = renderTurn(turn, raw);
    if (text !== null) rendered.push({ index: rendered.length, ts: turn.ts, role: turn.role, text });
  }
  return rendered;
}

/** Where a page starts: `offset` when given, else around the first turn containing `around`, else the last `limit` turns. */
export function pageStart(turns: RenderedTurn[], limit: number, offset: number | null, around: string | null): number {
  if (offset !== null) return Math.max(0, Math.min(offset, turns.length));
  if (around) {
    const needle = around.toLowerCase();
    const hit = turns.findIndex((turn) => turn.text.toLowerCase().includes(needle) || turn.ts.startsWith(around));
    if (hit >= 0) return Math.max(0, hit - Math.floor(limit / 2));
  }
  return Math.max(0, turns.length - limit);
}

/** At most `limit` turns from `start`, stopping early once `budget` characters are used (always at least one turn). */
export function page(turns: RenderedTurn[], start: number, limit: number, budget: number): Page {
  const out: RenderedTurn[] = [];
  let used = 0;
  for (let index = start; index < turns.length && out.length < limit; index++) {
    const size = turns[index].text.length;
    if (out.length && used + size > budget) break;
    out.push(turns[index]);
    used += size;
  }
  const end = start + out.length;
  return {
    total: turns.length,
    turns: out,
    next_offset: end < turns.length ? end : null,
    previous_offset: start > 0 ? Math.max(0, start - limit) : null,
  };
}
