/** Shared types and the Job model. */

import { MalformedResponseError } from "./errors.js";

export type Locale = "zh-CN" | "en";
export type AspectRatio = "auto" | "16:9" | "4:3";
export type JobStatus = "pending" | "processing" | "completed" | "failed";

/**
 * Outcome of a single page. `converted`: it became editable content.
 * `failed`: it did not.
 *
 * A value this client does not recognise is passed through as it arrives rather
 * than being folded into one of these — the service may add one, and losing it
 * would be worse than seeing it.
 */
export type PageStatus = "converted" | "failed";

/**
 * A response envelope as an object, with every field the contract guarantees.
 *
 * One helper for all three envelopes — a job, a cancellation result, a page entry.
 * The check used to be hand-written at each of them, and the copies drifted apart
 * from each other and from the Python client's, which is exactly the kind of
 * disagreement two clients for one API cannot afford.
 *
 * **A field counts as missing when the key is absent or its value is null.** The
 * two mean the same thing to a caller: there is no value to act on.
 *
 * The test is `== null`, which catches null and undefined and nothing else. A
 * falsiness test would look almost identical and be wrong: `cancellationRequested`
 * and `finalizing` are booleans whose `false` is a real answer the service sends,
 * and rejecting it would fail the response that says "no, the job is not still
 * winding down".
 *
 * `what` names the envelope for the message ("job response").
 */
export function isJsonObject(value: unknown): value is Record<string, any> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireFields(
  data: unknown,
  keys: readonly string[],
  what: string,
): Record<string, any> {
  if (!isJsonObject(data)) {
    throw new MalformedResponseError(`malformed ${what}, expected a JSON object`);
  }
  const d = data as Record<string, any>;
  for (const key of keys) {
    if (d[key] == null) {
      throw new MalformedResponseError(`malformed ${what}, missing ${key}`);
    }
  }
  return d;
}

export interface JobError {
  /**
   * Job-level failure reason: `JOB_CANCELLED` or `CONVERSION_FAILED`.
   *
   * These two are the whole set and are meant to stay that way — the finer
   * reasons live per page, in `pageResults`. Treat a code you do not recognise
   * as `CONVERSION_FAILED`.
   */
  code: string;
  /**
   * A sentence written for a person to read. Its language follows the client's
   * `acceptLanguage` option. **Do not branch on it** — branch on `code`.
   */
  message: string;
}

/** Why one page did not convert. */
export class PageError {
  /**
   * Per-page failure reason. The contract's set is exactly `CONVERSION_FAILED`,
   * `CONVERSION_TIMEOUT` and `PAGE_NOT_ATTEMPTED`; treat anything else as
   * `CONVERSION_FAILED`.
   *
   * `PAGE_NOT_ATTEMPTED` is the one that changes what you do. That page never
   * started and **is not in the delivered deck at all**. Every other failed page
   * *is* in the deck — as the original image, not as editable content.
   */
  readonly code: string;
  /**
   * A sentence written for a person to read, in the language asked for by the
   * client's `acceptLanguage`. **Do not branch on it** — branch on `code`.
   */
  readonly message: string;
  /**
   * Whether submitting the same image again could succeed. Every code the
   * service returns today says true, but **branch on this field rather than
   * hardcoding it**: a code added later may carry false.
   */
  readonly retryable: boolean;
  /** Raw entry, for forward-compatible access to fields added later. */
  readonly raw: Record<string, unknown>;

  constructor(data: Record<string, unknown>) {
    const d = data as Record<string, any>;
    // Lenient on purpose: `pageResults` reports what went wrong, and a gap in the
    // report is not itself worth turning into a thrown error the caller has to
    // handle. Every rule below is pinned identically in the Python client — one
    // body must not mean two different things depending on which SDK read it.
    //
    // A code has to be a non-empty string. An empty one names no failure, so it
    // falls back the same way an unrecognised one does, which is the contract's
    // own instruction to callers.
    this.code = typeof d.code === "string" && d.code !== "" ? d.code : "CONVERSION_FAILED";
    // A message has to be a string. A number is not turned into one: `42` is not a
    // sentence anybody wants to show a person.
    this.message = typeof d.message === "string" ? d.message : "";
    // A real boolean, never truthiness. A flag we cannot read is not a licence to
    // re-upload, so anything else — a missing field included — is false: telling a
    // caller "try this page again" on a guess costs them credits, telling them
    // "don't" costs them nothing they cannot recover by asking again themselves.
    this.retryable = d.retryable === true;
    this.raw = data;
  }
}

