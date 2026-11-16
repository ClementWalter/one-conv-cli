/**
 * The conversation archive's HTTP API (`archive/server.py`), served under /archive.
 *
 * Every node reads every capture and commits only captures whose origin is its own name; the
 * admin token commits under any origin, like the standalone server's shared token. Chunks are
 * the zlib-compressed bytes the client sends, stored in R2 as `chunks/<sha256 of the raw bytes>`
 * once their digest is verified. Manifests are hashed exactly as Python encodes them, so a
 * revision computed here equals the one the client computes.
 */

import type { ArchiveIndex, PutResult } from "./archive-index";
import type { Env } from "./env";
import { HttpError, type Route, boundedBody, dispatch, errorResponse, path, query, rawJson, sha256, utf8 } from "./http";
import { PyJsonError, PyNumber, PyObject, type PyValue, encode, parse } from "./pyjson";

export const CHUNK_BYTES = 1024 * 1024;
export const MAX_REQUEST_BYTES = 16 * 1024 * 1024;
export const PAGE_SIZE = 1000;
const FIELDS = ["turns", "turns_with_subagents", "search_text"];
const DIGEST = /^[0-9a-f]{64}$/;
// Python's listing, chunk and manifest responses are gzipped from this size when the client accepts it.
const GZIP_MINIMUM = 1000;
const MAX_SQLITE_INTEGER = 2n ** 63n - 1n;

/** A request the archive refuses as invalid (HTTP 422), as pydantic and ArchiveError are in Python. */
export class Invalid extends Error {}

export interface Caller {
  /** "" for the admin token (any origin), else the node whose token was presented. */
  node: string;
}

type Context = { env: Env; caller: Caller; index: DurableObjectStub<ArchiveIndex>; url: URL };

/** The raw bytes of a zlib chunk, refused unless it inflates to at most CHUNK_BYTES whose SHA-256 is `digest`. */
export async function decompress(data: Uint8Array, digest: string): Promise<Uint8Array> {
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    const reader = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate")).getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > CHUNK_BYTES) {
        await reader.cancel();
        throw new Invalid("Chunk does not match its digest");
      }
      parts.push(value);
    }
  } catch (error) {
    if (error instanceof Invalid) throw error;
    throw new Invalid("Chunk is not valid zlib data");
  }
  const raw = concat(parts);
  if ((await sha256(raw)) !== digest) throw new Invalid("Chunk does not match its digest");
  return raw;
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((size, part) => size + part.byteLength, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.byteLength;
  }
  return out;
}

/** `base64.b64decode(text, validate=True)`: only the base64 alphabet, correctly padded. */
export function base64(text: string): Uint8Array {
  if (text.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(text)) throw new Invalid("Chunk is not base64");
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) out[index] = binary.charCodeAt(index);
  return out;
}

// --- validation, as the pydantic models of archive/server.py declare it -------------------------

function object(value: PyValue | undefined, where: string, allowed: string[], required: string[]): PyObject {
  if (!(value instanceof PyObject)) throw new Invalid(`${where}: Input should be an object`);
  for (const key of value.keys()) if (!allowed.includes(key)) throw new Invalid(`${where}.${key}: Extra inputs are not permitted`);
  for (const key of required) if (!value.has(key)) throw new Invalid(`${where}.${key}: Field required`);
  return value;
}

function text(value: PyValue | undefined, where: string, minLength = 0, nullable = false): string | null {
  if (value === null && nullable) return null;
  if (typeof value !== "string") throw new Invalid(`${where}: Input should be a valid string`);
  if (value.length < minLength) throw new Invalid(`${where}: String should have at least ${minLength} character`);
  return value;
}

function finite(value: PyValue | undefined, where: string, nullable = false): void {
  if (value === null && nullable) return;
  if (!(value instanceof PyNumber) || !Number.isFinite(value.value)) throw new Invalid(`${where}: Input should be a finite number`);
}

function digestLists(value: PyValue | undefined, where: string): Map<string, string[]> {
  if (!(value instanceof PyObject)) throw new Invalid(`${where}: Input should be a valid dictionary`);
  const out = new Map<string, string[]>();
  for (const [name, parts] of value) {
    if (!Array.isArray(parts) || !parts.every((part) => typeof part === "string")) throw new Invalid(`${where}.${name}: Input should be a list of strings`);
    out.set(name, parts as string[]);
  }
  return out;
}

const MANIFEST_FIELDS = ["origin", "source", "cwd", "session", "locator", "mtime", "capture_version", "account_label", "summary", "unread_native", "freed", "fields", "originals"];

export interface Manifest {
  value: PyObject;
  origin: string;
  digests: string[];
  searchText: string[];
}

