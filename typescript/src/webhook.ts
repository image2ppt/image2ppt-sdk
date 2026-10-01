/**
 * Verifying completion callbacks (the Standard Webhooks scheme,
 * https://www.standardwebhooks.com).
 *
 * A job submitted with `callbackUrl` is announced by a `POST` to that URL when it
 * ends. Anyone can send a POST, so check every delivery with `verifyWebhook` before
 * acting on it:
 *
 * ```ts
 * // Express: the raw body, not express.json()
 * app.post("/hook", express.raw({ type: "application/json" }), (req, res) => {
 *   let event;
 *   try {
 *     event = verifyWebhook(req.body, req.headers, process.env.IMAGE2PPT_WEBHOOK_SECRET!);
 *   } catch (err) {
 *     if (err instanceof WebhookVerificationError) return res.sendStatus(400);
 *     throw err;
 *   }
 *   if (event.type === "job.completed") { ... }
 *   res.sendStatus(204); // any 2xx within 10 seconds counts as delivered
 * });
 * ```
 *
 * Every rule here is pinned identically in the Python client.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

import { WebhookVerificationError } from "./errors.js";
import { Job, isJsonObject } from "./types.js";

/** How far `webhook-timestamp` may be from the receiver's clock, either way. */
export const DEFAULT_TOLERANCE_SECONDS = 300;

const SECRET_PREFIX = "whsec_";
/**
 * Strict standard base64. `Buffer.from(x, "base64")` silently skips any character it
 * does not know, while Python's decoder refuses it — so the secret is checked against
 * the alphabet first and both clients agree on which secrets are malformed.
 */
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const TIMESTAMP = /^[0-9]+$/;
/** Unix seconds stay within 12 digits for tens of thousands of years; longer is refused. */
const MAX_TIMESTAMP_DIGITS = 12;

/** Anything a framework hands over as request headers. */
export type WebhookHeaders =
  | Headers
  | Record<string, string | string[] | undefined | null>;

export interface VerifyWebhookOptions {
  /** Largest allowed gap between `webhook-timestamp` and now, either way. Default 300. */
  toleranceSeconds?: number;
  /** Current Unix time in seconds, for tests; defaults to the clock. */
  now?: number;
}

/**
 * A verified callback.
 *
 * `type` is `job.completed` or `job.failed` today; **ignore types you do not
 * recognise** (still answer 2xx) — more may be added. `data` is the job, shaped like
 * `getJob`'s response without `callback`; `job` parses it for you. `id` stays the
 * same across retries of one delivery, so use it to drop duplicates.
 */
export class WebhookEvent {
  constructor(
    readonly id: string,
    readonly timestamp: number,
    readonly type: string | null,
    readonly data: Record<string, unknown>,
    readonly raw: Record<string, unknown>,
  ) {}

  /** `data` as a `Job`. Throws `MalformedResponseError` if it is not one. */
  get job(): Job {
    return Job.fromJson(this.data);
  }
}

/**
 * Check a callback's signature and timestamp; return the event it carries.
 *
 * @param payload The request body **exactly as received** — a Buffer/Uint8Array, or
 *   the same text decoded as UTF-8. Parsing the JSON and re-serialising it changes the
 *   bytes and the signature will not match; with Express use `express.raw()`.
 * @param headers The request headers: a fetch `Headers`, or a plain record such as
 *   Node's `req.headers`. Names are matched ignoring case.
 * @param secret The callback signing secret from the Developer / API page
 *   (`whsec_...`). During a rotation the service signs with the old and the new one
 *   for 24 hours, so a receiver holding either passes.
 * @throws WebhookVerificationError Missing header, timestamp outside the tolerance, no
 *   matching `v1` signature, or a verified body that is not a JSON object. Refuse the
 *   delivery.
 * @throws TypeError `secret` is not a `whsec_` + base64 secret — a configuration
 *   mistake on the receiving side, not a bad delivery, so it is kept apart: answering
 *   every genuine callback with 4xx would hide it. Also thrown when `payload` is not
 *   bytes or text, typically a body a framework has already parsed.
 */
