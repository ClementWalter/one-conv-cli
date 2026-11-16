/** The relay API (/v1): the rules of one_conv/live/hub.py, through the Worker's own fetch handler. */

import { beforeEach, describe, expect, test } from "vitest";
import { MAX_BODY, online } from "../src/relay";
import { ADMIN, body, call, message, mint, publish, twoNodes } from "./helpers";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);

beforeEach(async () => {
  await call("/admin/reset", { method: "POST", token: ADMIN });
});

describe("authentication", () => {
  test("a request without a token is refused", async () => {
    expect((await call("/v1/whoami")).status).toBe(401);
  });

  test("a request without a token says a bearer header is required", async () => {
    expect(await body(call("/v1/whoami"))).toEqual({ error: "exactly one bearer authorization header is required" });
  });

  test("a non-bearer scheme is refused", async () => {
    expect((await call("/v1/whoami", { headers: { authorization: "Basic abc" } })).status).toBe(401);
  });

  test("the token decides the node", async () => {
    expect(await body(call("/v1/whoami", { token: await mint("mac") }))).toEqual({ node: "mac" });
  });

  test("a revoked token is refused", async () => {
    const token = await mint("mac");
    await call("/admin/tokens/mac", { method: "DELETE", token: ADMIN });
    expect((await call("/v1/whoami", { token })).status).toBe(401);
  });

  test("minting again replaces the node's token", async () => {
    const first = await mint("mac");
    await mint("mac");
    expect((await call("/v1/whoami", { token: first })).status).toBe(401);
  });
});

describe("routing", () => {
  test("an unknown path is a 404 even without a token", async () => {
    expect((await call("/v1/nowhere")).status).toBe(404);
  });

  test("a known path under another method is a 405", async () => {
    expect((await call("/v1/whoami", { method: "POST" })).status).toBe(405);
  });
});

describe("publishing", () => {
  test("a node publishes its sessions", async () => {
    expect(await body(publish(await mint("mac"), [{ address: "claude:a" }, { address: "claude:b" }]))).toEqual({ published: 2 });
  });

  test.each([
    ["not a list", { sessions: "claude:a" }, 400],
    ["an address with a slash", { sessions: [{ address: "mac/claude:a" }] }, 400],
    ["an address listed twice", { sessions: [{ address: "claude:a" }, { address: "claude:a" }] }, 400],
    ["a malformed name", { sessions: [{ address: "claude:a", name: "Lead" }] }, 400],
    ["one name for two sessions", { sessions: [{ address: "claude:a", name: "x" }, { address: "claude:b", name: "x" }] }, 409],
    ["an unknown product", { sessions: [{ address: "cursor:a", product: "cursor-web" }] }, 400],
  ])("%s is refused", async (_, sent, status) => {
    expect((await call("/v1/sessions", { method: "PUT", token: await mint("mac"), body: sent })).status).toBe(status);
  });

  test("a name used on another node is refused", async () => {
    const { mac } = await twoNodes();
    expect((await publish(mac, [{ address: "claude:aaa", name: "worker" }])).status).toBe(409);
  });

  test("the directory lists each session with its node address", async () => {
    await twoNodes();
    const { sessions } = await body<{ sessions: { address: string }[] }>(call("/v1/sessions", { token: await mint("viewer") }));
    expect(sessions.map((s) => s.address)).toEqual(["box/claude:bbb", "mac/claude:aaa"]);
  });

  test("a fresh publication is online", async () => {
    const { mac } = await twoNodes();
    const { sessions } = await body<{ sessions: { online: boolean }[] }>(call("/v1/sessions", { token: mac }));
    expect(sessions.every((s) => s.online)).toBe(true);
  });

  test("a session without a product lists an empty one", async () => {
    const { mac } = await twoNodes();
    const { sessions } = await body<{ sessions: { product: string }[] }>(call("/v1/sessions", { token: mac }));
    expect(sessions[0].product).toBe("");
  });
});

describe("the lease", () => {
  test.each([
    ["bridge and publication recent", 100, 100, 130, true],
    ["bridge silent past the lease", 60, 100, 130, false],
    ["publication older than the lease", 100, 60, 130, false],
    ["a clock in the future", 140, 100, 130, false],
  ])("%s", (_, seen, published, at, expected) => {
    expect(online(seen, published, at)).toBe(expected);
  });
});

