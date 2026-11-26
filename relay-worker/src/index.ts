/**
 * one-conv's relay and conversation archive on Cloudflare: the wire API of `scripts/live-hub.py`.
 *
 *   /v1/...          the live relay, handled by the Relay Durable Object
 *   /archive/v1/...  the conversation archive (ArchiveIndex Durable Object + R2), same node tokens
 *   /admin/...       token minting, invites and archive import, guarded by the RELAY_ADMIN_TOKEN secret
 *   /mcp, /authorize, /oauth/..., /.well-known/oauth-...
 *                    the remote MCP server for the Claude app, behind OAuth (mcp-auth.ts)
 *   /s/<share id>    the guest page of a shared session (shares.ts), the same file the Python relay serves
 *   /s/<id>/mcp      the share's MCP server for a guest's agent, the secret as bearer token (share-mcp.ts, in the Relay)
 */

import sharePage from "./share-page.html";
import { archive } from "./archive";
import type { ImportedHead } from "./archive-index";
import type { Env } from "./env";
import { isMcpPath, mcp } from "./mcp-auth";
import { HttpError, type Route, bearer, boundedBody, decodedPath, dispatch, errorResponse, json, path, sha256, utf8 } from "./http";

export { ArchiveIndex } from "./archive-index";
export { Relay } from "./relay";

const ARCHIVE_PATH = "/archive";
// The guest page holds no data: it reads the share through /v1/shares/<id>/messages with the secret in a header.
const SHARE_PAGE = /^\/s\/[^/]+$/;
// The page runs only its own inline code and talks only to this origin.
export const PAGE_HEADERS: Record<string, string> = {
  "content-type": "text/html; charset=utf-8",
  "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "referrer-policy": "no-referrer",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
};

function page(request: Request): Response {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method Not Allowed", { status: 405, headers: { allow: "GET, HEAD", "content-type": "text/plain; charset=utf-8" } });
  }
  return new Response(request.method === "HEAD" ? null : sharePage, { headers: PAGE_HEADERS });
}
// Import batches carry search texts; a Durable Object call takes at most 32 MiB.
const ADMIN_BODY = 30 * 1024 * 1024;

function relay(env: Env) {
  return env.RELAY.get(env.RELAY.idFromName("relay"));
}

function index(env: Env) {
  return env.ARCHIVE_INDEX.get(env.ARCHIVE_INDEX.idFromName("archive"));
}

/** Whether the request carries the admin secret; compared by digest so timing reveals nothing. */
async function isAdmin(request: Request, env: Env): Promise<boolean> {
  const token = bearer(request);
  if (!env.RELAY_ADMIN_TOKEN || !token) return false;
  return (await sha256(token)) === (await sha256(env.RELAY_ADMIN_TOKEN));
}

async function adminBody(request: Request): Promise<Record<string, unknown>> {
  const parsed: unknown = JSON.parse(utf8(await boundedBody(request, ADMIN_BODY, new HttpError(413, "request too large"))) || "{}");
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new HttpError(400, "the request body must be a JSON object");
  return parsed as Record<string, unknown>;
}

function list<T>(value: unknown, name: string): T[] {
  if (!Array.isArray(value)) throw new HttpError(400, `${name} must be a list`);
  return value as T[];
}

const ADMIN: Route<Env>[] = [
  { pattern: path("/admin/tokens"), methods: ["GET"], handle: async (_, __, env) => json({ tokens: await relay(env).tokens() }) },
  {
    pattern: path("/admin/tokens"),
    methods: ["POST"],
    handle: async (request, _, env) => {
      const node = (await adminBody(request)).node;
      const minted = await relay(env).addToken(typeof node === "string" ? node : "");
      return minted.error ? errorResponse(400, minted.error) : json({ node, token: minted.token });
    },
  },
  {
    pattern: path("/admin/tokens/{node}"),
    methods: ["DELETE"],
    handle: async (_, params, env) => ((await relay(env).revoke(params.node)) ? json({ revoked: params.node }) : errorResponse(404, `no token for node ${params.node}`)),
  },
  {
    pattern: path("/admin/invites"),
    methods: ["POST"],
    handle: async (request, _, env) => {
      const made = await relay(env).adminInvite(await adminBody(request));
      return made.invite ? json(made.invite) : errorResponse(made.status ?? 400, made.error ?? "invite refused");
    },
  },
  { pattern: path("/admin/archive/stats"), methods: ["GET"], handle: async (_, __, env) => json(await index(env).stats()) },
  {
    pattern: path("/admin/archive/revisions"),
    methods: ["POST"],
    handle: async (request, _, env) => json({ added: await index(env).importRevisions(list((await adminBody(request)).revisions, "revisions")) }),
  },
  {
    pattern: path("/admin/archive/heads"),
    methods: ["POST"],
    handle: async (request, _, env) => json({ added: await index(env).importHeads(list<ImportedHead>((await adminBody(request)).heads, "heads")) }),
  },
  {
    pattern: path("/admin/archive/state"),
    methods: ["POST"],
    handle: async (request, _, env) => {
      const { sequence, generation } = await adminBody(request);
      if (typeof sequence !== "number" || typeof generation !== "string") throw new HttpError(400, "state is {sequence: int, generation: str}");
      return json(await index(env).importState(sequence, generation));
    },
  },
  {
    pattern: path("/admin/reset"),
    methods: ["POST"],
    handle: async (_, __, env) => {
      if (env.RELAY_ALLOW_RESET !== "1") return errorResponse(404, "resets are disabled");
      await Promise.all([relay(env).reset(), index(env).reset()]);
      return json({ reset: true });
    },
  },
];

async function admin(request: Request, env: Env, pathname: string): Promise<Response> {
  if (!env.RELAY_ADMIN_TOKEN) return errorResponse(503, "admin is disabled until RELAY_ADMIN_TOKEN is set");
  if (!(await isAdmin(request, env))) return errorResponse(401, "admin token required");
  try {
    return await dispatch(ADMIN, request, pathname, env);
  } catch (error) {
    if (error instanceof HttpError) return errorResponse(error.status, error.message);
    if (error instanceof SyntaxError || error instanceof TypeError) return errorResponse(400, "invalid JSON");
    throw error;
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const pathname = decodedPath(new URL(request.url));
    if (isMcpPath(pathname)) return mcp(request, env, ctx);
    if (pathname.startsWith(`${ARCHIVE_PATH}/`)) {
      const caller = async () => {
        if (await isAdmin(request, env)) return { node: "" };
        const node = await relay(env).nodeFor(bearer(request));
        return node === null ? null : { node };
      };
      return archive(request, env, pathname.slice(ARCHIVE_PATH.length), caller);
    }
    if (pathname.startsWith("/admin/")) return admin(request, env, pathname);
    if (SHARE_PAGE.test(pathname)) return page(request);
    return relay(env).fetch(request);
  },
} satisfies ExportedHandler<Env>;
