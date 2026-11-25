/**
 * OAuth 2.1 in front of /mcp, as the Claude app's custom connectors require: discovery metadata,
 * dynamic client registration, PKCE code and refresh grants, from @cloudflare/workers-oauth-provider
 * with its state in the OAUTH_KV namespace.
 *
 * There is one user, the owner: the /authorize page grants a client access when the owner types
 * the MCP_OWNER_PASSPHRASE secret. Failed attempts are counted per hour, and the page refuses
 * every attempt once FAILURE_LIMIT is reached, so the passphrase cannot be guessed online.
 *
 * The token audience is `<origin>/mcp` of the URL the request arrived at, so `wrangler dev` and
 * the deployed Worker each issue tokens for themselves.
 */

import { AuthorizationError, CimdFetchError, OAuthProvider, type ConsentDescription } from "@cloudflare/workers-oauth-provider";
import type { Env } from "./env";
import { sha256 } from "./http";
import { mcpApi } from "./mcp";

export const MCP_PATH = "/mcp";
const AUTHORIZE_PATH = "/authorize";
export const FAILURE_LIMIT = 10;
const DAY = 24 * 3600;
// The paths the OAuth provider answers; everything else stays with the relay and the archive.
const OWNED = [/^\/mcp(\/|$)/, /^\/authorize$/, /^\/oauth\//, /^\/\.well-known\/oauth-(authorization-server|protected-resource)(\/|$)/];

export function isMcpPath(pathname: string): boolean {
  return OWNED.some((pattern) => pattern.test(pathname));
}

const providers = new Map<string, OAuthProvider<Env>>();

function provider(origin: string): OAuthProvider<Env> {
  let found = providers.get(origin);
  if (!found) {
    found = new OAuthProvider<Env>({
      apiRoute: MCP_PATH,
      apiHandler: mcpApi,
      defaultHandler: { fetch: authorize },
      authorizeEndpoint: AUTHORIZE_PATH,
      tokenEndpoint: "/oauth/token",
      clientRegistrationEndpoint: "/oauth/register",
      resourceMetadata: { resource: `${origin}${MCP_PATH}`, authorization_servers: [origin] },
      // A phone connector stays connected while used, and asks for the passphrase again after two idle months.
      refreshTokenTTL: 365 * DAY,
      refreshTokenIdleTTL: 60 * DAY,
    });
    providers.set(origin, found);
  }
  return found;
}

/** Serve an OAuth or MCP request; the caller routes here only paths `isMcpPath` accepts. */
export async function mcp(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  if (!env.OAUTH_KV) return Response.json({ error: "the MCP server is disabled until the OAUTH_KV namespace is bound" }, { status: 503 });
  return provider(new URL(request.url).origin).fetch(request, env, ctx);
}

// --- the owner's consent page ------------------------------------------------------------------------

const escape = (value: string) => value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

function html(body: string, status = 200, headers = new Headers()): Response {
  headers.set("content-type", "text/html; charset=utf-8");
  headers.set("x-frame-options", "DENY");
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>one-conv</title>` +
      `<style>body{font:16px system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem}input,button{font:inherit;padding:.5rem}input{width:100%;box-sizing:border-box}</style>${body}`,
    { status, headers },
  );
}

function consentPage(details: ConsentDescription, handle: string): string {
  const origin = details.clientDomain ? `Published by <strong>${escape(details.clientDomain)}</strong>.` : "This app registered itself; its name is not verified.";
  const loopback = details.redirectIsLoopback ? "<p><strong>This sends access to an app on your computer.</strong> Continue only if you just started connecting it.</p>" : "";
  return (
    `<h1>Connect ${escape(details.clientName)} to one-conv?</h1>` +
    `<p>${origin} It will read your archived conversations, see your running agents and message them as you. ` +
    `Access is sent to <strong>${escape(details.redirectHost)}</strong>.</p>${loopback}` +
    `<form method="post"><input type="hidden" name="handle" value="${escape(handle)}">` +
    `<p><label>Owner passphrase<br><input type="password" name="passphrase" autocomplete="current-password" autofocus></label></p>` +
    `<p><button name="decision" value="approve">Allow</button> <button name="decision" value="deny">Deny</button></p></form>`
  );
}

function failureKey(at: number): string {
  return `mcp-owner-failures:${Math.floor(at / 3_600_000)}`;
}

/** Whether `typed` is the owner passphrase; compared by digest so timing reveals nothing. */
async function ownerPassphrase(typed: string, env: Env): Promise<boolean> {
  return (await sha256(typed)) === (await sha256(env.MCP_OWNER_PASSPHRASE!));
}

async function authorize(request: Request, env: Env): Promise<Response> {
  if (new URL(request.url).pathname !== AUTHORIZE_PATH) return new Response("Not Found", { status: 404 });
  if (!env.MCP_OWNER_PASSPHRASE) return html("<p>Connecting is disabled until the MCP_OWNER_PASSPHRASE secret is set.</p>", 503);
  const oauth = env.OAUTH_PROVIDER!;
  try {
    if (request.method === "GET") {
      const authRequest = await oauth.parseAuthRequest(request);
      const details = await oauth.describeConsent(authRequest);
      const consent = await oauth.beginConsent(authRequest);
      return html(consentPage(details, consent.handle), 200, consent.headers);
    }
    if (request.method !== "POST") return new Response("Method Not Allowed", { status: 405, headers: { allow: "GET, POST" } });
    const form = await request.formData();
    const handle = String(form.get("handle") ?? "");
    if (form.get("decision") !== "approve") {
      const denied = await oauth.denyConsent(request, handle);
      return new Response(null, { status: 302, headers: denied.headers });
    }
    const kv = env.OAUTH_KV!;
    const key = failureKey(Date.now());
    const failures = Number((await kv.get(key)) ?? "0");
    if (failures >= FAILURE_LIMIT) return html("<p>Too many wrong passphrases this hour. Try again later.</p>", 429);
    // The handle stays unused on a wrong passphrase, so going back and retrying works.
    if (!(await ownerPassphrase(String(form.get("passphrase") ?? ""), env))) {
      await kv.put(key, String(failures + 1), { expirationTtl: 7200 });
      return html("<p>Wrong passphrase. Go back and try again.</p>", 403);
    }
    const approved = await oauth.approveConsent(request, handle);
    const { redirectTo } = await oauth.completeAuthorization({ request: approved.request, userId: "owner", metadata: {}, scope: approved.request.scope, props: { owner: true } });
    approved.headers.set("location", redirectTo);
    return new Response(null, { status: 302, headers: approved.headers });
  } catch (error) {
    if (error instanceof AuthorizationError && error.redirectTo) return Response.redirect(error.redirectTo, 302);
    if (error instanceof AuthorizationError) return html(`<p>${escape(error.description)}</p>`, 400);
    if (error instanceof CimdFetchError) return html("<p>This app could not be verified.</p>", 400);
    throw error;
  }
}