/** What happened to one page of the deck. */
export class PageResult {
  /**
   * 1-based, and counts **pages of the delivered deck**. Without `pages` that is the
   * order the files were submitted in (a PDF follows its own page order). With
   * `pages` it is the k-th *selected* page, not the page number you selected:
   * `pages: "3,7"` gives entries 1 and 2.
   */
  readonly pageNumber: number;
  /** `converted` or `failed`; an unrecognised value is passed through as-is. */
  readonly status: PageStatus;
  /** Present only when `status` is `failed`. */
  readonly error?: PageError;
  /** Raw entry, for forward-compatible access to fields added later. */
  readonly raw: Record<string, unknown>;

  constructor(data: Record<string, unknown>) {
    // `pageNumber` and `status` are guaranteed by the contract and are the two
    // fields a caller has to have: without them an entry says nothing about which
    // page it is or how that page ended. Their *types* are checked too rather than
    // cast through — a page number of `"3"` would otherwise be quietly converted,
    // covering up a wrong type instead of reporting it.
    const d = requireFields(data, ["pageNumber", "status"], "pageResults entry");
    // A whole number, not merely a finite one: pages are counted, and `1.5` names
    // no page. `Number.isInteger` also rejects a numeric *string*, which is the
    // half of this the Python client had to be brought into line with.
    if (!Number.isInteger(d.pageNumber)) {
      throw new MalformedResponseError(
        `malformed pageResults entry, pageNumber is not a number: ${JSON.stringify(d.pageNumber)}`,
      );
    }
    if (typeof d.status !== "string") {
      throw new MalformedResponseError(
        `malformed pageResults entry, status is not a string: ${JSON.stringify(d.status)}`,
      );
    }
    this.pageNumber = d.pageNumber;
    this.status = d.status as PageStatus;
    // An `error` that is present but is not an object says nothing this model could
    // report, so it reads as absent rather than throwing: the entry itself is what
    // the contract guarantees, and a surprise *inside* `error` should not cost the
    // caller the rest of the ledger. Building a fully-defaulted `PageError` from it
    // instead would put a `CONVERSION_FAILED` on the page that nobody sent.
    this.error = isJsonObject(d.error) ? new PageError(d.error) : undefined;
    this.raw = data;
  }
}

export interface ClientOptions {
  /** Your API key (looks like `i2p_live_...`). */
  apiKey: string;
  /** Service base URL, defaults to `https://image2ppt.com`. */
  baseUrl?: string;
  /**
   * How long one request may sit with **no data moving**, in ms (default 60000).
   *
   * An idle timeout, not a cap on how long a request may take: a 40MB upload or a
   * large PPTX download that keeps making progress is never cut off, however long
   * it runs in total. The clock only starts when nothing has been sent or received.
   * Nothing at all for this long — including waiting for a response that never
   * begins — throws `APITimeoutError`.
   *
   * This is one HTTP request, not the whole-job wait; that one is `WaitOptions`.
   */
  timeoutMs?: number;
  /** Inject a custom fetch (for testing). Defaults to the global `fetch` (Node 18+). */
  fetch?: typeof fetch;
  /**
   * Language for the **error messages** the service sends back, as an
   * `Accept-Language` header value (e.g. `"zh-CN"`, `"fr, en;q=0.8"`). Sent
   * verbatim on every request. Unset by default, and then no header is sent at
   * all — which the service answers in English.
   *
   * **This is not `SubmitOptions.locale`, and the two are easy to confuse.**
   * `locale` decides what language the generated PPTX is written in.
   * `acceptLanguage` decides what language a failure is explained to *you* in.
   * They are unrelated: you can ask for an English deck and Chinese errors. That
   * is also why this is a free-form string rather than the `Locale` union — it is
   * an HTTP header value, and the header's own syntax (several languages, quality
   * weights) is what belongs in it.
   *
   * Either way, branch on `code`, never on `message`.
   */
  acceptLanguage?: string;
  /**
   * Total ms `submitAll` / `convertAll` may spend **waiting out rate limits** across
   * the whole call (default 1_800_000 = 30 min). Only waiting counts against it — the
   * time the uploads themselves take does not, so a slow link cannot quietly turn this
   * into "do not wait at all". Submitting a large pile *will* hit the per-minute page
   * quota, so waiting is the normal path, not an error.
   */
  rateLimitMaxWaitMs?: number;
  /**
   * When the service marks this SDK version deprecated, `console.warn` once.
   * Default true. Set to false to silence it.
   */
  warnOnDeprecated?: boolean;
}

