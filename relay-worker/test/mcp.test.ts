/** The remote MCP server: OAuth discovery, registration and the owner's consent, then each tool over JSON-RPC. */

import { exports } from "cloudflare:workers";
import { beforeEach, describe, expect, test } from "vitest";
import { messageId, terms } from "../src/mcp";
import capture from "./fixtures/capture.json";
import { ADMIN, body, call, mint, publish } from "./helpers";

// The provider accepts plain http only on loopback, so these requests use an https origin.
const ORIGIN = "https://relay.test";
const PASSPHRASE = "owner-passphrase";
const REDIRECT = "https://claude.ai/api/mcp/auth_callback";

beforeEach(async () => {
  await call("/admin/reset", { method: "POST", token: ADMIN });
});

function fetchWorker(path: string, init: RequestInit = {}): Promise<Response> {
  return exports.default.fetch(new Request(`${ORIGIN}${path}`, { redirect: "manual", ...init }));
}

function base64url(bytes: ArrayBuffer | Uint8Array): string {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

/** Session cookies of a response, as a browser would send them back. */
function cookies(response: Response): string {
  return response.headers
    .getSetCookie()
    .map((line) => line.split(";")[0])
    .join("; ");
}

interface Consent {
  clientId: string;
  verifier: string;
  handle: string;
  cookie: string;
}

/** Register a client as the Claude app does and open the consent page. */
async function openConsent(): Promise<Consent> {
  const registered = await fetchWorker("/oauth/register", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ redirect_uris: [REDIRECT], client_name: "Claude", token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"] }),
  });
  const clientId = ((await registered.json()) as { client_id: string }).client_id;
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const query = new URLSearchParams({ response_type: "code", client_id: clientId, redirect_uri: REDIRECT, code_challenge: challenge, code_challenge_method: "S256", state: "s1", resource: `${ORIGIN}/mcp` });
  const page = await fetchWorker(`/authorize?${query}`);
  const handle = /name="handle" value="([^"]+)"/.exec(await page.text())![1];
  return { clientId, verifier, handle, cookie: cookies(page) };
}

function submit(consent: Consent, fields: Record<string, string>): Promise<Response> {
  return fetchWorker("/authorize", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", cookie: consent.cookie },
    body: new URLSearchParams({ handle: consent.handle, ...fields }),
  });
}

/** An access token obtained through the whole authorization-code flow. */
async function connect(): Promise<string> {
  const consent = await openConsent();
  const approved = await submit(consent, { passphrase: PASSPHRASE, decision: "approve" });
  const code = new URL(approved.headers.get("location")!).searchParams.get("code")!;
  const token = await fetchWorker("/oauth/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: REDIRECT, client_id: consent.clientId, code_verifier: consent.verifier, resource: `${ORIGIN}/mcp` }),
  });
  return ((await token.json()) as { access_token: string }).access_token;
}

let rpcId = 0;
async function rpc(token: string, method: string, params: Record<string, unknown> = {}): Promise<Record<string, any>> {
  const response = await fetchWorker("/mcp", {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params }),
  });
  return (await response.json()) as Record<string, any>;
}

/** A tool's result parsed from its text content. */
async function tool(token: string, name: string, args: Record<string, unknown> = {}): Promise<any> {
  const reply = await rpc(token, "tools/call", { name, arguments: args });
  const text = reply.result.content[0].text as string;
  return reply.result.isError ? { error: text } : JSON.parse(text);
}

/** The fixture capture committed by node `mac`. */
async function archived(): Promise<void> {
  const mac = await mint("mac");
  await call("/archive/v1/chunks", { method: "POST", token: mac, body: { chunks: capture.chunks } });
  await call("/archive/v1/conversations", { method: "POST", token: mac, raw: capture.commit });
}

