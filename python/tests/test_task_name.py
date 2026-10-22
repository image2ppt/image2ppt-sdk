"""Task names on the wire and in job snapshots; all HTTP is mocked."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
import requests

from image2ppt import (
    IdempotencyKeyMismatchError,
    Image2PPTClient,
    InvalidNameError,
    Job,
    verify_webhook,
)
from test_client import FakeResponse, FakeSession


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "slides.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    return str(path)


def setup(handler):
    session = FakeSession(handler)
    return Image2PPTClient("i2p_live_test", session=session), session


def created(name="Quarterly report (2)", job_id="j"):
    return FakeResponse(201, {"jobId": job_id, "status": "pending", "name": name})


def test_multipart_name_and_resolved_create_response(pdf):
    client, session = setup(lambda *a, **k: created())
    job = client.submit([pdf], name="Quarterly report")
    assert session.calls[0][2]["data"] == {"name": "Quarterly report"}
    assert job.name == "Quarterly report (2)"


def test_url_submission_name():
    client, session = setup(lambda *a, **k: created())
    client.submit_urls(["https://example.com/slides.pdf"], name="Quarterly report")
    assert session.calls[0][2]["json"] == {
        "urls": ["https://example.com/slides.pdf"], "name": "Quarterly report"
    }


def test_unset_name_is_omitted_in_both_formats(pdf):
    client, session = setup(lambda *a, **k: created())
    client.submit([pdf])
    client.submit_urls(["https://example.com/slides.pdf"])
    assert "name" not in session.calls[0][2]["data"]
    assert "name" not in session.calls[1][2]["json"]


def test_status_and_list_names_reflect_website_renames():
    responses = iter([
        FakeResponse(200, {"jobId": "j", "status": "completed", "name": "Renamed"}),
        FakeResponse(200, {"data": [{"jobId": "j", "status": "completed", "name": "Renamed again"}]}),
    ])
    client, _ = setup(lambda *a, **k: next(responses))
    assert client.get_job("j").name == "Renamed"
    assert client.list_jobs().data[0].name == "Renamed again"


@pytest.mark.parametrize("data", [{}, {"name": None}, {"name": 42}])
def test_older_server_or_unusable_name_is_none(data):
    assert Job.from_dict({"jobId": "j", "status": "pending", **data}).name is None


def test_new_field_preserves_positional_job_constructor():
    assert Job("j", "pending", 3).slide_count == 3
    assert Job("j", "pending").name is None


def test_verified_webhook_carries_job_name():
    secret_bytes = b"test-secret"
    body = json.dumps({"type": "job.completed", "data": {
        "jobId": "j", "status": "completed", "name": "Final name"
    }}).encode()
    timestamp = 1700000000
    signature = base64.b64encode(hmac.new(
        secret_bytes, b"msg.1700000000." + body, hashlib.sha256
    ).digest()).decode()
    event = verify_webhook(body, {
        "webhook-id": "msg", "webhook-timestamp": str(timestamp),
        "webhook-signature": "v1," + signature,
    }, "whsec_" + base64.b64encode(secret_bytes).decode(), now=timestamp)
    assert event.data["name"] == "Final name"
    assert Job.from_dict(event.data).name == "Final name"


def test_invalid_name_http_error_is_typed_and_not_retried():
    client, session = setup(lambda *a, **k: FakeResponse(400, {
        "error": {"code": "INVALID_NAME", "message": "invalid task name"}
    }))
    with pytest.raises(InvalidNameError) as caught:
        client.submit_urls(["https://example.com/slides.pdf"], name="Server refused")
    assert caught.value.status_code == 400
    assert caught.value.code == "INVALID_NAME"
    assert not caught.value.is_transient
    assert len(session.calls) == 1


def test_retry_keeps_the_name_and_idempotency_key(pdf, no_sleep):
    def handler(*a, **k):
        if len(session.calls) == 1:
            raise requests.ConnectionError("lost response")
        return created()

    client, session = setup(handler)
    client.submit([pdf], name="Quarterly report", idempotency_key="stable-key")
    assert len(session.calls) == 2
    for _, _, kwargs in session.calls:
        assert kwargs["data"] == {"name": "Quarterly report"}
        assert kwargs["headers"]["Idempotency-Key"] == "stable-key"


def test_changed_name_with_same_key_surfaces_conflict():
    client, session = setup(lambda *a, **k: created() if len(session.calls) == 1 else FakeResponse(
        422, {"error": {"code": "IDEMPOTENCY_KEY_MISMATCH", "message": "different name"}}
    ))
    client.submit_urls(["https://example.com/slides.pdf"], name="Original", idempotency_key="key")
    with pytest.raises(IdempotencyKeyMismatchError):
        client.submit_urls(["https://example.com/slides.pdf"], name="Changed", idempotency_key="key")
    assert [call[2]["json"]["name"] for call in session.calls] == ["Original", "Changed"]


def completed_handler(method, url, **kwargs):
    if method == "POST":
        return created(name="Server name (2)")
    if url.endswith("/download"):
        return FakeResponse(200, content=b"PPTX", headers={
            "Content-Disposition": 'attachment; filename="Server name (2).pptx"'
        })
    return FakeResponse(200, {"jobId": "j", "status": "completed", "name": "Server name (2)"})


def test_convert_forwards_name_and_writes_only_callers_path(pdf, tmp_path):
    client, session = setup(completed_handler)
    dest = tmp_path / "chosen.pptx"
    job = client.convert([pdf], str(dest), name="Server name", poll_interval=0)
    assert session.calls[0][2]["data"] == {"name": "Server name"}
    assert job.name == "Server name (2)"
    assert dest.read_bytes() == b"PPTX"
    assert not (tmp_path / "Server name (2).pptx").exists()


def test_convert_all_reuses_name_and_keeps_part_filenames(pdf, tmp_path):
    client, session = setup(completed_handler)
    dest_dir = tmp_path / "decks"
    paths = client.convert_all([pdf, pdf], str(dest_dir), name="Batch name", poll_interval=0)
    assert paths == [str(dest_dir / "part-01.pptx"), str(dest_dir / "part-02.pptx")]
    posts = [kwargs for method, _, kwargs in session.calls if method == "POST"]
    assert [kwargs["data"] for kwargs in posts] == [{"name": "Batch name"}] * 2
    assert len({kwargs["headers"]["Idempotency-Key"] for kwargs in posts}) == 2
    assert all(path.read_bytes() == b"PPTX" for path in dest_dir.iterdir())


@pytest.mark.parametrize("method", ["submit", "submit_urls"])
@pytest.mark.parametrize("name", [42, True, []])
def test_non_string_name_is_rejected_before_http(method, name, pdf):
    client, session = setup(lambda *a, **k: created())
    inputs = [pdf] if method == "submit" else ["https://example.com/slides.pdf"]
    with pytest.raises(TypeError, match="name must be a string"):
        getattr(client, method)(inputs, name=name)
    assert session.calls == []


@pytest.mark.parametrize("name", ["", "  Quarterly\u200b  report  ", "😀" * 60])
def test_name_contents_are_sent_unchanged_for_service_validation(name, pdf):
    client, session = setup(lambda *a, **k: created())
    client.submit([pdf], name=name)
    client.submit_urls(["https://example.com/slides.pdf"], name=name)
    assert session.calls[0][2]["data"]["name"] == name
    assert session.calls[1][2]["json"]["name"] == name
