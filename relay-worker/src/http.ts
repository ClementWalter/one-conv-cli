/**
 * HTTP details the Python relay (Starlette) exposes and its clients rely on: JSON bodies with
 * `{"error": ...}`, bearer parsing, bounded body reads, and routing that answers 404 for an
 * unknown path and 405 for a known path under another method.
 */

export class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** A JSON response whose body is already encoded (Python-encoded manifests and listings). */
export function rawJson(text: string, status = 200): Response {
  return new Response(text, { status, headers: { "content-type": "application/json" } });
}

export function errorResponse(status: number, message: string): Response {
  return json({ error: message }, status);
}

/** The token of exactly one `Bearer <token>` authorization header, or null. */
export function bearer(request: Request): string | null {
  const value = request.headers.get("authorization");
  if (value === null) return null;
  const space = value.indexOf(" ");
  if (space < 0) return null;
  const scheme = value.slice(0, space);
  const token = value.slice(space + 1);
  // A repeated header arrives joined with ", ", which leaves whitespace inside the token.
  if (scheme.toLowerCase() !== "bearer" || !token || /\s/.test(token)) return null;
  return token;
}

/**
 * The request body, refused with `overflow` as soon as it exceeds `limit`, whatever
 * Content-Length says. `drain` keeps reading an oversized body so the client sees the answer
 * instead of a reset connection.
 */
export async function boundedBody(request: Request, limit: number, overflow: HttpError, drain = false): Promise<Uint8Array> {
  const declared = request.headers.get("content-length") ?? "";
  if (!drain && /^\d+$/.test(declared) && Number(declared) > limit) throw overflow;
  if (!request.body) return new Uint8Array();
  const parts: Uint8Array[] = [];
  let size = 0;
  const reader = request.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      if (!drain) {
        await reader.cancel();
        throw overflow;
      }
      continue;
    }
    parts.push(value);
  }
  if (size > limit) throw overflow;
  const out = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.byteLength;
  }
  return out;
}

export function utf8(bytes: Uint8Array): string {
  return new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes);
}

export type Params = Record<string, string>;
export interface Route<C> {
  pattern: RegExp;
  methods: string[];
  handle: (request: Request, params: Params, context: C) => Promise<Response>;
}

/** `/v1/messages/{msg_id}` as a regex whose groups are the path parameters (one segment each). */
export function path(template: string): RegExp {
  const source = template.replace(/\{(\w+)\}/g, "(?<$1>[^/]+)");
  return new RegExp(`^${source}$`);
}

/** Starlette's matching: the first full match wins, else a path match under another method is a 405. */
export async function dispatch<C>(routes: Route<C>[], request: Request, pathname: string, context: C): Promise<Response> {
  let allowed: string[] | null = null;
  for (const route of routes) {
    const match = route.pattern.exec(pathname);
    if (!match) continue;
    const methods = route.methods.includes("GET") ? [...route.methods, "HEAD"] : route.methods;
    if (methods.includes(request.method)) return route.handle(request, { ...match.groups }, context);
    allowed ??= methods;
  }
  if (allowed) return new Response("Method Not Allowed", { status: 405, headers: { allow: allowed.join(", "), "content-type": "text/plain; charset=utf-8" } });
  return new Response("Not Found", { status: 404, headers: { "content-type": "text/plain; charset=utf-8" } });
}

/** The decoded path, as Starlette matches routes on it. */
export function decodedPath(url: URL): string {
  try {
    return decodeURIComponent(url.pathname);
  } catch {
    return url.pathname;
  }
}

/** The last value of a query parameter, as Starlette's `query_params.get` returns it. */
export function query(url: URL, name: string): string | null {
  const values = url.searchParams.getAll(name);
  return values.length ? values[values.length - 1] : null;
}

/** Python's float() of a query value, or null where float() raises. */
export function pyFloat(text: string): number | null {
  const cleaned = text.trim().replace(/(?<=\d)_(?=\d)/g, "");
  const special = cleaned.toLowerCase().replace(/^[+-]/, "");
  if (["inf", "infinity"].includes(special)) return cleaned.startsWith("-") ? -Infinity : Infinity;
  if (special === "nan") return NaN;
  if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(cleaned)) return null;
  return Number(cleaned);
}

export function hex(bytes: ArrayBuffer | Uint8Array): string {
  return [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function sha256(data: string | Uint8Array): Promise<string> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

export function randomHex(bytes: number): string {
  return hex(crypto.getRandomValues(new Uint8Array(bytes)));
}

/** `secrets.token_urlsafe(32)`: 32 random bytes as unpadded base64url. */
export function tokenUrlsafe(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
