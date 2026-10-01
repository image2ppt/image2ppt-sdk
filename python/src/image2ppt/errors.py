"""Exception hierarchy for the image2ppt client.

Every error carries the HTTP ``status_code``, the server error ``code`` (from the
``{"error": {"code", "message"}}`` envelope), and a human-readable ``message``.
Branch on ``code``, not ``message`` — messages may be reworded.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class Image2PPTError(Exception):
    """Base class for all client errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        #: Jobs already created when this error was raised out of ``submit_all`` /
        #: ``convert_all``. They are **running on the server with credits already
        #: reserved** — they are not lost and not refunded by the failure. Wait on
        #: them (``wait``/``download``) or come back to them later. Empty for any
        #: error not raised out of a batch call.
        self.submitted_jobs: List[Any] = []
        #: Which entry of ``urls`` caused the error, counting from 0 — set only for
        #: errors the service ties to one link (``INVALID_URL``, ``URL_FETCH_FAILED``,
        #: and ``INVALID_FILE`` / ``PAYLOAD_TOO_LARGE`` for a downloaded file).
        #: ``None`` for everything else.
        self.index: Optional[int] = None
        #: The ``Idempotency-Key`` the failed submission was sent with — set on every
        #: error raised out of ``submit`` / ``submit_urls`` / ``convert``, ``None``
        #: elsewhere. **Submitting again with this same key is always safe** for 24
        #: hours: if an earlier attempt did create the job, the service hands that job
        #: back instead of creating and charging for a second one.
        self.idempotency_key: Optional[str] = None

    @property
    def is_transient(self) -> bool:
        """Whether retrying the *same* read is worth doing.

        This is the one thing ``wait()`` asks before backing off and polling again,
        and it is deliberately a property of the error rather than a guess made at
        the call site. The old test — "is this one of ours?" — got both halves
        wrong: a network blip is not ours and is very much worth retrying, while a
        404 is ours and never will be.

        The default: a 5xx says the server had a problem on its side, so the same
        request may well work in a few seconds. Anything else — a 4xx, or a local
        failure with no status code at all — will answer the same way every time.

        It says nothing about **writes**. Submitting has its own rule, built on the
        ``Idempotency-Key`` every submission carries — see
        ``Image2PPTClient.submit``.
        """
        return self.status_code is not None and self.status_code >= 500

    def __str__(self) -> str:
        parts = []
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.code:
            parts.append(self.code)
        prefix = " ".join(parts)
        return f"[{prefix}] {self.message}" if prefix else self.message


class APIConnectionError(Image2PPTError):
    """The request never completed at the transport level — no HTTP status exists.

    Connection refused or reset, DNS failure, TLS failure, a response body that
    stopped arriving mid-stream. The underlying library exception is kept as
    ``__cause__``, so ``raise ... from`` chaining shows it and
    ``exc.__cause__`` reaches it.

    Transient: the same read is worth trying again. A submission that fails this
    way is retried too, but only because it is resent with the same
    ``Idempotency-Key`` — see ``Image2PPTClient.submit``.
    """

    @property
    def is_transient(self) -> bool:
        """Always True — there is no status code to derive it from."""
        return True