/** The `Commit` model: `{"manifest": Manifest, "expected": str | None}`. */
export function validateCommit(body: PyValue): { manifest: Manifest; expected: string | null } {
  const commit = object(body, "commit", ["manifest", "expected"], ["manifest", "expected"]);
  const value = object(commit.get("manifest"), "manifest", MANIFEST_FIELDS, MANIFEST_FIELDS.filter((name) => name !== "freed"));
  const origin = text(value.get("origin"), "manifest.origin", 1)!;
  const source = text(value.get("source"), "manifest.source", 1);
  const cwd = text(value.get("cwd"), "manifest.cwd");
  const session = text(value.get("session"), "manifest.session", 1);
  text(value.get("locator"), "manifest.locator", 1);
  finite(value.get("mtime"), "manifest.mtime");
  text(value.get("capture_version"), "manifest.capture_version", 1);
  text(value.get("account_label"), "manifest.account_label", 0, true);
  const summary = value.get("summary");
  if (!(summary instanceof PyObject)) throw new Invalid("manifest.summary: Input should be a valid dictionary");
  const unread = value.get("unread_native");
  if (unread !== null && typeof unread !== "boolean") throw new Invalid("manifest.unread_native: Input should be a valid boolean");
  if (value.has("freed")) finite(value.get("freed"), "manifest.freed", true);
  const fields = digestLists(value.get("fields"), "manifest.fields");
  const originals = digestLists(value.get("originals"), "manifest.originals");
  if (summary.get("uuid") !== session || summary.get("source") !== source || summary.get("cwd") !== cwd) {
    throw new Invalid("manifest: Value error, Summary identity differs from capture");
  }
  if (fields.size !== FIELDS.length || !FIELDS.every((name) => fields.has(name))) throw new Invalid("manifest: Value error, Capture fields differ from this protocol");
  const digests = [...fields.values(), ...originals.values()].flat();
  if (!digests.every((digest) => DIGEST.test(digest))) throw new Invalid("manifest: Value error, Invalid chunk digest");
  const expected = text(commit.get("expected"), "commit.expected", 0, true);
  return { manifest: { value, origin, digests, searchText: fields.get("search_text")! }, expected };
}

function inventory(body: PyValue): string[] {
  const value = object(body, "inventory", ["digests"], ["digests"]).get("digests");
  if (!Array.isArray(value) || !value.every((digest) => typeof digest === "string")) throw new Invalid("digests: Input should be a list of strings");
  return value as string[];
}

function chunkMap(body: PyValue): [string, string][] {
  const value = object(body, "chunks", ["chunks"], ["chunks"]).get("chunks");
  if (!(value instanceof PyObject)) throw new Invalid("chunks: Input should be a valid dictionary");
  return [...value].map(([name, encoded]) => {
    if (typeof encoded !== "string") throw new Invalid(`chunks.${name}: Input should be a valid string`);
    return [name, encoded];
  });
}

function parseBody(bytes: Uint8Array): PyValue {
  try {
    return parse(utf8(bytes));
  } catch (error) {
    if (error instanceof PyJsonError || error instanceof TypeError) throw new Invalid(`Invalid JSON: ${(error as Error).message}`);
    throw error;
  }
}

function body(request: Request): Promise<Uint8Array> {
  return boundedBody(request, MAX_REQUEST_BYTES, new HttpError(413, "Request exceeds the archive's size limit"), true);
}

// --- endpoints -----------------------------------------------------------------------------------

/** The capture's search text as the Python store keeps it: its lines joined, lowercased. */
async function searchText(env: Env, digests: string[]): Promise<string> {
  const parts = await Promise.all(
    digests.map(async (digest) => {
      const object = await env.ARCHIVE.get(`chunks/${digest}`);
      if (!object) throw new Invalid("Chunk does not match its digest");
      return decompress(new Uint8Array(await object.arrayBuffer()), digest);
    }),
  );
  let lines: unknown;
  try {
    lines = JSON.parse(utf8(concat(parts)));
  } catch {
    throw new Invalid("search_text is not JSON");
  }
  const list = typeof lines === "string" ? [...lines] : lines;
  if (!Array.isArray(list) || !list.every((line) => typeof line === "string")) throw new Invalid("search_text is not a list of strings");
  return list.join("\n").toLowerCase();
}

async function commit(request: Request, context: Context): Promise<Response> {
  const parsed = parseBody(await body(request));
  const { manifest, expected } = validateCommit(parsed);
  const { node } = context.caller;
  if (node && manifest.origin !== node) throw new HttpError(403, `Node ${node} uploads only captures whose origin is ${node}.`);
  if ((await context.index.missingChunks(manifest.digests)).length) throw new HttpError(409, "Upload the capture's chunks before its manifest.");
  const search = await searchText(context.env, manifest.searchText);
  const value = manifest.value;
  const metadata = new PyObject([...value].filter(([name]) => name !== "fields" && name !== "originals"));
  const identity = ["origin", "source", "cwd", "session", "locator"].map((name) => value.get(name)!);
  const key = await sha256(encode(identity, true));
  const revision = await sha256(encode(value, true));
  const result: PutResult = await context.index.put(
    { key, revision, mtime: (value.get("mtime") as PyNumber).value, metadata: encode(metadata), search_text: search },
    encode(value),
    expected,
  );
  if ("conflict" in result) throw new HttpError(409, result.conflict);
  return rawJson(JSON.stringify(result));
}

