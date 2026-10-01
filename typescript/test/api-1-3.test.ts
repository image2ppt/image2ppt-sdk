/**
 * Tests for what API 1.3.0 added: pages, callbacks, Idempotency-Key, URLs, job lists.
 *
 * The matching Python tests live in `python/tests/test_api_1_3.py` and pin the same
 * cases — a body or a selection must mean the same thing to both clients.
 */

import { createHmac } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import sharp from "sharp";
import { withoutSleeping } from "./helpers.js";

import {
  APIConnectionError,
  CallbackStatus,
  IdempotencyKeyInProgressError,
  IdempotencyKeyMismatchError,
  Image2PPTClient,
  Image2PPTError,
  InvalidCallbackUrlError,
  InvalidFileError,
  InvalidIdempotencyKeyError,
  InvalidPagesError,
  InvalidParameterError,
  InvalidUrlError,
  Job,
  JobList,
  MalformedResponseError,
  MAX_PAGES_PER_JOB,
  PagesOutOfRangeError,
  RateLimitedError,
  ServerError,
  TooManySlidesError,
  UrlFetchFailedError,
  WebhookVerificationError,
  checkPageSelection,
  checkSubmission,
  verifyWebhook,
} from "../src/index.js";
import { parsePages, selectedPageCount } from "../src/pages.js";

// --------------------------------------------------------------------------- //
// fixtures
// --------------------------------------------------------------------------- //
type RecordingFetch = typeof fetch & { calls: Array<{ url: string; init: RequestInit }> };

function fetchScript(handler: (n: number) => Response | Promise<Response>): RecordingFetch {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const impl = (async (url: unknown, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    return handler(calls.length);
  }) as unknown as RecordingFetch;
  impl.calls = calls;
  return impl;
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

const created = (jobId = "job_1") => () => json(201, { jobId, status: "pending" });

function error(
  status: number,
  code: string,
  extra: Record<string, unknown> = {},
  headers: Record<string, string> = {},
): Response {
  return json(status, { error: { code, message: "nope", ...extra } }, headers);
}

function client(f: typeof fetch, timeoutMs?: number): Image2PPTClient {
  return new Image2PPTClient({ apiKey: "i2p_live_test", fetch: f, timeoutMs });
}

function headersOf(f: RecordingFetch, n = 0): Record<string, string> {
  return f.calls[n]!.init.headers as Record<string, string>;
}

/** The non-file form fields of the n-th multipart POST. */
async function formFields(f: RecordingFetch, n = 0): Promise<Record<string, string>> {
  const text = Buffer.from(await new Response(f.calls[n]!.init.body).arrayBuffer()).toString(
    "utf8",
  );
  const fields: Record<string, string> = {};
  for (const match of text.matchAll(/name="([^"]+)"\r\n\r\n([^\r]*)\r\n/g)) {
    fields[match[1]!] = match[2]!;
  }
  return fields;
}

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "image2ppt-api13-"));
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

async function images(count: number): Promise<string[]> {
  const paths: string[] = [];
  for (let i = 0; i < count; i += 1) {
    const path = join(dir, `img${String(i).padStart(3, "0")}.png`);
    await sharp({ create: { width: 8, height: 8, channels: 3, background: { r: i % 256, g: 0, b: 0 } } })
      .png()
      .toFile(path);
    paths.push(path);
  }
  return paths;
}

async function rejection(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => {
      throw new Error("expected a rejection");
    },
    (err: unknown) => err,
  );
}