describe("discovery", () => {
  test("/mcp without a token is a 401", async () => {
    expect((await fetchWorker("/mcp", { method: "POST" })).status).toBe(401);
  });

  test("the 401 points at the protected resource metadata", async () => {
    expect((await fetchWorker("/mcp", { method: "POST" })).headers.get("www-authenticate")).toContain(`resource_metadata="${ORIGIN}/.well-known/oauth-protected-resource/mcp"`);
  });

  test("the protected resource names this Worker as its authorization server", async () => {
    expect(await body(fetchWorker("/.well-known/oauth-protected-resource/mcp"))).toMatchObject({ resource: `${ORIGIN}/mcp`, authorization_servers: [ORIGIN] });
  });

  test("the authorization server offers dynamic registration", async () => {
    expect(await body(fetchWorker("/.well-known/oauth-authorization-server"))).toMatchObject({ registration_endpoint: `${ORIGIN}/oauth/register` });
  });

  test("relay routes are not behind OAuth", async () => {
    expect(await body(call("/v1/whoami", { token: await mint("mac") }))).toEqual({ node: "mac" });
  });
});

describe("the owner's consent", () => {
  test("the right passphrase redirects to the client with a code", async () => {
    const approved = await submit(await openConsent(), { passphrase: PASSPHRASE, decision: "approve" });
    expect(new URL(approved.headers.get("location")!).searchParams.has("code")).toBe(true);
  });

  test("a wrong passphrase is refused", async () => {
    expect((await submit(await openConsent(), { passphrase: "guess", decision: "approve" })).status).toBe(403);
  });

  test("the same page works after a wrong passphrase", async () => {
    const consent = await openConsent();
    await submit(consent, { passphrase: "guess", decision: "approve" });
    expect((await submit(consent, { passphrase: PASSPHRASE, decision: "approve" })).status).toBe(302);
  });

  test("deny sends access_denied to the client", async () => {
    const denied = await submit(await openConsent(), { decision: "deny" });
    expect(new URL(denied.headers.get("location")!).searchParams.get("error")).toBe("access_denied");
  });

  test("the code exchanges for a bearer token", async () => {
    expect(await connect()).toMatch(/^owner:/);
  });
});

describe("the MCP protocol", () => {
  test("initialize echoes a supported protocol version", async () => {
    expect((await rpc(await connect(), "initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "1" } })).result.protocolVersion).toBe("2025-06-18");
  });

  test("initialize answers an unknown version with one it supports", async () => {
    expect((await rpc(await connect(), "initialize", { protocolVersion: "1999-01-01" })).result.protocolVersion).toBe("2025-06-18");
  });

  test("tools/list names every tool", async () => {
    expect((await rpc(await connect(), "tools/list")).result.tools.map((t: { name: string }) => t.name)).toEqual([
      "search_conversations",
      "read_conversation",
      "list_live_sessions",
      "send_message",
      "message_status",
      "read_notes",
    ]);
  });

  test("an unknown method is a JSON-RPC error", async () => {
    expect((await rpc(await connect(), "resources/list")).error.code).toBe(-32601);
  });

  test("a notification is accepted without a body", async () => {
    const response = await fetchWorker("/mcp", {
      method: "POST",
      headers: { authorization: `Bearer ${await connect()}`, "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
    });
    expect(response.status).toBe(202);
  });

  test("GET has no server stream", async () => {
    expect((await fetchWorker("/mcp", { headers: { authorization: `Bearer ${await connect()}` } })).status).toBe(405);
  });
});

describe("search_conversations", () => {
  test("a phrase finds the conversation", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", { query: "Forty-Two" })).results.map((r: { key: string }) => r.key)).toEqual([capture.conversation]);
  });

  test("the hit carries a snippet around the phrase", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", { query: "naïve" })).results[0].snippet).toContain("café — naïve");
  });

  test("the hit names its node", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", { query: "quartz" })).results[0].node).toBe("mac");
  });

  test("a phrase in a different order finds nothing", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", { query: "forty-two quartz" })).results).toEqual([]);
  });

  test("all_words finds words in any order", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", { query: "forty-two quartz", all_words: true })).results).toHaveLength(1);
  });

  test("a node filter excludes other nodes", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", { query: "quartz", node: "box" })).results).toEqual([]);
  });

  test("a source filter keeps matching sources", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", { query: "quartz", source: "claude" })).results).toHaveLength(1);
  });

  test("an empty query lists recent conversations", async () => {
    await archived();
    expect((await tool(await connect(), "search_conversations", {})).results).toHaveLength(1);
  });

  test("a bad limit is a tool error", async () => {
    expect(await tool(await connect(), "search_conversations", { limit: 0 })).toEqual({ error: "limit must be an integer from 1 to 100" });
  });
});

