/** The archive API (/archive/v1): the rules of archive/server.py and archive/store.py. */

import { beforeEach, describe, expect, test } from "vitest";
import { decompress } from "../src/archive";
import { OVERLAP, searchParts } from "../src/archive-index";
import capture from "./fixtures/capture.json";
import { ADMIN, b64, body, call, deflate, mint } from "./helpers";

beforeEach(async () => {
  await call("/admin/reset", { method: "POST", token: ADMIN });
});

const commit = JSON.parse(capture.commit) as { manifest: Record<string, unknown>; expected: string | null };

async function upload(token: string, chunks: Record<string, string> = capture.chunks): Promise<Response> {
  return call("/archive/v1/chunks", { method: "POST", token, body: { chunks } });
}

/** The fixture capture committed by node `mac`; returns mac's token. */
async function committed(): Promise<string> {
  const mac = await mint("mac");
  await upload(mac);
  await call("/archive/v1/conversations", { method: "POST", token: mac, raw: capture.commit });
  return mac;
}

describe("authentication", () => {
  test("a request without a token is refused", async () => {
    expect(await body(call("/archive/v1/conversations"))).toEqual({ error: "Unauthorized" });
  });

  test("a node token reads the archive", async () => {
    expect(await body(call("/archive/v1/conversations", { token: await mint("mac") }))).toEqual({ items: [], next: null });
  });

  test("an unknown archive path is a 404 without a token", async () => {
    expect((await call("/archive/v1/nowhere")).status).toBe(404);
  });
});

describe("committing a capture", () => {
  test("its key is the one Python computes", async () => {
    const mac = await mint("mac");
    await upload(mac);
    expect((await body<{ key: string }>(call("/archive/v1/conversations", { method: "POST", token: mac, raw: capture.commit }))).key).toBe(capture.conversation);
  });

  test("its revision is the one Python computes", async () => {
    const mac = await mint("mac");
    await upload(mac);
    expect((await body<{ revision: string }>(call("/archive/v1/conversations", { method: "POST", token: mac, raw: capture.commit }))).revision).toBe(capture.revision);
  });

  test("the manifest reads back byte for byte as Python encodes it", async () => {
    const mac = await committed();
    const text = await (await call(`/archive/v1/conversations/${capture.conversation}`, { token: mac, headers: { "accept-encoding": "identity" } })).text();
    expect(text).toBe(capture.commit.slice('{"manifest":'.length, -',"expected":null}'.length));
  });

  test("a manifest before its chunks is a conflict", async () => {
    const mac = await mint("mac");
    expect((await call("/archive/v1/conversations", { method: "POST", token: mac, raw: capture.commit })).status).toBe(409);
  });

  test("a node cannot commit under another node's origin", async () => {
    const box = await mint("box");
    await upload(box);
    expect((await call("/archive/v1/conversations", { method: "POST", token: box, raw: capture.commit })).status).toBe(403);
  });

  test("the admin token commits under any origin", async () => {
    await upload(ADMIN);
    expect((await call("/archive/v1/conversations", { method: "POST", token: ADMIN, raw: capture.commit })).status).toBe(200);
  });

  test("the same revision again is accepted whatever was expected", async () => {
    const mac = await committed();
    // Textual edit: re-serializing in JS would turn the float 1767225600.0 into an int and change the revision.
    const again = capture.commit.replace('"expected":null', `"expected":"${"0".repeat(64)}"`);
    expect((await call("/archive/v1/conversations", { method: "POST", token: mac, raw: again })).status).toBe(200);
  });

  test("another revision expecting a stale head is a conflict", async () => {
    const mac = await committed();
    const stale = JSON.stringify({ manifest: { ...commit.manifest, mtime: 1767225601.5 }, expected: null });
    expect((await call("/archive/v1/conversations", { method: "POST", token: mac, raw: stale })).status).toBe(409);
  });

  test("another revision expecting the head advances it", async () => {
    const mac = await committed();
    const next = JSON.stringify({ manifest: { ...commit.manifest, mtime: 1767225601.5 }, expected: capture.revision });
    await call("/archive/v1/conversations", { method: "POST", token: mac, raw: next });
    const { items } = await body<{ items: { mtime: number }[] }>(call("/archive/v1/conversations", { token: mac }));
    expect(items[0].mtime).toBe(1767225601.5);
  });

  test("an earlier revision stays readable", async () => {
    const mac = await committed();
    const next = JSON.stringify({ manifest: { ...commit.manifest, mtime: 1767225601.5 }, expected: capture.revision });
    await call("/archive/v1/conversations", { method: "POST", token: mac, raw: next });
    const old = await body<{ mtime: number }>(call(`/archive/v1/conversations/${capture.conversation}?revision=${capture.revision}`, { token: mac }));
    expect(old.mtime).toBe(1767225600);
  });

  test.each([
    ["an extra field", { ...commit.manifest, extra: 1 }],
    ["a missing field", { ...commit.manifest, cwd: undefined }],
    ["an empty origin", { ...commit.manifest, origin: "" }],
    ["a summary of another session", { ...commit.manifest, summary: { ...(commit.manifest.summary as object), uuid: "other" } }],
    ["fields of another protocol", { ...commit.manifest, fields: { turns: [] } }],
    ["a malformed digest", { ...commit.manifest, originals: { a: ["XYZ"] } }],
  ])("%s is invalid", async (_, manifest) => {
    expect((await call("/archive/v1/conversations", { method: "POST", token: ADMIN, body: { manifest, expected: null } })).status).toBe(422);
  });

  test("a body that is not JSON is invalid", async () => {
    expect((await call("/archive/v1/conversations", { method: "POST", token: ADMIN, raw: "{" })).status).toBe(422);
  });
});

