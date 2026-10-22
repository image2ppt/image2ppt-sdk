/** Task names are submission options and server-resolved job metadata. */

import { createHmac } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  IdempotencyKeyMismatchError,
  Image2PPTClient,
  InvalidNameError,
  Job,
  verifyWebhook,
} from "../src/index.js";
import { withoutSleeping } from "./helpers.js";

interface Call {
  url: string;
  init: RequestInit;
}

function scripted(handler: (call: Call, n: number) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const fetch = (async (url: unknown, init: RequestInit = {}) => {
    const call = { url: String(url), init };
    calls.push(call);
    return handler(call, calls.length);
  }) as typeof globalThis.fetch;
  const client = new Image2PPTClient({ apiKey: "i2p_live_test", fetch });
  return { client, calls };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function created(name = "Quarterly review (2)", jobId = "j"): Response {
  return json(201, { jobId, status: "pending", name });
}

async function formFields(call: Call): Promise<Record<string, string>> {
  const text = await new Response(call.init.body).text();
  return Object.fromEntries(
    [...text.matchAll(/name="([^"]+)"\r\n\r\n([^\r]*)\r\n/g)].map((match) => [
      match[1]!,
      match[2]!,
    ]),
  );
}

let dir: string;
let pdf: string;
beforeEach(async () => {
  dir = await mkdtemp(join(process.cwd(), ".task-name-test-"));
  pdf = join(dir, "slides.pdf");
  await writeFile(pdf, "%PDF-1.4 test");
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe("task name", () => {
  it("forwards a name in multipart and returns the resolved name", async () => {
    const { client, calls } = scripted(() => created());
    const job = await client.submit([pdf], { name: "Quarterly review" });
    expect(await formFields(calls[0]!)).toEqual({ name: "Quarterly review" });
    expect(job.name).toBe("Quarterly review (2)");
  });

  it("forwards a name in the URL submission JSON", async () => {
    const { client, calls } = scripted(() => created());
    await client.submitUrls(["https://example.com/slides.pdf"], { name: "Quarterly review" });
    expect(JSON.parse(Buffer.from(calls[0]!.init.body as Uint8Array).toString())).toEqual({
      urls: ["https://example.com/slides.pdf"],
      name: "Quarterly review",
    });
  });

  it("omits an unspecified name from both submission formats", async () => {
    const { client, calls } = scripted(() => created());
    await client.submit([pdf]);
    await client.submitUrls(["https://example.com/slides.pdf"]);
    expect(await formFields(calls[0]!)).not.toHaveProperty("name");
    expect(JSON.parse(Buffer.from(calls[1]!.init.body as Uint8Array).toString())).not.toHaveProperty("name");
  });

  it("reads updated names from status and list responses", async () => {
    const { client } = scripted((_call, n) =>
      n === 1
        ? json(200, { jobId: "j", status: "completed", name: "Renamed on website" })
        : json(200, { data: [{ jobId: "j", status: "completed", name: "Renamed again" }] }),
    );
    expect((await client.getJob("j")).name).toBe("Renamed on website");
    expect((await client.listJobs()).data[0]!.name).toBe("Renamed again");
  });

  it.each([undefined, null, 42])("reads missing or unusable older-server name %j as null", (name) => {
    expect(new Job({ jobId: "j", status: "pending", name }).name).toBeNull();
  });

  it("exposes names in verified webhook jobs", () => {
    const key = Buffer.alloc(32, 1);
    const body = JSON.stringify({
      type: "job.completed",
      data: { jobId: "j", status: "completed", name: "Webhook name" },
    });
    const signature = createHmac("sha256", key).update(`msg_1.1700000000.${body}`).digest("base64");
    const event = verifyWebhook(body, {
      "webhook-id": "msg_1",
      "webhook-timestamp": "1700000000",
      "webhook-signature": `v1,${signature}`,
    }, `whsec_${key.toString("base64")}`, { now: 1700000000 });
    expect(event.job.name).toBe("Webhook name");
  });

  it("maps server INVALID_NAME to its public error class", async () => {
    const { client, calls } = scripted(() => json(400, {
      error: { code: "INVALID_NAME", message: "Invalid task name" },
    }));
    const error = await client.submitUrls(["https://example.com/slides.pdf"], {
      name: "Accepted locally",
      idempotencyKey: "name-error",
    }).catch((error: unknown) => error);
    expect(error).toBeInstanceOf(InvalidNameError);
    expect(error).toMatchObject({ code: "INVALID_NAME", statusCode: 400, idempotencyKey: "name-error" });
    expect(calls).toHaveLength(1);
  });

  it("keeps the name and key identical through retries", async () => {
    const { client, calls } = scripted((_call, n) =>
      n === 1 ? json(503, { error: { code: "UNAVAILABLE", message: "try later" } }) : created(),
    );
    await withoutSleeping(() => client.submitUrls(["https://example.com/slides.pdf"], {
      name: "Quarterly review", idempotencyKey: "name-retry",
    }));
    expect(calls).toHaveLength(2);
    expect(calls[0]!.init.body).toBe(calls[1]!.init.body);
    for (const call of calls) {
      expect(new Headers(call.init.headers).get("Idempotency-Key")).toBe("name-retry");
      expect(JSON.parse(Buffer.from(call.init.body as Uint8Array).toString()).name).toBe("Quarterly review");
    }
  });

  it("passes a same-key changed-name conflict through without retries", async () => {
    const { client, calls } = scripted((_call, n) => n === 1 ? created() : json(422, {
      error: { code: "IDEMPOTENCY_KEY_MISMATCH", message: "different request" },
    }));
    const urls = ["https://example.com/slides.pdf"];
    await client.submitUrls(urls, { name: "First", idempotencyKey: "same-key" });
    await expect(client.submitUrls(urls, { name: "Second", idempotencyKey: "same-key" })).rejects.toBeInstanceOf(IdempotencyKeyMismatchError);
    expect(calls).toHaveLength(2);
    expect(calls.map((call) => JSON.parse(Buffer.from(call.init.body as Uint8Array).toString()).name)).toEqual(["First", "Second"]);
  });

  it("convert forwards the name and downloads only to the caller's path", async () => {
    const fields: Record<string, string>[] = [];
    const { client } = scripted(async (call, n) => {
      if (n === 1) {
        fields.push(await formFields(call));
        return created();
      }
      if (call.url.endsWith("/download")) {
        return new Response("deck", { headers: { "content-disposition": 'attachment; filename="server-name.pptx"' } });
      }
      return json(200, { jobId: "j", status: "completed", name: "server-name" });
    });
    const dest = join(dir, "caller-name.pptx");
    const job = await client.convert([pdf], dest, { name: "Quarterly review", pollIntervalMs: 0 });
    expect(job.name).toBe("server-name");
    expect(fields).toEqual([{ name: "Quarterly review" }]);
    expect(await readFile(dest, "utf8")).toBe("deck");
    expect(await readdir(dir)).not.toContain("server-name.pptx");
  });

  it("convertAll sends the same base name to each batch and keeps part-NN paths", async () => {
    const second = join(dir, "second.pdf");
    await writeFile(second, "%PDF-1.4 test");
    const fields: Record<string, string>[] = [];
    const { client } = scripted(async (call) => {
      if (call.init.method === "POST") {
        fields.push(await formFields(call));
        return created(`Quarterly review (${fields.length + 1})`, `j${fields.length}`);
      }
      if (call.url.endsWith("/download")) {
        return new Response("deck", { headers: { "content-disposition": 'attachment; filename="server-name.pptx"' } });
      }
      return json(200, { jobId: "j", status: "completed", name: "server-name" });
    });
    const dest = join(dir, "decks");
    expect(await client.convertAll([pdf, second], dest, { name: "Quarterly review", pollIntervalMs: 0 })).toEqual([
      join(dest, "part-01.pptx"), join(dest, "part-02.pptx"),
    ]);
    expect(fields).toEqual([{ name: "Quarterly review" }, { name: "Quarterly review" }]);
    expect((await readdir(dest)).sort()).toEqual(["part-01.pptx", "part-02.pptx"]);
  });
});


describe("task name parameter types", () => {
  it.each([42, true, []])("rejects non-string %j before any HTTP", async (name) => {
    const { client, calls } = scripted(() => created());
    const options = { name: name as unknown as string };
    await expect(client.submit([pdf], options)).rejects.toThrow("name must be a string");
    await expect(client.submitUrls(["https://example.com/slides.pdf"], options)).rejects.toThrow("name must be a string");
    expect(calls).toHaveLength(0);
  });

  it.each(["", "  Quarterly\u200b  review  ", "😀".repeat(60)])(
    "sends name contents unchanged for service validation: %j", async (name) => {
      const { client, calls } = scripted(() => created());
      await client.submitUrls(["https://example.com/slides.pdf"], { name });
      const body = JSON.parse(Buffer.from(calls[0]!.init.body as Uint8Array).toString());
      expect(body.name).toBe(name);
    },
  );
});
