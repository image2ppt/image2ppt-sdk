# image2ppt — Node.js / TypeScript client

Official Node.js client for the [image2ppt](https://image2ppt.com) API. Turn a batch of images or PDF pages into one **editable** PowerPoint (`.pptx`).

Uses Node's built-in `fetch`, plus `sharp` for client-side image preparation. Requires Node 18.17+, 20.3+, or 21+ — the range `sharp` ships native binaries for.

## Install

```bash
npm install image2ppt
```

Fully typed. Works from JavaScript or TypeScript, ESM or CommonJS interop.

## Get an API key

Sign in at [image2ppt.com](https://image2ppt.com), open **Developer / API** from the account menu, and create a key (looks like `i2p_live_xxxx`). It's shown in full **once** — save it. API access is available to accounts with credits.

> **Server-side only.** This SDK reads files from disk and holds your API key — run it on your backend, never in a browser or any client a user can inspect.

## Quick start

One shot — submit, wait, download:

```ts
import { Image2PPTClient, JobCancelledError } from "image2ppt";

const client = new Image2PPTClient({ apiKey: process.env.IMAGE2PPT_API_KEY! });

const job = await client.convert(
  ["slide1.png", "slide2.png", "report.pdf"],
  "out.pptx",
  { locale: "zh-CN", aspectRatio: "16:9" }, // both optional
);
console.log(`done — ${job.slideCount} pages, ${job.creditsUsed} credits used`);
```

Step by step, if you want to control polling:

```ts
const job = await client.submit(["slide1.png"], { aspectRatio: "4:3" });
console.log("job:", job.jobId, "reserved:", job.creditsReserved);

const done = await client.wait(job.jobId, { pollIntervalMs: 5000, timeoutMs: 1_800_000 });
await client.download(done.jobId, "out.pptx");
```

Cancel a job you no longer need:

```ts
const result = await client.cancel(job.jobId);
if (result.finalizing) {
  console.log("cancellation accepted; running pages are still winding down");
}

try {
  const done = await client.wait(job.jobId);
  // At least one page completed: the partial deck remains downloadable.
  await client.download(done.jobId, "partial.pptx");
} catch (error) {
  if (!(error instanceof JobCancelledError)) throw error;
  // No page completed, so the reservation was refunded and there is no deck.
}
```

Cancellation is graceful: pages already running finish and are billed if successful;
pages that have not started are skipped and refunded. A page being dispatched at the
very moment the request arrives may still run to completion and be billed. The call
is idempotent. A job with retained pages finishes as `completed`; without any
deliverable it finishes as `failed`, and `wait()` throws `JobCancelledError` (a
subclass of `JobFailedError`). `JobAlreadyFinishedError` covers both a job that
finished on its own and one already past the point where cancelling could change the
outcome.

## Convert only some pages — `pages`

```ts
const job = await client.submit(["report.pdf"], { pages: "1-3, 7" });
```

Single pages and `start-end` ranges, separated by commas, counting from 1. Page numbers
run across the **whole submission in order** — an image is one page, a PDF is its page
count — so with a single PDF they are simply the PDF's own page numbers. Overlaps are
merged, and the deck keeps the pages in ascending order.

Only the selected pages are charged, and only they count towards the 50-page limit: a
200-page PDF with `pages: "1-50"` is fine. The spelling is checked locally before
anything is uploaded (`InvalidPagesError`); a page past the end comes back
`PagesOutOfRangeError`.

With a selection, `pageResults[i].pageNumber` is the page's place **in the deck**, not
the number you selected: `pages: "3,7"` gives entries 1 and 2.

## Submit by URL — `submitUrls`

Instead of uploading, hand the service `https` links and it downloads them itself:

```ts
const job = await client.submitUrls(
  ["https://example.com/slides/1.png", "https://example.com/report.pdf"],
  { pages: "1-4" },
);
```

Up to 50 links, converted in order into one deck, held to the same limits as uploads
(35MB per file, 90MB together, 50 pages). File types are judged by content. Links must
be `https` and resolve to public addresses; a link that is refused throws
`InvalidUrlError`, a download that fails throws `UrlFetchFailedError` (worth retrying
later — the other side may have been down), and for both `e.index` says which link,
counting from 0.

Downloading takes time, so `submitUrls` waits up to 180 seconds for an answer whatever
the client's `timeoutMs`. One submission by URL per account can be in flight at a time;
another gets `RateLimitedError`.

## Get told when a job ends — `callbackUrl` and `verifyWebhook`

Pass `callbackUrl` to any submit call and the service `POST`s to it when the job ends,
instead of you polling:

```ts
const job = await client.submit(["slide1.png"], {
  callbackUrl: "https://example.com/hooks/image2ppt",
});
```

Every delivery is signed ([Standard Webhooks](https://www.standardwebhooks.com)). Check
it with the **raw** request body and the signing secret from the Developer / API page:

```ts
import express from "express";
import { verifyWebhook, WebhookVerificationError } from "image2ppt";

// express.raw(), not express.json(): the signature is over the exact bytes received.
app.post("/hooks/image2ppt", express.raw({ type: "application/json" }), (req, res) => {
  let event;
  try {
    event = verifyWebhook(req.body, req.headers, process.env.IMAGE2PPT_WEBHOOK_SECRET!);
  } catch (e) {
    if (e instanceof WebhookVerificationError) return res.sendStatus(400);
    throw e;
  }
  if (event.type === "job.completed") {
    const job = event.job; // same shape as getJob()
    // ...
  } else if (event.type === "job.failed") {
    // ...
  }
  // Ignore types you don't recognise — more may be added. Answer 2xx either way.
  res.sendStatus(204);
});
```

- **Pass the body exactly as received** — a `Buffer`, `Uint8Array` or string. Parsing
  the JSON and serialising it again changes the bytes, and the signature will not match.
  `headers` can be Node's `req.headers` or a fetch `Headers`.
- **Answer 2xx within 10 seconds.** Anything else counts as a failure, and the service
  retries: 6 attempts over about 8.5 hours (right away, then +1 min, +5 min, +30 min,
  +2 h, +6 h). A retry of one delivery keeps its `event.id`, so use that to skip
  duplicates.
- **Timestamps more than 5 minutes off are refused**, either way, as protection against
  replays (`toleranceSeconds` to change it).
- A malformed secret, or a body your framework already parsed into an object, throws a
  `TypeError` — those are mistakes in your receiver, so they are kept apart from a bad
  delivery.
- Rotating the secret is safe: for 24 hours the service signs with both the old and the
  new one, and either verifies.

`getJob()` reports how delivery is going in `job.callback` — `status` is `pending`,
`delivered` or `failed` (all 6 attempts failed), with `attempts`, `lastResponseStatus`
and the times of the last and next attempt.

## Resubmitting safely — `idempotencyKey`

Every submission carries an `Idempotency-Key`. If the same key comes back with the same
request within 24 hours, the service returns the job it already created —
`job.replayed` is `true` — instead of creating and charging for a second one.

That is what lets this client **resend a submission whose outcome is unknown** — a
dropped connection, a timeout, a 5xx — without risking a double charge. It does so by
itself, always under the same key (see *How it works*). By default the key is a random
UUID per call. Pass your own to extend the protection across calls, processes or
restarts:

```ts
const job = await client.submit(paths, { idempotencyKey: `order-${order.id}` });
```

If a submission still fails, the key it used is on the error:

```ts
import { APIConnectionError, ServerError } from "image2ppt";

try {
  job = await client.submit(paths);
} catch (e) {
  if (!(e instanceof APIConnectionError || e instanceof ServerError)) throw e;
  // Later, or from another process: safe for 24 hours, because the same files,
  // options and key can never create a second job.
  job = await client.submit(paths, { idempotencyKey: e.idempotencyKey });
}
```

"The same request" means the same file bytes in the same order (or the same `urls`), and
the same `locale`, `aspectRatio`, `pages` and `callbackUrl`. Reusing a key for a
different request throws `IdempotencyKeyMismatchError`. Keys are 1–255 printable ASCII
characters, shared by all API keys of the account.

## List your jobs — `listJobs` / `iterJobs`

```ts
for await (const job of client.iterJobs({ createdFrom: "2026-10-01", createdTo: "2026-10-31" })) {
  console.log(job.jobId, job.status, job.creditsUsed, job.creditsRefunded);
}
```

Jobs submitted through the API, newest first; jobs deleted on the website are not
listed. Dates are `YYYY-MM-DD` in UTC and both inclusive. `iterJobs` follows the pages
for you; `listJobs` returns one page (`page.data`, `page.nextCursor`) if you want to page
yourself — pass `nextCursor` back as `cursor`, `limit` is 1–100 (default 20). Each job
has `getJob()`'s fields except `pageResults`. For reconciliation, add up `creditsUsed`
(charged) and `creditsRefunded`.

## Which pages made it

Once a job is terminal, `job.pageResults` says what happened to each page, in page
order, one entry per page of the deck (with `pages`, per *selected* page):

```ts
for (const page of done.pageResults ?? []) {
  if (page.status === "converted") continue;
  if (page.error?.code === "PAGE_NOT_ATTEMPTED") {
    // This page never started. It is NOT in the deck at all.
    console.log(`page ${page.pageNumber} is missing from the deck`);
  } else {
    // Attempted and failed. The page IS in the deck — as the original image,
    // not as editable content.
    console.log(`page ${page.pageNumber} came through as a flat image`);
  }
  if (page.error?.retryable) console.log("  worth submitting again");
}
```

- **`null` is not an empty list.** `pageResults` is `null` when the service did not
  send the field at all — while the job is still running, and for an early job with
  no per-page record. An empty array would mean the job had no pages. Check for
  `null` rather than assuming every terminal job carries a ledger.
- **The two kinds of failed page call for different things.** `PAGE_NOT_ATTEMPTED`
  means the page is absent from the deck; every other code means it is present as the
  original image. `creditsRefunded` only tells you *how many* pages did not convert —
  this tells you which, and what became of them.
- **Codes**: `CONVERSION_FAILED`, `CONVERSION_TIMEOUT`, `PAGE_NOT_ATTEMPTED`. Treat
  anything else as `CONVERSION_FAILED`. The job-level `error.code` is deliberately
  coarser and still has only its two values — the finer reasons live here.
- **Read `retryable`, don't assume it.** Every code today says `true`; a code added
  later may not.

## Error messages in your language

Error `message` text follows the request's `Accept-Language` header. This client sends
none by default, so messages come back in English. To change that:

```ts
const client = new Image2PPTClient({
  apiKey: process.env.IMAGE2PPT_API_KEY!,
  acceptLanguage: "zh-CN",
});
```

> **`acceptLanguage` is not `locale`, and the two are easy to mix up.** The
> per-submission `locale` decides what language the **generated deck** is written in.
> The client-level `acceptLanguage` decides what language a **failure is explained to
> you** in. They are unrelated — an English deck with Chinese error messages is a
> perfectly sensible combination. `acceptLanguage` is a free-form HTTP header value
> (`"fr, en;q=0.8"` is fine), not one of the two deck languages.

Either way, branch on `code`, never on `message`.

Check your balance:

```ts
const { email, credits } = await client.account();
console.log(email, "credits:", credits);
```

## How it works

- **Async.** `submit` resolves with a job id immediately; conversion runs in the background. A single page typically takes ~2 minutes; 90% of jobs finish within 3.
- **One job = one PPTX.** All files in a submission are merged into a single deck, in upload order.
- **Billed per page.** 1 page = 1 credit, reserved at submit and settled on completion. If some pages fail but others succeed, the job still completes with the good pages and the failed pages' credits are refunded (`creditsRefunded`).
- **Limits.** Each file ≤ 35MB; **the files in one request ≤ 90MB in total**; ≤ 50 pages per job (images count as 1, PDFs as their page count — or, with `pages`, the pages selected). All three are checked locally before upload — note the per-file limit is the *stricter* one, so a 40MB PDF is refused even though it fits a request. **The sizes counted are the ones that actually go on the wire**: image preparation happens first, then the final payload sizes drive pre-flight and batching. PDFs keep their on-disk size.
- **The check is never stricter than the documented limit.** 90MB of file content is meant to be usable, so a submission sitting exactly on it goes through. Auto-batching is the one place that is deliberately conservative — it fills a batch only to 40MB, because starting one more batch costs nothing while refusing something the server would have accepted does not.
- **Only the formats the API accepts.** `png`, `jpg`/`jpeg`, `webp`, `gif`, `pdf`. Anything else throws `InvalidFileError` locally — the batch calls check every file before submitting the first one, so an unsupported file at the end of the pile cannot leave you paying for the batches ahead of it.
- **The local page check is a lower bound.** The client does not parse PDFs, so it counts each one as *at least* 1 page. That is enough to refuse combinations that can never work (50 images plus any PDF is already 51 pages), but a submission that passes locally can still come back `TOO_MANY_SLIDES` — a 30-page PDF counts as 1 here and 30 on the server. With `pages`, the check counts the pages selected instead, and a PDF longer than 50 pages is fine.
- **Going over the request limit is not a polite error.** Past that the connection is cut before the API can answer, so the caller sees a write timeout or a broken pipe instead of a status code. The client therefore checks locally *before* uploading and throws `InvalidFileError` (`code: "PAYLOAD_TOO_LARGE"`) without sending a byte.
- **A submission whose outcome is unknown is resent, under the same `Idempotency-Key`.** A network error only tells you the exchange broke — not whether the request body arrived; the job may exist with credits already reserved and only the response lost. Because every attempt carries the same key, a resend of that case gets the existing job back rather than a second charge. So `submit()` resends after a dropped connection, an idle timeout or a 5xx (up to 2 more attempts, 1s and 2s apart), and waits out `IdempotencyKeyInProgressError` — an earlier attempt the service is still working on — for up to 10 attempts or 3 minutes. Any other error is thrown straight away. Whatever finally escapes carries `e.idempotencyKey`; see *Resubmitting safely*.
- **Downloads are all-or-nothing.** `download()` writes to a temporary file next to the destination and renames it into place at the end, so a dropped connection cannot leave a truncated `.pptx` behind — or destroy a good deck already sitting at that path.
- **Every request identifies the client** with a `User-Agent` of `image2ppt-node/<version>`. The service uses this to tell SDK versions apart — it is not part of authentication and never changes a request's outcome.
- **A deprecated SDK version logs one warning.** If this version is below the lowest the service still supports, the response carries a `Deprecation` header and the client warns once (`console.warn`). Pass `warnOnDeprecated: false` to `Image2PPTClient` to silence it.
- **Time units.** `pollIntervalMs` and `timeoutMs` are in **milliseconds** (idiomatic for Node's timers).
- **The 60-second request timeout is idle time, not total time.** `timeoutMs` (default 60000) is how long one request may go with **no data moving in either direction** — it is not a cap on how long a request may take. A 40MB upload or a large PPTX download that keeps making progress runs as long as it needs to; only a transfer that actually stalls is given up on, as `APITimeoutError`. A request that never gets a response at all is covered by the same clock. This matches the Python client's read timeout, so the two SDKs behave the same way on a slow link.
- **Every failure of the request is an `Image2PPTError`.** A refused connection, a reset mid-download, a 2xx that comes back as a proxy's HTML login page, a job body missing its own id — all of them arrive as an SDK error with the original kept on `.cause`, never as a raw `TypeError: fetch failed` or `SyntaxError`. **Your own filesystem is the exception, deliberately:** if `download` cannot write where you asked it to, you get the operating system's error — `ENOSPC`, `EACCES`, `ENOENT` — because that names the thing you have to go and fix, and no error of ours would say it better. The Python client draws the same line.

> Both the Node and Python SDKs pre-compress images that need processing before upload. PNG/JPEG files already at most 2MiB with a longest edge at most 2000px upload byte-for-byte unchanged. Other PNG/JPEG files, and all WebP/GIF files, may be resized, flattened onto white, and sent as full-colour (4:4:4) JPEG; PDFs are never compressed or decoded and are streamed unchanged.

## More files than one request can hold

`convert()` is one job, one PPTX. For a pile too big for a single request, `convertAll()` splits it and writes **one PPTX per batch** (no server-side merge — N batches means N decks):

```ts
const files = await client.convertAll(imagePaths, "decks/");
console.log(files); // ['decks/part-01.pptx', 'decks/part-02.pptx']
```

Batches hold at most 80MB of file content and at most 50 images; every PDF goes in a batch of its own, because the client does not parse PDFs and only the server knows their page count. `submitAll()` does the same splitting and hands back the jobs if you want to drive polling yourself. To see the plan without uploading anything, use `planBatches()`.

Both take `callbackUrl` — each batch's job calls it. They do not take `pages` or `idempotencyKey` (passing either throws a `TypeError`): page numbers run across one submission and a key names one job, so neither can span several batches. Each batch gets its own key, kept across that batch's retries.

**Rate limits are waited out, not thrown.** A pile big enough to need batching will hit the account's per-minute page quota (and its cap on concurrently active jobs). Both arrive as a `429` with a `Retry-After`; both are handled the same way — sleep that long, retry the same batch. Retrying a 429 is free: the server is saying it did *not* take the submission, so nothing was created and nothing was charged. Total waiting is capped by `rateLimitMaxWaitMs` (default 30 min) — and **only waiting counts against it**, not the time the uploads themselves take, so a slow link cannot quietly turn the cap into "do not wait at all". A single batch is also retried at most 10 times, whatever the budget says: every retry re-uploads the whole batch, and a service still refusing after ten tries will not be talked round by more of them.

If a batch call does fail partway, **the jobs it already created come back on the error**:

```ts
import { Image2PPTError } from "image2ppt";

try {
  const files = await client.convertAll(imagePaths, "decks/");
} catch (e) {
  if (e instanceof Image2PPTError) {
    // Already running with credits reserved — collect them, don't resubmit.
    for (const job of e.submittedJobs) console.log("still running:", job.jobId);
  }
  throw e;
}
```

## Rate limits

Per account (all keys share the budget): ≤ 10 concurrent jobs, ≤ 60 pages/minute submitted. Over the limit returns `429` with a `Retry-After` hint. **Only submissions are rate limited — polling job status is not.**

`submitAll()` / `convertAll()` wait these out for you: a pile big enough to need batching is a pile big enough to hit the quota, so a 429 mid-pile is the normal path, not an error. `submit()`, `submitUrls()` and `convert()` do not, so catch `RateLimitedError` and honor `retryAfter` (seconds) yourself — resending with the same key keeps it safe:

```ts
import { RateLimitedError } from "image2ppt";

const idempotencyKey = "order-42";
for (;;) {
  try {
    job = await client.submit(paths, { idempotencyKey });
    break;
  } catch (e) {
    if (e instanceof RateLimitedError) {
      await new Promise((r) => setTimeout(r, (e.retryAfter ?? 5) * 1000));
    } else throw e;
  }
}
```

## Errors

Every error this client raises about a *request* subclasses `Image2PPTError` and carries `statusCode`, `code`, and `message` — plus `index` (which of `urls` it is about, else `undefined`) and, out of a submit call, `idempotencyKey`. Branch on `code`, not `message`. The one thing that reaches you unwrapped is a failure of your own disk while `download` is writing — see the note above.

Every `Image2PPTError` also carries **`isTransient`** — whether **repeating this exact read** later could plausibly succeed. (A raw filesystem error from `download` has no such field; that is the same exception as above, and it is never transient — free the space or fix the permission first.) It is what `wait()` uses to decide whether a failed status poll should be backed off and retried or should end the wait: `true` for a 5xx, a rate limit, `IdempotencyKeyInProgressError`, `UrlFetchFailedError`, a dropped connection and a stalled request, `false` for a bad key, a job that does not exist, or a response this client cannot parse.

**Resubmitting is only safe with the same key.** A lost response cannot be told apart from a submission the server accepted, so resending under a *new* key can create the same job twice and **charge you twice**. Resend with `e.idempotencyKey`, as `submit()` itself does.

| Class | HTTP | code |
|---|---|---|
| `AuthenticationError` | 401 / 403 | `INVALID_API_KEY`, `API_KEY_REQUIRED`, `ACCOUNT_DELETED` |
| `InvalidFileError` | 400 / 413 | `INVALID_FILE`, `INVALID_PDF`, `PAYLOAD_TOO_LARGE` (the size checks also fire locally, before upload) |
| `UploadAbortedError` | 400 | `UPLOAD_ABORTED` — the body never finished arriving and the server took nothing, so **resending the same files is safe** |
| `MalformedUploadError` | 400 | `MALFORMED_UPLOAD` — the body was not valid `multipart/form-data`; **resending identical bytes will not help** |
| `NoFilesError` | 400 | `NO_FILES` — no files reached the server |
| `InvalidAspectRatioError` | 400 | `INVALID_ASPECT_RATIO` — use `auto`, `16:9`, or `4:3` |
| `TooManySlidesError` | 400 | `TOO_MANY_SLIDES` — also for more than 50 pages selected |
| `InvalidPagesError` | 400 | `INVALID_PAGES` — `pages` is misspelled (also checked locally) |
| `PagesOutOfRangeError` | 400 | `PAGES_OUT_OF_RANGE` — page 0, or past the end; subclasses `InvalidPagesError` |
| `InvalidCallbackUrlError` | 400 | `INVALID_CALLBACK_URL` — not `https`, has credentials, over 2048 characters, or not a public address |
| `InvalidIdempotencyKeyError` | 400 | `INVALID_IDEMPOTENCY_KEY` — not 1–255 printable ASCII characters (also checked locally) |
| `IdempotencyKeyMismatchError` | 422 | `IDEMPOTENCY_KEY_MISMATCH` — the key was used for a different request |
| `IdempotencyKeyInProgressError` | 409 | `IDEMPOTENCY_KEY_IN_PROGRESS` (has `retryAfter`) — an earlier request with this key is still being processed |
| `InvalidUrlError` | 400 | `INVALID_URL` — a link was refused before downloading (`e.index` says which) |
| `UrlFetchFailedError` | 400 | `URL_FETCH_FAILED` — downloading a link failed; may be temporary (`e.index` says which) |
| `InvalidParameterError` | 400 | `INVALID_PARAMETER`, `INVALID_JSON` — e.g. more than 50 `urls`, or a bad `listJobs` argument |
| `PageRateExceededError` | 400 | `PAGE_RATE_EXCEEDED` — this one submission has more pages than a minute's quota, so waiting will not help; split it |
| `InsufficientCreditsError` | 402 | `INSUFFICIENT_CREDITS` |
| `RateLimitedError` | 429 | `RATE_LIMITED` (has `retryAfter`) |
| `JobNotFoundError` | 404 | `JOB_NOT_FOUND` |
| `JobAlreadyFinishedError` | 409 | `JOB_ALREADY_FINISHED` — the job already finished, **or is past the point where cancelling could change the outcome** |
| `NotReadyError` | 409 | `NOT_READY` |
| `OutputExpiredError` | 410 | `OUTPUT_EXPIRED` |
| `JobCancelledError` | — | `JOB_CANCELLED` — cancellation settled with no deliverable; subclasses `JobFailedError` |
| `JobFailedError` | — | job's `error.code` (thrown by `wait()`; `.job` is the snapshot) |
| `ServerError` | 5xx | `JOB_CANCEL_FAILED`, `STORAGE_FAILED`, … — the service failed on its own side; **retrying later is reasonable** (`isTransient` is true). Branch on `.code`. |
| `APIConnectionError` | — | — (the request never completed: connection refused or reset, DNS or TLS failure, a body that stopped arriving; the underlying error is on `.cause`) |
| `APITimeoutError` | — | `REQUEST_TIMEOUT` — one request went `timeoutMs` with **no data moving**; subclasses `APIConnectionError` |
| `MalformedResponseError` | — | — (the server answered with something this client cannot read: a 2xx that is not JSON, or a body missing a field the contract guarantees) |
| `Image2PPTTimeoutError` | — | — (`wait()` exceeded its own `timeoutMs`; job may still be running — **not** a transport failure, and not the same as `APITimeoutError`) |
| `WebhookVerificationError` | — | — (`verifyWebhook` refused a delivery: missing header, stale timestamp, or no matching signature) |

> **5xx now lands on `ServerError` rather than the base class.** It still subclasses `Image2PPTError`, so `catch (e) { if (e instanceof Image2PPTError) }` is unaffected — only code matching on the exact class or on `e.name` needs updating.

```ts
import { APIConnectionError, Image2PPTError, JobFailedError } from "image2ppt";

try {
  await client.convert(paths, "out.pptx");
} catch (e) {
  if (e instanceof JobFailedError) console.error("failed:", e.code, e.message);
  // Covers APITimeoutError too — it's a subclass. `.cause` has the real reason.
  else if (e instanceof APIConnectionError) console.error("network:", e.message);
  else if (e instanceof Image2PPTError) console.error("request error:", e.statusCode, e.code);
  else throw e;
}
```

## Full API reference

See <https://image2ppt.com/en/docs/api> for the complete HTTP contract (endpoints, fields, error codes). 中文版：<https://image2ppt.com/docs/api>。

## Develop

```bash
npm install
npm run build   # tsc -> dist/
npm test        # vitest
```

## License

[MIT](./LICENSE)