describe("read_conversation", () => {
  test("a key reads its turns", async () => {
    await archived();
    expect((await tool(await connect(), "read_conversation", { key: capture.conversation })).turns.map((t: { text: string }) => t.text)).toEqual([
      "Quartz café — naïve 🚀",
      'Forty-two.\n\t"quoted"',
    ]);
  });

  test("a live address resolves to the archived session", async () => {
    await archived();
    expect((await tool(await connect(), "read_conversation", { key: "mac/claude:session-one" })).key).toBe(capture.conversation);
  });

  test("a limit pages from the end", async () => {
    await archived();
    expect((await tool(await connect(), "read_conversation", { key: capture.conversation, limit: 1 })).previous_offset).toBe(0);
  });

  test("an unknown key is a tool error", async () => {
    expect((await tool(await connect(), "read_conversation", { key: "0".repeat(64) })).error).toContain("no archived conversation");
  });
});

describe("live tools", () => {
  test("list_live_sessions shows published sessions with their @name", async () => {
    await (await publish(await mint("box"), [{ address: "claude:bbb", name: "worker" }])).text();
    expect((await tool(await connect(), "list_live_sessions", { online_only: false })).sessions.map((s: { name: string }) => s.name)).toEqual(["@worker"]);
  });

  test("send_message arrives as the app's human user", async () => {
    const box = await mint("box");
    await (await publish(box, [{ address: "claude:bbb", name: "worker" }])).text();
    await tool(await connect(), "send_message", { to: "@worker", text: "status?" });
    expect((await body<{ messages: { from: string }[] }>(call("/v1/inbox?wait=0", { token: box }))).messages[0].from).toBe("claude-app/user");
  });

  test("a retried send is stored once", async () => {
    const box = await mint("box");
    await (await publish(box, [{ address: "claude:bbb", name: "worker" }])).text();
    const token = await connect();
    await tool(token, "send_message", { to: "@worker", text: "status?" });
    expect((await tool(token, "send_message", { to: "@worker", text: "status?" })).duplicate).toBe(true);
  });

  test("an unknown target is a tool error", async () => {
    expect((await tool(await connect(), "send_message", { to: "@nobody", text: "hi" })).error).toContain("no published session is named @nobody");
  });

  test("message_status reports the receiver's outcome", async () => {
    const box = await mint("box");
    await (await publish(box, [{ address: "claude:bbb", name: "worker" }])).text();
    const token = await connect();
    const sent = await tool(token, "send_message", { to: "@worker", text: "status?" });
    expect((await tool(token, "message_status", { id: sent.id })).recipients[0].outcome).toBe("stored");
  });
});

describe("read_notes", () => {
  test("a note an agent sent to the user reads back", async () => {
    const mac = await mint("mac");
    await (await publish(mac, [{ address: "claude:aaa" }])).text();
    await call("/v1/messages", { method: "POST", token: mac, body: { id: "a".repeat(32), to: "user", from: "claude:aaa", text: "PR merged", tags: ["done"] } });
    expect((await tool(await connect(), "read_notes", {})).messages.map((note: { text: string }) => note.text)).toEqual(["PR merged"]);
  });

  test("a tag filter excludes other notes", async () => {
    const mac = await mint("mac");
    await (await publish(mac, [{ address: "claude:aaa" }])).text();
    await call("/v1/messages", { method: "POST", token: mac, body: { id: "a".repeat(32), to: "user", from: "claude:aaa", text: "PR merged", tags: ["done"] } });
    expect((await tool(await connect(), "read_notes", { tags: ["blocked"] })).messages).toEqual([]);
  });
});

describe("helpers", () => {
  test.each([
    ["a phrase is one lowercased term", "Hello World", false, ["hello world"]],
    ["all_words splits on whitespace", "Hello  World", true, ["hello", "world"]],
    ["repeated words count once", "a a b", true, ["a", "b"]],
    ["an empty query has no terms", "  ", true, []],
  ])("terms: %s", (_, query, allWords, expected) => {
    expect(terms(query, allWords)).toEqual(expected);
  });

  test("a message id is stable within its time window", async () => {
    expect(await messageId("@a", "hi", null, 1200)).toBe(await messageId("@a", "hi", null, 1799));
  });

  test("a message id changes in the next time window", async () => {
    expect(await messageId("@a", "hi", null, 1200)).not.toBe(await messageId("@a", "hi", null, 1800));
  });
});