async function conversations(request: Request, _: Record<string, string>, context: Context): Promise<Response> {
  if (request.method === "POST") return commit(request, context);
  const { url } = context;
  const since = query(url, "since");
  if (since !== null) {
    if (!/^[0-9]{1,19}$/.test(since) || BigInt(since) > MAX_SQLITE_INTEGER || url.searchParams.has("query") || url.searchParams.has("after")) {
      throw new Invalid("since must be a non-negative SQLite integer, without query or after");
    }
    return rawJson(await context.index.listChanges(Number(since), PAGE_SIZE));
  }
  return rawJson(await context.index.listConversations(query(url, "after") ?? "", query(url, "query")?.toLowerCase() ?? null, PAGE_SIZE));
}

async function conversation(_: Request, params: Record<string, string>, context: Context): Promise<Response> {
  const manifest = await context.index.readConversation(params.key, query(context.url, "revision"));
  return manifest ? rawJson(manifest) : errorResponse(404, "Not found");
}

async function missing(request: Request, _: Record<string, string>, context: Context): Promise<Response> {
  const digests = inventory(parseBody(await body(request)));
  return rawJson(JSON.stringify({ missing: await context.index.missingChunks(digests) }));
}

async function upload(request: Request, _: Record<string, string>, context: Context): Promise<Response> {
  const received: [string, Uint8Array][] = [];
  for (const [name, encoded] of chunkMap(parseBody(await body(request)))) {
    const data = base64(encoded);
    await decompress(data, name); // Verifies the digest of the raw content.
    received.push([name, data]);
  }
  // Bodies first: the index never lists a chunk R2 does not hold.
  await Promise.all(received.map(([name, data]) => context.env.ARCHIVE.put(`chunks/${name}`, data)));
  await context.index.addChunks(received.map(([name, data]) => [name, data.byteLength]));
  return rawJson(JSON.stringify({ stored: received.length }));
}

async function chunk(_: Request, params: Record<string, string>, context: Context): Promise<Response> {
  const object = await context.env.ARCHIVE.get(`chunks/${params.digest}`);
  if (!object) return errorResponse(404, "Not found");
  return new Response(object.body, { headers: { "content-type": "application/octet-stream", "content-length": String(object.size) } });
}

const ROUTES: Route<Context>[] = [
  { pattern: path("/v1/conversations"), methods: ["GET", "POST"], handle: conversations },
  { pattern: path("/v1/conversations/{key}"), methods: ["GET"], handle: conversation },
  { pattern: path("/v1/chunks"), methods: ["POST"], handle: upload },
  { pattern: path("/v1/chunks/missing"), methods: ["POST"], handle: missing },
  { pattern: path("/v1/chunks/{digest}"), methods: ["GET"], handle: chunk },
];

/** Gzip a response of at least GZIP_MINIMUM bytes for a client that accepts it, as Starlette's GZipMiddleware does. */
async function negotiated(request: Request, response: Response): Promise<Response> {
  if (!(request.headers.get("accept-encoding") ?? "").includes("gzip") || response.headers.has("content-encoding") || !response.body) return response;
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength < GZIP_MINIMUM) return new Response(bytes, response);
  const compressed = await new Response(new Blob([bytes]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer();
  const headers = new Headers(response.headers);
  headers.set("content-encoding", "gzip");
  headers.set("content-length", String(compressed.byteLength));
  headers.append("vary", "Accept-Encoding");
  // "manual": the body is already gzip; the runtime must send it as is.
  return new Response(compressed, { status: response.status, headers, encodeBody: "manual" });
}

/** Serve one /archive request; `caller` is null when the request carries no valid token. */
export async function archive(request: Request, env: Env, pathname: string, caller: () => Promise<Caller | null>): Promise<Response> {
  const index = env.ARCHIVE_INDEX.get(env.ARCHIVE_INDEX.idFromName("archive"));
  const url = new URL(request.url);
  const routes = ROUTES.map((route) => ({
    ...route,
    // Authentication runs after routing, so an unknown path is a 404 even without a token.
    handle: async (req: Request, params: Record<string, string>) => {
      const who = await caller();
      if (!who) return errorResponse(401, "Unauthorized");
      try {
        return await route.handle(req, params, { env, caller: who, index, url });
      } catch (error) {
        if (error instanceof HttpError) return errorResponse(error.status, error.message);
        if (error instanceof Invalid) return errorResponse(422, `Invalid v1 request: ${error.message}`.slice(0, 300));
        throw error;
      }
    },
  }));
  return negotiated(request, await dispatch(routes, request, pathname, null));
}