class APITimeoutError(APIConnectionError):
    """A single HTTP request took longer than the client's ``timeout``.

    Subclasses ``APIConnectionError`` so one ``except APIConnectionError`` covers
    every "the exchange did not complete" case.

    **Not the same as ``Image2PPTTimeoutError``.** This one is per-request: one
    HTTP call ran past the client's ``timeout`` and nothing came back.
    ``Image2PPTTimeoutError`` means ``wait()`` gave up on its overall deadline
    after any number of perfectly healthy polls — no request failed there at all.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = "REQUEST_TIMEOUT",
    ) -> None:
        super().__init__(message, status_code=status_code, code=code)


class MalformedResponseError(Image2PPTError):
    """The response arrived but this client cannot make sense of it.

    Two shapes: a 2xx whose body is not JSON at all (a captive-portal login page,
    a CDN error page served with the wrong status), and a JSON body missing a
    field the API contract guarantees.

    **Not transient, on purpose.** A body we cannot parse is a sign that something
    other than the API answered, or that the contract moved — neither gets better
    by asking again, and quietly retrying it for half an hour would hide the
    problem instead of showing it. Fail loudly and let the caller look.
    """

    @property
    def is_transient(self) -> bool:
        """Always False — see the class docstring."""
        return False


class ServerError(Image2PPTError):
    """The service answered with a 5xx.

    Per the API contract these are server-side and coming back later is
    reasonable, so this is transient. Still an ``Image2PPTError``, so existing
    ``except Image2PPTError`` code is unaffected.
    """


class AuthenticationError(Image2PPTError):
    """API key is missing, invalid, or the account is gone (401 / 403)."""


class InvalidFileError(Image2PPTError):
    """A file was rejected (400), or the request carried too much file content.

    Raised for an unsupported format, a single file over the 35MB per-file limit,
    or a request whose files add up to more than the 90MB per-request limit
    (413 ``PAYLOAD_TOO_LARGE``). The client raises the ``PAYLOAD_TOO_LARGE`` case
    locally, before uploading anything.
    """


class UploadAbortedError(Image2PPTError):
    """The upload was cut off before the body finished arriving (400 ``UPLOAD_ABORTED``).

    The server is telling you it did **not** take the submission — no job was created
    and no credits were reserved — so **resending the same files is safe**. (After an
    ``APIConnectionError`` resending is safe too, but only with the same
    ``idempotency_key``: that one cannot rule out that the job was created and only
    the response was lost.)

    If it keeps happening, the submission is probably too large for the link. Send
    fewer files per request, or use ``submit_all`` / ``convert_all`` to split.
    """


class MalformedUploadError(Image2PPTError):
    """The body was not valid ``multipart/form-data`` (400 ``MALFORMED_UPLOAD``).

    A client-side framing problem: **retrying the identical payload will not help**.
    Using this SDK unmodified you should never see it; if you do, please report it.
    """


class NoFilesError(Image2PPTError):
    """The request carried no files at all (400 ``NO_FILES``).

    Using this SDK you should never see it — ``submit`` refuses an empty ``paths``
    before opening a connection.
    """


class InvalidAspectRatioError(Image2PPTError):
    """``aspect_ratio`` was not one of the accepted values (400 ``INVALID_ASPECT_RATIO``).

    Accepted: ``auto`` (default), ``16:9``, ``4:3``. Nothing was created and nothing
    was charged — fix the value and submit again.
    """


class PageRateExceededError(Image2PPTError):
    """One submission holds more pages than the per-minute quota allows (400).

    Distinct from ``RateLimitedError``: a 429 means "not right now, try again in N
    seconds" and the same submission will eventually go through. ``PAGE_RATE_EXCEEDED``
    means this submission can *never* fit the window whole, so waiting does not help
    — split it, e.g. with ``submit_all`` / ``convert_all``.
    """


class TooManySlidesError(Image2PPTError):
    """The submission exceeds the 50-page-per-job limit (400 TOO_MANY_SLIDES)."""


class InsufficientCreditsError(Image2PPTError):
    """Not enough available credits to cover the submission (402)."""


class RateLimitedError(Image2PPTError):
    """Rate limited (429 RATE_LIMITED).

    ``retry_after`` is the server-suggested wait in seconds (from the
    ``Retry-After`` header); retry after that long.

    Both kinds of 429 land here — the per-minute page quota and the cap on
    concurrently active jobs — and both are handled the same way: wait, then try
    the same submission again. ``submit_all`` / ``convert_all`` do that for you;
    if one of them gives up, ``submitted_jobs`` holds the jobs already created.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code)
        self.retry_after = retry_after

    @property
    def is_transient(self) -> bool:
        """Always True — "not right now" is the whole meaning of a 429.

        Honour ``retry_after`` when you retry; the default 5xx rule would not
        cover this one, since a 429 is a 4xx.
        """
        return True


