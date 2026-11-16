/** The bindings and settings the Worker runs with (see wrangler.jsonc). */

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
}
