/** GET /v1/stream: deliveries pushed over a hibernatable WebSocket, with acks still over POST /v1/acks. */

import { abortAllDurableObjects, evictDurableObject, runInDurableObject } from "cloudflare:test";
import { env } from "cloudflare:workers";
import type { Env } from "../src/env";
import { beforeEach, describe, expect, test } from "vitest";
import { PING, PONG, REVOKED } from "../src/relay";
import { ADMIN, body, call, message, mint, twoNodes } from "./helpers";

beforeEach(async () => {
  // The body is read so no response stays in flight: evicting the relay waits for those to drain.
  await (await call("/admin/reset", { method: "POST", token: ADMIN })).text();
});

interface Delivery {
  delivery: string;
  id: string;
  text: string;
}

/** An open stream: frames are queued as they arrive and taken in order with next(). */
class Stream {
  private frames: string[] = [];
  private waiters: ((frame: string) => void)[] = [];
  readonly closed: Promise<{ code: number; reason: string }>;

  constructor(readonly socket: WebSocket) {
    socket.accept();
    socket.addEventListener("message", (event) => {
      const frame = typeof event.data === "string" ? event.data : new TextDecoder().decode(event.data as ArrayBuffer);
      const waiter = this.waiters.shift();
      if (waiter) waiter(frame);
      else this.frames.push(frame);
    });
    this.closed = new Promise((resolve) => socket.addEventListener("close", (event) => resolve({ code: event.code, reason: event.reason })));
  }

  next(): Promise<string> {
    const frame = this.frames.shift();
    return frame !== undefined ? Promise.resolve(frame) : new Promise((resolve) => this.waiters.push(resolve));
  }

  async delivery(): Promise<Delivery> {
    const { messages } = JSON.parse(await this.next()) as { messages: Delivery[] };
    return messages[0];
  }
}

async function open(token: string): Promise<Stream> {
  const response = await call("/v1/stream", { token, headers: { upgrade: "websocket" } });
  if (!response.webSocket) throw new Error(`no upgrade: ${response.status}`);
  return new Stream(response.webSocket);
}

// The bindings as the Worker sees them; `env` itself is typed with the generic runtime bindings.
const bindings = env as unknown as Env;
const relay = () => bindings.RELAY.get(bindings.RELAY.idFromName("relay"));

describe("opening a stream", () => {
  test("an upgrade without a token is refused", async () => {
    expect((await call("/v1/stream", { headers: { upgrade: "websocket" } })).status).toBe(401);
  });

  test("an upgrade with a revoked token is refused", async () => {
    const token = await mint("box");
    await call("/admin/tokens/box", { method: "DELETE", token: ADMIN });
    expect((await call("/v1/stream", { token, headers: { upgrade: "websocket" } })).status).toBe(401);
  });

  test("a plain GET is told to upgrade", async () => {
    expect((await call("/v1/stream", { token: await mint("box") })).status).toBe(426);
  });

  test("an accepted upgrade switches protocols", async () => {
    expect((await call("/v1/stream", { token: await mint("box"), headers: { upgrade: "websocket" } })).status).toBe(101);
  });
});

