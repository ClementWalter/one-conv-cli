/** Requests against the Worker as a bridge or the archive client sends them, plus test setup. */

import { exports } from "cloudflare:workers";

export const ADMIN = "admin-secret";

export interface Call {
  method?: string;
  token?: string | null;
  body?: unknown;
  raw?: BodyInit;
  headers?: Record<string, string>;
}

export function call(path: string, { method = "GET", token = null, body, raw, headers = {} }: Call = {}): Promise<Response> {
  const init: RequestInit = { method, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...headers } };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["content-type"] = "application/json";
  }
  if (raw !== undefined) init.body = raw;
  return exports.default.fetch(new Request(`http://relay.test${path}`, init));
}

export async function mint(node: string): Promise<string> {
  const response = await call("/admin/tokens", { method: "POST", token: ADMIN, body: { node } });
  return ((await response.json()) as { token: string }).token;
}

export async function publish(token: string, sessions: unknown[]): Promise<Response> {
  return call("/v1/sessions", { method: "PUT", token, body: { sessions } });
}

export function message(to: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { id: crypto.randomUUID().replace(/-/g, ""), to, from: "claude:aaa", text: "hello", ...extra };
}

/** Nodes `mac` and `box`, each with one published session (@lead and @worker). */
export async function twoNodes(): Promise<{ mac: string; box: string }> {
  const mac = await mint("mac");
  const box = await mint("box");
  // Bodies are read so no response stays in flight: evicting the relay waits for those to drain.
  await (await publish(mac, [{ address: "claude:aaa", name: "lead" }])).text();
  await (await publish(box, [{ address: "claude:bbb", name: "worker" }])).text();
  return { mac, box };
}

export async function body<T = Record<string, unknown>>(response: Response | Promise<Response>): Promise<T> {
  return (await (await response).json()) as T;
}

/** zlib bytes of `data`, as Python's zlib.compress writes them. */
export async function deflate(data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await new Response(new Blob([data]).stream().pipeThrough(new CompressionStream("deflate"))).arrayBuffer());
}

export function b64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes));
}
