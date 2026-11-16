/** The admin API: token management and the archive import routes, guarded by RELAY_ADMIN_TOKEN. */

import { beforeEach, describe, expect, test } from "vitest";
import { ADMIN, body, call, mint } from "./helpers";

beforeEach(async () => {
  await call("/admin/reset", { method: "POST", token: ADMIN });
});

function head(key: string, sequence: number) {
  return { key, revision: `r-${key}`, mtime: 1.5, metadata: `{"origin":"mac","mtime":1.5}`, search_text: `text of ${key}`, change_sequence: sequence };
}

async function heads(rows: unknown[]): Promise<Response> {
  return call("/admin/archive/heads", { method: "POST", token: ADMIN, body: { heads: rows } });
}

describe("guard", () => {
  test("a node token is not an admin token", async () => {
    expect((await call("/admin/tokens", { token: await mint("mac") })).status).toBe(401);
  });

  test("a request without a token is refused", async () => {
    expect((await call("/admin/tokens")).status).toBe(401);
  });
});

describe("tokens", () => {
  test("minted nodes are listed", async () => {
    await mint("mac");
    const { tokens } = await body<{ tokens: { node: string }[] }>(call("/admin/tokens", { token: ADMIN }));
    expect(tokens.map((row) => row.node)).toEqual(["mac"]);
  });

  test("a malformed node name is refused", async () => {
    expect((await call("/admin/tokens", { method: "POST", token: ADMIN, body: { node: "Mac Book" } })).status).toBe(400);
  });

  test("revoking an unknown node is a 404", async () => {
    expect((await call("/admin/tokens/ghost", { method: "DELETE", token: ADMIN })).status).toBe(404);
  });
});

describe("archive import", () => {
  test("revisions already present are not added again", async () => {
    const revisions = [{ key: "k", revision: "r-k", manifest: "{}" }];
    await call("/admin/archive/revisions", { method: "POST", token: ADMIN, body: { revisions } });
    expect(await body(call("/admin/archive/revisions", { method: "POST", token: ADMIN, body: { revisions } }))).toEqual({ added: 0 });
  });

  test("a head already present is kept", async () => {
    await heads([head("k", 1)]);
    expect(await body(heads([{ ...head("k", 1), revision: "other" }]))).toEqual({ added: 0 });
  });

  test("an imported head keeps its change sequence", async () => {
    await heads([head("k", 7)]);
    await call("/admin/archive/revisions", { method: "POST", token: ADMIN, body: { revisions: [{ key: "k", revision: "r-k", manifest: "{}" }] } });
    const changes = await body<{ sequence: number }>(call("/archive/v1/conversations?since=0", { token: ADMIN }));
    expect(changes.sequence).toBe(7);
  });

  test("a head whose sequence is taken gets the next free one", async () => {
    await heads([head("a", 1), head("b", 1)]);
    const changes = await body<{ items: { key: string }[] }>(call("/archive/v1/conversations?since=1", { token: ADMIN }));
    expect(changes.items.map((row) => row.key)).toEqual(["b"]);
  });

  test("the imported state sets the generation", async () => {
    await call("/admin/archive/state", { method: "POST", token: ADMIN, body: { sequence: 3, generation: "g1" } });
    expect((await body<{ generation: string }>(call("/archive/v1/conversations?since=0", { token: ADMIN }))).generation).toBe("g1");
  });

  test("an imported head is searchable", async () => {
    await heads([head("k", 1)]);
    const { items } = await body<{ items: { key: string }[] }>(call("/archive/v1/conversations?query=TEXT%20OF", { token: ADMIN }));
    expect(items.map((row) => row.key)).toEqual(["k"]);
  });
});