// --------------------------------------------------------------------------- //
// pages — spelled exactly as the service reads it
// --------------------------------------------------------------------------- //
describe("pages", () => {
  it.each([
    ["1-3,7", [[1, 3], [7, 7]]],
    ["1-3, 7", [[1, 3], [7, 7]]],
    [" 2 - 4 ", [[2, 4]]],
    ["007", [[7, 7]]],
    ["5,5,1-9", [[5, 5], [5, 5], [1, 9]]],
    ["﻿1　", [[1, 1]]],
    ["1-1000000000000", [[1, 1000000000000]]],
  ])("parses %j as the service does", (pages, ranges) => {
    expect(parsePages(pages)).toEqual(ranges);
  });

  it.each([
    "1,,2",
    "1,",
    "3-1",
    "1-",
    "-3",
    "a",
    "1 2",
    "１",
    "1.5",
    "+1",
    "0,abc", // spelling is judged before range, as the service does
    String(2n ** 53n),
    "1,".repeat(500) + "1", // 1001 characters
  ])("refuses %j locally as misspelled", (pages) => {
    let caught: unknown;
    try {
      parsePages(pages);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(InvalidPagesError);
    expect((caught as Error).constructor).toBe(InvalidPagesError);
    expect((caught as InvalidPagesError).code).toBe("INVALID_PAGES");
  });

  it("calls page zero out of range, not misspelled", () => {
    expect(() => parsePages("0-2")).toThrow(PagesOutOfRangeError);
  });

  it("counts the selection by merging, never expanding", () => {
    expect(selectedPageCount(parsePages("1-3,2-5,5,9"))).toBe(6);
    expect(selectedPageCount(parsePages("1-1000000000000"))).toBe(1000000000000);
  });

  it("bounds the selected pages, not the files", () => {
    checkSubmission(1, 60, 0, "1-10");
    checkSubmission(1, 0, 1, "40-60");
  });

  it("refuses more than fifty selected pages locally", () => {
    expect(() => checkSubmission(1, 0, 1, "1-51")).toThrow(TooManySlidesError);
    expect(() => checkPageSelection("1-60")).toThrow(TooManySlidesError);
  });

  it("knows a page past the end when there are only images", () => {
    expect(() => checkSubmission(1, 3, 0, "2-4")).toThrow(/has 3 pages/);
    expect(() => checkSubmission(1, 3, 0, "2-4")).toThrow(PagesOutOfRangeError);
    checkSubmission(1, 3, 1, "2-4");
  });

  it("keeps the old page count without a selection", () => {
    for (const blank of ["", "  ", undefined, null]) {
      expect(() => checkSubmission(1, MAX_PAGES_PER_JOB + 1, 0, blank)).toThrow(
        TooManySlidesError,
      );
    }
  });
});

// --------------------------------------------------------------------------- //
// submit: new fields and the Idempotency-Key header
// --------------------------------------------------------------------------- //
describe("submit options", () => {
  it("sends pages, the callback and a fresh key each call", async () => {
    const f = fetchScript(created());
    const paths = await images(3);

    await client(f).submit(paths, { pages: "1-2", callbackUrl: "https://example.com/hook" });
    await client(f).submit(paths);

    expect(await formFields(f, 0)).toEqual({
      pages: "1-2",
      callbackUrl: "https://example.com/hook",
    });
    expect(await formFields(f, 1)).toEqual({});
    const keys = [0, 1].map((n) => headersOf(f, n)["Idempotency-Key"]!);
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys.every((key) => key.length === 36)).toBe(true);
  });

  it("does not send a blank selection or callback", async () => {
    const f = fetchScript(created());
    await client(f).submit(await images(1), { pages: "  ", callbackUrl: "" });
    expect(await formFields(f)).toEqual({});
  });

  it("uses the caller's key", async () => {
    const f = fetchScript(created());
    await client(f).submit(await images(1), { idempotencyKey: "order-42" });
    expect(headersOf(f)["Idempotency-Key"]).toBe("order-42");
  });

  it.each(["", "has space", "x".repeat(256), "中文", "tab\t"])(
    "refuses the key %j before anything is sent",
    async (key) => {
      const f = fetchScript(created());
      const paths = await images(1);
      expect(await rejection(client(f).submit(paths, { idempotencyKey: key }))).toBeInstanceOf(
        InvalidIdempotencyKeyError,
      );
      expect(f.calls).toHaveLength(0);
    },
  );

  it("takes the longest and widest valid key", async () => {
    const f = fetchScript(created());
    const paths = await images(1);
    const key = Array.from({ length: 0x7f - 0x21 }, (_, i) => String.fromCharCode(0x21 + i))
      .join("")
      .repeat(3); // 282 characters: too long
    expect(await rejection(client(f).submit(paths, { idempotencyKey: key }))).toBeInstanceOf(
      InvalidIdempotencyKeyError,
    );
    await client(f).submit(paths, { idempotencyKey: key.slice(0, 255) });
    expect(headersOf(f)["Idempotency-Key"]).toBe(key.slice(0, 255));
  });

  it("refuses pages that are not text", async () => {
    const f = fetchScript(created());
    const paths = await images(1);
    expect(
      await rejection(client(f).submit(paths, { pages: 3 as unknown as string })),
    ).toBeInstanceOf(TypeError);
  });

  it("refuses a single string instead of reading one path per character", async () => {
    const f = fetchScript(created());
    const [path] = await images(1);
    expect(
      await rejection(client(f).submit(path as unknown as string[])),
    ).toBeInstanceOf(TypeError);
    expect(f.calls).toHaveLength(0);
  });

  it("never sends a misspelled selection", async () => {
    const f = fetchScript(created());
    const paths = await images(2);
    expect(await rejection(client(f).submit(paths, { pages: "1-" }))).toBeInstanceOf(
      InvalidPagesError,
    );
    expect(f.calls).toHaveLength(0);
  });

  it("passes the new options through convert", async () => {
    const f = fetchScript((n) => {
      if (n === 1) return json(201, { jobId: "j", status: "pending" });
      if (n === 2) return json(200, { jobId: "j", status: "completed" });
      return new Response("PPTX", { status: 200 });
    });
    await client(f).convert(await images(2), join(dir, "out.pptx"), {
      pages: "2",
      callbackUrl: "https://example.com/h",
      idempotencyKey: "k1",
      pollIntervalMs: 0,
    });
    expect(await formFields(f)).toEqual({ pages: "2", callbackUrl: "https://example.com/h" });
    expect(headersOf(f)["Idempotency-Key"]).toBe("k1");
  });

  it("lets submitAll take a callback but not pages or a key", async () => {
    const f = fetchScript(created());
    const paths = await images(2);
    await client(f).submitAll(paths, { callbackUrl: "https://example.com/h" });
    expect(await formFields(f)).toEqual({ callbackUrl: "https://example.com/h" });
    for (const extra of [{ pages: "1" }, { idempotencyKey: "k" }]) {
      expect(
        await rejection(client(f).submitAll(paths, extra as unknown as Record<string, string>)),
      ).toBeInstanceOf(TypeError);
      expect(
        await rejection(
          client(f).convertAll(paths, join(dir, "never-created"), extra as unknown as Record<string, string>),
        ),
      ).toBeInstanceOf(TypeError);
    }
  });

  it("resends a rate-limited batch under the same key", async () => {
    const f = fetchScript((n) =>
      n === 1 ? error(429, "RATE_LIMITED", {}, { "Retry-After": "3" }) : created()(),
    );
    const paths = await images(2);
    await withoutSleeping(() => client(f).submitAll(paths));
    expect(f.calls).toHaveLength(2);
    expect(headersOf(f, 0)["Idempotency-Key"]).toBe(headersOf(f, 1)["Idempotency-Key"]);
  });
});

