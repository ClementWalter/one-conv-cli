// Types `exports` from "cloudflare:workers" as the Worker's own module, and `env` as its bindings.
declare namespace Cloudflare {
  interface GlobalProps {
    mainModule: typeof import("../src/index");
  }
  interface Env extends import("../src/env").Env {}
}
