/** Invites: POST /v1/invites (node token) and /admin/invites (admin token) make one-time codes; POST /v1/join redeems one. */

import { runInDurableObject } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, test } from "vitest";
import type { Env } from "../src/env";
import { ADMIN, body, call, mint } from "./helpers";

beforeEach(async () => {
  await (await call("/admin/reset", { method: "POST", token: ADMIN })).text();
});

const bindings = env as unknown as Env;
const relay = () => bindings.RELAY.get(bindings.RELAY.idFromName("relay"));

interface Invite {
  node: string;
  code: string;
  expires: number;
}

async function invite(token: string, extra: Record<string, unknown> = {}): Promise<Response> {
  return call("/v1/invites", { method: "POST", token, body: { node: "studio", ...extra } });
}

async function join(code: unknown): Promise<Response> {
  return call("/v1/join", { method: "POST", body: { code } });
}

describe("making an invite", () => {
  test("a node token makes an invite bound to the node", async () => {
    expect((await body<Invite>(invite(await mint("mac")))).node).toBe("studio");
  });

  test("the admin token makes an invite too", async () => {
    expect((await call("/admin/invites", { method: "POST", token: ADMIN, body: { node: "studio" } })).status).toBe(200);
  });

  test("an admin invite for a node with a token is a 409", async () => {
    await mint("studio");
    expect((await call("/admin/invites", { method: "POST", token: ADMIN, body: { node: "studio" } })).status).toBe(409);
  });

  test("an invite expires within the default fifteen minutes", async () => {
    const before = Date.now() / 1000;
    const { expires } = await body<Invite>(invite(await mint("mac")));
    expect(expires - before).toBeLessThanOrEqual(15 * 60 + 1);
  });

  test("the relay stores only a digest of the code", async () => {
    const { code } = await body<Invite>(invite(await mint("mac")));
    const stored = await runInDurableObject(relay(), (_, state) => JSON.stringify(state.storage.sql.exec("SELECT * FROM invites").toArray()));
    expect(stored).not.toContain(code);
  });
});

describe("joining", () => {
  test("a redeemed code answers whoami as its node", async () => {
    const { code } = await body<Invite>(invite(await mint("mac")));
    const { token } = await body<{ token: string }>(join(code));
    expect(await body(call("/v1/whoami", { token }))).toEqual({ node: "studio" });
  });

  test("a code works once", async () => {
    const { code } = await body<Invite>(invite(await mint("mac")));
    await (await join(code)).text();
    expect((await join(code)).status).toBe(410);
  });

  test("an expired code is a 410", async () => {
    const { code } = await body<Invite>(invite(await mint("mac")));
    await runInDurableObject(relay(), (_, state) => state.storage.sql.exec("UPDATE invites SET expires = 0"));
    expect((await join(code)).status).toBe(410);
  });

  test("a code that is not a string is a 400", async () => {
    expect((await join(42)).status).toBe(400);
  });

  test("a body that is not JSON is a 400", async () => {
    expect((await call("/v1/join", { method: "POST", raw: "{" })).status).toBe(400);
  });

  test("a replacing invite revokes the node's old token", async () => {
    const mac = await mint("mac");
    const { code } = await body<Invite>(invite(mac, { node: "mac", replace: true }));
    await (await join(code)).text();
    expect((await call("/v1/whoami", { token: mac })).status).toBe(401);
  });
});
