/** A share that grants the transcript reads the shared session's archived copy, through /v1/shares/<id>/transcript and the MCP tools. */

import { beforeEach, describe, expect, test } from "vitest";
import { search } from "../src/share-transcript";
import capture from "./fixtures/capture.json";
import { ADMIN, body, call, mint, publish } from "./helpers";

beforeEach(async () => {
  await (await call("/admin/reset", { method: "POST", token: ADMIN })).text();
});

interface Made {
  id: string;
  secret: string;
  transcript: boolean;
}

/** Node `mac` archives the fixture capture (session-one), publishes it and shares it. */
async function shared(transcript: boolean): Promise<{ mac: string; made: Made }> {
  const mac = await mint("mac");
  await (await call("/archive/v1/chunks", { method: "POST", token: mac, body: { chunks: capture.chunks } })).text();
  await (await call("/archive/v1/conversations", { method: "POST", token: mac, raw: capture.commit })).text();
  await (await publish(mac, [{ address: "claude:session-one", title: "Quartz" }])).text();
  const made = await body<Made>(call("/v1/shares", { method: "POST", token: mac, body: { address: "claude:session-one", label: "Alice", transcript } }));
  return { mac, made };
}

function transcript(made: Made, path = ""): Promise<Response> {
  return call(`/v1/shares/${made.id}/transcript${path}`, { headers: { "x-share-secret": made.secret } });
}

async function tool(made: Made, name: string, args: Record<string, unknown> = {}): Promise<Record<string, unknown> & { error?: string }> {
  const { result } = await body<{ result: { structuredContent: Record<string, unknown>; isError?: boolean; content: { text: string }[] } }>(
    call(`/s/${made.id}/mcp`, { method: "POST", token: made.secret, body: { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } } }),
  );
  return result.isError ? { error: result.content[0].text } : result.structuredContent;
}

describe("a share with the transcript", () => {
  test("says so", async () => {
    expect((await shared(true)).made.transcript).toBe(true);
  });

  test("reads the shared session's turns", async () => {
    const { made } = await shared(true);
    const page = await body<{ turns: { text: string }[] }>(transcript(made));
    expect(page.turns.map((turn) => turn.text)).toEqual(["Quartz café — naïve 🚀", 'Forty-two.\n\t"quoted"']);
  });

  test("reports when the copy was archived", async () => {
    const { made } = await shared(true);
    expect((await body<{ archived_through: number }>(transcript(made))).archived_through).toBe(1767225600);
  });

  test("searches the shared session", async () => {
    const { made } = await shared(true);
    expect((await body<{ total_hits: number }>(transcript(made, "/search?query=forty"))).total_hits).toBe(1);
  });

  test("read_session ignores a key for another conversation", async () => {
    const { made } = await shared(true);
    expect(((await tool(made, "read_session", { key: "0".repeat(64) })).turns as unknown[]).length).toBe(2);
  });

  test("session_info shows the title", async () => {
    const { made } = await shared(true);
    expect((await tool(made, "session_info")).title).toBe("Quartz");
  });

  test("a bad limit is a 400", async () => {
    const { made } = await shared(true);
    expect((await transcript(made, "?limit=0")).status).toBe(400);
  });
});

describe("a share without the transcript", () => {
  test("refuses the transcript endpoint", async () => {
    const { made } = await shared(false);
    expect((await transcript(made)).status).toBe(403);
  });

  test("refuses read_session", async () => {
    const { made } = await shared(false);
    expect((await tool(made, "read_session")).error).toContain("does not grant");
  });

  test("session_info hides the title", async () => {
    const { made } = await shared(false);
    expect("title" in (await tool(made, "session_info"))).toBe(false);
  });

  test("lists no transcript tools", async () => {
    const { made } = await shared(false);
    const { result } = await body<{ result: { tools: { name: string }[] } }>(
      call(`/s/${made.id}/mcp`, { method: "POST", token: made.secret, body: { jsonrpc: "2.0", id: 1, method: "tools/list" } }),
    );
    expect(result.tools.map((t) => t.name)).not.toContain("read_session");
  });
});

describe("search", () => {
  const turns = [
    { index: 0, ts: "a", role: "user", text: "deploy quartz" },
    { index: 1, ts: "b", role: "assistant", text: "Quartz is deployed" },
    { index: 2, ts: "c", role: "user", text: "thanks" },
  ];

  test("needs every word, newest first", () => {
    expect(search(turns, "QUARTZ deploy", 10).hits.map((hit) => hit.index)).toEqual([1, 0]);
  });

  test("keeps the newest hits within the limit", () => {
    expect(search(turns, "quartz", 1).hits.map((hit) => hit.index)).toEqual([1]);
  });
});
