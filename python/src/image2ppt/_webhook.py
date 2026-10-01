"""Verifying completion callbacks (the `Standard Webhooks <https://www.standardwebhooks.com>`_ scheme).

A job submitted with ``callback_url`` is announced by a ``POST`` to that URL when it
ends. Anyone can send a POST, so check every delivery with ``verify_webhook`` before
acting on it::

    from image2ppt import verify_webhook, WebhookVerificationError

    try:
        event = verify_webhook(request.body, request.headers, "whsec_...")
    except WebhookVerificationError:
        return 400
    if event.type == "job.completed":
        ...
    return 204   # any 2xx within 10 seconds counts as delivered

Every rule here is pinned identically in the Node client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Union

from .errors import WebhookVerificationError
from .models import Job

#: How far ``webhook-timestamp`` may be from the receiver's clock, either way.
DEFAULT_TOLERANCE_SECONDS = 300

_SECRET_PREFIX = "whsec_"
#: Strict standard base64. Decoders differ in what they forgive — Node's skips any
#: character it does not know — so the secret is checked against the alphabet first
#: and both clients agree on which secrets are malformed.
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_TIMESTAMP = re.compile(r"[0-9]+")
#: Unix seconds stay within 12 digits for tens of thousands of years; anything longer
#: is refused before it is turned into a number.
_MAX_TIMESTAMP_DIGITS = 12


@dataclass
class WebhookEvent:
    """A verified callback.

    ``type`` is ``job.completed`` or ``job.failed`` today; **ignore types you do not
    recognise** (still answer 2xx) — more may be added. ``data`` is the job, shaped
    like ``get_job``'s response without ``callback``; ``job`` parses it for you.
    ``id`` stays the same across retries of one delivery, so use it to drop
    duplicates.
    """

    id: str
    timestamp: int
    type: Optional[str]
    data: Dict[str, Any]
    raw: Dict[str, Any]

    @property
    def job(self) -> Job:
        """``data`` as a ``Job``. Raises ``MalformedResponseError`` if it is not one."""
        return Job.from_dict(self.data)


def verify_webhook(
    payload: Union[bytes, bytearray, memoryview, str],
    headers: Mapping[str, Any],
    secret: str,
    *,
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> WebhookEvent:
    """Check a callback's signature and timestamp; return the event it carries.

    Args:
        payload: The request body **exactly as received** — bytes, or the same text
            decoded as UTF-8. Parsing the JSON and re-serialising it changes the bytes
            and the signature will not match.
        headers: The request headers, any mapping; names are matched ignoring case.
        secret: The callback signing secret from the Developer / API page
            (``whsec_...``). During a rotation the service signs with the old and the
            new one for 24 hours, so a receiver holding either passes.
        tolerance_seconds: Largest allowed gap between ``webhook-timestamp`` and now,
            either way. Default 300.
        now: Current Unix time in seconds, for tests; defaults to the clock.

    Raises:
        WebhookVerificationError: Missing header, timestamp outside the tolerance, no
            matching ``v1`` signature, or a verified body that is not a JSON object.
            Refuse the delivery.
        ValueError: ``secret`` is not a ``whsec_`` + base64 secret. That is a
            configuration mistake on the receiving side, not a bad delivery, so it is
            kept apart: answering every genuine callback with 4xx would hide it.
        TypeError: ``payload`` is not bytes or text — typically a body a framework
            has already parsed into a dict. Pass the raw body instead.
    """
    key = _decode_secret(secret)
    body = _payload_bytes(payload)
    if not _finite(tolerance_seconds) or tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be a finite, non-negative number")
    if now is not None and not _finite(now):
        raise ValueError("now must be a finite number of Unix seconds")

    msg_id = _header(headers, "webhook-id")
    timestamp = _header(headers, "webhook-timestamp")
    signatures = _header(headers, "webhook-signature")
    if not msg_id or not timestamp or not signatures:
        raise WebhookVerificationError(
            "missing webhook-id, webhook-timestamp or webhook-signature header"
        )
    if not _TIMESTAMP.fullmatch(timestamp):
        raise WebhookVerificationError(f"webhook-timestamp {timestamp!r} is not Unix seconds")
    if len(timestamp) > _MAX_TIMESTAMP_DIGITS:
        # Far outside any window, and too long to do arithmetic on safely.
        raise WebhookVerificationError("webhook-timestamp is outside the tolerance")
    sent_at = int(timestamp)
    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance_seconds:
        raise WebhookVerificationError(
            f"webhook-timestamp is {int(current - sent_at)}s from now, outside the "
            f"{tolerance_seconds:g}s tolerance"
        )

    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    # Split on the ASCII space the scheme uses, nothing wider: Python's bare split()
    # and a JavaScript /\s+/ disagree about what whitespace is.
    for entry in signatures.split(" "):
        version, _, signature = entry.partition(",")
        # Bytes on both sides: compare_digest refuses a str with non-ASCII characters.
        if version == "v1" and hmac.compare_digest(signature.encode("utf-8"), expected):
            return _event(msg_id, sent_at, body)
    raise WebhookVerificationError("no webhook-signature matches this payload and secret")


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _decode_secret(secret: str) -> bytes:
    """The HMAC key: the base64 after ``whsec_`` (a bare base64 secret is accepted too)."""
    if not isinstance(secret, str):
        raise TypeError(f"secret must be a string, not {type(secret).__name__}")
    encoded = secret[len(_SECRET_PREFIX) :] if secret.startswith(_SECRET_PREFIX) else secret
    if not encoded or not _BASE64.fullmatch(encoded):
        raise ValueError("secret is not a webhook signing secret (expected whsec_<base64>)")
    return base64.b64decode(encoded)


def _payload_bytes(payload: Union[bytes, bytearray, memoryview, str]) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    raise TypeError(
        f"payload must be the raw request body (bytes or str), not {type(payload).__name__}"
    )


def _header(headers: Mapping[str, Any], name: str) -> Optional[str]:
    """Case-insensitive lookup; a list of values (some frameworks) is joined by spaces."""
    for key, value in headers.items():
        if str(key).lower() == name:
            if isinstance(value, (list, tuple)):
                return " ".join(str(item) for item in value)
            return None if value is None else str(value)
    return None


def _event(msg_id: str, sent_at: int, body: bytes) -> WebhookEvent:
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise WebhookVerificationError("signature matches, but the body is not a JSON object")
    event_type = parsed.get("type")
    data = parsed.get("data")
    return WebhookEvent(
        id=msg_id,
        timestamp=sent_at,
        type=event_type if isinstance(event_type, str) else None,
        data=data if isinstance(data, dict) else {},
        raw=parsed,
    )