export function verifyWebhook(
  payload: Uint8Array | string,
  headers: WebhookHeaders,
  secret: string,
  options: VerifyWebhookOptions = {},
): WebhookEvent {
  const key = decodeSecret(secret);
  const body = payloadBytes(payload);
  const tolerance = options.toleranceSeconds ?? DEFAULT_TOLERANCE_SECONDS;
  if (typeof tolerance !== "number" || !Number.isFinite(tolerance) || tolerance < 0) {
    throw new TypeError("toleranceSeconds must be a finite, non-negative number");
  }
  if (options.now !== undefined && (typeof options.now !== "number" || !Number.isFinite(options.now))) {
    throw new TypeError("now must be a finite number of Unix seconds");
  }

  const msgId = header(headers, "webhook-id");
  const timestamp = header(headers, "webhook-timestamp");
  const signatures = header(headers, "webhook-signature");
  if (!msgId || !timestamp || !signatures) {
    throw new WebhookVerificationError(
      "missing webhook-id, webhook-timestamp or webhook-signature header",
    );
  }
  if (!TIMESTAMP.test(timestamp)) {
    throw new WebhookVerificationError(
      `webhook-timestamp ${JSON.stringify(timestamp)} is not Unix seconds`,
    );
  }
  if (timestamp.length > MAX_TIMESTAMP_DIGITS) {
    // Far outside any window, and too long to do arithmetic on safely.
    throw new WebhookVerificationError("webhook-timestamp is outside the tolerance");
  }
  const sentAt = Number(timestamp);
  const current = options.now ?? Date.now() / 1000;
  if (Math.abs(current - sentAt) > tolerance) {
    throw new WebhookVerificationError(
      `webhook-timestamp is ${Math.trunc(current - sentAt)}s from now, outside the ` +
        `${tolerance}s tolerance`,
    );
  }

  const expected = Buffer.from(
    createHmac("sha256", key)
      .update(Buffer.from(`${msgId}.${timestamp}.`, "utf8"))
      .update(body)
      .digest("base64"),
    "utf8",
  );
  // Split on the ASCII space the scheme uses, nothing wider: a /\s+/ and Python's
  // bare split() disagree about what whitespace is.
  for (const entry of signatures.split(" ")) {
    const comma = entry.indexOf(",");
    if (comma === -1 || entry.slice(0, comma) !== "v1") continue;
    const signature = Buffer.from(entry.slice(comma + 1), "utf8");
    // timingSafeEqual throws on a length mismatch, so compare lengths first; a
    // length is no secret.
    if (signature.length === expected.length && timingSafeEqual(signature, expected)) {
      return toEvent(msgId, sentAt, body);
    }
  }
  throw new WebhookVerificationError("no webhook-signature matches this payload and secret");
}

/** The HMAC key: the base64 after `whsec_` (a bare base64 secret is accepted too). */
function decodeSecret(secret: unknown): Buffer {
  if (typeof secret !== "string") {
    throw new TypeError(`secret must be a string, not ${typeof secret}`);
  }
  const encoded = secret.startsWith(SECRET_PREFIX) ? secret.slice(SECRET_PREFIX.length) : secret;
  if (!encoded || !BASE64.test(encoded)) {
    throw new TypeError("secret is not a webhook signing secret (expected whsec_<base64>)");
  }
  return Buffer.from(encoded, "base64");
}

function payloadBytes(payload: unknown): Buffer {
  if (typeof payload === "string") return Buffer.from(payload, "utf8");
  if (payload instanceof Uint8Array) {
    return Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength);
  }
  throw new TypeError(
    "payload must be the raw request body (Buffer, Uint8Array or string), not " +
      `${payload === null ? "null" : typeof payload} — with Express, use express.raw() ` +
      "instead of express.json() for this route",
  );
}

/** Case-insensitive lookup; a list of values is joined by spaces. */
function header(headers: WebhookHeaders, name: string): string | undefined {
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    return headers.get(name) ?? undefined;
  }
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() !== name) continue;
    if (Array.isArray(value)) return value.map(String).join(" ");
    return value == null ? undefined : String(value);
  }
  return undefined;
}

function toEvent(msgId: string, sentAt: number, body: Buffer): WebhookEvent {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body.toString("utf8"));
  } catch {
    parsed = undefined;
  }
  if (!isJsonObject(parsed)) {
    throw new WebhookVerificationError("signature matches, but the body is not a JSON object");
  }
  return new WebhookEvent(
    msgId,
    sentAt,
    typeof parsed.type === "string" ? parsed.type : null,
    isJsonObject(parsed.data) ? parsed.data : {},
    parsed,
  );
}