/** Options every kind of submission takes, including the batch calls. */
export interface SubmitAllOptions {
  /**
   * Language of the **generated deck**: `zh-CN` (default) or `en`.
   *
   * Not to be confused with the client-level `acceptLanguage`, which only sets
   * what language error messages come back in.
   */
  locale?: Locale;
  /** `auto` (default) / `16:9` / `4:3`. */
  aspectRatio?: AspectRatio;
  /**
   * An `https` URL the service POSTs to when the job ends. Check each delivery with
   * `verifyWebhook`. With `submitAll`, every batch's job calls it.
   */
  callbackUrl?: string;
}

/**
 * Options for one submission (`submit`, `submitUrls`, `convert`).
 *
 * `pages` and `idempotencyKey` exist only here, not on `SubmitAllOptions`: page
 * numbers run across one submission and a key names one job, so neither can span
 * the several batches `submitAll` makes.
 */
export interface SubmitOptions extends SubmitAllOptions {
  /**
   * Convert only these pages, e.g. `"1-3, 7"`. Page numbers run across the whole
   * submission in order — an image is one page, a PDF its page count — so with a
   * single PDF they are the PDF's own. Only the selected pages are charged and count
   * towards the 50-page limit; the PDF itself may be longer.
   */
  pages?: string;
  /**
   * 1–255 printable ASCII characters. Default: a random UUID per call. Pass your own
   * to make resubmitting safe across calls or processes — e.g. an id from your own
   * database.
   */
  idempotencyKey?: string;
}

export interface WaitOptions {
  /** Initial poll interval in ms (default 5000), backs off to 15000. */
  pollIntervalMs?: number;
  /** Overall wait cap in ms (default 1_800_000 = 30 min). */
  timeoutMs?: number;
}

export type ConvertOptions = SubmitOptions & WaitOptions;

export type ConvertAllOptions = SubmitAllOptions & WaitOptions;

export interface ListJobsOptions {
  /** `YYYY-MM-DD` in UTC, inclusive. */
  createdFrom?: string;
  /** `YYYY-MM-DD` in UTC, inclusive. */
  createdTo?: string;
  /** Jobs per page, 1–100 (default 20). */
  limit?: number;
  /** `nextCursor` from the previous page, passed back unchanged. */
  cursor?: string;
}

export interface Account {
  email: string;
  credits: number;
}

/**
 * Result of requesting graceful cancellation for a conversion job.
 *
 * Cancellation is a graceful drain, not a hard stop: pages already running finish
 * and are billed if they succeed, and **a page being dispatched at the very moment
 * the request arrives may still run to completion and be billed** too. Pages that
 * have not started are skipped and refunded. Repeating the call is safe.
 */
export interface CancellationResult {
  readonly jobId: string;
  /** Whether the service accepted the cancellation request. */
  readonly cancellationRequested: boolean;
  /** True while the job is still winding down; keep polling `getJob` until terminal. */
  readonly finalizing: boolean;
}

/**
 * Read the three documented fields of a cancellation response.
 *
 * `CancellationResult` is an interface, so this is the shape check its two class
 * siblings get in their constructors. Casting the body through instead would hand
 * back `finalizing: undefined`, which reads as "settled" and stops a caller polling
 * a job that is still draining.
 *
 * `false` is a real answer for both booleans and passes — see `requireFields`.
 *
 * Both are read by **identity, never truthiness** — the rule `PageError.retryable`
 * already reads by. `[]` is truthy in JavaScript and falsy in Python, so a coerced
 * field meant opposite things to the two clients for one API. Identity is the same
 * test in both, and each field falls to the side that costs the caller nothing when
 * the value cannot be read — which is why the two comparisons point opposite ways:
 *
 * - `cancellationRequested` needs a real `true`. Claiming a cancellation the service
 *   never accepted is the expensive mistake; repeating the call is safe.
 * - `finalizing` needs a real `false` to mean settled — otherwise an unreadable
 *   value ends the wait exactly as the cast described above would. One more poll
 *   costs nothing, and `getJob` is the authority on when the job is really terminal.
 */
