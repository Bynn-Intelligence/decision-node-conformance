"""A minimal reference client used by the runner to exercise the cases.

It implements the neutral SDK surface with the standard library only. It is not a
product SDK: it exists so the suite can prove that every case is consistent and that
the mock answers it. SDKs are written by hand in each language and checked against
the same case files.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import ipaddress
import math
import queue
import re
import struct
from decimal import Decimal
import email.utils
import http.client
import json
import os
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

VERSION = "1.0.0"
SPEC_VERSION = "1.3"
DEFAULT_INLINE_THRESHOLD = 524288  # local images under this many bytes in all go inline as base64
DEFAULT_UPLOAD_TIMEOUT = 60.0  # seconds for one storage PUT attempt, apart from the API timeout
DEFAULT_TIMEOUT = 10.0  # seconds for one API attempt
OCR_TIMEOUT = 30.0  # the default for a decision whose boosters name ocr
DEFAULT_BASE_URL = "https://api.decisionnode.com"
DEFAULT_MODEL = "decisionnode-moe"
RETRY_STATUSES = {408, 429} | set(range(500, 600))
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_SERVER_WAIT = 60.0

_UNSET = object()


# --------------------------------------------------------------------------- errors


class DecisionNodeError(Exception):
    def __init__(self, message=None, status=None, error_type=None, request_id=None, reason=None, body=None, headers=None, boosters=None, details=None):
        # The error's own message is the server's message, unchanged (empty when the
        # server sent none), or the client's description when there was no response.
        super().__init__(message or "")
        self.status = status
        self.error_type = error_type
        self.message = message or ""
        self.request_id = request_id
        self.reason = reason
        self.body = body
        self.headers = headers or {}
        # Spec 1.3: x-decisionnode-boosters parsed, booster name to {status, latency_ms}
        # in header order; empty when the header is absent.
        self.boosters = boosters if boosters is not None else {}
        # Suite 1.15: detail.details when it is an object (an enrollment's 529 says why in
        # its reason), else empty.
        self.details = details


class BadRequestError(DecisionNodeError):
    pass


class ContextLimitError(BadRequestError):
    pass


class AuthenticationError(DecisionNodeError):
    pass


class InsufficientCreditError(DecisionNodeError):
    pass


class RefusalError(DecisionNodeError):
    def __init__(self, *a, category=None, safety=None, **kw):
        super().__init__(*a, **kw)
        self.category = category
        self.safety = safety


class PermissionDeniedError(DecisionNodeError):
    pass


class NotFoundError(DecisionNodeError):
    pass


class ValidationError(DecisionNodeError):
    def __init__(self, *a, entries=None, **kw):
        super().__init__(*a, **kw)
        self.entries = entries


class RateLimitError(DecisionNodeError):
    def __init__(self, *a, retry_after=None, **kw):
        super().__init__(*a, **kw)
        self.retry_after = retry_after


class OverloadedError(DecisionNodeError):
    def __init__(self, *a, retry_after=None, **kw):
        super().__init__(*a, **kw)
        self.retry_after = retry_after


class ServerError(DecisionNodeError):
    pass


class ConnectionError(DecisionNodeError):  # noqa: A001 - the neutral name
    pass


class TimeoutError(DecisionNodeError):  # noqa: A001 - the neutral name
    pass


class RequestTooLargeError(DecisionNodeError):
    """Spec 1.3: a body over 26,214,400 bytes (25 MiB), refused locally or by the API's 413."""


class SessionLimitError(RateLimitError):
    """A 429 session_limit_exceeded: the key holds sessions.max_per_key open sessions."""


# --------------------------------------------------------------------------- builders


def truth(instructions=None, if_true=None, if_false=None) -> dict:
    q = {"type": "truth"}
    if instructions is not None:
        q["instructions"] = instructions
    criteria = {}
    if if_true is not None:
        criteria["true"] = if_true
    if if_false is not None:
        criteria["false"] = if_false
    if criteria:
        q["criteria"] = criteria
    return q


def choice(instructions=None, options=None) -> dict:
    if isinstance(options, (list, tuple)):
        criteria = {name: None for name in options}
    else:
        criteria = dict(options or {})
    q = {"type": "choice"}
    if instructions is not None:
        q["instructions"] = instructions
    q["criteria"] = criteria
    return q


def score(instructions=None, levels=None) -> dict:
    q = {"type": "score"}
    if instructions is not None:
        q["instructions"] = instructions
    q["criteria"] = list(levels or [])
    return q


def sniff_media_type(data: bytes):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class Image:
    def __init__(self, id, path=None, data=None, media_type=None, url=None, upload_id=None):
        self.id = id
        if path is not None:
            # Read now, so an unreadable file fails where the Image is made.
            with open(path, "rb") as fh:
                data = fh.read()
            media_type = None
        # The bytes are checked locally before any request (magic bytes, size, pixels):
        # see check_images. A declared media_type must match the bytes.
        self.declared = media_type
        self.data = data
        self.media_type = (media_type or sniff_media_type(data)) if data is not None else media_type
        self.url = url
        self.upload_id = upload_id

    def wire(self) -> dict:
        if self.url is not None:
            return {"id": self.id, "url": self.url}
        if self.upload_id is not None:
            out = {"id": self.id, "upload_id": self.upload_id}
            if self.media_type:
                out["media_type"] = self.media_type
            return out
        return {"id": self.id, "media_type": self.media_type, "data": base64.b64encode(self.data).decode("ascii")}


class VideoFrame:
    """Spec 1.2: one frame of a video, its time in seconds and its image as bytes or base64
    text. Frames always go inline (the API takes no url or upload_id for a frame)."""

    def __init__(self, t, data, media_type=None):
        self.t = t
        self.data = data
        self.declared = media_type
        if media_type is None and isinstance(data, (bytes, bytearray)):
            media_type = sniff_media_type(bytes(data))
        self.media_type = media_type

    def wire(self) -> dict:
        data = self.data
        if isinstance(data, (bytes, bytearray)):
            data = base64.b64encode(bytes(data)).decode("ascii")
        return {"t": self.t, "media_type": self.media_type, "data": data}


class Video:
    """Spec 1.2: a video as the frames the caller sampled; fps, start and end are left out
    of the wire form when not given, so the server's defaults apply."""

    def __init__(self, id, frames, fps=None, start=None, end=None):
        self.id = id
        self.frames = frames
        self.fps = fps
        self.start = start
        self.end = end

    def wire(self) -> dict:
        out = {"id": self.id}
        for name in ("fps", "start", "end"):
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        frames = self.frames if isinstance(self.frames, list) else self.frames
        out["frames"] = [f.wire() if isinstance(f, VideoFrame) else f for f in frames] if isinstance(frames, list) else frames
        return out


def video_wires(videos):
    return [v.wire() if isinstance(v, Video) else v for v in videos]


# --------------------------------------------------------------------------- results


def parse_safety(value):
    if value is None:
        return None
    value = value.strip()
    if value.startswith("unchecked"):
        reason = None
        for param in value.split(";")[1:]:
            k, _, v = param.strip().partition("=")
            if k == "reason":
                reason = v.strip().strip('"')
        return {"checked": False, "reason": reason}
    out = {"checked": True, "harm": None, "self_harm": None, "flagged": []}
    for member in value.split(","):
        member = member.strip()
        if not member:
            continue
        head, *params = member.split(";")
        name, _, num = head.partition("=")
        name = name.strip()
        try:
            out[name] = float(num)
        except ValueError:
            out[name] = None
        if any(p.strip() in ("flagged", "blocked") for p in params):
            out["flagged"].append(name)
    return out


class Answer:
    kind = None

    def __init__(self, raw: dict):
        self.raw = raw
        self.type = raw.get("type")


class UnknownAnswer(Answer):
    """An answer of a type this client does not know: its type and raw fields."""

    kind = "unknown"


class TruthAnswer(Answer):
    kind = "truth"

    def __init__(self, raw):
        super().__init__(raw)
        self.truth = raw["truth"]


class ChoiceAnswer(Answer):
    kind = "choice"

    def __init__(self, raw):
        super().__init__(raw)
        self.choice = raw["choice"]
        self.confidence = raw["confidence"]
        self.probabilities = dict(raw["probabilities"])


class ScoreAnswer(Answer):
    kind = "score"

    def __init__(self, raw):
        super().__init__(raw)
        self.score = raw["score"]
        self.confidence = raw["confidence"]
        self.probabilities = {int(k): v for k, v in raw["probabilities"].items()}
        self.legend = {int(k): v for k, v in raw["legend"].items()}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _number_map(v) -> bool:
    return isinstance(v, dict) and all(_number(x) for x in v.values())


def _index_keys(v) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and k.isdigit() for k in v)


def _well_formed(raw: dict, kind: str) -> bool:
    if kind == "truth":
        return _number(raw.get("truth"))
    if kind == "choice":
        return isinstance(raw.get("choice"), str) and _number(raw.get("confidence")) and _number_map(raw.get("probabilities"))
    if kind == "score":
        probs, legend = raw.get("probabilities"), raw.get("legend")
        return (
            _number(raw.get("score"))
            and _number(raw.get("confidence"))
            and _number_map(probs)
            and _index_keys(probs)
            and _index_keys(legend)
        )
    return False


def parse_answer(raw) -> Answer:
    """A typed answer, or UnknownAnswer for an unknown type or a known type whose
    fields are missing or of the wrong type: one odd answer never fails the decision."""
    if not isinstance(raw, dict):
        return UnknownAnswer({"value": raw})
    kind = raw.get("type")
    classes = {"truth": TruthAnswer, "choice": ChoiceAnswer, "score": ScoreAnswer}
    if kind in classes and _well_formed(raw, kind):
        return classes[kind](raw)
    return UnknownAnswer(raw)


class Decision:
    def __init__(self, body: dict, headers: dict):
        self.raw = body
        self.model = body.get("model")
        self.answers = {k: parse_answer(v) for k, v in (body.get("answers") or {}).items()}
        usage = body.get("usage") or {}
        self.input_tokens = usage.get("input_tokens")
        self.output_tokens = usage.get("output_tokens")
        # Spec 1.2: only a request with videos carries these; empty when absent, never 0.
        self.video_frames = usage.get("video_frames")
        self.video_seconds = usage.get("video_seconds")
        warnings = body.get("warnings")
        self.warnings = [w for w in warnings] if isinstance(warnings, list) else []
        # Spec 1.3: only a request that named boosters carries these; empty when absent.
        evidence = body.get("evidence")
        self.evidence = {name: Evidence(name, e) for name, e in evidence.items()} if isinstance(evidence, dict) else None
        self.booster_tokens = usage.get("booster_tokens")
        self.booster_calls = usage.get("boosters")
        # Spec 1.3: what the answer cost, as received; never summed or recomputed here.
        cost = usage.get("cost")
        self.cost = Cost(cost) if isinstance(cost, dict) else None
        # Spec 1.3: a fallback answer names the model that answered and the one asked for.
        served = headers.get("x-decisionnode-fallback")
        self.fallback = {"served_model": served, "requested_model": headers.get("x-decisionnode-requested-model")} if served is not None else None
        self.request_id = headers.get("x-request-id")
        lat = headers.get("x-decisionnode-latency-ms")
        self.latency_ms = None
        if lat is not None:
            try:
                f = float(lat)
                self.latency_ms = int(f) if f.is_integer() else f
            except ValueError:
                self.latency_ms = None
        self.safety = parse_safety(headers.get("x-decisionnode-safety"))

    def __getitem__(self, key):
        return self.answers[key]


class ModelInfo:
    def __init__(self, raw: dict):
        self.raw = raw
        self.name = raw.get("name")
        self.description = raw.get("description")
        self.release_date = raw.get("release_date")
        self.limits = {k: v for k, v in raw.items() if k not in ("name", "description", "release_date")}
        # Spec 1.3: a fine-tuned model's entry; a catalogue entry has none of them.
        self.fine_tuned = raw.get("fine_tuned")
        self.base = raw.get("base")
        self.adapter_version = raw.get("adapter_version")
        self.status = raw.get("status")


# --------------------------------------------------------------------------- client


class _Response:
    def __init__(self, status, headers, text):
        self.status = status
        self.headers = headers
        self.text = text