describe("deliveries", () => {
  test("a delivery pending before the stream opens is pushed on connect", async () => {
    const { mac, box } = await twoNodes();
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "queued" }) });
    expect((await (await open(box)).delivery()).text).toBe("queued");
  });

  test("a pushed delivery has the shape /v1/inbox hands out", async () => {
    const { mac, box } = await twoNodes();
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb") });
    const pushed = await (await open(box)).delivery();
    const { messages } = await body<{ messages: Delivery[] }>(call("/v1/inbox?wait=0", { token: box }));
    expect(pushed).toEqual(messages[0]);
  });

  test("a delivery created after the stream opened is pushed", async () => {
    const { mac, box } = await twoNodes();
    const stream = await open(box);
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "live" }) });
    expect((await stream.delivery()).text).toBe("live");
  });

  test("a request to the node's bridge is pushed like a message", async () => {
    const { mac, box } = await twoNodes();
    const stream = await open(box);
    await call("/v1/messages", { method: "POST", token: mac, body: message("box", { mode: "request", text: '{"kind":"probe"}' }) });
    expect((await stream.delivery()).text).toBe('{"kind":"probe"}');
  });

  test("every stream the node holds gets the delivery, as every poller would", async () => {
    const { mac, box } = await twoNodes();
    const [first, second] = [await open(box), await open(box)];
    const sent = message("box/claude:bbb");
    await call("/v1/messages", { method: "POST", token: mac, body: sent });
    expect([(await first.delivery()).id, (await second.delivery()).id]).toEqual([sent.id, sent.id]);
  });

  test("a delivery for another node is not pushed", async () => {
    const { mac, box } = await twoNodes();
    const stream = await open(box);
    await call("/v1/messages", { method: "POST", token: box, body: message("mac/claude:aaa", { from: "claude:bbb", text: "to mac" }) });
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "to box" }) });
    expect((await stream.delivery()).text).toBe("to box");
  });

  test("an acknowledged delivery is not pushed again on reconnect", async () => {
    const { mac, box } = await twoNodes();
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "first" }) });
    const first = await (await open(box)).delivery();
    await call("/v1/acks", { method: "POST", token: box, body: { delivery: first.delivery, outcome: "accepted" } });
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "second" }) });
    expect((await (await open(box)).delivery()).text).toBe("second");
  });

  test("an unacknowledged delivery is pushed again on reconnect", async () => {
    const { mac, box } = await twoNodes();
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb") });
    const first = await open(box);
    const handed = await first.delivery();
    first.socket.close(1000, "bridge stopped");
    expect((await (await open(box)).delivery()).delivery).toBe(handed.delivery);
  });
});

describe("keepalive and revocation", () => {
  test("a ping is answered with a pong", async () => {
    const stream = await open(await mint("box"));
    stream.socket.send(PING);
    expect(await stream.next()).toBe(PONG);
  });

  test("revoking the token closes the node's stream", async () => {
    const stream = await open(await mint("box"));
    await call("/admin/tokens/box", { method: "DELETE", token: ADMIN });
    expect((await stream.closed).code).toBe(REVOKED);
  });

  test("replacing the token closes the stream opened with the old one", async () => {
    const stream = await open(await mint("box"));
    await mint("box");
    expect((await stream.closed).code).toBe(REVOKED);
  });

  test("revoking another node's token leaves the stream open", async () => {
    const { box } = await twoNodes();
    const stream = await open(box);
    await call("/admin/tokens/mac", { method: "DELETE", token: ADMIN });
    await call("/v1/messages", { method: "POST", token: box, body: message("box/claude:bbb", { from: "claude:bbb", text: "still open" }) });
    expect((await stream.delivery()).text).toBe("still open");
  });
});

describe("hibernation", () => {
  // Eviction waits for in-flight responses to drain, and earlier tests leave some unread.
  beforeEach(async () => {
    await abortAllDurableObjects();
    await (await call("/admin/reset", { method: "POST", token: ADMIN })).text();
  });

  test("the socket is tagged with its node", async () => {
    await open(await mint("box"));
    expect(await runInDurableObject(relay(), (_, state) => state.getWebSockets("box").length)).toBe(1);
  });

  test("the socket's attachment names its node", async () => {
    await open(await mint("box"));
    expect(await runInDurableObject(relay(), (_, state) => state.getWebSockets("box")[0].deserializeAttachment())).toEqual({ node: "box" });
  });

  test("a delivery reaches a stream opened before the object was evicted", async () => {
    const { mac, box } = await twoNodes();
    const stream = await open(box);
    await evictDurableObject(relay());
    await call("/v1/messages", { method: "POST", token: mac, body: message("box/claude:bbb", { text: "after sleep" }) });
    expect((await stream.delivery()).text).toBe("after sleep");
  });

  test("a revocation closes a stream opened before the object was evicted", async () => {
    const stream = await open(await mint("box"));
    await evictDurableObject(relay());
    await call("/admin/tokens/box", { method: "DELETE", token: ADMIN });
    expect((await stream.closed).code).toBe(REVOKED);
  });
});