export function parseCancellationResult(data: unknown): CancellationResult {
  const d = requireFields(
    data,
    ["jobId", "cancellationRequested", "finalizing"],
    "cancellation response",
  );
  return {
    jobId: d.jobId as string,
    cancellationRequested: d.cancellationRequested === true,
    finalizing: d.finalizing !== false,
  };
}

/**
 * `pageResults` as a list, or `null` when the response did not carry the field.
 *
 * The `?? null` the other optional Job fields use, except that a value which *is*
 * there has to be the shape the contract promises — an array of entries — because
 * unlike a missing credit count, a mangled ledger would be read as a real answer
 * about which pages made it into the deck.
 */
function parsePageResults(value: unknown): PageResult[] | null {
  if (value == null) return null;
  if (!Array.isArray(value)) {
    throw new MalformedResponseError("malformed job response, pageResults is not a list");
  }
  return value.map((entry) => new PageResult(entry as Record<string, unknown>));
}

/** A whole number: `3` and `3.0` count, `"3"`, `true` and `1.5` do not. */
export function wholeNumber(value: unknown): number | null {
  return Number.isInteger(value) ? (value as number) : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * How delivering the completion callback is going (only for jobs submitted with
 * `callbackUrl`).
 *
 * `status` is `pending` (the job has not finished, or the next attempt is
 * scheduled), `delivered`, or `failed` (all 6 attempts failed; no more retries).
 * `lastResponseStatus` is `null` for a network error or timeout. Times are
 * `YYYY-MM-DD HH:MM:SS` in UTC, or `null`.
 *
 * Read leniently, like `PageError`: this is a report on a side channel, and a
 * surprise inside it should not cost you the job it is attached to. A field of the
 * wrong type reads as `null`; the original is on `raw`.
 */
export class CallbackStatus {
  readonly url: string | null;
  /** `pending` | `delivered` | `failed`. */
  readonly status: string | null;
  readonly attempts: number | null;
  readonly lastAttemptAt: string | null;
  readonly lastResponseStatus: number | null;
  readonly nextAttemptAt: string | null;
  readonly raw: Record<string, unknown>;

  constructor(data: Record<string, unknown>) {
    this.url = stringOrNull(data.url);
    this.status = stringOrNull(data.status);
    this.attempts = wholeNumber(data.attempts);
    this.lastAttemptAt = stringOrNull(data.lastAttemptAt);
    this.lastResponseStatus = wholeNumber(data.lastResponseStatus);
    this.nextAttemptAt = stringOrNull(data.nextAttemptAt);
    this.raw = data;
  }

  /** Build it from a `callback` value, or return null for anything but an object. */
  static fromJson(value: unknown): CallbackStatus | null {
    return isJsonObject(value) ? new CallbackStatus(value) : null;
  }
}

/** A snapshot of a conversion job's state. */
export class Job {
  readonly jobId: string;
  readonly status: JobStatus;
  readonly slideCount: number | null;
  readonly progress: number | null;
  readonly creditsReserved: number | null;
  readonly creditsUsed: number | null;
  readonly creditsRefunded: number | null;
  readonly createdAt: string | null;
  readonly completedAt: string | null;
  /** Optional in the public shape so pre-cancellation structural `Job` values still type-check. */
  readonly cancellationRequested?: boolean;
  readonly downloadUrl: string | null;
  readonly error: JobError | null;
  /**
   * Per-page outcome, in page order, `slideCount` entries long — or `null`.
   *
   * That shape is what the API documents, and this client passes the ledger
   * through as it arrived rather than cross-checking it against `slideCount`.
   * Refusing a job over a ledger that did not add up would cost the caller a deck
   * they can actually download, which is the worse trade. Check the length
   * yourself if your own logic depends on it.
   *
   * `null` means the service did not send the field, which is a different fact
   * from an empty array and must not be confused with one. It is absent while the
   * job is still running (mid-run, "this page failed" and "this page has not had
   * its turn" cannot be told apart), and absent for an early job that has no
   * per-page record at all.
   *
   * `creditsRefunded` says how many pages did not convert; this says **which**,
   * and whether each one still made it into the deck as its original image. See
   * `PageError.code`.
   *
   * Declared required rather than optional (unlike `cancellationRequested`)
   * precisely because absence is meaningful here: a third `undefined` state would
   * make "the service didn't say" and "nobody filled this in" indistinguishable,
   * which is the confusion `null` exists to prevent.
   */
  readonly pageResults: PageResult[] | null;
  /** Delivery of the completion callback; `null` unless submitted with `callbackUrl`. */
  readonly callback: CallbackStatus | null;
  /**
   * True when `submit` got this job back from an earlier request with the same
   * `Idempotency-Key` (response header `Idempotent-Replayed: true`) instead of
   * creating it now. Nothing was charged for the replay. Set by the client after
   * construction, since it comes from a header rather than the body.
   */
  replayed = false;
  /** Raw response body, for forward-compatible access to new fields. */
  readonly raw: Record<string, unknown>;

  constructor(data: Record<string, unknown>) {
    // The contract guarantees `jobId` and `status`, and a job without them is not a
    // job: a snapshot with either one undefined used to sail through here, and
    // `wait()` would then poll that nonsense object until its half-hour deadline ran
    // out. The same standard `cancel()` has always held its own response to — which
    // is why all three envelopes now go through one check.
    const d = requireFields(data, ["jobId", "status"], "job response");
    this.jobId = d.jobId;
    this.status = d.status;
    this.slideCount = d.slideCount ?? null;
    this.progress = d.progress ?? null;
    this.creditsReserved = d.creditsReserved ?? null;
    this.creditsUsed = d.creditsUsed ?? null;
    this.creditsRefunded = d.creditsRefunded ?? null;
    this.createdAt = d.createdAt ?? null;
    this.completedAt = d.completedAt ?? null;
    // Identity, not `?? false` and not `Boolean(...)`: the field is declared
    // `boolean`, and coercing it left the two clients reading the same malformed
    // body differently, because `[]` is truthy in JavaScript and falsy in Python.
    // `true` is the only value that says the service accepted a cancellation. See
    // `parseCancellationResult` for why the envelope's `finalizing` compares the
    // other way round.
    this.cancellationRequested = d.cancellationRequested === true;
    this.downloadUrl = d.downloadUrl ?? null;
    // Same rule as a page entry's `error` one level down: present but not an object
    // says nothing this model could report, so it reads as absent. The original is
    // still on `raw`.
    this.error = isJsonObject(d.error) ? (d.error as JobError) : null;
    this.pageResults = parsePageResults(d.pageResults);
    this.callback = CallbackStatus.fromJson(d.callback);
    this.raw = data;
  }

  /** Whether the job finished successfully (deliverable downloadable). */
  get isCompleted(): boolean {
    return this.status === "completed";
  }

  /** Whether the job failed. */
  get isFailed(): boolean {
    return this.status === "failed";
  }

  /** Whether the job reached a terminal state (completed or failed). */
  get isTerminal(): boolean {
    return this.isCompleted || this.isFailed;
  }

  static fromJson(data: Record<string, unknown>): Job {
    return new Job(data);
  }
}

/**
 * One page of `listJobs`: API-submitted jobs, newest first.
 *
 * Each job in `data` has the same shape as `getJob` returns, minus `pageResults`.
 * `nextCursor` is `null` on the last page; otherwise pass it back as `cursor`
 * unchanged. For reconciliation, add up each job's `creditsUsed` (charged) and
 * `creditsRefunded`.
 */
export class JobList {
  readonly data: Job[];
  readonly nextCursor: string | null;
  readonly raw: Record<string, unknown>;

  /**
   * `nextCursor` that is missing or not a string reads as the last page: an
   * unreadable cursor could not be passed back anyway, and stopping is the answer
   * that cannot loop forever.
   */
  constructor(data: unknown) {
    const d = requireFields(data, ["data"], "job list response");
    if (!Array.isArray(d.data)) {
      throw new MalformedResponseError("malformed job list response, data must be an array");
    }
    this.data = d.data.map((item: unknown) => Job.fromJson(item as Record<string, unknown>));
    this.nextCursor = typeof d.nextCursor === "string" && d.nextCursor !== "" ? d.nextCursor : null;
    this.raw = d;
  }
}