class DecisionNode:
    def __init__(self, api_key=None, base_url=None, model=None, timeout=None, max_retries=2, headers=None, http_client=None, inline_threshold=None, upload_timeout=None):
        # An explicitly empty value counts as not given: the environment, then the default.
        self.api_key = api_key or os.environ.get("DECISIONNODE_API_KEY") or None
        self.base_url = (base_url or os.environ.get("DECISIONNODE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("DECISIONNODE_MODEL") or DEFAULT_MODEL
        # A timeout the caller sets is used as given, on every call; left
        # out, 10 s, or 30 s for a decision whose boosters name ocr.
        self._timeout_given = timeout is not None
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
            raise ValueError("timeout must be a number of seconds above 0, got %r" % (timeout,))
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be an integer of 0 or more, got %r" % (max_retries,))
        parts = urlsplit(self.base_url)
        # A key never travels unencrypted: plain http only to this machine.
        if parts.scheme == "http":
            if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
                raise ValueError("base_url must use https; http is accepted only for localhost, 127.0.0.1 and ::1, got %r" % self.base_url)
        elif parts.scheme != "https" or not parts.hostname:
            raise ValueError("base_url must be an https URL, got %r" % self.base_url)
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.headers = dict(headers or {})
        self._opener = _without_redirects(http_client) if http_client is not None else None
        self._local = threading.local()
        self._sleep = time.sleep  # the wait between attempts; tests may replace it
        self._clock = time.time  # the time uploads and URLs are judged by; tests may replace it
        self._held = {}  # model name -> its /v1/models entry, once models() has been called
        if inline_threshold is not None and (isinstance(inline_threshold, bool) or not isinstance(inline_threshold, int) or inline_threshold < 0):
            raise ValueError("inline_threshold must be an integer of 0 or more, got %r" % (inline_threshold,))
        self.inline_threshold = DEFAULT_INLINE_THRESHOLD if inline_threshold is None else inline_threshold
        # Suite 1.6: the storage PUT has its own timeout (default 60 s), apart from the
        # API attempt's: an image of 10 MB on a slow uplink needs more than 10 s.
        if upload_timeout is not None and (isinstance(upload_timeout, bool) or not isinstance(upload_timeout, (int, float)) or not upload_timeout > 0):
            raise ValueError("upload_timeout must be a number of seconds above 0, got %r" % (upload_timeout,))
        self.upload_timeout = DEFAULT_UPLOAD_TIMEOUT if upload_timeout is None else float(upload_timeout)
        self._cache = collections.OrderedDict()  # (sha256, media type) -> (upload_id, expires_at)
        self._cache_lock = threading.Lock()
        self.batches = Batches(self)
        self.collections = Collections(self)
        self.usage = Usage(self)
        self.keys = Keys(self)

    # ---- transport

    def _connection(self):
        parts = urlsplit(self.base_url)
        conn = getattr(self._local, "conn", None)
        if conn is None:
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = cls(parts.hostname, parts.port, timeout=self.timeout)
            self._local.conn = conn
        return conn, parts.path

    def _drop(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
        self._local.conn = None

    def _send_once(self, method, path, body, content_type=None, timeout=None, extra_headers=None):
        # One deadline for the whole attempt: connecting, sending and every byte of the body.
        timeout = timeout or self.timeout
        deadline = time.monotonic() + timeout
        if self._opener is not None:
            return self._send_with_opener(method, path, body, deadline, content_type, timeout, extra_headers)
        conn, prefix = self._connection()
        headers = self._headers(body, content_type, extra_headers)
        data = _encode(body)
        try:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request(method, prefix + path, body=data, headers=headers)
            _set_remaining(conn.sock, deadline)
            resp = conn.getresponse()
            text = _read_body(resp, conn.sock, deadline)
        except (socket.timeout, TimeoutError_builtin) as exc:
            self._drop()
            raise TimeoutError("the request timed out after %ss" % timeout) from exc
        except (OSError, http.client.HTTPException) as exc:
            self._drop()
            raise ConnectionError("connection error: %s" % exc) from exc
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        if resp.will_close:
            self._drop()
        return _Response(resp.status, hdrs, text)

    def _headers(self, body, content_type=None, extra=None):
        headers = {"Authorization": "Bearer %s" % self.api_key, "User-Agent": "decisionnode-python/%s" % VERSION, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        headers.update(self.headers)
        headers.update(extra or {})
        return headers

    def _send_with_opener(self, method, path, body, deadline, content_type=None, timeout=None, extra_headers=None):
        timeout = timeout or self.timeout
        data = _encode(body)
        req = urllib.request.Request(self.base_url + path, data=data, headers=self._headers(body, content_type, extra_headers), method=method)
        try:
            try:
                resp = self._opener.open(req, timeout=max(0.001, deadline - time.monotonic()))
            except urllib.error.HTTPError as err:  # any status outside 2xx, 3xx included
                resp = err
            sock = _sock_of(resp)
            text = _read_body(resp, sock, deadline)
            status, hdrs = resp.status if hasattr(resp, "status") else resp.code, {k.lower(): v for k, v in resp.headers.items()}
            resp.close()
        except (socket.timeout, TimeoutError_builtin) as exc:
            raise TimeoutError("the request timed out after %ss" % timeout) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError_builtin)):
                raise TimeoutError("the request timed out after %ss" % timeout) from exc
            raise ConnectionError("connection error: %s" % exc.reason) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError("connection error: %s" % exc) from exc
        return _Response(status, hdrs, text)

    def _request(self, method, path, body=None):
        parsed, headers, _ = self._exchange(method, path, body)
        return parsed, headers

    def _exchange(self, method, path, body=None, policy="default", content_type=None, raw=False, timeout=None, extra_headers=None):
        """The parsed JSON body, the headers and the status of a successful response.

        policy "default" retries as every call does; "mutation" (batch create, add and
        finalize) retries only a 429 or a 529, never a timeout, a lost answer or a 5xx;
        "session_open" never retries a 429 session_limit_exceeded; "ocr" (a decision whose
        boosters name ocr) never retries a timeout, since the server
        finishes that attempt and may bill it; "idempotent" (a call that carries its own
        Idempotency-Key, the same on every attempt) retries as "default" and also a 409
        idempotency_in_progress, after its Retry-After."""
        if not self.api_key:
            raise AuthenticationError("no API key: pass api_key or set DECISIONNODE_API_KEY")
        attempt = 0
        while True:
            try:
                resp = self._send_once(method, path, body, content_type, timeout, extra_headers)
            except (TimeoutError, ConnectionError) as exc:
                if attempt >= self.max_retries or policy == "mutation" or (policy == "ocr" and isinstance(exc, TimeoutError)):
                    raise
                self._sleep(backoff(attempt))
                attempt += 1
                continue
            if 200 <= resp.status <= 299:
                if raw:
                    return resp.text, resp.headers, resp.status
                try:
                    return json.loads(resp.text), resp.headers, resp.status
                except ValueError as exc:
                    raise ServerError("the response is not JSON", status=resp.status, request_id=resp.headers.get("x-request-id")) from exc
            retryable = resp.status in RETRY_STATUSES
            if policy == "mutation":
                retryable = resp.status in (429, 529)
            if policy == "session_open" and resp.status == 429 and _error_type(resp) == "session_limit_exceeded":
                retryable = False
            if policy == "idempotent" and resp.status == 409 and _error_type(resp) == "idempotency_in_progress":
                retryable = True
            if resp.status == 529 and _detail_reason(resp) == "enrollment_unpriced":
                # Suite 1.15: no price for an enrollment, which a retry never cures.
                retryable = False
            if resp.status == 529 and _detail_reason(resp) == "ingestion_delayed":
                # Spec 1.4: no document ready while ingestion is delayed by an outage: a
                # wait of a minute or more, so the caller decides.
                retryable = False
            if resp.status == 429 and _error_type(resp) == "key_spend_cap_reached":
                # Suite 1.20: the key's daily spend cap, reset at 00:00 UTC: a stop, not a wait.
                retryable = False
            if retryable and attempt < self.max_retries:
                wait = retry_wait(resp.headers)
                self._sleep(wait if wait is not None else backoff(attempt))
                attempt += 1
                continue
            raise make_error(resp)

    # ---- the surface

    def decide(self, state=_UNSET, questions=None, images=None, model=None, videos=None, boosters=None, fallback=None):
        return Decision(*self._decision_call("/v1/decide", state, questions, images, model, videos=videos, boosters=boosters, fallback=fallback)[:2])

    def _helper(self, state, qid, question, images, model, expected, videos=None, boosters=None, fallback=None):
        """check, classify and score promise a typed answer: a missing or malformed one
        is a ServerError carrying the server's reason, never an UnknownAnswer."""
        body, headers, status = self._decision_call("/v1/decide", state, {qid: question}, images, model, videos=videos, boosters=boosters, fallback=fallback)
        decision = Decision(body, headers)
        answer = decision.answers.get(qid)
        if not isinstance(answer, expected):
            raise ServerError(
                "the response holds no well-formed %s answer for %r" % (expected.kind, qid),
                status=status,
                request_id=headers.get("x-request-id"),
                reason=headers.get("x-decisionnode-error"),
                body=body,
                headers=headers,
            )
        return answer

    def _body(self, state, questions, images, model, wires=None, videos=None, boosters=None, fallback=None):
        if not questions:
            raise ValueError("questions must hold at least one question")
        body = {"model": model or self.model, "state": "" if state is _UNSET or state is None else state, "questions": dict(questions)}
        if images:
            body["images"] = wires if wires is not None else [img.wire() if isinstance(img, Image) else img for img in images]
        if videos:
            body["videos"] = video_wires(videos)
        if boosters:
            # Spec 1.3: in the caller's order; none or an empty list leaves the key out.
            body["boosters"] = booster_wires(boosters)
        if fallback is not None:
            # Spec 1.3: sent only when given; left out, the server's default (true) applies.
            body["fallback"] = fallback
        return body

    def decide_many(self, requests, concurrency=8):
        """One DecisionResult per request, in input order: a Decision or the error it ended with."""
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("concurrency must be an integer of at least 1, got %r" % (concurrency,))

        # Every request is checked before any is sent: one invalid request is an argument
        # error for the whole call and nothing goes out.
        bodies = []
        for i, req in enumerate(requests):
            if not isinstance(req, dict):
                raise ValueError("request %d must be a mapping of decide() arguments" % i)
            try:
                body = self._body(req.get("state", _UNSET), req.get("questions"), None, req.get("model"), boosters=req.get("boosters"))
            except ValueError as exc:
                raise ValueError("request %d: %s" % (i, exc)) from exc
            self._check_local(body, req.get("images") or [], videos=req.get("videos"))
            _check_size(self, req.get("state", _UNSET), req.get("questions"), req.get("images"), req.get("model"), req.get("videos"), req.get("boosters"))
            bodies.append(req)

        def run(req):
            try:
                return Decision(*self._decision_call("/v1/decide", req.get("state", _UNSET), req.get("questions"), req.get("images"), req.get("model"), videos=req.get("videos"), boosters=req.get("boosters"), fallback=req.get("fallback"))[:2])
            except DecisionNodeError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(run, bodies))

    def check(self, state=_UNSET, instructions=None, if_true=None, if_false=None, images=None, model=None, videos=None, boosters=None, fallback=None):
        return self._helper(state, "check", truth(instructions, if_true, if_false), images, model, TruthAnswer, videos, boosters, fallback)

    def classify(self, state=_UNSET, instructions=None, options=None, images=None, model=None, videos=None, boosters=None, fallback=None):
        return self._helper(state, "classify", choice(instructions, options), images, model, ChoiceAnswer, videos, boosters, fallback)

    def score(self, state=_UNSET, instructions=None, levels=None, images=None, model=None, videos=None, boosters=None, fallback=None):
        return self._helper(state, "score", score(instructions, levels), images, model, ScoreAnswer, videos, boosters, fallback)

    def gate(self, action, context=None, images=None, model=None, videos=None, boosters=None, fallback=None):
        state = {"action": action}
        if context is not None:
            state["context"] = context
        return self.decide(state, gate_questions(), images, model, videos=videos, boosters=boosters, fallback=fallback)

    def models(self):
        body, _ = self._request("GET", "/v1/models")
        entries = [m for m in body.get("models", []) if isinstance(m, dict)]
        # Held for the local checks: a limit only /v1/models states is checked once known.
        self._held = {m.get("name"): m for m in entries}
        return [ModelInfo(m) for m in entries]

    def _limits(self, model=None):
        """The /v1/models entry of the model a request names, when this client holds it:
        the entry of that name, else the entry that states the name as its alias or its
        pinned version (version and alias on every entry). A name
        the list does not state holds nothing, so its checks are left to the server."""
        name = model or self.model
        entry = self._held.get(name)
        if entry is None:
            for m in self._held.values():
                if name in (m.get("alias"), m.get("version")):
                    entry = m
                    break
        return dict(entry, model=name) if entry else {}

    def check_request(self, method, **args):
        """Suite 1.6: every local check the call would run before sending, run alone. It
        raises exactly what the call would raise for the request and sends nothing; it
        returns None when the request would be sent. Frames: Session.check_frame."""
        if method == "decide":
            # Spec 1.3: boosters are the server's to judge; they count only toward the body's nesting.
            _check_decision(self, "/v1/decide", args.get("state", _UNSET), args.get("questions"), args.get("images"), args.get("model"), args.get("videos"), args.get("boosters"))
        elif method == "open_session":
            body = _open_body(self, **args)
            check_session_open(body, self._limits(body["model"]))
        elif method in ("batches.create", "batches.add"):
            _check_batch(self, list(args.get("requests") or []), allow_empty=method == "batches.create")
        else:
            raise ValueError("check_request takes decide, open_session, batches.create or batches.add, got %r" % (method,))
        return None

    def _check_local(self, body, images, profile="decide", videos=None):
        """Every fixed rule, before any request: the body (its images and videos as
        placeholders, so the nesting counts them), the images, then (spec 1.2) the videos."""
        images = list(images or [])
        videos = list(videos or [])
        probe = dict(body)
        image_ids = [img.id if isinstance(img, Image) else (img.get("id") if isinstance(img, dict) else None) for img in images]
        if images:
            probe["images"] = [{"id": iid} for iid in image_ids]
        if videos:
            probe["videos"] = [{"id": ""} for _ in videos]
        else:
            probe.pop("videos", None)
        limits = self._limits(body.get("model"))
        check_request(probe, profile, limits, image_ids=image_ids, tail_entries=count_entries(images, videos, limits))
        check_images(images, limits)
        check_videos(videos, images)

    def decide_raw(self, body):
        parsed, _ = self._request("POST", "/v1/decide", body)
        return parsed


def _encode(body):
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _error_type(resp) -> str | None:
    try:
        detail = json.loads(resp.text).get("detail")
    except (ValueError, AttributeError):
        return None
    return detail.get("error_type") if isinstance(detail, dict) else None


def _detail_reason(resp) -> str | None:
    """detail.details.reason of an error response, when it has one."""
    try:
        detail = json.loads(resp.text).get("detail")
    except (ValueError, AttributeError):
        return None
    details = detail.get("details") if isinstance(detail, dict) else None
    return details.get("reason") if isinstance(details, dict) else None


TimeoutError_builtin = __builtins__["TimeoutError"] if isinstance(__builtins__, dict) else __builtins__.TimeoutError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never follow: the 3xx comes back as an HTTPError and becomes an error


def _without_redirects(opener):
    """A copy of the caller's opener with every redirect handler replaced, so a key is
    never sent to another URL whatever the caller configured."""
    if not isinstance(opener, urllib.request.OpenerDirector):
        raise ValueError("http_client must be a urllib.request.OpenerDirector")
    copy = urllib.request.OpenerDirector()
    copy.addheaders = list(opener.addheaders)
    for handler in opener.handlers:
        if isinstance(handler, urllib.request.HTTPSHandler):
            # The caller's transport may not turn off certificate or hostname checks.
            ctx = getattr(handler, "_context", None)
            if ctx is not None and (ctx.verify_mode == ssl.CERT_NONE or not ctx.check_hostname):
                raise ValueError("http_client must keep TLS certificate and hostname checks on")
        if not isinstance(handler, urllib.request.HTTPRedirectHandler):
            copy.add_handler(handler)
    copy.add_handler(_NoRedirect())
    return copy


def _sock_of(resp):
    fp = getattr(resp, "fp", None)
    for _ in range(4):  # HTTPError -> HTTPResponse -> BufferedReader -> SocketIO
        if fp is None:
            return None
        sock = getattr(fp, "_sock", None)
        if sock is not None:
            return sock
        fp = getattr(fp, "fp", None) or getattr(fp, "raw", None)
    return None


def _set_remaining(sock, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("the attempt's deadline passed")
    if sock is not None:
        try:
            sock.settimeout(remaining)
        except OSError:
            # urllib detaches the socket it hands back; its timeout stays the one given
            # to open(), and the deadline is still checked before every read.
            pass


def _read_body(resp, sock, deadline) -> str:
    """Read the whole body before the deadline; a body that trickles in past it times out."""
    chunks = []
    while True:
        _set_remaining(sock, deadline)
        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def gate_questions() -> dict:
    return {
        "risk": score(
            "How much harm could this action cause if it is wrong?",
            ["none", "low", "moderate", "high", "severe"],
        ),
        "touches_production": truth(
            "Does this action change a production system, production data or anything customers use?"
        ),
        "verdict": choice(
            "Should this action run as it is, wait for a person to confirm it, or be blocked?",
            {
                "allow": "safe to run without asking",
                "confirm": "a person should confirm it first",
                "block": "should not run",
            },
        ),
    }


def backoff(attempt: int) -> float:
    base = min(8.0, 0.5 * (2**attempt))
    return base * (0.75 + random.random() * 0.25)


def retry_wait(headers: dict):
    """The wait the server asked for, in seconds, capped at 60 s; None when it asked for none.

    retry-after-ms first, then Retry-After as seconds or as an HTTP date.
    """
    ms = headers.get("retry-after-ms")
    if ms is not None:
        try:
            return min(MAX_SERVER_WAIT, max(0.0, float(ms) / 1000.0))
        except ValueError:
            pass
    s = headers.get("retry-after")
    if s is not None:
        try:
            return min(MAX_SERVER_WAIT, max(0.0, float(s)))
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            return min(MAX_SERVER_WAIT, max(0.0, when.timestamp() - time.time()))
    return None


# Spec 1.3: the status text of an error whose body is not JSON, the same in every
# language and never the HTTP library's own wording.
STATUS_TEXT = {
    400: "Bad Request", 401: "Unauthorized", 402: "Payment Required", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout", 409: "Conflict",
    410: "Gone", 413: "Content Too Large", 422: "Unprocessable Content", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable",
    504: "Gateway Timeout", 529: "Overloaded",
}


def edge_message(status, request_id) -> str:
    """The client's own description of an error whose body is not JSON (an edge's or a
    proxy's page, or no body): "HTTP <status> <text>", then the request id when sent."""
    text = "HTTP %d" % status
    if status in STATUS_TEXT:
        text += " " + STATUS_TEXT[status]
    if request_id is not None:
        text += " (request id %s)" % request_id
    return text


def make_error(resp: _Response) -> DecisionNodeError:
    status = resp.status
    headers = resp.headers
    try:
        body = json.loads(resp.text)
        is_json = True
    except ValueError:
        body = None
        is_json = False
    detail = body.get("detail") if isinstance(body, dict) else None
    error_type = message = None
    if not is_json:
        # The body is never read into any field; the class follows the status alone.
        message = edge_message(status, headers.get("x-request-id"))
    entries = None
    category = None
    details = None
    if isinstance(detail, dict):
        error_type = detail.get("error_type")
        message = detail.get("message")
        category = detail.get("category")
        details = detail.get("details") if isinstance(detail.get("details"), dict) else None
    elif isinstance(detail, str):
        message = detail
    elif isinstance(detail, list):
        entries = detail
    kw = dict(
        message=message,
        status=status,
        error_type=error_type,
        request_id=headers.get("x-request-id"),
        reason=headers.get("x-decisionnode-error"),
        body=body,
        headers=headers,
        boosters=parse_boosters(headers.get("x-decisionnode-boosters")),
        details=details,
    )
    if status == 400:
        if error_type == "max_tokens_exceeded":
            return ContextLimitError(**kw)
        return BadRequestError(**kw)
    if status == 401:
        return AuthenticationError(**kw)
    if status == 402:
        return InsufficientCreditError(**kw)
    if status == 403:
        if error_type == "refusal_error":
            return RefusalError(category=category, safety=parse_safety(headers.get("x-decisionnode-safety")), **kw)
        return PermissionDeniedError(**kw)
    if status == 404:
        return NotFoundError(**kw)
    if status == 413:
        return RequestTooLargeError(**kw)
    if status == 422:
        return ValidationError(entries=entries, **kw)
    if status == 429 and error_type == "session_limit_exceeded":
        return SessionLimitError(retry_after=retry_wait(headers), **kw)
    if status == 429:
        return RateLimitError(retry_after=retry_wait(headers), **kw)
    if status == 529:
        return OverloadedError(retry_after=retry_wait(headers), **kw)
    if 500 <= status <= 599:
        return ServerError(**kw)
    # Every other status, redirects included (they are never followed): the base class.
    return DecisionNodeError(**kw)


# =========================================================================== spec 1.1
#
# Local checks, the inline threshold and uploads, number, truth read from noul,
# sessions and the Batch API.

MAX_IMAGES = 16  # spec 1.2 (4 in spec 1.1)
MAX_VIDEOS = 4
MAX_UPLOAD_FILES = 16
MAX_VIDEO_FPS = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_URL_CHARS = 2048
MAX_IMAGE_ID_CHARS = 64
MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10
MAX_GRID_VALUES = 4096
MAX_BODY_BYTES = 26214400  # spec 1.3: the API's request body limit, 25 MiB (64 MB before)
BODY_TOO_LARGE = "The request body is over 26,214,400 bytes (25 MiB). Send large images as uploads (POST /v1/uploads) and name them by upload_id."
UPLOAD_ID_PLACEHOLDER = "upl_" + "0" * 32
STALE_UPLOAD = {"upload_expired", "upload_consumed", "upload_not_found", "upload_incomplete"}
CACHE_ENTRIES = 256
CACHE_MARGIN_S = 300
REOPEN_CODES = {1001, 1006, 1011, 4404, 4408, 4409, 4410}
BATCH_ADD_BYTES = 10 * 1024 * 1024
MIN_POLL_INTERVAL = 10.0


def number(instructions=None, min=None, max=None, step=None) -> dict:  # noqa: A002 - neutral names
    q = {"type": "number"}
    if instructions is not None:
        q["instructions"] = instructions
    for name, value in (("min", min), ("max", max), ("step", step)):
        if value is not None:
            q[name] = value
    return q


def point(instructions=None, image=None, units=None, max=None) -> dict:  # noqa: A002
    """Spec 1.2: one spot in an image; image and units left out when not given. With max
   , up to max spots: the answer is a PointsAnswer."""
    return _place("point", instructions, image, units, max)


def box(instructions=None, image=None, units=None, max=None) -> dict:  # noqa: A002
    """Spec 1.2: a rectangle in an image; image and units left out when not given. With
    max, up to max rectangles: the answer is a BoxesAnswer."""
    return _place("box", instructions, image, units, max)


def _place(qtype, instructions, image, units, most=None) -> dict:
    q = {"type": qtype}
    if instructions is not None:
        q["instructions"] = instructions
    if image is not None:
        q["image"] = image
    if units is not None:
        q["units"] = units
    if most is not None:
        q["max"] = most
    return q


def local_error(cls, message, error_type="api_usage_error", reason=None):
    """The API's own error class, error_type and message, raised before any request:
    no status, no request id; reason holds what x-decisionnode-error would say."""
    return cls(message, status=None, error_type=error_type, reason=reason)


def image_dimensions(data: bytes, media_type: str):
    try:
        if media_type == "image/png":
            if data[12:16] != b"IHDR":
                return None
            return struct.unpack(">II", data[16:24])
        if media_type == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2 : i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return w, h
                i += 2 + length
            return None
        if media_type == "image/webp":
            kind = data[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    except (struct.error, IndexError):
        return None
    return None


MAX_NESTING = 128
MAX_WINDOW = 64
MAX_TTL_SECONDS = 3600
MAX_SEQ = 2**53 - 1
TWO_53 = 2**53
MAX_NUMBER_DECIMALS = 6
LIST_LIMIT_MAX = 100
DECIDE_TYPES = ("truth", "noul", "choice", "score", "number", "point", "box")
PLACE_TYPES = ("point", "box")
UNITS = ("pixels", "normalized")
PLACES_MIN, PLACES_MAX = 2, 16  # a point or box question's max
MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")


def local_422(entries, reason=None):
    """The API's 422, raised before any request: the entries the server would list."""
    return ValidationError("", status=None, error_type=None, entries=entries, reason=reason)


def _entry(kind, loc, msg, value, ctx=None):
    e = {"type": kind, "loc": list(loc), "msg": msg, "input": value}
    if ctx is not None:
        e["ctx"] = ctx
    return e


def _is_sdl(value) -> bool:
    return isinstance(value, (str, dict, list))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sdl_entries(loc, value) -> list:
    """A value that may be a string, an object or an array: three entries."""
    return [
        _entry("string_type", loc + ["str"], "Input should be a valid string", value),
        _entry("dict_type", loc + ["dict[any,any]"], "Input should be a valid dictionary", value),
        _entry("list_type", loc + ["list[any]"], "Input should be a valid list", value),
    ]


def nesting_depth(value) -> int:
    """How many objects and arrays sit inside each other, the outermost included."""
    deepest = 0
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, (list, tuple)):
            children = node
        else:
            continue
        deepest = max(deepest, depth)
        stack.extend((c, depth + 1) for c in children if isinstance(c, (dict, list, tuple)))
    return deepest


def check_nesting(body) -> None:
    depth = nesting_depth(body)
    if depth > MAX_NESTING:
        raise local_error(BadRequestError, "Invalid request.", reason="the request nests %d objects and arrays inside each other; the limit is %d" % (depth, MAX_NESTING))


def _dec_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _places(value: Decimal) -> int:
    exp = value.normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def _json_number(value: Decimal):
    text = _dec_text(value)
    return float(text) if "." in text else int(text)


def _check_number(qid, q, loc, entries, deferred, max_values):
    """The 422 entries in the server's order, then the 400."""
    raw = {name: q.get(name) for name in ("min", "max", "step")}
    defaults = {"min": Decimal(0), "max": Decimal(255), "step": Decimal(1)}
    values = {}
    ok = True
    for name in ("min", "max", "step"):
        sent = raw[name]
        floc = loc + [name]
        if sent is None:
            values[name] = defaults[name]
            continue
        if isinstance(sent, bool) or not isinstance(sent, (int, float)):
            entries.append(_entry("float_type", floc, "Input should be a valid number", sent))
            ok = False
            continue
        if isinstance(sent, float) and not math.isfinite(sent):
            entries.append(_entry("finite_number", floc, "Input should be a finite number", sent))
            ok = False
            continue
        dec = Decimal(repr(sent)) if isinstance(sent, float) else Decimal(sent)
        if name in ("min", "max") and dec < -TWO_53:
            entries.append(_entry("greater_than_equal", floc, "Input should be greater than or equal to -%d" % TWO_53, sent, {"ge": -TWO_53}))
            ok = False
            continue
        if name in ("min", "max") and dec > TWO_53:
            entries.append(_entry("less_than_equal", floc, "Input should be less than or equal to %d" % TWO_53, sent, {"le": TWO_53}))
            ok = False
            continue
        if name == "step" and dec <= 0:
            entries.append(_entry("greater_than", floc, "Input should be greater than 0", sent, {"gt": 0}))
            ok = False
            continue
        if name in ("min", "step") and _places(dec) > MAX_NUMBER_DECIMALS:
            entries.append(_entry("decimal_max_places", floc, "Decimal input should have no more than %d decimal places" % MAX_NUMBER_DECIMALS, sent, {"decimal_places": MAX_NUMBER_DECIMALS}))
            ok = False
            continue
        values[name] = dec
    if not ok:
        return
    lo, hi, step = values["min"], values["max"], values["step"]
    d = max(_places(lo), _places(step))
    if d > 0:
        bound = Decimal(10) ** (15 - d) - Decimal(10) ** (-d)
        text = _dec_text(bound)
        for name, val in (("min", lo), ("max", hi)):
            if val > bound:
                entries.append(_entry("less_than_equal", loc + [name], "Input should be less than or equal to %s" % text, raw[name], {"le": _json_number(bound)}))
                return
            if val < -bound:
                entries.append(_entry("greater_than_equal", loc + [name], "Input should be greater than or equal to -%s" % text, raw[name], {"ge": _json_number(-bound)}))
                return
    if lo > hi:
        max_sent = raw["max"] if raw["max"] is not None else 255
        min_sent = raw["min"] if raw["min"] is not None else 0
        entries.append(_entry("less_than_equal", loc + ["min"], "Input should be less than or equal to %s" % json.dumps(max_sent), min_sent, {"le": max_sent}))
        return
    scale = Decimal(10) ** d
    n = (int((hi * scale).to_integral_value(rounding="ROUND_FLOOR")) - int(lo * scale)) // int(step * scale) + 1
    if n > max_values:
        entries.append(_entry("too_long", loc, "Grid should have at most %d values, not %d" % (max_values, n), q, {"field_type": "Grid", "max_length": max_values, "actual_length": n}))
        return
    if q.get("instructions") is None or q.get("instructions") == "":
        deferred.append(local_error(BadRequestError, "Number question must have instructions: %s" % qid, None))


def _check_question(qid, q, entries, deferred, profile, limits):
    base = ["body", "questions", qid]
    if not isinstance(q, dict):
        entries.append(_entry("model_type", base, "Input should be a valid dictionary or object", q))
        return
    if q.get("type") is None:
        entries.append(_entry("missing", base + ["type"], "Field required", q))
        return
    qtype = q["type"]
    if qtype not in DECIDE_TYPES:
        # An unknown type is the 400 "Invalid request.", its fields unread.
        deferred.append(local_error(BadRequestError, "Invalid request.", reason="question '%s': unknown type %s" % (qid, json.dumps(qtype))))
        return
    loc = base + [qtype]
    instructions = q.get("instructions")
    if instructions is not None and not _is_sdl(instructions):
        entries.extend(_sdl_entries(loc + ["instructions"], instructions))
    criteria = q.get("criteria")
    if qtype == "number":
        if limits.get("number_max_grid_values") == 0:
            deferred.append(local_error(BadRequestError, "Model %s does not answer number questions." % limits.get("model")))
            return
        _check_number(qid, q, loc, entries, deferred, limits.get("number_max_grid_values") or MAX_GRID_VALUES)
    elif qtype == "choice":
        most = limits.get("max_choice_options") or MAX_CHOICE_OPTIONS
        if criteria is None:
            entries.append(_entry("missing", loc + ["criteria"], "Field required", q))
        elif not isinstance(criteria, dict):
            entries.append(_entry("dict_type", loc + ["criteria"], "Input should be a valid dictionary", criteria))
        else:
            for name, desc in criteria.items():
                if desc is not None and not _is_sdl(desc):
                    entries.extend(_sdl_entries(loc + ["criteria", name], desc))
            if len(criteria) == 0:
                deferred.append(local_error(BadRequestError, "Choice question must have at least one choice: %s" % qid, None))
            elif len(criteria) > most:
                deferred.append(local_error(BadRequestError, "Too many choices. Must have at most %d choices." % most, None))
    elif qtype == "score":
        most = limits.get("max_score_levels") or MAX_SCORE_LEVELS
        if criteria is None:
            entries.append(_entry("missing", loc + ["criteria"], "Field required", q))
            return
        levels = None
        if isinstance(criteria, list):
            levels = criteria
        elif isinstance(criteria, dict) and sorted(criteria, key=lambda k: (len(k), k)) == [str(i) for i in range(len(criteria))]:
            levels = [criteria[str(i)] for i in range(len(criteria))]
        else:
            entries.append(_entry("list_type", loc + ["criteria"], "Input should be a valid list", criteria))
            return
        if len(levels) == 0:
            entries.append(_entry("too_short", loc + ["criteria"], "List should have at least 1 item after validation, not 0", criteria))
            return
        for i, level in enumerate(levels):
            if not _is_sdl(level):
                entries.extend(_sdl_entries(loc + ["criteria", i], level))
        if len(levels) > most:
            deferred.append(local_error(BadRequestError, "Too many score levels. Must have at most %d levels." % most, None))
    elif qtype in PLACE_TYPES:
        return  # spec 1.2: checked by _check_place, with the request's image ids
    else:  # truth and its alias noul
        if criteria is not None and not isinstance(criteria, dict):
            entries.append(_entry("dict_type", loc + ["criteria"], "Input should be a valid dictionary", criteria))
            return
        crit = criteria or {}
        for side in ("true", "false"):
            if crit.get(side) is not None and not _is_sdl(crit[side]):
                entries.extend(_sdl_entries(loc + ["criteria", side], crit[side]))
        if instructions is None and crit.get("true") is None and crit.get("false") is None:
            name = "Noul" if qtype == "noul" else "Truth"
            deferred.append(local_error(BadRequestError, "%s question must have criteria or instructions: %s" % (name, qid), None))


def _check_place(qid, q, entries, deferred, image_ids) -> None:
    """Spec 1.2: a point or box question. Its 422 entries (the image, then the units),
    then the missing instructions as the plain 400."""
    qtype = q["type"]
    loc = ["body", "questions", qid, qtype]
    if image_ids is not None:
        image = q.get("image")
        if not image_ids:
            entries.append(_entry("image_required", loc, "A %s question needs an image in the request" % qtype, q))
        elif image is None:
            if len(image_ids) >= 2:
                entries.append(_entry("missing", loc + ["image"], "Field required", q))
        elif not isinstance(image, str):
            entries.append(_entry("string_type", loc + ["image"], "Input should be a valid string", image))
        elif image not in image_ids:
            entries.append(_entry("unknown_image", loc + ["image"], "Input should be the id of an image of the request", image))
    units = q.get("units")
    if units is not None and units not in UNITS:
        entries.append(_entry("literal_error", loc + ["units"], "Input should be 'pixels' or 'normalized'", units, {"expected": "'pixels' or 'normalized'"}))
    most = q.get("max")
    if most is not None:
        # Several places, max an integer from 2 to 16.
        if not _int(most):
            entries.append(_entry("int_type", loc + ["max"], "Input should be a valid integer", most))
        elif most < PLACES_MIN:
            entries.append(_entry("greater_than_equal", loc + ["max"], "Input should be greater than or equal to %d" % PLACES_MIN, most, {"ge": PLACES_MIN}))
        elif most > PLACES_MAX:
            entries.append(_entry("less_than_equal", loc + ["max"], "Input should be less than or equal to %d" % PLACES_MAX, most, {"le": PLACES_MAX}))
    if q.get("instructions") is None:
        deferred.append(local_error(BadRequestError, "%s question must have instructions: %s" % (qtype.capitalize(), qid), None))


def check_request(body, profile="decide", limits=None, extra_entries=(), image_ids=None, tail_entries=()) -> None:
    """Every fixed rule of the request is checked before
    any request, in the server's order (nesting, the 422 entries, then the first 400),
    and raised with the class, error_type, message and entries the API would send.
    image_ids: the request's image ids, for the point and box rules (spec 1.2); None
    where a request carries no images of its own (a session's open)."""
    check_nesting(body)
    limits = limits or {}
    entries = list(extra_entries)
    deferred = []
    state = body.get("state")
    if state is not None and not _is_sdl(state):
        entries.extend(_sdl_entries(["body", "state"], state))
    for qid, q in (body.get("questions") or {}).items():
        _check_question(qid, q, entries, deferred, profile, limits)
        if isinstance(q, dict) and q.get("type") in PLACE_TYPES:
            _check_place(qid, q, entries, deferred, image_ids)
    entries.extend(tail_entries)
    if entries:
        raise local_422(entries)
    if deferred:
        raise deferred[0]


def check_questions(questions, profile="decide", limits=None) -> None:
    check_request({"questions": questions}, profile, limits)


# ---------------------------------------------------------------- images, checked locally

BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
        "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
        "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "2001:db8::/32", "fd00:ec2::254/128",
    )
]
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _blocked_address(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # a DNS name: resolved and judged by the server
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr in NAT64:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return any(addr.version == net.version and addr in net for net in BLOCKED_NETWORKS)


def check_image_url(iid, url) -> None:
    """The image URL rules that need no network: https (else the API's
    api_usage_error), then image_url_refused for the length, user information, a port
    other than 443, no host or a literal address in a blocked range."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise local_error(BadRequestError, "Image %s: only https URLs are accepted." % iid)
    reason = None
    parts = urlsplit(url)
    if len(url) > MAX_URL_CHARS:
        reason = "the URL is %d characters, over the limit of %d" % (len(url), MAX_URL_CHARS)
    elif "@" in parts.netloc:
        reason = "the URL carries user information"
    else:
        try:
            port = parts.port
        except ValueError:
            port = -1
        if port not in (None, 443):
            reason = "the URL uses a port other than 443"
        elif not parts.hostname:
            reason = "the URL has no host"
        elif _blocked_address(parts.hostname):
            reason = "the URL resolves to a private address"
    if reason is not None:
        raise local_error(BadRequestError, "Image '%s': %s." % (iid, reason), "image_url_refused")


def max_images(limits) -> int:
    """The image limit: the held /v1/models entry's max_images, else its images.max_images."""
    limits = limits or {}
    nested = limits.get("images") if isinstance(limits.get("images"), dict) else {}
    return limits.get("max_images") or nested.get("max_images") or MAX_IMAGES


def image_input(img):
    """An image as a 422 entry's input lists it: its wire form, a local image's bytes left out."""
    if not isinstance(img, Image):
        return img
    if img.url is not None:
        return {"id": img.id, "url": img.url}
    out = {"id": img.id}
    if img.upload_id is not None:
        out["upload_id"] = img.upload_id
    if img.media_type is not None:
        out["media_type"] = img.media_type
    return out


def count_entries(images, videos, limits=None) -> list:
    """Suite 1.9: more images or videos than a request takes is the API's 422 too_long on
    ["body", "images"] or ["body", "videos"], listed after the questions' entries."""
    out = []
    for name, items, most in (("images", list(images or []), max_images(limits)), ("videos", list(videos or []), MAX_VIDEOS)):
        if len(items) > most:
            given = [image_input(i) for i in items] if name == "images" else video_wires(items)
            msg = "List should have at most %d items after validation, not %d" % (most, len(items))
            out.append(_entry("too_long", ["body", name], msg, given, {"field_type": "List", "max_length": most, "actual_length": len(items)}))
    return out


def check_images(images, limits=None, id_length=True) -> None:
    """Every image is checked before any request.
    id_length: the 64-character id rule, for every body that carries the ids; a
    session frame's image goes without its id, so frames skip it."""
    limits = limits or {}
    images = list(images or [])
    biggest = limits.get("max_image_bytes") or MAX_IMAGE_BYTES
    seen = set()
    for i, img in enumerate(images):
        iid = img.id if isinstance(img, Image) else (img.get("id") if isinstance(img, dict) else None)
        if not isinstance(iid, str) or iid == "":
            raise local_error(BadRequestError, "Image %d needs an id." % i)
        if id_length and len(iid) > MAX_IMAGE_ID_CHARS:
            # At most 64 characters, counted as code points.
            raise local_error(BadRequestError, "Image %d: its id is longer than %d characters." % (i, MAX_IMAGE_ID_CHARS))
        if iid in seen:
            raise local_error(BadRequestError, "Duplicate image id: %s" % iid)
        seen.add(iid)
        if not isinstance(img, Image):
            continue
        if img.url is not None:
            check_image_url(img.id, img.url)
            continue
        if img.upload_id is not None:
            if img.media_type is not None and img.media_type not in MEDIA_TYPES:
                raise local_error(BadRequestError, "Image %s: media_type must be image/jpeg, image/png or image/webp." % img.id)
            continue
        if img.data is None:
            continue
        found = sniff_media_type(img.data)
        if found is None:
            raise local_error(BadRequestError, "Image %s is not a JPEG, PNG or WebP image." % img.id)
        if img.declared and img.declared != found:
            raise local_error(BadRequestError, "Image %s could not be decoded as %s." % (img.id, img.declared))
        if len(img.data) > biggest:
            raise local_error(BadRequestError, "Image %s is larger than 10 MB." % img.id)
        dims = image_dimensions(img.data, found)
        if dims is None:
            raise local_error(BadRequestError, "Image %s: its header cannot be read." % img.id)
        if dims[0] * dims[1] > MAX_IMAGE_PIXELS:
            raise local_error(BadRequestError, "Image %s is %dx%d pixels, over 40 megapixels." % (img.id, dims[0], dims[1]))


def _num_text(value) -> str:
    """A number as the server writes it in a message: printf's %g (six significant
    digits, no trailing zeros: 1.5, 4, 0.333333)."""
    return "%g" % value


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def check_videos(videos, images=()) -> None:
    """Spec 1.2: the video checks that need no decoding, in the server's order and with its
    messages. Frame counts, a frame's bytes, pixels and
    size, the window and the token budget are the server's."""
    videos = list(videos or [])
    if not videos:
        return
    seen = set()
    for img in images or []:
        iid = img.id if isinstance(img, Image) else (img.get("id") if isinstance(img, dict) else None)
        if isinstance(iid, str):
            seen.add(iid)
    for i, video in enumerate(videos):
        wire = video.wire() if isinstance(video, Video) else video
        if not isinstance(wire, dict):
            raise ValueError("video %d must be a Video or a mapping" % i)
        vid = wire.get("id")
        if not isinstance(vid, str) or vid == "":
            raise local_error(BadRequestError, "Video %d needs an id." % i)
        if vid in seen:
            raise local_error(BadRequestError, "Video id %r is used twice (ids are unique across images and videos)." % vid)
        seen.add(vid)

        def refuse(problem):
            return local_error(BadRequestError, "Video %r: %s." % (vid, problem))

        frames = wire.get("frames")
        if not isinstance(frames, list) or not frames:
            raise refuse("frames must be a non-empty list of frames")
        before = None
        for n, frame in enumerate(frames):
            if not isinstance(frame, dict):
                raise refuse("frame %d must be an object with t, media_type and data" % n)
            t = frame.get("t")
            if not _finite_number(t) or t < 0:
                raise refuse("frame %d: t must be a number of seconds, at least 0" % n)
            if before is not None and t <= before:
                raise refuse("frame %d: t must be larger than the frame before it (%s)" % (n, _num_text(before)))
            before = t
            if not isinstance(frame.get("data"), str):
                raise refuse("frame %d: data must be base64 text" % n)
            if frame.get("media_type") not in MEDIA_TYPES:
                raise refuse("frame %d: media_type must be one of %s" % (n, ", ".join(MEDIA_TYPES)))
        fps = wire.get("fps")
        if fps is not None and (not _finite_number(fps) or not 0 < fps <= MAX_VIDEO_FPS):
            raise refuse("fps must be a number above 0 and at most %d" % MAX_VIDEO_FPS)
        for name in ("start", "end"):
            value = wire.get(name)
            if value is not None and (not _finite_number(value) or value < 0):
                raise refuse("%s must be a number of seconds, at least 0" % name)
        if wire.get("end") is not None and wire["end"] <= (wire.get("start") or 0):
            raise refuse("end must be after start")


def check_session_open(body, limits=None) -> None:
    """window 1 to 64 and ttl_seconds 1 to 3,600 (a value the client
    holds from /v1/models replaces the bound), instructions and state of the three types,
    every question rule; a version that serves no sessions when the client knows it."""
    check_nesting(body)
    limits = limits or {}
    sessions = limits.get("sessions") or {}
    entries = []
    for name in ("instructions", "state"):
        v = body.get(name)
        if v is not None and not _is_sdl(v):
            entries.extend(_sdl_entries(["body", name], v))
    bounds = {"window": sessions.get("max_window") or MAX_WINDOW, "ttl_seconds": sessions.get("max_ttl_seconds") or MAX_TTL_SECONDS}
    for name, hi in bounds.items():
        v = body.get(name)
        if v is None:
            continue
        if not _is_int(v):
            entries.append(_entry("int_type", ["body", name], "Input should be a valid integer", v))
        elif v < 1:
            entries.append(_entry("greater_than_equal", ["body", name], "Input should be greater than or equal to 1", v, {"ge": 1}))
        elif v > hi:
            entries.append(_entry("less_than_equal", ["body", name], "Input should be less than or equal to %d" % hi, v, {"le": hi}))
    check_request({"questions": body.get("questions")}, "decide", limits, entries)
    if sessions.get("enabled") is False:
        raise local_error(BadRequestError, "Model %s does not serve sessions." % limits.get("model"))


class NumberAnswer(Answer):
    kind = "number"

    def __init__(self, raw):
        super().__init__(raw)
        self.number = raw["number"]
        self.expected = raw["expected"]
        self.confidence = raw["confidence"]
        self.probabilities = dict(raw["probabilities"])


_parse_answer_1_0 = parse_answer


def parse_answer(raw) -> Answer:  # noqa: F811 - spec 1.1 adds number and reads noul as truth
    if isinstance(raw, dict) and raw.get("type") == "noul":
        if _number(raw.get("noul")):
            ans = TruthAnswer.__new__(TruthAnswer)
            Answer.__init__(ans, raw)
            ans.truth = raw["noul"]
            return ans
        return UnknownAnswer(raw)
    if isinstance(raw, dict) and raw.get("type") == "number":
        ok = _number(raw.get("number")) and _number(raw.get("expected")) and _number(raw.get("confidence")) and _number_map(raw.get("probabilities"))
        return NumberAnswer(raw) if ok else UnknownAnswer(raw)
    return _parse_answer_1_0(raw)


class PointAnswer(Answer):
    """Spec 1.2: x and y, from the image's top-left corner."""

    kind = "point"
    FIELDS = ("x", "y", "present", "confidence", "image", "width", "height")

    def __init__(self, raw):
        super().__init__(raw)
        for name in self.FIELDS:
            setattr(self, name, raw[name])


class BoxAnswer(Answer):
    """Spec 1.2: the top-left (x1, y1) and bottom-right (x2, y2) corners."""

    kind = "box"
    FIELDS = ("x1", "y1", "x2", "y2", "present", "confidence", "image", "width", "height")

    def __init__(self, raw):
        super().__init__(raw)
        for name in self.FIELDS:
            setattr(self, name, raw[name])


class Place:
    """One place of a PointsAnswer (x, y) or a BoxesAnswer (x1, y1, x2, y2), with its
    confidence."""

    def __init__(self, raw, fields):
        self.raw = raw
        for name in fields:
            setattr(self, name, raw[name])


class PointsAnswer(Answer):
    """A point question asked with max. count, present, complete and
    points, left to right by the centre, each x, y and confidence."""

    kind = "points"
    FIELDS = ("count", "present", "complete", "points", "image", "width", "height")
    PLACE = ("x", "y", "confidence")
    LIST = "points"

    def __init__(self, raw):
        super().__init__(raw)
        for name in self.FIELDS:
            setattr(self, name, raw[name])
        setattr(self, self.LIST, [Place(p, self.PLACE) for p in raw[self.LIST]])


class BoxesAnswer(PointsAnswer):
    """A box question asked with max. count, present, complete and
    boxes, left to right by the centre, each x1, y1, x2, y2 and confidence."""

    kind = "boxes"
    FIELDS = ("count", "present", "complete", "boxes", "image", "width", "height")
    PLACE = ("x1", "y1", "x2", "y2", "confidence")
    LIST = "boxes"


def _places_ok(raw, cls) -> bool:
    found = raw.get(cls.LIST)
    return (
        _int(raw.get("count"))
        and raw["count"] >= 0
        and _number(raw.get("present"))
        and _number(raw.get("complete"))
        and isinstance(found, list)
        and all(isinstance(p, dict) and all(_number(p.get(n)) for n in cls.PLACE) for p in found)
        and isinstance(raw.get("image"), str)
        and _number(raw.get("width"))
        and _number(raw.get("height"))
    )


_parse_answer_1_1 = parse_answer


def parse_answer(raw) -> Answer:  # noqa: F811 - spec 1.2 adds point and box
    if isinstance(raw, dict) and raw.get("type") in ("point", "box") and ("points" in raw or "boxes" in raw):
        # The several-places shape, told by its list.
        cls = PointsAnswer if raw["type"] == "point" else BoxesAnswer
        return cls(raw) if cls.LIST in raw and _places_ok(raw, cls) else UnknownAnswer(raw)
    if isinstance(raw, dict) and raw.get("type") in ("point", "box"):
        cls = PointAnswer if raw["type"] == "point" else BoxAnswer
        ok = all(isinstance(raw.get(n), str) if n == "image" else _number(raw.get(n)) for n in cls.FIELDS)
        return cls(raw) if ok else UnknownAnswer(raw)
    return _parse_answer_1_1(raw)


# ---------------------------------------------------------------- images: inline or uploaded


def _cache_key(img):
    return hashlib.sha256(img.data).hexdigest(), img.media_type


def _cache_get(self, img):
    key = _cache_key(img)
    with self._cache_lock:
        hit = self._cache.get(key)
        if hit is None:
            return None
        if self._clock() >= hit[1] - CACHE_MARGIN_S:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return hit[0]


def _cache_put(self, img, upload_id, expires_at):
    with self._cache_lock:
        self._cache[_cache_key(img)] = (upload_id, expires_at)
        self._cache.move_to_end(_cache_key(img))
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)