describe("chunks", () => {
  test("the missing check lists what is not stored, in order", async () => {
    const mac = await mint("mac");
    const [first, second] = Object.keys(capture.chunks);
    await upload(mac, { [first]: capture.chunks[first as keyof typeof capture.chunks] });
    expect(await body(call("/archive/v1/chunks/missing", { method: "POST", token: mac, body: { digests: [second, first, second] } }))).toEqual({ missing: [second, second] });
  });

  test("an upload counts what it stored", async () => {
    expect(await body(upload(await mint("mac")))).toEqual({ stored: Object.keys(capture.chunks).length });
  });

  test("a stored chunk reads back as its compressed bytes", async () => {
    const mac = await mint("mac");
    await upload(mac);
    const [name, encoded] = Object.entries(capture.chunks)[0];
    const data = new Uint8Array(await (await call(`/archive/v1/chunks/${name}`, { token: mac, headers: { "accept-encoding": "identity" } })).arrayBuffer());
    expect(b64(data)).toBe(encoded);
  });

  test("an unknown chunk is not found", async () => {
    expect((await call(`/archive/v1/chunks/${"0".repeat(64)}`, { token: await mint("mac") })).status).toBe(404);
  });

  test("a chunk under another digest is invalid", async () => {
    const [, encoded] = Object.entries(capture.chunks)[0];
    expect((await upload(await mint("mac"), { ["0".repeat(64)]: encoded })).status).toBe(422);
  });

  test("a chunk that is not base64 is invalid", async () => {
    expect((await upload(await mint("mac"), { ["0".repeat(64)]: "not base64!" })).status).toBe(422);
  });

  test("an invalid chunk stores nothing from its batch", async () => {
    const mac = await mint("mac");
    const [name, encoded] = Object.entries(capture.chunks)[0];
    await upload(mac, { [name]: encoded, ["0".repeat(64)]: encoded });
    expect(await body(call("/archive/v1/chunks/missing", { method: "POST", token: mac, body: { digests: [name] } }))).toEqual({ missing: [name] });
  });

  test("a body over the archive's limit is refused", async () => {
    const huge = "x".repeat(16 * 1024 * 1024 + 1);
    expect((await call("/archive/v1/chunks", { method: "POST", token: await mint("mac"), raw: huge })).status).toBe(413);
  });
});