// --------------------------------------------------------------------------- //
// submitUrls
// --------------------------------------------------------------------------- //
describe("submitUrls", () => {
  it("posts JSON with the key", async () => {
    const f = fetchScript(created());
    const job = await client(f, 30_000).submitUrls(
      ["https://example.com/a.png", "https://example.com/b.pdf"],
      {
        locale: "en",
        aspectRatio: "16:9",
        pages: "1-2",
        callbackUrl: "https://example.com/h",
        idempotencyKey: "k",
      },
    );
    expect(job.jobId).toBe("job_1");
    const call = f.calls[0]!;
    expect(call.url).toMatch(/\/api\/v1\/jobs$/);
    expect(JSON.parse(Buffer.from(call.init.body as Uint8Array).toString("utf8"))).toEqual({
      urls: ["https://example.com/a.png", "https://example.com/b.pdf"],
      locale: "en",
      aspectRatio: "16:9",
      pages: "1-2",
      callbackUrl: "https://example.com/h",
    });
    expect(headersOf(f)).toMatchObject({
      "Idempotency-Key": "k",
      "Content-Type": "application/json",
    });
  });

  it("outwaits the client's own idle timeout while the service downloads", async () => {
    // A fetch that honours the abort signal and answers only after 150ms. With a
    // 50ms idle timeout an upload would be given up on; a submission by URL must not.
    const slowAnswer = (init: RequestInit): Promise<Response> =>
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => resolve(created()()), 150);
        init.signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(init.signal!.reason);
        });
      });
    const f = fetchScript(() => slowAnswer(f.calls[f.calls.length - 1]!.init));
    const job = await client(f, 50).submitUrls(["https://example.com/a"]);
    expect(job.jobId).toBe("job_1");
    expect(f.calls).toHaveLength(1);
  });

  it("refuses bad input locally", async () => {
    const f = fetchScript(created());
    const c = client(f);
    expect(await rejection(c.submitUrls([]))).toBeInstanceOf(Error);
    expect(
      await rejection(c.submitUrls(Array(51).fill("https://example.com/x"))),
    ).toBeInstanceOf(InvalidParameterError);
    expect(
      await rejection(c.submitUrls("https://example.com/x" as unknown as string[])),
    ).toBeInstanceOf(TypeError);
    expect(
      await rejection(c.submitUrls(["https://example.com/x"], { pages: "1-51" })),
    ).toBeInstanceOf(TooManySlidesError);
    expect(f.calls).toHaveLength(0);
  });

  it("says which link failed", async () => {
    const f = fetchScript(() => error(400, "URL_FETCH_FAILED", { index: 1 }));
    const err = (await rejection(
      client(f).submitUrls(["https://example.com/a", "https://example.com/b"]),
    )) as UrlFetchFailedError;
    expect(err).toBeInstanceOf(UrlFetchFailedError);
    expect(err.index).toBe(1);
    expect(err.isTransient).toBe(true);
    expect(err.idempotencyKey).toBeDefined();
  });
});