def _cache_drop(self, upload_ids):
    with self._cache_lock:
        for key in [k for k, v in self._cache.items() if v[0] in upload_ids]:
            del self._cache[key]


def _parse_time(text):
    try:
        return email.utils.parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        pass
    import datetime

    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()


def _upload_images(self, imgs):
    """One presign for the distinct images, one PUT each, then the cache."""
    distinct = []
    seen = {}
    for img in imgs:
        key = _cache_key(img)
        if key not in seen:
            seen[key] = len(distinct)
            distinct.append(img)
    def presign(group):
        files = [
            {
                "content_type": img.media_type,
                "byte_size": len(img.data),
                "checksum": base64.b64encode(hashlib.md5(img.data).digest()).decode("ascii"),
                "sha256": base64.b64encode(hashlib.sha256(img.data).digest()).decode("ascii"),
            }
            for img in group
        ]
        body, _, _ = self._exchange("POST", "/v1/uploads", {"files": files})
        # The URLs live expires_in seconds (300) from now, by the client's clock.
        return body.get("uploads") or [], self._clock() + float(body.get("expires_in") or 300)

    uploads, urls_expire = presign(distinct)
    ids = {}
    for img, up in zip(distinct, uploads):
        try:
            self._put(up, img.data)
        except DecisionNodeError as exc:
            # A 403 once the URL's 5 minutes have passed means presign again,
            # once; a 403 inside them is a refusal and is raised.
            if exc.status != 403 or self._clock() < urls_expire:
                raise
            (up,), _ = presign([img])
            self._put(up, img.data)
        _cache_put(self, img, up["upload_id"], _parse_time(up["expires_at"]))
        ids[_cache_key(img)] = up["upload_id"]
    return [ids[_cache_key(img)] for img in imgs]