describe("sending", () => {
  test("a message reaches the target node's inbox", async () => {
    const { mac, box } = await twoNodes();
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "run the tests" }) });
    const { messages } = await body<{ messages: { text: string }[] }>(call("/v1/inbox?wait=0", { token: box }));
    expect(messages.map((m) => m.text)).toEqual(["run the tests"]);
  });

  test("an @name reaches its session and names the sender with its node", async () => {
    const { mac, box } = await twoNodes();
    await call("/v1/messages", { method: "POST", token: mac, body: message("@worker") });
    const { messages } = await body<{ messages: { from: string }[] }>(call("/v1/inbox?wait=0", { token: box }));
    expect(messages[0].from).toBe("mac/claude:aaa");
  });

  test("a sender cannot claim another node's session", async () => {
    const { mac } = await twoNodes();
    expect((await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { from: "claude:bbb" }) })).status).toBe(403);
  });

  test.each([
    [{ id: "short" }, 400],
    [{ text: "" }, 400],
    [{ reply: "maybe" }, 400],
    [{ reply: null }, 400],
    [{ mode: "wake" }, 400],
    [{ to: "nowhere" }, 400],
    [{ to: "box/claude:nobody" }, 404],
    [{ to: "@nobody" }, 404],
    [{ attachments: ["0".repeat(32)] }, 400],
    [{ attachments: "x" }, 400],
  ])("%j is refused with %i", async (bad, status) => {
    const { mac } = await twoNodes();
    expect((await call("/v1/messages", { method: "POST", token: mac, body: { ...message("box/claude:bbb"), ...bad } })).status).toBe(status);
  });

  test("a malformed address names itself in Python's repr", async () => {
    const { mac } = await twoNodes();
    expect(await body(call("/v1/messages", { method: "POST", token: mac, body: message("nowhere") }))).toEqual({
      error: "a remote address is NODE/harness:id or @name, not 'nowhere'",
    });
  });

  test("a retried id is answered by the first receipt, marked duplicate", async () => {
    const { mac } = await twoNodes();
    const sent = message("box/claude:bbb");
    const first = await body<{ recipients: unknown }>(call("/v1/messages", { method: "POST", token: mac, body: sent }));
    const retry = await body(call("/v1/messages", { method: "POST", token: mac, body: sent }));
    expect(retry).toEqual({ ...first, duplicate: true });
  });

  test("a retried id is stored once", async () => {
    const { mac, box } = await twoNodes();
    const sent = message("box/claude:bbb");
    await call("/v1/messages", { method: "POST", token: mac, body: sent });
    await call("/v1/messages", { method: "POST", token: mac, body: sent });
    expect((await body<{ messages: unknown[] }>(call("/v1/inbox?wait=0", { token: box }))).messages).toHaveLength(1);
  });

  test("a reused id with another payload is a conflict", async () => {
    const { mac } = await twoNodes();
    const sent = message("box/claude:bbb");
    await call("/v1/messages", { method: "POST", token: mac, body: sent });
    expect((await call("/v1/messages", { method: "POST", token: mac, body: { ...sent, mode: "fyi" } })).status).toBe(409);
  });

  test("a body declared too large is refused before it is read", async () => {
    const { mac } = await twoNodes();
    expect((await call("/v1/messages", { method: "POST", token: mac, raw: "{}", headers: { "content-length": String(MAX_BODY + 1) } })).status).toBe(413);
  });

  test("a body that is not an object is refused", async () => {
    const { mac } = await twoNodes();
    expect((await call("/v1/messages", { method: "POST", token: mac, body: [1] })).status).toBe(400);
  });

  test("invalid JSON is refused", async () => {
    const { mac } = await twoNodes();
    expect(await body(call("/v1/messages", { method: "POST", token: mac, raw: "{", headers: { "content-type": "application/json" } }))).toEqual({
      error: "invalid JSON",
    });
  });
});