class IdempotencyKeyInProgressError(Image2PPTError):
    """An earlier request with the same ``Idempotency-Key`` is still being processed
    (409 ``IDEMPOTENCY_KEY_IN_PROGRESS``).

    Typically the first attempt timed out on this side while the service was still
    working on it. Wait ``retry_after`` seconds and send the same request with the
    same key: you get that job back once it exists. ``submit`` does this for you
    before giving up.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code)
        self.retry_after = retry_after

    @property
    def is_transient(self) -> bool:
        """Always True — the earlier request will finish one way or the other."""
        return True


class IdempotencyKeyMismatchError(Image2PPTError):
    """The ``Idempotency-Key`` was already used for a *different* request in the last
    24 hours (422 ``IDEMPOTENCY_KEY_MISMATCH``).

    "The same request" means the same file bytes in the same order (or the same
    ``urls``), plus the same ``locale``, ``aspect_ratio``, ``pages`` and
    ``callback_url``. Use a new key for a new submission.
    """


class InvalidIdempotencyKeyError(Image2PPTError):
    """The ``Idempotency-Key`` is not 1–255 printable ASCII characters
    (400 ``INVALID_IDEMPOTENCY_KEY``). Checked locally before anything is sent."""


class InvalidCallbackUrlError(Image2PPTError):
    """``callback_url`` was refused (400 ``INVALID_CALLBACK_URL``).

    It must be ``https``, at most 2048 characters, carry no user name or password,
    and resolve only to public addresses.
    """


class InvalidPagesError(Image2PPTError):
    """``pages`` is not a valid selection (400 ``INVALID_PAGES``).

    Write single pages or ``start-end`` ranges separated by commas, counting from 1,
    e.g. ``"1-3, 7"``; at most 1000 characters. Checked locally before anything is
    sent.
    """


class PagesOutOfRangeError(InvalidPagesError):
    """``pages`` names page 0 or a page past the end of the submission
    (400 ``PAGES_OUT_OF_RANGE``). The service's ``message`` says how many pages the
    submission has."""


class InvalidParameterError(Image2PPTError):
    """A request parameter has the wrong type or value (400 ``INVALID_PARAMETER``, or
    ``INVALID_JSON`` for a body that is not a JSON object).

    Raised for more than 50 ``urls``, and by ``list_jobs`` for a bad ``limit``,
    ``cursor``, ``created_from`` or ``created_to`` — the ``message`` says which.
    """


class InvalidUrlError(Image2PPTError):
    """One of ``urls`` was refused before downloading (400 ``INVALID_URL``): not a
    valid ``https`` URL, carrying a user name or password, or pointing at a
    non-public address. ``index`` says which entry."""


class UrlFetchFailedError(Image2PPTError):
    """Downloading one of ``urls`` failed (400 ``URL_FETCH_FAILED``): it did not
    resolve, answered non-2xx, timed out, redirected more than 4 times, or redirected
    to ``http``. ``index`` says which entry.

    Transient: the other side may simply have been down for a moment, so submitting
    again later can work. It is not retried for you — every attempt counts against
    the per-minute page quota.
    """

    @property
    def is_transient(self) -> bool:
        """Always True — see the class docstring."""
        return True


class WebhookVerificationError(Image2PPTError):
    """A completion callback failed ``verify_webhook``: a header is missing, the
    timestamp is outside the tolerance, or no signature matches.

    Refuse that delivery (answer 4xx). A genuine one is retried by the service.
    """


class JobNotFoundError(Image2PPTError):
    """The job id doesn't exist, or isn't owned by this key's account (404)."""


class JobAlreadyFinishedError(Image2PPTError):
    """Cancellation came too late to change anything (409 ``JOB_ALREADY_FINISHED``).

    Either the job had already finished on its own, or it was past the point where
    cancelling could still change the outcome. Both mean the same thing to you:
    fetch the job with ``get_job`` and work with the result it already has.
    """


class NotReadyError(Image2PPTError):
    """The job hasn't finished yet, so the deliverable can't be downloaded (409)."""


class OutputExpiredError(Image2PPTError):
    """The job finished, but its PPTX passed the retention window and was reaped (410)."""


class JobFailedError(Image2PPTError):
    """The job ended in failure (raised by ``wait`` when it polls status=failed).

    ``job`` is the failure snapshot; ``code`` / ``message`` come from its ``error`` field.
    """

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        job: Optional[Any] = None,
    ) -> None:
        super().__init__(message, code=code)
        self.job = job


class JobCancelledError(JobFailedError):
    """A graceful cancellation settled without any deliverable pages."""