// --------------------------------------------------------------------------- //
// error envelope: new codes, index
// --------------------------------------------------------------------------- //
describe("error envelope", () => {
  it.each([
    [400, "INVALID_CALLBACK_URL", InvalidCallbackUrlError],
    [400, "INVALID_IDEMPOTENCY_KEY", InvalidIdempotencyKeyError],
    [422, "IDEMPOTENCY_KEY_MISMATCH", IdempotencyKeyMismatchError],
    [400, "INVALID_JSON", InvalidParameterError],
    [400, "INVALID_PARAMETER", InvalidParameterError],
    [400, "INVALID_URL", InvalidUrlError],
    [400, "URL_FETCH_FAILED", UrlFetchFailedError],
    [400, "INVALID_PAGES", InvalidPagesError],
    [400, "PAGES_OUT_OF_RANGE", PagesOutOfRangeError],
  ] as const)("maps %d %s to its own type", async (status, code, cls) => {
    const f = fetchScript(() => error(status, code));
    const err = (await rejection(client(f).listJobs())) as Image2PPTError;
    expect(err).toBeInstanceOf(cls);
    expect(err.code).toBe(code);
    expect(err.statusCode).toBe(status);
    expect(err.index).toBeUndefined();
  });

  it("carries Retry-After on in-progress", async () => {
    const f = fetchScript(() =>
      error(409, "IDEMPOTENCY_KEY_IN_PROGRESS", {}, { "Retry-After": "2" }),
    );
    const err = (await rejection(client(f).getJob("j"))) as IdempotencyKeyInProgressError;
    expect(err).toBeInstanceOf(IdempotencyKeyInProgressError);
    expect(err.retryAfter).toBe(2);
  });

  it.each([
    [0, 0],
    [3, 3],
    [3.0, 3],
    [-1, undefined],
    ["1", undefined],
    [true, undefined],
    [1.5, undefined],
  ])("keeps only a whole non-negative index: %j", async (raw, parsed) => {
    const f = fetchScript(() => error(400, "INVALID_URL", { index: raw }));
    const err = (await rejection(client(f).submitUrls(["https://example.com/a"]))) as InvalidUrlError;
    expect(err).toBeInstanceOf(InvalidUrlError);
    expect(err.index).toBe(parsed);
  });

  it("keeps a downloaded file's old error type and adds the index", async () => {
    const f = fetchScript(() => error(400, "INVALID_FILE", { index: 0 }));
    const err = (await rejection(client(f).submitUrls(["https://example.com/a"]))) as InvalidFileError;
    expect(err).toBeInstanceOf(InvalidFileError);
    expect(err.index).toBe(0);
  });
});