def _put(self, up, data):
    """The PUT: exactly the returned headers and the bytes; never the key; no redirects."""
    url = urlsplit(up["url"])
    if url.scheme == "http":
        if (url.hostname or "").lower() not in LOOPBACK_HOSTS:
            raise ValueError("the storage URL must be https, got %r" % up["url"])
    elif url.scheme != "https":
        raise ValueError("the storage URL must be https, got %r" % up["url"])
    target = url.path + ("?" + url.query if url.query else "")
    attempt = 0
    while True:
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = cls(url.hostname, url.port, timeout=self.upload_timeout)
        try:
            conn.request("PUT", target, body=data, headers=dict(up.get("headers") or {}))
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.status
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            if attempt >= self.max_retries:
                if isinstance(exc, socket.timeout):
                    raise TimeoutError("the upload timed out after %ss" % self.upload_timeout) from exc
                raise ConnectionError("upload failed: %s" % exc) from exc
            self._sleep(backoff(attempt))
            attempt += 1
            continue
        conn.close()
        if 200 <= status <= 299:
            return
        if status == 412 and attempt > 0:
            return  # an earlier attempt landed: the object is written once
        if 500 <= status <= 599 and attempt < self.max_retries:
            self._sleep(backoff(attempt))
            attempt += 1
            continue
        m = re.search(r"<Code>([^<]*)</Code>", text)
        raise DecisionNodeError("the upload to storage failed with %d" % status, status=status, error_type="upload_failed", reason=m.group(1) if m else None)


def _image_wires(self, images, always_upload=False, project=False):
    """The images' wire forms: URLs and ids as given; local bytes inline under the
    threshold, else uploaded; cached bytes by their upload_id whatever their size.
    project: nothing is uploaded, an image that would be is named by a placeholder
    id of the real length, so the body's size is known before anything is sent."""
    wires = [None] * len(images)
    placed = set()
    local = []
    for i, img in enumerate(images):
        if not isinstance(img, Image):
            wires[i] = img
        elif img.data is None:
            wires[i] = img.wire()
        else:
            hit = _cache_get(self, img)
            if hit is not None:
                wires[i] = {"id": img.id, "upload_id": hit}
                placed.add(hit)
            else:
                local.append(i)
    total = sum(len(images[i].data) for i in range(len(images)) if isinstance(images[i], Image) and images[i].data is not None)
    if local and (always_upload or total >= self.inline_threshold):
        ids = [UPLOAD_ID_PLACEHOLDER] * len(local) if project else _upload_images(self, [images[i] for i in local])
        for i, uid in zip(local, ids):
            wires[i] = {"id": images[i].id, "upload_id": uid}
            placed.add(uid)
    else:
        for i in local:
            wires[i] = images[i].wire()
    return wires, placed


def _check_decision(self, path, state, questions, images, model, videos=None, boosters=None):
    if not questions:
        raise ValueError("questions must hold at least one question")
    # Boosters are in the probe for the nesting limit, which is on the whole body
    #; nothing else about them is checked locally.
    probe = self._body(state, questions, None, model, boosters=boosters)
    self._check_local(probe, list(images or []), "decide", list(videos or []))
    _check_size(self, state, questions, images, model, videos, boosters)


def _check_body_size(body) -> None:
    """Spec 1.3: the encoded body, as it would be sent, against the API's 25 MiB."""
    size = len(_encode(body))
    if size > MAX_BODY_BYTES:
        raise local_error(RequestTooLargeError, BODY_TOO_LARGE, "request_too_large_error", "the body is %d bytes; the limit is %d" % (size, MAX_BODY_BYTES))


def _check_size(self, state, questions, images, model, videos=None, boosters=None) -> None:
    images = list(images or [])
    wires = _image_wires(self, images, project=True)[0] if images else None
    _check_body_size(self._body(state, questions, images, model, wires, list(videos or []), boosters))


def _decision_call(self, path, state, questions, images, model, videos=None, boosters=None, fallback=None):
    images = list(images or [])
    videos = list(videos or [])
    _check_decision(self, path, state, questions, images, model, videos, boosters)
    for attempt in (0, 1):
        wires, placed = _image_wires(self, images)
        body = self._body(state, questions, images, model, wires, videos, boosters, fallback)
        try:
            if _names_ocr(body):
                return self._exchange("POST", path, body, policy="ocr", timeout=None if self._timeout_given else OCR_TIMEOUT)
            return self._exchange("POST", path, body)
        except BadRequestError as exc:
            # A reference the SDK placed went stale: drop it, upload again, send once more.
            if attempt == 0 and placed and exc.error_type in STALE_UPLOAD:
                _cache_drop(self, placed)
                continue
            raise


def _names_ocr(body) -> bool:
    """Whether a decision's boosters name ocr, bare or in the object form."""
    return any(b == "ocr" or (isinstance(b, dict) and b.get("name") == "ocr") for b in body.get("boosters") or [])


def upload(self, images):
    images = list(images)
    if len(images) > MAX_UPLOAD_FILES:
        # One presign takes 1 to 16 files: its own 400, refused before it is sent.
        raise local_error(BadRequestError, "files: 1 to %d files, got %d." % (MAX_UPLOAD_FILES, len(images)))
    check_images(images, self._limits())
    out = []
    todo = []
    for img in images:
        hit = _cache_get(self, img)
        out.append(hit)
        if hit is None:
            todo.append(img)
    if todo:
        new = iter(_upload_images(self, todo))
        out = [x if x is not None else next(new) for x in out]
    return out


def number_helper(self, state=_UNSET, instructions=None, min=None, max=None, step=None, images=None, model=None, videos=None, boosters=None, fallback=None):  # noqa: A002
    return self._helper(state, "number", number(instructions, min, max, step), images, model, NumberAnswer, videos, boosters, fallback)


DecisionNode._put = _put
DecisionNode._decision_call = _decision_call
DecisionNode.upload = upload
DecisionNode.number = number_helper


# ---------------------------------------------------------------- sessions


class SessionClosed(Exception):
    def __init__(self, code, reason=""):
        super().__init__("session closed with %s %s" % (code, reason))
        self.code = code
        self.reason = reason
        self.reopen = code in REOPEN_CODES


class Reply:
    def __init__(self, raw):
        self.raw = raw
        self.seq = raw.get("seq")
        self.answers = {k: parse_answer(v) for k, v in (raw.get("answers") or {}).items()}
        self.dropped = raw.get("dropped")
        self.usage = raw.get("usage")
        self.latency_ms = raw.get("latency_ms")
        self.safety = raw.get("safety")


class FrameError:
    def __init__(self, raw):
        self.raw = raw
        err = raw.get("error") or {}
        self.seq = raw.get("seq")
        self.error_type = err.get("error_type")
        self.message = err.get("message")
        self.category = err.get("category")
        self.retry_after_ms = raw.get("retry_after_ms")
        self.usage = raw.get("usage")
        self.safety = raw.get("safety")


