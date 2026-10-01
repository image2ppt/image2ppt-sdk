"""The image2ppt API client."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence
from urllib.parse import quote

import requests
from PIL import Image, UnidentifiedImageError

from ._compress import IMAGE_MIMES, compress_image_for_upload
from ._limits import (
    UploadItem,
    check_file_size,
    check_page_selection,
    check_submission,
    plan_batches,
)
from ._pages import is_blank, normalize_pages, parse_pages
from .errors import (
    APIConnectionError,
    APITimeoutError,
    IdempotencyKeyInProgressError,
    IdempotencyKeyMismatchError,
    Image2PPTError,
    Image2PPTTimeoutError,
    InvalidFileError,
    InvalidIdempotencyKeyError,
    InvalidParameterError,
    JobCancelledError,
    JobFailedError,
    MalformedResponseError,
    RateLimitedError,
    ServerError,
    exception_for,
)
from .models import CancellationResult, Job, JobList, _is_whole_number
from ._version import __version__

DEFAULT_BASE_URL = "https://image2ppt.com"

_LOG = logging.getLogger("image2ppt")

#: Sent on every request so the service knows which client version made it.
#:
#: The whole header has to be exactly this string: appending another product token
#: means the request is no longer recognised as coming from an official SDK. It is
#: not part of authentication and never changes the outcome of a request.
_USER_AGENT = f"image2ppt-python/{__version__}"

#: Wait between rate-limited retries when the server sends no ``Retry-After``.
_RATE_LIMIT_FALLBACK_WAIT = 5.0

#: Floor for a server-sent ``Retry-After``. ``Retry-After: 0`` is a legal value
#: meaning "retry now", and a proxy can even send a negative one; taken literally
#: either turns every retry loop in this client into a tight loop that re-sends the
#: same multipart body as fast as the link allows — for up to ``rate_limit_max_wait``
#: seconds, with tens of megabytes of files on each pass. A floor makes a retry a
#: retry instead of a flood, and costs nothing when the server means it.
_MIN_RETRY_AFTER = 1.0

#: ``Retry-After`` as plain decimal seconds — the only spelling this client accepts.
#: The other legal form is an HTTP-date, which does not match and falls back.
#:
#: Written with ``[0-9]`` and an explicit leading/trailing space-or-tab rather than
#: ``\d`` and ``.strip()``. Python's ``\d`` matches every Unicode decimal digit and
#: JavaScript's matches only ASCII, so ``Retry-After: ５`` would be five seconds to one
#: client and unparseable to the other — the exact two-client disagreement this pattern
#: exists to remove. Space and tab are the only whitespace HTTP allows around a field
#: value; ``.strip()`` would also eat Unicode spaces that never belong there.
_RETRY_AFTER_SECONDS = re.compile(r"[ \t]*([0-9]+(?:\.[0-9]+)?)[ \t]*")

#: Longest delay this client will ever wait in one go, in seconds (~24.8 days).
#:
#: The line is Node's timer range — a delay past 2**31-1 milliseconds is not
#: representable there, and ``setTimeout`` silently clamps it to *1 millisecond*, so an
#: out-of-range wait turns into "retry immediately, at full speed". Python fails
#: differently on the same input (``time.sleep`` raises ``OverflowError``), which is the
#: other half of the problem: the two clients would stop agreeing. Drawing the line at
#: the same number in both keeps them in step. Nothing legitimate lives out here.
#:
#: It bounds **every** wait, not just a server-sent ``Retry-After``: the polling backoff
#: is seeded from the caller's own ``poll_interval``, and on repeated 429s without a
#: ``Retry-After`` that seed is reused unchanged — so an absurd ``poll_interval`` with a
#: large enough ``timeout`` would reach the timer the same way.
_MAX_SLEEP = (2**31 - 1) / 1000

#: How many times one batch may be re-sent after a 429 before giving up.
#:
#: The waiting budget alone does not bound the work: a server answering
#: ``Retry-After: 1`` indefinitely costs only a second per round, so a 30-minute
#: budget would buy ~1800 rounds — and every round re-uploads the whole batch, tens
#: of megabytes at a time. A server still refusing after this many tries is not going
#: to be talked round by more of them.
_MAX_BATCH_ATTEMPTS = 10

#: Waits before resending a submission whose outcome is unknown — a dropped
#: connection, a timeout, a 5xx. One entry per extra attempt, so two retries.
#:
#: Resending is safe only because every attempt carries the same ``Idempotency-Key``:
#: if an earlier attempt did create the job, the service answers with that job
#: instead of creating a second one. The count stays small because each attempt can
#: re-upload up to 90MB.
_SUBMIT_RETRY_DELAYS = (1.0, 2.0)

#: Bounds on waiting out ``IDEMPOTENCY_KEY_IN_PROGRESS`` — an earlier attempt with
#: the same key that the service is still working on (typically one this client gave
#: up on after its per-request timeout). Bounded by attempts as well as time for the
#: reason ``_MAX_BATCH_ATTEMPTS`` gives: every attempt re-sends the whole body. The
#: wait starts at ``Retry-After`` and grows by half each time, up to the cap.
_IN_PROGRESS_MAX_ATTEMPTS = 10
_IN_PROGRESS_MAX_WAIT = 180.0
_IN_PROGRESS_WAIT_CAP = 30.0

#: Floor for the per-request timeout of a submission by URL. The service may spend
#: up to 120 seconds downloading before it even starts creating the job, so the
#: client's usual 60 would give up on requests that are going fine.
_URL_SUBMIT_MIN_TIMEOUT = 180.0

#: Most links one submission by URL may carry.
_MAX_URLS = 50

#: What ``Idempotency-Key`` may contain: 1–255 printable ASCII characters.
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}")


def _ensure_writable_dir(dest_dir: str) -> None:
    """Create ``dest_dir`` if needed and prove a file can actually be written in it.

    Creating the directory is not enough on its own: when it already exists,
    ``os.makedirs(..., exist_ok=True)`` succeeds no matter what the permissions
    are, so a read-only destination sails through and only fails later — after the
    jobs exist and the credits are spent.

    The proof is an actual file, not ``os.access``. ``os.access`` answers from the
    permission bits alone and gets it wrong in exactly the environments that need
    the answer: it ignores read-only mounts and ACLs, and running as root it
    reports writable for directories nothing can be written to. Creating a real
    file is the same operation ``download`` will do a few seconds later, so it is
    the same answer.

    The probe goes through ``tempfile.mkstemp``, which creates a randomly named file
    with ``O_CREAT | O_EXCL``. A predictable name opened for writing would follow —
    and truncate — whatever already sits at that path, including a symlink someone
    left in a shared output directory. Only the entry this call actually created is
    removed afterwards.
    """
    os.makedirs(dest_dir, exist_ok=True)
    try:
        fd, probe = tempfile.mkstemp(prefix=".image2ppt-write-test-", dir=dest_dir)
    except OSError as exc:
        raise OSError(
            f"cannot write to dest_dir {dest_dir!r} ({exc.strerror or exc}); "
            "nothing was submitted"
        ) from exc
    os.close(fd)
    try:
        os.remove(probe)
    except OSError:
        pass  # already gone: nothing to clean up


class _WaitBudget:
    """Seconds left for waiting out rate limits — spent only by actual waiting.

    A wall-clock deadline fixed at the start of the call would be eaten by the
    uploads themselves: a large pile on a slow uplink can burn the whole allowance
    before the first 429 even arrives, and then ``rate_limit_max_wait`` quietly
    means "do not wait at all" — with the cutoff depending on link speed rather
    than on anything the caller chose. The option promises time spent waiting, so
    only waiting takes from it.
    """

    __slots__ = ("remaining",)

    def __init__(self, seconds: float) -> None:
        self.remaining = seconds

    def spend(self, seconds: float) -> bool:
        """Wait ``seconds`` if the budget covers it; return False if it does not."""
        if seconds > self.remaining:
            return False
        time.sleep(seconds)
        self.remaining -= seconds
        return True


def _attach_submitted_jobs(exc: BaseException, jobs: Sequence[Job]) -> None:
    """Record the jobs created so far on an exception escaping a batch call.

    Every ``Image2PPTError`` declares ``submitted_jobs``; this also reaches the
    rarer non-SDK escape (an ``OSError`` opening a file, say), so the caller never
    has to know which kind they caught to find out what they already paid for.
    """
    try:
        exc.submitted_jobs = list(jobs)  # type: ignore[attr-defined]
    except AttributeError:
        pass  # exotic exception type with no __dict__: nothing we can do


@contextmanager
def _transport_errors(what: str) -> Iterator[None]:
    """Translate a ``requests`` transport failure into this SDK's own error.

    The READMEs promise that everything this client raises subclasses
    ``Image2PPTError``. A bare ``requests`` exception would break that promise, and
    callers would have to import ``requests`` to catch it.

    **Every block that touches the socket goes through here**, not only the call
    that opens the request. ``download`` opens its response with ``stream=True``,
    so the body is still on the wire long after the status line arrived: reading it
    — to write the deck, to parse a JSON reply, or to read the ``{"error": ...}``
    envelope of a non-2xx — is a second, separate chance for the connection to
    drop. ``requests`` reports that as a ``ChunkedEncodingError``, which is not a
    ``ValueError``, so it used to walk straight past the guards that only expected
    a body which failed to *parse*.

    A per-request timeout gets its own class because the answer to it is different:
    the request may simply need longer. Either way the original exception stays
    reachable as ``__cause__``, so nothing is lost by the translation.

    ``what`` names the exchange as a noun phrase — "the request to ...".
    """
    try:
        yield
    except requests.exceptions.Timeout as exc:
        raise APITimeoutError(f"{what} timed out: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise APIConnectionError(f"{what} did not complete: {exc}") from exc


def _response_header(headers: Any, name: str) -> Optional[str]:
    """Look up an HTTP header, ignoring case.

    ``requests`` headers are already case-insensitive; the fake session used in
    tests is a plain dict. One lookup covers both.

    **Every** header read goes through here, ``Retry-After`` included. That one
    decides how long a retry sleeps, so it is the last read that should be the one
    exception to the rule.
    """
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return "" if value is None else str(value)
    return None


def _resolve_idempotency_key(key: Optional[str]) -> str:
    """The caller's key, checked, or a fresh random one when they gave none."""
    if key is None:
        return str(uuid.uuid4())
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise InvalidIdempotencyKeyError(
            "idempotency_key must be 1-255 printable ASCII characters (no spaces)",
            code="INVALID_IDEMPOTENCY_KEY",
        )
    return key


