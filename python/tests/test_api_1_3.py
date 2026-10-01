"""Tests for what API 1.3.0 added: pages, callbacks, Idempotency-Key, URLs, job lists.

The matching Node tests live in ``typescript/test/api-1-3.test.ts`` and pin the same
cases — a body or a selection must mean the same thing to both clients.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json

import pytest
import requests
from PIL import Image

from image2ppt import (
    MAX_PAGES_PER_JOB,
    APIConnectionError,
    CallbackStatus,
    IdempotencyKeyInProgressError,
    IdempotencyKeyMismatchError,
    Image2PPTClient,
    InvalidCallbackUrlError,
    InvalidFileError,
    InvalidIdempotencyKeyError,
    InvalidPagesError,
    InvalidParameterError,
    InvalidUrlError,
    Job,
    JobList,
    MalformedResponseError,
    PagesOutOfRangeError,
    RateLimitedError,
    ServerError,
    TooManySlidesError,
    UrlFetchFailedError,
    WebhookVerificationError,
    check_page_selection,
    check_submission,
    verify_webhook,
)
from image2ppt._pages import parse_pages, selected_page_count


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeSession:
    def __init__(self, handler):
        self.headers = {}
        self._handler = handler
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._handler("POST", url, **kwargs)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._handler("GET", url, **kwargs)


def client_and_session(handler, **kwargs):
    session = FakeSession(handler)
    return Image2PPTClient("i2p_live_test", session=session, **kwargs), session


def created(job_id="job_1"):
    return lambda *a, **k: FakeResponse(201, {"jobId": job_id, "status": "pending"})


def error(status, code, message="nope", headers=None, **extra):
    return FakeResponse(status, {"error": {"code": code, "message": message, **extra}}, headers)


@pytest.fixture
def images(tmp_path):
    def make(count):
        paths = []
        for i in range(count):
            buf = io.BytesIO()
            Image.new("RGB", (8, 8), (i % 256, 0, 0)).save(buf, format="PNG")
            path = tmp_path / f"img{i:03d}.png"
            path.write_bytes(buf.getvalue())
            paths.append(str(path))
        return paths

    return make


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "deck.pdf"
    path.write_bytes(b"%PDF-1.4 not really")
    return str(path)


# --------------------------------------------------------------------------- #
# pages — spelled exactly as the service reads it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "pages, ranges",
    [
        ("1-3,7", [(1, 3), (7, 7)]),
        ("1-3, 7", [(1, 3), (7, 7)]),
        (" 2 - 4 ", [(2, 4)]),
        ("007", [(7, 7)]),
        ("5,5,1-9", [(5, 5), (5, 5), (1, 9)]),
        ("﻿1　", [(1, 1)]),  # JavaScript whitespace, not Python's
        ("1-1000000000000", [(1, 1000000000000)]),
    ],
)
def test_valid_selections_parse_as_the_service_parses_them(pages, ranges):
    assert parse_pages(pages) == ranges


@pytest.mark.parametrize(
    "pages",
    [
        "1,,2",
        "1,",
        "3-1",
        "1-",
        "-3",
        "a",
        "1 2",
        "１",  # full-width digit: Python's \d would take it, the service does not
        "1.5",
        "+1",
        "0,abc",  # spelling is judged before range, as the service does
        str(2**53),
        "1," * 500 + "1",  # 1001 characters
    ],
)
def test_misspelled_selections_are_refused_locally(pages):
    with pytest.raises(InvalidPagesError) as exc:
        parse_pages(pages)
    assert type(exc.value) is InvalidPagesError
    assert exc.value.code == "INVALID_PAGES"


def test_page_zero_is_out_of_range_not_misspelled():
    with pytest.raises(PagesOutOfRangeError) as exc:
        parse_pages("0-2")
    assert exc.value.code == "PAGES_OUT_OF_RANGE"


def test_the_selected_count_merges_overlaps_without_expanding():
    assert selected_page_count(parse_pages("1-3,2-5,5,9")) == 6
    assert selected_page_count(parse_pages("1-1000000000000")) == 1000000000000


def test_a_selection_bounds_the_selected_pages_not_the_files():
    # 60 images would be refused whole; 10 of them selected is a 10-page job.
    check_submission(total_bytes=1, image_pages=60, pages="1-10")
    # A PDF may itself run past 50 pages; 21 selected is fine.
    check_submission(total_bytes=1, image_pages=0, pdf_files=1, pages="40-60")


def test_selecting_more_than_fifty_pages_is_refused_locally():
    with pytest.raises(TooManySlidesError):
        check_submission(total_bytes=1, image_pages=0, pdf_files=1, pages="1-51")
    with pytest.raises(TooManySlidesError):
        check_page_selection("1-60")


def test_with_only_images_a_page_past_the_end_is_known_locally():
    with pytest.raises(PagesOutOfRangeError, match="has 3 pages"):
        check_submission(total_bytes=1, image_pages=3, pages="2-4")
    # With a PDF the total is unknown, so the same selection goes to the service.
    check_submission(total_bytes=1, image_pages=3, pdf_files=1, pages="2-4")


def test_without_a_selection_the_old_page_count_still_applies():
    with pytest.raises(TooManySlidesError):
        check_submission(total_bytes=1, image_pages=MAX_PAGES_PER_JOB + 1)
    for blank in ("", "  ", None):
        with pytest.raises(TooManySlidesError):
            check_submission(total_bytes=1, image_pages=MAX_PAGES_PER_JOB + 1, pages=blank)


# --------------------------------------------------------------------------- #
# submit: new fields and the Idempotency-Key header
# --------------------------------------------------------------------------- #
def test_submit_sends_pages_callback_and_a_fresh_key_each_call(images):
    client, session = client_and_session(created())
    paths = images(3)

    client.submit(paths, pages="1-2", callback_url="https://example.com/hook")
    client.submit(paths)

    first, second = session.calls
    assert first[2]["data"] == {"pages": "1-2", "callbackUrl": "https://example.com/hook"}
    assert second[2]["data"] == {}
    keys = [call[2]["headers"]["Idempotency-Key"] for call in session.calls]
    assert keys[0] != keys[1] and all(len(key) == 36 for key in keys)


def test_blank_pages_and_callback_are_not_sent(images):
    client, session = client_and_session(created())
    client.submit(images(1), pages="  ", callback_url="")
    assert session.calls[0][2]["data"] == {}


def test_submit_uses_the_callers_key(images):
    client, session = client_and_session(created())
    client.submit(images(1), idempotency_key="order-42")
    assert session.calls[0][2]["headers"] == {"Idempotency-Key": "order-42"}


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "中文", "tab\t"])
def test_an_invalid_key_is_refused_before_anything_is_sent(images, key):
    client, session = client_and_session(created())
    with pytest.raises(InvalidIdempotencyKeyError):
        client.submit(images(1), idempotency_key=key)
    assert session.calls == []


def test_the_longest_and_widest_valid_key_passes(images):
    client, session = client_and_session(created())
    key = "".join(chr(c) for c in range(0x21, 0x7F)) * 3  # 282 chars: too long
    with pytest.raises(InvalidIdempotencyKeyError):
        client.submit(images(1), idempotency_key=key)
    client.submit(images(1), idempotency_key=key[:255])
    assert session.calls[0][2]["headers"]["Idempotency-Key"] == key[:255]


def test_pages_that_are_not_text_are_a_type_error(images):
    client, _ = client_and_session(created())
    with pytest.raises(TypeError):
        client.submit(images(1), pages=3)  # type: ignore[arg-type]


def test_a_single_string_is_not_read_as_one_path_per_character(images):
    client, session = client_and_session(created())
    with pytest.raises(TypeError):
        client.submit(images(1)[0])  # type: ignore[arg-type]
    assert session.calls == []


def test_a_misspelled_selection_never_reaches_the_wire(images):
    client, session = client_and_session(created())
    with pytest.raises(InvalidPagesError):
        client.submit(images(2), pages="1-")
    assert session.calls == []


def test_convert_passes_the_new_options_through(images, tmp_path):
    def handler(method, url, **kwargs):
        if method == "POST":
            return FakeResponse(201, {"jobId": "j", "status": "pending"})
        if url.endswith("/download"):
            resp = FakeResponse(200)
            resp.iter_content = lambda chunk_size=0: iter([b"PPTX"])
            resp.close = lambda: None
            return resp
        return FakeResponse(200, {"jobId": "j", "status": "completed"})

    client, session = client_and_session(handler)
    client.convert(
        images(2),
        str(tmp_path / "out.pptx"),
        pages="2",
        callback_url="https://example.com/h",
        idempotency_key="k1",
        poll_interval=0,
    )
    post = session.calls[0][2]
    assert post["data"] == {"pages": "2", "callbackUrl": "https://example.com/h"}
    assert post["headers"] == {"Idempotency-Key": "k1"}


def test_submit_all_takes_a_callback_but_not_pages_or_a_key(images):
    client, session = client_and_session(created())
    client.submit_all(images(2), callback_url="https://example.com/h")
    assert session.calls[0][2]["data"] == {"callbackUrl": "https://example.com/h"}
    with pytest.raises(TypeError):
        client.submit_all(images(2), pages="1")  # type: ignore[call-arg]


def test_a_rate_limited_batch_is_resent_under_the_same_key(images, no_sleep):
    responses = iter([error(429, "RATE_LIMITED", headers={"Retry-After": "3"}), created()()])
    client, session = client_and_session(lambda *a, **k: next(responses))

    client.submit_all(images(2))

    keys = [call[2]["headers"]["Idempotency-Key"] for call in session.calls]
    assert len(keys) == 2 and keys[0] == keys[1]


# --------------------------------------------------------------------------- #
# submit_urls
# --------------------------------------------------------------------------- #
def test_submit_urls_posts_json_with_a_long_enough_timeout():
    client, session = client_and_session(created(), timeout=30)
    job = client.submit_urls(
        ["https://example.com/a.png", "https://example.com/b.pdf"],
        locale="en",
        aspect_ratio="16:9",
        pages="1-2",
        callback_url="https://example.com/h",
        idempotency_key="k",
    )
    assert job.job_id == "job_1"
    kwargs = session.calls[0][2]
    assert kwargs["json"] == {
        "urls": ["https://example.com/a.png", "https://example.com/b.pdf"],
        "locale": "en",
        "aspectRatio": "16:9",
        "pages": "1-2",
        "callbackUrl": "https://example.com/h",
    }
    assert kwargs["headers"] == {"Idempotency-Key": "k"}
    assert kwargs["timeout"] == 180.0
    assert "files" not in kwargs


def test_submit_urls_refuses_bad_input_locally():
    client, session = client_and_session(created())
    with pytest.raises(ValueError):
        client.submit_urls([])
    with pytest.raises(InvalidParameterError):
        client.submit_urls(["https://example.com/x"] * 51)
    with pytest.raises(TypeError):
        client.submit_urls("https://example.com/x")  # type: ignore[arg-type]
    with pytest.raises(TooManySlidesError):
        client.submit_urls(["https://example.com/x"], pages="1-51")
    assert session.calls == []


def test_a_link_error_says_which_link():
    client, _ = client_and_session(
        lambda *a, **k: error(400, "URL_FETCH_FAILED", "timed out", index=1)
    )
    with pytest.raises(UrlFetchFailedError) as exc:
        client.submit_urls(["https://example.com/a", "https://example.com/b"])
    assert exc.value.index == 1
    assert exc.value.is_transient
    assert exc.value.idempotency_key is not None


# --------------------------------------------------------------------------- #
# error envelope: new codes, index
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status, code, cls",
    [
        (400, "INVALID_CALLBACK_URL", InvalidCallbackUrlError),
        (400, "INVALID_IDEMPOTENCY_KEY", InvalidIdempotencyKeyError),
        (422, "IDEMPOTENCY_KEY_MISMATCH", IdempotencyKeyMismatchError),
        (400, "INVALID_JSON", InvalidParameterError),
        (400, "INVALID_PARAMETER", InvalidParameterError),
        (400, "INVALID_URL", InvalidUrlError),
        (400, "URL_FETCH_FAILED", UrlFetchFailedError),
        (400, "INVALID_PAGES", InvalidPagesError),
        (400, "PAGES_OUT_OF_RANGE", PagesOutOfRangeError),
    ],
)
def test_new_error_codes_map_to_their_own_types(status, code, cls):
    client, _ = client_and_session(lambda *a, **k: error(status, code))
    with pytest.raises(cls) as exc:
        client.list_jobs()
    assert exc.value.code == code and exc.value.status_code == status
    assert exc.value.index is None


def test_in_progress_carries_retry_after():
    client, _ = client_and_session(
        lambda *a, **k: error(409, "IDEMPOTENCY_KEY_IN_PROGRESS", headers={"Retry-After": "2"})
    )
    with pytest.raises(IdempotencyKeyInProgressError) as exc:
        client.get_job("j")
    assert exc.value.retry_after == 2.0


@pytest.mark.parametrize("raw, parsed", [(0, 0), (3, 3), (3.0, 3), (-1, None), ("1", None), (True, None), (1.5, None)])
def test_only_a_whole_non_negative_index_is_kept(raw, parsed):
    client, _ = client_and_session(lambda *a, **k: error(400, "INVALID_URL", index=raw))
    with pytest.raises(InvalidUrlError) as exc:
        client.submit_urls(["https://example.com/a"])
    assert exc.value.index == parsed


def test_a_downloaded_file_error_keeps_its_old_type_and_gains_the_index():
    client, _ = client_and_session(lambda *a, **k: error(400, "INVALID_FILE", index=0))
    with pytest.raises(InvalidFileError) as exc:
        client.submit_urls(["https://example.com/a"])
    assert exc.value.index == 0


# --------------------------------------------------------------------------- #
# job fields: callback, replayed
# --------------------------------------------------------------------------- #
def test_job_reads_the_callback_status():
    job = Job.from_dict(
        {
            "jobId": "j",
            "status": "completed",
            "callback": {
                "url": "https://example.com/hook",
                "status": "pending",
                "attempts": 1,
                "lastAttemptAt": "2026-10-01 08:00:00",
                "lastResponseStatus": 503,
                "nextAttemptAt": "2026-10-01 08:01:00",
            },
        }
    )
    assert job.callback == CallbackStatus(
        url="https://example.com/hook",
        status="pending",
        attempts=1,
        last_attempt_at="2026-10-01 08:00:00",
        last_response_status=503,
        next_attempt_at="2026-10-01 08:01:00",
        raw=job.raw["callback"],
    )
    assert job.replayed is False


def test_a_callback_field_of_the_wrong_type_reads_as_none():
    job = Job.from_dict(
        {"jobId": "j", "status": "pending", "callback": {"status": "delivered", "attempts": "2", "lastResponseStatus": None}}
    )
    assert job.callback is not None
    assert job.callback.status == "delivered"
    assert job.callback.attempts is None
    assert job.callback.last_response_status is None
    assert Job.from_dict({"jobId": "j", "status": "pending", "callback": "x"}).callback is None


# --------------------------------------------------------------------------- #
# list_jobs / iter_jobs
# --------------------------------------------------------------------------- #
def test_list_jobs_sends_only_given_params_and_parses_the_page():
    client, session = client_and_session(
        lambda *a, **k: FakeResponse(
            200, {"data": [{"jobId": "a", "status": "completed", "creditsUsed": 3}], "nextCursor": "c2"}
        )
    )
    page = client.list_jobs(created_from="2026-10-01", created_to="", limit=0, cursor="c1")

    assert session.calls[0][1].endswith("/api/v1/jobs")
    assert session.calls[0][2]["params"] == {"createdFrom": "2026-10-01", "limit": 0, "cursor": "c1"}
    assert isinstance(page, JobList)
    assert [job.job_id for job in page.data] == ["a"]
    assert page.data[0].credits_used == 3
    assert page.next_cursor == "c2"


def test_a_list_without_an_array_is_malformed():
    client, _ = client_and_session(lambda *a, **k: FakeResponse(200, {"data": {}}))
    with pytest.raises(MalformedResponseError):
        client.list_jobs()


def test_iter_jobs_follows_the_cursor_and_waits_out_rate_limits(no_sleep):
    responses = iter([
        FakeResponse(200, {"data": [{"jobId": "a", "status": "completed"}], "nextCursor": "c2"}),
        error(429, "RATE_LIMITED", headers={"Retry-After": "4"}),
        FakeResponse(502),
        FakeResponse(200, {"data": [{"jobId": "b", "status": "failed"}], "nextCursor": None}),
    ])
    client, session = client_and_session(lambda *a, **k: next(responses))

    assert [job.job_id for job in client.iter_jobs(limit=1)] == ["a", "b"]
    assert [call[2]["params"] for call in session.calls] == [
        {"limit": 1},
        {"limit": 1, "cursor": "c2"},
        {"limit": 1, "cursor": "c2"},
        {"limit": 1, "cursor": "c2"},
    ]
    assert no_sleep == [4.0, 1.0]


def test_iter_jobs_gives_up_after_ten_attempts_on_a_page(no_sleep):
    client, session = client_and_session(lambda *a, **k: FakeResponse(503))
    with pytest.raises(ServerError):
        list(client.iter_jobs())
    assert len(session.calls) == 10


def test_iter_jobs_does_not_retry_a_bad_parameter(no_sleep):
    client, session = client_and_session(lambda *a, **k: error(400, "INVALID_PARAMETER"))
    with pytest.raises(InvalidParameterError):
        list(client.iter_jobs(limit=500))
    assert len(session.calls) == 1


def test_iter_jobs_gives_up_on_rate_limits_too(no_sleep):
    client, session = client_and_session(lambda *a, **k: error(429, "RATE_LIMITED"))
    with pytest.raises(RateLimitedError):
        list(client.iter_jobs())
    assert len(session.calls) == 10


# --------------------------------------------------------------------------- #
# verify_webhook — Standard Webhooks
# --------------------------------------------------------------------------- #
# The official test vector from the Standard Webhooks reference libraries.
VECTOR_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
VECTOR_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
VECTOR_TS = 1614265330
VECTOR_BODY = b'{"test": 2432232314}'
VECTOR_SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def vector_headers(**overrides):
    headers = {
        "webhook-id": VECTOR_ID,
        "webhook-timestamp": str(VECTOR_TS),
        "webhook-signature": VECTOR_SIG,
    }
    headers.update(overrides)
    return headers


def sign(secret, msg_id, ts, body):
    key = base64.b64decode(secret[len("whsec_"):])
    digest = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def test_the_official_vector_verifies():
    event = verify_webhook(VECTOR_BODY, vector_headers(), VECTOR_SECRET, now=VECTOR_TS)
    assert event.id == VECTOR_ID
    assert event.timestamp == VECTOR_TS
    assert event.raw == {"test": 2432232314}
    assert event.type is None and event.data == {}


def test_text_payload_and_mixed_case_headers_verify():
    headers = {key.title(): value for key, value in vector_headers().items()}
    verify_webhook(VECTOR_BODY.decode(), headers, VECTOR_SECRET, now=VECTOR_TS)


def test_a_bare_base64_secret_is_accepted():
    verify_webhook(VECTOR_BODY, vector_headers(), VECTOR_SECRET[len("whsec_"):], now=VECTOR_TS)


def test_a_tampered_body_fails():
    with pytest.raises(WebhookVerificationError):
        verify_webhook(b'{"test": 2432232315}', vector_headers(), VECTOR_SECRET, now=VECTOR_TS)


@pytest.mark.parametrize("skew", [-301, 301])
def test_a_timestamp_outside_the_window_fails_either_way(skew):
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_webhook(VECTOR_BODY, vector_headers(), VECTOR_SECRET, now=VECTOR_TS + skew)


@pytest.mark.parametrize("skew", [-300, 300])
def test_the_window_edge_is_inside(skew):
    verify_webhook(VECTOR_BODY, vector_headers(), VECTOR_SECRET, now=VECTOR_TS + skew)


def test_any_one_v1_signature_among_several_passes():
    wrong = "v1,Zm9vYmFy" + "A" * 34
    header = f"v2,abc {wrong}  {VECTOR_SIG}"
    verify_webhook(VECTOR_BODY, vector_headers(**{"webhook-signature": header}), VECTOR_SECRET, now=VECTOR_TS)


def test_a_non_v1_signature_alone_fails():
    header = VECTOR_SIG.replace("v1,", "v2,")
    with pytest.raises(WebhookVerificationError):
        verify_webhook(VECTOR_BODY, vector_headers(**{"webhook-signature": header}), VECTOR_SECRET, now=VECTOR_TS)


def test_a_non_ascii_signature_fails_cleanly():
    with pytest.raises(WebhookVerificationError):
        verify_webhook(
            VECTOR_BODY, vector_headers(**{"webhook-signature": "v1,签名"}), VECTOR_SECRET, now=VECTOR_TS
        )


@pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp", "webhook-signature"])
def test_a_missing_header_fails(missing):
    headers = vector_headers()
    del headers[missing]
    with pytest.raises(WebhookVerificationError, match="missing"):
        verify_webhook(VECTOR_BODY, headers, VECTOR_SECRET, now=VECTOR_TS)


@pytest.mark.parametrize("ts", ["1614265330.0", "-1", "１６１４２６５３３０", "abc"])
def test_a_timestamp_that_is_not_plain_seconds_fails(ts):
    with pytest.raises(WebhookVerificationError):
        verify_webhook(VECTOR_BODY, vector_headers(**{"webhook-timestamp": ts}), VECTOR_SECRET, now=VECTOR_TS)


@pytest.mark.parametrize("secret", ["whsec_", "whsec_not base64!", "whsec_abc", 123])
def test_a_malformed_secret_is_a_configuration_error(secret):
    with pytest.raises((ValueError, TypeError)) as exc:
        verify_webhook(VECTOR_BODY, vector_headers(), secret, now=VECTOR_TS)
    assert not isinstance(exc.value, WebhookVerificationError)


def test_an_already_parsed_body_is_a_type_error():
    with pytest.raises(TypeError, match="raw request body"):
        verify_webhook({"test": 2432232314}, vector_headers(), VECTOR_SECRET, now=VECTOR_TS)  # type: ignore[arg-type]


def test_a_real_event_exposes_its_job():
    secret = "whsec_" + base64.b64encode(bytes(range(32))).decode()
    body = json.dumps(
        {"type": "job.completed", "data": {"jobId": "j", "status": "completed", "creditsUsed": 2}}
    ).encode()
    headers = {
        "webhook-id": "msg_1",
        "webhook-timestamp": "1700000000",
        "webhook-signature": sign(secret, "msg_1", 1700000000, body),
    }
    event = verify_webhook(body, headers, secret, now=1700000010)
    assert event.type == "job.completed"
    assert event.job.job_id == "j" and event.job.credits_used == 2


def test_a_signed_body_that_is_not_an_object_fails():
    secret = "whsec_" + base64.b64encode(bytes(32)).decode()
    body = b"[1, 2]"
    headers = {
        "webhook-id": "m",
        "webhook-timestamp": "100",
        "webhook-signature": sign(secret, "m", 100, body),
    }
    with pytest.raises(WebhookVerificationError, match="JSON object"):
        verify_webhook(body, headers, secret, now=100)


def test_the_transport_error_path_is_unchanged_for_reads(no_sleep):
    """A dropped connection on a read still raises straight away from list_jobs."""

    def handler(*_a, **_k):
        raise requests.exceptions.ConnectionError("reset")

    client, session = client_and_session(handler)
    with pytest.raises(APIConnectionError):
        client.list_jobs()
    assert len(session.calls) == 1


# --------------------------------------------------------------------------- #
# fixes from the PR review round
# --------------------------------------------------------------------------- #
def test_paths_may_be_path_objects_as_before(images):
    from pathlib import Path

    client, session = client_and_session(created())
    client.submit([Path(p) for p in images(2)])
    assert len(session.calls) == 1


def test_a_file_error_during_a_resend_still_carries_the_key(pdf, no_sleep):
    import os

    state = {"n": 0}

    def handler(*_a, **_k):
        state["n"] += 1
        os.remove(pdf)  # gone before the resend reopens it
        raise requests.exceptions.ConnectionError("reset")

    client, _ = client_and_session(handler)
    with pytest.raises(OSError) as exc:
        client.submit([pdf], idempotency_key="order-7")
    assert exc.value.idempotency_key == "order-7"
    assert state["n"] == 1


def test_submit_urls_resends_under_the_same_generated_key(no_sleep):
    responses = iter([FakeResponse(503), FakeResponse(201, {"jobId": "j", "status": "pending"})])
    client, session = client_and_session(lambda *a, **k: next(responses))

    client.submit_urls(["https://example.com/a.png"])

    keys = [call[2]["headers"]["Idempotency-Key"] for call in session.calls]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert session.calls[0][2]["json"] == session.calls[1][2]["json"]


def test_surrounding_whitespace_does_not_count_towards_the_length():
    assert parse_pages("1" + " " * 1000) == [(1, 1)]


def test_a_known_total_reports_out_of_range_before_too_many(images):
    client, session = client_and_session(created())
    with pytest.raises(PagesOutOfRangeError):
        client.submit(images(3), pages="1-60")
    assert session.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tolerance_seconds": float("nan")},
        {"tolerance_seconds": -1},
        {"now": float("nan")},
        {"now": float("inf")},
    ],
)
def test_clock_options_must_be_finite(kwargs):
    kwargs.setdefault("now", VECTOR_TS)
    with pytest.raises(ValueError):
        verify_webhook(VECTOR_BODY, vector_headers(), VECTOR_SECRET, **kwargs)


@pytest.mark.parametrize("digits", [13, 309, 5000])
def test_an_absurdly_long_timestamp_is_refused_as_a_bad_delivery(digits):
    with pytest.raises(WebhookVerificationError):
        verify_webhook(
            VECTOR_BODY, vector_headers(**{"webhook-timestamp": "1" * digits}), VECTOR_SECRET
        )


def test_a_batch_that_grew_since_planning_is_refused_before_sending(images, monkeypatch):
    import dataclasses

    from image2ppt import MAX_UPLOAD_BYTES

    client, session = client_and_session(created())
    paths = images(2)
    real_prepare = client._prepare_file
    calls = {"n": 0}

    def prepare(path):
        calls["n"] += 1
        item = real_prepare(path)
        if calls["n"] > len(paths):  # after planning: the file "grew" on disk
            item = dataclasses.replace(item, size=MAX_UPLOAD_BYTES)
        return item

    monkeypatch.setattr(client, "_prepare_file", prepare)
    with pytest.raises(InvalidFileError):
        client.submit_all(paths)
    assert session.calls == []


def test_convert_hands_back_the_key_when_the_job_fails_after_submission(images, tmp_path):
    from image2ppt import JobFailedError

    def handler(method, url, **kwargs):
        if method == "POST":
            return FakeResponse(201, {"jobId": "j", "status": "pending"})
        return FakeResponse(
            200,
            {"jobId": "j", "status": "failed", "error": {"code": "CONVERSION_FAILED", "message": "x"}},
        )

    client, session = client_and_session(handler)
    with pytest.raises(JobFailedError) as exc:
        client.convert(images(1), str(tmp_path / "out.pptx"), poll_interval=0)
    sent = session.calls[0][2]["headers"]["Idempotency-Key"]
    assert exc.value.idempotency_key == sent
