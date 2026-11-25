/** The bindings and settings the Worker runs with (see wrangler.jsonc). */

import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import type { ArchiveIndex } from "./archive-index";
import type { Relay } from "./relay";

export interface Env {
  RELAY: DurableObjectNamespace<Relay>;
  ARCHIVE_INDEX: DurableObjectNamespace<ArchiveIndex>;
  /** Chunk bodies (`chunks/<sha256>`) and attachment bodies (`attachments/<id>`). */
  ARCHIVE: R2Bucket;
  /** Guards /admin: minting and revoking node tokens, importing an archive. Set with `wrangler secret put`. */
  RELAY_ADMIN_TOKEN?: string;
  /** "1" enables POST /admin/reset, which empties everything; only test runs set it. */
  RELAY_ALLOW_RESET?: string;
  /** OAuth clients, grants and tokens of the remote MCP server (hashed by the provider library). */
  OAUTH_KV?: KVNamespace;
  /** What the owner types on the /authorize page to connect a client. Set with `wrangler secret put`. */
  MCP_OWNER_PASSPHRASE?: string;
  /** Set by the OAuth provider before it calls the /authorize handler. */
  OAUTH_PROVIDER?: OAuthHelpers;
}