describe("decompress verifies like Python's", () => {
  const raw = new TextEncoder().encode("Quartz");
  const digest = "6dbfeeb207b36340969e7258f1662ee6f3dd81c7dd9520cd7f12e16f8fa3989e"; // sha256(b"Quartz")

  test("a complete stream of the digest's content is accepted", async () => {
    expect(await decompress(await deflate(raw), digest)).toEqual(raw);
  });

  test("trailing bytes after the stream are refused", async () => {
    const data = await deflate(raw);
    await expect(decompress(new Uint8Array([...data, 0]), digest)).rejects.toThrow("Chunk is not valid zlib data");
  });

  test("a truncated stream is refused", async () => {
    const data = await deflate(raw);
    await expect(decompress(data.slice(0, -2), digest)).rejects.toThrow("Chunk is not valid zlib data");
  });

  test("more than one MiB of raw content is refused", async () => {
    const data = await deflate(new Uint8Array(1024 * 1024 + 1));
    await expect(decompress(data, digest)).rejects.toThrow("Chunk does not match its digest");
  });
});

describe("listing", () => {
  test("rows carry the metadata, key and revision", async () => {
    const mac = await committed();
    const { items } = await body<{ items: Record<string, unknown>[] }>(call("/archive/v1/conversations", { token: mac }));
    expect([items[0].origin, items[0].key, items[0].revision]).toEqual(["mac", capture.conversation, capture.revision]);
  });

  test("rows leave out the chunk lists", async () => {
    const mac = await committed();
    const { items } = await body<{ items: Record<string, unknown>[] }>(call("/archive/v1/conversations", { token: mac }));
    expect("fields" in items[0]).toBe(false);
  });

  test("a query matches the lowercased search text", async () => {
    const mac = await committed();
    const { items } = await body<{ items: unknown[] }>(call(`/archive/v1/conversations?query=${encodeURIComponent("CAFÉ")}`, { token: mac }));
    expect(items).toHaveLength(1);
  });

  test("a query matching nothing lists nothing", async () => {
    const mac = await committed();
    expect(await body(call("/archive/v1/conversations?query=absent", { token: mac }))).toEqual({ items: [], next: null });
  });

  test("rows after the last key are not listed", async () => {
    const mac = await committed();
    expect((await body<{ items: unknown[] }>(call(`/archive/v1/conversations?after=${capture.conversation}`, { token: mac }))).items).toEqual([]);
  });

  test("a large listing is gzipped for a client that accepts it", async () => {
    const mac = await committed();
    const response = await call("/archive/v1/conversations", { token: mac, headers: { "accept-encoding": "gzip" } });
    expect(response.headers.get("content-encoding")).toBe("gzip");
  });

  test("changes since zero list the commit with the sequence watermark", async () => {
    const mac = await committed();
    const changes = await body<{ items: unknown[]; next: unknown; sequence: number }>(call("/archive/v1/conversations?since=0", { token: mac }));
    expect([changes.items.length, changes.next, changes.sequence]).toEqual([1, null, 1]);
  });

  test.each([["-1"], ["1.5"], ["9223372036854775808"], ["0&after=a"], ["0&query=a"]])("since=%s is invalid", async (since) => {
    expect((await call(`/archive/v1/conversations?since=${since}`, { token: await mint("mac") })).status).toBe(422);
  });

  test("an unknown conversation is not found", async () => {
    expect((await call("/archive/v1/conversations/nothing", { token: await mint("mac") })).status).toBe(404);
  });
});

describe("search parts", () => {
  const text = "ab".repeat(600_000);

  test("join back to the text", () => {
    expect(searchParts(text).map(([part]) => part).join("")).toBe(text);
  });

  test("each tail repeats what follows", () => {
    const parts = searchParts(text);
    expect(parts[0][1]).toBe(parts[1][0].slice(0, OVERLAP));
  });

  test("an empty text is one empty part", () => {
    expect(searchParts("")).toEqual([["", ""]]);
  });
});