// --------------------------------------------------------------------------- //
// job fields: callback, replayed
// --------------------------------------------------------------------------- //
describe("job fields", () => {
  it("reads the callback status", () => {
    const job = Job.fromJson({
      jobId: "j",
      status: "completed",
      callback: {
        url: "https://example.com/hook",
        status: "pending",
        attempts: 1,
        lastAttemptAt: "2026-10-01 08:00:00",
        lastResponseStatus: 503,
        nextAttemptAt: "2026-10-01 08:01:00",
      },
    });
    expect(job.callback).toBeInstanceOf(CallbackStatus);
    expect(job.callback).toMatchObject({
      url: "https://example.com/hook",
      status: "pending",
      attempts: 1,
      lastAttemptAt: "2026-10-01 08:00:00",
      lastResponseStatus: 503,
      nextAttemptAt: "2026-10-01 08:01:00",
    });
    expect(job.replayed).toBe(false);
  });

  it("reads a callback field of the wrong type as null", () => {
    const job = Job.fromJson({
      jobId: "j",
      status: "pending",
      callback: { status: "delivered", attempts: "2", lastResponseStatus: null },
    });
    expect(job.callback?.status).toBe("delivered");
    expect(job.callback?.attempts).toBeNull();
    expect(job.callback?.lastResponseStatus).toBeNull();
    expect(Job.fromJson({ jobId: "j", status: "pending", callback: "x" }).callback).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// listJobs / iterJobs
// --------------------------------------------------------------------------- //
describe("listJobs", () => {
  it("sends only the given params and parses the page", async () => {
    const f = fetchScript(() =>
      json(200, {
        data: [{ jobId: "a", status: "completed", creditsUsed: 3 }],
        nextCursor: "c2",
      }),
    );
    const page = await client(f).listJobs({
      createdFrom: "2026-10-01",
      createdTo: "",
      limit: 0,
      cursor: "c1",
    });
    const url = new URL(f.calls[0]!.url);
    expect(url.pathname).toBe("/api/v1/jobs");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      createdFrom: "2026-10-01",
      limit: "0",
      cursor: "c1",
    });
    expect(page).toBeInstanceOf(JobList);
    expect(page.data.map((job) => job.jobId)).toEqual(["a"]);
    expect(page.data[0]!.creditsUsed).toBe(3);
    expect(page.nextCursor).toBe("c2");
  });

  it("calls a list without an array malformed", async () => {
    const f = fetchScript(() => json(200, { data: {} }));
    expect(await rejection(client(f).listJobs())).toBeInstanceOf(MalformedResponseError);
  });

  it("follows the cursor and waits out rate limits", async () => {
    const responses = [
      () => json(200, { data: [{ jobId: "a", status: "completed" }], nextCursor: "c2" }),
      () => error(429, "RATE_LIMITED", {}, { "Retry-After": "4" }),
      () => new Response("", { status: 502 }),
      () => json(200, { data: [{ jobId: "b", status: "failed" }], nextCursor: null }),
    ];
    const f = fetchScript((n) => responses[n - 1]!());
    const { result: ids, delays } = await withoutSleeping(async () => {
      const seen: string[] = [];
      for await (const job of client(f).iterJobs({ limit: 1 })) seen.push(job.jobId);
      return seen;
    });
    expect(ids).toEqual(["a", "b"]);
    expect(f.calls.map((call) => Object.fromEntries(new URL(call.url).searchParams))).toEqual([
      { limit: "1" },
      { limit: "1", cursor: "c2" },
      { limit: "1", cursor: "c2" },
      { limit: "1", cursor: "c2" },
    ]);
    expect(delays).toEqual([4_000, 1_000]);
  });

  async function drain(c: Image2PPTClient, limit?: number): Promise<void> {
    for await (const _ of c.iterJobs(limit === undefined ? {} : { limit })) {
      // just walk it
    }
  }

  it("gives up after ten attempts on a page", async () => {
    const f = fetchScript(() => new Response("", { status: 503 }));
    const { result: err } = await withoutSleeping(() => rejection(drain(client(f))));
    expect(err).toBeInstanceOf(ServerError);
    expect(f.calls).toHaveLength(10);
  });

  it("does not retry a bad parameter", async () => {
    const f = fetchScript(() => error(400, "INVALID_PARAMETER"));
    const { result: err } = await withoutSleeping(() => rejection(drain(client(f), 500)));
    expect(err).toBeInstanceOf(InvalidParameterError);
    expect(f.calls).toHaveLength(1);
  });

  it("gives up on rate limits too", async () => {
    const f = fetchScript(() => error(429, "RATE_LIMITED"));
    const { result: err } = await withoutSleeping(() => rejection(drain(client(f))));
    expect(err).toBeInstanceOf(RateLimitedError);
    expect(f.calls).toHaveLength(10);
  });

  it("still lets a dropped connection on a single list call through at once", async () => {
    const f = fetchScript(() => {
      throw new TypeError("fetch failed");
    });
    expect(await rejection(client(f).listJobs())).toBeInstanceOf(APIConnectionError);
    expect(f.calls).toHaveLength(1);
  });
});

// --------------------------------------------------------------------------- //
// verifyWebhook — Standard Webhooks
// --------------------------------------------------------------------------- //
// The official test vector from the Standard Webhooks reference libraries.
const VECTOR_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw";
const VECTOR_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek";
const VECTOR_TS = 1614265330;
const VECTOR_BODY = Buffer.from('{"test": 2432232314}');
const VECTOR_SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=";

function vectorHeaders(overrides: Record<string, string> = {}): Record<string, string> {
  return {
    "webhook-id": VECTOR_ID,
    "webhook-timestamp": String(VECTOR_TS),
    "webhook-signature": VECTOR_SIG,
    ...overrides,
  };
}

function sign(secret: string, msgId: string, ts: number, body: Buffer): string {
  const key = Buffer.from(secret.slice("whsec_".length), "base64");
  return (
    "v1," +
    createHmac("sha256", key).update(`${msgId}.${ts}.`).update(body).digest("base64")
  );
}

describe("verifyWebhook", () => {
  it("verifies the official vector", () => {
    const event = verifyWebhook(VECTOR_BODY, vectorHeaders(), VECTOR_SECRET, { now: VECTOR_TS });
    expect(event.id).toBe(VECTOR_ID);
    expect(event.timestamp).toBe(VECTOR_TS);
    expect(event.raw).toEqual({ test: 2432232314 });
    expect(event.type).toBeNull();
    expect(event.data).toEqual({});
  });

  it("verifies a text payload, mixed-case headers, and a fetch Headers object", () => {
    const mixed = Object.fromEntries(
      Object.entries(vectorHeaders()).map(([k, v]) => [k.replace(/\b\w/g, (c) => c.toUpperCase()), v]),
    );
    verifyWebhook(VECTOR_BODY.toString("utf8"), mixed, VECTOR_SECRET, { now: VECTOR_TS });
    verifyWebhook(new Uint8Array(VECTOR_BODY), new Headers(vectorHeaders()), VECTOR_SECRET, {
      now: VECTOR_TS,
    });
  });

  it("joins an array header value", () => {
    verifyWebhook(
      VECTOR_BODY,
      { ...vectorHeaders(), "webhook-signature": ["v2,abc", VECTOR_SIG] },
      VECTOR_SECRET,
      { now: VECTOR_TS },
    );
  });

  it("accepts a bare base64 secret", () => {
    verifyWebhook(VECTOR_BODY, vectorHeaders(), VECTOR_SECRET.slice("whsec_".length), {
      now: VECTOR_TS,
    });
  });

  it("fails a tampered body", () => {
    expect(() =>
      verifyWebhook(Buffer.from('{"test": 2432232315}'), vectorHeaders(), VECTOR_SECRET, {
        now: VECTOR_TS,
      }),
    ).toThrow(WebhookVerificationError);
  });

  it.each([-301, 301])("fails a timestamp %d seconds off", (skew) => {
    expect(() =>
      verifyWebhook(VECTOR_BODY, vectorHeaders(), VECTOR_SECRET, { now: VECTOR_TS + skew }),
    ).toThrow(/tolerance/);
  });

  it.each([-300, 300])("keeps the window edge %d inside", (skew) => {
    verifyWebhook(VECTOR_BODY, vectorHeaders(), VECTOR_SECRET, { now: VECTOR_TS + skew });
  });

  it("passes when any one v1 signature among several matches", () => {
    const wrong = "v1,Zm9vYmFy" + "A".repeat(34);
    verifyWebhook(
      VECTOR_BODY,
      vectorHeaders({ "webhook-signature": `v2,abc ${wrong}  ${VECTOR_SIG}` }),
      VECTOR_SECRET,
      { now: VECTOR_TS },
    );
  });

  it("fails a non-v1 signature alone", () => {
    expect(() =>
      verifyWebhook(
        VECTOR_BODY,
        vectorHeaders({ "webhook-signature": VECTOR_SIG.replace("v1,", "v2,") }),
        VECTOR_SECRET,
        { now: VECTOR_TS },
      ),
    ).toThrow(WebhookVerificationError);
  });

  it("fails a non-ASCII signature cleanly", () => {
    expect(() =>
      verifyWebhook(VECTOR_BODY, vectorHeaders({ "webhook-signature": "v1,签名" }), VECTOR_SECRET, {
        now: VECTOR_TS,
      }),
    ).toThrow(WebhookVerificationError);
  });

  it.each(["webhook-id", "webhook-timestamp", "webhook-signature"])(
    "fails without %s",
    (missing) => {
      const headers = vectorHeaders();
      delete headers[missing];
      expect(() =>
        verifyWebhook(VECTOR_BODY, headers, VECTOR_SECRET, { now: VECTOR_TS }),
      ).toThrow(/missing/);
    },
  );

  it.each(["1614265330.0", "-1", "１６１４２６５３３０", "abc"])(
    "fails the timestamp %j",
    (ts) => {
      expect(() =>
        verifyWebhook(VECTOR_BODY, vectorHeaders({ "webhook-timestamp": ts }), VECTOR_SECRET, {
          now: VECTOR_TS,
        }),
      ).toThrow(WebhookVerificationError);
    },
  );

  it.each(["whsec_", "whsec_not base64!", "whsec_abc", 123])(
    "calls the secret %j a configuration error",
    (secret) => {
      let caught: unknown;
      try {
        verifyWebhook(VECTOR_BODY, vectorHeaders(), secret as string, { now: VECTOR_TS });
      } catch (err) {
        caught = err;
      }
      expect(caught).toBeInstanceOf(TypeError);
      expect(caught).not.toBeInstanceOf(WebhookVerificationError);
    },
  );

  it("refuses an already parsed body", () => {
    expect(() =>
      verifyWebhook({ test: 2432232314 } as unknown as string, vectorHeaders(), VECTOR_SECRET, {
        now: VECTOR_TS,
      }),
    ).toThrow(/raw request body/);
  });

  it("exposes a real event's job", () => {
    const secret = "whsec_" + Buffer.from(Array.from({ length: 32 }, (_, i) => i)).toString("base64");
    const body = Buffer.from(
      JSON.stringify({
        type: "job.completed",
        data: { jobId: "j", status: "completed", creditsUsed: 2 },
      }),
    );
    const event = verifyWebhook(
      body,
      {
        "webhook-id": "msg_1",
        "webhook-timestamp": "1700000000",
        "webhook-signature": sign(secret, "msg_1", 1700000000, body),
      },
      secret,
      { now: 1700000010 },
    );
    expect(event.type).toBe("job.completed");
    expect(event.job.jobId).toBe("j");
    expect(event.job.creditsUsed).toBe(2);
  });

  it("fails a signed body that is not an object", () => {
    const secret = "whsec_" + Buffer.alloc(32).toString("base64");
    const body = Buffer.from("[1, 2]");
    expect(() =>
      verifyWebhook(
        body,
        {
          "webhook-id": "m",
          "webhook-timestamp": "100",
          "webhook-signature": sign(secret, "m", 100, body),
        },
        secret,
        { now: 100 },
      ),
    ).toThrow(/JSON object/);
  });
});

// --------------------------------------------------------------------------- //
// fixes from the PR review round
// --------------------------------------------------------------------------- //
describe("review-round fixes", () => {
  it("a file error during a resend still carries the key", async () => {
    const pdf = join(dir, "deck.pdf");
    await writeFile(pdf, "%PDF-1.4 not really");
    let n = 0;
    const f = fetchScript(async () => {
      n += 1;
      await rm(pdf, { force: true }); // gone before the resend reopens it
      throw new TypeError("fetch failed");
    });
    const err = (await withoutSleeping(() =>
      rejection(client(f).submit([pdf], { idempotencyKey: "order-7" })),
    )).result as { idempotencyKey?: string };
    expect(err.idempotencyKey).toBe("order-7");
    expect(n).toBe(3); // the file is read while sending, so every attempt gets that far
  });

  it("submitUrls resends under the same generated key", async () => {
    const f = fetchScript((n) => (n === 1 ? new Response("", { status: 503 }) : created("j")()));
    await withoutSleeping(() => client(f).submitUrls(["https://example.com/a.png"]));
    const keys = f.calls.map((call) => headersOf(f, f.calls.indexOf(call))["Idempotency-Key"]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
    expect(f.calls[0]!.init.body).toBe(f.calls[1]!.init.body);
  });

  it("surrounding whitespace does not count towards the length", () => {
    expect(parsePages("1" + " ".repeat(1000))).toEqual([[1, 1]]);
  });

  it("a known total reports out of range before too many", async () => {
    const f = fetchScript(created());
    expect(await rejection(client(f).submit(await images(3), { pages: "1-60" }))).toBeInstanceOf(
      PagesOutOfRangeError,
    );
    expect(f.calls).toHaveLength(0);
  });

  it.each([
    { toleranceSeconds: Number.NaN, now: VECTOR_TS },
    { toleranceSeconds: -1, now: VECTOR_TS },
    { now: Number.NaN },
    { now: Number.POSITIVE_INFINITY },
  ])("clock options must be finite: %o", (options) => {
    expect(() => verifyWebhook(VECTOR_BODY, vectorHeaders(), VECTOR_SECRET, options)).toThrow(
      TypeError,
    );
  });

  it.each([13, 309, 5000])("an absurdly long timestamp (%i digits) is a bad delivery", (digits) => {
    expect(() =>
      verifyWebhook(
        VECTOR_BODY,
        vectorHeaders({ "webhook-timestamp": "1".repeat(digits) }),
        VECTOR_SECRET,
      ),
    ).toThrow(WebhookVerificationError);
  });
});

describe("convert", () => {
  it("hands back the key when the job fails after submission", async () => {
    const f = fetchScript((n) =>
      n === 1
        ? created("j")()
        : json(200, { jobId: "j", status: "failed", error: { code: "CONVERSION_FAILED", message: "x" } }),
    );
    const err = (await rejection(
      client(f).convert(await images(1), join(dir, "out.pptx"), { pollIntervalMs: 0 }),
    )) as { idempotencyKey?: string };
    expect(err.idempotencyKey).toBe(headersOf(f, 0)["Idempotency-Key"]);
    expect(err.idempotencyKey).toBeTruthy();
  });
});