class Image2PPTTimeoutError(Image2PPTError):
    """``wait`` exceeded its ``timeout`` before the job reached a terminal state.

    This does not mean the job failed — it may still be running. Re-``wait`` on the
    ``job_id`` later. (The prefix avoids shadowing the builtin ``TimeoutError``.)

    **Not the same as ``APITimeoutError``.** Nothing went wrong on the wire here:
    every poll may have answered promptly, the job just took longer than the
    deadline you gave ``wait()``. ``APITimeoutError`` is the other one — a single
    HTTP request that ran past the client's per-request ``timeout``.
    """

    def __init__(self, message: str, *, job_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.job_id = job_id


# Server error code -> exception class. Unlisted codes fall back to the status-code
# map, then to ``ServerError`` for a 5xx and the base class for anything else.
_CODE_TO_EXC: Dict[str, type] = {
    "INVALID_API_KEY": AuthenticationError,
    "API_KEY_REQUIRED": AuthenticationError,
    "ACCOUNT_DELETED": AuthenticationError,
    "INVALID_FILE": InvalidFileError,
    "INVALID_PDF": InvalidFileError,
    "PAYLOAD_TOO_LARGE": InvalidFileError,
    "UPLOAD_ABORTED": UploadAbortedError,
    "MALFORMED_UPLOAD": MalformedUploadError,
    "NO_FILES": NoFilesError,
    "INVALID_ASPECT_RATIO": InvalidAspectRatioError,
    "PAGE_RATE_EXCEEDED": PageRateExceededError,
    "TOO_MANY_SLIDES": TooManySlidesError,
    "INSUFFICIENT_CREDITS": InsufficientCreditsError,
    "RATE_LIMITED": RateLimitedError,
    "JOB_NOT_FOUND": JobNotFoundError,
    "JOB_ALREADY_FINISHED": JobAlreadyFinishedError,
    "NOT_READY": NotReadyError,
    "OUTPUT_EXPIRED": OutputExpiredError,
    "IDEMPOTENCY_KEY_IN_PROGRESS": IdempotencyKeyInProgressError,
    "IDEMPOTENCY_KEY_MISMATCH": IdempotencyKeyMismatchError,
    "INVALID_IDEMPOTENCY_KEY": InvalidIdempotencyKeyError,
    "INVALID_CALLBACK_URL": InvalidCallbackUrlError,
    "INVALID_PAGES": InvalidPagesError,
    "PAGES_OUT_OF_RANGE": PagesOutOfRangeError,
    "INVALID_PARAMETER": InvalidParameterError,
    "INVALID_JSON": InvalidParameterError,
    "INVALID_URL": InvalidUrlError,
    "URL_FETCH_FAILED": UrlFetchFailedError,
}
_STATUS_TO_EXC: Dict[int, type] = {
    401: AuthenticationError,
    403: AuthenticationError,  # API_KEY_REQUIRED / ACCOUNT_DELETED (fallback if code absent)
    402: InsufficientCreditsError,
    404: JobNotFoundError,
    409: NotReadyError,
    410: OutputExpiredError,
    413: InvalidFileError,
    429: RateLimitedError,
}


def exception_for(
    *,
    status_code: int,
    code: Optional[str],
    message: str,
    retry_after: Optional[float] = None,
    index: Optional[int] = None,
) -> Image2PPTError:
    """Build the mapped exception for an error envelope."""
    exc: Image2PPTError
    if status_code == 429:
        exc = RateLimitedError(
            message,
            status_code=429,
            code=code or "RATE_LIMITED",
            retry_after=retry_after,
        )
    elif code == "IDEMPOTENCY_KEY_IN_PROGRESS":
        exc = IdempotencyKeyInProgressError(
            message, status_code=status_code, code=code, retry_after=retry_after
        )
    else:
        exc_cls = _CODE_TO_EXC.get(code or "") or _STATUS_TO_EXC.get(status_code)
        if exc_cls is None:
            # Every 5xx is a ``ServerError``, whatever code it carries — the contract
            # says these are server-side and worth coming back to. It still subclasses
            # ``Image2PPTError``, so nothing catching that stops working.
            exc_cls = ServerError if status_code >= 500 else Image2PPTError
        exc = exc_cls(message, status_code=status_code, code=code)
    exc.index = index
    return exc
