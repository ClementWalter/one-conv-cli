/** Shares: a node links a guest to one session (/v1/shares); the guest chats through /v1/shares/<id>/messages and the /s/<id> page. */

import { runInDurableObject } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, test } from "vitest";
import type { Env } from "../src/env";
import { ADMIN, body, call, message, mint, publish } from "./helpers";

beforeEach(async () => {
  await (await call("/admin/reset", { method: "POST", token: ADMIN })).text();
});

const bindings = env as unknown as Env;
const relay = () => bindings.RELAY.get(bindings.RELAY.idFromName("relay"));

interface Made {
  id: string;
  secret: string;
  address: string;
  expires: number;
}

interface Delivery {
  from: string;
  to: string;
  kind?: string;
  label?: string;
  text: string;
  delivery: string;
}

/** Node `mac` publishes claude:aaa (@lead) and shares it with Alice. */
async function shared(extra: Record<string, unknown> = {}): Promise<{ mac: string; made: Made }> {
  const mac = await mint("mac");
  await (await publish(mac, [{ address: "claude:aaa", name: "lead", title: "Fix the build", status: "idle" }])).text();
  const made = await body<Made>(call("/v1/shares", { method: "POST", token: mac, body: { address: "claude:aaa", label: "Alice", ...extra } }));
  return { mac, made };
}

function guest(made: Made, method = "GET", text?: unknown, secret = made.secret): Promise<Response> {
  return call(`/v1/shares/${made.id}/messages${method === "GET" ? "?after=0" : ""}`, {
    method,
    headers: { "x-share-secret": secret },
    ...(method === "POST" ? { body: { text } } : {}),
  });
}

async function inbox(token: string): Promise<Delivery[]> {
  return (await body<{ messages: Delivery[] }>(call("/v1/inbox?wait=0", { token }))).messages;
}

describe("making a share", () => {
  test("needs a node token", async () => {
    expect((await call("/v1/shares", { method: "POST", body: { address: "claude:aaa" } })).status).toBe(401);
  });

  test("returns a sixteen hex character id", async () => {
    expect((await shared()).made.id).toMatch(/^[0-9a-f]{16}$/);
  });

  test("binds the full session address", async () => {
    expect((await shared()).made.address).toBe("mac/claude:aaa");
  });

  test("stores only a digest of the secret", async () => {
    const { made } = await shared();
    const stored = await runInDurableObject(relay(), (_, state) => JSON.stringify(state.storage.sql.exec("SELECT * FROM shares").toArray()));
    expect(stored).not.toContain(made.secret);
  });

  test("an unpublished session is a 409", async () => {
    const mac = await mint("mac");
    expect((await call("/v1/shares", { method: "POST", token: mac, body: { address: "claude:zzz" } })).status).toBe(409);
  });

  test("another node's session is a 403", async () => {
    const { mac } = await shared();
    expect((await call("/v1/shares", { method: "POST", token: mac, body: { address: "box/claude:bbb" } })).status).toBe(403);
  });

  test.each([59, 30 * 86400 + 1, "24h", 3600.5])("a ttl of %s is a 400", async (ttl) => {
    const { mac } = await shared();
    expect((await call("/v1/shares", { method: "POST", token: mac, body: { address: "claude:aaa", ttl } })).status).toBe(400);
  });

  test("files a note to the user tagged share", async () => {
    const { mac } = await shared();
    const notes = await body<{ messages: { tags: string[] }[] }>(call("/v1/user?tag=share", { token: mac }));
    expect(notes.messages.length).toBe(1);
  });

  test("lists the share for its node", async () => {
    const { mac, made } = await shared();
    const listed = await body<{ shares: { id: string }[] }>(call("/v1/shares", { token: mac }));
    expect(listed.shares.map((row) => row.id)).toEqual([made.id]);
  });
});