describe("delivery", () => {
  async function delivered(): Promise<{ mac: string; box: string; id: string; delivery: string }> {
    const { mac, box } = await twoNodes();
    const sent = message("box/claude:bbb");
    await call("/v1/messages", { method: "POST", token: mac, body: sent });
    const { messages } = await body<{ messages: { delivery: string }[] }>(call("/v1/inbox?wait=0", { token: box }));
    return { mac, box, id: sent.id as string, delivery: messages[0].delivery };
  }

  test("an unacknowledged delivery is handed out again", async () => {
    const { box, delivery } = await delivered();
    const { messages } = await body<{ messages: { delivery: string }[] }>(call("/v1/inbox?wait=0", { token: box }));
    expect(messages[0].delivery).toBe(delivery);
  });

  test("an acknowledged delivery leaves the inbox", async () => {
    const { box, delivery } = await delivered();
    await call("/v1/acks", { method: "POST", token: box, body: { delivery, outcome: "accepted" } });
    expect(await body(call("/v1/inbox?wait=0", { token: box }))).toEqual({ messages: [] });
  });

  test("only the receiving node reports an outcome", async () => {
    const { mac, delivery } = await delivered();
    expect((await call("/v1/acks", { method: "POST", token: mac, body: { delivery, outcome: "accepted" } })).status).toBe(404);
  });

  test("an unknown outcome is refused", async () => {
    const { box, delivery } = await delivered();
    expect((await call("/v1/acks", { method: "POST", token: box, body: { delivery, outcome: "read" } })).status).toBe(400);
  });

  test("the first reported outcome stands", async () => {
    const { box, delivery } = await delivered();
    await call("/v1/acks", { method: "POST", token: box, body: { delivery, outcome: "unconfirmed" } });
    expect(await body(call("/v1/acks", { method: "POST", token: box, body: { delivery, outcome: "accepted" } }))).toEqual({ delivery, outcome: "unconfirmed" });
  });

  test("the sender reads the reported outcome", async () => {
    const { mac, box, id, delivery } = await delivered();
    await call("/v1/acks", { method: "POST", token: box, body: { delivery, outcome: "refused" } });
    const status = await body<{ recipients: { outcome: string }[] }>(call(`/v1/messages/${id}`, { token: mac }));
    expect(status.recipients[0].outcome).toBe("refused");
  });

  test("another node cannot read a message's status", async () => {
    const { box, id } = await delivered();
    expect((await call(`/v1/messages/${id}`, { token: box })).status).toBe(404);
  });

  test("a long poll returns a message sent while it waits", async () => {
    const { mac, box } = await twoNodes();
    const polling = body<{ messages: unknown[] }>(call("/v1/inbox?wait=5", { token: box }));
    await new Promise((resolve) => setTimeout(resolve, 200));
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb") });
    expect((await polling).messages).toHaveLength(1);
  });

  test("an empty long poll ends at its wait", async () => {
    const { box } = await twoNodes();
    const started = Date.now();
    await call("/v1/inbox?wait=0.3", { token: box });
    expect(Date.now() - started).toBeGreaterThanOrEqual(250);
  });
});

describe("attachments", () => {
  async function uploaded(): Promise<{ mac: string; box: string; id: string }> {
    const { mac, box } = await twoNodes();
    const { id } = await body<{ id: string }>(call("/v1/attachments", { method: "POST", token: mac, raw: PNG, headers: { "content-type": "image/png" } }));
    return { mac, box, id };
  }

  test("an uploaded image is served back by its id", async () => {
    const { box, id } = await uploaded();
    expect(new Uint8Array(await (await call(`/v1/attachments/${id}`, { token: box })).arrayBuffer())).toEqual(PNG);
  });

  test("an uploaded image is served with its media type", async () => {
    const { box, id } = await uploaded();
    expect((await call(`/v1/attachments/${id}`, { token: box })).headers.get("content-type")).toBe("image/png");
  });

  test("a delivery carries the attachment's metadata", async () => {
    const { mac, box, id } = await uploaded();
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "", attachments: [id] }) });
    const { messages } = await body<{ messages: { attachments: unknown }[] }>(call("/v1/inbox?wait=0", { token: box }));
    expect(messages[0].attachments).toEqual([{ id, media_type: "image/png", size: PNG.length }]);
  });

  test("an upload that is not an image is refused", async () => {
    const { mac } = await twoNodes();
    expect((await call("/v1/attachments", { method: "POST", token: mac, raw: "%PDF", headers: { "content-type": "application/pdf" } })).status).toBe(415);
  });

  test("an empty image is refused", async () => {
    const { mac } = await twoNodes();
    expect((await call("/v1/attachments", { method: "POST", token: mac, raw: new Uint8Array(), headers: { "content-type": "image/png" } })).status).toBe(413);
  });

  test("an image over 10 MB is refused", async () => {
    const { mac } = await twoNodes();
    const big = new Uint8Array(10_000_001);
    expect((await call("/v1/attachments", { method: "POST", token: mac, raw: big, headers: { "content-type": "image/png" } })).status).toBe(413);
  });

  test("an unknown attachment is not found", async () => {
    const { mac } = await twoNodes();
    expect((await call(`/v1/attachments/${"f".repeat(32)}`, { token: mac })).status).toBe(404);
  });

  test("a malformed attachment id is refused", async () => {
    const { mac } = await twoNodes();
    expect((await call("/v1/attachments/zz", { token: mac })).status).toBe(400);
  });
});