def _string_list(value: Any, name: str, *, paths: bool = False) -> List[str]:
    """``value`` as a list of strings, refusing a bare string.

    A string is itself a sequence, so ``submit("deck.pdf")`` would otherwise be read
    as one file per character. With ``paths``, ``pathlib.Path`` and other path-like
    entries are accepted and turned into strings, as they always were.
    """
    if isinstance(value, (str, bytes, os.PathLike)):
        raise TypeError(f"{name} must be a list, not a single {type(value).__name__}")
    items = []
    for item in value:
        if paths and isinstance(item, os.PathLike):
            item = os.fspath(item)
        if not isinstance(item, str):
            raise TypeError(f"{name} must hold strings, not {type(item).__name__}")
        items.append(item)
    return items


def _rate_limit_delay(exc: RateLimitedError) -> float:
    """How long to wait before retrying a 429: ``Retry-After``, else a fixed wait."""
    return exc.retry_after if exc.retry_after is not None else _RATE_LIMIT_FALLBACK_WAIT


def _submission_fields(
    locale: Optional[str],
    aspect_ratio: Optional[str],
    pages: Optional[str],
    callback_url: Optional[str],
) -> Dict[str, str]:
    """The optional fields both kinds of submission carry, leaving out unset ones.

    An empty or all-whitespace ``pages`` or ``callback_url`` means "not given" to
    the service, so it is not sent at all.
    """
    if callback_url is not None and not isinstance(callback_url, str):
        raise TypeError(f"callback_url must be a string, not {type(callback_url).__name__}")
    fields: Dict[str, str] = {}
    if locale is not None:
        fields["locale"] = locale
    if aspect_ratio is not None:
        fields["aspectRatio"] = aspect_ratio
    selection = normalize_pages(pages)
    if selection is not None:
        fields["pages"] = selection
    if callback_url is not None and not is_blank(callback_url):
        fields["callbackUrl"] = callback_url
    return fields


def _link_url(value: Optional[str]) -> Optional[str]:
    """Pull the URL out of a ``Link: <url>; rel=...`` header, or None."""
    if not value:
        return None
    start = value.find("<")
    end = value.find(">", start + 1)
    if start == -1 or end == -1:
        return None
    url = value[start + 1 : end].strip()
    return url or None


