# image2ppt — Python client

Official Python client for the [image2ppt](https://image2ppt.com) API. Turn a batch of images or PDF pages into one **editable** PowerPoint (`.pptx`).

## Install

```bash
pip install image2ppt
```

Requires Python 3.9+. Depends on `requests` and `Pillow` (Pillow powers optional client-side image pre-compression — see below).

## Get an API key

Sign in at [image2ppt.com](https://image2ppt.com), open **Developer / API** from the account menu, and create a key (looks like `i2p_live_xxxx`). It's shown in full **once** — save it. API access is available to accounts with credits.

> **Server-side only.** Keep your key on your backend. Never embed it in a browser, mobile app, or anything a user can inspect.

## Quick start

One shot — submit, wait, download:

```python
from image2ppt import Image2PPTClient, JobCancelledError

client = Image2PPTClient(api_key="i2p_live_your_key")

job = client.convert(
    ["slide1.png", "slide2.png", "report.pdf"],
    dest_path="out.pptx",
    locale="zh-CN",       # optional: "zh-CN" (default) or "en"
    aspect_ratio="16:9",  # optional: "auto" (default) / "16:9" / "4:3"
)
print("done — credits used:", job.credits_used, "refunded:", job.credits_refunded)
```

Step by step, if you want to control polling:

```python
job = client.submit(["slide1.png"], aspect_ratio="4:3")
print("job:", job.job_id, "credits reserved:", job.credits_reserved)

job = client.wait(job.job_id, poll_interval=5, timeout=1800)
client.download(job.job_id, "out.pptx")
```

Cancel a job you no longer need:

```python
result = client.cancel(job.job_id)
if result.finalizing:
    print("cancellation accepted; running pages are still winding down")

try:
    done = client.wait(job.job_id)
    # At least one page completed: the partial deck remains downloadable.
    client.download(done.job_id, "partial.pptx")
except JobCancelledError:
    # No page completed, so the reservation was refunded and there is no deck.
    pass
```

Cancellation is graceful: pages already running finish and are billed if successful;
pages that have not started are skipped and refunded. A page being dispatched at the
very moment the cancellation arrives may still run to completion and be billed — this
is a drain, not a hard stop. The call is idempotent. A job with retained pages finishes
as `completed`; without any deliverable it finishes as `failed`, and `wait()` raises
`JobCancelledError` (a subclass of `JobFailedError`).

If the request comes too late — the job already finished, or it is past the point where
cancelling could still change the outcome — you get `JobAlreadyFinishedError` instead.
Fetch the job with `get_job()` and work with the result it already has.

Check your balance:

```python
info = client.account()
print(info["email"], "credits:", info["credits"])
```

## Convert only some pages — `pages`

```python
job = client.submit(["report.pdf"], pages="1-3, 7")
```

Single pages and `start-end` ranges, separated by commas, counting from 1. Page numbers
run across the **whole submission in order** — an image is one page, a PDF is its page
count — so with a single PDF they are simply the PDF's own page numbers. Overlaps are
merged, and the deck keeps the pages in ascending order.

Only the selected pages are charged, and only they count towards the 50-page limit: a
200-page PDF with `pages="1-50"` is fine. The spelling is checked locally before anything
is uploaded (`InvalidPagesError`); a page past the end comes back `PagesOutOfRangeError`.

With a selection, `page_results[i].page_number` is the page's place **in the deck**, not
the number you selected: `pages="3,7"` gives entries 1 and 2.

## Submit by URL — `submit_urls`

Instead of uploading, hand the service `https` links and it downloads them itself:

```python
job = client.submit_urls(
    ["https://example.com/slides/1.png", "https://example.com/report.pdf"],
    pages="1-4",
)
```

Up to 50 links, converted in order into one deck, held to the same limits as uploads
(35MB per file, 90MB together, 50 pages). File types are judged by content. Links must
be `https` and resolve to public addresses; a link that is refused raises
`InvalidUrlError`, a download that fails raises `UrlFetchFailedError` (worth retrying
later — the other side may have been down), and for both `e.index` says which link,
counting from 0.

Downloading takes time, so `submit_urls` waits up to 180 seconds for an answer whatever
the client's `timeout`. One submission by URL per account can be in flight at a time;
another gets `RateLimitedError`.

## Get told when a job ends — `callback_url` and `verify_webhook`

Pass `callback_url` to any submit call and the service `POST`s to it when the job ends,
instead of you polling:

```python
job = client.submit(["slide1.png"], callback_url="https://example.com/hooks/image2ppt")
```

Every delivery is signed ([Standard Webhooks](https://www.standardwebhooks.com)). Check
it with the **raw** request body and the signing secret from the Developer / API page:

```python
from image2ppt import verify_webhook, WebhookVerificationError

# Flask shown; any framework works — pass the raw body bytes and the headers.
@app.post("/hooks/image2ppt")
def image2ppt_hook():
    try:
        event = verify_webhook(request.get_data(), request.headers, "whsec_...")
    except WebhookVerificationError:
        return "", 400
    if event.type == "job.completed":
        job = event.job          # same shape as get_job()
        ...
    elif event.type == "job.failed":
        ...
    # Ignore types you don't recognise — more may be added. Answer 2xx either way.
    return "", 204
```

- **Pass the body exactly as received.** Parsing the JSON and serialising it again
  changes the bytes, and the signature will not match.
- **Answer 2xx within 10 seconds.** Anything else counts as a failure, and the service
  retries: 6 attempts over about 8.5 hours (right away, then +1 min, +5 min, +30 min,
  +2 h, +6 h). A retry of one delivery keeps its `event.id`, so use that to skip
  duplicates.
- **Timestamps more than 5 minutes off are refused**, either way, as protection against
  replays (`tolerance_seconds` to change it).
- A malformed secret raises `ValueError`, and a body your framework already parsed into a
  dict raises `TypeError` — those are mistakes in your receiver, so they are kept apart
  from a bad delivery.
- Rotating the secret is safe: for 24 hours the service signs with both the old and the
  new one, and either verifies.

`get_job()` reports how delivery is going in `job.callback` — `status` is `pending`,
`delivered` or `failed` (all 6 attempts failed), with `attempts`, `last_response_status`
and the times of the last and next attempt.

## Resubmitting safely — `idempotency_key`

Every submission carries an `Idempotency-Key`. If the same key comes back with the same
request within 24 hours, the service returns the job it already created —
`job.replayed` is `True` — instead of creating and charging for a second one.

That is what lets this client **resend a submission whose outcome is unknown** — a
dropped connection, a timeout, a 5xx — without risking a double charge. It does so by
itself, always under the same key (see *How it works*). By default the key is a random
UUID per call. Pass your own to extend the protection across calls, processes or
restarts:

```python
job = client.submit(paths, idempotency_key=f"order-{order.id}")
```

If a submission still fails, the key it used is on the exception:

```python
from image2ppt import APIConnectionError, ServerError

try:
    job = client.submit(paths)
except (APIConnectionError, ServerError) as e:
    # Later, or from another process: safe for 24 hours, because the same files,
    # options and key can never create a second job.
    job = client.submit(paths, idempotency_key=e.idempotency_key)
```

"The same request" means the same file bytes in the same order (or the same `urls`), and
the same `locale`, `aspect_ratio`, `pages` and `callback_url`. Reusing a key for a
different request raises `IdempotencyKeyMismatchError`. Keys are 1–255 printable ASCII
characters, shared by all API keys of the account.

## List your jobs — `list_jobs` / `iter_jobs`

```python
for job in client.iter_jobs(created_from="2026-10-01", created_to="2026-10-31"):
    print(job.job_id, job.status, job.credits_used, job.credits_refunded)
```

Jobs submitted through the API, newest first; jobs deleted on the website are not
listed. Dates are `YYYY-MM-DD` in UTC and both inclusive. `iter_jobs` follows the pages
for you; `list_jobs` returns one page (`page.data`, `page.next_cursor`) if you want to
page yourself — pass `next_cursor` back as `cursor`, `limit` is 1–100 (default 20).
Each job has `get_job()`'s fields except `page_results`. For reconciliation, add up
`credits_used` (charged) and `credits_refunded`.

## Which pages made it — `job.page_results`

Once a job is terminal, it reports what happened to **every** page, in page order, one
entry per page of the deck (with `pages`, per *selected* page). `credits_refunded` tells you *how many* pages did not convert;
`page_results` tells you *which ones*, and what to do about them.

```python
job = client.wait(job_id)

if job.page_results is None:
    print("this job reported no per-page ledger")
else:
    for page in job.page_results:
        if page.status == "converted":
            continue
        # ``error`` is None when the entry carried none this client could read.
        # Say so rather than guessing: neither "where is it" nor "is it worth
        # resubmitting" is knowable without it.
        if page.error is None:
            print(f"page {page.page_number} failed, with no reason given")
            continue
        if page.error.code == "PAGE_NOT_ATTEMPTED":
            print(f"page {page.page_number} is NOT in the deck at all")
        else:
            print(f"page {page.page_number} is in the deck as the original image")
        if page.error.retryable:
            print("  resubmitting this one is worth a try")
```

**A failed page ends up one of two ways, and the difference is what you act on.**
`PAGE_NOT_ATTEMPTED` means the page never started and **is not in the delivered deck at
all** — the deck is short by that page, and its credit was refunded. Every other failure
code means the page **is** in the deck, as the original image rather than editable
content.

The per-page `error.code` values the contract defines today are exactly
`CONVERSION_FAILED`, `CONVERSION_TIMEOUT`, and `PAGE_NOT_ATTEMPTED`. Treat a code you do
not recognise as `CONVERSION_FAILED`. Note this is a *finer* set than the job-level
`job.error["code"]`, which still has only its two long-standing values — the two levels
differ deliberately, and the [API reference](https://image2ppt.com/en/docs/api) explains why.

`error.retryable` says whether resubmitting the same image could succeed. Every code
above carries `True` today — **branch on the field anyway** rather than hardcoding it,
since a code added later may carry `False`.

**`None` and `[]` are different facts.** `page_results` is `None` when the job reported
no ledger at all: it is still running (while it is, "this page failed" and "this page
has not had its turn" are indistinguishable), or it is an early job with no per-page
record. An empty list would mean a job with no pages. Check `is not None` before
iterating.

## What language error messages come back in

Error `message` text follows the request's `Accept-Language` header. This client sends
none by default, so you get English. Set `accept_language` to change that:

```python
client = Image2PPTClient(api_key="i2p_live_your_key", accept_language="zh-CN")
```

It is sent verbatim on every request, and it is a free-form HTTP header value — the
full `Accept-Language` syntax works (`"fr-CH, fr;q=0.9, en;q=0.8"`).

> **`accept_language` is not `locale`.** `locale` is a per-submission option that decides
> **what language the generated PPTX is written in**. `accept_language` is a client-level
> option that decides **what language error messages come back in**. They are unrelated,
> they take different kinds of value, and setting one does nothing to the other — you can
> ask for a Chinese deck while reading English errors, or the reverse.

Whatever the language, `code` never changes with it. Keep branching on `code`.

## How it works

- **Async.** `submit` returns a job id immediately; conversion runs in the background. A single page typically takes ~2 minutes; 90% of jobs finish within 3.
- **One job = one PPTX.** All files in a submission are merged into a single deck, in upload order.
- **Billed per page.** 1 page = 1 credit, reserved at submit and settled on completion. If some pages fail but others succeed, the job still `completed`s with the good pages and the failed pages' credits are refunded (`credits_refunded`) — `page_results` says which pages those were.
- **Limits.** Each file ≤ 35MB; **the files in one request ≤ 90MB in total**; ≤ 50 pages per job (images count as 1, PDFs as their page count — or, with `pages`, the pages selected). All three are checked locally before upload — note the per-file limit is the *stricter* one, so a 40MB PDF is refused even though it fits a request. **The sizes counted are the ones that actually go on the wire**: for an image that is its size *after* client-side compression, so a 40MB PNG that compresses to 2MB is fine. (The Node SDK compresses before upload the same way, so both clients reach the same verdict on the same file.)
- **The check is never stricter than the documented limit.** 90MB of file content is meant to be usable, so a submission sitting exactly on it goes through. Auto-batching is the one place that is deliberately conservative — it fills a batch only to 40MB, because starting one more batch costs nothing while refusing something the server would have accepted does not.
- **Only the formats the API accepts.** `png`, `jpg`/`jpeg`, `webp`, `gif`, `pdf`. Anything else raises `InvalidFileError` locally — the batch calls check every file before submitting the first one, so an unsupported file at the end of the pile cannot leave you paying for the batches ahead of it.
- **The local page check is a lower bound.** The client does not parse PDFs, so it counts each one as *at least* 1 page. That is enough to refuse combinations that can never work (50 images plus any PDF is already 51 pages), but a submission that passes locally can still come back `TOO_MANY_SLIDES` — a 30-page PDF counts as 1 here and 30 on the server. With `pages`, the check counts the pages selected instead, and a PDF longer than 50 pages is fine.
- **Going over the request limit is not a polite error.** Past that the connection is cut before the API can answer, so the caller sees a write timeout or a broken pipe instead of a status code. The client therefore checks locally *before* uploading and raises `InvalidFileError` (`code="PAYLOAD_TOO_LARGE"`) without sending a byte.
- **A submission whose outcome is unknown is resent, under the same `Idempotency-Key`.** A connection error only tells you the exchange broke — not whether the request body arrived; the job may exist with credits already reserved and only the response lost. Because every attempt carries the same key, a resend of that case gets the existing job back rather than a second charge. So `submit()` resends after a dropped connection, a per-request timeout or a 5xx (up to 2 more attempts, 1s and 2s apart), and waits out `IdempotencyKeyInProgressError` — an earlier attempt the service is still working on — for up to 10 attempts or 3 minutes. Any other error is raised straight away. Whatever finally escapes carries `e.idempotency_key`; see *Resubmitting safely*.
- **Downloads are all-or-nothing.** `download()` writes to a temporary file next to the destination and renames it into place at the end, so a dropped connection cannot leave a truncated `.pptx` behind — or destroy a good deck already sitting at that path.
- **The 60-second request timeout is idle time, not total time.** `timeout` (default 60) is how long one request may go with **no data moving** — it is not a cap on how long a request may take. A 40MB upload or a large PPTX download that keeps making progress runs as long as it needs to; only a transfer that actually stalls is given up on, as `APITimeoutError`. A request that never gets a response at all is covered by the same clock. The Node SDK's `timeoutMs` means exactly the same thing, so the two clients behave the same way on a slow link.
- **Every request identifies the client** with a `User-Agent` of `image2ppt-python/<version>`. The service uses this to tell SDK versions apart — it is not part of authentication and never changes a request's outcome.
- **A deprecated SDK version logs one warning.** If this version is below the lowest the service still supports, the response carries a `Deprecation` header and the client warns once (logger `image2ppt`). Pass `warn_on_deprecated=False` to `Image2PPTClient` to silence it.
- **Client-side pre-compression.** Images are compressed before upload (≤2000px, ≤2MB, full-colour JPEG) — the same shape the API works from, so you send fewer bytes without changing the result. PDFs are uploaded as-is.

## More files than one request can hold

`convert()` is one job, one PPTX. For a pile too big for a single request, `convert_all()` splits it and writes **one PPTX per batch** (no server-side merge — N batches means N decks):

```python
paths = client.convert_all(image_paths, dest_dir="decks/")
print(paths)  # ['decks/part-01.pptx', 'decks/part-02.pptx']
```

Batches hold at most 80MB of file content and at most 50 images; every PDF goes in a batch of its own, because the client does not parse PDFs and only the server knows their page count. `submit_all()` does the same splitting and hands back the jobs if you want to drive polling yourself. To see the plan without uploading anything, use `plan_batches()`.

Both take `callback_url` — each batch's job calls it. They do not take `pages` or `idempotency_key`: page numbers run across one submission and a key names one job, so neither can span several batches. Each batch gets its own key, kept across that batch's retries.

**Rate limits are waited out, not raised.** A pile big enough to need batching will hit the account's per-minute page quota (and its cap on concurrently active jobs). Both arrive as a `429` with a `Retry-After`; both are handled the same way — sleep that long, retry the same batch. Retrying a 429 is free: the server is saying it did *not* take the submission, so nothing was created and nothing was charged. Total waiting is capped by `rate_limit_max_wait` (default 30 min) — and **only waiting counts against it**, not the time the uploads themselves take, so a slow link cannot quietly turn the cap into "do not wait at all". A single batch is also retried at most 10 times, whatever the budget says: every retry re-uploads the whole batch, and a service still refusing after ten tries will not be talked round by more of them.

If a batch call does fail partway, **the jobs it already created come back on the exception**:

```python
from image2ppt import Image2PPTError

try:
    paths = client.convert_all(image_paths, dest_dir="decks/")
except Image2PPTError as e:
    # These are already running with credits reserved — collect them, don't resubmit.
    for job in e.submitted_jobs:
        print("still running:", job.job_id)
    raise
```

## Rate limits

Per account (all keys share the budget): ≤ 10 concurrent jobs, ≤ 60 pages/minute submitted. Over the limit returns `429` with a `Retry-After` hint. **Only submissions are rate limited — polling job status is not.**

`submit_all()` / `convert_all()` wait these out for you: a pile big enough to need batching is a pile big enough to hit the quota, so a 429 mid-pile is the normal path, not an error. `submit()`, `submit_urls()` and `convert()` do not, so catch `RateLimitedError` and honor `retry_after` yourself — resending with the same key keeps it safe:

```python
import time
from image2ppt import RateLimitedError

key = "order-42"
while True:
    try:
        job = client.submit(paths, idempotency_key=key)
        break
    except RateLimitedError as e:
        time.sleep(e.retry_after if e.retry_after is not None else 5)
```

## Errors

Every exception this client raises about a *request* subclasses `Image2PPTError` and carries `status_code`, `code`, and `message` — plus `index` (which of `urls` it is about, else `None`) and, out of a submit call, `idempotency_key`. Branch on `code`, not `message`. **A raw `requests` exception never reaches you** — a dropped connection, a per-request timeout, and a response body this client cannot parse all arrive as the SDK types below, with the original exception kept as `__cause__`.

**Your own filesystem is the exception, deliberately.** If `download` cannot write where you asked it to, you get the operating system's `OSError` — `ENOSPC`, `EACCES`, `ENOENT` — because that names the thing you have to go and fix, and no error of ours would say it better. So catch `OSError` alongside `Image2PPTError` around `download`. The Node client draws the same line.

| Exception | HTTP | code |
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
| `IdempotencyKeyInProgressError` | 409 | `IDEMPOTENCY_KEY_IN_PROGRESS` (has `retry_after`) — an earlier request with this key is still being processed |
| `InvalidUrlError` | 400 | `INVALID_URL` — a link was refused before downloading (`e.index` says which) |
| `UrlFetchFailedError` | 400 | `URL_FETCH_FAILED` — downloading a link failed; may be temporary (`e.index` says which) |
| `InvalidParameterError` | 400 | `INVALID_PARAMETER`, `INVALID_JSON` — e.g. more than 50 `urls`, or a bad `list_jobs` argument |
| `PageRateExceededError` | 400 | `PAGE_RATE_EXCEEDED` — this one submission has more pages than a minute's quota, so waiting will not help; split it |
| `InsufficientCreditsError` | 402 | `INSUFFICIENT_CREDITS` |
| `RateLimitedError` | 429 | `RATE_LIMITED` (has `retry_after`) |
| `JobNotFoundError` | 404 | `JOB_NOT_FOUND` |
| `JobAlreadyFinishedError` | 409 | `JOB_ALREADY_FINISHED` — the cancellation came too late to change anything: the job had already finished, or was past the point where cancelling could still change the outcome |
| `NotReadyError` | 409 | `NOT_READY` |
| `OutputExpiredError` | 410 | `OUTPUT_EXPIRED` |
| `JobCancelledError` | — | `JOB_CANCELLED` — cancellation settled with no deliverable; subclasses `JobFailedError` |
| `JobFailedError` | — | job's `error.code` (raised by `wait()`; `e.job` is the snapshot) |
| `ServerError` | 5xx | `JOB_CANCEL_FAILED` — the service could not accept the cancellation; **retrying is safe**. Every other 5xx lands here too; branch on `e.code`. |
| `APIConnectionError` | — | — (the request never completed: connection refused or reset, DNS or TLS failure, a body that stopped arriving) |
| `APITimeoutError` | — | `REQUEST_TIMEOUT` — one HTTP request ran past the client's `timeout`; subclasses `APIConnectionError` |
| `MalformedResponseError` | — | — (a 2xx that is not JSON, or a body missing a field the contract guarantees) |
| `Image2PPTTimeoutError` | — | — (`wait()` exceeded its `timeout`; job may still be running) |
| `WebhookVerificationError` | — | — (`verify_webhook` refused a delivery: missing header, stale timestamp, or no matching signature) |

> **Changed in 0.5.0:** a 5xx used to arrive as the base `Image2PPTError` and now arrives as `ServerError`. `ServerError` subclasses `Image2PPTError`, so **`except Image2PPTError` code is unaffected**; only code that checked for the base class *exactly* sees a difference.

**Two different timeouts, and they are not interchangeable.** `APITimeoutError` means a single HTTP request ran past the client's per-request `timeout` — nothing came back. `Image2PPTTimeoutError` means `wait()` hit its own overall deadline after any number of perfectly healthy polls; no request failed at all, the job is just taking longer. Re-`wait()` on the job id for the second one.

```python
from image2ppt import APIConnectionError, Image2PPTError, JobFailedError

try:
    job = client.convert(paths, "out.pptx")
except JobFailedError as e:
    print("conversion failed:", e.code, e.message)
except APIConnectionError as e:
    # Covers APITimeoutError too. The underlying exception is e.__cause__.
    print("could not reach the service:", e.message)
except Image2PPTError as e:
    print("request error:", e.status_code, e.code, e.message)
```

### Which failures are worth retrying

Every `Image2PPTError` carries `is_transient`, and it is the same question `wait()` asks itself before polling again: **would repeating this exact read plausibly work?** (A raw `OSError` from `download` has no such attribute; that is the same exception as above, and it is never transient — free the space or fix the permission first.)

```python
except Image2PPTError as e:
    if e.is_transient:
        time.sleep(5)  # a 5xx, a rate limit, or a network blip
```

It is `True` for `ServerError` (any 5xx), `RateLimitedError`, `IdempotencyKeyInProgressError`, `UrlFetchFailedError`, `APIConnectionError` and `APITimeoutError`; `False` for everything else — including `MalformedResponseError`, on purpose: a response this client cannot parse means something other than the API answered, or the contract moved, and neither gets better by asking again.

**Resubmitting is only safe with the same key.** A lost response cannot be told apart from a rejected request, so resending under a *new* key could create the same job twice and charge for it twice. Resend with `e.idempotency_key`, as `submit()` itself does.

## Full API reference

See <https://image2ppt.com/en/docs/api> for the complete HTTP contract (endpoints, fields, error codes). 中文版：<https://image2ppt.com/docs/api>。

## License

[MIT](./LICENSE)
