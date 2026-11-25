/** Rendering and paging archived turns for read_conversation. */

import { describe, expect, test } from "vitest";
import { type RenderedTurn, TOOL_RESULT_CHARS, cleanText, page, pageStart, renderTurn, renderTurns } from "../src/transcript";

const turn = (blocks: Record<string, unknown>[], role = "assistant") => ({ ts: "2026-01-01T10:00:00Z", role, blocks });
const rendered = (count: number, size = 10): RenderedTurn[] =>
  Array.from({ length: count }, (_, index) => ({ index, ts: `2026-01-01T10:${String(index).padStart(2, "0")}:00Z`, role: "user", text: `${index}`.padEnd(size, "x") }));

describe("cleanText", () => {
  test.each([
    ["a system reminder is dropped", "before<system-reminder>noise</system-reminder> after", "before after"],
    ["a task notification is dropped", "<task-notification>done</task-notification>result", "result"],
    ["a command wrapper becomes its name", "<command-message>x</command-message> <command-name>/review</command-name>", "/review"],
    ["surrounding whitespace is trimmed", "  text \n", "text"],
  ])("%s", (_, text, expected) => {
    expect(cleanText(text)).toBe(expected);
  });
});

describe("renderTurn", () => {
  test.each([
    ["text blocks are joined by lines", turn([{ type: "text", text: "one" }, { type: "text", text: "two" }]), false, "one\ntwo"],
    ["thinking is hidden without raw", turn([{ type: "thinking", thinking: "hmm" }, { type: "text", text: "answer" }]), false, "answer"],
    ["thinking shows with raw", turn([{ type: "thinking", thinking: "hmm" }]), true, "[thinking] hmm"],
    ["a tool call shows with raw", turn([{ type: "tool_use", name: "Bash", input: { command: "ls" } }]), true, '→ Bash({"command":"ls"})'],
    ["a tool result shows with raw", turn([{ type: "tool_result", content: [{ type: "text", text: "ok" }] }], "user"), true, "← ok"],
    ["a plain-text capture turn renders its text", { ts: "", role: "user", text: "hello" }, false, "hello"],
  ])("%s", (_, value, raw, expected) => {
    expect(renderTurn(value, raw)).toBe(expected);
  });

  test("a bare tool round-trip renders to nothing without raw", () => {
    expect(renderTurn(turn([{ type: "tool_use", name: "Bash", input: {} }]), false)).toBeNull();
  });

  test("a long tool result keeps its start and end within the limit", () => {
    const text = renderTurn(turn([{ type: "tool_result", content: "a".repeat(TOOL_RESULT_CHARS) + "b".repeat(TOOL_RESULT_CHARS) }]), true)!;
    expect(text).toBe(`← ${"a".repeat(TOOL_RESULT_CHARS / 2)}\n[… ${TOOL_RESULT_CHARS} characters cut …]\n${"b".repeat(TOOL_RESULT_CHARS / 2)}`);
  });
});

describe("renderTurns", () => {
  test("indexes count only the turns that render", () => {
    const turns = [turn([{ type: "text", text: "a" }]), turn([{ type: "tool_use", name: "x" }]), turn([{ type: "text", text: "b" }])];
    expect(renderTurns(turns, false).map((value) => [value.index, value.text])).toEqual([
      [0, "a"],
      [1, "b"],
    ]);
  });
});

describe("pageStart", () => {
  test.each([
    ["the last turns by default", 10, 4, null, null, 6],
    ["an explicit offset", 10, 4, 2, null, 2],
    ["an offset past the end stops at the end", 10, 4, 50, null, 10],
    ["centered on the first matching text", 10, 4, null, "5XX", 3],
    ["centered on a matching time", 10, 4, null, "2026-01-01T10:07", 5],
    ["the last turns when nothing matches", 10, 4, null, "absent", 6],
    ["a short conversation starts at zero", 2, 4, null, null, 0],
  ])("%s", (_, count, limit, offset, around, expected) => {
    expect(pageStart(rendered(count), limit, offset, around)).toBe(expected);
  });
});

describe("page", () => {
  test("it holds at most limit turns", () => {
    expect(page(rendered(10), 0, 3, 1000).turns.map((value) => value.index)).toEqual([0, 1, 2]);
  });

  test("it stops when the budget is used", () => {
    expect(page(rendered(10, 100), 0, 10, 250).turns).toHaveLength(2);
  });

  test("it always holds one turn even above the budget", () => {
    expect(page(rendered(3, 500), 0, 10, 100).turns).toHaveLength(1);
  });

  test("next_offset follows the page", () => {
    expect(page(rendered(10), 2, 3, 1000).next_offset).toBe(5);
  });

  test("next_offset is null at the end", () => {
    expect(page(rendered(10), 8, 3, 1000).next_offset).toBeNull();
  });

  test("previous_offset is one limit back", () => {
    expect(page(rendered(10), 5, 3, 1000).previous_offset).toBe(2);
  });

  test("previous_offset is null at the start", () => {
    expect(page(rendered(10), 0, 3, 1000).previous_offset).toBeNull();
  });
});