describe("the guest", () => {
  test("reads the share's label", async () => {
    const { made } = await shared();
    expect((await body<{ share: { label: string } }>(guest(made))).share.label).toBe("Alice");
  });

  test("reads the session's title", async () => {
    const { made } = await shared();
    expect((await body<{ share: { title: string } }>(guest(made))).share.title).toBe("Fix the build");
  });

  test("with a wrong secret is a 404", async () => {
    const { made } = await shared();
    expect((await guest(made, "GET", undefined, "wrong")).status).toBe(404);
  });

  test("without a secret is a 404", async () => {
    const { made } = await shared();
    expect((await call(`/v1/shares/${made.id}/messages`)).status).toBe(404);
  });

  test("sends a message that reaches the bound session", async () => {
    const { mac, made } = await shared();
    await (await guest(made, "POST", "hello")).text();
    expect((await inbox(mac))[0].to).toBe("claude:aaa");
  });

  test("is named by the share's guest address", async () => {
    const { mac, made } = await shared();
    await (await guest(made, "POST", "hello")).text();
    expect((await inbox(mac))[0].from).toBe(`guest:${made.id}`);
  });

  test("arrives with kind guest", async () => {
    const { mac, made } = await shared();
    await (await guest(made, "POST", "hello")).text();
    expect((await inbox(mac))[0].kind).toBe("guest");
  });

  test("arrives with the share's label", async () => {
    const { mac, made } = await shared();
    await (await guest(made, "POST", "hello")).text();
    expect((await inbox(mac))[0].label).toBe("Alice");
  });

  test("cannot choose another recipient", async () => {
    const { mac, made } = await shared();
    await (await call(`/v1/shares/${made.id}/messages`, { method: "POST", headers: { "x-share-secret": made.secret }, body: { text: "hi", to: "box/claude:bbb" } })).text();
    expect((await inbox(mac))[0].to).toBe("claude:aaa");
  });

  test("sees its own message in the exchange", async () => {
    const { made } = await shared();
    await (await guest(made, "POST", "hello")).text();
    expect((await body<{ messages: { text: string }[] }>(guest(made))).messages.map((row) => row.text)).toEqual(["hello"]);
  });

  test("over 8 KB is a 413", async () => {
    const { made } = await shared();
    expect((await guest(made, "POST", "x".repeat(8193))).status).toBe(413);
  });

  test("an empty message is a 400", async () => {
    const { made } = await shared();
    expect((await guest(made, "POST", "  ")).status).toBe(400);
  });

  test("a twenty-first message within five minutes is a 429", async () => {
    const { made } = await shared();
    for (let i = 0; i < 20; i++) await (await guest(made, "POST", `message ${i}`)).text();
    expect((await guest(made, "POST", "one more")).status).toBe(429);
  });

  test("is refused while the session is offline", async () => {
    const { made } = await shared();
    await runInDurableObject(relay(), (_, state) => state.storage.sql.exec("UPDATE nodes SET seen = 0"));
    expect((await guest(made, "POST", "hello")).status).toBe(409);
  });

  test("of a revoked share gets a 410", async () => {
    const { mac, made } = await shared();
    await (await call(`/v1/shares/${made.id}`, { method: "DELETE", token: mac })).text();
    expect((await guest(made)).status).toBe(410);
  });

  test("of an expired share gets a 410", async () => {
    const { made } = await shared();
    await runInDurableObject(relay(), (_, state) => state.storage.sql.exec("UPDATE shares SET expires = 0"));
    expect((await guest(made)).status).toBe(410);
  });

  test("writing first files one more note to the user", async () => {
    const { mac, made } = await shared();
    await (await guest(made, "POST", "hello")).text();
    await (await guest(made, "POST", "again")).text();
    const notes = await body<{ messages: unknown[] }>(call("/v1/user?tag=share", { token: mac }));
    expect(notes.messages.length).toBe(2);
  });
});

describe("answering the guest", () => {
  test("the shared session's answer shows on the page", async () => {
    const { mac, made } = await shared();
    await (await call("/v1/messages", { method: "POST", token: mac, body: message(`guest:${made.id}`, { text: "hi Alice" }) })).text();
    expect((await body<{ messages: { from: string; text: string }[] }>(guest(made))).messages[0]).toMatchObject({ from: "agent", text: "hi Alice" });
  });

  test("another session of the node is a 403", async () => {
    const { mac, made } = await shared();
    await (await publish(mac, [{ address: "claude:aaa" }, { address: "claude:ccc" }])).text();
    const sent = await call("/v1/messages", { method: "POST", token: mac, body: message(`guest:${made.id}`, { from: "claude:ccc" }) });
    expect(sent.status).toBe(403);
  });

  test("another node is a 403", async () => {
    const { made } = await shared();
    const box = await mint("box");
    await (await publish(box, [{ address: "claude:aaa" }])).text();
    expect((await call("/v1/messages", { method: "POST", token: box, body: message(`guest:${made.id}`) })).status).toBe(403);
  });

  test("a revoked share is a 410", async () => {
    const { mac, made } = await shared();
    await (await call(`/v1/shares/${made.id}`, { method: "DELETE", token: mac })).text();
    expect((await call("/v1/messages", { method: "POST", token: mac, body: message(`guest:${made.id}`) })).status).toBe(410);
  });

  test("a retried answer is stored once", async () => {
    const { mac, made } = await shared();
    const answer = message(`guest:${made.id}`);
    await (await call("/v1/messages", { method: "POST", token: mac, body: answer })).text();
    await (await call("/v1/messages", { method: "POST", token: mac, body: answer })).text();
    expect((await body<{ messages: unknown[] }>(guest(made))).messages.length).toBe(1);
  });
});

describe("the page", () => {
  test("is HTML", async () => {
    expect((await call("/s/0123456789abcdef")).headers.get("content-type")).toBe("text/html; charset=utf-8");
  });

  test("allows no third-party resources", async () => {
    expect((await call("/s/0123456789abcdef")).headers.get("content-security-policy")).toContain("default-src 'none'");
  });

  test("reads the secret from the fragment into a header", async () => {
    expect(await (await call("/s/0123456789abcdef")).text()).toContain('"x-share-secret": secret');
  });

  test("answers a POST with 405", async () => {
    expect((await call("/s/0123456789abcdef", { method: "POST" })).status).toBe(405);
  });
});