class _WebSocket:
    """A minimal RFC 6455 client: masked frames out, automatic pongs, one reader thread."""

    def __init__(self, url, headers, timeout):
        parts = urlsplit(url)
        if parts.scheme == "ws":
            if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
                raise ValueError("the stream URL must be wss, got %r" % url)
            sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        elif parts.scheme == "wss":
            raw = socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname)
        else:
            raise ValueError("the stream URL must be wss, got %r" % url)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = ["GET %s HTTP/1.1" % (parts.path or "/"), "Host: %s" % parts.netloc, "Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Key: %s" % key, "Sec-WebSocket-Version: 13"]
        lines += ["%s: %s" % (k, v) for k, v in headers.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        self.sock = sock
        self.buf = b""
        head = self._until(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        if status != 101:
            raise DecisionNodeError("the stream refused the upgrade with %d" % status, status=status)
        self.sock.settimeout(None)
        self.lock = threading.Lock()
        self.events = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the stream closed during the handshake")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(marker)
        return head

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        key = int.from_bytes((mask * (n // 4 + 1))[:n], "big") if n else 0
        body = (int.from_bytes(payload, "big") ^ key).to_bytes(n, "big") if n else b""
        with self.lock:
            self.sock.sendall(head + mask + body)

    def _reader(self):
        parts = []
        try:
            while True:
                b1, b2 = self._read(2)
                opcode = b1 & 0x0F
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                payload = self._read(n)
                if opcode == 0x9:
                    self.send(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x8:
                    code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                    try:
                        self.send(0x8, payload[:2])
                    except OSError:
                        pass
                    self.events.put(("close", (code, payload[2:].decode("utf-8", "replace"))))
                    return
                parts.append(payload)
                if b1 & 0x80:
                    self.events.put(("text", b"".join(parts).decode("utf-8")))
                    parts = []
        except (EOFError, OSError):
            self.events.put(("close", (1006, "")))

    def close(self, code=1000):
        try:
            self.send(0x8, struct.pack(">H", code))
        except OSError:
            pass


class Session:
    def __init__(self, client, raw):
        self.client = client
        self.raw = raw
        for k, v in raw.items():
            setattr(self, k, v)
        self.answers = {k: parse_answer(v) for k, v in (raw.get("answers") or {}).items()}
        self._ws = None
        self._seq = 0
        self._closed = None

    def connect(self):
        headers = {"Authorization": "Bearer %s" % self.client.api_key, "User-Agent": "decisionnode-python/%s" % VERSION}
        headers.update(self.client.headers)
        self._ws = _WebSocket(self.raw["stream_url"], headers, self.client.timeout)
        return self

    def send(self, frame=None, image=None, questions=None, reset=None, seq=None):
        self.check_frame(frame, image, questions, reset, seq)
        if seq is None:
            self._seq += 1
            seq = self._seq
        msg = {"seq": seq}
        if frame is not None:
            msg["frame"] = frame
        if image is not None:
            if isinstance(image, Image):
                wires, _ = _image_wires(self.client, [image])
                wire = dict(wires[0])
                wire.pop("id", None)
                msg["image"] = wire
            else:
                msg["image"] = image
        if questions is not None:
            msg["questions"] = list(questions)
        if reset is not None:
            msg["reset"] = reset
        self._ws.send(0x1, json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        return seq

    def check_frame(self, frame=None, image=None, questions=None, reset=None, seq=None):
        """The frame's fixed rules, the checks send() runs before
        anything goes on the socket. A fault raises the API's class with the error_type
        and message the server's frame error would carry (api_usage_error); nothing is
        sent and seq is untouched. Returns None for a frame send() would send."""
        if seq is not None and (not _is_int(seq) or not 0 <= seq <= MAX_SEQ):
            raise local_error(BadRequestError, "seq: must be an integer from 0 to %d." % MAX_SEQ)
        if frame is None and image is None:
            raise local_error(BadRequestError, "frame: a frame needs a frame value or an image.")
        if frame is not None and (not _is_sdl(frame) or frame == ""):
            raise local_error(BadRequestError, "frame: must be a string, an object or an array, not empty.")
        if questions is not None:
            questions = list(questions)
            if not questions:
                raise local_error(BadRequestError, "questions: must be a non-empty list of the session's question ids.")
            known = self.raw.get("questions")
            for qid in questions if isinstance(known, list) else ():
                if qid not in known:
                    raise local_error(BadRequestError, "questions: unknown question id %s" % ("'%s'" % qid if isinstance(qid, str) else json.dumps(qid)))
        if reset is not None and not isinstance(reset, bool):
            raise local_error(BadRequestError, "reset: must be true or false.")
        if frame is not None and nesting_depth(frame) + 1 > MAX_NESTING:
            raise local_error(BadRequestError, "The frame nests more than %d objects and arrays inside each other." % MAX_NESTING)
        if isinstance(image, Image):
            if image.url is not None:
                raise local_error(BadRequestError, "image: a frame takes bytes or an upload, not a url.")
            check_images([image], self.client._limits(self.raw.get("model")), id_length=False)
        return None

    def next_event(self, timeout=10.0):
        """The next Reply, FrameError or SessionClosed (the last one)."""
        if self._closed is not None:
            return self._closed
        kind, value = self._ws.events.get(timeout=timeout)
        if kind == "close":
            self._closed = SessionClosed(*value)
            return self._closed
        raw = json.loads(value)
        return FrameError(raw) if "error" in raw else Reply(raw)

    def replies(self, timeout=10.0):
        while True:
            ev = self.next_event(timeout)
            yield ev
            if isinstance(ev, SessionClosed):
                return

    def close(self):
        if self._ws is not None:
            self._ws.close(1000)

    def end(self):
        try:
            body, _ = self.client._request("DELETE", "/v1/sessions/%s" % self.raw["session_id"])
            return body
        except NotFoundError:
            return None  # already ended: success


def _open_body(self, state=None, questions=None, instructions=None, model=None, window=None, ttl_seconds=None):
    if not questions:
        raise ValueError("questions must hold at least one question")
    body = {"model": model or self.model}
    for name, value in (("instructions", instructions), ("state", state)):
        if value is not None:
            body[name] = value
    body["questions"] = dict(questions)
    for name, value in (("window", window), ("ttl_seconds", ttl_seconds)):
        if value is not None:
            body[name] = value
    return body


def open_session(self, state=None, questions=None, instructions=None, model=None, window=None, ttl_seconds=None):
    body = _open_body(self, state, questions, instructions, model, window, ttl_seconds)
    check_session_open(body, self._limits(body["model"]))
    raw, _, _ = self._exchange("POST", "/v1/sessions", body, policy="session_open")
    return Session(self, raw)


def supervise(self, body, feed, on_reply, max_sessions=10):
    """One feed, latest wins, reopen on the reopen codes."""
    session = None
    failures = 0
    opened = 0
    answered = 0
    last = None
    pending = 0

    def drain(block_s):
        nonlocal last, answered, pending, session
        deadline = time.monotonic() + block_s
        while session is not None:
            try:
                ev = session.next_event(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return
            if isinstance(ev, SessionClosed):
                last = ev
                session = None
                return
            pending = max(0, pending - 1 - len(getattr(ev, "dropped", None) or []))
            if isinstance(ev, Reply):
                answered += 1
            on_reply(ev)
            if block_s > 0 and pending == 0:
                return

    for reading in feed:
        drain(0.0)
        while session is None:
            if last is not None and not last.reopen and last.code != 1013:
                return last
            if opened >= max_sessions:
                return last
            if last is not None and last.code not in (4408, 4410):
                if answered:
                    failures = 0
                self._sleep(min(30.0, 1.0 * (2**failures)) * (0.5 + random.random() * 0.5))
                failures += 1
            session = open_session(self, **body)
            opened += 1
            answered = 0
            session.connect()
            last = None
        session.send(**reading)
        pending += 1
    drain(3.0)
    if session is not None:
        session.end()
        session.close()
    return last


DecisionNode.open_session = open_session
DecisionNode.supervise = supervise


# ---------------------------------------------------------------- batches


class BatchResult:
    def __init__(self, raw):
        self.raw = raw
        self.custom_id = raw.get("custom_id")
        self.status = raw.get("status")
        self.headers = {k.lower(): v for k, v in (raw.get("headers") or {}).items()}
        self.decision = None
        self.error = None
        body = raw.get("body")
        if self.status == 200 and isinstance(body, dict):
            self.decision = Decision(body, self.headers)
        elif isinstance(self.status, int):
            self.error = make_error(_Response(self.status, self.headers, json.dumps(body)))


def _check_lines(self, requests, allow_empty) -> None:
    """The batch request rules, for the requests of one call, before any is
    sent: the custom_id rules (lines numbered from 1 within the call), an empty add, and
    the limits /v1/models states for the key (requests_per_batch, max_custom_id_chars)
    once the client holds them. Each line's own request is checked as a decide call."""
    if not requests and not allow_empty:
        raise local_error(BadRequestError, "The call holds no requests.")
    batch = self._limits().get("batch") or {}
    most = batch.get("requests_per_batch")
    if most is not None and len(requests) > most:
        raise local_error(BadRequestError, "The batch would hold %d requests. The limit is %d." % (len(requests), most))
    longest = batch.get("max_custom_id_chars")
    seen = set()
    for n, req in enumerate(requests, start=1):
        if not isinstance(req, dict):
            raise ValueError("request %d must be a mapping of decide() arguments and a custom_id" % n)
        if req.get("custom_id") is None:
            raise local_error(BadRequestError, "Line %d has no custom_id." % n)
        cid = req["custom_id"]
        if not isinstance(cid, str) or len(cid) < 1 or (longest is not None and len(cid) > longest):
            raise local_error(BadRequestError, "Line %d: custom_id must be a string of 1 to %d characters." % (n, longest or 512))
        if cid in seen:
            shown = json.dumps(cid, ensure_ascii=False)
            raise local_error(BadRequestError, "Line %d: custom_id %s is already in the batch." % (n, shown if len(shown) <= 64 else shown[:64] + "..."))
        seen.add(cid)


def _line_body(self, req) -> dict:
    questions = req.get("questions")
    if not questions:
        raise ValueError("questions must hold at least one question")
    out = {"custom_id": req["custom_id"], "model": req.get("model") or self.model, "state": "" if req.get("state") is None else req["state"], "questions": dict(questions)}
    if req.get("boosters"):
        out["boosters"] = booster_wires(req["boosters"])
    if req.get("fallback") is not None:
        out["fallback"] = req["fallback"]
    return out


def _check_batch(self, requests, allow_empty) -> None:
    """Every request of a create or add call checked before any image is uploaded or
    any line sent: the call's custom_id rules, then each line as a decide request."""
    _check_lines(self, requests, allow_empty)
    for req in requests:
        out = _line_body(self, req)
        self._check_local({k: v for k, v in out.items() if k != "custom_id"}, req.get("images") or [], videos=req.get("videos"))
        # Spec 1.3: each line, as it would be sent, within the API's body limit.
        _check_body_size(_line_obj(self, req, project=True))


def _line_obj(self, req, project=False) -> dict:
    images = list(req.get("images") or [])
    out = _line_body(self, req)
    if images:
        out["images"], _ = _image_wires(self, images, project=project)
    if req.get("videos"):
        out["videos"] = video_wires(req["videos"])
    for k, v in req.items():
        if k not in out and k not in ("images", "videos", "boosters"):
            out[k] = v
    return out


def _line(self, req) -> bytes:
    return json.dumps(_line_obj(self, req), ensure_ascii=False).encode("utf-8") + b"\n"


COLLECTION_NAME_MAX = 80  # with the item's reference and label below
ITEM_REFERENCE_MAX, ITEM_LABEL_MAX = 64, 128
ITEM_REFERENCE_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"  # checked after the length, so an empty one is too short


ECHO_CHARS = 64  # a collections 422 echoes a string cut to 64 characters, then "..."


def _echoed(value):
    """An input as a collections 422 entry echoes it, as the API does: a string cut to
    64 characters with "..." added, an object or an array never (null)."""
    if isinstance(value, str):
        return value[:ECHO_CHARS] + ("..." if len(value) > ECHO_CHARS else "")
    if isinstance(value, (dict, list)):
        return None
    return value


def _string_entries(loc, value, most, pattern=None) -> list:
    """A collections string field's 422 entry, as the API answers it: missing, not a
    string, empty, too long, or (with pattern) not matching it; its input echoed within
    bounds."""
    if value is None:
        return [_entry("missing", loc, "Field required", None)]
    if not isinstance(value, str):
        return [_entry("string_type", loc, "Input should be a valid string", _echoed(value))]
    if not value:
        return [_entry("string_too_short", loc, "String should have at least 1 character", value, {"min_length": 1})]
    if len(value) > most:
        return [_entry("string_too_long", loc, "String should have at most %d characters" % most, _echoed(value), {"max_length": most})]
    if pattern and not re.fullmatch(pattern, value):
        return [_entry("string_pattern_mismatch", loc, "String should match pattern '%s'" % pattern, value, {"pattern": pattern})]
    return []


METADATA_KEYS, METADATA_KEY_CHARS, METADATA_TEXT_CHARS = 20, 40, 500
METADATA_KEY_PATTERN = r"[a-z0-9_]{1,40}"


def _metadata_entries(metadata) -> list:
    """Suite 1.15: an item's metadata's 422 entries, the limits a client may hold: an
    object of at most 20 keys (that alone, before any key is read), each key 1 to 40 of
    a-z, 0-9 and _, each value a text of at most 500 characters, a number or a boolean.
    A text's control or invisible characters, a number's range and the 4 KB are the
    server's."""
    loc = ["body", "metadata"]
    if not isinstance(metadata, dict):
        return [_entry("value_error", loc, "Metadata is an object of keys and values.", _echoed(metadata))]
    if len(metadata) > METADATA_KEYS:
        return [_entry("value_error", loc, "Metadata holds at most %d keys." % METADATA_KEYS, None)]
    out = []
    for key, value in metadata.items():
        if not isinstance(key, str) or not re.fullmatch(METADATA_KEY_PATTERN, key):
            key = str(key)
            shown = "".join("?" if unicodedata.category(c) == "Cc" else c for c in key[:METADATA_KEY_CHARS]) + ("..." if len(key) > METADATA_KEY_CHARS else "")
            out.append(_entry("value_error", loc, 'A metadata key is 1 to %d characters of a-z, 0-9 and _: "%s".' % (METADATA_KEY_CHARS, shown), None))
        elif isinstance(value, str):
            if len(value) > METADATA_TEXT_CHARS:
                out.append(_entry("value_error", loc + [key], "A metadata text is at most %d characters." % METADATA_TEXT_CHARS, _echoed(value)))
        elif not isinstance(value, (bool, int, float)):
            out.append(_entry("value_error", loc + [key], "A metadata value is a text, a number or true or false.", _echoed(value)))
    return out


def _reference_query(name, value):
    """Suite 1.15: a reference or reference_prefix of the items list, checked as the API
    checks it."""
    if not isinstance(value, str) or not re.fullmatch(ITEM_REFERENCE_PATTERN, value):
        raise local_error(BadRequestError, "Query parameter %s must be 1 to 64 characters of A-Z, a-z, 0-9, _, ., : and -, got %s." % (name, _printable(str(value), 64)))


def _printable(value, most):
    """A caller's value as an API message echoes it: its first most characters, any but
    printable ASCII as \\u{hex}, then "..." when it was longer."""
    out = "".join(c if 0x20 <= ord(c) <= 0x7E else "\\u{%x}" % ord(c) for c in value[:most])
    return out + ("..." if len(value) > most else "")


class Collections:
    """A workspace's collections of faces, and the public ones it may
    use. Every result is the API's object as sent. create and enroll send an
    Idempotency-Key, a new UUID per call and the same on every attempt of it, so the
    server enrolls and bills once however often the call is tried; they then retry like
    any call."""

    def __init__(self, client):
        self.c = client

    def _call(self, method, path, body=None, policy="default", key=None):
        extra = {"Idempotency-Key": key} if key else None
        parsed, _, _ = self.c._exchange(method, path, body, policy=policy, extra_headers=extra)
        return parsed

    @staticmethod
    def _id(value):
        return quote(str(value), safe="")

    def create(self, name, type="face"):  # noqa: A002 - the API's field
        entries = []
        if type != "face":
            entries.append(_entry("literal_error", ["body", "type"], "Input should be 'face'", _echoed(type), {"expected": "'face'"}))
        entries.extend(_string_entries(["body", "name"], name, COLLECTION_NAME_MAX))
        if entries:
            raise local_422(entries)
        return self._call("POST", "/v1/collections", {"type": type, "name": name}, "idempotent", str(uuid.uuid4()))

    def list(self):
        return self._call("GET", "/v1/collections")["collections"]

    def get(self, collection_id):
        return self._call("GET", "/v1/collections/%s" % self._id(collection_id))

    def delete(self, collection_id):
        return self._call("DELETE", "/v1/collections/%s" % self._id(collection_id))

    def enroll(self, collection_id, image, reference, label=None, metadata=None):
        """One face from one image: sent as a decision's image is (a URL as given, local
        bytes inline or uploaded); from 1.15 the item's metadata when given."""
        entries = []
        if image is None:
            entries.append(_entry("missing", ["body", "image"], "Field required", None))
        entries.extend(_string_entries(["body", "reference"], reference, ITEM_REFERENCE_MAX, ITEM_REFERENCE_PATTERN))
        if label is not None:
            # Its pattern (no control or invisible character) is the server's to judge.
            entries.extend(_string_entries(["body", "label"], label, ITEM_LABEL_MAX))
        if metadata is not None:
            entries.extend(_metadata_entries(metadata))
        if entries:
            raise local_422(entries)
        # Only a URL's rules are a decision's (the 400 image_url_refused); every other fault
        # of the picture is the server's 422 image_unreadable, which no client can foresee.
        if isinstance(image, Image) and image.url is not None:
            check_image_url(image.id, image.url)
        wires, _ = _image_wires(self.c, [image])
        body = {"image": wires[0], "reference": reference}
        if label is not None:
            body["label"] = label
        if metadata is not None:
            body["metadata"] = metadata
        return self._call("POST", "/v1/collections/%s/items" % self._id(collection_id), body, "idempotent", str(uuid.uuid4()))

    def update_item(self, collection_id, item_id, reference=_UNSET, label=_UNSET, metadata=_UNSET):
        """Suite 1.15: PATCH an item, sending only the fields given; a label given as null
        clears it and metadata replaces the item's whole object. It carries an
        Idempotency-Key, as create and enroll, and retries as they do."""
        entries, body = [], {}
        if reference is not _UNSET:
            entries.extend(_string_entries(["body", "reference"], reference, ITEM_REFERENCE_MAX, ITEM_REFERENCE_PATTERN))
            body["reference"] = reference
        if label is not _UNSET:
            if label is not None:
                entries.extend(_string_entries(["body", "label"], label, ITEM_LABEL_MAX))
            body["label"] = label
        if metadata is not _UNSET:
            entries.extend(_metadata_entries(metadata))
            body["metadata"] = metadata
        if not body:
            entries.append(_entry("missing", ["body"], "Send reference, label, metadata or any of them", None))
        if entries:
            raise local_422(entries)
        path = "/v1/collections/%s/items/%s" % (self._id(collection_id), self._id(item_id))
        return self._call("PATCH", path, body, "idempotent", str(uuid.uuid4()))

    def list_items(self, collection_id, offset=None, limit=None, reference=None, reference_prefix=None):
        # offset an integer of 0 or more, limit 1 to 100, as batches.list.
        if offset is not None and (not _is_int(offset) or offset < 0):
            raise local_error(BadRequestError, "Query parameter offset must be an integer of 0 or more, got %s." % (offset,))
        if limit is not None and (not _is_int(limit) or not 1 <= limit <= LIST_LIMIT_MAX):
            raise local_error(BadRequestError, "Query parameter limit must be an integer from 1 to %d, got %s." % (LIST_LIMIT_MAX, limit))
        # From 1.15 the items of a reference (exact) or of a prefix, never both.
        if reference is not None and reference_prefix is not None:
            raise local_error(BadRequestError, "Send query parameter reference or reference_prefix, not both.")
        q = []
        if offset is not None:
            q.append("offset=%d" % offset)
        if limit is not None:
            q.append("limit=%d" % limit)
        for name, value in (("reference", reference), ("reference_prefix", reference_prefix)):
            if value is not None:
                _reference_query(name, value)
                q.append("%s=%s" % (name, quote(value, safe="")))
        return self._call("GET", "/v1/collections/%s/items" % self._id(collection_id) + ("?" + "&".join(q) if q else ""))

    def delete_item(self, collection_id, item_id):
        return self._call("DELETE", "/v1/collections/%s/items/%s" % (self._id(collection_id), self._id(item_id)))


USAGE_GRANULARITIES = ("none", "hour", "day", "month")
USAGE_GROUPS = ("model", "channel")
USAGE_SCOPES = ("full", "decide", "keys")
USAGE_STATUSES = ("active", "expired", "revoked", "all")
USAGE_PAGE_MAX = 1000
USAGE_MAX_KEY_IDS = 100
USAGE_KEY_ID = r"key_[0-9a-f]{32}"
USAGE_LABEL_NAME = r"[a-z0-9_.-]{1,32}"


class Usage:
    """Suite 1.18: per-key usage. key(key_id, ...) is GET /v1/keys/{key_id}/usage and
    list(start, end, ...) GET /v1/usage, each returning the API's object as sent; all
    follows next_cursor until it is null and returns every page's items, since a page can
    hold fewer keys than limit. Reads retry as any call. The rules the API checks while it
    reads the query are checked locally first; the period's (the hours it is rounded to,
    400 days, hour granularity over 7 days, limit with buckets) and the cursor are the
    server's."""

    def __init__(self, client):
        self.c = client

    def _period(self, start, end, granularity, group_by) -> list:
        if granularity is not None and granularity not in USAGE_GRANULARITIES:
            raise local_error(BadRequestError, "granularity is none, hour, day or month.")
        if group_by is not None and group_by not in USAGE_GROUPS:
            raise local_error(BadRequestError, "group_by is model or channel.")
        q = []
        for name, value in (("start", start), ("end", end), ("granularity", granularity), ("group_by", group_by)):
            if value is not None:
                q.append((name, value))
        return q

    def _get(self, path, q):
        query = "&".join("%s=%s" % (quote(k, safe=""), quote(str(v), safe="")) for k, v in q)
        parsed, _, _ = self.c._exchange("GET", path + ("?" + query if query else ""))
        return parsed

    def key(self, key_id, start=None, end=None, granularity=None, group_by=None):
        """One key's usage; start and end default, on the server, to the UTC month so far."""
        q = self._period(start, end, granularity, group_by)
        return self._get("/v1/keys/%s/usage" % quote(str(key_id), safe=""), q)

    def _list_query(self, start, end, granularity, group_by, labels, device_id, key_ids, scope, status, include_empty, limit, cursor) -> list:
        q = self._period(start, end, granularity, group_by)
        # Labels by name, so a map without order sends them the same every time.
        for name, value in sorted((labels or {}).items()):
            if not isinstance(name, str) or not re.fullmatch(USAGE_LABEL_NAME, name) or not isinstance(value, str) \
                    or not 1 <= len(value) <= 64 or any(unicodedata.category(c) == "Cc" for c in value):
                raise local_error(BadRequestError, "A label filter is name:value, as labels are written.")
            q.append(("label", "%s:%s" % (name, value)))
        if device_id is not None:
            if not isinstance(device_id, str) or not 1 <= len(device_id) <= 128 or not all(0x20 <= ord(ch) <= 0x7E for ch in device_id) or device_id.startswith(" "):
                raise local_error(BadRequestError, "device_id is 1 to 128 printable ASCII characters, not starting with a space.")
            q.append(("device_id", device_id))
        for i, kid in enumerate(key_ids or []):
            if not isinstance(kid, str) or not re.fullmatch(USAGE_KEY_ID, kid):
                raise local_error(BadRequestError, "key_id is a key's id, key_ and 32 hex digits.")
            if i == USAGE_MAX_KEY_IDS:
                raise local_error(BadRequestError, "At most 100 key_id filters.")
            q.append(("key_id", kid))
        if scope is not None:
            if scope not in USAGE_SCOPES:
                raise local_error(BadRequestError, "scope is full, decide or keys.")
            q.append(("scope", scope))
        if status is not None:
            if status not in USAGE_STATUSES:
                raise local_error(BadRequestError, "status is active, expired, revoked or all.")
            q.append(("status", status))
        if include_empty is not None:
            if not isinstance(include_empty, bool):
                raise local_error(BadRequestError, "include_empty is true or false.")
            q.append(("include_empty", "true" if include_empty else "false"))
        if limit is not None:
            if not _is_int(limit) or not 1 <= limit <= USAGE_PAGE_MAX:
                raise local_error(BadRequestError, "limit is a whole number from 1 to 1000.")
            q.append(("limit", limit))
        if cursor is not None:
            q.append(("cursor", cursor))
        return q

    def list(self, start, end, granularity=None, group_by=None, labels=None, device_id=None, key_ids=None, scope=None,
             status=None, include_empty=None, limit=None, cursor=None):
        return self._get("/v1/usage", self._list_query(start, end, granularity, group_by, labels, device_id, key_ids, scope, status, include_empty, limit, cursor))

    def all(self, start, end, granularity=None, group_by=None, labels=None, device_id=None, key_ids=None, scope=None,
            status=None, include_empty=None, limit=None):
        """Every key's usage over the period: list's pages, next_cursor followed until null."""
        items, cursor = [], None
        while True:
            page = self.list(start, end, granularity, group_by, labels, device_id, key_ids, scope, status, include_empty, limit, cursor)
            items.extend(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return items


KEYS_PAGE_MAX = 200
KEYS_MAX_LABELS = 16
KEYS_MAX_REVOKE_DEVICES = 1000
IDEMPOTENCY_KEY_RULE = "Idempotency-Key must be sent once, as 1 to 255 printable ASCII characters."


def _key_entry(kind, loc, msg, value):
    return _entry(kind, ["body"] + list(loc), msg, _echoed(value))


def _key_name_entries(name) -> list:
    """A key's name, the bounds a client may hold: trimmed, 1 to 48 characters. Its
    character set (letters, digits, spaces, dots, dashes, underscores, as the server's
    Unicode tables read letters and digits) is the server's."""
    if not isinstance(name, str) or not name.strip():
        return [_key_entry("value_error", ["name"], "Give the key a name.", name)]
    if len(name.strip()) > 48:
        return [_key_entry("value_error", ["name"], "Key names are at most 48 characters.", name)]
    return []


def _key_device_ok(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and all(0x20 <= ord(c) <= 0x7E for c in value) and not value.startswith(" ")


def _key_labels_entries(labels) -> list:
    if not isinstance(labels, dict):
        return [_key_entry("value_error", ["labels"], "labels is an object of names to values.", labels)]
    if len(labels) > KEYS_MAX_LABELS:
        return [_key_entry("value_error", ["labels"], "A key has at most 16 labels.", labels)]
    for name, value in labels.items():
        if not isinstance(name, str) or not re.fullmatch(USAGE_LABEL_NAME, name):
            return [_key_entry("value_error", ["labels"], "A label's name is 1 to 32 of a-z, 0-9, _ . and -.", labels)]
        if not isinstance(value, str) or not 1 <= len(value) <= 64 or any(unicodedata.category(c) == "Cc" for c in value):
            return [_key_entry("value_error", ["labels", name], "A label's value is 1 to 64 characters of text.", labels)]
    return []


def _key_cap_entries(cap) -> list:
    usd = cap.get("usd") if isinstance(cap, dict) else None
    ok = isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd)
    cents = round(usd * 100) if ok else 0
    if not ok or abs(usd * 100 - cents) >= 1e-6 or cents < 1 or usd > 1000:
        return [_key_entry("value_error", ["daily_spend_cap"], "daily_spend_cap.usd is from 0.01 to 1000, in cents, or null.", cap)]
    return []


def _key_fields(body, entries, device_id, labels, limits, daily_spend_cap, expires_at, may_search_collections, clears):
    """The fields beyond the name, in the order the body sends them; on an update a
    field given as null (clears) is sent as null and checked no further."""
    if device_id is not _UNSET:
        if not _key_device_ok(device_id):
            entries.append(_key_entry("value_error", ["device_id"], "device_id is 1 to 128 printable ASCII characters, not starting with a space.", device_id))
        body["device_id"] = device_id
    if labels is not _UNSET:
        if not (clears and labels is None):
            entries.extend(_key_labels_entries(labels))
        body["labels"] = labels
    if limits is not _UNSET:
        if not (clears and limits is None) and not isinstance(limits, dict):
            entries.append(_key_entry("value_error", ["limits"], "limits is an object, or null.", limits))
        body["limits"] = limits
    if daily_spend_cap is not _UNSET:
        if not (clears and daily_spend_cap is None):
            entries.extend(_key_cap_entries(daily_spend_cap))
        body["daily_spend_cap"] = daily_spend_cap
    if expires_at is not _UNSET:
        body["expires_at"] = expires_at
    if may_search_collections is not _UNSET:
        body["may_search_collections"] = may_search_collections


class Keys:
    """Suite 1.20: a keys key's decide keys, /v1/keys. create(name, ...) is POST /v1/keys,
    with an Idempotency-Key (the one given, else a new UUID per call, the same on every
    attempt), so a retry or a repeat answers the same key and secret; it retries as any
    call. list and all page the keys (all follows next_cursor), update is PATCH with only
    the fields given (null clears labels, limits, the cap and the expiry), revoke is
    DELETE, revoke_by_labels POST /v1/keys/revoke. Each returns the API's object as sent.
    The fixed rules are checked locally first, the first one refused as the API would; a
    limit's ceiling (the workspace's), the expiry's window (from now) and a name's
    characters are the server's."""

    def __init__(self, client):
        self.c = client

    def _call(self, method, path, body=None, policy="default", key=None):
        extra = {"Idempotency-Key": key} if key else None
        parsed, _, _ = self.c._exchange(method, path, body, policy=policy, extra_headers=extra)
        return parsed

    def create(self, name, device_id=_UNSET, labels=_UNSET, limits=_UNSET, daily_spend_cap=_UNSET, expires_at=_UNSET,
               may_search_collections=_UNSET, idempotency_key=None):
        entries = _key_name_entries(name)
        body = {"name": name}
        if not entries:
            _key_fields(body, entries, device_id, labels, limits, daily_spend_cap, expires_at, may_search_collections, clears=False)
        if entries:
            raise local_422(entries[:1])
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255
                                            or not all(0x20 <= ord(c) <= 0x7E for c in idempotency_key)):
            raise local_error(BadRequestError, IDEMPOTENCY_KEY_RULE)
        return self._call("POST", "/v1/keys", body, "idempotent", idempotency_key or str(uuid.uuid4()))

    def _list_query(self, labels, device_id, status, limit, cursor) -> list:
        q = []
        for name, value in sorted((labels or {}).items()):
            if not isinstance(name, str) or not re.fullmatch(USAGE_LABEL_NAME, name) or not isinstance(value, str) \
                    or not 1 <= len(value) <= 64 or any(unicodedata.category(c) == "Cc" for c in value):
                raise local_error(BadRequestError, "A label filter is name:value, as labels are written.")
            q.append(("label", "%s:%s" % (name, value)))
        if device_id is not None:
            if not _key_device_ok(device_id):
                raise local_error(BadRequestError, "device_id is 1 to 128 printable ASCII characters, not starting with a space.")
            q.append(("device_id", device_id))
        if status is not None:
            if status not in USAGE_STATUSES:
                raise local_error(BadRequestError, "status is active, expired, revoked or all.")
            q.append(("status", status))
        if limit is not None:
            if not _is_int(limit) or not 1 <= limit <= KEYS_PAGE_MAX:
                raise local_error(BadRequestError, "limit is a whole number from 1 to 200.")
            q.append(("limit", limit))
        if cursor is not None:
            q.append(("cursor", cursor))
        return q

    def list(self, labels=None, device_id=None, status=None, limit=None, cursor=None):
        q = self._list_query(labels, device_id, status, limit, cursor)
        query = "&".join("%s=%s" % (quote(k, safe=""), quote(str(v), safe="")) for k, v in q)
        return self._call("GET", "/v1/keys" + ("?" + query if query else ""))

    def all(self, labels=None, device_id=None, status=None, limit=None):
        items, cursor = [], None
        while True:
            page = self.list(labels, device_id, status, limit, cursor)
            items.extend(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return items

    def update(self, key_id, name=_UNSET, labels=_UNSET, limits=_UNSET, daily_spend_cap=_UNSET, expires_at=_UNSET,
               may_search_collections=_UNSET):
        body, entries = {}, []
        if name is not _UNSET:
            entries = _key_name_entries(name)
            body["name"] = name
        if not entries:
            _key_fields(body, entries, _UNSET, labels, limits, daily_spend_cap, expires_at, may_search_collections, clears=True)
        if entries:
            raise local_422(entries[:1])
        if not body:
            raise local_error(BadRequestError, "Nothing to change.")
        return self._call("PATCH", "/v1/keys/%s" % quote(str(key_id), safe=""), body)

    def revoke(self, key_id):
        return self._call("DELETE", "/v1/keys/%s" % quote(str(key_id), safe=""))

    def revoke_by_labels(self, labels, device_ids=None, dry_run=None):
        """Revokes the active decide keys that carry every label given (and, with
        device_ids, one of those devices), at most 10,000 a call (more says whether any are
        left); a dry run counts them (would_revoke). Retried as any call."""
        entries = _key_labels_entries(labels)
        if not entries and not labels:
            entries = [_key_entry("value_error", ["labels"], "A bulk revoke names at least one label: every key it revokes has them all.", labels)]
        body = {"labels": labels}
        if not entries and device_ids is not None:
            if not isinstance(device_ids, (list, tuple)) or len(device_ids) > KEYS_MAX_REVOKE_DEVICES:
                entries = [_key_entry("value_error", ["device_ids"], "device_ids is a list of at most 1,000 device ids.", device_ids)]
            else:
                for d in device_ids:
                    if not _key_device_ok(d):
                        entries = [_key_entry("value_error", ["device_id"], "device_id is 1 to 128 printable ASCII characters, not starting with a space.", d)]
                        break
            body["device_ids"] = list(device_ids) if isinstance(device_ids, (list, tuple)) else device_ids
        if entries:
            raise local_422(entries[:1])
        if dry_run is not None:
            body["dry_run"] = dry_run
        return self._call("POST", "/v1/keys/revoke", body)


class Batches:
    def __init__(self, client):
        self.c = client

    def _call(self, method, path, body=None, policy="default", content_type=None):
        parsed, _, _ = self.c._exchange(method, path, body, policy=policy, content_type=content_type)
        return parsed

    def create(self, requests=()):
        requests = list(requests)
        _check_batch(self.c, requests, allow_empty=True)
        data = b"".join(_line(self.c, r) for r in requests)
        return self._call("POST", "/v1/batches", data, "mutation", "application/x-ndjson")

    def add(self, batch_id, requests):
        requests = list(requests)
        _check_batch(self.c, requests, allow_empty=False)
        lines = [_line(self.c, r) for r in requests]
        chunks, cur, size = [], [], 0
        for ln in lines:
            if cur and size + len(ln) > BATCH_ADD_BYTES:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(ln)
            size += len(ln)
        chunks.append(cur)
        out = None
        for chunk in chunks:
            out = self._call("POST", "/v1/batches/%s/requests" % batch_id, b"".join(chunk), "mutation", "application/x-ndjson")
        return out

    def finalize(self, batch_id):
        try:
            return self._call("POST", "/v1/batches/%s/finalize" % batch_id, None, "mutation")
        except DecisionNodeError as exc:
            if exc.status == 409 and exc.error_type == "invalid_batch_state":
                batch = self.get(batch_id)
                if batch.get("state") != "draft":
                    return batch  # an earlier finalize went through
            raise

    def get(self, batch_id):
        return self._call("GET", "/v1/batches/%s" % batch_id)

    def get_with_headers(self, batch_id):
        parsed, headers, _ = self.c._exchange("GET", "/v1/batches/%s" % batch_id)
        return parsed, headers

    def list(self, offset=None, limit=None):
        # offset an integer of 0 or more, limit 1 to 100.
        if offset is not None and (not _is_int(offset) or offset < 0):
            raise local_error(BadRequestError, "Query parameter offset must be an integer of 0 or more, got %s." % (offset,))
        if limit is not None and (not _is_int(limit) or not 1 <= limit <= LIST_LIMIT_MAX):
            raise local_error(BadRequestError, "Query parameter limit must be an integer from 1 to %d, got %s." % (LIST_LIMIT_MAX, limit))
        q = []
        if offset is not None:
            q.append("offset=%d" % offset)
        if limit is not None:
            q.append("limit=%d" % limit)
        return self._call("GET", "/v1/batches" + ("?" + "&".join(q) if q else ""))

    def results(self, batch_id):
        text, _, _ = self.c._exchange("GET", "/v1/batches/%s/results" % batch_id, raw=True)
        return [BatchResult(json.loads(line)) for line in text.split("\n") if line.strip()]

    def cancel(self, batch_id):
        return self._call("POST", "/v1/batches/%s/cancel" % batch_id)


def run_batch(self, requests, poll_interval=60.0, timeout=26 * 3600.0):
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or poll_interval < MIN_POLL_INTERVAL:
        raise ValueError("poll_interval must be at least %s seconds" % MIN_POLL_INTERVAL)
    draft = self.batches.create([])
    try:
        if requests:
            self.batches.add(draft["id"], requests)
    except Exception:
        try:
            self.batches.cancel(draft["id"])
        finally:
            raise
    batch = self.batches.finalize(draft["id"])
    waited = 0.0
    while batch.get("ended_at") is None:
        if waited >= timeout:
            raise TimeoutError("the batch %s did not end within %ss" % (batch["id"], timeout))
        wait = poll_interval
        batch_headers = getattr(self, "_last_batch_headers", None) or {}
        server = retry_wait(batch_headers)
        if server is not None:
            wait = max(wait, server)
        self._sleep(wait)
        waited += wait
        batch, self._last_batch_headers = self.batches.get_with_headers(batch["id"])
    self._last_batch_headers = None
    return batch, self.batches.results(batch["id"])


DecisionNode.run_batch = run_batch


# =========================================================================== spec 1.3
#
# Boosters: the request option, the evidence that comes back, the header on errors.
# Fine-tuned models need nothing here: a model name is free text, sent as given.


class Booster:
    """Booster(name, images, require, options): a booster in its object form,
    {"name", "images"?, "require"?, "options"?}, an argument not given left out. With
    no option given it is sent as the bare name, the plain list's form."""

    def __init__(self, name, images=None, require=None, options=None):
        self.name = name
        self.images = images
        self.require = require
        self.options = options

    def wire(self):
        # With no option set the wire form is the bare name, so a
        # list of such boosters is the plain list ["age", "ai_generated"].
        if self.images is None and self.require is None and self.options is None:
            return self.name
        out = {"name": self.name}
        if self.images is not None:
            out["images"] = list(self.images)
        if self.require is not None:
            out["require"] = self.require
        if self.options is not None:
            out["options"] = dict(self.options)
        return out


def booster_wires(boosters) -> list:
    """Names and plain wire-form maps pass as given; nothing is checked locally."""
    return [b.wire() if isinstance(b, Booster) else b for b in boosters]


def parse_boosters(value) -> dict:
    """x-decisionnode-boosters (`age=ok:212,ai_generated=timeout:800`): booster name to
    {status, latency_ms}, in header order; empty when the header is absent."""
    out = {}
    for member in (value or "").split(","):
        name, sep, rest = member.strip().partition("=")
        if not sep or not name:
            continue
        status, colon, ms = rest.partition(":")
        latency = int(ms) if colon and ms.isdigit() else None
        out[name] = {"status": status, "latency_ms": latency}
    return out


class Reading:
    kind = "unknown"
    FIELDS: tuple = ()

    def __init__(self, raw):
        self.raw = raw
        for name in self.FIELDS:
            setattr(self, name, raw.get(name))


class UnknownReading(Reading):
    """A reading of a booster this client does not know, or one with a field missing or
    of the wrong type: its raw fields, and the decision does not fail."""


class AgeReading(Reading):
    kind = "age"
    FIELDS = ("image", "faces", "age_years", "low", "high", "confidence")


class AiGeneratedReading(Reading):
    kind = "ai_generated"
    FIELDS = ("image", "generated", "confidence", "method", "top_generator", "c2pa_signer", "c2pa")


class OcrReading(Reading):
    """The text a picture shows (or the answer to the prompt), and
    truncated, true when the evidence was cut at the context limit. Both are always sent."""

    kind = "ocr"
    FIELDS = ("image", "text", "truncated")


class FaceBox:
    """A face's box: its corners, in pixels of the image as sent, or fractions when every
    place question of the request asks for normalized units."""

    def __init__(self, raw: dict):
        self.x1, self.y1, self.x2, self.y2 = raw["x1"], raw["y1"], raw["x2"], raw["y2"]


class FaceDetection:
    def __init__(self, raw: dict):
        self.box = FaceBox(raw["box"])
        self.confidence = raw["confidence"]


class FaceMatch:
    """A face matched in a collection: its box, the score, the
    collection searched, the item's reference, and name (a public figure) or label (an
    item of the workspace's own collection that has one), empty when absent. From 1.16
    collection_name (an own collection's display name, or its id; a public collection's
    id) and metadata (an own item's, when it has any), each empty when absent, as from
    an answer from before these fields."""

    def __init__(self, raw: dict):
        self.box = FaceBox(raw["box"])
        self.score = raw["score"]
        self.collection = raw["collection"]
        self.reference = raw["reference"]
        self.name = raw.get("name")
        self.label = raw.get("label")
        self.collection_name = raw.get("collection_name")
        self.metadata = raw.get("metadata")


class FaceDetectionReading(Reading):
    """Faces, the count, and detections, each face's box and confidence."""

    kind = "face_detection"
    FIELDS = ("image", "faces", "detections")

    def __init__(self, raw):
        super().__init__(raw)
        self.detections = [FaceDetection(d) for d in raw["detections"]]


class FaceRecognitionReading(Reading):
    """Faces, the faces searched, and matches in the collection, by
    score. It replaces wanted_person, which was never released."""

    kind = "face_recognition"
    FIELDS = ("image", "faces", "matches")

    def __init__(self, raw):
        super().__init__(raw)
        self.matches = [FaceMatch(m) for m in raw["matches"]]
        # From 1.16: an own collection's display name, empty when absent (a public
        # collection, or an answer from before these fields).
        self.collection_name = raw.get("collection_name")


class DepthReading(Reading):
    """Depth Vision (bynn_depth, from 1.21): meters to what a picture shows. ahead (the
    nearest obstacle straight ahead, the floor left out) and each of the seven
    directions, left to right, are empty when none is found; ground says whether a floor
    was found and left out; grid is grid_rows by grid_cols, row by row from the top left.
    Every number as sent. Estimates from one picture, for decisions and planning, never a
    safety stop."""

    kind = "bynn_depth"
    FIELDS = ("image", "ahead", "directions", "ground", "grid_rows", "grid_cols", "grid", "depth_min", "depth_max", "depth_median", "width", "height")


class SegmentOpening:
    """The largest hole a match encloses: its outline and centre ([x, y] fractions from
    the top left), its width and height (fractions of the picture's), its offset ([dx,
    dy], its centroid less the centre of the match's box) and its share of the picture."""

    def __init__(self, raw: dict):
        self.polygon, self.centroid, self.width, self.height, self.offset, self.area = (raw[k] for k in ("polygon", "centroid", "width", "height", "offset", "area"))


class SegmentDepth:
    """A match's median distance, the median seen through its opening and that of the
    opening's rim, in meters, each empty when none was read."""

    def __init__(self, raw: dict):
        self.median, self.opening_median, self.opening_rim_median = (raw.get(k) for k in ("median", "opening_median", "opening_rim_median"))


class SegmentSize:
    """A match's width and height in meters, and its opening's ({width, height}, empty
    when it has none)."""

    def __init__(self, raw: dict):
        self.width, self.height = raw["width"], raw["height"]
        self.opening = None if raw.get("opening") is None else {"width": raw["opening"]["width"], "height": raw["opening"]["height"]}


class SegmentInstance:
    """One match: the phrase it matched (label), its score, box ([x1, y1, x2, y2]),
    outline ([x, y] points), area and centroid, as fractions of the picture from the top
    left; its opening, depth and size_m, each empty when null or absent."""

    def __init__(self, raw: dict):
        self.label, self.score, self.box, self.polygon, self.area, self.centroid = (raw[k] for k in ("label", "score", "box", "polygon", "area", "centroid"))
        self.opening = None if raw.get("opening") is None else SegmentOpening(raw["opening"])
        self.depth = None if raw.get("depth") is None else SegmentDepth(raw["depth"])
        self.size_m = None if raw.get("size_m") is None else SegmentSize(raw["size_m"])


class SegmentReading(Reading):
    """Segment Vision (bynn_segment, from 1.21): what the prompt names, found in a
    picture. prompts are the phrases as read; instances the matches, in the order sent
    (possibly none); depth_source (bynn_depth, empty with depth off) and assumed_hfov_deg
    are empty when null or absent. Distances and sizes are estimates from one picture,
    for decisions and planning, never a safety stop."""

    kind = "bynn_segment"
    FIELDS = ("image", "width", "height", "prompts", "depth_source", "assumed_hfov_deg")

    def __init__(self, raw):
        super().__init__(raw)
        self.instances = [SegmentInstance(i) for i in raw["instances"]]


def _whole(v) -> bool:
    return _int(v) and v >= 0


def _numbers(v, n: int) -> bool:
    return isinstance(v, list) and len(v) == n and all(_number(x) for x in v)


def _points(v) -> bool:
    return isinstance(v, list) and all(_numbers(p, 2) for p in v)


def _nullable(v, ok) -> bool:
    return v is None or ok(v)


def _depth_ok(raw: dict) -> bool:
    """Every field but ahead required; directions exactly seven, each a number or null;
    grid a list of rows of numbers. {"image", "readable": false} is a generic reading."""
    directions, grid = raw.get("directions"), raw.get("grid")
    return (
        isinstance(raw.get("image"), str)
        and _nullable(raw.get("ahead"), _number)
        and isinstance(directions, list)
        and len(directions) == 7
        and all(_nullable(d, _number) for d in directions)
        and isinstance(raw.get("ground"), bool)
        and _whole(raw.get("grid_rows"))
        and _whole(raw.get("grid_cols"))
        and isinstance(grid, list)
        and all(isinstance(row, list) and all(_number(c) for c in row) for row in grid)
        and all(_number(raw.get(k)) for k in ("depth_min", "depth_max", "depth_median"))
        and _whole(raw.get("width"))
        and _whole(raw.get("height"))
    )


def _opening_ok(o) -> bool:
    return (
        isinstance(o, dict)
        and _points(o.get("polygon"))
        and _numbers(o.get("centroid"), 2)
        and all(_number(o.get(k)) for k in ("width", "height", "area"))
        and _numbers(o.get("offset"), 2)
    )


def _extent_ok(e) -> bool:
    return isinstance(e, dict) and _number(e.get("width")) and _number(e.get("height"))


def _instance_ok(i) -> bool:
    """A match of the wrong shape makes the whole reading generic."""
    return (
        isinstance(i, dict)
        and isinstance(i.get("label"), str)
        and _number(i.get("score"))
        and _numbers(i.get("box"), 4)
        and _points(i.get("polygon"))
        and _number(i.get("area"))
        and _numbers(i.get("centroid"), 2)
        and _nullable(i.get("opening"), _opening_ok)
        and _nullable(i.get("depth"), lambda d: isinstance(d, dict) and all(_nullable(d.get(k), _number) for k in ("median", "opening_median", "opening_rim_median")))
        and _nullable(i.get("size_m"), lambda s: _extent_ok(s) and _nullable(s.get("opening"), _extent_ok))
    )


def _segment_ok(raw: dict) -> bool:
    prompts, instances = raw.get("prompts"), raw.get("instances")
    return (
        isinstance(raw.get("image"), str)
        and _whole(raw.get("width"))
        and _whole(raw.get("height"))
        and isinstance(prompts, list)
        and all(isinstance(p, str) for p in prompts)
        and isinstance(instances, list)
        and all(_instance_ok(i) for i in instances)
        and _nullable(raw.get("depth_source"), lambda v: isinstance(v, str))
        and _nullable(raw.get("assumed_hfov_deg"), _number)
    )


def _box_ok(box) -> bool:
    return isinstance(box, dict) and all(_number(box.get(k)) for k in ("x1", "y1", "x2", "y2"))


def _face_detection_ok(raw: dict) -> bool:
    if not isinstance(raw.get("image"), str) or not _int(raw.get("faces")) or raw["faces"] < 0:
        return False
    found = raw.get("detections")
    return isinstance(found, list) and all(isinstance(d, dict) and _box_ok(d.get("box")) and _number(d.get("confidence")) for d in found)


def _face_recognition_ok(raw: dict) -> bool:
    if not isinstance(raw.get("image"), str) or not _int(raw.get("faces")) or raw["faces"] < 0:
        return False
    if raw.get("collection_name") is not None and not isinstance(raw["collection_name"], str):
        return False
    found = raw.get("matches")
    return isinstance(found, list) and all(
        isinstance(m, dict)
        and _box_ok(m.get("box"))
        and _number(m.get("score"))
        and isinstance(m.get("collection"), str)
        and isinstance(m.get("reference"), str)
        and all(m.get(k) is None or isinstance(m[k], str) for k in ("name", "label", "collection_name"))
        and (m.get("metadata") is None or isinstance(m["metadata"], dict))
        for m in found
    )


def _ocr_ok(raw: dict) -> bool:
    return isinstance(raw.get("image"), str) and isinstance(raw.get("text"), str) and isinstance(raw.get("truncated"), bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _age_ok(raw: dict) -> bool:
    if not isinstance(raw.get("image"), str) or not _int(raw.get("faces")) or raw["faces"] < 0:
        return False
    face = raw["faces"] > 0
    for name, ok in (("age_years", _int), ("low", _int), ("high", _int), ("confidence", _number)):
        value = raw.get(name)
        if value is None:
            if face:
                return False
        elif not ok(value):
            return False
    return True


def _ai_generated_ok(raw: dict) -> bool:
    """The API always writes top_generator (null unless the picture reads as likely
    generated, and on every c2pa reading) and c2pa_signer (null unless the file's
    Content Credentials are trusted); c2pa is there only as "untrusted" (any other
    string kept as received). Fields the client does not know are ignored."""
    top = raw.get("top_generator")
    return (
        isinstance(raw.get("image"), str)
        and _number(raw.get("generated"))
        and _number(raw.get("confidence"))
        and isinstance(raw.get("method"), str)
        and "top_generator" in raw
        and (top is None or (isinstance(top, dict) and isinstance(top.get("name"), str) and _number(top.get("probability"))))
        and "c2pa_signer" in raw
        and (raw["c2pa_signer"] is None or isinstance(raw["c2pa_signer"], str))
        and (raw.get("c2pa") is None or isinstance(raw["c2pa"], str))
    )


READINGS = {
    "age": (AgeReading, _age_ok),
    "ai_generated": (AiGeneratedReading, _ai_generated_ok),
    "bynn_depth": (DepthReading, _depth_ok),
    "bynn_segment": (SegmentReading, _segment_ok),
    "face_detection": (FaceDetectionReading, _face_detection_ok),
    "face_recognition": (FaceRecognitionReading, _face_recognition_ok),
    "ocr": (OcrReading, _ocr_ok),
}


def parse_reading(booster, raw) -> Reading:
    if not isinstance(raw, dict):
        return UnknownReading({"value": raw})
    cls, ok = READINGS.get(booster, (None, None))
    if cls is not None and ok(raw):
        return cls(raw)
    return UnknownReading(raw)


class Cost:
    """usage.cost: usd, model_usd and boosters_usd in USD, and
    free_tier. Read as sent: usd is the server's, never model_usd plus boosters_usd."""

    def __init__(self, raw):
        self.raw = raw
        self.usd = raw.get("usd")
        self.model_usd = raw.get("model_usd")
        self.boosters_usd = raw.get("boosters_usd")
        self.free_tier = raw.get("free_tier")


class Evidence:
    """One booster's entry of a decision's evidence. status is kept as received."""

    def __init__(self, booster, raw):
        raw = raw if isinstance(raw, dict) else {}
        self.booster = booster
        self.raw = raw
        self.version = raw.get("version")
        self.status = raw.get("status")
        self.latency_ms = raw.get("latency_ms")
        # Spec 1.3: the calls of this booster billed on this request; empty when absent.
        self.billed_calls = raw.get("billed_calls")
        results = raw.get("results")
        self.results = [parse_reading(booster, r) for r in results] if isinstance(results, list) else []


# =========================================================================== spec 1.4
#
# Knowledge: collections of type text, their documents (text inline, files by a knowledge
# upload), waiting for them, search, the knowledge booster and its reading.

SPEC_VERSION = "1.4"  # noqa: F811 - spec 1.4 adds knowledge
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KNOWLEDGE_TYPES = ("text/plain", "text/markdown", "application/pdf", DOCX, PPTX, XLSX, "text/html", "image/png", "image/jpeg", "image/tiff", "image/webp")
KNOWLEDGE_EXTENSIONS = (
    (".txt", "text/plain"), (".md", "text/markdown"), (".markdown", "text/markdown"), (".pdf", "application/pdf"),
    (".docx", DOCX), (".pptx", PPTX), (".xlsx", XLSX), (".html", "text/html"), (".htm", "text/html"),
    (".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".tif", "image/tiff"), (".tiff", "image/tiff"), (".webp", "image/webp"),
)
MAX_KNOWLEDGE_FILE_BYTES = 52428800  # a knowledge file, 50 MB
KNOWLEDGE_UPLOAD_TIMEOUT = 120.0  # one PUT of a knowledge file, unless upload_timeout is set
WAIT_TIMEOUT = 20.0  # one attempt of a write with ?wait=true, which the server holds up to 10 s
TITLE_MAX, PIN_RANK_MAX = 200, 1000
SCOPE_KEY_PATTERN = r"^[a-z0-9_]{1,40}$"
READY_POLL_INTERVAL, READY_POLL_MIN, READY_POLL_MAX, READY_TIMEOUT = 1.0, 0.1, 10.0, 300.0


class Document:
    """One text item's content: text (Markdown by default, or plain), a file by path,
    bytes or a stream, or a knowledge upload made before. A file is read when the call
    is made; a stream is read to its end once, since the presign needs its size and
    checksums first."""

    def __init__(self, text=None, format=None, path=None, data=None, stream=None, content_type=None, filename=None, upload_id=None):  # noqa: A002 - the API's field
        given = [x for x in (text, path, data, stream, upload_id) if x is not None]
        if len(given) != 1:
            raise ValueError("a Document is one of text, path, data, stream or upload_id")
        if text is not None and not isinstance(text, str):
            raise ValueError("text must be a string")
        if format is not None and text is None:
            raise ValueError("format goes with text; a file's format follows its type")
        self.text, self.format, self.path, self.data, self.stream = text, format, path, data, stream
        self.content_type, self.upload_id = content_type, upload_id
        self.filename = filename if filename is not None else (os.path.basename(path) if path is not None else None)

    def read(self):
        """(bytes, content type or None) of a file document."""
        if self.path is not None:
            with open(self.path, "rb") as fh:
                data = fh.read(MAX_KNOWLEDGE_FILE_BYTES + 1)
        elif self.stream is not None:
            data = self.stream.read(MAX_KNOWLEDGE_FILE_BYTES + 1)
        else:
            data = self.data
        if isinstance(data, str):
            data = data.encode("utf-8")
        return bytes(data), self.content_type or knowledge_type_of(self.filename, data)


def knowledge_type_of(filename, data):
    """The type a file name's extension names, else the first bytes'; None when neither
    tells (text and the ZIP containers need content_type or a file name)."""
    if filename:
        lower = filename.lower()
        for ext, ctype in KNOWLEDGE_EXTENSIONS:
            if lower.endswith(ext):
                return ctype
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return sniff_media_type(data)


def knowledge(collection, query=None, scope=None, match=None, filter=None, top_k=None, max_tokens=None, pinned=None, min_score=None, include_text=None, require=None) -> dict:  # noqa: A002
    """The knowledge booster's object: collection required, every other option only when
    given, require only when true. Never the bare name, which the API refuses."""
    options = {"collection": collection}
    for name, value in (("query", query), ("scope", scope), ("match", match), ("filter", filter), ("top_k", top_k), ("max_tokens", max_tokens),
                        ("pinned", pinned), ("min_score", min_score), ("include_text", include_text)):
        if value is not None:
            options[name] = value
    b = {"name": "knowledge"}
    if require:
        b["require"] = True
    b["options"] = options
    return b


def _title_entries(title) -> list:
    """A text item's title as the API judges it: 1 to 200 characters once trimmed."""
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= TITLE_MAX:
        return [_entry("string_too_long", ["body", "title"], "A title is 1 to 200 characters", _echoed(title))]
    return []


def _item_entries(reference, pinned, pin_rank, label, metadata) -> list:
    """The fields every text write shares, as every collection item's (suite 1.15), and
    pin_rank."""
    entries = []
    if reference is not None:
        entries.extend(_string_entries(["body", "reference"], reference, ITEM_REFERENCE_MAX, ITEM_REFERENCE_PATTERN))
    if pin_rank is not None and (not _is_int(pin_rank) or not 0 <= pin_rank <= PIN_RANK_MAX):
        entries.append(_entry("int_range", ["body", "pin_rank"], "Input should be an integer from 0 to 1000", _echoed(pin_rank)))
    if label is not None:
        entries.extend(_string_entries(["body", "label"], label, ITEM_LABEL_MAX))
    if metadata is not None:
        entries.extend(_metadata_entries(metadata))
    return entries


def _stem(filename):
    return filename.rsplit(".", 1)[0] if filename and "." in filename.lstrip(".") else filename


def _prepare_document(self, document, title):
    """What a document sends: ("text", text, format, title) inline, or ("upload", bytes,
    content type, filename) for a file to upload, or ("upload_id", id), after its local
    checks."""
    if not isinstance(document, Document):
        raise ValueError("document must be a Document")
    entries = []
    if document.upload_id is not None:
        if title is not None:
            entries.extend(_title_entries(title))
        if entries:
            raise local_422(entries)
        return ("upload_id", document.upload_id)
    if document.text is not None:
        if title is None:
            entries.append(_entry("missing", ["body", "title"], "Field required", None))
        else:
            entries.extend(_title_entries(title))
        if document.text == "":
            entries.append(_entry("string_too_short", ["body", "text"], "The text is empty", ""))
        if document.format is not None and document.format not in ("markdown", "text"):
            entries.append(_entry("literal_error", ["body", "format"], "Input should be 'markdown' or 'text'", _echoed(document.format)))
        if entries:
            raise local_422(entries)
        return ("text", document.text, document.format, title)
    data, ctype = document.read()
    if ctype is None:
        raise ValueError("the document's type cannot be told from its name or its first bytes: give content_type or filename")
    if ctype not in KNOWLEDGE_TYPES:
        raise local_error(BadRequestError, "files[0].content_type: must be one of %s, got %s." % (", ".join(KNOWLEDGE_TYPES), ctype))
    if not data:
        raise local_error(BadRequestError, "files[0].byte_size: must be an integer from 1 to %d." % MAX_KNOWLEDGE_FILE_BYTES)
    if len(data) > MAX_KNOWLEDGE_FILE_BYTES:
        raise local_error(BadRequestError, "files[0].byte_size: %d is over the limit of %d bytes." % (len(data), MAX_KNOWLEDGE_FILE_BYTES))
    if ctype in ("text/plain", "text/markdown") and len(data) < self.inline_threshold:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and "\x00" not in text:
            # A small text or Markdown file goes inline: one call instead of three.
            title = title if title is not None else _stem(document.filename)
            if title is None:
                entries.append(_entry("missing", ["body", "title"], "Field required", None))
            else:
                entries.extend(_title_entries(title))
            if entries:
                raise local_422(entries)
            return ("text", text, "markdown" if ctype == "text/markdown" else "text", title)
    if title is not None:
        entries.extend(_title_entries(title))
    if entries:
        raise local_422(entries)
    return ("upload", data, ctype, document.filename)


def _upload_documents(self, files):
    """One presign of purpose knowledge for the files ((bytes, content type, filename)),
    one PUT each (120 s unless upload_timeout is set), and their upload ids."""

    def presign(group):
        wire = []
        for data, ctype, filename in group:
            f = {
                "purpose": "knowledge",
                "content_type": ctype,
                "byte_size": len(data),
                "checksum": base64.b64encode(hashlib.md5(data).digest()).decode("ascii"),
                "sha256": base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
            }
            if filename:
                f["filename"] = filename
            wire.append(f)
        body, _, _ = self._exchange("POST", "/v1/uploads", {"files": wire})
        return body.get("uploads") or [], self._clock() + float(body.get("expires_in") or 300)

    uploads, urls_expire = presign(files)
    ids = []
    timeout = self.upload_timeout if self._upload_timeout_given else KNOWLEDGE_UPLOAD_TIMEOUT
    for f, up in zip(files, uploads):
        try:
            self._put(up, f[0], timeout)
        except DecisionNodeError as exc:
            if exc.status != 403 or self._clock() < urls_expire:
                raise
            (up,), _ = presign([f])
            self._put(up, f[0], timeout)
        ids.append(up["upload_id"])
    return ids


def upload_documents(self, documents):
    """Spec 1.4: uploads 1 to 16 documents whatever their type and size (a text document
    as text/markdown or text/plain by its format) and returns one upload id each."""
    documents = list(documents)
    if not 1 <= len(documents) <= MAX_UPLOAD_FILES:
        raise local_error(BadRequestError, "files: 1 to %d files, got %d." % (MAX_UPLOAD_FILES, len(documents)))
    files = []
    for i, d in enumerate(documents):
        if not isinstance(d, Document) or d.upload_id is not None:
            raise ValueError("documents[%d] must be a text or file Document" % i)
        if d.text is not None:
            files.append((d.text.encode("utf-8"), "text/plain" if d.format == "text" else "text/markdown", d.filename))
            continue
        data, ctype = d.read()
        if ctype is None:
            raise ValueError("documents[%d]: its type cannot be told from its name or its first bytes: give content_type or filename" % i)
        if ctype not in KNOWLEDGE_TYPES:
            raise local_error(BadRequestError, "files[%d].content_type: must be one of %s, got %s." % (i, ", ".join(KNOWLEDGE_TYPES), ctype))
        if not data:
            raise local_error(BadRequestError, "files[%d].byte_size: must be an integer from 1 to %d." % (i, MAX_KNOWLEDGE_FILE_BYTES))
        if len(data) > MAX_KNOWLEDGE_FILE_BYTES:
            raise local_error(BadRequestError, "files[%d].byte_size: %d is over the limit of %d bytes." % (i, len(data), MAX_KNOWLEDGE_FILE_BYTES))
        files.append((data, ctype, d.filename))
    return _upload_documents(self, files)


def _put_with_timeout(self, up, data, timeout=None):
    """The PUT of an upload, with a knowledge file's own timeout."""
    if timeout is None:
        return _put_image(self, up, data)
    kept = self.upload_timeout
    self.upload_timeout = timeout
    try:
        return _put_image(self, up, data)
    finally:
        self.upload_timeout = kept


_put_image = _put
DecisionNode._put = _put_with_timeout
DecisionNode.upload_documents = upload_documents
_init_1_3 = DecisionNode.__init__


def _init(self, *args, **kwargs):
    _init_1_3(self, *args, **kwargs)
    self._upload_timeout_given = kwargs.get("upload_timeout") is not None or len(args) > 8


DecisionNode.__init__ = _init


def _write_document(self, method, path, document, title, reference, pinned, pin_rank, label, metadata, wait):
    """POST or PUT of a text item: the local checks, the upload when the document needs
    one, then the call with its own Idempotency-Key, the same on every attempt. A stale
    upload this call placed is uploaded again once, the call sent once more with a new
    key (its body changed)."""
    c = self.c
    entries = _item_entries(reference, pinned, pin_rank, label, metadata)
    if pinned is not None and not isinstance(pinned, bool):
        entries.append(_entry("bool_type", ["body", "pinned"], "Input should be a valid boolean", _echoed(pinned)))
    try:
        what = _prepare_document(c, document, title)
    except ValidationError as exc:
        raise local_422(list(exc.entries) + entries)
    if entries:
        raise local_422(entries)
    query = "?wait=true" if wait else ""
    timeout = None if c._timeout_given or not wait else WAIT_TIMEOUT
    for attempt in (0, 1):
        body = {}
        if what[0] == "text":
            body["title"] = what[3]
            body["text"] = what[1]
            if what[2] is not None:
                body["format"] = what[2]
        else:
            if title is not None:
                body["title"] = title
            body["upload_id"] = what[1] if what[0] == "upload_id" else _upload_documents(c, [what[1:]])[0]
        for name, value in (("reference", reference), ("pinned", pinned), ("pin_rank", pin_rank), ("label", label), ("metadata", metadata)):
            if value is not None:
                body[name] = value
        try:
            parsed, _, _ = c._exchange(method, path + query, body, policy="idempotent", timeout=timeout, extra_headers={"Idempotency-Key": str(uuid.uuid4())})
            return parsed
        except BadRequestError as exc:
            if attempt == 0 and what[0] == "upload" and exc.error_type in STALE_UPLOAD:
                continue
            raise


def add_document(self, collection_id, document, title=None, reference=None, pinned=None, pin_rank=None, label=None, metadata=None, wait=False):
    """Spec 1.4: POST /v1/collections/{id}/items on a text collection."""
    return _write_document(self, "POST", "/v1/collections/%s/items" % self._id(collection_id), document, title, reference, pinned, pin_rank, label, metadata, wait)


def replace_document(self, collection_id, item_id, document, title=None, reference=None, pinned=None, pin_rank=None, label=None, metadata=None, wait=False):
    """Spec 1.4: PUT /v1/collections/{id}/items/{item_id}, the item replaced whole."""
    path = "/v1/collections/%s/items/%s" % (self._id(collection_id), self._id(item_id))
    return _write_document(self, "PUT", path, document, title, reference, pinned, pin_rank, label, metadata, wait)


def _create(self, name, type="face", scope_key=None):  # noqa: A002 - the API's field
    """POST /v1/collections: face (the default) or, from spec 1.4, text with an optional
    scope_key."""
    entries = []
    if type not in ("face", "text"):
        entries.append(_entry("literal_error", ["body", "type"], "Input should be 'face' or 'text'", _echoed(type), {"expected": "'face' or 'text'"}))
    entries.extend(_string_entries(["body", "name"], name, COLLECTION_NAME_MAX))
    if scope_key is not None and not (isinstance(scope_key, str) and re.fullmatch(SCOPE_KEY_PATTERN, scope_key)):
        entries.append(_entry("string_pattern_mismatch", ["body", "scope_key"], "String should match pattern '%s'" % SCOPE_KEY_PATTERN, _echoed(scope_key)))
    if entries:
        raise local_422(entries)
    body = {"type": type, "name": name}
    if scope_key is not None:
        body["scope_key"] = scope_key
    return self._call("POST", "/v1/collections", body, "idempotent", str(uuid.uuid4()))


_update_item_1_3 = Collections.update_item


def _update_item(self, collection_id, item_id, reference=_UNSET, label=_UNSET, metadata=_UNSET, pinned=_UNSET, pin_rank=_UNSET, title=_UNSET):
    """PATCH an item: from spec 1.4 a text item's pinned, pin_rank and title too (a new
    title is a new version, 202)."""
    if pinned is _UNSET and pin_rank is _UNSET and title is _UNSET:
        return _update_item_1_3(self, collection_id, item_id, reference, label, metadata)
    entries, body = [], {}
    if reference is not _UNSET:
        entries.extend(_string_entries(["body", "reference"], reference, ITEM_REFERENCE_MAX, ITEM_REFERENCE_PATTERN))
        body["reference"] = reference
    if label is not _UNSET:
        if label is not None:
            entries.extend(_string_entries(["body", "label"], label, ITEM_LABEL_MAX))
        body["label"] = label
    if metadata is not _UNSET:
        entries.extend(_metadata_entries(metadata))
        body["metadata"] = metadata
    if pinned is not _UNSET:
        if not isinstance(pinned, bool):
            entries.append(_entry("bool_type", ["body", "pinned"], "Input should be a valid boolean", _echoed(pinned)))
        body["pinned"] = pinned
    if pin_rank is not _UNSET:
        if not _is_int(pin_rank) or not 0 <= pin_rank <= PIN_RANK_MAX:
            entries.append(_entry("int_range", ["body", "pin_rank"], "Input should be an integer from 0 to 1000", _echoed(pin_rank)))
        body["pin_rank"] = pin_rank
    if title is not _UNSET:
        entries.extend(_title_entries(title))
        body["title"] = title
    if entries:
        raise local_422(entries)
    path = "/v1/collections/%s/items/%s" % (self._id(collection_id), self._id(item_id))
    return self._call("PATCH", path, body, "idempotent", str(uuid.uuid4()))


def _paging(offset, limit):
    if offset is not None and (not _is_int(offset) or offset < 0):
        raise local_error(BadRequestError, "Query parameter offset must be an integer of 0 or more, got %s." % (offset,))
    if limit is not None and (not _is_int(limit) or not 1 <= limit <= LIST_LIMIT_MAX):
        raise local_error(BadRequestError, "Query parameter limit must be an integer from 1 to %d, got %s." % (LIST_LIMIT_MAX, limit))
    q = []
    if offset is not None:
        q.append("offset=%d" % offset)
    if limit is not None:
        q.append("limit=%d" % limit)
    return "?" + "&".join(q) if q else ""


def get_item(self, collection_id, item_id, include_text=False):
    """Spec 1.4: one item; with include_text a text item's stored text."""
    path = "/v1/collections/%s/items/%s" % (self._id(collection_id), self._id(item_id))
    return self._call("GET", path + ("?include=text" if include_text else ""))


def list_chunks(self, collection_id, item_id, offset=None, limit=None):
    """Spec 1.4: a text item's live chunks, paged; never vectors."""
    path = "/v1/collections/%s/items/%s/chunks" % (self._id(collection_id), self._id(item_id))
    return self._call("GET", path + _paging(offset, limit))


def search(self, collection_id, query, scope=None, match=None, filter=None, top_k=None, pinned=None, max_tokens=None):  # noqa: A002
    """Spec 1.4: POST /v1/collections/{id}/search, each argument sent only when given,
    as given; nothing checked locally. A KnowledgeReading with every passage's text."""
    body = {"query": query}
    for name, value in (("scope", scope), ("match", match), ("filter", filter), ("top_k", top_k), ("pinned", pinned), ("max_tokens", max_tokens)):
        if value is not None:
            body[name] = value
    return parse_reading("knowledge", self._call("POST", "/v1/collections/%s/search" % self._id(collection_id), body))


def wait_until_ready(self, collection_id, item_id=None, timeout=READY_TIMEOUT, poll_interval=READY_POLL_INTERVAL):
    """Spec 1.4: reads the item until it is ready or failed (and returns it, a failed one
    as a value), or the collection until nothing is queued or processing (and returns
    it). The first read at once, then poll_interval, times 1.5 after each read, up to
    10 s; past timeout the timeout error."""
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or poll_interval < READY_POLL_MIN:
        raise ValueError("poll_interval must be at least %s s, got %r" % (READY_POLL_MIN, poll_interval))
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
        raise ValueError("timeout must be a number of seconds above 0, got %r" % (timeout,))
    started = time.monotonic()
    waited = 0.0
    interval = float(poll_interval)
    while True:
        if item_id is not None:
            got = self.get_item(collection_id, item_id)
            if isinstance(got, dict) and got.get("status") in ("ready", "failed"):
                return got
        else:
            got = self.get(collection_id)
            counts = got.get("status_counts") if isinstance(got, dict) else None
            counts = counts if isinstance(counts, dict) else {}
            if not counts.get("queued") and not counts.get("processing"):
                return got
        spent = max(time.monotonic() - started, waited)
        if spent >= timeout:
            raise TimeoutError("not ready after %ss" % timeout)
        wait = min(interval, timeout - spent)
        self.c._sleep(wait)
        waited += wait
        interval = min(READY_POLL_MAX, interval * 1.5)


Collections.create = _create
Collections.update_item = _update_item
Collections.add_document = add_document
Collections.replace_document = replace_document
Collections.get_item = get_item
Collections.list_chunks = list_chunks
Collections.search = search
Collections.wait_until_ready = wait_until_ready


class KnowledgePassage:
    """One passage of a knowledge reading, in the block's order (pinned first): its
    citation (item, chunk, UTF-8 byte offsets into the item's stored text, end
    exclusive) and what the reading says of it, as sent."""

    FIELDS = ("n", "pinned", "item", "reference", "title", "heading", "chunk", "byte_start", "byte_end", "tokens", "score", "lexical", "rendered", "text")

    def __init__(self, raw):
        self.raw = raw
        for name in self.FIELDS:
            setattr(self, name, raw.get(name))


class KnowledgeReading(Reading):
    """Spec 1.4: the knowledge booster's one reading, and the search route's answer. A
    search's answer has no truncated and its passages no rendered: those are empty."""

    kind = "knowledge"
    FIELDS = ("collection", "collection_name", "corpus", "query", "budget", "scope", "warnings", "passages", "omitted", "pending_items", "search", "truncated")

    def __init__(self, raw):
        super().__init__(raw)
        self.warnings = list(raw.get("warnings") or [])
        self.passages = [KnowledgePassage(p) for p in raw["passages"]]


def _opt(raw, key, kinds) -> bool:
    return raw.get(key) is None or (isinstance(raw[key], kinds) and not (bool not in kinds and isinstance(raw[key], bool)))


def _passage_ok(p) -> bool:
    return (
        isinstance(p, dict)
        and _is_int(p.get("n")) and _is_int(p.get("chunk")) and _is_int(p.get("byte_start")) and _is_int(p.get("byte_end"))
        and isinstance(p.get("item"), str) and isinstance(p.get("pinned"), bool)
        and all(_opt(p, k, (str,)) for k in ("reference", "title", "heading", "text"))
        and _opt(p, "score", (int, float)) and _opt(p, "tokens", (int,))
        and _opt(p, "lexical", (bool,)) and _opt(p, "rendered", (bool,))
    )


def _knowledge_ok(raw) -> bool:
    return (
        isinstance(raw.get("collection"), str)
        and isinstance(raw.get("passages"), list)
        and all(_passage_ok(p) for p in raw["passages"])
        and _opt(raw, "collection_name", (str,))
        and _opt(raw, "truncated", (bool,))
        and (raw.get("warnings") is None or (isinstance(raw["warnings"], list) and all(isinstance(w, str) for w in raw["warnings"])))
        and all(_opt(raw, k, (dict,)) for k in ("corpus", "query", "budget", "scope", "omitted", "search"))
    )


READINGS["knowledge"] = (KnowledgeReading, _knowledge_ok)
