// Unit tests run inside workerd (Miniflare) with local Durable Objects and R2; nothing reaches Cloudflare.
import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: { bindings: { RELAY_ADMIN_TOKEN: "admin-secret", RELAY_ALLOW_RESET: "1" } },
    }),
  ],
});