@dataclass(frozen=True)
class _PreparedFile:
    """One file resolved to exactly what will go into the multipart body.

    Built before any connection is opened, so the request size is known up front.

    ``payload`` holds the bytes for an image (already compressed). For a PDF it is
    ``None`` and the file is read from ``path`` when the request is built — ``size``
    is then its size on disk.
    """

    filename: str
    mime: str
    payload: Optional[bytes]
    path: str
    size: int
    is_image: bool


class Image2PPTClient:
    """Client for the image2ppt API.

    Args:
        api_key: Your API key (looks like ``i2p_live_...``), created on the
            Developer / API page.
        base_url: Service base URL, defaults to ``https://image2ppt.com``.
        timeout: Per-HTTP-request timeout in seconds (not the whole-job wait).
        session: Optional ``requests.Session`` to inject (for testing or pooling).
        rate_limit_max_wait: Total seconds ``submit_all`` / ``convert_all`` may
            spend **waiting out rate limits** across the whole call (default 1800 =
            30 min). Only waiting counts against it — the time the uploads
            themselves take does not, so a slow link cannot quietly turn this into
            "do not wait at all". Submitting a large pile *will* hit the per-minute
            page quota, so waiting is the normal path, not an error.
        accept_language: Sent verbatim as the ``Accept-Language`` header on every
            request, or ``None`` (default) to send no such header at all — which
            is what this client has always done, so the default changes nothing.
            It decides **what language error ``message`` text comes back in**;
            without it you get English.

            Not to be confused with the per-submission ``locale``: that one
            decides what language the generated PPTX is written in. This is an
            HTTP header value, free-form and not restricted to the ``locale``
            values, because ``Accept-Language`` has its own syntax
            (``"zh-CN"``, ``"fr-CH, fr;q=0.9, en;q=0.8"``). The two are unrelated
            and can be set independently.
        warn_on_deprecated: When the service marks this SDK version deprecated,
            log one warning on the ``image2ppt`` logger. Default True. Set False
            to silence it.
    """

    #: Supported input extensions -> MIME type (for labeling multipart uploads).
    _MIME_BY_EXT = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
    }

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 60.0,
        session: Optional[requests.Session] = None,
        rate_limit_max_wait: float = 1800.0,
        accept_language: Optional[str] = None,
        warn_on_deprecated: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limit_max_wait = max(0.0, rate_limit_max_wait)
        self.accept_language = accept_language
        self.warn_on_deprecated = warn_on_deprecated
        self._deprecation_warned = False
        self._session = session or requests.Session()
        # Set once on the session, like the other two: they belong to every request
        # this client makes, so no call site has to remember them. Left off entirely
        # when the caller did not ask for it — an absent header and an empty one are
        # not the same request.
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": _USER_AGENT}
        if accept_language is not None:
            headers["Accept-Language"] = accept_language
        self._session.headers.update(headers)

    # ----- public methods ---------------------------------------------- #
    def submit(
        self,
        paths: Sequence[str],
        *,
        locale: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        pages: Optional[str] = None,
        callback_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Job:
        """Submit a batch of files and create a conversion job.

        Checked locally before anything is uploaded: the files must add up to at
        most 90MB and at most 50 pages (the pages *selected*, when ``pages`` is
        given), and ``pages`` must be spelled correctly. Over a limit this raises
        without opening a connection — going over the size cap on the wire does not
        come back as a clean error, it comes back as a dead connection.

        **Every submission carries an ``Idempotency-Key``**, and that is what makes
        a failed one safe to resend: if an earlier attempt did create the job, the
        service hands that job back (``job.replayed`` is True) instead of creating
        and charging for a second one. So this call resends by itself, always with
        the same key, when the outcome is unknown — a dropped connection, a
        per-request timeout, or a 5xx (up to 2 more attempts, 1s and 2s apart) —
        and waits out ``IDEMPOTENCY_KEY_IN_PROGRESS``, an earlier attempt the service
        is still working on (up to 10 attempts / 3 minutes). A 429 is not retried
        here; ``submit_all`` waits those out.

        If it still fails, the key is on the exception as ``exc.idempotency_key``.
        Calling ``submit`` again with that key and the same files and options is
        safe for 24 hours; calling it without the key may create a second job.

        Args:
            paths: Local file paths (one or more). Supports png/jpeg/webp/gif/pdf,
                each file <= 35MB, and <= 90MB of file content per request. An
                image is 1 page, a PDF is its page count; the total must be
                <= 50 pages. For more files than one request can hold, use
                ``submit_all`` / ``convert_all``.
            locale: ``zh-CN`` (default) or ``en``.
            aspect_ratio: ``auto`` (default) / ``16:9`` / ``4:3``.
            pages: Convert only these pages, e.g. ``"1-3, 7"``. Page numbers run
                across the whole submission in order — an image is one page, a PDF
                its page count — so with a single PDF they are the PDF's own. Only
                the selected pages are charged and count towards the 50-page limit;
                the PDF itself may be longer.
            callback_url: An ``https`` URL the service POSTs to when the job ends.
                Check each delivery with ``verify_webhook``.
            idempotency_key: 1–255 printable ASCII characters. Default: a random
                UUID per call. Pass your own to make resubmitting safe across calls
                or processes — e.g. an id from your own database.

        Returns:
            A ``Job`` with ``status`` ``pending`` (for a replay: the job's current
            status), plus ``slide_count`` and ``credits_reserved`` (credits locked
            at submit time).

        Raises:
            AuthenticationError, InvalidFileError (including the local per-file
            and ``PAYLOAD_TOO_LARGE`` pre-flight failures), TooManySlidesError,
            InvalidPagesError, PagesOutOfRangeError, InvalidCallbackUrlError,
            InsufficientCreditsError, RateLimitedError,
            IdempotencyKeyMismatchError (the key was used for a different request).
            ``APIConnectionError`` (``APITimeoutError`` for a per-request timeout),
            ``ServerError`` or ``IdempotencyKeyInProgressError`` once the retries
            above run out.
        """
        paths = _string_list(paths, "paths", paths=True)
        if not paths:
            raise ValueError("at least one file is required")
        key = _resolve_idempotency_key(idempotency_key)
        fields = _submission_fields(locale, aspect_ratio, pages, callback_url)
        # The spelling needs no files: refuse a typo before compressing anything.
        # Range and count wait for the page total, so the error is the one the
        # service would give.
        if "pages" in fields:
            parse_pages(fields["pages"])
        prepared = [self._prepare_file(path) for path in paths]
        self._check_prepared(prepared, fields.get("pages"))
        return self._submit_with_key(lambda: self._post_files(prepared, fields, key), key)

    def submit_urls(
        self,
        urls: Sequence[str],
        *,
        locale: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        pages: Optional[str] = None,
        callback_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Job:
        """Create a conversion job from files the service downloads itself.

        Instead of uploading, hand over ``https`` links; the service fetches them in
        order and converts them as one deck. The downloaded files are held to the
        same limits as uploads (35MB each, 90MB together, 50 pages). File types are
        judged by content, not by the link or its ``Content-Type``.

        Downloading takes time — up to 60 seconds a link and 120 for the request —
        so this call waits at least 180 seconds for an answer, whatever the client's
        ``timeout``. Retries and ``idempotency_key`` work exactly as in ``submit``.
        Only one submission by URL per account can be in flight at a time; another
        gets a ``RateLimitedError``.

        Args:
            urls: 1–50 ``https`` links. Each must resolve to a public address.
            locale, aspect_ratio, pages, callback_url, idempotency_key: as ``submit``.
                With links the page total is not known here, so ``pages`` is only
                checked for spelling and for selecting more than 50 pages.

        Raises:
            The errors of ``submit``, plus ``InvalidUrlError`` (a link was refused
            before downloading) and ``UrlFetchFailedError`` (a download failed; may
            be temporary). For those two — and ``InvalidFileError`` about a
            downloaded file — ``exc.index`` says which link, counting from 0.
            ``InvalidParameterError`` for more than 50 links, raised locally.
        """
        urls = _string_list(urls, "urls")
        if not urls:
            raise ValueError("at least one URL is required")
        if len(urls) > _MAX_URLS:
            raise InvalidParameterError(
                f"{len(urls)} URLs in one submission, over the {_MAX_URLS} allowed",
                code="INVALID_PARAMETER",
            )
        key = _resolve_idempotency_key(idempotency_key)
        fields = _submission_fields(locale, aspect_ratio, pages, callback_url)
        check_page_selection(fields.get("pages"))
        body: Dict[str, Any] = {"urls": urls, **fields}
        return self._submit_with_key(
            lambda: self._post(
                f"{self.base_url}/api/v1/jobs",
                json=body,
                headers={"Idempotency-Key": key},
                timeout=max(self.timeout, _URL_SUBMIT_MIN_TIMEOUT),
            ),
            key,
        )

    def submit_all(
        self,
        paths: Sequence[str],
        *,
        locale: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        callback_url: Optional[str] = None,
    ) -> List[Job]:
        """Split files into submittable batches and create **one job per batch**.

        For a pile of files too big or too numerous for a single request. Batching
        rules live in ``image2ppt._limits.plan_batches``: at most 80MB of file
        content and at most 50 images per batch, and every PDF in a batch of its
        own (the SDK does not parse PDFs, so only the server knows their page
        count). Input order is preserved.

        **Each returned job produces its own PPTX.** There is no server-side merge
        — N batches means N decks. If you need exactly one deck, keep the
        submission inside one request's limits and use ``convert``.

        **Rate limits are waited out, not raised.** A pile big enough to need
        batching is a pile big enough to hit the account's per-minute page quota
        (and its cap on concurrently active jobs). Both arrive as a 429 with a
        ``Retry-After``, and both are handled the same way: sleep that long, then
        try the same batch again. Waiting is the normal path here. Total waiting is
        capped by the client's ``rate_limit_max_wait``.

        **Each batch is its own submission with its own ``Idempotency-Key``**, and
        is retried exactly as ``submit`` retries — plus the 429s above, with the
        same key. There is no ``pages`` or ``idempotency_key`` option here: page
        numbers run across one submission and a key names one job, so neither can
        span several batches. Submit batches yourself with ``submit`` if you need
        them.

        **If it does give up, the jobs already created are handed back on the
        exception**, in ``exc.submitted_jobs``. Those jobs are running on the
        server with credits already reserved — they are not lost and not refunded.
        Wait on them or fetch them later; do not resubmit those files.

        Args:
            paths: Local file paths.
            locale: ``zh-CN`` (default) or ``en``.
            aspect_ratio: ``auto`` (default) / ``16:9`` / ``4:3``.
            callback_url: As ``submit``; every batch's job calls it when it ends.

        Returns:
            One pending ``Job`` per batch, in batch order.

        Raises:
            InvalidFileError: A single file is over the 35MB per-file limit, so
                no batching can carry it. Plus the same errors as ``submit`` for
                each batch.
            RateLimitedError: Still rate limited after ``rate_limit_max_wait``
                seconds of waiting.
        """
        paths = _string_list(paths, "paths", paths=True)
        if not paths:
            raise ValueError("at least one file is required")
        fields = _submission_fields(locale, aspect_ratio, None, callback_url)

        # Planning measures every file; the compressed bytes are then dropped and each
        # batch is prepared again when its turn comes, so memory holds one batch,
        # not the whole pile. Compression is deterministic, so the sizes match.
        batches = plan_batches(
            UploadItem(path=item.path, size=item.size, is_pdf=not item.is_image)
            for item in (self._prepare_file(path) for path in paths)
        )
        budget = _WaitBudget(self.rate_limit_max_wait)
        jobs: List[Job] = []
        for batch in batches:
            try:
                files = [self._prepare_file(item.path) for item in batch]
                # Checked again: a file changed on disk since planning could push
                # this batch over a limit, and an oversized request is cut off.
                self._check_prepared(files, None)
                jobs.append(self._submit_batch(files, fields, budget))
            except Exception as exc:
                # Whatever went wrong, the earlier batches are already jobs on the
                # server with credits reserved. Losing the ids would mean the caller
                # paid for work they can never collect.
                _attach_submitted_jobs(exc, jobs)
                raise
        return jobs

    def get_job(self, job_id: str) -> Job:
        """Fetch the current job state as a ``Job`` snapshot. Raises JobNotFoundError."""
        resp = self._get(f"{self.base_url}/api/v1/jobs/{quote(job_id, safe='')}")
        return Job.from_dict(self._parse_json(resp))

    def cancel(self, job_id: str) -> CancellationResult:
        """Request graceful cancellation of a conversion job.

        Pages already running finish and remain in the deliverable; pages that
        have not started are skipped and refunded. A page being dispatched at the
        very moment the cancellation arrives may still run to completion and be
        billed — the cut is a graceful drain, not a hard stop. Repeating the call
        is safe. When ``finalizing`` is true, keep polling with ``get_job`` until
        the job reaches a terminal state.

        Raises ``JobAlreadyFinishedError`` (409) when the request came too late to
        change anything: either the job had already finished, or it was past the
        point where cancelling could still change the outcome.
        """
        resp = self._post(f"{self.base_url}/api/v1/jobs/{quote(job_id, safe='')}/cancel")
        return CancellationResult.from_dict(self._parse_json(resp))

    def wait(
        self,
        job_id: str,
        *,
        poll_interval: float = 5.0,
        timeout: float = 1800.0,
    ) -> Job:
        """Poll until the job reaches a terminal state; return the completed ``Job``.

        The poll interval starts at ``poll_interval`` and backs off to 15s max. On a
        429 it waits the ``Retry-After`` seconds before continuing. A failed job
        raises JobFailedError; exceeding ``timeout`` raises Image2PPTTimeoutError
        (the job itself may still be running).

        Args:
            job_id: The job id.
            poll_interval: Initial poll interval in seconds (default 5).
            timeout: Overall wait cap in seconds (default 1800 = 30 min).
        """
        deadline = time.monotonic() + timeout
        interval = poll_interval
        while True:
            try:
                job = self.get_job(job_id)
            except RateLimitedError as exc:
                sleep_for = exc.retry_after if exc.retry_after is not None else interval
                self._sleep_until(deadline, sleep_for, job_id)
                continue
            except Image2PPTError as exc:
                # One poll failed. The job itself is very likely still running, so a
                # failure that could go the other way next time is backed off and
                # retried until the deadline rather than ending the whole wait.
                #
                # The error says which kind it is — see ``Image2PPTError.is_transient``.
                # Asking the error rather than asking "is this one of ours?" is what
                # makes a dropped connection or a single slow poll survivable while a
                # 404, a bad key, or a response we cannot parse still stops
                # immediately.
                if not exc.is_transient:
                    raise
                self._sleep_until(deadline, interval, job_id)
                interval = min(interval * 1.5, 15.0)
                continue

            if job.is_completed:
                return job
            if job.is_failed:
                err = job.error or {}
                error_class = (
                    JobCancelledError if err.get("code") == "JOB_CANCELLED" else JobFailedError
                )
                raise error_class(
                    err.get("message") or "conversion failed",
                    code=err.get("code"),
                    job=job,
                )

            self._sleep_until(deadline, interval, job_id)
            interval = min(interval * 1.5, 15.0)

    def download(self, job_id: str, dest_path: str) -> str:
        """Stream a completed job's PPTX to ``dest_path``; return that path.

        **``dest_path`` either holds a complete deck or is left exactly as it was.**
        The bytes go to a temporary file beside it and are renamed into place once
        the last one arrives, so a connection dropped mid-download cannot leave a
        truncated ``.pptx`` — nor destroy a good deck that was already there. That
        matters most for ``convert_all``, whose contract is "the decks already
        downloaded stay on disk": a half-written ``part-02.pptx`` would be indexed
        as one of them.

        Raises NotReadyError (409) if the job isn't done, JobNotFoundError (404) if
        it doesn't exist, OutputExpiredError (410) if the deliverable was reaped.
        """
        resp = self._get(
            f"{self.base_url}/api/v1/jobs/{quote(job_id, safe='')}/download",
            stream=True,
        )
        try:
            if not resp.ok:
                self._raise_for_error(resp)
            # Same directory as the destination, so the rename is atomic rather than
            # a cross-filesystem copy.
            fd, partial = tempfile.mkstemp(
                prefix=f".{os.path.basename(dest_path)}.",
                suffix=".part",
                dir=os.path.dirname(dest_path) or ".",
            )
            try:
                with os.fdopen(fd, "wb") as out:
                    # The body arrives after the status line, so a drop here is a
                    # second, separate chance to fail — one the request's own
                    # wrapping cannot see, since the request itself already
                    # succeeded.
                    with _transport_errors(f"the deliverable for job {job_id}"):
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                out.write(chunk)
                os.replace(partial, dest_path)
            except BaseException:
                try:
                    os.remove(partial)
                except OSError:
                    pass  # already gone
                raise
        finally:
            resp.close()
        return dest_path

    def convert(
        self,
        paths: Sequence[str],
        dest_path: str,
        *,
        locale: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        pages: Optional[str] = None,
        callback_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        poll_interval: float = 5.0,
        timeout: float = 1800.0,
    ) -> Job:
        """One-shot: submit -> wait for completion -> download to ``dest_path``.

        Arguments mirror ``submit`` and ``wait``. For the synchronous
        "give me a batch of images, hand me back a PPTX" case.

        One job, one PPTX — the files must fit in a single submission (90MB of
        file content, 50 pages). For more than that, ``convert_all`` splits the pile
        and writes one PPTX per batch.
        """
        # Resolved here, not in submit: a wait or download that fails after the job
        # exists must hand back the key too, or retrying convert() would create and
        # pay for a second job.
        key = _resolve_idempotency_key(idempotency_key)
        job = self.submit(
            paths,
            locale=locale,
            aspect_ratio=aspect_ratio,
            pages=pages,
            callback_url=callback_url,
            idempotency_key=key,
        )
        try:
            completed = self.wait(job.job_id, poll_interval=poll_interval, timeout=timeout)
            self.download(completed.job_id, dest_path)
        except Exception as exc:
            try:
                exc.idempotency_key = key  # type: ignore[attr-defined]
            except AttributeError:
                pass  # exotic exception type with no __dict__: nothing we can do
            raise
        return completed

    def convert_all(
        self,
        paths: Sequence[str],
        dest_dir: str,
        *,
        locale: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        callback_url: Optional[str] = None,
        poll_interval: float = 5.0,
        timeout: float = 1800.0,
    ) -> List[str]:
        """Batch version of ``convert``: submit everything, wait, download each deck.

        Files are split with ``submit_all``, every batch is submitted first (so the
        server works on them in parallel), then each job is waited on and
        downloaded in order.

        **This writes one PPTX per batch, not one merged deck.** Output files are
        named ``part-01.pptx``, ``part-02.pptx``, ... inside ``dest_dir`` — stable
        for the same input, and never overwriting each other. Existing files with
        those names are overwritten. ``convert`` is unchanged: one job, one PPTX.

        Args:
            paths: Local file paths.
            dest_dir: Directory for the PPTX files. Created **and proven writable
                before anything is submitted**, so an unusable destination costs
                nothing.
            locale: ``zh-CN`` (default) or ``en``.
            aspect_ratio: ``auto`` (default) / ``16:9`` / ``4:3``.
            callback_url: As ``submit_all``.
            poll_interval: Initial poll interval in seconds.
            timeout: Wait cap **per job** in seconds, not for the whole pile.

        Returns:
            The written file paths, in batch order.

        Rate limits during submission are waited out — see ``submit_all``.

        Raises:
            JobFailedError, Image2PPTTimeoutError, RateLimitedError: A job failed,
                ran past its wait cap, or the pile stayed rate limited too long.
                Earlier batches that already downloaded stay on disk, and every job
                created so far is on the exception as ``exc.submitted_jobs`` —
                those are still running with credits reserved, so wait on them
                rather than resubmitting.
        """
        # Before anything is submitted: if the destination is unusable, fail now
        # rather than after N jobs exist with credits reserved and nowhere to put
        # their output. This is the one step that can fail for free.
        _ensure_writable_dir(dest_dir)

        jobs = self.submit_all(
            paths, locale=locale, aspect_ratio=aspect_ratio, callback_url=callback_url
        )

        written: List[str] = []
        try:
            for index, job in enumerate(jobs, start=1):
                completed = self.wait(job.job_id, poll_interval=poll_interval, timeout=timeout)
                dest_path = os.path.join(dest_dir, f"part-{index:02d}.pptx")
                self.download(completed.job_id, dest_path)
                written.append(dest_path)
        except Exception as exc:
            # Same contract as submit_all: the jobs are already paid for, so the
            # caller gets their ids back instead of having to guess.
            _attach_submitted_jobs(exc, jobs)
            raise
        return written

    def list_jobs(
        self,
        *,
        created_from: Optional[str] = None,
        created_to: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> JobList:
        """One page of the jobs this account submitted through the API, newest first.

        Jobs deleted on the website are not listed. Each job has ``get_job``'s shape
        minus ``page_results``; for reconciliation, add up ``credits_used`` and
        ``credits_refunded``. To walk every page, use ``iter_jobs``.

        Args:
            created_from / created_to: ``YYYY-MM-DD`` in UTC, both inclusive; either
                may be left out.
            limit: Jobs per page, 1–100 (default 20).
            cursor: ``next_cursor`` from the previous page, passed back unchanged.

        Raises:
            InvalidParameterError: A parameter is invalid; ``message`` says which.
        """
        params: Dict[str, Any] = {}
        for name, value in (
            ("createdFrom", created_from),
            ("createdTo", created_to),
            ("limit", limit),
            ("cursor", cursor),
        ):
            # None and "" both mean "not given"; everything else goes as given, so
            # the service is the one to judge it.
            if value is not None and value != "":
                params[name] = value
        resp = self._get(f"{self.base_url}/api/v1/jobs", params=params)
        return JobList.from_dict(self._parse_json(resp))

    def iter_jobs(
        self,
        *,
        created_from: Optional[str] = None,
        created_to: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Job]:
        """Every job ``list_jobs`` would list, following ``next_cursor`` to the end.

        ``limit`` is the page size, not a cap on how many jobs come back. A 429 is
        waited out (``Retry-After``, else 5s), and a page that fails in a way worth
        repeating (``is_transient``) is fetched again after a short backoff — at
        most 10 attempts per page before the error is raised.
        """
        cursor: Optional[str] = None
        while True:
            page = self._list_page_with_retries(created_from, created_to, limit, cursor)
            yield from page.data
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def account(self) -> Dict[str, Any]:
        """Return account info: ``{"email": ..., "credits": available_credits}``."""
        resp = self._get(f"{self.base_url}/api/v1/account")
        return self._parse_json(resp)

    # ----- internal helpers -------------------------------------------- #
    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        """``session.get``, through the one wrapper every exchange goes through."""
        return self._send(self._session.get, url, **kwargs)

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        """``session.post``, through the one wrapper every exchange goes through."""
        return self._send(self._session.post, url, **kwargs)

    def _send(self, send: Any, url: str, **kwargs: Any) -> requests.Response:
        """Make one call: the timeout, the transport translation, the version notice.

        The three things that belong to *every* request this client makes, applied
        in the one place all of them pass through rather than remembered at each
        call site.

        ``timeout`` is a default, not an override, so a caller may still pass their
        own. It has to be applied here because ``requests`` has none of its own: a
        call site that forgot it would block until the peer hung up, which on a
        wedged connection is never.

        The transport translation itself lives in ``_transport_errors`` — see there
        for why every block that touches the socket has to go through it, not just
        this one.

        It takes the bound ``.get`` / ``.post`` rather than calling a generic
        ``.request``: those two methods are the whole surface an injected session
        has to provide, and widening it would break every caller who supplies one.
        """
        kwargs.setdefault("timeout", self.timeout)
        with _transport_errors(f"the request to {url}"):
            resp = send(url, **kwargs)
        self._warn_if_deprecated(resp)
        return resp

    def _submit_batch(
        self, prepared: Sequence[_PreparedFile], fields: Dict[str, str], budget: _WaitBudget
    ) -> Job:
        """Submit one planned batch, waiting out rate limits while ``budget`` allows.

        The batch is prepared and gets its ``Idempotency-Key`` once, before the loop,
        so every attempt sends the very same request under the same key — what lets
        the service recognise a resend.

        A 429 is the server saying it did *not* take the submission — nothing was
        created and nothing charged — so trying the same batch again is free. Both
        flavors (per-minute page quota, concurrent-job cap) carry a ``Retry-After``
        and are handled identically; when the header is missing we fall back to a
        fixed wait. Other failures are retried inside ``_submit_with_key``.

        Two things stop this: the shared waiting ``budget``, and
        ``_MAX_BATCH_ATTEMPTS``. The budget bounds time spent waiting; the attempt
        count bounds the uploads, which the budget cannot see — a server answering
        ``Retry-After: 1`` forever costs almost no budget per round while re-sending
        the whole batch every time.
        """
        key = _resolve_idempotency_key(None)
        attempts_left = _MAX_BATCH_ATTEMPTS
        while True:
            try:
                return self._submit_with_key(
                    lambda: self._post_files(prepared, fields, key), key
                )
            except RateLimitedError as exc:
                attempts_left -= 1
                # On the last attempt, do not wait first: nothing follows the wait,
                # so it would only delay the error the caller is already getting.
                if attempts_left <= 0 or not budget.spend(_rate_limit_delay(exc)):
                    raise

    def _check_prepared(self, prepared: Sequence[_PreparedFile], pages: Optional[str]) -> None:
        """Pre-flight, before a single byte goes out: an oversized request is not
        answered with an error, it is cut off — so it must never be sent."""
        for item in prepared:
            check_file_size(item.path, item.size)
        check_submission(
            total_bytes=sum(item.size for item in prepared),
            image_pages=sum(1 for item in prepared if item.is_image),
            # A PDF's real page count is only known server-side; counting it as at
            # least 1 is what stops "50 images + a PDF" from being sent as a
            # submission that is certain to come back over the page limit.
            pdf_files=sum(1 for item in prepared if not item.is_image),
            pages=pages,
        )

    def _submit_with_key(self, send: Callable[[], requests.Response], key: str) -> Job:
        """Send one submission until its outcome is known; see ``submit`` for the rules.

        ``send`` makes one attempt and must send the identical request every time,
        under ``key``. Whatever finally escapes carries ``key`` as
        ``idempotency_key``, so the caller can resend safely.

        A 429 is left to the caller on purpose. By the contract, resending the
        same request under a key whose job exists is answered with that job (and
        one still being processed with "in progress"), so a 429 here still means
        nothing was taken.
        """
        retries = iter(_SUBMIT_RETRY_DELAYS)
        in_progress_attempts = 0
        in_progress_budget = _WaitBudget(_IN_PROGRESS_MAX_WAIT)
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    resp = send()
                    job = Job.from_dict(self._parse_json(resp))
                    job.replayed = (
                        _response_header(resp.headers, "Idempotent-Replayed") or ""
                    ).strip().lower() == "true"
                    return job
                except IdempotencyKeyInProgressError as exc:
                    in_progress_attempts += 1
                    first = exc.retry_after if exc.retry_after is not None else 2.0
                    delay = min(first * 1.5 ** (in_progress_attempts - 1), _IN_PROGRESS_WAIT_CAP)
                    if in_progress_attempts >= _IN_PROGRESS_MAX_ATTEMPTS or not (
                        in_progress_budget.spend(delay)
                    ):
                        raise
                except (APIConnectionError, ServerError):
                    delay = next(retries, None)
                    if delay is None:
                        raise
                    time.sleep(delay)
                except IdempotencyKeyMismatchError as exc:
                    if attempt > 1:
                        # Only a request that created a job holds its key, so an
                        # earlier attempt of this very call did — and the files
                        # changed on disk in between, or this would have been a replay.
                        exc.message += (
                            "; an earlier attempt of this call did create a job with "
                            "this key before the request changed (did a file change on "
                            "disk?) — find it with list_jobs()"
                        )
                        exc.args = (exc.message,)
                    raise
        except Exception as exc:
            # Not only SDK errors: a PDF that vanished between attempts raises an
            # OSError, and the first attempt may already have created the job.
            try:
                exc.idempotency_key = key  # type: ignore[attr-defined]
            except AttributeError:
                pass  # exotic exception type with no __dict__: nothing we can do
            raise

    def _list_page_with_retries(
        self,
        created_from: Optional[str],
        created_to: Optional[str],
        limit: Optional[int],
        cursor: Optional[str],
    ) -> JobList:
        """One ``list_jobs`` page for ``iter_jobs``, retried while that is worth it.

        Listing is a read, so repeating it is free: a 429 waits ``Retry-After`` and
        anything ``is_transient`` backs off, both within ``_MAX_BATCH_ATTEMPTS``.
        """
        backoff = 1.0
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.list_jobs(
                    created_from=created_from,
                    created_to=created_to,
                    limit=limit,
                    cursor=cursor,
                )
            except Image2PPTError as exc:
                if attempt == _MAX_BATCH_ATTEMPTS or not exc.is_transient:
                    raise
                if isinstance(exc, RateLimitedError):
                    time.sleep(_rate_limit_delay(exc))
                else:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 15.0)

    def _prepare_file(self, path: str) -> _PreparedFile:
        """Resolve one path to its multipart part and its exact size on the wire."""
        filename = os.path.basename(path)
        mime = self._guess_mime(filename)
        if mime not in IMAGE_MIMES:
            # PDFs and other non-images: uploaded as-is and streamed from disk, so
            # the wire size is the size on disk.
            return _PreparedFile(
                filename=filename,
                mime=mime,
                payload=None,
                path=path,
                size=os.path.getsize(path),
                is_image=False,
            )

        # Images: pre-compress to the server spec so its pass is a passthrough.
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            payload, out_mime = compress_image_for_upload(raw, mime)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
            # Corrupt/truncated image, or one over Pillow's decompression-bomb
            # threshold. Surface it as an SDK error (like a server INVALID_FILE)
            # so callers catching Image2PPTError don't get a raw Pillow type.
            raise InvalidFileError(
                f"could not read image {filename!r}: {exc}",
                code="INVALID_FILE",
            ) from exc
        if out_mime == "image/jpeg" and not filename.lower().endswith((".jpg", ".jpeg")):
            # Compressed to JPEG: align the extension so name matches content.
            filename = os.path.splitext(filename)[0] + ".jpg"
        return _PreparedFile(
            filename=filename,
            mime=out_mime,
            payload=payload,
            path=path,
            size=len(payload),
            is_image=True,
        )

    def _post_files(
        self, prepared: Sequence[_PreparedFile], fields: Dict[str, str], key: str
    ) -> requests.Response:
        """POST the multipart submission once, under ``key``.

        PDFs are reopened and read from disk on every attempt; images go from the
        bytes compressed once in ``_prepare_file``, so a resend carries the same image
        bytes as the first attempt.
        """
        opened = []
        multipart = []
        try:
            for item in prepared:
                if item.payload is not None:
                    multipart.append(("files", (item.filename, item.payload, item.mime)))
                else:
                    handle = open(item.path, "rb")
                    opened.append(handle)
                    multipart.append(("files", (item.filename, handle, item.mime)))
            return self._post(
                f"{self.base_url}/api/v1/jobs",
                files=multipart,
                data=fields,
                headers={"Idempotency-Key": key},
            )
        finally:
            for handle in opened:
                handle.close()

    def _guess_mime(self, filename: str) -> str:
        """MIME type for a supported extension; refuse anything else locally.

        ``_MIME_BY_EXT`` is what the API accepts. Guessing a type for anything else
        meant a ``.txt`` or ``.docx`` was treated as PDF-like, given a batch of its
        own and uploaded, only to come back ``INVALID_FILE`` — and in ``submit_all``
        the batches ahead of it were already jobs with credits reserved. The
        supported set is known locally, so this is a failure that can cost nothing.

        The trade-off is deliberate: a format the service starts accepting is
        refused here until this list is updated and released.
        """
        ext = os.path.splitext(filename)[1].lower()
        try:
            return self._MIME_BY_EXT[ext]
        except KeyError:
            raise InvalidFileError(
                f"{filename!r} is not a supported file type; this client accepts "
                f"{', '.join(sorted(self._MIME_BY_EXT))}. Nothing was uploaded",
                code="INVALID_FILE",
            ) from None

    def _sleep_until(self, deadline: float, seconds: float, job_id: str) -> None:
        """Sleep ``seconds``, but never past ``deadline`` and never past ``_MAX_SLEEP``.

        Raises TimeoutError if the deadline has already passed. The ``_MAX_SLEEP``
        clamp is not redundant with the deadline: ``deadline`` is derived from the
        caller's ``timeout``, so both bounds here can be caller-supplied and neither
        constrains the other. See ``_MAX_SLEEP`` for what an out-of-range wait does.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Image2PPTTimeoutError(f"timed out waiting for job {job_id}", job_id=job_id)
        time.sleep(min(seconds, remaining, _MAX_SLEEP))

    def _parse_json(self, resp: requests.Response) -> Dict[str, Any]:
        """Return the JSON body on 2xx; otherwise raise the mapped exception.

        A 2xx that is not JSON is not this API answering — it is a captive portal,
        a proxy login page, or a CDN error page wearing a success status. That is a
        ``MalformedResponseError`` rather than a raw ``ValueError``, so it stays
        catchable as an ``Image2PPTError`` like everything else here.
        """
        if not resp.ok:
            self._raise_for_error(resp)
        # The parsing guard sits *inside* the transport one on purpose. ``requests``
        # raises a ``JSONDecodeError`` that is both a ``ValueError`` and one of its
        # own transport exceptions, so a body that arrived intact and merely is not
        # JSON has to be claimed by the inner handler before the outer one can
        # mistake it for a dropped connection.
        with _transport_errors(f"the HTTP {resp.status_code} response body"):
            try:
                return resp.json()
            except ValueError as exc:
                raise MalformedResponseError(
                    f"expected a JSON response but could not parse the body (HTTP "
                    f"{resp.status_code})",
                    status_code=resp.status_code,
                ) from exc

    def _warn_if_deprecated(self, resp: requests.Response) -> None:
        """Log at most one warning if this SDK version has been marked deprecated.

        A response from a version below the support floor carries a ``Deprecation``
        header — successful ones included, which is why this is checked before the
        status code rather than after. Presence is the whole signal, the value is not
        parsed. ``Sunset`` and ``Link`` join the message when present. ``wait()``
        polls every few seconds, so this is latched per client.

        Everything below is inside the guard on purpose: this notice is advisory, and
        nothing it does — reading the headers included — may turn a served response
        into a raised exception.
        """
        if not self.warn_on_deprecated or self._deprecation_warned:
            return
        try:
            if _response_header(resp.headers, "Deprecation") is None:
                return
            self._deprecation_warned = True
            parts = [
                f"This image2ppt Python SDK ({__version__}) has been marked deprecated."
            ]
            url = _link_url(_response_header(resp.headers, "Link"))
            if url:
                parts.append(f"See {url} for what changed.")
            sunset = _response_header(resp.headers, "Sunset")
            if sunset:
                parts.append(f"Support is planned to end {sunset}.")
            parts.append(
                "Pass warn_on_deprecated=False to Image2PPTClient(...) to silence this warning."
            )
            _LOG.warning(" ".join(parts))
        except Exception:
            # Advisory only: neither a throwing logging handler nor an unexpected
            # response object may fail the request.
            pass

    def _raise_for_error(self, resp: requests.Response) -> None:
        """Parse the ``{"error": {code, message}}`` envelope and raise the mapped error."""
        code: Optional[str] = None
        message: Optional[str] = None
        index: Optional[int] = None
        # Reading the envelope is itself a read off the socket: on a ``download``
        # the response was opened with ``stream=True``, so the error body has not
        # arrived yet and the connection can still die here. A body that failed to
        # *parse* is nothing worth reporting — the status code already says what
        # happened — but a body that never finished arriving is a transport failure
        # and has to be raised as one rather than silently reported as the status.
        with _transport_errors(f"the HTTP {resp.status_code} error body"):
            try:
                body = resp.json()
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict):
                    code = err.get("code")
                    message = err.get("message")
                    raw_index = err.get("index")
                    if _is_whole_number(raw_index) and raw_index >= 0:
                        index = int(raw_index)
            except ValueError:
                pass  # non-JSON error body (a gateway HTML page): fall back to status text
        message = message or f"request failed (HTTP {resp.status_code})"

        raise exception_for(
            status_code=resp.status_code,
            code=code,
            message=message,
            retry_after=self._parse_retry_after(
                _response_header(resp.headers, "Retry-After")
            ),
            index=index,
        )

    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[float]:
        """Parse the Retry-After header as seconds (contract: integer seconds).

        Anything unusable comes back as ``None`` so the caller falls back to its own
        wait: a missing header, an HTTP-date, a negative value (which additionally
        made ``time.sleep`` raise ``ValueError`` out of ``submit_all``), a value past
        ``_MAX_SLEEP``, or any spelling that is not plain decimal seconds.

        The syntax is matched explicitly rather than handed to the language's number
        parser. Both parsers are lenient in their own way — ``float`` takes ``"1e3"``
        and ``"nan"``, JavaScript's ``Number`` takes ``"0x10"`` — so "whatever the
        parser accepts" would mean the two clients disagreeing about the same header.
        One pattern, one answer.

        A usable value is floored at ``_MIN_RETRY_AFTER`` — see that constant for why
        zero cannot be taken literally.
        """
        if not value:
            return None
        match = _RETRY_AFTER_SECONDS.fullmatch(value)
        if match is None:
            return None
        seconds = float(match.group(1))
        if seconds > _MAX_SLEEP:
            return None
        return max(seconds, _MIN_RETRY_AFTER)
