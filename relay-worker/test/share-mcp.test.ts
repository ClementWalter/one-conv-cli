/** The share's MCP server, POST /s/<id>/mcp with the secret as bearer token, and guest GETs that wait for an answer. */

import { beforeEach, describe, expect, test } from "vitest";
import { ADMIN, body, call, message, mint, publish } from "./helpers";

beforeEach(async () => {
  await (await call("/admin/reset", { method: "POST", token: ADMIN })).text();
});

interface Made {
  id: string;
  secret: string;
}

/** Node `mac` publishes claude:aaa and shares it with Alice. */
async function shared(): Promise<{ mac: string; made: Made }> {
  const mac = await mint("mac");
  await (await publish(mac, [{ address: "claude:aaa", name: "lead", title: "Fix the build", status: "idle" }])).text();
  const made = await body<Made>(call("/v1/shares", { method: "POST", token: mac, body: { address: "claude:aaa", label: "Alice" } }));
  return { mac, made };
}

function rpc(made: Made, method: string, params: Record<string, unknown> = {}, secret = made.secret, id: number | null = 1): Promise<Response> {
  return call(`/s/${made.id}/mcp`, { method: "POST", token: secret, body: { jsonrpc: "2.0", method, params, ...(id === null ? {} : { id }) } });
}

async function tool(made: Made, name: string, args: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const { result } = await body<{ result: { structuredContent: Record<string, unknown>; isError?: boolean; content: { text: string }[] } }>(rpc(made, "tools/call", { name, arguments: args }));
  return result.isError ? { error: result.content[0].text } : result.structuredContent;
}

function answer(mac: string, made: Made, text = "the answer"): Promise<Response> {
  return call("/v1/messages", { method: "POST", token: mac, body: message(`guest:${made.id}`, { text }) });
}

describe("the share's MCP server", () => {
  test("needs a bearer secret", async () => {
    const { made } = await shared();
    expect((await rpc(made, "ping", {}, "")).status).toBe(401);
  });

  test("refuses a wrong secret", async () => {
    const { made } = await shared();
    expect((await rpc(made, "ping", {}, "wrong")).status).toBe(404);
  });

  test("of a revoked share answers 410", async () => {
    const { mac, made } = await shared();
    await (await call(`/v1/shares/${made.id}`, { method: "DELETE", token: mac })).text();
    expect((await rpc(made, "ping")).status).toBe(410);
  });

  test("answers GET with 405", async () => {
    const { made } = await shared();
    expect((await call(`/s/${made.id}/mcp`, { token: made.secret })).status).toBe(405);
  });

  test("warns that the session is another person's agent", async () => {
    const { made } = await shared();
    const { result } = await body<{ result: { instructions: string } }>(rpc(made, "initialize", { protocolVersion: "2025-06-18" }));
    expect(result.instructions).toContain("another person's agent");
  });

  test("lists four tools", async () => {
    const { made } = await shared();
    const { result } = await body<{ result: { tools: { name: string }[] } }>(rpc(made, "tools/list"));
    expect(result.tools.map((t) => t.name).sort()).toEqual(["ask", "read_replies", "send", "session_info"]);
  });

  test("accepts a notification with 202", async () => {
    const { made } = await shared();
    expect((await rpc(made, "notifications/initialized", {}, made.secret, null)).status).toBe(202);
  });

  test("send reaches the bound session as a guest", async () => {
    const { mac, made } = await shared();
    await tool(made, "send", { text: "hello" });
    const { messages } = await body<{ messages: { kind: string }[] }>(call("/v1/inbox?wait=0", { token: mac }));
    expect(messages[0].kind).toBe("guest");
  });

  test("ask returns the answer the session posts while it waits", async () => {
    const { mac, made } = await shared();
    const asking = tool(made, "ask", { text: "status?", wait_seconds: 30 });
    await new Promise((resolve) => setTimeout(resolve, 100));
    await (await answer(mac, made)).text();
    expect((await asking).reply).toBe("the answer");
  });

  test("ask without an answer is pending", async () => {
    const { made } = await shared();
    expect((await tool(made, "ask", { text: "anyone?", wait_seconds: 0 })).status).toBe("pending");
  });

  test("read_replies returns answers after the cursor", async () => {
    const { mac, made } = await shared();
    const { after } = await tool(made, "send", { text: "question" });
    await (await answer(mac, made)).text();
    expect(((await tool(made, "read_replies", { after })).replies as { text: string }[]).map((r) => r.text)).toEqual(["the answer"]);
  });

  test("session_info names the session by its @name", async () => {
    const { made } = await shared();
    expect((await tool(made, "session_info")).name).toBe("lead");
  });

  test("a message over 8 KB is a tool error", async () => {
    const { made } = await shared();
    expect((await tool(made, "send", { text: "x".repeat(8193) })).error).toContain("8 KB");
  });

  test("wait_seconds over ten minutes is a tool error", async () => {
    const { made } = await shared();
    expect((await tool(made, "ask", { text: "hi", wait_seconds: 601 })).error).toContain("wait_seconds");
  });

  test("an unknown tool is a protocol error", async () => {
    const { made } = await shared();
    expect((await body<{ error: { code: number } }>(rpc(made, "tools/call", { name: "read_transcript" }))).error.code).toBe(-32602);
  });
});

describe("a guest GET with wait", () => {
  test("returns the answer posted while it waits", async () => {
    const { mac, made } = await shared();
    const { after } = await tool(made, "send", { text: "question" });
    const waiting = call(`/v1/shares/${made.id}/messages?after=${after}&wait=10`, { headers: { "x-share-secret": made.secret } });
    await new Promise((resolve) => setTimeout(resolve, 100));
    await (await answer(mac, made, "later")).text();
    const { messages } = await body<{ messages: { text: string }[] }>(waiting);
    expect(messages[messages.length - 1].text).toBe("later");
  });

  test("ends when the share is revoked", async () => {
    const { mac, made } = await shared();
    const waiting = call(`/v1/shares/${made.id}/messages?after=0&wait=10`, { headers: { "x-share-secret": made.secret } });
    await new Promise((resolve) => setTimeout(resolve, 100));
    await (await call(`/v1/shares/${made.id}`, { method: "DELETE", token: mac })).text();
    expect((await waiting).status).toBe(410);
  });
});
