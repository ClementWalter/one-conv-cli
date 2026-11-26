/** The board (/v1/board): topics, posts, files in R2, deletes and search, through the Worker's own fetch handler. */

import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, test } from "vitest";
import { contentType, fileName, parseReads } from "../src/board";
import type { Env } from "../src/env";
import { ADMIN, body, call, twoNodes } from "./helpers";

// The bindings as the Worker sees them; `env` itself is typed with the generic runtime bindings.
const bindings = env as unknown as Env;

const BYTES = new Uint8Array([0, 1, 2, 250, 251, 252]);

beforeEach(async () => {
  await call("/admin/reset", { method: "POST", token: ADMIN });
});

function post(token: string, topic: string, extra: Record<string, unknown> = {}): Promise<Response> {
  return call(`/v1/board/${topic}`, { method: "POST", token, body: { id: crypto.randomUUID().replace(/-/g, ""), from: "claude:aaa", text: "a finding", ...extra } });
}

function upload(token: string, topic: string, data: Uint8Array = BYTES, name = "report.bin"): Promise<Response> {
  return call(`/v1/board/${topic}/files`, { method: "POST", token, raw: data, headers: { "content-type": "application/octet-stream", "x-file-name": encodeURIComponent(name) } });
}

async function hex(data: Uint8Array): Promise<string> {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", data))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** mac posted BYTES as report.bin to `release`; returns the tokens and the file id. */
async function shared(): Promise<{ mac: string; box: string; file: string }> {
  const { mac, box } = await twoNodes();
  const file = (await body<{ id: string }>(upload(mac, "release"))).id;
  await (await post(mac, "release", { files: [file] })).text();
  return { mac, box, file };
}

describe("posts", () => {
  test("another node reads a post", async () => {
    const { mac, box } = await twoNodes();
    await (await post(mac, "release", { text: "ship monday" })).text();
    expect((await body<{ posts: { text: string }[] }>(call("/v1/board/release", { token: box }))).posts.map((p) => p.text)).toEqual(["ship monday"]);
  });

  test("a post names its node and author", async () => {
    const { mac, box } = await twoNodes();
    await (await post(mac, "release")).text();
    expect((await body<{ posts: { from: string }[] }>(call("/v1/board/release", { token: box }))).posts[0].from).toBe("mac/claude:aaa");
  });

  test("unread counts posts after the reader's position", async () => {
    const { mac } = await twoNodes();
    const first = await body<{ seq: number }>(post(mac, "release"));
    await (await post(mac, "release")).text();
    expect((await body<{ topics: { unread: number }[] }>(call(`/v1/board?read=release:${first.seq}`, { token: mac }))).topics[0].unread).toBe(1);
  });

  test("a retry under the same id is a duplicate", async () => {
    const { mac } = await twoNodes();
    const id = "a".repeat(32);
    await (await post(mac, "release", { id })).text();
    expect((await body<{ duplicate: boolean }>(post(mac, "release", { id }))).duplicate).toBe(true);
  });

  test("text over 32 KB is refused", async () => {
    const { mac } = await twoNodes();
    expect((await post(mac, "release", { text: "x".repeat(32 * 1024 + 1) })).status).toBe(413);
  });

  test("a lone surrogate is refused", async () => {
    const { mac } = await twoNodes();
    expect((await post(mac, "release", { text: "broken \ud800" })).status).toBe(400);
  });
});

describe("files", () => {
  test("a file downloads byte-identical", async () => {
    const { box, file } = await shared();
    expect(new Uint8Array(await (await call(`/v1/board/release/files/${file}`, { token: box })).arrayBuffer())).toEqual(BYTES);
  });

  test("a download carries the file's sha256", async () => {
    const { box, file } = await shared();
    expect((await call(`/v1/board/release/files/${file}`, { token: box })).headers.get("x-content-sha256")).toBe(await hex(BYTES));
  });

  test("a download is an attachment, never rendered", async () => {
    const { box, file } = await shared();
    expect((await call(`/v1/board/release/files/${file}`, { token: box })).headers.get("content-disposition")).toBe("attachment; filename*=UTF-8''report.bin");
  });

  test("the bytes live in R2 under board/<topic>/<id>", async () => {
    const { file } = await shared();
    expect(await bindings.ARCHIVE.head(`board/release/${file}`)).not.toBeNull();
  });

  test("a reset empties the board's R2 objects", async () => {
    await shared();
    await call("/admin/reset", { method: "POST", token: ADMIN });
    expect((await bindings.ARCHIVE.list({ prefix: "board/" })).objects).toEqual([]);
  });

  test("a download needs a node token", async () => {
    const { file } = await shared();
    expect((await call(`/v1/board/release/files/${file}`)).status).toBe(401);
  });
});

describe("deletes", () => {
  test("another node cannot delete a post", async () => {
    const { mac, box } = await twoNodes();
    const made = await body<{ id: string }>(post(mac, "release"));
    expect((await call(`/v1/board/release/posts/${made.id}`, { method: "DELETE", token: box })).status).toBe(403);
  });

  test("the admin token deletes any post", async () => {
    const { mac } = await twoNodes();
    const made = await body<{ id: string }>(post(mac, "release"));
    expect((await call(`/v1/board/release/posts/${made.id}`, { method: "DELETE", token: ADMIN })).status).toBe(200);
  });

  test("an admin delete hides the post", async () => {
    const { mac } = await twoNodes();
    const made = await body<{ id: string }>(post(mac, "release"));
    await (await call(`/v1/board/release/posts/${made.id}`, { method: "DELETE", token: ADMIN })).text();
    expect((await body<{ posts: unknown[] }>(call("/v1/board/release", { token: mac }))).posts).toEqual([]);
  });

  test("deleting the last post drops the topic from the listing", async () => {
    const { mac } = await twoNodes();
    const made = await body<{ id: string }>(post(mac, "release"));
    await (await call(`/v1/board/release/posts/${made.id}`, { method: "DELETE", token: mac })).text();
    expect((await body<{ topics: unknown[] }>(call("/v1/board", { token: mac }))).topics).toEqual([]);
  });

  test("posting to an emptied topic lists it again", async () => {
    const { mac } = await twoNodes();
    const made = await body<{ id: string }>(post(mac, "release"));
    await (await call(`/v1/board/release/posts/${made.id}`, { method: "DELETE", token: mac })).text();
    await (await post(mac, "release", { text: "back" })).text();
    expect((await body<{ topics: { topic: string }[] }>(call("/v1/board", { token: mac }))).topics.map((t) => t.topic)).toEqual(["release"]);
  });

  test("an admin delete of an unknown post is not found", async () => {
    await twoNodes();
    expect((await call(`/v1/board/release/posts/${"0".repeat(32)}`, { method: "DELETE", token: ADMIN })).status).toBe(404);
  });
});

describe("search", () => {
  test("an all-words search matches in any order", async () => {
    const { mac } = await twoNodes();
    await (await post(mac, "release", { text: "lock the migration" })).text();
    expect((await body<{ posts: unknown[] }>(call("/v1/board/search?q=migration%20lock&words=1", { token: mac }))).posts.length).toBe(1);
  });
});

describe("helpers", () => {
  test.each([
    ["a percent-encoded name decodes", "r%C3%A9sum%C3%A9.txt", "résumé.txt"],
    ["a plain name stays", "notes.md", "notes.md"],
  ])("fileName: %s", (_, raw, expected) => {
    expect(fileName(raw)).toBe(expected);
  });

  test.each([["a slash", "a%2Fb"], ["dot-dot", ".."], ["empty", ""], ["malformed escape", "%E0%A4%A"], ["a control character", "a%0Ab"]])("fileName refuses %s", (_, raw) => {
    expect(() => fileName(raw)).toThrow();
  });

  test.each([
    ["a media type is kept", "text/markdown; charset=utf-8", "text/markdown; charset=utf-8"],
    ["no media type is a byte stream", "", "application/octet-stream"],
    ["a malformed one is a byte stream", "markdown", "application/octet-stream"],
  ])("contentType: %s", (_, raw, expected) => {
    expect(contentType(raw)).toBe(expected);
  });

  test("parseReads maps each topic to its seq", () => {
    expect([...parseReads(["release:4", "infra:0"])]).toEqual([
      ["release", 4],
      ["infra", 0],
    ]);
  });

  test("parseReads refuses a value without a seq", () => {
    expect(() => parseReads(["release"])).toThrow();
  });
});
