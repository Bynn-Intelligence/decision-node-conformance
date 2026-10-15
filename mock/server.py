#!/usr/bin/env python3
"""DecisionNode mock API server for SDK conformance testing.

Standard library only, Python 3.10 or later.

    python3 mock/server.py --port 0

The first line on stdout is "listening on http://127.0.0.1:<port>".

Endpoints of the API:

    POST   /v1/decide                     the decision call, validated like the hosted API
    GET    /v1/models                     the model list with each model's limits
    POST   /v1/uploads                    presign 1 to 16 image uploads (URLs on this mock)
    POST   /v1/sessions                   open a session
    GET    /v1/sessions/{id}/stream       the session's WebSocket (RFC 6455, written here)
    DELETE /v1/sessions/{id}              end a session
    POST   /v1/batches                    create a batch (JSON Lines or a JSON array)
    GET    /v1/batches                    list the key's batches
    GET    /v1/batches/{id}               retrieve a batch (each retrieve moves its lifecycle)
    POST   /v1/batches/{id}/requests      add requests to a draft
    POST   /v1/batches/{id}/finalize      queue a draft
    GET    /v1/batches/{id}/results       the results, JSON Lines or one JSON page
    POST   /v1/batches/{id}/cancel        cancel a batch

The mock's object store and control endpoints (no key needed):

    PUT  /_mock/s3/<object key>?X-Amz-Signature=<token>   the presigned upload target
    GET  /_mock/health     liveness of the mock itself
    POST /_mock/reset      forget recorded requests, scenario progress and session state
    POST /_mock/scenarios  register a scenario
    GET  /_mock/scenarios  list scenario names
    GET  /_mock/requests?session=<id>[&path=<path>]   the requests recorded for a session
    POST /_mock/clock?session=<id>       move the session's clock: {"advance_seconds": n} or {"offset_seconds": n}
    GET  /_mock/clock?session=<id>       read it: {"epoch_seconds", "offset_seconds", "now"}
    POST /_mock/workspace?session=<id>   workspace flags: {"zero_retention": true}
    POST /_mock/truth?session=<id>       {"legacy_noul": true}: truth answers come back as {"type": "noul", "noul": x}
    POST /_mock/batches?session=<id>     the batch lifecycle: {"polls_to_running": 1, "polls_to_end": 2, ...}
    POST /_mock/sessions?session=<id>    session behaviour: {"frame_delay_ms": 0, "close": {...}, ...}
    POST /_mock/boosters?session=<id>    boosters: {"disabled": ["age"], "status": {"ai_generated": "timeout"}}
    POST /_mock/workers?session=<id>     models without a ready worker: {"cold": ["decisionnode-moe"]}
    GET  /_mock/uploads?session=<id>     the uploads presigned in a session

Request headers read by the mock (never by the hosted API):

    x-mock-scenario: <name>   answer from that scenario instead of the normal handler
    x-mock-session: <id>      groups recorded requests, scenario progress and state (default "default")

Answers are deterministic: each one is derived from a hash of the resolved model,
the state, the images and the question itself, so the same request always gets the
same response and a question's answer does not depend on the other questions.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import decimal
import email.utils
import hashlib
import ipaddress
import itertools
import json
import math
import os
import queue
import random
import re
import socket
import struct
import sys
import threading
import time
import traceback
import unicodedata
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

VERSION = "1.6.0"

# --------------------------------------------------------------------------- models

# Model ids: the family ids decisionnode-moe and decisionnode-flash
# always mean the current release; a pinned id is decisionnode-<family>-<version>, the
# version as the servers report it. Every answer names the pinned id that answered.
# The older ids stay accepted (never listed): decisionnode-1.0 and decisionnode-latest
# for the MoE family, decisionnode-1.0-flash and decisionnode-flash-latest for Flash.
SERVED_VERSION = "0.9.0-rc1"  # the mock's own; cases never assert it, only the family prefix
MOE = "decisionnode-moe-" + SERVED_VERSION
FLASH = "decisionnode-flash-" + SERVED_VERSION
MODELS = [
    {
        "name": "decisionnode-moe",
        "description": "DecisionNode MoE, the main model. Currently %s." % MOE,
        "release_date": "2026-10-01",
        "resolves_to": MOE,
        "family": "decisionnode-moe",
        "max_input_tokens": 32768,
    },
    {
        "name": MOE,
        "description": "DecisionNode MoE %s." % SERVED_VERSION,
        "release_date": "2026-10-01",
        "resolves_to": MOE,
        "family": "decisionnode-moe",
        "max_input_tokens": 32768,
    },
    {
        "name": "decisionnode-flash",
        "description": "DecisionNode Flash, the fast and lower-cost model. Currently %s." % FLASH,
        "release_date": "2026-10-01",
        "resolves_to": FLASH,
        "family": "decisionnode-flash",
        "max_input_tokens": 8192,
    },
    {
        "name": FLASH,
        "description": "DecisionNode Flash %s." % SERVED_VERSION,
        "release_date": "2026-10-01",
        "resolves_to": FLASH,
        "family": "decisionnode-flash",
        "max_input_tokens": 8192,
    },
]
MODEL_INDEX = {m["name"]: m for m in MODELS}
OLDER_IDS = {
    "decisionnode-1.0": "decisionnode-moe",
    "decisionnode-latest": "decisionnode-moe",
    "decisionnode-1.0-flash": "decisionnode-flash",
    "decisionnode-flash-latest": "decisionnode-flash",
}
for _old, _family in OLDER_IDS.items():
    MODEL_INDEX[_old] = MODEL_INDEX[_family]
FAMILY_OF = {m["resolves_to"]: m["family"] for m in MODELS}
DEFAULT_MODEL = "decisionnode-moe"

# What an answer cost: usage.cost on every answer. The live prices are the mock's own
# (one price for every token, images and video frames included); the API's live in its
# database. A batch line pays its family's share of the live price, the
# batch_price_factor each /v1/models entry states, as the API's catalogue sets it; a
# fine-tuned model has none. The mock has no free tier.
PRICE_PER_MILLION = {"decisionnode-moe": 0.042, "decisionnode-flash": 0.021}
BATCH_PRICE_FACTORS = {"decisionnode-moe": 0.7, "decisionnode-flash": 0.5}
FINE_TUNED_PREMIUM_PER_MILLION = 0.02
BOOSTER_CALL_USD = 0.002
FALLBACK_MODEL = {MOE: FLASH}  # live /v1/decide only

# Fine-tuned models (suite 1.11): <family>@<workspace-slug>/<name> is the version deployed
# now, <family>@<workspace-slug>/<name>:<n> pins version n; the older bases decisionnode-1.0
# and decisionnode-1.0-flash stay accepted. Only the keys of that
# workspace may send them (a key holding ws-acme is in workspace acme); every other name,
# an undeployed version and another workspace's model are the 400 "Unknown model".
FINE_TUNED = [
    {
        "workspace": "acme",
        "base": "decisionnode-moe",
        "name": "fraud",
        "version": 3,
        "status": "deployed",
        "description": "Claims review, trained on 2,400 decisions",
        "release_date": "2026-10-09",
    },
    {
        "workspace": "acme",
        "base": "decisionnode-flash",
        "name": "triage",
        "version": 1,
        "status": "ready",
        "description": "Fine-tuned from DecisionNode Flash",
        "release_date": "2026-10-09",
    },
]
FINE_TUNED_BASES = {"decisionnode-moe": "decisionnode-moe", "decisionnode-flash": "decisionnode-flash", "decisionnode-1.0": "decisionnode-moe", "decisionnode-1.0-flash": "decisionnode-flash"}
FINE_TUNED_NAME = re.compile(r"^(decisionnode-moe|decisionnode-flash|decisionnode-1\.0|decisionnode-1\.0-flash)@([a-z0-9-]{1,64})/([a-z0-9-]{3,40})(?::([1-9][0-9]{0,8}))?$")


def fine_tuned_id(v: dict, pinned: bool = True) -> str:
    name = "%s@%s/%s" % (v["base"], v["workspace"], v["name"])
    return "%s:%d" % (name, v["version"]) if pinned else name


def requested_id(name: str) -> str:
    """The canonical form of the model a request asked for: a family
    id or any alias of one is the family id, a pinned id stays that pinned id."""
    m = MODEL_INDEX.get(name)
    if m is None:
        return name
    return name if name == m["resolves_to"] else m["family"]


def resolve_model(name: str, workspace: str):
    """(the pinned id a response names, the token limit), or None for a name this key may
    not send: a catalogue name, else a deployed fine-tuned version of the key's workspace."""
    m = MODEL_INDEX.get(name)
    if m is not None:
        return m["resolves_to"], m["max_input_tokens"]
    found = FINE_TUNED_NAME.match(name)
    if found is None or found.group(2) != workspace:
        return None
    base, slug, own, n = found.groups()
    base = FINE_TUNED_BASES[base]
    versions = [
        v
        for v in FINE_TUNED
        if v["workspace"] == slug and v["base"] == base and v["name"] == own and v["status"] == "deployed" and (n is None or v["version"] == int(n))
    ]
    if not versions:
        return None
    v = max(versions, key=lambda x: x["version"])
    return fine_tuned_id(v), MODEL_INDEX[base]["max_input_tokens"]


# boosters (suite 1.11): specialist models that read a request's images before the decision
BOOSTERS = {
    "age": {"version": "age-v1", "latency_ms": 212, "usd": BOOSTER_CALL_USD},
    "ai_generated": {"version": "ai_generated-v1", "latency_ms": 340, "usd": BOOSTER_CALL_USD},
    # Suite 1.21: Depth Vision, distances in meters; Segment Vision, what the prompt names
    # found in a picture (free, its depth in the same call).
    "bynn_depth": {"version": "bynn_depth-v1", "latency_ms": 24, "usd": 0.001},
    "bynn_segment": {"version": "bynn_segment-v1", "latency_ms": 75, "usd": 0.0},
    # Faces matched against one collection.
    "face_recognition": {"version": "face_recognition-v1", "latency_ms": 230, "usd": BOOSTER_CALL_USD},
}
# A booster's options as the API judges them (suite 1.21, in its words): each key in the
# order sent, the first problem the reason, then what the named booster takes or needs.
BAD_COLLECTION = "\"options\": 'collection' must be a string"
BAD_THRESHOLD = "\"options\": 'threshold' must be a number from 0 to 1"
BAD_GRID_SIZE = "\"options\": 'grid_size' must be an integer from 2 to 16"
BAD_MAX_INSTANCES = "\"options\": 'max_instances' must be an integer from 1 to 16"
BAD_MIN_SCORE = "\"options\": 'min_score' must be a number from 0 to 1"
BAD_POLYGON_POINTS = "\"options\": 'polygon_points' must be an integer from 3 to 64"
BAD_DEPTH = "\"options\": 'depth' must be true or false"
BAD_PHRASES = "\"options\": 'prompt' must be a string, or an array of 1 to 4 strings, each of at most 120 characters"
NO_PHRASE = "\"options\": 'prompt' must name something to find"
MAX_PROMPT_CHARS = 2000
MAX_PHRASES = 4
MAX_PHRASE_CHARS = 120
MAX_BOOSTER_NAME_BYTES = 64  # a name or an option key quoted in a reason is cut to this
DEPTH_GRID = 8
SEGMENT_DEFAULTS = {"max_instances": 8, "min_score": 0.25, "polygon_points": 24, "depth": True}
SEGMENT_HFOV_DEG = 65.0
FACE_THRESHOLD = 0.5
CELEBRITIES = (("celeb-00412", "A Public Figure"), ("celeb-00977", "Another Public Figure"))
MAX_BOOSTERS = 4
BOOSTER_KEYS = ("name", "images", "require", "options")
BOOSTER_BUDGET_MS = 800  # a booster that did not answer in time reports its budget
BOOSTER_ERROR_MS = 5
BOOSTER_ENTRY_TOKENS = 6  # the mock's estimate of what the evidence block adds to input_tokens
BOOSTER_READING_TOKENS = 18
GENERATORS = ("Generator A", "Generator B", "Generator C", "Generator D")
LIKELY_GENERATED = 0.69  # top_generator is named only above this; a photograph reads null

MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10
MAX_IMAGES = 16  # suite 1.7 (4 before)
MAX_VIDEOS = 4
MAX_VIDEO_FPS = 4
DEFAULT_VIDEO_FPS = 2
MAX_FRAMES_REQUEST = 128  # frames sent over all of a request's videos (the API's limit; the SDKs never count them)
MAX_SENT_FRAMES = 1024  # one video's list
VIDEO_FRAME_TOKENS = 65  # the mock's approximation for one frame read
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TOKENS = 765
MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")
MAX_URL_CHARS = 2048
MAX_IMAGE_ID_CHARS = 64  # code points
MAX_NESTING = 128  # containers inside each other, the body's own object included
MAX_BODY_BYTES = 26214400  # 25 MiB, the public request body limit (64 MB before)
BODY_TOO_LARGE = "The request body is over 26,214,400 bytes (25 MiB). Send large images as uploads (POST /v1/uploads) and name them by upload_id."
QUESTION_TYPES = ("truth", "choice", "score")
DECIDE_TYPES = ("truth", "noul", "choice", "score", "number", "point", "box")
PLACE_TYPES = ("point", "box")
PLACE_UNITS = ("pixels", "normalized")
PLACE_WIDTH, PLACE_HEIGHT = 1920, 1080  # the size this mock answers for every image
# The order objects were created in, across threads: a clock reading can repeat on a
# coarse clock (Windows), so two creates sent back to back would sort as equal.
CREATION_ORDER = itertools.count(1)
PLACES_MIN, PLACES_MAX = 2, 16  # a point or box question's max
# Collections.
COLLECTION_NAME_MAX = 80
ITEM_REFERENCE_MAX, ITEM_LABEL_MAX = 64, 128
ITEM_REFERENCE_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
ENROLLMENT_USD = 0.001
PUBLIC_COLLECTION_ITEMS = {"celebrities": 1200}
IDEMPOTENCY_WINDOW_S = 24 * 3600  # with the three messages below
IDEMPOTENCY_MALFORMED = "Idempotency-Key must be sent once, as 1 to 255 printable ASCII characters."
IDEMPOTENCY_IN_PROGRESS = "A request with this Idempotency-Key is still running. Try again shortly."
IDEMPOTENCY_REUSED = "This Idempotency-Key was already used with a different request."
COLLECTIONS_MAX, ITEMS_MAX = 100, 1_000_000  # per workspace; a scenario may lower them
ITEMS_COUNTED = 10_000  # a filtered items page counts its matches up to this, then says total_capped

# number questions
NUMBER_ONE_QUESTION_VALUES = 256
NUMBER_MAX_GRID_VALUES = 4096
NUMBER_MAX_DECIMALS = 6
TWO_53 = 2**53

# uploads
UPLOAD_TTL_S = 3600
UPLOAD_URL_TTL_S = 300
UPLOAD_MAX_FILES = 16
UPLOAD_BODY_LIMIT = 64 * 1024

# sessions
SESSION_DEFAULTS = {
    "enabled": True,
    "max_per_key": 4,
    "default_window": 8,
    "max_window": 64,
    "default_ttl_seconds": 300,
    "max_ttl_seconds": 3600,
    "idle_timeout_seconds": 120,
    "max_frame_tokens": 1535,
    "max_frame_images": 1,
    "window_tokens": 1536,
    "max_prefix_tokens": 34752,
}
SESSION_MAX_MESSAGE_BYTES = 1024 * 1024  # 16 MiB before suite 1.5
SESSION_PING_TIMEOUT_S = 45

# batches
BATCH_MAX_REQUESTS = 10000
BATCH_MAX_BYTES = 256 * 1024 * 1024
BATCH_ADD_BYTES = 10 * 1024 * 1024
BATCH_ACTIVE_PER_KEY = 10
BATCH_RESULTS_DAYS = 7
BATCH_EXPIRY_HOURS = 24
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BATCH_DEFAULTS = {"polls_to_running": 1, "polls_to_end": 2, "end_state": "completed", "running_in_progress": 0, "get_retry_after": None}


def models_body(workspace: str = "default", max_places: int = PLACES_MAX) -> dict:
    out = []
    for m in MODELS:
        out.append(
            {
                "name": m["name"],
                "description": m["description"],
                "release_date": m["release_date"],
                # Each family's entry, then each pinned version
                # served, every one naming its pinned version and its family id; the
                # family's entry also names its current version.
                "version": m["resolves_to"],
                "alias": m["family"],
                **({"current_version": m["resolves_to"]} if m["name"] == m["family"] else {}),
                **model_prices(m["family"]),
                **model_limits(m, max_places),
            }
        )
    # The key's workspace's fine-tuned models: every ready or deployed version under its
    # pinned name, and the plain name of a model with a deployed version, each with its
    # base's limits.
    mine = sorted((v for v in FINE_TUNED if v["workspace"] == workspace), key=lambda v: (fine_tuned_id(v, False), v["version"]))
    for v in mine:
        base = MODEL_INDEX[v["base"]]
        entries = []
        if v["status"] == "deployed":
            entries.append(fine_tuned_id(v, False))
        entries.append(fine_tuned_id(v))
        for name in entries:
            out.append(
                {
                    "name": name,
                    "description": v["description"],
                    "release_date": v["release_date"],
                    "version": fine_tuned_id(v),
                    **model_prices(FAMILY_OF[base["resolves_to"]], fine_tuned=True),
                    **model_limits(base, max_places),
                    "fine_tuned": True,
                    "base": v["base"],
                    "family": FAMILY_OF[base["resolves_to"]],
                    "adapter_version": v["version"],
                    "status": v["status"],
                }
            )
    return {"models": out}


def model_limits(m: dict, max_places: int = PLACES_MAX) -> dict:
    """The limits of a catalogue model's entry; a fine-tuned model's entry carries its base's.
    places.max_places only while the versions answer several places."""
    return {
        **({"places": {"max_places": max_places}} if max_places else {}),
        "max_input_tokens": m["max_input_tokens"],
        "max_choice_options": MAX_CHOICE_OPTIONS,
        "max_score_levels": MAX_SCORE_LEVELS,
        "max_images": MAX_IMAGES,
        "max_image_bytes": MAX_IMAGE_BYTES,
        "number_one_question_values": NUMBER_ONE_QUESTION_VALUES,
        "number_max_grid_values": NUMBER_MAX_GRID_VALUES,
        "number_default_range": [0, 255],
        "number_default_step": 1,
        "upload_max_bytes": MAX_IMAGE_BYTES,
        "upload_ttl_seconds": UPLOAD_TTL_S,
        "upload_media_types": list(MEDIA_TYPES),
        "sessions": dict(SESSION_DEFAULTS),
        "batch": {
            "requests_per_batch": BATCH_MAX_REQUESTS,
            "batch_bytes": BATCH_MAX_BYTES,
            "add_bytes": BATCH_ADD_BYTES,
            "active_batches_per_key": BATCH_ACTIVE_PER_KEY,
            "queued_requests_per_key": 100000,
            "time_to_completion_hours": BATCH_EXPIRY_HOURS,
            "result_retention_days": BATCH_RESULTS_DAYS,
            "max_custom_id_chars": 512,
        },
    }


# --------------------------------------------------------------------------- helpers


def mock_log(server, text: str) -> None:
    """Write one log line where the person running the mock asked for it: --log-file, else stderr with
    --verbose, else nowhere. The mock never writes to stderr unasked, so a harness that
    pipes stderr without reading it can never fill the pipe and stall the mock."""
    path = getattr(server, "log_file", None)
    line = text if text.endswith("\n") else text + "\n"
    try:
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        elif getattr(server, "verbose", False):
            sys.stderr.write(line)
            sys.stderr.flush()
    except (OSError, ValueError):
        pass


def compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def ascii_header(text: str) -> str:
    """Visible ASCII only: anything else is written as a JSON-style escape."""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            out.append(ch)
        elif code <= 0xFFFF:
            out.append("\\u%04x" % code)
        else:
            code -= 0x10000
            out.append("\\u%04x\\u%04x" % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
    return "".join(out)


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class LiteralFloat(float):
    """A float that keeps the text it was parsed from, so a number question's grid is
    built from the exact decimal the client wrote."""

    def __new__(cls, text):
        obj = float.__new__(cls, text)
        obj.literal = text
        return obj


def loads(text: str):
    return json.loads(text, parse_float=LiteralFloat)


class Refused(Exception):
    """A request the API refuses: status, body and the reason for x-decisionnode-error."""

    def __init__(self, status: int, body, reason: str, headers: dict | None = None):
        super().__init__(reason)
        self.status = status
        self.body = body
        self.reason = reason
        self.headers = headers or {}


def usage_error(message: str, reason: str | None = None) -> Refused:
    return Refused(400, {"detail": {"error_type": "api_usage_error", "message": message}}, reason or message)


def typed_error(status: int, error_type: str, message: str, reason: str | None = None, headers: dict | None = None) -> Refused:
    return Refused(status, {"detail": {"error_type": error_type, "message": message}}, reason or message, headers)


def entry(kind: str, loc: list, msg: str, value, ctx=None, reason=None) -> dict:
    e = {"type": kind, "loc": loc, "msg": msg, "input": value}
    if ctx is not None:
        e["ctx"] = ctx
    if reason is not None:
        e["_reason"] = reason
    return e


def missing(loc: list, value) -> dict:
    return entry("missing", loc, "Field required", value)


ECHO_CHARS = 64  # a collections 422 echoes a string cut to 64 characters, then "..."


def echoed(value):
    """An input as a collections 422 entry echoes it, as the API does: a string cut to
    64 characters with "..." added, a number, a boolean or null as it is, an object or
    an array never (null), so never the whole body or a whole image object."""
    if isinstance(value, str):
        return value[:ECHO_CHARS] + ("..." if len(value) > ECHO_CHARS else "")
    if isinstance(value, (dict, list)):
        return None
    return value


def centry(kind: str, loc: list, msg: str, value, ctx=None) -> dict:
    """A collections 422 entry, its input echoed within bounds."""
    return entry(kind, loc, msg, echoed(value), ctx)


METADATA_KEYS, METADATA_KEY_CHARS, METADATA_TEXT_CHARS, METADATA_BYTES = 20, 40, 500, 4096
METADATA_KEY = re.compile(r"[a-z0-9_]{1,40}")


def forbidden_char(c: str) -> bool:
    """A control or invisible character, which no name, label or metadata text holds."""
    o = ord(c)
    return (
        o <= 0x1F or 0x7F <= o <= 0x9F or o == 0xAD or 0x600 <= o <= 0x605 or o in (0x61C, 0x6DD, 0x70F, 0x8E2, 0x180E, 0xFEFF)
        or 0x890 <= o <= 0x891 or 0x200B <= o <= 0x200F or 0x2028 <= o <= 0x202E or 0x2060 <= o <= 0x206F
        or 0xFFF9 <= o <= 0xFFFB or o in (0x110BD, 0x110CD, 0xE0001) or 0x13430 <= o <= 0x1343F
        or 0x1BCA0 <= o <= 0x1BCA3 or 0x1D173 <= o <= 0x1D17A or 0xE0020 <= o <= 0xE007F
    )


def metadata_entries(metadata) -> list:
    """An item's metadata's 422 entries, as the API lists them: a flat object of at most
    20 keys of a-z, 0-9 and _ (1 to 40), each a text (at most 500 characters, no control
    or invisible one), a number (under 10^15 in size, and 0 or at least 10^-6) or a
    boolean, at most 4 KB as compact JSON."""
    loc = ["body", "metadata"]
    if not isinstance(metadata, dict):
        return [centry("value_error", loc, "Metadata is an object of keys and values.", metadata)]
    if len(metadata) > METADATA_KEYS:
        return [centry("value_error", loc, "Metadata holds at most %d keys." % METADATA_KEYS, metadata)]
    out = []
    for key, value in metadata.items():
        if not METADATA_KEY.fullmatch(key):
            shown = "".join("?" if unicodedata.category(c) == "Cc" else c for c in key[:METADATA_KEY_CHARS]) + ("..." if len(key) > METADATA_KEY_CHARS else "")
            out.append(centry("value_error", loc, 'A metadata key is 1 to %d characters of a-z, 0-9 and _: "%s".' % (METADATA_KEY_CHARS, shown), metadata))
            continue
        at = loc + [key]
        if isinstance(value, str):
            if len(value) > METADATA_TEXT_CHARS:
                out.append(centry("value_error", at, "A metadata text is at most %d characters." % METADATA_TEXT_CHARS, value))
            elif any(forbidden_char(c) for c in value):
                out.append(centry("value_error", at, "A metadata text holds no control or invisible characters.", value))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            size = abs(float(value))
            if not (math.isfinite(size) and size < 1e15 and (size == 0 or size >= 1e-6)):
                out.append(centry("value_error", at, "A metadata number is under 10^15 in size, and 0 or at least 10^-6.", value))
        elif not isinstance(value, bool):
            out.append(centry("value_error", at, "A metadata value is a text, a number or true or false.", value))
    if not out and len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > METADATA_BYTES:
        out.append(centry("value_error", loc, "Metadata is at most %d bytes as JSON." % METADATA_BYTES, metadata))
    return out


def reference_entries(loc: list, reference) -> list:
    """A reference's entries: a string of 1 to 64 of A-Za-z0-9_.:-, its length first."""
    if not isinstance(reference, str):
        return [centry("string_type", loc, "Input should be a valid string", reference)]
    e = string_length(loc, reference, ITEM_REFERENCE_MAX)
    if e:
        return [e]
    if not re.fullmatch(ITEM_REFERENCE_PATTERN, reference):
        return [centry("string_pattern_mismatch", loc, "String should match pattern '%s'" % ITEM_REFERENCE_PATTERN, reference, {"pattern": ITEM_REFERENCE_PATTERN})]
    return []


def label_entries(loc: list, label) -> list:
    """A label's entries: none or null, else a string of 1 to 128 characters."""
    if label is None:
        return []
    if not isinstance(label, str):
        return [centry("string_type", loc, "Input should be a valid string", label)]
    e = string_length(loc, label, ITEM_LABEL_MAX)
    if e:
        return [e]
    if any(forbidden_char(c) for c in label):
        return [centry("string_pattern_mismatch", loc, "A label holds no control or invisible characters", label)]
    return []


def string_length(loc: list, value: str, most: int):
    """A collections string's length entry, in characters, against 1 to most."""
    if not value:
        return centry("string_too_short", loc, "String should have at least 1 character", value, {"min_length": 1})
    if len(value) > most:
        return centry("string_too_long", loc, "String should have at most %d characters" % most, value, {"max_length": most})
    return None


def str_dict_list(loc: list, value) -> list:
    return [
        entry("string_type", loc + ["str"], "Input should be a valid string", value),
        entry("dict_type", loc + ["dict[any,any]"], "Input should be a valid dictionary", value),
        entry("list_type", loc + ["list[any]"], "Input should be a valid list", value),
    ]


def is_sdl(value) -> bool:
    return isinstance(value, (str, dict, list))


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def loc_text(loc: list) -> str:
    parts = [str(p) for p in loc[1:]]
    return ".".join(parts) if parts else "body"


def refuse_422(entries: list) -> Refused:
    first = entries[0]
    reason = first.get("_reason")
    if reason is not None:
        if len(entries) > 1:
            reason += " and %d more" % (len(entries) - 1)
    else:
        reason = "%s: %s" % (loc_text(first["loc"]), first["msg"])
    clean = [{k: v for k, v in e.items() if k != "_reason"} for e in entries]
    return Refused(422, {"detail": clean}, reason)


def nesting_depth(value) -> int:
    """How many objects and arrays sit inside each other, the outermost included."""
    deepest = 0
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return deepest


def too_deep(depth: int) -> Refused:
    return usage_error("Invalid request.", "the request nests %d objects and arrays inside each other; the limit is %d" % (depth, MAX_NESTING))


def check_nesting(value) -> None:
    depth = nesting_depth(value)
    if depth > MAX_NESTING:
        raise too_deep(depth)


def parse_body(body_bytes: bytes):
    """The JSON body, or the 422 the API sends for a body that is not JSON."""
    try:
        text = body_bytes.decode("utf-8")
        return loads(text)
    except RecursionError:
        # Far deeper than the 128 levels the API takes: the same 400.
        raise usage_error("Invalid request.", "the request nests more objects and arrays inside each other than the limit of %d" % MAX_NESTING)
    except UnicodeDecodeError:
        raise Refused(
            422,
            {"detail": [{"type": "json_invalid", "loc": ["body", 0], "msg": "JSON decode error", "input": {}, "ctx": {"error": "Invalid UTF-8"}}]},
            "the body is not valid UTF-8",
        )
    except json.JSONDecodeError as exc:
        raise Refused(
            422,
            {"detail": [{"type": "json_invalid", "loc": ["body", exc.pos], "msg": "JSON decode error", "input": {}, "ctx": {"error": exc.msg}}]},
            "the body is not JSON: %s at position %d" % (exc.msg, exc.pos),
        )


# --------------------------------------------------------------------------- validation


def validate(body_bytes: bytes, profile: str = "decide", resolver=None, workspace: str = "default", booster_conf: dict | None = None, max_places: int = PLACES_MAX, collections=None) -> dict:
    """Return a normalised request or raise Refused."""
    return validate_obj(parse_body(body_bytes), profile, resolver, workspace=workspace, booster_conf=booster_conf, max_places=max_places, collections=collections)


def validate_obj(body, profile: str = "decide", resolver=None, places: bool = True, workspace: str = "default", booster_conf: dict | None = None, max_places: int = PLACES_MAX, collections=None) -> dict:
    check_nesting(body)
    if places and not max_places:
        # Checked before the rest of the body.
        places_not_supported(body)
    if not isinstance(body, dict):
        raise Refused(
            422,
            {"detail": [entry("model_attributes_type", ["body"], "Input should be a valid dictionary or object to extract fields from", body)]},
            "the body must be a JSON object",
        )

    entries: list = []
    deferred: list = []  # 400s, reported only when there is no 422

    # model
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        entries.append(entry("string_type", ["body", "model"], "Input should be a valid string", model))

    # state
    state = body.get("state")
    if state is not None and not is_sdl(state):
        entries.extend(str_dict_list(["body", "state"], state))

    # questions
    questions = body.get("questions")
    grids: dict = {}
    if "questions" not in body or questions is None:
        entries.append(missing(["body", "questions"], body))
    elif not isinstance(questions, dict):
        entries.append(entry("dict_type", ["body", "questions"], "Input should be a valid dictionary", questions))
    elif len(questions) == 0:
        entries.append(entry("too_short", ["body", "questions"], "Dictionary should have at least 1 item after validation, not 0", questions))
    else:
        for qid, q in questions.items():
            validate_question(qid, q, entries, deferred, profile, grids)
            if places and isinstance(q, dict) and q.get("type") in PLACE_TYPES:
                raw_images = body.get("images")
                ids = [i.get("id") for i in raw_images if isinstance(i, dict)] if isinstance(raw_images, list) else []
                validate_place(qid, q, entries, deferred, ids)

    # fallback (suite 1.11): a boolean
    fallback = body.get("fallback")
    if fallback is not None and not isinstance(fallback, bool):
        entries.append(entry("bool_type", ["body", "fallback"], "Input should be a valid boolean", fallback))

    # images
    images = body.get("images")
    if images is not None and not isinstance(images, list):
        entries.append(entry("list_type", ["body", "images"], "Input should be a valid list", images))

    # videos (suite 1.7)
    videos = body.get("videos")
    if videos is not None and not isinstance(videos, list):
        entries.append(entry("list_type", ["body", "videos"], "Input should be a valid list", videos))

    # more images or videos than a request takes (suite 1.9: the API's 422 too_long)
    for name, items, most in (("images", images, MAX_IMAGES), ("videos", videos, MAX_VIDEOS)):
        if isinstance(items, list) and len(items) > most:
            msg = "List should have at most %d items after validation, not %d" % (most, len(items))
            entries.append(entry("too_long", ["body", name], msg, items, {"field_type": "List", "max_length": most, "actual_length": len(items)}))

    if entries:
        raise refuse_422(entries)

    # 400s, in a fixed order
    name = model if model is not None else DEFAULT_MODEL
    resolved = resolve_model(name, workspace)
    if resolved is None:
        raise usage_error("Unknown model: %s" % name, "unknown model '%s'; GET /v1/models lists the models" % name)
    model_id, limit = resolved

    # Boosters are checked before any of them runs (suite 1.11); once the list is
    # accepted, every later answer, a refusal included, carries x-decisionnode-boosters.
    raw_ids = [i.get("id") for i in images if isinstance(i, dict)] if isinstance(images, list) else []
    plan = booster_plan(check_boosters(body.get("boosters"), raw_ids, booster_conf or {}, collections), booster_conf or {})
    try:
        out = validate_rest(deferred, plan, state, questions, grids, images, videos, resolver, model_id, limit)
        out["fallback"] = fallback
        out["requested_model"] = requested_id(name)
        return out
    except Refused as exc:
        if plan:
            exc.headers.setdefault("x-decisionnode-boosters", booster_header(plan))
        raise


def validate_rest(deferred, plan, state, questions, grids, images, videos, resolver, model_id, limit) -> dict:
    """validate_obj from its first 400 after the model and the boosters: the questions'
    400s, the images and videos, the boosters' readings, then the token count."""
    for item in deferred:
        raise item

    image_list = validate_images(images or [], resolver)
    video_list = validate_videos(videos or [], [i["id"] for i in image_list])
    evidence, calls, booster_tokens, booster_usd = run_boosters(plan, image_list)

    # normalise questions
    norm = {}
    answer_types = {}
    for qid, q in questions.items():
        norm[qid] = normalise_question(q, grids.get(qid))
        answer_types[qid] = "noul" if q["type"] == "noul" else q["type"]

    state_text = "" if state is None else (state if isinstance(state, str) else compact(state))

    tokens = count_tokens(state_text, norm, image_list, grids) + VIDEO_FRAME_TOKENS * sum(v["frames_read"] for v in video_list) + booster_tokens
    if tokens > limit:
        raise Refused(
            400,
            {"detail": {"error_type": "max_tokens_exceeded"}},
            "the request is %d tokens, the limit of %s is %d" % (tokens, model_id, limit),
        )

    out = {
        "model": model_id,
        "state_text": state_text,
        "questions": norm,
        "answer_types": answer_types,
        "grids": grids,
        "images": image_list,
        "videos": video_list,
        "tokens": tokens,
    }
    if plan:
        out.update(evidence=evidence, booster_calls=calls, booster_tokens=booster_tokens, booster_usd=booster_usd, booster_header=booster_header(plan))
    return out


def validate_question(qid: str, q, entries: list, deferred: list, profile: str = "decide", grids: dict | None = None) -> None:
    base = ["body", "questions", qid]
    if not isinstance(q, dict):
        entries.append(entry("model_type", base, "Input should be a valid dictionary or object", q))
        return
    if "type" not in q or q.get("type") is None:
        entries.append(missing(base + ["type"], q))
        return
    qtype = q["type"]
    if qtype not in DECIDE_TYPES:
        reason = "question '%s': unknown type %s; the types are truth, noul, choice, score, number, point and box" % (qid, compact(qtype))
        deferred.append(usage_error("Invalid request.", reason))
        return
    loc = base + [qtype]

    instructions = q.get("instructions")
    if instructions is not None and not is_sdl(instructions):
        entries.extend(str_dict_list(loc + ["instructions"], instructions))

    criteria = q.get("criteria")

    if qtype == "number":
        validate_number(qid, q, loc, entries, deferred, grids if grids is not None else {})
    elif qtype == "choice":
        if criteria is None:
            entries.append(missing(loc + ["criteria"], q))
        elif not isinstance(criteria, dict):
            entries.append(entry("dict_type", loc + ["criteria"], "Input should be a valid dictionary", criteria))
        else:
            for name, desc in criteria.items():
                if desc is not None and not is_sdl(desc):
                    entries.extend(str_dict_list(loc + ["criteria", name], desc))
            if len(criteria) == 0:
                deferred.append(
                    Refused(400, {"detail": "Choice question must have at least one choice: %s" % qid}, "question '%s': a choice needs at least one option" % qid)
                )
            elif len(criteria) > MAX_CHOICE_OPTIONS:
                deferred.append(
                    Refused(
                        400,
                        {"detail": "Too many choices. Must have at most %d choices." % MAX_CHOICE_OPTIONS},
                        "question '%s': a choice takes up to %d options, got %d" % (qid, MAX_CHOICE_OPTIONS, len(criteria)),
                    )
                )
    elif qtype == "score":
        if criteria is None:
            entries.append(missing(loc + ["criteria"], q))
            return
        levels = None
        if isinstance(criteria, list):
            levels = criteria
        elif isinstance(criteria, dict):
            keys = list(criteria.keys())
            if sorted(keys, key=lambda k: (len(k), k)) == [str(i) for i in range(len(keys))]:
                levels = [criteria[str(i)] for i in range(len(keys))]
            else:
                entries.append(entry("list_type", loc + ["criteria"], "Input should be a valid list", criteria))
                return
        else:
            entries.append(entry("list_type", loc + ["criteria"], "Input should be a valid list", criteria))
            return
        if len(levels) == 0:
            entries.append(entry("too_short", loc + ["criteria"], "List should have at least 1 item after validation, not 0", criteria))
            return
        for i, level in enumerate(levels):
            if not is_sdl(level):
                entries.extend(str_dict_list(loc + ["criteria", i], level))
        if len(levels) > MAX_SCORE_LEVELS:
            deferred.append(
                Refused(
                    400,
                    {"detail": "Too many score levels. Must have at most %d levels." % MAX_SCORE_LEVELS},
                    "question '%s': a score takes up to %d levels, got %d" % (qid, MAX_SCORE_LEVELS, len(levels)),
                )
            )
    elif qtype in PLACE_TYPES:
        return  # validate_place, with the request's image ids
    else:  # truth and its alias noul
        if criteria is not None and not isinstance(criteria, dict):
            entries.append(entry("dict_type", loc + ["criteria"], "Input should be a valid dictionary", criteria))
            return
        crit = criteria or {}
        for side in ("true", "false"):
            if side in crit and crit[side] is not None and not is_sdl(crit[side]):
                entries.extend(str_dict_list(loc + ["criteria", side], crit[side]))
        has_criteria = any(crit.get(side) is not None for side in ("true", "false"))
        if instructions is None and not has_criteria:
            if qtype == "noul":
                deferred.append(
                    Refused(400, {"detail": "Noul question must have criteria or instructions: %s" % qid}, "question '%s': a noul question needs instructions or criteria" % qid)
                )
            else:
                deferred.append(
                    Refused(400, {"detail": "Truth question must have criteria or instructions: %s" % qid}, "question '%s': a truth question needs instructions or criteria" % qid)
                )


def validate_place(qid: str, q: dict, entries: list, deferred: list, image_ids: list) -> None:
    """A point or box question: the image it looks in (one of the request's, named when
    there are two or more), its units, then its instructions (the plain 400)."""
    qtype = q["type"]
    loc = ["body", "questions", qid, qtype]
    image = q.get("image")
    if not image_ids:
        entries.append(entry("image_required", loc, "A %s question needs an image in the request" % qtype, q))
    elif image is None:
        if len(image_ids) >= 2:
            entries.append(missing(loc + ["image"], q))
    elif not isinstance(image, str):
        entries.append(entry("string_type", loc + ["image"], "Input should be a valid string", image))
    elif image not in image_ids:
        entries.append(entry("unknown_image", loc + ["image"], "Input should be the id of an image of the request", image))
    units = q.get("units")
    if units is not None and units not in PLACE_UNITS:
        entries.append(entry("literal_error", loc + ["units"], "Input should be 'pixels' or 'normalized'", units, {"expected": "'pixels' or 'normalized'"}))
    most = q.get("max")
    if most is not None:
        if not isinstance(most, int) or isinstance(most, bool):
            entries.append(entry("int_type", loc + ["max"], "Input should be a valid integer", most))
        elif most < PLACES_MIN:
            entries.append(entry("greater_than_equal", loc + ["max"], "Input should be greater than or equal to %d" % PLACES_MIN, most, {"ge": PLACES_MIN}))
        elif most > PLACES_MAX:
            entries.append(entry("less_than_equal", loc + ["max"], "Input should be less than or equal to %d" % PLACES_MAX, most, {"le": PLACES_MAX}))
    if q.get("instructions") is None:
        name = qtype.capitalize()
        deferred.append(Refused(400, {"detail": "%s question must have instructions: %s" % (name, qid)}, "question '%s': a %s question needs instructions" % (qid, qtype)))


def place_answer(seed: str, q: dict) -> dict:
    """A point or a box on the image the question names, the mock's size for every image;
    with max, the list of places."""
    stream = Stream(seed)
    normalized = q.get("units") == "normalized"

    def coord(fraction: float, size: int):
        return round(fraction, 4) if normalized else min(size - 1, int(fraction * size))

    if q.get("max") is not None:
        return places_answer(stream, q, coord)

    out: dict = {"type": q["type"]}
    if q["type"] == "point":
        out["x"], out["y"] = coord(stream.next(), PLACE_WIDTH), coord(stream.next(), PLACE_HEIGHT)
    else:
        xs = sorted((stream.next(), stream.next()))
        ys = sorted((stream.next(), stream.next()))
        out["x1"], out["y1"] = coord(xs[0], PLACE_WIDTH), coord(ys[0], PLACE_HEIGHT)
        out["x2"], out["y2"] = coord(xs[1], PLACE_WIDTH), coord(ys[1], PLACE_HEIGHT)
    out["present"] = round(stream.next(), 2)
    out["confidence"] = round(stream.next(), 2)
    out.update(image=q["image"], width=PLACE_WIDTH, height=PLACE_HEIGHT)
    return out


def places_answer(stream, q: dict, coord) -> dict:
    """Several places: up to max, found as the stream says (sometimes more than max, so
    complete is low), in a fixed order: left to right by the centre, ties from the top."""
    most = q["max"]
    found = int(stream.next() * (most + 2))
    count = min(found, most)
    places = []
    for _ in range(count):
        if q["type"] == "point":
            x, y = stream.next(), stream.next()
            places.append(((x, y), {"x": coord(x, PLACE_WIDTH), "y": coord(y, PLACE_HEIGHT)}))
        else:
            xs = sorted((stream.next(), stream.next()))
            ys = sorted((stream.next(), stream.next()))
            centre = ((xs[0] + xs[1]) / 2, (ys[0] + ys[1]) / 2)
            places.append((centre, {"x1": coord(xs[0], PLACE_WIDTH), "y1": coord(ys[0], PLACE_HEIGHT), "x2": coord(xs[1], PLACE_WIDTH), "y2": coord(ys[1], PLACE_HEIGHT)}))
    places.sort(key=lambda p: p[0])
    for _, place in places:
        place["confidence"] = round(0.5 + stream.next() / 2, 2)
    out = {"type": q["type"], "count": count}
    out["present"] = round(0.9 + stream.next() / 10, 2) if count else round(stream.next() / 10, 2)
    out["complete"] = round(0.9 + stream.next() / 10, 2) if found <= most else round(stream.next() / 4, 2)
    out["points" if q["type"] == "point" else "boxes"] = [p for _, p in places]
    out.update(image=q["image"], width=PLACE_WIDTH, height=PLACE_HEIGHT)
    return out


def places_not_supported(body) -> None:
    """The API's 422 while the model version has no several-places capability: one
    entry per point or box question that carries max, before the rest of the body is read."""
    questions = body.get("questions") if isinstance(body, dict) else None
    if not isinstance(questions, dict):
        return
    entries = []
    for qid, q in questions.items():
        if isinstance(q, dict) and q.get("type") in PLACE_TYPES and q.get("max") is not None:
            loc = ["body", "questions", qid, q["type"], "max"]
            entries.append(entry("not_supported", loc, "Several places are not supported by this model version", q["max"]))
    if entries:
        raise refuse_422(entries)


# --------------------------------------------------------------------------- number questions


def dec_text(value: Decimal) -> str:
    """The shortest plain decimal form: no exponent, no trailing zeros, no -0."""
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def places(value: Decimal) -> int:
    exp = value.normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def number_field(value):
    """(Decimal or None, fault type or None) for one of min, max, step."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "float_type"
    if isinstance(value, float) and not math.isfinite(value):
        return None, "finite_number"
    if isinstance(value, LiteralFloat):
        return Decimal(value.literal), None
    if isinstance(value, float):
        return Decimal(repr(value)), None
    return Decimal(value), None


def json_number(value: Decimal):
    """A decimal as the JSON number the grid writes: an integer when whole."""
    text = dec_text(value)
    if "." in text:
        return float(text)
    return int(text)


def validate_number(qid: str, q: dict, loc: list, entries: list, deferred: list, grids: dict) -> None:
    """One 422 entry per faulty field, then the grid checks."""
    raw = {name: q.get(name) for name in ("min", "max", "step")}
    defaults = {"min": Decimal(0), "max": Decimal(255), "step": Decimal(1)}
    values = {}
    ok = {}
    for name in ("min", "max", "step"):
        dec, fault = number_field(raw[name])
        sent = raw[name]
        floc = loc + [name]
        if fault == "float_type":
            kind = "a string" if isinstance(sent, str) else ("a boolean" if isinstance(sent, bool) else ("an object" if isinstance(sent, dict) else "an array"))
            entries.append(entry("float_type", floc, "Input should be a valid number", sent, reason="question '%s': %s must be a number, got %s" % (qid, name, kind)))
            ok[name] = False
            continue
        if fault == "finite_number":
            entries.append(entry("finite_number", floc, "Input should be a finite number", sent, reason="question '%s': %s must be a finite number" % (qid, name)))
            ok[name] = False
            continue
        if dec is None:
            values[name] = defaults[name]
            ok[name] = True
            continue
        if name in ("min", "max") and dec < -TWO_53:
            entries.append(entry("greater_than_equal", floc, "Input should be greater than or equal to -%d" % TWO_53, sent, {"ge": -TWO_53}, "question '%s': %s must be within plus or minus 2^53" % (qid, name)))
            ok[name] = False
            continue
        if name in ("min", "max") and dec > TWO_53:
            entries.append(entry("less_than_equal", floc, "Input should be less than or equal to %d" % TWO_53, sent, {"le": TWO_53}, "question '%s': %s must be within plus or minus 2^53" % (qid, name)))
            ok[name] = False
            continue
        if name == "step" and dec <= 0:
            entries.append(entry("greater_than", floc, "Input should be greater than 0", sent, {"gt": 0}, "question '%s': step must be above 0, got %s" % (qid, compact(sent))))
            ok[name] = False
            continue
        if name in ("min", "step") and places(dec) > NUMBER_MAX_DECIMALS:
            entries.append(
                entry(
                    "decimal_max_places",
                    floc,
                    "Decimal input should have no more than %d decimal places" % NUMBER_MAX_DECIMALS,
                    sent,
                    {"decimal_places": NUMBER_MAX_DECIMALS},
                    "question '%s': %s has %d decimals, at most %d" % (qid, name, places(dec), NUMBER_MAX_DECIMALS),
                )
            )
            ok[name] = False
            continue
        values[name] = dec
        ok[name] = True
    if not all(ok.values()):
        return
    lo, hi, step = values["min"], values["max"], values["step"]
    d = max(places(lo), places(step))
    if d > 0:
        bound = Decimal(10) ** (15 - d) - Decimal(10) ** (-d)
        btext = dec_text(bound)
        for name, val in (("min", lo), ("max", hi)):
            if val > bound:
                entries.append(
                    entry("less_than_equal", loc + [name], "Input should be less than or equal to %s" % btext, raw[name], {"le": json_number(bound)},
                          "question '%s': %s must be within plus or minus %s on a grid with %d decimals, so every value fits 15 significant digits" % (qid, name, btext, d))
                )
                return
            if val < -bound:
                entries.append(
                    entry("greater_than_equal", loc + [name], "Input should be greater than or equal to -%s" % btext, raw[name], {"ge": json_number(-bound)},
                          "question '%s': %s must be within plus or minus %s on a grid with %d decimals, so every value fits 15 significant digits" % (qid, name, btext, d))
                )
                return
    if lo > hi:
        max_sent = raw["max"] if raw["max"] is not None else 255
        min_sent = raw["min"] if raw["min"] is not None else 0
        entries.append(
            entry("less_than_equal", loc + ["min"], "Input should be less than or equal to %s" % compact(max_sent), min_sent, {"le": max_sent},
                  "question '%s': min %s is above max %s" % (qid, compact(min_sent), compact(max_sent)))
        )
        return
    scale = Decimal(10) ** d
    m = int(lo * scale)
    s = int(step * scale)
    big_m = int((hi * scale).to_integral_value(rounding=decimal.ROUND_FLOOR))
    n = (big_m - m) // s + 1
    if n > NUMBER_MAX_GRID_VALUES:
        entries.append(
            entry(
                "too_long",
                loc,
                "Grid should have at most %d values, not %d" % (NUMBER_MAX_GRID_VALUES, n),
                q,
                {"field_type": "Grid", "max_length": NUMBER_MAX_GRID_VALUES, "actual_length": n},
                "question '%s': the grid holds %d values, the limit is %d; narrow the range or widen the step" % (qid, n, NUMBER_MAX_GRID_VALUES),
            )
        )
        return
    instructions = q.get("instructions")
    if instructions is None or instructions == "":
        deferred.append(Refused(400, {"detail": "Number question must have instructions: %s" % qid}, "question '%s': a number question needs instructions" % qid))
        return
    grids[qid] = {"m": m, "s": s, "n": n, "d": d, "min": dec_text(lo), "max": dec_text(hi), "step": dec_text(step)}


def grid_values(grid: dict) -> list:
    """(text, JSON value, float) for every grid value, low to high."""
    out = []
    d = grid["d"]
    for i in range(grid["n"]):
        dec = Decimal(grid["m"] + i * grid["s"]).scaleb(-d)
        text = dec_text(dec)
        out.append((text, json_number(dec), float(text)))
    return out


def half_up(x: float, n: int) -> float:
    """floor(x * 10^n + 0.5 + 1e-9) / 10^n; never -0.0."""
    v = math.floor(x * 10**n + 0.5 + 1e-9) / 10**n
    return 0.0 if v == 0 else v


def round_expected(x: float, n: int) -> float:
    exact = Decimal(x)
    if exact != 0:
        int_digits = max(1, exact.copy_abs().adjusted() + 1)
        n = min(n, max(0, 15 - int_digits))
    q = (exact * (Decimal(10) ** n) + Decimal("0.5") + Decimal("1e-9")).to_integral_value(rounding=decimal.ROUND_FLOOR)
    v = float(q.scaleb(-n))
    return 0.0 if v == 0 else v


def number_answer(seed: str, grid: dict) -> dict:
    """number, expected, confidence and the listed probabilities."""
    values = grid_values(grid)
    n = len(values)
    if n == 1:
        text, value, f = values[0]
        return {"type": "number", "number": value, "expected": round_expected(f, max(2, grid["d"])), "confidence": 1.0, "probabilities": {text: 1.0}}
    stream = Stream(seed)
    peak = int(stream.next() * n) % n
    width = 0.4 + stream.next() * 2.0
    weights = []
    for i in range(n):
        dist = abs(i - peak)
        w = math.exp(-dist / width)
        if dist <= 12:
            w *= 0.85 + 0.3 * stream.next()
        weights.append(w)
    total = math.fsum(weights)
    p = [w / total for w in weights]
    k = max(range(n), key=lambda i: (p[i], -i))
    expected = round_expected(math.fsum(values[i][2] * p[i] for i in range(n)), max(2, grid["d"]))
    listed = [i for i in range(n) if p[i] > 0.001 or i == k]
    rounded = {i: half_up(p[i], 3) for i in listed}
    guard = 0
    while math.fsum(rounded.values()) > 1 + 1e-9 and guard < 10000:
        guard += 1
        others = [i for i in listed if i != k and rounded[i] > 0]
        target = min(others, key=lambda i: (-p[i], i)) if others else k
        rounded[target] = half_up(rounded[target] - 0.001, 3)
    return {
        "type": "number",
        "number": values[k][1],
        "expected": expected,
        "confidence": half_up(p[k], 2),
        "probabilities": {values[i][0]: rounded[i] for i in listed},
    }


def normalise_question(q: dict, grid: dict | None = None) -> dict:
    qtype = q["type"]
    if qtype == "number":
        return {"type": "number", "instructions": q.get("instructions"), "min": grid["min"], "max": grid["max"], "step": grid["step"]}
    if qtype in PLACE_TYPES:
        out = {"type": qtype, "instructions": q.get("instructions"), "image": q.get("image"), "units": q.get("units") or "pixels"}
        if q.get("max") is not None:
            out["max"] = q["max"]  # several places
        return out
    if qtype == "noul":
        qtype = "truth"  # the same question as truth, answered in its own name
    out = {"type": qtype, "instructions": q.get("instructions")}
    criteria = q.get("criteria")
    if qtype == "score" and isinstance(criteria, dict):
        criteria = [criteria[str(i)] for i in range(len(criteria))]
    if qtype == "truth":
        crit = criteria or {}
        criteria = {k: crit.get(k) for k in ("true", "false") if crit.get(k) is not None}
    out["criteria"] = criteria
    return out


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _num(value) -> str:
    # As the API writes a number in a message: printf's %g.
    return "%g" % value


def validate_videos(videos: list, image_ids: list) -> list:
    """The frames form of the API's videos field: the form's checks in the API's order and
    words, then the request's frame count, then each frame inside the window (base64, its
    type from its first bytes), then the window holding a frame. Returns, per video, its id,
    a digest, the frames read by the sampling rule and the seconds of its window."""
    seen = set(image_ids)
    for i, video in enumerate(videos):
        if not isinstance(video, dict):
            raise refuse_422([entry("dict_type", ["body", "videos", i], "Input should be a valid dictionary", video)])
        vid = video.get("id")
        if not isinstance(vid, str) or not vid:
            raise usage_error("Video %d needs an id." % i)
        if vid in seen:
            raise usage_error("Video id %r is used twice (ids are unique across images and videos)." % vid)
        seen.add(vid)

        def refuse(problem, vid=vid):
            return usage_error("Video %r: %s." % (vid, problem), "video '%s': %s" % (vid, problem))

        forms = [name for name in ("data", "url", "frames") if video.get(name) is not None]
        if len(forms) != 1:
            raise refuse("needs exactly one of data, url and frames")
        if forms != ["frames"]:
            raise refuse("video files and URLs are not taken: send the video as frames with timestamps")
        frames = video["frames"]
        if not isinstance(frames, list) or not frames:
            raise refuse("frames must be a non-empty list of frames")
        if len(frames) > MAX_SENT_FRAMES:
            raise refuse("%d frames, the limit is %d" % (len(frames), MAX_SENT_FRAMES))
        before = None
        for n, frame in enumerate(frames):
            if not isinstance(frame, dict):
                raise refuse("frame %d must be an object with t, media_type and data" % n)
            t = frame.get("t")
            if not _finite(t) or t < 0:
                raise refuse("frame %d: t must be a number of seconds, at least 0" % n)
            if before is not None and t <= before:
                raise refuse("frame %d: t must be larger than the frame before it (%s)" % (n, _num(before)))
            before = t
            if not isinstance(frame.get("data"), str):
                raise refuse("frame %d: data must be base64 text" % n)
            if frame.get("media_type") not in MEDIA_TYPES:
                raise refuse("frame %d: media_type must be one of %s" % (n, ", ".join(MEDIA_TYPES)))
        fps = video.get("fps")
        if fps is not None and (not _finite(fps) or not 0 < fps <= MAX_VIDEO_FPS):
            raise refuse("fps must be a number above 0 and at most %d" % MAX_VIDEO_FPS)
        for name in ("start", "end"):
            value = video.get(name)
            if value is not None and (not _finite(value) or value < 0):
                raise refuse("%s must be a number of seconds, at least 0" % name)
        if video.get("end") is not None and video["end"] <= (video.get("start") or 0):
            raise refuse("end must be after start")
    sent = sum(len(v["frames"]) for v in videos)
    if sent > MAX_FRAMES_REQUEST:
        raise usage_error(
            "The videos send %d frames, the limit is %d a request (over all its videos): send fewer frames, a shorter window or a lower rate." % (sent, MAX_FRAMES_REQUEST),
            "%d frames sent, the limit is %d a request" % (sent, MAX_FRAMES_REQUEST),
        )
    out = []
    for video in videos:
        vid = video["id"]
        frames = video["frames"]
        start = video.get("start") or 0
        end = video.get("end")
        fps = video.get("fps") or DEFAULT_VIDEO_FPS
        digest = hashlib.sha256()
        inside = [(n, f) for n, f in enumerate(frames) if start <= f["t"] and (end is None or f["t"] < end)]
        if not inside:
            raise usage_error(
                "Video %r: no frame lies in the window [%s, %s) s (the frames run from %s to %s s)." % (vid, _num(start), "end" if end is None else _num(end), _num(frames[0]["t"]), _num(frames[-1]["t"])),
                "video '%s': the window holds no frame" % vid,
            )
        read = []
        for n, f in inside:
            try:
                blob = base64.b64decode(f["data"], validate=True)
            except (binascii.Error, ValueError):
                raise usage_error("Video %r: frame %d: data is not valid base64." % (vid, n)) from None
            if len(blob) > MAX_IMAGE_BYTES:
                raise usage_error("Video %r: frame %d is larger than %d bytes." % (vid, n, MAX_IMAGE_BYTES))
            if sniff(blob) != f["media_type"]:
                raise usage_error("Video %r: frame %d: the data is not %s." % (vid, n, f["media_type"]))
            if not read or f["t"] >= read[-1] + 1.0 / fps - 0.001:
                read.append(f["t"])
                digest.update(("%r:" % f["t"]).encode() + hashlib.sha256(blob).digest())
        gaps = sorted(b["t"] - a["t"] for a, b in zip(frames, frames[1:]))
        median = gaps[len(gaps) // 2] if gaps else 0
        last = frames[-1]["t"] + median
        seconds = min(end, last) - max(start, inside[0][1]["t"]) if end is not None else last - max(start, inside[0][1]["t"])
        out.append({"id": vid, "digest": digest.hexdigest(), "frames_read": len(read), "seconds": seconds})
    return out


def validate_images(images: list, resolver=None) -> list:
    seen = set()
    out = []
    for i, img in enumerate(images):
        if not isinstance(img, dict):
            raise usage_error("Image %d must be an object." % i)
        iid = img.get("id")
        if not isinstance(iid, str) or iid == "":
            raise usage_error("Image %d needs an id." % i)
        if len(iid) > MAX_IMAGE_ID_CHARS:
            # At most 64 characters, counted as code points.
            raise usage_error("Image %d: its id is longer than %d characters." % (i, MAX_IMAGE_ID_CHARS), "image %d: an id of %d characters" % (i, len(iid)))
        if iid in seen:
            raise usage_error("Duplicate image id: %s" % iid)
        seen.add(iid)
        data, url, media_type, upload_id = img.get("data"), img.get("url"), img.get("media_type"), img.get("upload_id")
        if upload_id is not None:
            if data is not None or url is not None:
                raise usage_error("Image %s needs exactly one of data, url or upload_id." % iid)
            if media_type is not None and media_type not in MEDIA_TYPES:
                raise usage_error("Image %s: media_type must be image/jpeg, image/png or image/webp." % iid)
            if resolver is None:
                raise upload_fault("upload_not_found", iid, upload_id, "was not found.")
            raw = resolver(iid, upload_id, media_type)
            out.append({"id": iid, "digest": hashlib.sha256(raw).hexdigest()})
            continue
        if (data is None) == (url is None):
            raise usage_error("Image %s needs exactly one of data or url." % iid)
        if url is not None:
            if not isinstance(url, str) or not url.startswith("https://"):
                raise usage_error("Image %s: only https URLs are accepted." % iid)
            refused = url_refusal(url)
            if refused is not None:
                raise upload_fault_url(iid, refused)
            out.append({"id": iid, "digest": hashlib.sha256(url.encode("utf-8")).hexdigest()})
            continue
        if not isinstance(data, str):
            raise usage_error("Image %s: data must be a base64 string." % iid)
        if media_type not in MEDIA_TYPES:
            raise usage_error("Image %s: media_type must be image/jpeg, image/png or image/webp." % iid)
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise usage_error("Image %s: data is not valid base64." % iid)
        if len(raw) > MAX_IMAGE_BYTES:
            raise usage_error("Image %s is larger than 10 MB." % iid)
        found = sniff(raw)
        if found != media_type:
            raise usage_error("Image %s could not be decoded as %s." % (iid, media_type))
        out.append({"id": iid, "digest": hashlib.sha256(raw).hexdigest()})
    return out


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


def blocked_address(host: str) -> bool:
    """A literal IP in a blocked range (IPv4-mapped and NAT64
    addresses by the IPv4 address they embed). A DNS name is not judged here."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr in NAT64:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return any(addr.version == net.version and addr in net for net in BLOCKED_NETWORKS)


def url_refusal(url: str) -> str | None:
    """The fixed rules the API applies to an https image URL before any
    network work (the fetch itself is not mocked), or None when it passes them."""
    if len(url) > MAX_URL_CHARS:
        return "the URL is %d characters, over the limit of %d" % (len(url), MAX_URL_CHARS)
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return "the URL carries user information"
    try:
        port = parts.port
    except ValueError:
        return "the URL's port is not a number"
    if port not in (None, 443):
        return "the URL uses port %d; only 443 is taken" % port
    host = parts.hostname or ""
    if not host:
        return "the URL has no host"
    if blocked_address(host):
        return "the URL resolves to a private address"
    return None


def upload_fault_url(iid: str, reason: str) -> Refused:
    return typed_error(400, "image_url_refused", "Image '%s': %s." % (iid, reason))


def question_tokens(q: dict, grid: dict | None = None) -> int:
    n = 4 + math.ceil(len(compact(q).encode("utf-8")) / 4)
    if grid is not None:
        n += 2 * grid["n"]
    return n


def count_tokens(state_text: str, questions: dict, images: list, grids: dict | None = None) -> int:
    """The mock's own approximation: about four bytes of text per token."""
    grids = grids or {}
    n = 8 + math.ceil(len(state_text.encode("utf-8")) / 4)
    for qid, q in questions.items():
        n += question_tokens(q, grids.get(qid))
    n += IMAGE_TOKENS * len(images)
    return n


# --------------------------------------------------------------------------- answers


class Stream:
    """Deterministic uniform numbers in (0, 1] from a seed."""

    def __init__(self, seed: str):
        self.seed = seed.encode("utf-8")
        self.counter = 0

    def next(self) -> float:
        digest = hashlib.sha256(self.seed + b"/" + str(self.counter).encode()).digest()
        self.counter += 1
        return (int.from_bytes(digest[:8], "big") + 1) / 2.0**64


def distribution(stream: Stream, k: int) -> list:
    peak = int(stream.next() * k) % k
    weights = []
    for i in range(k):
        w = stream.next()
        if i == peak:
            w += 0.5 + stream.next() * 1.5
        weights.append(w)
    total = sum(weights)
    return [w / total for w in weights]


def round_probabilities(raw: list) -> list:
    """Round each entry to two decimals, then move the largest entries by 0.01 until
    the list sums to 1. A moved entry keeps the raw float of the addition, so a value
    such as 0.8200000000000001 can appear, as on the hosted API."""
    rounded = [round(p, 2) for p in raw]
    steps = int(round((1.0 - sum(rounded)) * 100))
    order = sorted(range(len(raw)), key=lambda i: (-raw[i], i))
    i = 0
    guard = 0
    while steps != 0 and guard < 10 * len(raw) + 100:
        guard += 1
        idx = order[i % len(order)]
        i += 1
        if steps > 0:
            rounded[idx] = rounded[idx] + 0.01
            steps -= 1
        elif rounded[idx] >= 0.01:
            rounded[idx] = rounded[idx] - 0.01
            steps += 1
    return rounded


def answer(seed: str, q: dict) -> dict:
    stream = Stream(seed)
    qtype = q["type"]
    if qtype == "truth":
        return {"type": "truth", "truth": round(stream.next(), 2)}
    if qtype == "choice":
        names = list(q["criteria"].keys())
        k = len(names)
        if k == 1:
            return {"type": "choice", "choice": names[0], "confidence": 1.0, "probabilities": {names[0]: 1.0}}
        raw = distribution(stream, k)
        best = max(range(k), key=lambda i: (raw[i], -i))
        rounded = round_probabilities(raw)
        confidence = round((k * raw[best] - 1) / (k - 1), 2)
        order = sorted(range(k), key=lambda i: names[i])
        return {
            "type": "choice",
            "choice": names[best],
            "confidence": confidence,
            "probabilities": {names[i]: rounded[i] for i in order},
        }
    levels = q["criteria"]
    k = len(levels)
    legend = {str(i): levels[i] for i in range(k)}
    if k == 1:
        return {"type": "score", "score": 0.0, "confidence": 1.0, "legend": legend, "probabilities": {"0": 1.0}}
    raw = distribution(stream, k)
    mode = max(range(k), key=lambda i: (raw[i], -i))
    d = math.floor(k * k / 4) / k
    spread = sum(raw[i] * abs(i - mode) for i in range(k))
    confidence = min(1.0, max(0.0, 1 - spread / d))
    rounded = round_probabilities(raw)
    return {
        "type": "score",
        "score": round(sum(i * raw[i] for i in range(k)), 2),
        "confidence": round(confidence, 2),
        "legend": legend,
        "probabilities": {str(i): rounded[i] for i in range(k)},
    }


def needs_model(q: dict, grid: dict | None = None) -> bool:
    if q["type"] == "choice":
        return len(q["criteria"]) > 1
    if q["type"] == "score":
        return len(q["criteria"]) > 1
    if q["type"] == "number":
        return grid is not None and grid["n"] > 1
    return True


def safety_of(req: dict, request_seed: str) -> tuple:
    """(header value, body object) for the safety check the hosted API runs."""
    grids = req.get("grids") or {}
    if not any(needs_model(q, grids.get(qid)) for qid, q in req["questions"].items()):
        return 'unchecked;reason="no-model-question"', {"checked": False, "reason": "no-model-question"}
    stream = Stream("safety/" + request_seed)
    harm = stream.next() * 0.05
    self_harm = stream.next() * 0.02
    header = "harm=%.3f, self_harm=%.3f" % (harm, self_harm)
    body = {"checked": True, "harm": round(harm, 4), "self_harm": round(self_harm, 4), "flagged": []}
    return header, body


def usd(amount: float) -> float:
    return round(amount, 9)


def cost_of(model: str, tokens: int, booster_usd: float = 0.0, price_factor: float = 1.0) -> dict:
    """usage.cost: usd is model_usd plus boosters_usd, each at most 9 decimals; the
    boosters at each one's price per picture read (run_boosters)."""
    base = model.split("@", 1)[0]
    family = FAMILY_OF.get(base) or FINE_TUNED_BASES.get(base) or base
    per_million = PRICE_PER_MILLION.get(family, 0.0) + (FINE_TUNED_PREMIUM_PER_MILLION if "@" in model else 0.0)
    model_usd = usd(tokens * per_million / 1e6 * price_factor)
    boosters_usd = usd(booster_usd)
    return {"usd": usd(model_usd + boosters_usd), "model_usd": model_usd, "boosters_usd": boosters_usd, "free_tier": False}


def family_of(model: str) -> str:
    base = model.split("@", 1)[0]
    return FAMILY_OF.get(base) or FINE_TUNED_BASES.get(base) or base


def batch_price_factor(model: str) -> float:
    """The share of the live price a batch line of model pays: its family's, as its
    /v1/models entry states it."""
    return BATCH_PRICE_FACTORS.get(family_of(model), 1.0)


def model_prices(family: str, fine_tuned: bool = False) -> dict:
    """A /v1/models entry's prices, named as the API names them (USD per million
    tokens). The mock bills every token alike, so images and video frames cost what
    input does; a fine-tuned model adds its premium and states no batch factor."""
    price = round(PRICE_PER_MILLION.get(family, 0.0) + (FINE_TUNED_PREMIUM_PER_MILLION if fine_tuned else 0.0), 9)
    out = {"price_per_million_input": price, "price_per_million_output": 0, "price_per_million_image": price, "price_per_million_video": price}
    if not fine_tuned:
        out["batch_price_factor"] = BATCH_PRICE_FACTORS[family]
    return out


def decide(req: dict, legacy_noul: bool = False, price_factor: float = 1.0) -> tuple:
    seed_of = {"model": req["model"], "state": req["state_text"], "images": [[i["id"], i["digest"]] for i in req["images"]]}
    if req.get("videos"):
        seed_of["videos"] = [[v["id"], v["digest"]] for v in req["videos"]]
    base = compact(seed_of)
    answers = {}
    grids = req.get("grids") or {}
    types = req.get("answer_types") or {}
    for qid, q in req["questions"].items():
        seed = hashlib.sha256((base + "\n" + compact(q) + "\n" + json.dumps(q, sort_keys=True)).encode("utf-8")).hexdigest()
        if q["type"] == "number":
            out = number_answer(seed, grids[qid])
        elif q["type"] in PLACE_TYPES:
            # One image and no "image": the request's image.
            out = place_answer(seed, q if q.get("image") is not None else dict(q, image=req["images"][0]["id"]))
        else:
            out = answer(seed, q)
        if out["type"] == "truth" and (types.get(qid) == "noul" or legacy_noul):
            out = {"type": "noul", "noul": out["truth"]}
        answers[qid] = out
    body = {"model": req["model"], "answers": answers}
    if req.get("evidence") is not None:
        # Only a request that named boosters carries these.
        body["evidence"] = req["evidence"]
    body["usage"] = {"input_tokens": req["tokens"], "output_tokens": 0}
    if req.get("evidence") is not None:
        body["usage"]["booster_tokens"] = req["booster_tokens"]
        body["usage"]["boosters"] = req["booster_calls"]
    if req.get("videos"):
        # Only a request with videos carries these (and warnings, which this mock sends only from a scenario).
        body["usage"]["video_frames"] = sum(v["frames_read"] for v in req["videos"])
        body["usage"]["video_seconds"] = sum(v["seconds"] for v in req["videos"])
    body["usage"]["cost"] = cost_of(req["model"], req["tokens"], req.get("booster_usd") or 0.0, price_factor)
    request_seed = hashlib.sha256((base + compact(req["questions"])).encode("utf-8")).hexdigest()
    return body, request_seed


# --------------------------------------------------------------------------- boosters


def boosters_malformed(reason: str) -> Refused:
    return usage_error("Invalid request.", "boosters: " + reason)


def check_boosters(raw, image_ids: list, conf: dict, collections=None) -> list:
    """The request's boosters as the API checks them before any runs: the list's shape
    (the 400 api_usage_error "Invalid request.", the reason in x-decisionnode-error), then
    each entry in order: a name that is no booster (400 booster_unknown), a booster turned
    off for the workspace (403 booster_not_enabled), images it cannot read (400
    booster_input_mismatch). null and [] mean no boosters."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise boosters_malformed("must be a list of booster names or objects")
    if len(raw) > MAX_BOOSTERS:
        raise boosters_malformed("at most %d boosters, got %d" % (MAX_BOOSTERS, len(raw)))
    out = []
    seen = set()
    for i, item in enumerate(raw):
        if isinstance(item, str):
            b = {"booster": item, "images": None, "require": False, "options": {}, "index": i}
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            # The object's key is name (it was booster); any other key is refused.
            unknown = [k for k in item if k not in BOOSTER_KEYS]
            if unknown:
                raise boosters_malformed("boosters[%d]: '%s' is not a key of a booster object" % (i, unknown[0]))
            images, require, options = item.get("images"), item.get("require"), item.get("options")
            if images is not None and not (isinstance(images, list) and all(isinstance(x, str) for x in images)):
                raise boosters_malformed("boosters[%d].images must be a list of image ids" % i)
            if require is not None and not isinstance(require, bool):
                raise boosters_malformed("boosters[%d].require must be true or false" % i)
            if options is not None and not isinstance(options, dict):
                raise usage_error("Invalid request.", "boosters[%d]: \"options\" must be an object" % i)
            b = {"booster": item["name"], "images": images, "require": bool(require), "options": dict(options or {}), "index": i}
        else:
            raise boosters_malformed("boosters[%d] is neither a booster name nor an object with name" % i)
        # A bare name is judged with no options: face_recognition and bynn_segment need one.
        problem = options_problem(b)
        if problem is not None:
            raise usage_error("Invalid request.", "boosters[%d]: %s" % (i, problem))
        if b["booster"] in seen:
            raise boosters_malformed("'%s' is named twice" % b["booster"])
        seen.add(b["booster"])
        out.append(b)
    disabled = set(conf.get("disabled") or [])
    for b in out:
        name = b["booster"]
        if name not in BOOSTERS:
            raise typed_error(400, "booster_unknown", "Unknown booster: %s" % name, "'%s' is not a booster; the boosters are %s" % (name, ", ".join(sorted(BOOSTERS))))
        if name in disabled:
            raise typed_error(403, "booster_not_enabled", "The booster '%s' is not enabled for this workspace." % name)
        if not image_ids:
            raise typed_error(400, "booster_input_mismatch", "The booster '%s' needs at least one image." % name)
        for iid in b["images"] or []:
            if iid not in image_ids:
                raise typed_error(400, "booster_input_mismatch", "Booster '%s': '%s' is not an image of the request." % (name, iid))
        if name == "face_recognition":
            check_face_recognition(b, collections)
    return out


def check_face_recognition(b: dict, collections) -> None:
    """face_recognition's collection (a public collection's name or the id of one of the
    workspace's own), resolved before any model runs, so an unknown one is its 404. Its
    options were judged with the list (options_problem)."""
    collection = b["options"]["collection"]
    if collections is None:
        raise typed_error(404, "collection_not_found", "Collection not found: %s." % collection)
    b["collection"] = collections(collection)
    b["threshold"] = b["options"].get("threshold", FACE_THRESHOLD)


def cut_name(text: str) -> str:
    """A name or a key as a reason quotes it: its first 64 bytes, on a character boundary."""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_BOOSTER_NAME_BYTES:
        return text
    return raw[:MAX_BOOSTER_NAME_BYTES].decode("utf-8", "ignore")


def whole_in(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def number_in(value, low: float, high: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


def prompt_of(value):
    """options.prompt as read, judged once the booster is known: a string of at most
    2,000 characters, an array of at most 4 strings, or None for anything else."""
    if isinstance(value, str):
        return value if len(value) <= MAX_PROMPT_CHARS else None
    if isinstance(value, list) and len(value) <= MAX_PHRASES and all(isinstance(p, str) for p in value):
        return list(value)
    return None


OPTION_RULES = {
    "collection": (lambda v: isinstance(v, str), BAD_COLLECTION),
    "max_instances": (lambda v: whole_in(v, 1, 16), BAD_MAX_INSTANCES),
    "min_score": (lambda v: number_in(v, 0, 1), BAD_MIN_SCORE),
    "polygon_points": (lambda v: whole_in(v, 3, 64), BAD_POLYGON_POINTS),
    "depth": (lambda v: isinstance(v, bool), BAD_DEPTH),
    "grid_size": (lambda v: whole_in(v, 2, 16), BAD_GRID_SIZE),
    "threshold": (lambda v: number_in(v, 0, 1), BAD_THRESHOLD),
}


def options_problem(b: dict):
    """Why a booster's options are refused, as the API words it (its 400 api_usage_error,
    the reason after "boosters[<i>]: "), else None. First each key in the order sent: a
    value of the wrong type or out of range, a key no booster takes; then what the named
    booster takes or needs (a name that is no booster takes any of the known keys: it is
    booster_unknown later); then Segment Vision's phrases, kept cleaned in b["phrases"]."""
    options = b["options"]
    problem = None
    for key, value in options.items():
        if key == "prompt":
            continue  # judged once the booster is known
        rule = OPTION_RULES.get(key)
        if rule is None:
            found = "\"options\": '%s' is not an option" % cut_name(key)
        else:
            found = None if rule[0](value) else rule[1]
        if problem is None:
            problem = found
    if problem is not None:
        return problem
    name = b["booster"]
    if name not in BOOSTERS:
        return None

    def not_an_option(key):
        return "\"options\": '%s' is not an option" % key

    searches = name == "face_recognition"
    if searches and "collection" not in options:
        return "\"options\": 'collection' is required"
    for key in ("collection", "threshold"):
        if not searches and key in options:
            return not_an_option(key)
    if "prompt" not in options:
        if name == "bynn_segment":
            return "\"options\": 'prompt' is required"
    elif name != "bynn_segment":
        return not_an_option("prompt")
    if name != "bynn_depth" and "grid_size" in options:
        return not_an_option("grid_size")
    if name != "bynn_segment":
        for key in ("max_instances", "min_score", "polygon_points", "depth"):
            if key in options:
                return not_an_option(key)
        return None
    prompt = prompt_of(options["prompt"])
    if prompt is None:
        return BAD_PHRASES
    phrases = segment_phrases([prompt] if isinstance(prompt, str) else prompt)
    if isinstance(phrases, str):
        return phrases
    b["phrases"] = phrases
    return None


def separates(c: str) -> bool:
    """A character no phrase keeps: a control character, one that reorders text, or the
    model's separator."""
    o = ord(c)
    return unicodedata.category(c) == "Cc" or c == "|" or o in (0x061C, 0x200E, 0x200F) or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069


def segment_phrases(raw: list):
    """Segment Vision's phrases cleaned as the API cleans them, or the reason they are
    refused: 1 to 4, each at most 120 characters as sent; separators become spaces, white
    space folds, the ends are trimmed; empty phrases and repeats (by their lower case)
    drop; none left names nothing."""
    if not raw or len(raw) > MAX_PHRASES or any(len(p) > MAX_PHRASE_CHARS for p in raw):
        return BAD_PHRASES
    kept, seen = [], set()
    for phrase in raw:
        cleaned = " ".join("".join(" " if separates(c) else c for c in phrase).split())
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        kept.append(cleaned)
    return kept or NO_PHRASE


def booster_plan(boosters: list, conf: dict) -> list:
    """What each booster will report: its status (ok unless the session's settings say
    timeout or error; skipped when it is given no image to read) and its time."""
    status_of = conf.get("status") or {}
    plan = []
    for b in boosters:
        name = b["booster"]
        status = status_of.get(name, "ok")
        if status == "ok" and b["images"] == []:
            status = "skipped"
        latency = {"ok": BOOSTERS[name]["latency_ms"], "timeout": BOOSTER_BUDGET_MS, "error": BOOSTER_ERROR_MS}.get(status, 0)
        plan.append(dict(b, status=status, latency_ms=latency))
    return plan


def booster_header(plan: list) -> str:
    """x-decisionnode-boosters: each booster's status and time, in the request's order."""
    return ",".join("%s=%s:%d" % (p["booster"], p["status"], p["latency_ms"]) for p in plan)


def face_recognition_reading(b: dict, iid: str, stream) -> dict:
    """One or two faces, the first a close match when the collection has anyone to match,
    the second a weak one; a match is kept at the request's threshold or above. As a
    API answers from 1.16's fields on: every match names its collection (collection_name: an
    own collection's display name, a public one's id), an own item's match carries its
    metadata when it has any, and the result of an own collection its display name."""
    col = b["collection"]
    faces = 1 + int(stream.next() * 2)
    matches = []
    for n in range(faces):
        score = round(0.7 + stream.next() * 0.29, 4) if n == 0 else round(stream.next() * 0.6, 4)
        xs = sorted((stream.next(), stream.next()))
        ys = sorted((stream.next(), stream.next()))
        box = {"x1": int(xs[0] * PLACE_WIDTH), "y1": int(ys[0] * PLACE_HEIGHT), "x2": int(xs[1] * PLACE_WIDTH), "y2": int(ys[1] * PLACE_HEIGHT)}
        pick = stream.next()
        if score < b["threshold"]:
            continue
        if col["kind"] == "own":
            if not col["items"]:
                continue
            item = col["items"][int(pick * len(col["items"])) % len(col["items"])]
            match = {"box": box, "score": score, "collection": col["id"], "reference": item["reference"]}
            if item.get("label") is not None:
                match["label"] = item["label"]
            match["collection_name"] = col["name"] or col["id"]
            if item.get("metadata"):
                match["metadata"] = item["metadata"]
        elif col["kind"] == "celebrities":
            ref, who = CELEBRITIES[int(pick * len(CELEBRITIES)) % len(CELEBRITIES)]
            match = {"box": box, "score": score, "collection": col["id"], "reference": ref, "name": who, "collection_name": col["id"]}
        else:
            continue
        matches.append(match)
    matches.sort(key=lambda m: m["score"], reverse=True)
    out = {"image": iid, "faces": faces, "matches": matches}
    if col["kind"] == "own" and col["name"]:
        out["collection_name"] = col["name"]
    return out


def depth_reading(b: dict, iid: str, stream) -> dict:
    """Depth Vision: a grid_size by grid_size grid of distances in meters, row by row from
    the top left; each of the seven directions, left to right, the nearest cell of its
    column (null past 15 m, open space), ahead the nearest of the middle third; the
    totals over the grid. Two decimals, as the API sends them."""
    n = b["options"].get("grid_size", DEPTH_GRID)
    grid = [[round(0.5 + stream.next() * 19.5, 2) for _ in range(n)] for _ in range(n)]
    cells = sorted(v for row in grid for v in row)
    half = len(cells) // 2
    median = cells[half] if len(cells) % 2 else round((cells[half - 1] + cells[half]) / 2, 2)

    def nearest(cols):
        m = min(grid[r][c] for r in range(n) for c in cols)
        return m if m <= 15 else None

    directions = [nearest([min(n - 1, int((j + 0.5) * n / 7))]) for j in range(7)]
    ahead = nearest(range(n // 3, n - n // 3))
    return {
        "image": iid,
        "ahead": ahead,
        "directions": directions,
        "ground": stream.next() < 0.7,
        "grid_rows": n,
        "grid_cols": n,
        "grid": grid,
        "depth_min": cells[0],
        "depth_max": cells[-1],
        "depth_median": median,
        "width": PLACE_WIDTH,
        "height": PLACE_HEIGHT,
    }


def outline(cx: float, cy: float, rx: float, ry: float, points: int) -> list:
    """An ellipse's outline as [x, y] fractions, four decimals."""
    return [[round(cx + rx * math.cos(2 * math.pi * k / points), 4), round(cy + ry * math.sin(2 * math.pi * k / points), 4)] for k in range(points)]


def segment_reading(b: dict, iid: str, stream) -> dict:
    """Segment Vision: 0 to 2 matches of each phrase, each an ellipse inside its box (the
    outline at polygon_points, coordinates as fractions from the top left), every second
    one with an opening above and right of its middle, whose offset, as the model writes
    it, is the opening's centroid less the centre of the match's box; at most
    max_instances, by score, each at min_score or above.
    With depth on, each match's distances and size in meters (a pinhole at 65 degrees),
    depth_source bynn_depth; with it off those are null."""
    options = dict(SEGMENT_DEFAULTS, **{k: v for k, v in b["options"].items() if k in SEGMENT_DEFAULTS})
    depth = options["depth"]
    per_meter = 2 * math.tan(math.radians(SEGMENT_HFOV_DEG / 2))
    instances = []
    for phrase in b["phrases"]:
        for k in range(int(stream.next() * 3)):
            score = round(options["min_score"] + stream.next() * (1 - options["min_score"]), 3)
            w, h = 0.1 + stream.next() * 0.3, 0.1 + stream.next() * 0.3
            cx, cy = round(w / 2 + stream.next() * (1 - w), 4), round(h / 2 + stream.next() * (1 - h), 4)
            inst = {
                "label": phrase,
                "score": score,
                "box": [round(cx - w / 2, 4), round(cy - h / 2, 4), round(cx + w / 2, 4), round(cy + h / 2, 4)],
                "polygon": outline(cx, cy, w / 2, h / 2, options["polygon_points"]),
                "area": round(math.pi * w * h / 4, 5),
                "centroid": [cx, cy],
                "opening": None,
                "depth": None,
                "size_m": None,
            }
            hole = k % 2 == 0
            if hole:
                box = inst["box"]
                ox, oy = round(cx + w / 10, 4), round(cy - h / 8, 4)
                inst["opening"] = {
                    "polygon": outline(ox, oy, w / 4, h / 4, options["polygon_points"]),
                    "centroid": [ox, oy],
                    "width": round(w / 2, 4),
                    "height": round(h / 2, 4),
                    "offset": [round(ox - (box[0] + box[2]) / 2, 4), round(oy - (box[1] + box[3]) / 2, 4)],
                    "area": round(math.pi * w * h / 16, 5),
                }
            if depth:
                meters = round(1 + stream.next() * 19, 2)
                through = round(meters + 1 + stream.next() * 5, 2)
                inst["depth"] = {"median": meters, "opening_median": through if hole else None, "opening_rim_median": meters if hole else None}
                size = {"width": round(meters * per_meter * w, 2), "height": round(meters * per_meter * h * PLACE_HEIGHT / PLACE_WIDTH, 2), "opening": None}
                if hole:
                    size["opening"] = {"width": round(size["width"] / 2, 2), "height": round(size["height"] / 2, 2)}
                inst["size_m"] = size
            instances.append(inst)
    instances.sort(key=lambda i: i["score"], reverse=True)
    return {
        "image": iid,
        "width": PLACE_WIDTH,
        "height": PLACE_HEIGHT,
        "prompts": b["phrases"],
        "instances": instances[: options["max_instances"]],
        "depth_source": "bynn_depth" if depth else None,
        "assumed_hfov_deg": SEGMENT_HFOV_DEG,
    }


def reading(name: str, iid: str, digest: str, b: dict | None = None) -> dict:
    """A booster's fixed reading of one image, derived from the image's id and bytes."""
    stream = Stream("booster/%s/%s/%s" % (name, iid, digest))
    if name == "face_recognition":
        return face_recognition_reading(b, iid, stream)
    if name == "bynn_depth":
        return depth_reading(b, iid, stream)
    if name == "bynn_segment":
        return segment_reading(b, iid, stream)
    if name == "age":
        if stream.next() < 0.2:
            return {"image": iid, "faces": 0}
        years = 18 + int(stream.next() * 50)
        return {
            "image": iid,
            "age_years": years,
            "low": years - 3 - int(stream.next() * 4),
            "high": years + 3 + int(stream.next() * 4),
            "confidence": round(0.55 + stream.next() * 0.4, 2),
            "faces": 1 + int(stream.next() * 3),
        }
    generated = round(stream.next(), 2)
    confidence = round(0.5 + stream.next() * 0.45, 2)
    top = None
    if generated > LIKELY_GENERATED:
        top = {"name": GENERATORS[int(stream.next() * len(GENERATORS)) % len(GENERATORS)], "probability": round(stream.next() * generated, 2)}
    # Read from the pixels only: the mock sees no Content Credentials, so no signer
    # and no "c2pa" field.
    return {"image": iid, "generated": generated, "confidence": confidence, "method": "inference", "top_generator": top, "c2pa_signer": None}


def run_boosters(plan: list, images: list) -> tuple:
    """(evidence, booster calls billed, booster tokens, what they cost in USD). A required
    booster that timed out or failed refuses the request with the 529 booster_unavailable."""
    if not plan:
        return None, 0, 0, 0.0
    for p in plan:
        if p["require"] and p["status"] in ("timeout", "error"):
            words = "did not answer in time." if p["status"] == "timeout" else "failed."
            raise typed_error(
                529,
                "booster_unavailable",
                "The booster '%s' %s" % (p["booster"], words),
                "the required booster '%s' reported %s" % (p["booster"], p["status"]),
                {"Retry-After": "1", "retry-after-ms": "50"},
            )
    digests = {i["id"]: i["digest"] for i in images}
    evidence = {}
    calls = 0
    tokens = 0
    spent = 0.0
    for p in plan:
        name = p["booster"]
        read = p["images"] if p["images"] is not None else [i["id"] for i in images]
        billed = 0
        if p["status"] == "ok":
            results = [reading(name, iid, digests[iid], p) for iid in read]
            billed = len(results)
        elif p["status"] in ("timeout", "error"):
            # One entry per picture the booster was meant to read, with no reading, so a
            # client sees which went unread; not billed.
            results = [{"image": iid} for iid in read]
        else:
            results = []
        calls += billed
        spent += billed * BOOSTERS[name]["usd"]
        # billed_calls: one per image read with status ok; a reading reused from a cache is
        # billed too.
        evidence[name] = {"version": BOOSTERS[name]["version"], "status": p["status"], "latency_ms": p["latency_ms"], "billed_calls": billed, "results": results}
        tokens += BOOSTER_ENTRY_TOKENS + BOOSTER_READING_TOKENS * billed
    return evidence, calls, tokens, spent


# --------------------------------------------------------------------------- uploads


def upload_fault(error_type: str, iid: str, upload_id, rest: str) -> Refused:
    message = "Image '%s': upload %s %s" % (iid, upload_id if isinstance(upload_id, str) else compact(upload_id), rest)
    return typed_error(400, error_type, message)


def b64_len_ok(value, raw_len: int, text_len: int) -> bool:
    if not isinstance(value, str) or len(value) != text_len:
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == raw_len
    except (binascii.Error, ValueError):
        return False


def validate_presign(body) -> list:
    """Every file checked before anything is written."""
    if not isinstance(body, dict):
        raise usage_error("The body must be a JSON object.")
    files = body.get("files")
    if not isinstance(files, list):
        raise usage_error("files: must be a list of 1 to %d files." % UPLOAD_MAX_FILES)
    if not 1 <= len(files) <= UPLOAD_MAX_FILES:
        raise usage_error("files: 1 to %d files, got %d." % (UPLOAD_MAX_FILES, len(files)))
    out = []
    for i, f in enumerate(files):
        where = "files[%d]" % i
        if not isinstance(f, dict):
            raise usage_error("%s: must be an object." % where)
        ctype = f.get("content_type")
        if ctype not in MEDIA_TYPES:
            raise usage_error("%s.content_type: must be image/jpeg, image/png or image/webp, got %s." % (where, compact(ctype)))
        size = f.get("byte_size")
        if not is_int(size) or size < 1:
            raise usage_error("%s.byte_size: must be an integer from 1 to %d." % (where, MAX_IMAGE_BYTES))
        if size > MAX_IMAGE_BYTES:
            raise usage_error("%s.byte_size: %d is over the limit of %d bytes." % (where, size, MAX_IMAGE_BYTES))
        checksum = f.get("checksum")
        if not b64_len_ok(checksum, 16, 24):
            raise usage_error("%s.checksum: must be the file's MD5 in base64, 24 characters." % where)
        sha = f.get("sha256")
        if sha is not None and not b64_len_ok(sha, 32, 44):
            raise usage_error("%s.sha256: must be the file's SHA-256 in base64, 44 characters." % where)
        out.append({"content_type": ctype, "byte_size": size, "checksum": checksum, "sha256": sha})
    return out


KEY_SCOPE_DENIED = "This key may only make decisions."
KEY_SEARCH_DENIED = "This key may not search collections."
KEY_KEYS_ONLY = "This key may only manage keys."
KEY_PUBLISHABLE_ONLY = "A publishable key may only make decisions."
# A publishable key (suite 1.19), dn_pub_...: decisions only, from its origins or, unless
# it holds web-only, from a native app (no Origin); holding flash-only, Flash alone.
PUBLISHABLE_ORIGINS = ("https://app.example.com",)
PUBLISHABLE_FLASH_ONLY = ["decisionnode-flash"]
DECIDE_KEY_ROUTES = ("decide", "models")
KEYS_KEY_ROUTES = ("key_usage", "usage_page", "keys_create", "keys_list", "keys_update", "keys_revoke", "keys_revoke_bulk")
KEYS_ROUTES = ("keys_create", "keys_list", "keys_update", "keys_revoke", "keys_revoke_bulk")
KEY_NOT_KEYS = "Keys are managed with a keys key, made in the console."


def printable(value: str, most: int) -> str:
    """A caller's value as a message echoes it: its first most characters, any but
    printable ASCII written as \\u{hex}, then "..." when it was longer."""
    out = "".join(c if 0x20 <= ord(c) <= 0x7E else "\\u{%x}" % ord(c) for c in value[:most])
    return out + ("..." if len(value) > most else "")


def key_scope(key: str | None) -> str:
    """A key holding scope-decide is a decide key, one holding scope-keys a keys key
    (suite 1.18); every other key is a full one."""
    if (key or "").startswith("dn_pub_"):
        return "publishable"
    if "scope-decide" in (key or ""):
        return "decide"
    return "keys" if "scope-keys" in (key or "") else "full"


def may_search_collections(key: str | None) -> bool:
    """A full key always may; a decide key only when it holds may-search."""
    return key_scope(key) == "full" or "may-search" in (key or "")

# --------------------------------------------------------------------------- per-key usage (suite 1.18)

# Each workspace's customer keys and their usage, by the hour: a fixed set, so a case can
# expect its figures. A row is (key id, hour, model, channel, requests,
# free_tier_requests, input_tokens, image_tokens, video_tokens, booster_calls,
# model micro-dollars, booster micro-dollars); the routes sum them.
USAGE_KEYS = {
    "default": [
        {"id": "key_00000000000000000000000000000d01", "name": "Drone 1", "scope": "decide", "device_id": "drone-1",
         "labels": {"customer": "acme", "plan": "pro"}, "created_at": "2026-08-20T08:00:00Z", "revoked_at": None, "expires_at": None},
        {"id": "key_00000000000000000000000000000d02", "name": "Drone 2", "scope": "decide", "device_id": "drone-2",
         "labels": {"customer": "acme", "plan": "basic"}, "created_at": "2026-08-15T08:00:00Z", "revoked_at": "2026-09-20T12:00:00Z", "expires_at": None},
        {"id": "key_00000000000000000000000000000d03", "name": "Drone 3", "scope": "decide", "device_id": "drone-3",
         "labels": {"customer": "globex", "plan": "pro"}, "created_at": "2026-08-10T08:00:00Z", "revoked_at": None, "expires_at": "2026-09-25T00:00:00Z"},
        {"id": "key_00000000000000000000000000000f04", "name": "Ingest server", "scope": "full", "device_id": None,
         "labels": {}, "created_at": "2026-08-05T08:00:00Z", "revoked_at": None, "expires_at": None},
        {"id": "key_00000000000000000000000000000e05", "name": "Provisioner", "scope": "keys", "device_id": None,
         "labels": {}, "created_at": "2026-08-01T08:00:00Z", "revoked_at": None, "expires_at": None},
    ],
    "acme": [
        {"id": "key_00000000000000000000000000000a01", "name": "Acme drone", "scope": "decide", "device_id": "acme-1",
         "labels": {"customer": "acme"}, "created_at": "2026-08-01T08:00:00Z", "revoked_at": None, "expires_at": None},
    ],
}
USAGE_ROWS = [
    ("key_00000000000000000000000000000d01", "2026-08-31T23:00:00Z", MOE, "live", 2, 0, 1800, 0, 0, 0, 76, 0),
    ("key_00000000000000000000000000000d01", "2026-09-03T09:00:00Z", MOE, "live", 3, 0, 3000, 1200, 0, 1, 126, 24000),
    ("key_00000000000000000000000000000d01", "2026-09-03T10:00:00Z", MOE, "batch", 2, 0, 2000, 0, 0, 0, 59, 0),
    ("key_00000000000000000000000000000d01", "2026-09-04T14:00:00Z", FLASH, "session", 1, 0, 800, 0, 0, 0, 28, 0),
    ("key_00000000000000000000000000000d01", "2026-09-04T15:00:00Z", FLASH, "live", 4, 4, 1600, 0, 0, 0, 0, 0),
    ("key_00000000000000000000000000000d02", "2026-09-12T08:00:00Z", FLASH, "live", 5, 0, 4000, 0, 0, 0, 140, 0),
    ("key_00000000000000000000000000000d03", "2026-09-14T06:00:00Z", MOE, "live", 1, 0, 500, 300, 0, 2, 21, 48000),
    ("key_00000000000000000000000000000f04", "2026-09-02T00:00:00Z", FLASH, "batch", 100, 0, 50000, 0, 0, 0, 875, 0),
    ("key_00000000000000000000000000000f04", "2026-09-02T01:00:00Z", MOE, "live", 1, 0, 700, 0, 0, 0, 29, 0),
    ("key_00000000000000000000000000000a01", "2026-09-05T10:00:00Z", MOE, "live", 7, 0, 7000, 0, 0, 0, 294, 0),
]
USAGE_MAX_DAYS, USAGE_MAX_HOURLY_DAYS = 400, 7
USAGE_PAGE_DEFAULT, USAGE_PAGE_MAX, USAGE_PAGE_MAX_BUCKETED, USAGE_MAX_KEY_IDS = 100, 1000, 100, 100
USAGE_MAX_ROWS, USAGE_MAX_MODELS_COUNTED = 150_000, 16
USAGE_CHANNELS = ("live", "batch", "session")
KEY_ID = re.compile(r"key_[0-9a-f]{32}")
LABEL_NAME = re.compile(r"[a-z0-9_.-]{1,32}")


def usage_time(name: str, raw: str) -> float:
    """An RFC 3339 time, any offset, as seconds since the epoch."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})", raw)
    if not m:
        raise usage_error("%s is an RFC 3339 time, such as 2026-10-01T00:00:00Z." % name)
    import calendar
    try:
        y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
        datetime_check = time.strptime("%04d-%02d-%02d %02d:%02d:%02d" % (y, mo, d, h, mi, min(se, 59)), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise usage_error("%s is an RFC 3339 time, such as 2026-10-01T00:00:00Z." % name)
    t = calendar.timegm(datetime_check) + float(m.group(7) or 0)
    tz = m.group(8)
    if tz not in ("Z", "z"):
        sign = 1 if tz[0] == "+" else -1
        t -= sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return t


def usage_iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def usage_epoch(iso_text: str) -> float:
    import calendar
    return float(calendar.timegm(time.strptime(iso_text, "%Y-%m-%dT%H:%M:%SZ")))


def usage_span(asked: dict, now: float, defaults: bool) -> dict:
    """The period read: start down and end up to whole UTC hours; without them, the
    UTC month so far (one key's route only); then its bounds, as the API checks them."""
    start, end = asked.get("start"), asked.get("end")
    if start is None or end is None:
        if not defaults:
            raise usage_error("start and end are required.")
        g = time.gmtime(now)
        start = start if start is not None else float(__import__("calendar").timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0)))
        end = end if end is not None else now
    start = start - start % 3600
    end = end if end % 3600 == 0 else end - end % 3600 + 3600
    if end <= start:
        raise usage_error("end is after start.")
    if end - start > USAGE_MAX_DAYS * 86400:
        raise usage_error("A period is at most 400 days.")
    granularity = asked.get("granularity") or "none"
    if granularity == "hour" and end - start > USAGE_MAX_HOURLY_DAYS * 86400:
        raise usage_error("granularity hour is for a period of at most 7 days; use day or month.")
    return {"start": start, "end": end, "granularity": granularity, "group_by": asked.get("group_by")}


def usage_period_take(asked: dict, name: str, value: str) -> bool:
    """Takes name if it is one of the period's (start, end, granularity, group_by)."""
    if name in ("start", "end"):
        asked[name] = usage_time(name, value)
    elif name == "granularity":
        if value not in ("none", "hour", "day", "month"):
            raise usage_error("granularity is none, hour, day or month.")
        asked[name] = value
    elif name == "group_by":
        if value not in ("model", "channel"):
            raise usage_error("group_by is model or channel.")
        asked[name] = value
    else:
        return False
    return True


def usage_bucket_start(t: float, granularity: str) -> float:
    g = time.gmtime(t)
    import calendar
    if granularity == "hour":
        return t - t % 3600
    if granularity == "day":
        return t - t % 86400
    return float(calendar.timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0)))


def usage_bucket_end(t: float, granularity: str) -> float:
    import calendar
    if granularity == "hour":
        return t + 3600
    if granularity == "day":
        return t + 86400
    g = time.gmtime(t)
    y, mo = (g.tm_year + 1, 1) if g.tm_mon == 12 else (g.tm_year, g.tm_mon + 1)
    return float(calendar.timegm((y, mo, 1, 0, 0, 0)))


def usage_add(t: dict, row) -> None:
    for k, v in zip(("requests", "free_tier_requests", "input_tokens", "image_tokens", "video_tokens", "booster_calls", "model_micros", "boosters_micros"), row[4:]):
        t[k] = t.get(k, 0) + v


def usage_totals(t: dict) -> dict:
    """Totals as the API writes them: the counts, and the cost as an answer's
    usage.cost writes it, without its free_tier (free_tier_requests counts those)."""
    model, boosters = t.get("model_micros", 0), t.get("boosters_micros", 0)
    return {
        "requests": t.get("requests", 0), "free_tier_requests": t.get("free_tier_requests", 0),
        "input_tokens": t.get("input_tokens", 0), "image_tokens": t.get("image_tokens", 0),
        "video_tokens": t.get("video_tokens", 0), "booster_calls": t.get("booster_calls", 0),
        "cost": {"usd": usd((model + boosters) / 1e6), "model_usd": usd(model / 1e6), "boosters_usd": usd(boosters / 1e6)},
    }


def usage_key_object(k: dict, now: float) -> dict:
    if k["revoked_at"] is not None:
        status = "revoked"
    elif k["expires_at"] is not None and usage_epoch(k["expires_at"]) <= now:
        status = "expired"
    else:
        status = "active"
    return {"id": k["id"], "name": k["name"], "scope": k["scope"], "device_id": k["device_id"], "labels": dict(sorted(k["labels"].items())),
            "status": status, "created_at": k["created_at"], "revoked_at": k["revoked_at"], "expires_at": k["expires_at"]}


def usage_of(k: dict, span: dict, now: float) -> dict:
    """One key's usage over span: its totals and, as asked, its buckets (only those with
    usage, oldest first, the first and last cut to the period) and its groups."""
    rows = [r for r in USAGE_ROWS if r[0] == k["id"] and span["start"] <= usage_epoch(r[1]) < span["end"]]
    totals: dict = {}
    for r in rows:
        usage_add(totals, r)
    out = {"key": usage_key_object(k, now), "totals": usage_totals(totals)}
    g = span["granularity"]
    if g != "none":
        buckets: dict = {}
        for r in rows:
            b = usage_bucket_start(usage_epoch(r[1]), g)
            usage_add(buckets.setdefault(b, {}), r)
        out["buckets"] = [{"start": usage_iso(max(b, span["start"])), "end": usage_iso(min(usage_bucket_end(b, g), span["end"])), "totals": usage_totals(t)}
                          for b, t in sorted(buckets.items())]
    if span["group_by"] == "model":
        models: dict = {}
        for r in rows:
            usage_add(models.setdefault(r[2], {}), r)
        out["by_model"] = [{"model": m, "totals": usage_totals(t)} for m, t in sorted(models.items())]
    elif span["group_by"] == "channel":
        channels: dict = {c: {} for c in USAGE_CHANNELS}
        for r in rows:
            usage_add(channels[r[3]], r)
        out["by_channel"] = [{"channel": c, "totals": usage_totals(channels[c])} for c in USAGE_CHANNELS]
    return out


def usage_visible(workspace: str, scope: str) -> list:
    """The keys a caller may see: a full key every customer key of its workspace, a
    keys key the decide keys; newest first (created_at, then id)."""
    keys = USAGE_KEYS.get(workspace, [])
    if scope == "keys":
        keys = [k for k in keys if k["scope"] == "decide"]
    return sorted(keys, key=lambda k: (k["created_at"], k["id"]), reverse=True)


def usage_as_of(span: dict, now: float) -> str:
    """How far the figures are complete: up to about two minutes ago, to the minute;
    never past the period's end."""
    folded = now - 120
    return usage_iso(min(span["end"], folded - folded % 60))


def usage_cursor(k: dict) -> str:
    micros = int(usage_epoch(k["created_at"]) * 1_000_000)
    return base64.urlsafe_b64encode(("%d:%s" % (micros, k["id"][4:])).encode()).decode().rstrip("=")


def usage_parse_cursor(raw: str):
    if len(raw) > 128:
        return None
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        micros, hexid = text.split(":", 1)
        if not re.fullmatch(r"-?\d+", micros) or not re.fullmatch(r"[0-9a-f]{32}", hexid.replace("-", "")):
            return None
        return int(micros), "key_" + hexid.replace("-", "")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


# --------------------------------------------------------------------------- /v1/keys (suite 1.20)

KEY_SECRET_PREFIX = "dn_live_"
KEYS_PAGE_DEFAULT, KEYS_PAGE_MAX = 50, 200
KEYS_REVOKE_PER_CALL, KEYS_MAX_REVOKE_DEVICES, KEYS_MAX_LABELS = 10_000, 1_000, 16
# The workspace's own limits, the ceiling of a key's (the mock's).
WORKSPACE_LIMITS = {"requests_per_second": 50, "tokens_per_second": 200_000}
DAILY_CAP_MAX_USD = 1000.0
EXPIRY_MAX_DAYS = 3653


class KeyFault(Exception):
    """A refused field: its path (dotted) and why; the 422 value_error entry."""

    def __init__(self, path: str, message: str):
        super().__init__(message)
        self.path, self.message = path, message


def key_entry(kind: str, loc: list, msg: str, value) -> dict:
    return centry(kind, ["body"] + list(loc), msg, value)


def key_fault(f: KeyFault, value) -> Refused:
    return refuse_422([key_entry("value_error", f.path.split("."), f.message, value)])


def key_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise KeyFault("name", "Give the key a name.")
    if len(name) > 48:
        raise KeyFault("name", "Key names are at most 48 characters.")
    if not all(c.isalnum() or c in "_ .-" for c in name):
        raise KeyFault("name", "Use letters, digits, spaces, dots, dashes and underscores.")
    return name


def key_device_id(raw: str) -> str:
    if not raw or len(raw) > 128 or not all(0x20 <= ord(c) <= 0x7E for c in raw) or raw.startswith(" "):
        raise KeyFault("device_id", "device_id is 1 to 128 printable ASCII characters, not starting with a space.")
    return raw


def key_labels(value) -> dict:
    if not isinstance(value, dict):
        raise KeyFault("labels", "labels is an object of names to values.")
    if len(value) > KEYS_MAX_LABELS:
        raise KeyFault("labels", "A key has at most 16 labels.")
    out = {}
    for name, v in value.items():
        if not LABEL_NAME.fullmatch(name):
            raise KeyFault("labels", "A label's name is 1 to 32 of a-z, 0-9, _ . and -.")
        if not isinstance(v, str) or not 1 <= len(v) <= 64 or any(unicodedata.category(c) == "Cc" for c in v):
            raise KeyFault("labels." + name, "A label's value is 1 to 64 characters of text.")
        out[name] = v
    return out


def key_cap(value) -> int:
    usd = value.get("usd") if isinstance(value, dict) else None
    ok = isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd)
    cents = round(usd * 100) if ok else 0
    if not ok or abs(usd * 100 - cents) >= 1e-6 or cents < 1 or usd > DAILY_CAP_MAX_USD:
        raise KeyFault("daily_spend_cap", "daily_spend_cap.usd is from 0.01 to 1000, in cents, or null.")
    return cents * 10_000


def key_expiry(raw, now: float) -> str:
    fault = KeyFault("expires_at", "expires_at is a time (RFC 3339) from a minute to 10 years ahead, or null.")
    if not isinstance(raw, str):
        raise fault
    try:
        at = usage_time("expires_at", raw.strip())
    except Refused:
        raise fault
    if at < now + 60 or at > now + EXPIRY_MAX_DAYS * 86400:
        raise fault
    return usage_iso(at)


def key_limit(value, field: str, what: str):
    ceiling = WORKSPACE_LIMITS[field]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
        raise KeyFault("limits." + field, "A key's %s is a whole number from 1 to the workspace's %d, or null." % (what, ceiling))
    return value


def key_fields(body: dict, now: float, allow_device: bool) -> dict:
    """The body's fields beyond the name, each checked in the body's order; a field
    present with null clears it. The first fault is the 422."""
    out: dict = {}
    for name, value in body.items():
        if name == "name":
            continue
        if name == "device_id" and allow_device:
            if value is None:
                out["device_id"] = None
            elif isinstance(value, str):
                try:
                    out["device_id"] = key_device_id(value)
                except KeyFault as f:
                    raise key_fault(f, value)
            else:
                raise key_fault(KeyFault("device_id", "device_id is 1 to 128 printable ASCII characters, not starting with a space."), value)
        elif name == "device_id":
            raise refuse_422([key_entry("value_error", ["device_id"], "A key's device_id is set when it is made: revoke it and make another.", value)])
        elif name == "labels":
            try:
                out["labels"] = {} if value is None else key_labels(value)
            except KeyFault as f:
                raise key_fault(f, value)
        elif name == "limits":
            if value is None:
                out["limits"] = (None, None)
            elif isinstance(value, dict):
                try:
                    out["limits"] = (key_limit(value.get("requests_per_second"), "requests_per_second", "requests a second"),
                                     key_limit(value.get("tokens_per_second"), "tokens_per_second", "tokens a second"))
                except KeyFault as f:
                    raise key_fault(f, value)
            else:
                raise refuse_422([key_entry("value_error", ["limits"], "limits is an object, or null.", value)])
        elif name == "daily_spend_cap":
            try:
                out["cap"] = None if value is None else key_cap(value)
            except KeyFault as f:
                raise key_fault(f, value)
        elif name == "expires_at":
            try:
                out["expires_at"] = None if value is None else key_expiry(value, now)
            except KeyFault as f:
                raise key_fault(f, value)
        elif name == "may_search_collections":
            if not isinstance(value, bool):
                raise refuse_422([key_entry("bool_type", ["may_search_collections"], "Input should be a valid boolean", value)])
            out["may_search"] = value
        else:
            raise refuse_422([key_entry("extra_forbidden", [name], "Extra inputs are not permitted", None)])
    return out


def key_secret(key_id: str, salt: str) -> str:
    return KEY_SECRET_PREFIX + hashlib.sha256(("%s\n%s" % (key_id, salt)).encode()).hexdigest()


def key_status(k: dict, now: float) -> str:
    if k["revoked_at"] is not None:
        return "revoked"
    if k["expires_at"] is not None and usage_epoch(k["expires_at"]) <= now:
        return "expired"
    return "active"


def key_wire(k: dict, now: float) -> dict:
    """A decide key as /v1/keys shows it."""
    limits = None if k["rps"] is None and k["tps"] is None else {"requests_per_second": k["rps"], "tokens_per_second": k["tps"]}
    return {
        "id": k["id"], "name": k["name"], "prefix": KEY_SECRET_PREFIX, "last4": k["last4"], "status": key_status(k, now), "scope": "decide",
        "may_search_collections": k["may_search"], "limits": limits,
        "daily_spend_cap": None if k["cap"] is None else {"usd": usd(k["cap"] / 1e6)},
        "spent_today": None if k["cap"] is None else {"usd": 0},
        "expires_at": k["expires_at"], "device_id": k["device_id"], "labels": dict(sorted(k["labels"].items())),
        "created_by_key": None if k["created_by"] is None else {"id": k["created_by"]},
        "created_at": k["created_at"], "last_used_at": None, "revoked_at": k["revoked_at"],
    }


def calling_key_id(key: str | None) -> str:
    """The calling key's id, as created_by_key names it."""
    return "key_" + hashlib.sha256((key or "").encode()).hexdigest()[:32]



def workspace_of(key: str | None) -> str:
    """Keys share a workspace by default; a key holding ws-<name> is in workspace <name>."""
    m = re.search(r"ws-([A-Za-z0-9]+)", key or "")
    return m.group(1) if m else "default"


def s3_error(code: str, message: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>%s</Code><Message>%s</Message><RequestId>%s</RequestId></Error>' % (
        code,
        message,
        uuid.uuid4().hex[:16].upper(),
    )


# --------------------------------------------------------------------------- WebSocket (RFC 6455)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")


def ws_frame(opcode: int, payload: bytes) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class WsConn:
    """The server side of one WebSocket: frames out under a lock, frames in on a reader thread."""

    def __init__(self, handler):
        self.rfile = handler.rfile
        self.sock = handler.connection
        self.lock = threading.Lock()
        self.close_sent = False
        self.gone = False

    def send(self, opcode: int, payload: bytes) -> bool:
        with self.lock:
            if self.close_sent or self.gone:
                return False
            try:
                self.sock.sendall(ws_frame(opcode, payload))
                return True
            except OSError:
                self.gone = True
                return False

    def send_json(self, text: str) -> bool:
        return self.send(0x1, text.encode("utf-8"))

    def close(self, code: int, reason: str = "") -> None:
        with self.lock:
            if self.close_sent or self.gone:
                return
            self.close_sent = True
            try:
                self.sock.sendall(ws_frame(0x8, struct.pack(">H", code) + reason.encode("utf-8")[:123]))
            except OSError:
                self.gone = True

    def drop(self) -> None:
        with self.lock:
            self.gone = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _read(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.rfile.read(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def read_message(self):
        """(kind, payload): text, binary, ping, pong, close, too_big; raises EOFError."""
        parts = []
        size = 0
        first_op = None
        while True:
            b1, b2 = self._read(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if opcode >= 0x8 and (n > 125 or not fin):
                raise ValueError("a control frame over 125 bytes or fragmented")
            mask = self._read(4) if masked else None
            if opcode >= 0x8:  # control frames are never fragmented
                payload = self._read(n)
                if mask:
                    payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                return {0x8: "close", 0x9: "ping", 0xA: "pong"}.get(opcode, "binary"), payload
            if size + n > SESSION_MAX_MESSAGE_BYTES:
                return "too_big", b""
            payload = self._read(n)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload)) if n < 4096 else _unmask(payload, mask)
            if first_op is None:
                first_op = opcode
            parts.append(payload)
            size += n
            if fin:
                return ("text" if first_op == 0x1 else "binary"), b"".join(parts)


def _unmask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ key).to_bytes(n, "big")


# --------------------------------------------------------------------------- scenarios and sessions


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.scenarios: dict = {}
        self.counters: dict = {}
        self.sessions: dict = {}
        self.uploads: dict = {}
        self.tokens: dict = {}
        self.objects: dict = {}
        self.offsets: dict = {}
        self.flags: dict = {}
        self.applied: set = set()
        self.dn_sessions: dict = {}
        self.batches: dict = {}
        self.collections: dict = {}
        self.idempotency: dict = {}
        # Suite 1.20: each session's workspaces' decide keys, and the creates kept for
        # their Idempotency-Key.
        self.api_keys: dict = {}
        self.key_creates: dict = {}

    def session(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            s = {"requests": [], "in_flight": 0, "max_in_flight": 0}
            self.sessions[sid] = s
        return s

    def now(self, sid: str) -> float:
        return time.time() + self.offsets.get(sid, 0.0)

    def flag(self, sid: str) -> dict:
        f = self.flags.get(sid)
        if f is None:
            f = {"zero_retention": False, "legacy_noul": False, "batch": {}, "session": {}, "boosters": {}, "workers": {}, "places": {}, "collections": {}}
            self.flags[sid] = f
        return f

    def apply_settings(self, sid: str, name: str) -> None:
        """A scenario's settings (clock, workspace, truth, batch and session behaviour)
        apply to its session from the first request that names it."""
        scenario = self.scenarios.get(name)
        if not scenario:
            return
        with self.lock:
            if (sid, name) in self.applied:
                return
            self.applied.add((sid, name))
            if "clock_offset_seconds" in scenario:
                self.offsets[sid] = float(scenario["clock_offset_seconds"])
            f = self.flag(sid)
            ws = scenario.get("workspace") or {}
            if "zero_retention" in ws:
                f["zero_retention"] = bool(ws["zero_retention"])
            if "truth_as_noul" in scenario:
                f["legacy_noul"] = bool(scenario["truth_as_noul"])
            if "batch" in scenario:
                f["batch"] = dict(scenario["batch"])
            if "session" in scenario:
                f["session"] = dict(scenario["session"])
            if "boosters" in scenario:
                f["boosters"] = dict(scenario["boosters"])
            if "workers" in scenario:
                f["workers"] = dict(scenario["workers"])
            if "places" in scenario:
                f["places"] = dict(scenario["places"])
            if "collections" in scenario:
                f["collections"] = dict(scenario["collections"])

    def reset(self, sid: str | None = None) -> None:
        with self.lock:
            if sid is None:
                self.sessions.clear()
                self.counters.clear()
                self.offsets.clear()
                self.flags.clear()
                self.applied.clear()
                self.uploads.clear()
                self.tokens.clear()
                self.objects.clear()
                self.batches.clear()
                self.collections.clear()
                self.idempotency.clear()
                self.api_keys.clear()
                self.key_creates.clear()
                for ses in self.dn_sessions.values():
                    ses["close_request"] = (1001, "mock reset")
                self.dn_sessions.clear()
            else:
                self.sessions.pop(sid, None)
                for key in [k for k in self.counters if k[0] == sid]:
                    del self.counters[key]
                self.offsets.pop(sid, None)
                self.flags.pop(sid, None)
                self.applied = {a for a in self.applied if a[0] != sid}
                for uid in [u for u, up in self.uploads.items() if up["sid"] == sid]:
                    up = self.uploads.pop(uid)
                    self.tokens.pop(up["token"], None)
                    self.objects.pop(up["object_key"], None)
                for bid in [b for b, bt in self.batches.items() if bt["sid"] == sid]:
                    del self.batches[bid]
                for key in [k for k, col in self.collections.items() if col["sid"] == sid]:
                    del self.collections[key]
                for key in [k for k in self.idempotency if k[0] == sid]:
                    del self.idempotency[key]
                for store in (self.api_keys, self.key_creates):
                    for key in [k for k in store if k[0] == sid]:
                        del store[key]
                for xid in [x for x, ses in self.dn_sessions.items() if ses["sid"] == sid]:
                    self.dn_sessions[xid]["close_request"] = (1001, "mock reset")
                    del self.dn_sessions[xid]

    def next_step(self, sid: str, name: str, body, method: str = "POST", path: str = "") -> tuple:
        """(step or None, attempt number within the scenario key)."""
        scenario = self.scenarios.get(name)
        if scenario is None:
            return None, 0
        steps = scenario.get("steps")
        after = scenario.get("after", "normal")
        rule_index = -1
        if "rules" in scenario:
            steps = None
            for i, rule in enumerate(scenario["rules"]):
                if rule_matches(rule.get("match", {}), body, method, path):
                    steps = rule.get("steps", [])
                    after = rule.get("after", after)
                    rule_index = i
                    break
            if steps is None:
                fallback = scenario.get("otherwise")
                if fallback is None:
                    return {"action": "normal"}, 0
                steps = fallback.get("steps", [])
                after = fallback.get("after", after)
        if steps is None:
            return {"action": "normal"}, 0
        with self.lock:
            key = (sid, name, rule_index)
            n = self.counters.get(key, 0)
            self.counters[key] = n + 1
        if n < len(steps):
            return steps[n], n + 1
        if after == "repeat_last" and steps:
            return steps[-1], n + 1
        return {"action": "normal"}, n + 1


def json_pointer(doc, pointer: str):
    if pointer == "":
        return True, doc
    if not pointer.startswith("/"):
        return False, None
    cur = doc
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def rule_matches(match: dict, body, method: str = "POST", path: str = "") -> bool:
    """JSON pointers into the body, plus $method, $path (exact) and $path_prefix."""
    for pointer, expected in match.items():
        if pointer == "$method":
            if method != expected:
                return False
            continue
        if pointer == "$path":
            if path != expected:
                return False
            continue
        if pointer == "$path_prefix":
            if not path.startswith(expected):
                return False
            continue
        if pointer == "$path_suffix":
            if not path.endswith(expected):
                return False
            continue
        found, value = json_pointer(body, pointer)
        if not found or value != expected:
            return False
    return True


HTTP_DATE = re.compile(r"\{http_date([+-]\d+)?\}")


def expand_header(value: str) -> str:
    """Replace {http_date+N} with the HTTP date N seconds from now (N may be negative)."""
    return HTTP_DATE.sub(lambda m: email.utils.formatdate(time.time() + int(m.group(1) or 0), usegmt=True), value)


def load_cases(state: State, cases_dir: str) -> int:
    count = 0
    for kind in ("requests", "responses", "errors", "behaviour"):
        folder = os.path.join(cases_dir, kind)
        if not os.path.isdir(folder):
            continue
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(folder, fname), "r", encoding="utf-8") as fh:
                case = json.load(fh)
            cid = case.get("id") or fname[:-5]
            if kind in ("behaviour", "requests"):
                scenario = (case.get("mock") or {}).get("scenario")
                if scenario is None:
                    continue
                state.scenarios[("behaviour/" if kind == "behaviour" else "request/") + cid] = scenario
            else:
                prefix = "response/" if kind == "responses" else "error/"
                # A recorded response is replayed as recorded: without an x-request-id when it has none.
                state.scenarios[prefix + cid] = {"steps": [dict(case["response"], no_request_id=True)], "after": "repeat_last"}
            count += 1
    return count


def new_id(prefix: str, n_hex: int) -> str:
    return prefix + uuid.uuid4().hex[:n_hex] if n_hex <= 32 else prefix + (uuid.uuid4().hex + uuid.uuid4().hex)[:n_hex]


# --------------------------------------------------------------------------- HTTP

API_ROUTES = [
    (re.compile(r"^/v1/decide$"), {"POST": "decide"}),
    (re.compile(r"^/v1/models$"), {"GET": "models"}),
    (re.compile(r"^/v1/uploads$"), {"POST": "uploads"}),
    (re.compile(r"^/v1/sessions$"), {"POST": "session_open"}),
    (re.compile(r"^/v1/sessions/([^/]+)/stream$"), {"GET": "session_stream"}),
    (re.compile(r"^/v1/sessions/([^/]+)$"), {"DELETE": "session_end"}),
    (re.compile(r"^/v1/batches$"), {"POST": "batch_create", "GET": "batch_list"}),
    (re.compile(r"^/v1/batches/([^/]+)$"), {"GET": "batch_get"}),
    (re.compile(r"^/v1/batches/([^/]+)/requests$"), {"POST": "batch_add"}),
    (re.compile(r"^/v1/batches/([^/]+)/finalize$"), {"POST": "batch_finalize"}),
    (re.compile(r"^/v1/batches/([^/]+)/results$"), {"GET": "batch_results"}),
    (re.compile(r"^/v1/batches/([^/]+)/cancel$"), {"POST": "batch_cancel"}),
    # Collections of faces a request's pictures can be matched against.
    (re.compile(r"^/v1/collections$"), {"POST": "collection_create", "GET": "collection_list"}),
    (re.compile(r"^/v1/collections/([^/]+)$"), {"GET": "collection_get", "DELETE": "collection_delete"}),
    (re.compile(r"^/v1/collections/([^/]+)/items$"), {"POST": "item_enroll", "GET": "item_list"}),
    (re.compile(r"^/v1/collections/([^/]+)/items/([^/]+)$"), {"PATCH": "item_update", "DELETE": "item_delete"}),
    # Per-key usage (suite 1.18).
    (re.compile(r"^/v1/keys/([^/]+)/usage$"), {"GET": "key_usage"}),
    # A keys key's decide keys (suite 1.20).
    (re.compile(r"^/v1/keys$"), {"POST": "keys_create", "GET": "keys_list"}),
    (re.compile(r"^/v1/keys/revoke$"), {"POST": "keys_revoke_bulk"}),
    (re.compile(r"^/v1/keys/([^/]+)$"), {"PATCH": "keys_update", "DELETE": "keys_revoke"}),
    (re.compile(r"^/v1/usage$"), {"GET": "usage_page"}),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DecisionNodeMock/" + VERSION
    sys_version = ""
    # A keep-alive connection with no request for this long is closed, so the idle
    # connections of many short-lived clients never pile up threads in the mock. A
    # WebSocket clears it after the upgrade (sessions have their own idle timer).
    timeout = 60

    def version_string(self):
        return self.server_version

    # quiet unless --verbose
    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False) or getattr(self.server, "log_file", None):
            mock_log(self.server, "%s %s" % (self.address_string(), fmt % args))

    # ---- plumbing

    def read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                line = self.rfile.readline()
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def land(self) -> None:
        """The request leaves the in-flight count, once: just before its response's last
        bytes go out, since a client that has read them may send its next request before
        this thread returns, or at the end of the handler when nothing was sent."""
        sess = getattr(self, "flight", None)
        if sess is not None:
            self.flight = None
            with self.server.state.lock:
                sess["in_flight"] -= 1

    def send(self, status: int, body, headers: dict | None = None, raw_text: str | None = None, raw_bytes: bytes | None = None) -> None:
        if getattr(self, "suppress", False):
            # The work is done (an object stored, a batch finalized); the scenario's
            # own response goes out instead of this one.
            self.status_sent = status
            return
        if raw_bytes is not None:
            data = raw_bytes
        else:
            data = (raw_text if raw_text is not None else json.dumps(body, ensure_ascii=False)).encode("utf-8")
        headers = dict(headers or {})
        lower = {k.lower() for k in headers}
        try:
            self.send_response(status)
            if "content-type" not in lower:
                self.send_header("Content-Type", "application/json")
            for k, v in headers.items():
                self.send_header(k, str(v))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            trickle = getattr(self, "trickle", None)
            if trickle and data:
                # Headers go out at once; the body follows in pieces with a pause before
                # each one, so a client's timeout must cover the whole body, not each read.
                self.wfile.flush()
                pieces = max(1, int(trickle.get("chunks") or 1))
                size = -(-len(data) // pieces)
                for i in range(0, len(data), size):
                    time.sleep((trickle.get("interval_ms") or 0) / 1000.0)
                    if i + size >= len(data):
                        self.land()
                    self.wfile.write(data[i : i + size])
                    self.wfile.flush()
            else:
                self.land()
                self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            self.close_connection = True
        self.status_sent = status

    def drop(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.status_sent = None

    def error_headers(self, request_id: str, reason: str | None) -> dict:
        h = {"x-request-id": request_id}
        if reason:
            h["x-decisionnode-error"] = ascii_header(reason)
        return h

    def refuse(self, rid: str, exc: Refused, extra: dict | None = None) -> None:
        h = self.error_headers(rid, exc.reason)
        h.update(exc.headers)
        if extra:
            h.update(extra)
        self.send(exc.status, exc.body, h)

    # ---- dispatch

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")

    def record_request(self, sid: str, method: str, path: str, query: str, body_bytes: bytes, scenario: str | None) -> dict:
        state: State = self.server.state
        try:
            parsed = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
        except (ValueError, UnicodeDecodeError, RecursionError):
            parsed = None
        record = {
            "time": time.time(),
            "method": method,
            "path": path,
            "query": query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body_text": body_bytes.decode("utf-8", errors="replace"),
            "body_base64": base64.b64encode(body_bytes).decode("ascii"),
            "body": parsed,
            "scenario": scenario or None,
            "attempt": 0,
            "status": None,
        }
        with state.lock:
            sess = state.session(sid)
            sess["requests"].append(record)
        return record

    def dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        body_bytes = self.read_body()
        if path.startswith("/_mock/s3/"):
            self.storage(method, parts, body_bytes)
            return
        if path.startswith("/_mock/"):
            self.control(method, path, parse_qs(parts.query), body_bytes)
            return

        state: State = self.server.state
        sid = self.headers.get("x-mock-session") or "default"
        scenario = self.headers.get("x-mock-scenario") or ""
        if not scenario and path.endswith("/stream"):
            # A WebSocket handshake inherits the session and scenario of the open call,
            # so a client need not repeat its mock headers on the upgrade.
            m = re.match(r"^/v1/sessions/([^/]+)/stream$", path)
            ses = state.dn_sessions.get(m.group(1)) if m else None
            if ses is not None:
                if not self.headers.get("x-mock-session"):
                    sid = ses["sid"]
                scenario = ses.get("scenario") or ""
        record = self.record_request(sid, method, path, parts.query, body_bytes, scenario)
        with state.lock:
            sess = state.session(sid)
            sess["in_flight"] += 1
            sess["max_in_flight"] = max(sess["max_in_flight"], sess["in_flight"])
        self.flight = sess
        self.status_sent = None
        self.trickle = None
        self.suppress = False
        self.sid = sid
        self.scenario_name = scenario
        self.record = record
        try:
            step, attempt = (None, 0)
            if scenario:
                if scenario not in state.scenarios:
                    rid = "req_" + uuid.uuid4().hex
                    self.send(400, {"detail": {"error_type": "mock_error", "message": "Unknown mock scenario: %s" % scenario}}, {"x-request-id": rid})
                    return
                state.apply_settings(sid, scenario)
                step, attempt = state.next_step(sid, scenario, record["body"], method, path)
            record["attempt"] = attempt
            try:
                if step is not None:
                    self.play(step, method, path, body_bytes, parts)
                else:
                    self.normal(method, path, body_bytes, parts)
            except Exception:  # noqa: BLE001 - see internal_fault
                self.internal_fault()
        finally:
            self.land()
            if record.get("status") is None:
                record["status"] = self.status_sent

    def internal_fault(self) -> None:
        """A fault inside the mock: logged, and answered with a 500 the client can see
        (when nothing was sent yet), instead of a connection dropped without a response."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout)):
            self.close_connection = True
            return
        mock_log(self.server, "mock fault on %s %s:\n%s" % (self.command, self.path, traceback.format_exc()))
        self.suppress = False
        self.trickle = None
        if self.status_sent is None:
            self.send(500, {"detail": {"error_type": "mock_error", "message": "The mock server failed on this request: %s" % exc}}, {"x-request-id": "req_" + uuid.uuid4().hex})
        self.close_connection = True

    def play(self, step: dict, method: str, path: str, body_bytes: bytes, parts=None, storage_upload=None) -> None:
        advance = step.get("advance_clock_seconds")
        if advance:
            # Time passes before this request is handled: an upload URL presigned earlier
            # is that much older when this PUT arrives, by the session's clock.
            state: State = self.server.state
            with state.lock:
                state.offsets[self.sid] = state.offsets.get(self.sid, 0.0) + float(advance)
        delay = step.get("delay_ms") or 0
        if delay:
            time.sleep(delay / 1000.0)
        action = step.get("action") or ("respond" if "status" in step else "normal")
        self.trickle = step.get("trickle")
        if action == "close":
            if step.get("after_normal"):
                self.suppress = True
                self.run_normal(method, path, body_bytes, parts, storage_upload)
                self.suppress = False
            self.drop()
            return
        if action == "normal":
            self.run_normal(method, path, body_bytes, parts, storage_upload)
            return
        if step.get("after_normal"):
            # Do the work as the API would, then answer with this step instead: an
            # answer lost after the effect, as a retried PUT or finalize meets it.
            self.suppress = True
            self.run_normal(method, path, body_bytes, parts, storage_upload)
            self.suppress = False
        headers = {k: expand_header(str(v)).replace("{mock_url}", "http://" + self.host()) for k, v in (step.get("headers") or {}).items()}
        if not any(k.lower() == "x-request-id" for k in headers) and not step.get("no_request_id"):
            headers["x-request-id"] = "req_" + uuid.uuid4().hex
        if "body_text" in step:
            self.send(step["status"], None, headers, raw_text=step["body_text"])
        else:
            self.send(step["status"], step.get("body"), headers)

    def run_normal(self, method, path, body_bytes, parts, storage_upload):
        if storage_upload is not None or path.startswith("/_mock/s3/"):
            self.storage_normal(method, path, body_bytes, storage_upload)
        else:
            self.normal(method, path, body_bytes, parts)

    # ---- context

    def key(self) -> str | None:
        value = self.headers.get("Authorization") or ""
        m = re.fullmatch(r"Bearer ([!-~]+)", value)
        return m.group(1) if m else None

    def host(self) -> str:
        return self.headers.get("Host") or "%s:%d" % self.server.server_address[:2]

    def now(self) -> float:
        return self.server.state.now(self.sid)

    def resolver(self, consumed: list, attachments: dict | None = None, use_store: bool = True):
        """Resolves an upload_id in an image slot."""
        state: State = self.server.state
        workspace = workspace_of(self.key())
        now = self.now()

        def resolve(iid, upload_id, media_type):
            if attachments is not None and isinstance(upload_id, str) and upload_id in attachments and not use_store:
                att = attachments[upload_id]
                if media_type is not None and media_type != att["content_type"]:
                    raise upload_fault("upload_media_type_mismatch", iid, upload_id, "is %s; the request says %s." % (att["content_type"], media_type))
                return att["data"]
            if not isinstance(upload_id, str) or not re.fullmatch(r"upl_[0-9a-f]{32}", upload_id):
                raise upload_fault("upload_not_found", iid, upload_id, "was not found.")
            with state.lock:
                up = state.uploads.get(upload_id)
                if up is None or up["workspace"] != workspace or not use_store:
                    raise upload_fault("upload_not_found", iid, upload_id, "was not found.")
                if up["state"] == "consumed":
                    raise upload_fault("upload_consumed", iid, upload_id, "was deleted after its first decision (zero retention). Upload the image again.")
                if now >= up["expires_at"]:
                    raise upload_fault("upload_expired", iid, upload_id, "expired at %s. Upload the image again." % iso(up["expires_at"]))
                if media_type is not None and media_type != up["content_type"]:
                    raise upload_fault("upload_media_type_mismatch", iid, upload_id, "is %s; the request says %s." % (up["content_type"], media_type))
                data = state.objects.get(up["object_key"])
                if data is None:
                    raise upload_fault("upload_incomplete", iid, upload_id, "has no bytes yet. PUT the file to its URL first.")
                if len(data) != up["byte_size"]:
                    raise upload_fault("upload_mismatch", iid, upload_id, "holds %d bytes; %d were declared." % (len(data), up["byte_size"]))
                if sniff(data) != up["content_type"]:
                    raise upload_fault("upload_media_type_mismatch", iid, upload_id, "holds bytes that are not %s." % up["content_type"])
                consumed.append(upload_id)
                if attachments is not None:
                    attachments[upload_id] = {"data": data, "content_type": up["content_type"]}
                return data

        return resolve

    def consume(self, upload_ids: list) -> None:
        state: State = self.server.state
        if not upload_ids or not state.flag(self.sid)["zero_retention"]:
            return
        with state.lock:
            for uid in upload_ids:
                up = state.uploads.get(uid)
                if up is not None and up["state"] == "pending":
                    up["state"] = "consumed"
                    state.objects.pop(up["object_key"], None)

    # ---- the API

    def normal(self, method: str, path: str, body_bytes: bytes, parts=None) -> None:
        rid = "req_" + uuid.uuid4().hex
        started = time.perf_counter()
        route = None
        args = ()
        for pattern, methods in API_ROUTES:
            m = pattern.match(path)
            if m:
                route = methods
                args = m.groups()
                break
        if route is None:
            self.send(404, {"detail": {"error_type": "not_found_error", "message": "Not found."}}, self.error_headers(rid, "no endpoint at %s" % path))
            return
        if method not in route:
            allow = ", ".join(sorted(route))
            h = self.error_headers(rid, "%s takes %s only" % (path, allow))
            h["Allow"] = allow
            self.send(405, {"detail": {"error_type": "method_not_allowed_error", "message": "Method not allowed."}}, h)
            return
        name = route[method]
        if name == "session_stream":
            self.session_stream(rid, args[0])
            return
        auth = self.check_auth()
        if auth is not None:
            self.send(401, {"detail": {"error_type": "authentication_error", "message": "Cannot authenticate with the server. Please check your API key and try again."}}, self.error_headers(rid, auth))
            return
        if key_scope(self.key()) == "decide" and name not in DECIDE_KEY_ROUTES:
            # A decide key makes decisions and lists the models, nothing else: refused
            # before anything is read or counted.
            self.refuse(rid, typed_error(403, "key_scope_denied", KEY_SCOPE_DENIED))
            return
        if key_scope(self.key()) == "full" and name in KEYS_ROUTES:
            # Keys are managed with a keys key alone.
            self.refuse(rid, typed_error(403, "key_scope_denied", KEY_NOT_KEYS))
            return
        if key_scope(self.key()) == "keys" and name not in KEYS_KEY_ROUTES:
            # A keys key manages keys and reads their usage, nothing else.
            self.refuse(rid, typed_error(403, "key_scope_denied", KEY_KEYS_ONLY))
            return
        if key_scope(self.key()) == "publishable":
            # A publishable key makes decisions (the models list included in its refusal),
            # from its origins, or from a native app unless it holds web-only.
            if name != "decide":
                self.refuse(rid, typed_error(403, "key_scope_denied", KEY_PUBLISHABLE_ONLY))
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin not in PUBLISHABLE_ORIGINS:
                self.refuse(rid, typed_error(403, "origin_not_allowed", "This key does not answer calls from this origin."))
                return
            if origin is None and "web-only" in (self.key() or ""):
                self.refuse(rid, typed_error(403, "origin_not_allowed", "This key answers calls from its origins only: a call without an Origin needs a key that allows native apps."))
                return
        query = parse_qs(parts.query, keep_blank_values=True) if parts is not None else {}
        getattr(self, "api_" + name)(rid, started, body_bytes, query, *args)

    def check_auth(self) -> str | None:
        value = self.headers.get("Authorization")
        if not value:
            return "no Authorization header; send Authorization: Bearer <key>"
        m = re.fullmatch(r"Bearer ([!-~]+)", value)
        if not m:
            return "the Authorization header must be Bearer <key>"
        key = m.group(1)
        required = self.server.api_key
        if required is not None and key != required:
            return "unknown API key"
        if required is None and key.startswith("invalid"):
            return "unknown API key"
        return None

    def booster_conf(self) -> dict:
        return self.server.state.flag(self.sid)["boosters"]

    def max_places(self) -> int:
        """The several-places capability of the models served, on
        unless the scenario's places setting says max_places 0."""
        return int(self.server.state.flag(self.sid)["places"].get("max_places", PLACES_MAX))

    def api_models(self, rid, started, body_bytes, query):
        self.send(200, models_body(workspace_of(self.key()), self.max_places()), {"x-request-id": rid})

    def api_decide(self, rid, started, body_bytes, query, profile="decide"):
        if len(body_bytes) > MAX_BODY_BYTES:
            # Suite 1.11: the API answers this from Content-Length and closes the connection.
            self.close_connection = True
            self.refuse(rid, typed_error(413, "request_too_large_error", BODY_TOO_LARGE, "the body is %d bytes; the limit is %d" % (len(body_bytes), MAX_BODY_BYTES)))
            return
        consumed: list = []
        try:
            req = validate(body_bytes, profile, self.resolver(consumed), workspace_of(self.key()), self.booster_conf(), self.max_places(), self.search_collection)
            if key_scope(self.key()) == "publishable" and "flash-only" in (self.key() or "") and ("@" in req["model"] or family_of(req["model"]) != "decisionnode-flash"):
                allowed = ", ".join(PUBLISHABLE_FLASH_ONLY)
                raise Refused(403, {"detail": {"error_type": "model_not_allowed", "message": "This key may not ask this model. Its models: %s." % allowed,
                                               "details": {"allowed": list(PUBLISHABLE_FLASH_ONLY)}}}, "This key may not ask this model. Its models: %s." % allowed)
        except Refused as exc:
            detail = exc.body.get("detail") if isinstance(exc.body, dict) else None
            if not (isinstance(detail, dict) and str(detail.get("error_type", "")).startswith("upload_")):
                # The model answered (with a refusal): the uploads it read are used.
                self.consume(consumed)
            self.refuse(rid, exc)
            return
        self.consume(consumed)
        fallback_headers = {}
        workers = self.server.state.flag(self.sid)["workers"]
        # The cold list may name a model by any id it is accepted under (a family id,
        # a pinned id, an older id): each is read as the pinned id it resolves to.
        cold = {MODEL_INDEX[c]["resolves_to"] if c in MODEL_INDEX else c for c in (workers.get("cold") or [])}
        if req["model"] in cold:
            # Suite 1.11: no ready worker for the model. A live request for the MoE family
            # is answered by Flash unless it says "fallback": false; anything else (Flash,
            # a fine-tuned model) is the 529.
            served = FALLBACK_MODEL.get(req["model"])
            if req.get("requested_model") == req["model"]:
                # A pinned id is answered only by that version: it never falls back.
                served = None
            if served is None or req.get("fallback") is False or served in cold:
                wait_ms = int(workers.get("retry_after_ms", 50))
                headers = {"Retry-After": str(max(1, -(-wait_ms // 1000))), "retry-after-ms": str(wait_ms)}
                if req.get("booster_header"):
                    headers["x-decisionnode-boosters"] = req["booster_header"]
                why = "the request says fallback false" if served is not None and req.get("fallback") is False else "it has no fallback"
                self.refuse(rid, typed_error(529, "overloaded_error", "The model has no ready worker. Try again shortly.", "%s has no ready worker and %s" % (req["model"], why)), headers)
                return
            tokens_limit = MODEL_INDEX[served]["max_input_tokens"]
            if req["tokens"] > tokens_limit:
                self.refuse(rid, Refused(400, {"detail": {"error_type": "max_tokens_exceeded"}}, "the request is %d tokens, the limit of %s is %d" % (req["tokens"], served, tokens_limit)))
                return
            fallback_headers = {"x-decisionnode-fallback": served, "x-decisionnode-requested-model": req["requested_model"]}
            req = dict(req, model=served)
        legacy = self.server.state.flag(self.sid)["legacy_noul"] and profile == "decide"
        body, request_seed = decide(req, legacy)
        headers = {"x-request-id": rid, "x-decisionnode-model": req["model"]}
        headers.update(fallback_headers)
        if req.get("booster_header"):
            headers["x-decisionnode-boosters"] = req["booster_header"]
        if self.server.safety:
            header, safety_body = safety_of(req, request_seed)
            headers["x-decisionnode-safety"] = header
            flag = (self.headers.get("x-decisionnode-safety-body") or "").strip().lower()
            if flag in ("true", "1", "yes"):
                body["safety"] = safety_body
        headers["x-decisionnode-latency-ms"] = str(max(1, int((time.perf_counter() - started) * 1000)))
        self.send(200, body, headers)

    # ---- uploads

    def api_uploads(self, rid, started, body_bytes, query):
        state: State = self.server.state
        if len(body_bytes) > UPLOAD_BODY_LIMIT:
            self.refuse(rid, typed_error(413, "request_too_large_error", "The body is over %d bytes." % UPLOAD_BODY_LIMIT))
            return
        try:
            try:
                body = json.loads(body_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise usage_error("The body must be a JSON object.")
            files = validate_presign(body)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        key = self.key()
        workspace = workspace_of(key)
        now = self.now()
        out = []
        with state.lock:
            for f in files:
                upload_id = "upl_" + uuid.uuid4().hex
                object_key = "uploads/%s/%s" % (workspace, uuid.uuid4().hex)
                token = uuid.uuid4().hex + uuid.uuid4().hex
                signed = ["content-length", "content-md5", "content-type", "host", "if-none-match"]
                headers = {"Content-Type": f["content_type"], "Content-MD5": f["checksum"], "If-None-Match": "*"}
                if f["sha256"] is not None:
                    signed.append("x-amz-checksum-sha256")
                    headers["x-amz-checksum-sha256"] = f["sha256"]
                url = "http://%s/_mock/s3/%s?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=mock%%2F%s%%2Fmock-region%%2Fs3%%2Faws4_request&X-Amz-Date=%s&X-Amz-Expires=%d&X-Amz-SignedHeaders=%s&X-Amz-Signature=%s" % (
                    self.host(),
                    object_key,
                    time.strftime("%Y%m%d", time.gmtime(now)),
                    time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
                    UPLOAD_URL_TTL_S,
                    quote(";".join(signed), safe=""),
                    token,
                )
                expires_at = math.floor(now) + UPLOAD_TTL_S
                state.uploads[upload_id] = {
                    "id": upload_id,
                    "sid": self.sid,
                    "scenario": self.scenario_name,
                    "workspace": workspace,
                    "content_type": f["content_type"],
                    "byte_size": f["byte_size"],
                    "md5": f["checksum"],
                    "sha256": f["sha256"],
                    "object_key": object_key,
                    "token": token,
                    "url_expires": now + UPLOAD_URL_TTL_S,
                    "expires_at": expires_at,
                    "state": "pending",
                    "puts": 0,
                    "authorization_seen": False,
                }
                state.tokens[token] = upload_id
                out.append({"upload_id": upload_id, "url": url, "method": "PUT", "headers": headers, "expires_at": iso(expires_at)})
        self.send(201, {"uploads": out, "expires_in": UPLOAD_URL_TTL_S}, {"x-request-id": rid})

    def storage(self, method: str, parts, body_bytes: bytes) -> None:
        """The mock's object store: the target of every presigned PUT."""
        state: State = self.server.state
        token = (parse_qs(parts.query).get("X-Amz-Signature") or [None])[0]
        with state.lock:
            upload = state.uploads.get(state.tokens.get(token or ""))
        sid = upload["sid"] if upload else (self.headers.get("x-mock-session") or "default")
        scenario = upload["scenario"] if upload else ""
        record = self.record_request(sid, method, parts.path, parts.query, body_bytes, scenario)
        self.status_sent = None
        self.trickle = None
        self.suppress = False
        self.sid = sid
        self.scenario_name = scenario
        if upload is not None:
            with state.lock:
                upload["puts"] += 1
                if self.headers.get("Authorization") is not None:
                    upload["authorization_seen"] = True
        try:
            step, attempt = (None, 0)
            if scenario and scenario in state.scenarios:
                step, attempt = state.next_step(sid, scenario, None, method, parts.path)
            record["attempt"] = attempt
            try:
                if step is not None:
                    self.play(step, method, parts.path, body_bytes, parts, storage_upload=upload or {})
                else:
                    self.storage_normal(method, parts.path, body_bytes, upload)
            except Exception:  # noqa: BLE001 - see internal_fault
                self.internal_fault()
        finally:
            record["status"] = self.status_sent

    def storage_normal(self, method: str, path: str, body_bytes: bytes, upload) -> None:
        state: State = self.server.state
        xml = {"Content-Type": "application/xml", "x-amz-request-id": uuid.uuid4().hex[:16].upper()}
        if method != "PUT":
            self.send(405, None, dict(xml, Allow="PUT"), raw_text=s3_error("MethodNotAllowed", "The specified method is not allowed against this resource."))
            return
        if not upload or path != "/_mock/s3/" + upload["object_key"]:
            self.send(403, None, xml, raw_text=s3_error("SignatureDoesNotMatch", "The request signature we calculated does not match the signature you provided."))
            return
        if state.now(upload["sid"]) > upload["url_expires"]:
            self.send(403, None, xml, raw_text=s3_error("AccessDenied", "Request has expired"))
            return
        h = self.headers
        signed_ok = (
            h.get("Content-Type") == upload["content_type"]
            and h.get("Content-MD5") == upload["md5"]
            and h.get("If-None-Match") == "*"
            and (upload["sha256"] is None or h.get("x-amz-checksum-sha256") == upload["sha256"])
            and str(len(body_bytes)) == (h.get("Content-Length") or str(len(body_bytes)))
            and len(body_bytes) == upload["byte_size"]
        )
        if not signed_ok:
            self.send(403, None, xml, raw_text=s3_error("SignatureDoesNotMatch", "The request signature we calculated does not match the signature you provided. Check your key and signing method."))
            return
        if base64.b64encode(hashlib.md5(body_bytes).digest()).decode("ascii") != upload["md5"]:
            self.send(400, None, xml, raw_text=s3_error("BadDigest", "The Content-MD5 you specified did not match what was received."))
            return
        if upload["sha256"] is not None and base64.b64encode(hashlib.sha256(body_bytes).digest()).decode("ascii") != upload["sha256"]:
            self.send(400, None, xml, raw_text=s3_error("BadDigest", "The x-amz-checksum-sha256 you specified did not match the calculated checksum."))
            return
        with state.lock:
            if upload["object_key"] in state.objects:
                exists = True
            else:
                exists = False
                state.objects[upload["object_key"]] = body_bytes
        if exists:
            self.send(412, None, xml, raw_text=s3_error("PreconditionFailed", "At least one of the pre-conditions you specified did not hold"))
            return
        self.send(200, None, dict(xml, ETag='"%s"' % hashlib.md5(body_bytes).hexdigest()), raw_text="")

    # ---- sessions

    def session_conf(self) -> dict:
        return self.server.state.flag(self.sid)["session"]

    def api_session_open(self, rid, started, body_bytes, query):
        state: State = self.server.state
        bill0 = {"x-decisionnode-billable-tokens": "0"}
        try:
            body = parse_body(body_bytes)
            check_nesting(body)
            if not isinstance(body, dict):
                raise Refused(
                    422,
                    {"detail": [entry("model_attributes_type", ["body"], "Input should be a valid dictionary or object to extract fields from", body)]},
                    "the body must be a JSON object",
                )
            entries = []
            for name in ("instructions", "state"):
                v = body.get(name)
                if v is not None and not is_sdl(v):
                    entries.extend(str_dict_list(["body", name], v))
            limits = {"window": (1, SESSION_DEFAULTS["max_window"]), "ttl_seconds": (1, SESSION_DEFAULTS["max_ttl_seconds"])}
            for name, (lo, hi) in limits.items():
                v = body.get(name)
                if v is None:
                    continue
                if not is_int(v):
                    entries.append(entry("int_type", ["body", name], "Input should be a valid integer", v))
                elif v < lo:
                    entries.append(entry("greater_than_equal", ["body", name], "Input should be greater than or equal to %d" % lo, v, {"ge": lo}))
                elif v > hi:
                    entries.append(entry("less_than_equal", ["body", name], "Input should be less than or equal to %d" % hi, v, {"le": hi}))
            fixed = {"model": body.get("model"), "state": "", "questions": body.get("questions")}
            if "questions" not in body:
                fixed.pop("questions")
            held = None
            req = None
            try:
                # A session's questions look at its frames' images: the image rules wait for a frame.
                req = validate_obj(fixed, "decide", None, places=False, workspace=workspace_of(self.key()))
            except Refused as exc:
                if exc.status == 422:
                    entries.extend(exc.body["detail"])
                else:
                    held = exc
            if entries:
                raise refuse_422(entries)
            for name in body:
                if name == "images":
                    raise usage_error("images: send images in frames, not when opening.")
                if name == "boosters":
                    # Suite 1.11: sessions take no boosters (their prefix holds no images).
                    raise usage_error("boosters are not available on sessions", "boosters: sessions take no boosters; send them on POST /v1/decide or a batch line")
                if name not in ("model", "instructions", "state", "questions", "window", "ttl_seconds"):
                    raise usage_error("%s: this field is not taken by POST /v1/sessions." % name)
            if held is not None:
                raise held
        except Refused as exc:
            self.refuse(rid, exc, bill0)
            return
        key = self.key()
        conf = self.session_conf()
        now = self.now()
        max_per_key = int(conf.get("max_per_key", SESSION_DEFAULTS["max_per_key"]))
        with state.lock:
            mine = [s for s in state.dn_sessions.values() if s["key"] == key and s["sid"] == self.sid and not s["ended"] and now < s["expires_at"]]
            if len(mine) >= max_per_key:
                earliest = min((s["expires_at"] for s in mine), default=now + 1)
                wait = min(30.0, max(1.0, earliest - now))
                self.refuse(
                    rid,
                    typed_error(429, "session_limit_exceeded", "This key already holds %d open sessions, the limit." % len(mine), "this key holds %d open sessions; sessions.max_per_key is %d" % (len(mine), max_per_key)),
                    dict(bill0, **{"Retry-After": str(int(math.ceil(wait))), "retry-after-ms": str(int(wait * 1000))}),
                )
                return
        window = body.get("window") if body.get("window") is not None else SESSION_DEFAULTS["default_window"]
        ttl = body.get("ttl_seconds") if body.get("ttl_seconds") is not None else SESSION_DEFAULTS["default_ttl_seconds"]
        instructions = body.get("instructions") if body.get("instructions") is not None else ""
        st = body.get("state") if body.get("state") is not None else ""
        prefix_text = (instructions if isinstance(instructions, str) else compact(instructions)) + (st if isinstance(st, str) else compact(st))
        prefix_tokens = 8 + math.ceil(len(prefix_text.encode("utf-8")) / 4)
        q_tokens = {qid: question_tokens(q, req["grids"].get(qid)) for qid, q in req["questions"].items()}
        total = prefix_tokens + sum(q_tokens.values())
        if total > SESSION_DEFAULTS["max_prefix_tokens"]:
            self.refuse(rid, Refused(400, {"detail": {"error_type": "max_tokens_exceeded"}}, "fixed part %d tokens, limit %d" % (total, SESSION_DEFAULTS["max_prefix_tokens"])), bill0)
            return
        ses_id = "ses_" + uuid.uuid4().hex[:24]
        expires_at = math.ceil(now + ttl)
        ses = {
            "id": ses_id,
            "sid": self.sid,
            "scenario": self.scenario_name,
            "key": key,
            "workspace": workspace_of(key),
            "model": req["model"],
            "instructions": instructions,
            "state": st,
            "questions": req["questions"],
            "answer_types": req["answer_types"],
            "grids": req["grids"],
            "q_tokens": q_tokens,
            "order": list(req["questions"].keys()),
            "window": window,
            "ttl": ttl,
            "expires_at": expires_at,
            "opened_at": time.time(),
            "attached": False,
            "ended": False,
            "close_request": None,
            "answered": 0,
            "dropped": 0,
            "cut": 0,
            "last_seq": None,
            "billed": total,
            "history": [],
        }
        oreq = {"model": req["model"], "state_text": compact({"instructions": instructions, "state": st, "frames": []}), "questions": req["questions"],
                "answer_types": req["answer_types"], "grids": req["grids"], "images": [], "tokens": total}
        out, seed = decide(oreq)
        _, safety_body = safety_of(oreq, seed)
        with state.lock:
            state.dn_sessions[ses_id] = ses
        resp = {
            "session_id": ses_id,
            "model": req["model"],
            "stream_url": "ws://%s/v1/sessions/%s/stream" % (self.host(), ses_id),
            "window": window,
            "ttl_seconds": ttl,
            "expires_at": iso(expires_at),
            "limits": {
                "max_frame_tokens": SESSION_DEFAULTS["max_frame_tokens"],
                "max_frame_images": 1,
                "window_tokens": SESSION_DEFAULTS["window_tokens"],
                "idle_timeout_seconds": SESSION_DEFAULTS["idle_timeout_seconds"],
            },
            "questions": ses["order"],
            "prefix_tokens": prefix_tokens,
            "usage": {"input_tokens": total, "output_tokens": 0, "cost": cost_of(req["model"], total)},
            "answers": out["answers"],
            "safety": safety_body,
        }
        headers = {
            "x-request-id": rid,
            "x-decisionnode-model": req["model"],
            "x-decisionnode-latency-ms": str(max(1, int((time.perf_counter() - started) * 1000))),
            "x-decisionnode-billable-tokens": str(total),
        }
        self.send(200, resp, headers)

    def api_session_end(self, rid, started, body_bytes, query, ses_id):
        state: State = self.server.state
        bill0 = {"x-decisionnode-billable-tokens": "0"}
        key = self.key()
        now = self.now()
        with state.lock:
            ses = state.dn_sessions.get(ses_id)
            live = ses is not None and ses["key"] == key and not ses["ended"] and now < ses["expires_at"] and not self.attach_expired(ses)
            if live:
                ses["ended"] = True
                if ses["attached"]:
                    ses["close_request"] = (1000, "the session was ended")
        if not live:
            self.refuse(rid, typed_error(404, "not_found_error", "No open session with this id for this key."), bill0)
            return
        body = {
            "session_id": ses["id"],
            "model": ses["model"],
            "status": "ended",
            "expires_at": iso(ses["expires_at"]),
            "frames_answered": ses["answered"],
            "frames_dropped": ses["dropped"],
            "frames_cut": ses["cut"],
            "last_seq": ses["last_seq"],
            "tokens_billed": ses["billed"],
        }
        self.send(200, body, dict(bill0, **{"x-request-id": rid}))

    def attach_expired(self, ses) -> bool:
        conf = self.server.state.flag(ses["sid"])["session"]
        idle = conf.get("idle_timeout_ms", SESSION_DEFAULTS["idle_timeout_seconds"] * 1000) / 1000.0
        return not ses["attached"] and not ses.get("was_attached") and time.time() - ses["opened_at"] > idle

    def session_stream(self, rid, ses_id):
        """The handshake, then frames, replies and close codes."""
        state: State = self.server.state
        upgrade = (self.headers.get("Upgrade") or "").lower()
        ws_key = self.headers.get("Sec-WebSocket-Key")
        if upgrade != "websocket" or not ws_key:
            h = self.error_headers(rid, "this path takes a WebSocket")
            h["Upgrade"] = "websocket"
            self.send(426, {"detail": {"error_type": "api_usage_error", "message": "This path takes a WebSocket."}}, h)
            return
        key = self.key() if self.check_auth() is None else None
        conf = self.session_conf()
        # The socket is attached before the handshake is answered, so a DELETE sent as
        # soon as the client is connected finds it attached and closes it with 1000.
        with state.lock:
            ses = state.dn_sessions.get(ses_id)
            if key is None:
                code = 4401
            elif ses is None or ses["key"] != key or ses["ended"] or state.now(ses["sid"]) >= ses["expires_at"] or self.attach_expired(ses):
                if ses is not None and not ses["ended"] and ses["key"] == key:
                    ses["ended"] = True
                code = 4404
            elif ses["attached"]:
                code = 4409
            else:
                code = None
                on_connect = (conf.get("close") or {}).get("on_connect")
                if on_connect is not None:
                    code = int(on_connect)
                    if code != 1013:
                        ses["ended"] = True
                else:
                    ses["attached"] = True
                    ses["was_attached"] = True
        try:
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", ws_accept(ws_key))
            self.send_header("x-request-id", rid)
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(None)
        except OSError:
            self.close_connection = True
            if code is None:
                with state.lock:
                    ses["attached"] = False
            return
        self.status_sent = 101
        self.close_connection = True
        conn = WsConn(self)
        if code is not None:
            reason = {4401: "unknown key", 4404: "no open session with this id for this key", 4409: "a socket is already attached", 1013: "try again later"}.get(code, (conf.get("close") or {}).get("reason", "closed"))
            self.ws_finish(conn, code, reason)
            return
        try:
            SessionLoop(self, conn, ses, conf).run()
        finally:
            with state.lock:
                ses["ended"] = True
                ses["attached"] = False

    def ws_finish(self, conn: WsConn, code: int | None, reason: str, events=None) -> None:
        """Send the close frame (code None: drop without one), wait briefly for the echo."""
        if code is None or code == 1006:
            conn.drop()
            return
        conn.close(code, reason)
        deadline = time.time() + 1.0
        if events is not None:
            while time.time() < deadline:
                try:
                    kind, _ = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if kind in ("close", "eof"):
                    break
        else:
            try:
                self.connection.settimeout(1.0)
                while time.time() < deadline:
                    kind, _ = conn.read_message()
                    if kind == "close":
                        break
            except (EOFError, OSError, ValueError):
                pass
        conn.drop()

    # ---- batches

    def batch_conf(self) -> dict:
        conf = dict(BATCH_DEFAULTS)
        conf.update(self.server.state.flag(self.sid)["batch"])
        return conf

    def batch_of(self, rid, bid):
        state: State = self.server.state
        b = state.batches.get(bid)
        if not re.fullmatch(r"batch_[0-9A-HJKMNP-TV-Z]{10}", bid or "") or b is None or b["key"] != self.key() or b["sid"] != self.sid:
            self.refuse(rid, typed_error(404, "not_found_error", "No batch %s for this key." % bid))
            return None
        return b

    def parse_lines(self, body_bytes: bytes, existing: set, allow_empty: bool) -> list:
        """JSON Lines or an array; every custom_id checked."""
        enc = (self.headers.get("Content-Encoding") or "identity").strip().lower()
        if enc not in ("", "identity"):
            raise usage_error("Compressed uploads are not accepted.")
        try:
            text = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise usage_error("The upload is not valid UTF-8.")
        if text.startswith("﻿"):
            text = text[1:]
        stripped = text.strip()
        items = []
        if stripped == "" or stripped == "[]":
            items = []
        elif stripped.startswith("["):
            try:
                arr = json.loads(stripped)
            except (ValueError, RecursionError):
                raise usage_error("The upload is neither JSON Lines nor a JSON array.")
            for el in arr:
                items.append((compact(el), el))
        else:
            for raw_line in text.split("\n"):
                line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
                if line.strip() == "":
                    continue
                try:
                    el = json.loads(line)
                except (ValueError, RecursionError):
                    if not items:
                        raise usage_error("The upload is neither JSON Lines nor a JSON array.")
                    raise usage_error("Line %d is not a JSON object." % (len(items) + 1))
                items.append((line, el))
        if not items and not allow_empty:
            raise usage_error("The call holds no requests.")
        seen = set(existing)
        out = []
        for n, (line_text, el) in enumerate(items, start=1):
            if not isinstance(el, dict):
                raise usage_error("Line %d is not a JSON object." % n)
            if "custom_id" not in el:
                raise usage_error("Line %d has no custom_id." % n)
            cid = el["custom_id"]
            if not isinstance(cid, str) or not 1 <= len(cid) <= 512:
                raise usage_error("Line %d: custom_id must be a string of 1 to 512 characters." % n)
            if cid in seen:
                shown = compact(cid)
                if len(shown) > 64:
                    shown = shown[:64] + "..."
                raise usage_error("Line %d: custom_id %s is already in the batch." % (n, shown))
            seen.add(cid)
            out.append((cid, line_text, el))
        return out

    def batch_lines(self, b, parsed) -> list:
        """Validate each line as the live call would; a refused line is kept as a failed line."""
        lines = []
        for cid, line_text, el in parsed:
            body = {k: v for k, v in el.items() if k != "custom_id"}
            line = {"custom_id": cid, "body": body, "status": None, "result_text": None, "headers": {}, "state": "pending"}
            consumed: list = []
            try:
                validate_obj(loads(compact(body)), "decide", self.resolver(consumed, b["attachments"]), workspace=workspace_of(self.key()), booster_conf=self.booster_conf(), max_places=self.max_places(), collections=self.booster_collection)
                self.consume(consumed)
            except Refused as exc:
                is_upload = isinstance(exc.body.get("detail"), dict) and str(exc.body["detail"].get("error_type", "")).startswith("upload_")
                if not is_upload:
                    self.consume(consumed)
                if not (isinstance(exc.body.get("detail"), dict) and exc.body["detail"].get("error_type") == "max_tokens_exceeded"):
                    line["status"] = exc.status
                    line["result_text"] = json.dumps(exc.body, ensure_ascii=False)
                    line["headers"] = dict({"x-decisionnode-error": ascii_header(exc.reason)}, **exc.headers)
                    line["state"] = "refused"
            lines.append(line)
        return lines

    def batch_object(self, b) -> dict:
        lines = b["lines"]
        counters = {"requests": len(lines), "queued": 0, "in_progress": 0, "completed": 0, "failed": 0, "expired": 0, "cancelled": 0}
        usage = 0
        for ln in lines:
            if ln["state"] == "queued":
                counters["queued"] += 1
            elif ln["state"] == "in_progress":
                counters["in_progress"] += 1
            elif ln["state"] in ("refused", "done"):
                if ln["status"] == 200:
                    counters["completed"] += 1
                    usage += ln["tokens"]
                elif ln["status"] == "expired":
                    counters["expired"] += 1
                elif ln["status"] == "cancelled":
                    counters["cancelled"] += 1
                else:
                    counters["failed"] += 1
        ended = b["ended_at"] is not None
        return {
            "id": b["id"],
            "state": b["state"],
            "counters": counters,
            "created_at": iso(b["created_at"]),
            "finalized_at": iso(b["finalized_at"]) if b["finalized_at"] is not None else None,
            "expires_at": iso(b["finalized_at"] + BATCH_EXPIRY_HOURS * 3600) if b["finalized_at"] is not None else None,
            "ended_at": iso(b["ended_at"]) if ended else None,
            "results_url": "http://%s/v1/batches/%s/results" % (b["host"], b["id"]) if ended else None,
            "results_expire_at": iso(b["ended_at"] + BATCH_RESULTS_DAYS * 86400) if ended else None,
            "usage": {"input_tokens": usage, "output_tokens": 0},
            "error": b["error"],
        }

    def run_line(self, b, line) -> None:
        rid = "req_" + uuid.uuid4().hex[:24]
        try:
            req = validate_obj(
                loads(compact(line["body"])),
                "decide",
                self.resolver([], b["attachments"], use_store=False),
                workspace=workspace_of(self.key()),
                booster_conf=self.booster_conf(),
                max_places=self.max_places(),
                collections=self.booster_collection,
            )
        except Refused as exc:
            headers = dict({"x-request-id": rid, "x-decisionnode-error": ascii_header(exc.reason)}, **exc.headers)
            line.update(status=exc.status, result_text=json.dumps(exc.body, ensure_ascii=False), headers=headers, state="done", tokens=0)
            return
        body, seed = decide(req, price_factor=batch_price_factor(req["model"]))
        header, _ = safety_of(req, seed)
        headers = {"x-request-id": rid, "x-decisionnode-model": req["model"]}
        if req.get("booster_header"):
            headers["x-decisionnode-boosters"] = req["booster_header"]
        if self.server.safety:
            headers["x-decisionnode-safety"] = header
        line.update(status=200, result_text=json.dumps(body, ensure_ascii=False), headers=headers, state="done", tokens=req["tokens"])

    def end_batch(self, b, state_name: str, now: float) -> None:
        for ln in b["lines"]:
            if ln["state"] in ("queued", "in_progress", "pending"):
                if state_name == "completed":
                    self.run_line(b, ln)
                elif state_name == "failed":
                    ln.update(status=500, result_text=json.dumps({"detail": {"error_type": "api_error", "message": "Something went wrong on our side."}}),
                              headers={"x-decisionnode-error": "the batch failed: its request file could not be read"}, state="done", tokens=0)
                elif state_name == "expired":
                    ln.update(status="expired", result_text="null", headers={}, state="done", tokens=0)
                else:
                    ln.update(status="cancelled", result_text="null", headers={}, state="done", tokens=0)
        b["state"] = state_name
        b["ended_at"] = now
        if state_name == "failed":
            b["error"] = {"error_type": "batch_failed", "message": "The batch's request file could not be read."}

    def advance(self, b, now: float, poll: bool) -> None:
        """The mock's lifecycle: each retrieve after finalize moves the batch on."""
        if b["ended_at"] is not None or b["state"] == "draft":
            return
        if now >= b["finalized_at"] + BATCH_EXPIRY_HOURS * 3600:
            for ln in b["lines"]:
                if ln["state"] == "in_progress":
                    self.run_line(b, ln)
            self.end_batch(b, "expired", now)
            return
        if not poll:
            return
        b["polls"] += 1
        conf = b["conf"]
        if b["cancel_requested"]:
            for ln in b["lines"]:
                if ln["state"] == "in_progress":
                    self.run_line(b, ln)
            self.end_batch(b, "cancelled", now)
            return
        if b.get("finish_now") or b["polls"] >= int(conf["polls_to_end"]):
            for ln in b["lines"]:
                if ln["state"] == "in_progress":
                    self.run_line(b, ln)
            self.end_batch(b, conf["end_state"] if not b.get("finish_now") else "completed", now)
            return
        if b["polls"] >= int(conf["polls_to_running"]) and b["state"] == "queued":
            b["state"] = "running"
            queued = [ln for ln in b["lines"] if ln["state"] == "queued"]
            half = (len(queued) + 1) // 2
            for ln in queued[:half]:
                self.run_line(b, ln)
            rest = [ln for ln in b["lines"] if ln["state"] == "queued"]
            for ln in rest[: int(conf["running_in_progress"])]:
                ln["state"] = "in_progress"

    def active_batches(self, key) -> list:
        return [b for b in self.server.state.batches.values() if b["key"] == key and b["sid"] == self.sid and b["ended_at"] is None]

    def batch_send(self, rid, status, b, extra=None) -> None:
        h = {"x-request-id": rid}
        if extra:
            h.update(extra)
        self.send(status, self.batch_object(b), h)

    def api_batch_create(self, rid, started, body_bytes, query):
        state: State = self.server.state
        key = self.key()
        try:
            if len(self.active_batches(key)) >= BATCH_ACTIVE_PER_KEY:
                raise typed_error(429, "batch_limit_exceeded", "This key already has %d active batches." % BATCH_ACTIVE_PER_KEY, headers={"Retry-After": "30", "retry-after-ms": "30000"})
            if len(body_bytes) > BATCH_MAX_BYTES:
                raise typed_error(413, "request_too_large_error", "The call is over %d bytes." % BATCH_MAX_BYTES)
            parsed = self.parse_lines(body_bytes, set(), allow_empty=True)
            if len(parsed) > BATCH_MAX_REQUESTS:
                raise usage_error("The batch would hold %d requests. The limit is %d." % (len(parsed), BATCH_MAX_REQUESTS))
        except Refused as exc:
            self.refuse(rid, exc)
            return
        now = self.now()
        bid = "batch_" + "".join(random.choice(CROCKFORD) for _ in range(10))
        b = {"id": bid, "key": key, "sid": self.sid, "host": self.host(), "state": "draft", "lines": [], "created_at": math.floor(now), "seq": next(CREATION_ORDER),
             "finalized_at": None, "ended_at": None, "error": None, "polls": 0, "cancel_requested": False, "attachments": {}, "bytes": len(body_bytes), "conf": self.batch_conf()}
        b["lines"] = self.batch_lines(b, parsed)
        with state.lock:
            state.batches[bid] = b
        self.batch_send(rid, 201, b)

    def api_batch_add(self, rid, started, body_bytes, query, bid):
        b = self.batch_of(rid, bid)
        if b is None:
            return
        try:
            if b["state"] != "draft":
                raise typed_error(409, "invalid_batch_state", "Batch %s is %s. Only a draft takes requests." % (bid, b["state"]))
            if len(body_bytes) > BATCH_ADD_BYTES:
                raise typed_error(413, "request_too_large_error", "The call is over %d bytes." % BATCH_ADD_BYTES)
            if b["bytes"] + len(body_bytes) > BATCH_MAX_BYTES:
                raise typed_error(413, "request_too_large_error", "The batch would be over %d bytes." % BATCH_MAX_BYTES)
            parsed = self.parse_lines(body_bytes, {ln["custom_id"] for ln in b["lines"]}, allow_empty=False)
            if len(b["lines"]) + len(parsed) > BATCH_MAX_REQUESTS:
                raise usage_error("The batch would hold %d requests. The limit is %d." % (len(b["lines"]) + len(parsed), BATCH_MAX_REQUESTS))
        except Refused as exc:
            self.refuse(rid, exc)
            return
        b["lines"].extend(self.batch_lines(b, parsed))
        b["bytes"] += len(body_bytes)
        self.batch_send(rid, 200, b)

    def api_batch_finalize(self, rid, started, body_bytes, query, bid):
        b = self.batch_of(rid, bid)
        if b is None:
            return
        try:
            if len(body_bytes) > 1024 * 1024:
                raise typed_error(413, "request_too_large_error", "This call takes no body; send at most 1 MiB.")
            if b["state"] != "draft":
                raise typed_error(409, "invalid_batch_state", "Batch %s is %s. Only a draft can be finalized." % (bid, b["state"]))
            if not b["lines"]:
                raise usage_error("Batch %s holds no requests." % bid)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        b["state"] = "queued"
        b["finalized_at"] = math.floor(self.now())
        queued = 0
        for ln in b["lines"]:
            if ln["state"] == "pending":
                ln["state"] = "queued"
                queued += 1
        if queued == 0:
            b["finish_now"] = True
        self.batch_send(rid, 200, b)

    def api_batch_get(self, rid, started, body_bytes, query, bid):
        b = self.batch_of(rid, bid)
        if b is None:
            return
        self.advance(b, self.now(), poll=True)
        extra = None
        ra = b["conf"].get("get_retry_after")
        if ra is not None and b["ended_at"] is None:
            extra = {"Retry-After": str(ra)}
        self.batch_send(rid, 200, b, extra)

    def api_batch_cancel(self, rid, started, body_bytes, query, bid):
        b = self.batch_of(rid, bid)
        if b is None:
            return
        now = self.now()
        self.advance(b, now, poll=False)
        if len(body_bytes) > 1024 * 1024:
            self.refuse(rid, typed_error(413, "request_too_large_error", "This call takes no body; send at most 1 MiB."))
            return
        if b["ended_at"] is not None and b["state"] != "cancelled":
            self.refuse(rid, typed_error(409, "invalid_batch_state", "Batch %s has already ended %s." % (bid, b["state"])))
            return
        if b["state"] == "cancelled" or b["cancel_requested"]:
            self.batch_send(rid, 200, b)
            return
        if b["state"] == "draft":
            self.end_batch(b, "cancelled", now)
            self.batch_send(rid, 200, b)
            return
        for ln in b["lines"]:
            if ln["state"] == "queued":
                ln.update(status="cancelled", result_text="null", headers={}, state="done", tokens=0)
        if any(ln["state"] == "in_progress" for ln in b["lines"]):
            b["cancel_requested"] = True
        else:
            self.end_batch(b, "cancelled", now)
        self.batch_send(rid, 200, b)

    def query_int(self, query, name, default, lo, hi, rule):
        values = query.get(name)
        if not values:
            return default
        v = values[0]
        if re.fullmatch(r"\d+", v or "") and lo <= int(v) <= (hi if hi is not None else int(v)):
            return int(v)
        raise usage_error("Query parameter %s must be %s, got %s." % (name, rule, v))

    def api_batch_list(self, rid, started, body_bytes, query):
        try:
            offset = self.query_int(query, "offset", 0, 0, None, "an integer of 0 or more")
            limit = self.query_int(query, "limit", 20, 1, 100, "an integer from 1 to 100")
        except Refused as exc:
            self.refuse(rid, exc)
            return
        key = self.key()
        now = self.now()
        mine = [b for b in self.server.state.batches.values() if b["key"] == key and b["sid"] == self.sid]
        for b in mine:
            self.advance(b, now, poll=False)
        mine.sort(key=lambda b: (b["created_at"], b["seq"], b["id"]), reverse=True)
        page = mine[offset : offset + limit]
        nxt = offset + len(page) if offset + len(page) < len(mine) else None
        self.send(200, {"batches": [self.batch_object(b) for b in page], "offset": offset, "limit": limit, "total": len(mine), "next_offset": nxt}, {"x-request-id": rid})

    # ----------------------------------------------------------------- collections

    def collection_conf(self) -> dict:
        """The scenario's collections setting: faces (how many faces every enrollment
        finds), enrollment (unpriced or face_model_unavailable: every enrollment's 529),
        max_collections, max_items and region_unavailable."""
        return self.server.state.flag(self.sid)["collections"]

    def public_collections(self) -> list:
        return [{"id": "celebrities", "type": "face", "name": "celebrities", "public": True, "items": PUBLIC_COLLECTION_ITEMS["celebrities"], "created_at": "2026-10-12T00:00:00Z"}]

    def own_collections(self) -> list:
        ws = workspace_of(self.key())
        mine = [c for c in self.server.state.collections.values() if c["workspace"] == ws and c["sid"] == self.sid]
        return sorted(mine, key=lambda c: c["seq"], reverse=True)

    def collection_object(self, c) -> dict:
        return {"id": c["id"], "type": c["type"], "name": c["name"], "public": False, "items": len(c["items"]), "created_at": c["created_at"], "deletion": None}

    def find_collection(self, cid, search: bool = False):
        """(own collection, public object): a foreign or unknown id is collection_not_found,
        the same answer in both cases. wanted-persons is not offered: a route naming it
        answers as an unknown id, a search of it (search) the 403 collection_not_enabled."""
        if cid == "wanted-persons":
            if search:
                raise typed_error(403, "collection_not_enabled", "wanted-persons is not enabled for this workspace. Contact hello@bynn.com.")
            raise typed_error(404, "collection_not_found", "Collection not found: %s." % cid)
        for c in self.own_collections():
            if c["id"] == cid:
                return c, None
        for pub in self.public_collections():
            if pub["id"] == cid:
                return None, pub
        raise typed_error(404, "collection_not_found", "Collection not found: %s." % cid)

    def search_collection(self, cid: str) -> dict:
        """booster_collection for this request's key: a decide key without leave to search
        collections is refused once face_recognition's options are checked, before any
        collection is looked up or any booster runs."""
        if not may_search_collections(self.key()):
            raise typed_error(403, "key_scope_denied", KEY_SEARCH_DENIED)
        return self.booster_collection(cid)

    def booster_collection(self, cid: str) -> dict:
        """The collection face_recognition searches: {id, kind, items}; wanted-persons is
        its 403 collection_not_enabled, any other unknown or foreign id the 404."""
        own, pub = self.find_collection(cid, search=True)
        if pub:
            return {"id": pub["id"], "kind": "celebrities", "name": None, "items": []}
        items = sorted(own["items"].values(), key=lambda i: i["seq"])
        # The display name as the search carries it: cut to 64 characters, none when it
        # folds to nothing (control and separator characters as spaces, then trimmed).
        name = re.sub(r"\s+", " ", "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in own["name"])).strip()[:64].strip() or None
        return {"id": own["id"], "kind": "own", "name": name, "items": [{"reference": i["reference"], "label": i["label"], "metadata": i.get("metadata") or {}} for i in items]}

    def idem_begin(self, rid, body_bytes):
        """An Idempotency-Key on a collections POST. None: no key, the
        call runs as before. False: answered here (the first answer replayed, or a 409).
        Otherwise the key, held as running until the call ends. A key is the workspace's,
        kept 24 hours, and bound to the route and the body's SHA-256."""
        keys = self.headers.get_all("Idempotency-Key") or []
        if not keys:
            return None
        key = keys[0]
        if len(keys) > 1 or not 1 <= len(key) <= 255 or any(not 0x20 <= ord(ch) <= 0x7E for ch in key):
            self.refuse(rid, usage_error(IDEMPOTENCY_MALFORMED, IDEMPOTENCY_MALFORMED))
            return False
        state: State = self.server.state
        k = (self.sid, workspace_of(self.key()), key)
        route = self.command + " " + urlsplit(self.path).path
        sha = hashlib.sha256(body_bytes).hexdigest()
        now = self.now()
        with state.lock:
            e = state.idempotency.get(k)
            if e is not None and now - e["at"] > IDEMPOTENCY_WINDOW_S:
                e = None
            if e is None:
                state.idempotency[k] = {"state": "running", "route": route, "sha": sha, "at": now}
                return k
        if e["route"] != route or e["sha"] != sha:
            self.refuse(rid, typed_error(409, "idempotency_key_reused", IDEMPOTENCY_REUSED, IDEMPOTENCY_REUSED))
        elif e["state"] == "running":
            self.refuse(rid, typed_error(409, "idempotency_in_progress", IDEMPOTENCY_IN_PROGRESS, IDEMPOTENCY_IN_PROGRESS, headers={"Retry-After": "1", "x-decisionnode-retry-after-ms": "1000"}))
        else:
            self.send(e["status"], None, {"x-request-id": rid, "x-decisionnode-idempotent-replay": "true"}, raw_text=e["text"])
        return False

    def idem_keep(self, k, status, text) -> None:
        with self.server.state.lock:
            e = self.server.state.idempotency.get(k)
            if e is not None:
                e.update(state="done", status=status, text=text)

    def idem_send(self, k, rid, status, body) -> None:
        """The answer that changed something, kept for the key's replays, then sent."""
        text = json.dumps(body, ensure_ascii=False)
        if k:
            self.idem_keep(k, status, text)
        self.send(status, None, {"x-request-id": rid}, raw_text=text)

    def idem_refuse(self, k, rid, exc) -> None:
        """A refusal: kept for replay only when it was billed (the 422 no_face), else the
        key is let go, so a retry runs again."""
        if k:
            detail = exc.body.get("detail") if isinstance(exc.body, dict) else None
            if exc.status == 422 and isinstance(detail, list) and any(isinstance(d, dict) and d.get("type") == "no_face" for d in detail):
                self.idem_keep(k, 422, json.dumps(exc.body, ensure_ascii=False))
            else:
                with self.server.state.lock:
                    self.server.state.idempotency.pop(k, None)
        self.refuse(rid, exc)

    def api_collection_create(self, rid, started, body_bytes, query):
        # The body is checked before its key is taken, as the API does: a 422 is never kept.
        try:
            body = parse_body(body_bytes)
            if not isinstance(body, dict):
                raise refuse_422([centry("model_attributes_type", ["body"], "Input should be a valid dictionary or object to extract fields from", body)])
            entries = []
            ctype, name = body.get("type"), body.get("name")
            if ctype is None:
                entries.append(centry("missing", ["body", "type"], "Field required", body))
            elif ctype != "face":
                entries.append(centry("literal_error", ["body", "type"], "Input should be 'face'", ctype, {"expected": "'face'"}))
            if name is None:
                entries.append(centry("missing", ["body", "name"], "Field required", body))
            elif not isinstance(name, str):
                entries.append(centry("string_type", ["body", "name"], "Input should be a valid string", name))
            elif string_length(["body", "name"], name, COLLECTION_NAME_MAX):
                entries.append(string_length(["body", "name"], name, COLLECTION_NAME_MAX))
            elif any(forbidden_char(c) for c in name):
                entries.append(centry("string_pattern_mismatch", ["body", "name"], "A name holds no control or invisible characters", name))
            if entries:
                raise refuse_422(entries)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        idem = self.idem_begin(rid, body_bytes)
        if idem is False:
            return
        try:
            if any(c["name"] == name for c in self.own_collections()):
                raise refuse_422([centry("name_taken", ["body", "name"], "A collection with this name already exists in this workspace", name)])
            conf = self.collection_conf()
            if conf.get("region_unavailable"):
                raise typed_error(403, "collections_region_unavailable", "Collections are not available yet for this workspace's storage location.")
            most = int(conf.get("max_collections", COLLECTIONS_MAX))
            if len(self.own_collections()) >= most:
                raise typed_error(409, "collections_limit_reached", "This workspace has reached its limit of %d collections." % most)
        except Refused as exc:
            self.idem_refuse(idem, rid, exc)
            return
        state: State = self.server.state
        # The id follows from the workspace and the name, so a case can expect it; the
        # store is per mock session, so sessions never meet.
        ws = workspace_of(self.key())
        cid = "col_" + hashlib.sha256(("%s\n%s" % (ws, name)).encode("utf-8")).hexdigest()[:32]
        c = {"id": cid, "workspace": ws, "sid": self.sid, "type": "face", "name": name, "created_at": iso(math.floor(self.now())), "seq": next(CREATION_ORDER), "items": {}, "enrolled": 0}
        with state.lock:
            state.collections[(self.sid, cid)] = c
        self.idem_send(idem, rid, 201, self.collection_object(c))

    def api_collection_list(self, rid, started, body_bytes, query):
        out = [self.collection_object(c) for c in self.own_collections()] + self.public_collections()
        self.send(200, {"collections": out}, {"x-request-id": rid})

    def api_collection_get(self, rid, started, body_bytes, query, cid):
        try:
            own, pub = self.find_collection(cid)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        self.send(200, self.collection_object(own) if own else pub, {"x-request-id": rid})

    def api_collection_delete(self, rid, started, body_bytes, query, cid):
        try:
            own, pub = self.find_collection(cid)
            if pub:
                raise typed_error(403, "collection_read_only", "%s is a public collection and cannot be changed or listed." % cid)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        with self.server.state.lock:
            self.server.state.collections.pop((self.sid, own["id"]), None)
        self.send(200, {"id": own["id"], "deleted": True}, {"x-request-id": rid})

    def api_item_enroll(self, rid, started, body_bytes, query, cid):
        # The body is checked before its key is taken and before the collection is looked
        # up, as the API does: a 422 is never kept.
        try:
            body = parse_body(body_bytes)
            if not isinstance(body, dict):
                raise refuse_422([centry("model_attributes_type", ["body"], "Input should be a valid dictionary or object to extract fields from", body)])
            entries = []
            image, reference, label = body.get("image"), body.get("reference"), body.get("label")
            if image is None:
                entries.append(centry("missing", ["body", "image"], "Field required", body))
            elif isinstance(image, dict) and isinstance(image.get("id"), str) and string_length(["body", "image", "id"], image["id"], MAX_IMAGE_ID_CHARS):
                entries.append(string_length(["body", "image", "id"], image["id"], MAX_IMAGE_ID_CHARS))
            if reference is None:
                entries.append(centry("missing", ["body", "reference"], "Field required", body))
            else:
                entries.extend(reference_entries(["body", "reference"], reference))
            entries.extend(label_entries(["body", "label"], label))
            metadata = body.get("metadata", {})
            if "metadata" in body:
                entries.extend(metadata_entries(metadata))
            if entries:
                raise refuse_422(entries)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        idem = self.idem_begin(rid, body_bytes)
        if idem is False:
            return
        consumed: list = []
        try:
            own, pub = self.find_collection(cid)
            if pub:
                raise typed_error(403, "collection_read_only", "%s is a public collection and cannot be changed or listed." % cid)
            conf = self.collection_conf()
            if conf.get("region_unavailable"):
                raise typed_error(403, "collections_region_unavailable", "Collections are not available yet for this workspace's storage location.")
            most = int(conf.get("max_items", ITEMS_MAX))
            if sum(len(c["items"]) for c in self.own_collections()) >= most:
                raise typed_error(409, "items_limit_reached", "This workspace has reached its limit of %d items in its collections." % most)
            if conf.get("enrollment") == "unpriced":
                # No price for an enrollment: before anything is read, and no Retry-After.
                raise Refused(529, {"detail": {"error_type": "overloaded_error", "message": "Face enrollment is not available on this deployment yet.", "details": {"reason": "enrollment_unpriced"}}}, "Face enrollment is not available on this deployment yet.")
            if isinstance(image, dict) and isinstance(image.get("data"), str):
                # A picture that cannot be read is the 422 image_unreadable, never billed.
                try:
                    raw = base64.b64decode(image["data"], validate=True)
                except (binascii.Error, ValueError):
                    raw = None
                why = "not base64" if raw is None else ("not a JPEG, PNG or WebP image" if sniff(raw) is None else None)
                if why:
                    raise refuse_422([centry("image_unreadable", ["body", "image"], "The image could not be read: %s." % why, image.get("id"))])
            img = validate_images([image], self.resolver(consumed))[0]
            if conf.get("enrollment") == "face_model_unavailable":
                # No server serves the face model: found by its call, once the picture is read.
                raise Refused(529, {"detail": {"error_type": "overloaded_error", "message": "The face model is not available right now. Retry later.", "details": {"reason": "face_model_unavailable"}}}, "The face model is not available right now. Retry later.", {"Retry-After": "1", "retry-after-ms": "1000"})
            stream = Stream(img["digest"] + "\n" + reference)
            found = int(conf["faces"]) if "faces" in conf else 1 + int(stream.next() * 3)
            if found == 0:
                raise refuse_422([centry("no_face", ["body", "image"], "No face was found in the image.", img["id"])])
            xs = sorted((stream.next(), stream.next()))
            ys = sorted((stream.next(), stream.next()))
            box = {"x1": int(xs[0] * PLACE_WIDTH), "y1": int(ys[0] * PLACE_HEIGHT), "x2": int(xs[1] * PLACE_WIDTH), "y2": int(ys[1] * PLACE_HEIGHT)}
        except Refused as exc:
            self.idem_refuse(idem, rid, exc)
            return
        self.consume(consumed)
        own["enrolled"] += 1
        iid = "itm_" + hashlib.sha256(("%s\n%s\n%d" % (own["id"], reference, own["enrolled"])).encode("utf-8")).hexdigest()[:32]
        item = {"id": iid, "reference": reference, "label": label, "metadata": metadata, "created_at": iso(math.floor(self.now())), "seq": next(CREATION_ORDER)}
        with self.server.state.lock:
            own["items"][iid] = item
        out = {"id": iid, "collection": own["id"], "reference": reference, "label": label, "metadata": metadata, "faces_found": found, "box": box, "created_at": item["created_at"],
               "usage": {"enrollments": 1, "cost": {"usd": ENROLLMENT_USD}}}
        self.idem_send(idem, rid, 201, out)

    def item_object(self, i) -> dict:
        return {"id": i["id"], "reference": i["reference"], "label": i["label"], "metadata": i.get("metadata", {}), "created_at": i["created_at"]}

    def api_item_list(self, rid, started, body_bytes, query, cid):
        try:
            offset = self.query_int(query, "offset", 0, 0, None, "an integer of 0 or more")
            limit = self.query_int(query, "limit", 20, 1, 100, "an integer from 1 to 100")
            reference = (query.get("reference") or [None])[0]
            prefix = (query.get("reference_prefix") or [None])[0]
            if reference is not None and prefix is not None:
                raise usage_error("Send query parameter reference or reference_prefix, not both.")
            for name, raw in (("reference", reference), ("reference_prefix", prefix)):
                if raw is not None and not re.fullmatch(ITEM_REFERENCE_PATTERN, raw):
                    raise usage_error("Query parameter %s must be 1 to 64 characters of A-Z, a-z, 0-9, _, ., : and -, got %s." % (name, printable(raw, 64)))
            own, pub = self.find_collection(cid)
            if pub:
                raise typed_error(403, "collection_read_only", "%s is a public collection and cannot be changed or listed." % cid)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        if reference is None and prefix is None:
            items = sorted(own["items"].values(), key=lambda i: i["seq"], reverse=True)
        else:
            # Found by reference: in reference order (its bytes), then the id.
            keep = (lambda r: r == reference) if reference is not None else (lambda r: r.startswith(prefix))
            items = sorted((i for i in own["items"].values() if keep(i["reference"])), key=lambda i: (i["reference"].encode("utf-8"), i["id"]))
        total, capped = min(len(items), ITEMS_COUNTED), len(items) > ITEMS_COUNTED
        page = items[offset : offset + limit]
        nxt = offset + len(page) if offset + len(page) < len(items) else None
        self.send(200, {"items": [self.item_object(i) for i in page], "offset": offset, "limit": limit, "total": total, "total_capped": capped, "next_offset": nxt,
                        "reference": reference, "reference_prefix": prefix}, {"x-request-id": rid})

    def api_item_update(self, rid, started, body_bytes, query, cid, item_id):
        # The body is checked before its key is taken, as an enrollment's.
        try:
            body = parse_body(body_bytes)
            if not isinstance(body, dict):
                raise refuse_422([centry("model_attributes_type", ["body"], "Input should be a valid dictionary or object to extract fields from", body)])
            entries = []
            if "reference" in body:
                entries.extend(reference_entries(["body", "reference"], body["reference"]))
            entries.extend(label_entries(["body", "label"], body.get("label")))
            if "metadata" in body:
                entries.extend(metadata_entries(body["metadata"]))
            if not any(k in body for k in ("reference", "label", "metadata")):
                entries.append(centry("missing", ["body"], "Send reference, label, metadata or any of them", body))
            if entries:
                raise refuse_422(entries)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        idem = self.idem_begin(rid, body_bytes)
        if idem is False:
            return
        try:
            own, pub = self.find_collection(cid)
            if pub:
                raise typed_error(403, "collection_read_only", "%s is a public collection and cannot be changed or listed." % cid)
            item = own["items"].get(item_id)
            if item is None:
                raise typed_error(404, "item_not_found", "Item not found: %s." % item_id)
        except Refused as exc:
            self.idem_refuse(idem, rid, exc)
            return
        with self.server.state.lock:
            for k in ("reference", "label", "metadata"):
                if k in body:
                    item[k] = body[k]
        self.idem_send(idem, rid, 200, self.item_object(item))

    def api_item_delete(self, rid, started, body_bytes, query, cid, item_id):
        try:
            own, pub = self.find_collection(cid)
            if pub:
                raise typed_error(403, "collection_read_only", "%s is a public collection and cannot be changed or listed." % cid)
            if item_id not in own["items"]:
                raise typed_error(404, "item_not_found", "Item not found: %s." % item_id)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        with self.server.state.lock:
            own["items"].pop(item_id, None)
        self.send(200, {"id": item_id, "deleted": True}, {"x-request-id": rid})

    # ----------------------------------------------------------------- per-key usage

    def usage_pairs(self) -> list:
        """The query's pairs in order, repeats kept, as the API reads them."""
        return parse_qsl(urlsplit(self.path).query, keep_blank_values=True)[:256]

    def api_key_usage(self, rid, started, body_bytes, query, key_id):
        """GET /v1/keys/{key_id}/usage: one key's usage. The query is checked first; a key
        outside the caller's set, of another workspace or malformed, is the 404."""
        now = self.now()
        try:
            asked: dict = {}
            for name, value in self.usage_pairs():
                if not usage_period_take(asked, name, value):
                    raise usage_error("Unknown query parameter: %s." % printable(name, 32))
            span = usage_span(asked, now, defaults=True)
            visible = {k["id"]: k for k in usage_visible(workspace_of(self.key()), key_scope(self.key()))}
            key = visible.get(key_id) if KEY_ID.fullmatch(key_id) else None
            if key is None:
                raise typed_error(404, "not_found_error", "Not found.")
        except Refused as exc:
            self.refuse(rid, exc)
            return
        body = usage_of(key, span, now)
        out = {"key": body.pop("key"), "period": {"start": usage_iso(span["start"]), "end": usage_iso(span["end"])}, "as_of": usage_as_of(span, now)}
        out.update(body)
        self.send(200, out, {"x-request-id": rid, "Cache-Control": "private, no-store"})

    def api_usage_page(self, rid, started, body_bytes, query):
        """GET /v1/usage: a page of keys, newest first, each with its usage over the
        period; within the read's budget, so a page can hold fewer keys than limit."""
        now = self.now()
        try:
            asked: dict = {}
            labels, device, keys, scope, status, include_empty, limit, cursor = {}, None, [], None, "all", False, None, None
            for name, value in self.usage_pairs():
                if usage_period_take(asked, name, value):
                    continue
                if name == "label":
                    if ":" not in value:
                        raise usage_error("A label filter is name:value.")
                    k, v = value.split(":", 1)
                    if not LABEL_NAME.fullmatch(k) or not 1 <= len(v) <= 64 or any(unicodedata.category(c) == "Cc" for c in v):
                        raise usage_error("A label filter is name:value, as labels are written.")
                    labels[k] = v
                elif name == "device_id":
                    if not value or len(value) > 128 or not all(0x20 <= ord(c) <= 0x7E for c in value) or value.startswith(" "):
                        raise usage_error("device_id is 1 to 128 printable ASCII characters, not starting with a space.")
                    device = value
                elif name == "key_id":
                    if not KEY_ID.fullmatch(value):
                        raise usage_error("key_id is a key's id, key_ and 32 hex digits.")
                    if len(keys) == USAGE_MAX_KEY_IDS:
                        raise usage_error("At most 100 key_id filters.")
                    keys.append(value)
                elif name == "scope":
                    if value not in ("full", "decide", "keys"):
                        raise usage_error("scope is full, decide or keys.")
                    scope = value
                elif name == "status":
                    if value not in ("active", "expired", "revoked", "all"):
                        raise usage_error("status is active, expired, revoked or all.")
                    status = value
                elif name == "include_empty":
                    if value not in ("true", "false"):
                        raise usage_error("include_empty is true or false.")
                    include_empty = value == "true"
                elif name == "limit":
                    if not re.fullmatch(r"-?\d+", value) or not 1 <= int(value) <= USAGE_PAGE_MAX:
                        raise usage_error("limit is a whole number from 1 to 1000.")
                    limit = int(value)
                elif name == "cursor":
                    cursor = usage_parse_cursor(value)
                    if cursor is None:
                        raise usage_error("cursor is a next_cursor this list gave.")
                else:
                    raise usage_error("Unknown query parameter: %s." % printable(name, 32))
            span = usage_span(asked, now, defaults=False)
            bucketed = span["granularity"] != "none"
            if limit is not None and bucketed and limit > USAGE_PAGE_MAX_BUCKETED:
                raise usage_error("limit is at most 100 with a granularity other than none.")
        except Refused as exc:
            self.refuse(rid, exc)
            return
        hours = max(1, int((span["end"] - span["start"]) // 3600))
        limit = min(limit if limit is not None else (USAGE_PAGE_MAX_BUCKETED if bucketed else USAGE_PAGE_DEFAULT), max(1, USAGE_MAX_ROWS // hours))
        chosen = []
        for k in usage_visible(workspace_of(self.key()), key_scope(self.key())):
            st = usage_key_object(k, now)["status"]
            if status != "all" and st != status:
                continue
            if any(k["labels"].get(n) != v for n, v in labels.items()):
                continue
            if device is not None and k["device_id"] != device:
                continue
            if keys and k["id"] not in keys:
                continue
            if scope is not None and k["scope"] != scope:
                continue
            if cursor is not None and (int(usage_epoch(k["created_at"]) * 1_000_000), k["id"]) >= cursor:
                continue
            if not include_empty and not any(r[0] == k["id"] and span["start"] <= usage_epoch(r[1]) < span["end"] for r in USAGE_ROWS):
                continue
            chosen.append(k)
            if len(chosen) == limit + 1:
                break
        # As many as fit the read's budget: each key's hours times the models it ever used.
        fit, rows = 0, 0
        for k in chosen[:limit]:
            models = len({r[2] for r in USAGE_ROWS if r[0] == k["id"]})
            rows += hours * min(models, USAGE_MAX_MODELS_COUNTED)
            if fit > 0 and rows > USAGE_MAX_ROWS:
                break
            fit += 1
        more = len(chosen) > fit
        page = chosen[:fit]
        body = {"period": {"start": usage_iso(span["start"]), "end": usage_iso(span["end"])}, "as_of": usage_as_of(span, now),
                "items": [usage_of(k, span, now) for k in page], "next_cursor": usage_cursor(page[-1]) if more and page else None}
        self.send(200, body, {"x-request-id": rid, "Cache-Control": "private, no-store"})

    # ----------------------------------------------------------------- /v1/keys

    def managed_keys(self) -> list:
        """The session's workspace's decide keys, seeded from the usage fixture's."""
        state: State = self.server.state
        ws = workspace_of(self.key())
        with state.lock:
            if (self.sid, ws) not in state.api_keys:
                state.api_keys[(self.sid, ws)] = [
                    {"id": k["id"], "name": k["name"], "last4": k["id"][-4:], "device_id": k["device_id"], "labels": dict(k["labels"]),
                     "created_at": k["created_at"], "revoked_at": k["revoked_at"], "expires_at": k["expires_at"], "may_search": False,
                     "rps": None, "tps": None, "cap": None, "created_by": None, "salt": None}
                    for k in USAGE_KEYS.get(ws, []) if k["scope"] == "decide"]
            return state.api_keys[(self.sid, ws)]

    def find_key(self, raw: str) -> dict:
        for k in self.managed_keys():
            if k["id"] == raw:
                return k
        raise typed_error(404, "not_found_error", "Not found.")

    def key_object(self, rid, status, body):
        self.send(status, body, {"x-request-id": rid})

    def api_keys_create(self, rid, started, body_bytes, query):
        now = self.now()
        try:
            body = parse_body(body_bytes)
            if not isinstance(body, dict):
                raise refuse_422([key_entry("model_attributes_type", [], "Input should be an object", body)])
            name_value = body.get("name")
            if not isinstance(name_value, str):
                raise refuse_422([key_entry("missing", ["name"], "Field required", None)])
            try:
                name = key_name(name_value)
            except KeyFault as f:
                raise key_fault(f, name_value)
            f = key_fields(body, now, allow_device=True)
            keys = self.headers.get_all("Idempotency-Key") or []
            if len(keys) > 1 or (keys and not (1 <= len(keys[0]) <= 255 and all(0x20 <= ord(c) <= 0x7E for c in keys[0]))):
                raise usage_error(IDEMPOTENCY_MALFORMED)
        except Refused as exc:
            self.refuse(rid, exc)
            return
        state: State = self.server.state
        caller = calling_key_id(self.key())
        sha = hashlib.sha256(body_bytes).hexdigest()
        if keys:
            kept = state.key_creates.get((self.sid, workspace_of(self.key()), keys[0]))
            if kept is not None:
                if kept["sha"] != sha:
                    self.refuse(rid, typed_error(409, "idempotency_key_reused", IDEMPOTENCY_REUSED))
                    return
                if kept["by"] != caller:
                    self.refuse(rid, typed_error(409, "idempotency_key_reused", IDEMPOTENCY_REUSED))
                    return
                k = next(k for k in self.managed_keys() if k["id"] == kept["key_id"])
                if k["revoked_at"] is not None:
                    self.refuse(rid, typed_error(409, "key_not_replayable", "The key this Idempotency-Key made is revoked or its secret can no longer be shown: revoke it if it is not, then create a new key with a new Idempotency-Key."))
                    return
                self.send(201, {"key": key_wire(k, now), "secret": key_secret(k["id"], k["salt"])}, {"x-request-id": rid, "x-decisionnode-idempotent-replay": "true"})
                return
        device = f.get("device_id")
        for k in self.managed_keys():
            if key_status(k, now) != "active":
                continue
            if k["name"] == name:
                self.refuse(rid, refuse_422([key_entry("name_taken", ["name"], "An active key of this workspace has this name", name)]))
                return
            if device is not None and k["device_id"] == device:
                self.refuse(rid, refuse_422([key_entry("device_id_taken", ["device_id"], "An active key of this workspace has this device_id: revoke it first", device)]))
                return
        kid = "key_" + uuid.uuid4().hex
        salt = uuid.uuid4().hex
        secret = key_secret(kid, salt)
        rps, tps = f.get("limits", (None, None))
        k = {"id": kid, "name": name, "last4": secret[-4:], "device_id": device, "labels": f.get("labels", {}), "created_at": iso(math.floor(now)),
             "revoked_at": None, "expires_at": f.get("expires_at"), "may_search": f.get("may_search", False), "rps": rps, "tps": tps,
             "cap": f.get("cap"), "created_by": caller, "salt": salt, "seq": next(CREATION_ORDER)}
        with state.lock:
            self.managed_keys().append(k)
            if keys:
                state.key_creates[(self.sid, workspace_of(self.key()), keys[0])] = {"sha": sha, "by": caller, "key_id": kid}
        self.send(201, {"key": key_wire(k, now), "secret": secret}, {"x-request-id": rid})

    def api_keys_list(self, rid, started, body_bytes, query):
        now = self.now()
        try:
            labels, device, status, limit, cursor = {}, None, "active", KEYS_PAGE_DEFAULT, None
            for name, value in self.usage_pairs()[:64]:
                if name == "label":
                    if ":" not in value:
                        raise usage_error("A label filter is name:value.")
                    k, v = value.split(":", 1)
                    if not LABEL_NAME.fullmatch(k) or not 1 <= len(v) <= 64 or any(unicodedata.category(c) == "Cc" for c in v):
                        raise usage_error("A label filter is name:value, as labels are written.")
                    labels[k] = v
                elif name == "device_id":
                    try:
                        device = key_device_id(value)
                    except KeyFault as f:
                        raise usage_error(f.message)
                elif name == "status":
                    if value not in ("active", "expired", "revoked", "all"):
                        raise usage_error("status is active, expired, revoked or all.")
                    status = value
                elif name == "limit":
                    if not re.fullmatch(r"-?\d+", value) or not 1 <= int(value) <= KEYS_PAGE_MAX:
                        raise usage_error("limit is a whole number from 1 to 200.")
                    limit = int(value)
                elif name == "cursor":
                    cursor = usage_parse_cursor(value)
                    if cursor is None:
                        raise usage_error("cursor is a next_cursor this list gave.")
                else:
                    raise usage_error("Unknown query parameter: %s." % printable(name, 32))
        except Refused as exc:
            self.refuse(rid, exc)
            return
        rows = []
        for k in sorted(self.managed_keys(), key=lambda k: (k["created_at"], k["id"]), reverse=True):
            if status != "all" and key_status(k, now) != status:
                continue
            if any(k["labels"].get(n) != v for n, v in labels.items()):
                continue
            if device is not None and k["device_id"] != device:
                continue
            if cursor is not None and (int(usage_epoch(k["created_at"]) * 1_000_000), k["id"]) >= cursor:
                continue
            rows.append(k)
        page = rows[:limit]
        nxt = usage_cursor(page[-1]) if len(rows) > limit and page else None
        self.send(200, {"items": [key_wire(k, now) for k in page], "next_cursor": nxt}, {"x-request-id": rid})

    def api_keys_update(self, rid, started, body_bytes, query, key_id):
        now = self.now()
        try:
            body = parse_body(body_bytes)
            if not isinstance(body, dict):
                raise refuse_422([key_entry("model_attributes_type", [], "Input should be an object", body)])
            if not body:
                raise usage_error("Nothing to change.")
            name = None
            if "name" in body:
                try:
                    name = key_name(body["name"] if isinstance(body["name"], str) else "")
                except KeyFault as f:
                    raise key_fault(f, body["name"])
            f = key_fields(body, now, allow_device=False)
            k = self.find_key(key_id)
            if k["revoked_at"] is not None:
                raise typed_error(409, "already_revoked", "This key is revoked already.")
            name_only = list(body) == ["name"]
            if not name_only and key_status(k, now) == "expired":
                raise typed_error(409, "key_expired", "This key has expired: make a new one. An expired key is never brought back.")
            new_name = name if name is not None else k["name"]
            if any(o is not k and key_status(o, now) == "active" and o["name"] == new_name for o in self.managed_keys()):
                raise refuse_422([key_entry("name_taken", ["name"], "An active key of this workspace has this name", new_name)])
        except Refused as exc:
            self.refuse(rid, exc)
            return
        with self.server.state.lock:
            k["name"] = new_name
            if "labels" in f:
                k["labels"] = f["labels"]
            if "limits" in f:
                k["rps"], k["tps"] = f["limits"]
            if "cap" in f:
                k["cap"] = f["cap"]
            if "expires_at" in f:
                k["expires_at"] = f["expires_at"]
            if "may_search" in f:
                k["may_search"] = f["may_search"]
        self.send(200, key_wire(k, now), {"x-request-id": rid})

    def api_keys_revoke(self, rid, started, body_bytes, query, key_id):
        try:
            k = self.find_key(key_id)
            if k["revoked_at"] is not None:
                raise typed_error(409, "already_revoked", "This key is revoked already.")
        except Refused as exc:
            self.refuse(rid, exc)
            return
        with self.server.state.lock:
            k["revoked_at"] = iso(math.floor(self.now()))
        self.send(200, {"id": k["id"], "revoked": True}, {"x-request-id": rid})

    def api_keys_revoke_bulk(self, rid, started, body_bytes, query):
        try:
            body = parse_body(body_bytes)
            if not isinstance(body, dict):
                raise refuse_422([key_entry("model_attributes_type", [], "Input should be an object", body)])
            labels_value = body.get("labels")
            try:
                labels = key_labels(labels_value)
            except KeyFault as f:
                raise key_fault(f, labels_value)
            if not labels:
                raise refuse_422([key_entry("value_error", ["labels"], "A bulk revoke names at least one label: every key it revokes has them all.", labels_value)])
            devices = body.get("device_ids")
            if devices is not None:
                if not isinstance(devices, list) or len(devices) > KEYS_MAX_REVOKE_DEVICES:
                    raise refuse_422([key_entry("value_error", ["device_ids"], "device_ids is a list of at most 1,000 device ids.", devices)])
                for d in devices:
                    try:
                        key_device_id(d if isinstance(d, str) else "")
                    except KeyFault as f:
                        raise key_fault(f, d)
            dry_run = body.get("dry_run")
            if dry_run is not None and not isinstance(dry_run, bool):
                raise refuse_422([key_entry("bool_type", ["dry_run"], "Input should be a valid boolean", dry_run)])
            for name in body:
                if name not in ("labels", "device_ids", "dry_run"):
                    raise refuse_422([key_entry("extra_forbidden", [name], "Extra inputs are not permitted", None)])
        except Refused as exc:
            self.refuse(rid, exc)
            return
        picked = [k for k in self.managed_keys() if k["revoked_at"] is None and all(k["labels"].get(n) == v for n, v in labels.items())
                  and (devices is None or k["device_id"] in devices)]
        if dry_run:
            self.send(200, {"would_revoke": min(len(picked), KEYS_REVOKE_PER_CALL), "more": len(picked) > KEYS_REVOKE_PER_CALL}, {"x-request-id": rid})
            return
        done = sorted(picked, key=lambda k: k["id"])[:KEYS_REVOKE_PER_CALL]
        at = iso(math.floor(self.now()))
        with self.server.state.lock:
            for k in done:
                k["revoked_at"] = at
        self.send(200, {"revoked": len(done), "more": len(picked) > len(done)}, {"x-request-id": rid})

    def result_line(self, ln) -> str:
        return '{"custom_id":%s,"status":%s,"body":%s,"headers":%s}' % (compact(ln["custom_id"]), compact(ln["status"]), ln["result_text"], compact(ln["headers"]))

    def api_batch_results(self, rid, started, body_bytes, query, bid):
        b = self.batch_of(rid, bid)
        if b is None:
            return
        now = self.now()
        self.advance(b, now, poll=False)
        try:
            fmt = (query.get("format") or [None])[0]
            if fmt is not None and fmt != "json":
                raise usage_error("Query parameter format must be json, got %s." % fmt)
            if b["ended_at"] is None:
                raise typed_error(409, "results_not_ready", "Batch %s is %s. Results are ready when it has ended." % (bid, b["state"]))
            if now >= b["ended_at"] + BATCH_RESULTS_DAYS * 86400:
                raise typed_error(410, "results_expired", "The results of batch %s were deleted 7 days after it ended." % bid)
            offset = self.query_int(query, "offset", 0, 0, None, "an integer of 0 or more") if fmt else 0
            limit = self.query_int(query, "limit", 1000, 1, 1000, "an integer from 1 to 1000") if fmt else 1000
        except Refused as exc:
            self.refuse(rid, exc)
            return
        lines = [self.result_line(ln) for ln in b["lines"]]
        if fmt == "json":
            page = lines[offset : offset + limit]
            nxt = offset + len(page) if offset + len(page) < len(lines) else None
            text = '{"results":[%s],"offset":%d,"limit":%d,"total":%d,"next_offset":%s}' % (",".join(page), offset, limit, len(lines), compact(nxt))
            self.send(200, None, {"x-request-id": rid}, raw_text=text)
            return
        data = "".join(line + "\n" for line in lines).encode("utf-8")
        self.send(200, None, {"x-request-id": rid, "Content-Type": "application/x-ndjson", "ETag": '"%s"' % hashlib.sha256(data).hexdigest()}, raw_bytes=data)

    # ---- control

    def control(self, method: str, path: str, query: dict, body_bytes: bytes) -> None:
        state: State = self.server.state
        sid_q = (query.get("session") or ["default"])[0]

        def body_json():
            try:
                return json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except (ValueError, UnicodeDecodeError, RecursionError):
                return None

        if path == "/_mock/health" and method == "GET":
            self.send(200, {"ok": True, "version": VERSION, "scenarios": len(state.scenarios)})
        elif path == "/_mock/reset" and method == "POST":
            sid = (query.get("session") or [None])[0]
            state.reset(sid)
            self.send(200, {"ok": True})
        elif path == "/_mock/scenarios" and method == "GET":
            self.send(200, {"scenarios": sorted(state.scenarios)})
        elif path == "/_mock/scenarios" and method == "POST":
            try:
                scenario = json.loads(body_bytes.decode("utf-8"))
                name = scenario.pop("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("name")
                settings = ("steps", "rules", "clock_offset_seconds", "workspace", "truth_as_noul", "batch", "session", "boosters", "workers", "places", "collections")
                if not any(k in scenario for k in settings):
                    raise ValueError("steps or rules")
            except (ValueError, KeyError, AttributeError, UnicodeDecodeError) as exc:
                self.send(400, {"error": "a scenario needs a name and steps or rules: %s" % exc})
                return
            with state.lock:
                state.scenarios[name] = scenario
                for key in [k for k in state.counters if k[1] == name]:
                    del state.counters[key]
                state.applied = {a for a in state.applied if a[1] != name}
            self.send(200, {"ok": True, "name": name})
        elif path == "/_mock/requests" and method == "GET":
            sid = sid_q
            only = (query.get("path") or [None])[0]
            with state.lock:
                if sid == "*":
                    reqs = [r for sess in state.sessions.values() for r in sess["requests"]]
                    reqs.sort(key=lambda r: r["time"])
                    peak = max((sess["max_in_flight"] for sess in state.sessions.values()), default=0)
                else:
                    sess = state.sessions.get(sid) or {"requests": [], "max_in_flight": 0}
                    reqs, peak = list(sess["requests"]), sess["max_in_flight"]
            if only is not None:
                reqs = [r for r in reqs if r["path"] == only]
            self.send(200, {"session": sid, "requests": reqs, "max_in_flight": peak})
        elif path == "/_mock/clock" and method == "GET":
            # The session's clock, for a harness whose SDK takes an injectable clock
            # (client.clock "mock" in a case): epoch_seconds is the time the mock's
            # storage, uploads, batches and sessions judge by.
            with state.lock:
                offset = state.offsets.get(sid_q, 0.0)
            now = time.time() + offset
            self.send(200, {"offset_seconds": offset, "epoch_seconds": now, "now": iso(now)})
        elif path == "/_mock/clock" and method == "POST":
            body = body_json()
            if not isinstance(body, dict):
                self.send(400, {"error": "send {\"advance_seconds\": n} or {\"offset_seconds\": n}"})
                return
            with state.lock:
                if "offset_seconds" in body:
                    state.offsets[sid_q] = float(body["offset_seconds"])
                if "advance_seconds" in body:
                    state.offsets[sid_q] = state.offsets.get(sid_q, 0.0) + float(body["advance_seconds"])
                offset = state.offsets.get(sid_q, 0.0)
            self.send(200, {"ok": True, "offset_seconds": offset, "now": iso(time.time() + offset)})
        elif path in ("/_mock/workspace", "/_mock/truth", "/_mock/batches", "/_mock/sessions", "/_mock/boosters", "/_mock/workers") and method == "POST":
            body = body_json()
            if not isinstance(body, dict):
                self.send(400, {"error": "send a JSON object"})
                return
            with state.lock:
                f = state.flag(sid_q)
                if path == "/_mock/workspace":
                    if "zero_retention" in body:
                        f["zero_retention"] = bool(body["zero_retention"])
                    out = {"zero_retention": f["zero_retention"]}
                elif path == "/_mock/truth":
                    if "legacy_noul" in body:
                        f["legacy_noul"] = bool(body["legacy_noul"])
                    out = {"legacy_noul": f["legacy_noul"]}
                elif path == "/_mock/batches":
                    f["batch"].update(body)
                    out = dict(f["batch"])
                elif path == "/_mock/boosters":
                    f["boosters"].update(body)
                    out = dict(f["boosters"])
                elif path == "/_mock/workers":
                    f["workers"].update(body)
                    out = dict(f["workers"])
                else:
                    f["session"].update(body)
                    out = dict(f["session"])
            self.send(200, dict(out, ok=True))
        elif path == "/_mock/uploads" and method == "GET":
            with state.lock:
                ups = [
                    {k: up[k] for k in ("id", "workspace", "content_type", "byte_size", "state", "puts", "authorization_seen")}
                    | {"expires_at": iso(up["expires_at"]), "stored": up["object_key"] in state.objects}
                    for up in state.uploads.values()
                    if up["sid"] == sid_q
                ]
            self.send(200, {"uploads": ups})
        else:
            self.send(404, {"error": "unknown control endpoint"})


# --------------------------------------------------------------------------- a session's socket


class SessionLoop:
    """One attached socket: latest wins, replies, frame errors, pings, timers and closes."""

    def __init__(self, handler: Handler, conn: WsConn, ses: dict, conf: dict):
        self.h = handler
        self.conn = conn
        self.ses = ses
        self.conf = conf
        self.state: State = handler.server.state
        self.events: queue.Queue = queue.Queue()
        self.cond = threading.Condition()
        self.running = None
        self.waiting = None
        self.carry: list = []
        self.accepted = 0
        self.replies = 0
        self.stop = False
        self.close_request = None
        self.last_seq = None
        self.frame_errors = {str(k): v for k, v in (conf.get("frame_errors") or {}).items()}

    def record(self, method: str, text: str) -> None:
        rec = {
            "time": time.time(),
            "method": method,
            "path": "/v1/sessions/%s/stream" % self.ses["id"],
            "query": "",
            "headers": {},
            "body_text": text,
            "body_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "body": None,
            "scenario": self.ses.get("scenario"),
            "attempt": 0,
            "status": None,
        }
        try:
            rec["body"] = json.loads(text) if text else None
        except (ValueError, RecursionError):
            pass
        with self.state.lock:
            self.state.session(self.ses["sid"])["requests"].append(rec)

    def reader(self) -> None:
        try:
            while True:
                kind, payload = self.conn.read_message()
                if kind == "ping":
                    self.conn.send(0xA, payload)
                    self.events.put(("inbound", None))
                    continue
                if kind == "pong":
                    self.record("PONG", "")
                    self.events.put(("inbound", None))
                    continue
                self.events.put((kind, payload))
                if kind in ("close", "too_big"):
                    return
        except (EOFError, OSError, ValueError, OverflowError, MemoryError):
            # The client went away mid-stream or sent a malformed frame: the session
            # loop sees the end of the socket and finishes the same for any drop.
            self.events.put(("eof", None))
        except Exception:  # noqa: BLE001 - a fault here must still end the loop
            mock_log(self.h.server, "mock fault in a session reader:\n" + traceback.format_exc())
            self.events.put(("eof", None))

    def frame_error(self, seq, error_type: str, message: str, extra: dict | None = None) -> None:
        msg = {"seq": seq, "error": {"error_type": error_type, "message": message}}
        if extra:
            if "category" in extra:
                msg["error"]["category"] = extra["category"]
            for k in ("retry_after_ms", "usage", "safety"):
                if k in extra:
                    msg[k] = extra[k]
        self.conn.send_json(compact(msg))

    def validate_frame(self, text: str):
        """(frame, None) for an accepted frame, (None, (seq, message)) for a validation error."""
        try:
            f = json.loads(text)
        except (ValueError, RecursionError):
            return None, (None, "Frames are text messages holding one JSON object.")
        if not isinstance(f, dict):
            return None, (None, "Frames are text messages holding one JSON object.")
        seq = f.get("seq")
        if seq is not None and (not is_int(seq) or not 0 <= seq <= 2**53 - 1):
            return None, (None, "seq: must be an integer from 0 to 9007199254740991.")
        if seq is None:
            seq = 0 if self.last_seq is None else self.last_seq + 1
        if nesting_depth(f) > MAX_NESTING:
            return None, (seq, "The frame nests more than %d objects and arrays inside each other." % MAX_NESTING)
        for name in f:
            if name not in ("seq", "frame", "image", "questions", "reset"):
                return None, (seq, "%s: this field is not taken in a frame." % name)
        frame = f.get("frame")
        image = f.get("image")
        if frame is None and image is None:
            return None, (seq, "frame: a frame needs a frame value or an image.")
        if frame is not None and (not is_sdl(frame) or frame == ""):
            return None, (seq, "frame: must be a string, an object or an array, not empty.")
        questions = f.get("questions")
        if questions is not None:
            if not isinstance(questions, list) or not questions:
                return None, (seq, "questions: must be a non-empty list of the session's question ids.")
            for qid in questions:
                if qid not in self.ses["questions"]:
                    return None, (seq, "questions: unknown question id %s" % ("'%s'" % qid if isinstance(qid, str) else compact(qid)))
        reset = f.get("reset")
        if reset is not None and not isinstance(reset, bool):
            return None, (seq, "reset: must be true or false.")
        tokens = 0
        if frame is not None:
            text_value = frame if isinstance(frame, str) else compact(frame)
            tokens = math.ceil(len(text_value.encode("utf-8")) / 4)
            if tokens > SESSION_DEFAULTS["max_frame_tokens"]:
                return None, (seq, "The frame is %d tokens, the limit is %d." % (tokens, SESSION_DEFAULTS["max_frame_tokens"]))
        raw_image = None
        if image is not None:
            if not isinstance(image, dict):
                return None, (seq, "image: must be an object with media_type and data, or upload_id.")
            if "upload_id" in image:
                if any(k not in ("upload_id", "media_type") for k in image):
                    return None, (seq, "image: upload_id takes no data.")
                consumed: list = []
                try:
                    raw_image = self.h.resolver(consumed)("frame", image["upload_id"], image.get("media_type"))
                except Refused as exc:
                    return None, (seq, exc.body["detail"]["message"], exc.body["detail"]["error_type"])
                self.h.consume(consumed)
            else:
                if set(image) != {"media_type", "data"} or image.get("media_type") not in MEDIA_TYPES or not isinstance(image.get("data"), str):
                    return None, (seq, "image: must be an object with media_type and data, or upload_id.")
                try:
                    raw_image = base64.b64decode(image["data"], validate=True)
                except (binascii.Error, ValueError):
                    return None, (seq, "image: cannot be decoded.")
                if len(raw_image) > MAX_IMAGE_BYTES:
                    return None, (seq, "image: over the limit of 10 MB.")
                if sniff(raw_image) != image["media_type"]:
                    return None, (seq, "image: cannot be decoded.")
        asked = self.ses["order"] if questions is None else [q for q in self.ses["order"] if q in set(questions)]
        return {"seq": seq, "frame": frame, "tokens": tokens, "image": raw_image, "questions": asked, "reset": bool(reset), "dropped": []}, None

    def accept(self, fr: dict) -> None:
        with self.cond:
            self.accepted += 1
            if self.running is None:
                self.running = fr
            else:
                if self.waiting is not None:
                    fr["dropped"] = self.waiting["dropped"] + [self.waiting["seq"]]
                    if self.waiting["reset"]:
                        fr["reset"] = True
                self.waiting = fr
            self.cond.notify_all()

    def answerer(self) -> None:
        hold = int(self.conf.get("hold_until_frames", 0) or 0)
        delay = (self.conf.get("frame_delay_ms") or 0) / 1000.0
        close_after = (self.conf.get("close") or {}).get("after_replies")
        while True:
            with self.cond:
                while not self.stop and (self.running is None or self.accepted < hold):
                    self.cond.wait(0.05)
                if self.stop:
                    return
                fr = self.running
            if delay:
                time.sleep(delay)
            if self.stop:
                return
            try:
                self.answer(fr)
            except Exception:  # noqa: BLE001 - one frame's fault never stops the session
                mock_log(self.h.server, "mock fault answering a frame:\n" + traceback.format_exc())
                self.frame_error(fr["seq"], "api_error", "An internal error occurred for this frame. Send it again.")
            with self.cond:
                self.running = self.waiting
                self.waiting = None
                if self.running is not None:
                    self.running["dropped"] = self.carry + self.running["dropped"]
                    self.carry = []
            if close_after is not None and self.replies >= int(close_after):
                c = self.conf["close"]
                self.close_request = (int(c.get("code", 1000)), c.get("reason", "closed by the mock scenario"))
                return

    def answer(self, fr: dict) -> None:
        ses = self.ses
        planned = self.frame_errors.get(str(fr["seq"]))
        if planned is not None and planned.get("error_type") != "rate_limit_error":
            et = planned["error_type"]
            if et == "refusal_error":
                cat = planned.get("category", "harm")
                billed_tokens = fr["tokens"] + sum(ses["q_tokens"][q] for q in fr["questions"])
                usage = {"input_tokens": billed_tokens, "output_tokens": 0, "cost": cost_of(ses["model"], billed_tokens)}
                ses["billed"] += usage["input_tokens"]
                msg = (
                    "This request was refused under the usage policy: it asks to choose, rank or target people to be harmed."
                    if cat == "harm"
                    else "This request was refused under the usage policy: it asks for help to harm oneself. If you or someone else is in danger, contact local emergency services or a crisis line."
                )
                safety = {"checked": True, "harm": 0.912 if cat == "harm" else 0.0021, "self_harm": 0.0041 if cat == "harm" else 0.954, "flagged": [cat], "blocked": cat}
                self.frame_error(fr["seq"], et, msg, {"category": cat, "usage": usage, "safety": safety})
            elif et == "overloaded_error":
                self.frame_error(fr["seq"], et, "The frame could not start in time. Retry after retry_after_ms, or send the next reading.", {"retry_after_ms": int(planned.get("retry_after_ms", 1500))})
            elif et == "api_error":
                self.frame_error(fr["seq"], et, "An internal error occurred for this frame. Send it again.")
            else:
                self.frame_error(fr["seq"], et, planned.get("message", "image: cannot be decoded."))
            self.carry = fr["dropped"] + self.carry
            return
        # the window: newest window - 1 earlier frames that fit window_tokens with this one
        history = [] if fr["reset"] else list(ses["history"])
        earlier = history[-(ses["window"] - 1):] if ses["window"] > 1 else []
        total = fr["tokens"] + 1
        kept = []
        for item in reversed(earlier):
            if total + item["tokens"] + 1 > SESSION_DEFAULTS["window_tokens"]:
                break
            total += item["tokens"] + 1
            kept.insert(0, item)
        token_cut = len(earlier) - len(kept)
        frames = [item["frame"] for item in kept] + ([fr["frame"]] if fr["frame"] is not None else [])
        state_text = compact({"instructions": ses["instructions"], "state": ses["state"], "frames": frames})
        images = [{"id": "frame", "digest": hashlib.sha256(fr["image"]).hexdigest()}] if fr["image"] is not None else []
        qs = {q: ses["questions"][q] for q in fr["questions"]}
        usage_tokens = fr["tokens"] + (IMAGE_TOKENS if images else 0) + sum(ses["q_tokens"][q] for q in fr["questions"])
        started = time.perf_counter()
        req = {"model": ses["model"], "state_text": state_text, "questions": qs, "answer_types": ses["answer_types"], "grids": ses["grids"], "images": images, "tokens": usage_tokens}
        body, seed = decide(req, self.state.flag(ses["sid"])["legacy_noul"])
        _, safety_body = safety_of(req, seed)
        reply = {"seq": fr["seq"], "answers": body["answers"]}
        if fr["dropped"]:
            reply["dropped"] = fr["dropped"]
        reply["usage"] = {"input_tokens": usage_tokens, "output_tokens": 0, "cost": cost_of(ses["model"], usage_tokens)}
        reply["latency_ms"] = round(max(0.1, (time.perf_counter() - started) * 1000), 1)
        reply["safety"] = safety_body
        with self.state.lock:
            if self.stop or ses["ended"] and ses.get("close_request"):
                return
            new_history = kept + ([{"frame": fr["frame"], "tokens": fr["tokens"]}] if fr["frame"] is not None else [])
            ses["history"] = new_history
            ses["cut"] += token_cut
            ses["answered"] += 1
            ses["dropped"] += len(fr["dropped"])
            ses["last_seq"] = fr["seq"]
            ses["billed"] += usage_tokens
        self.conn.send_json(compact(reply))
        self.replies += 1

    def run(self) -> None:
        ping_every = (self.conf.get("ping_interval_ms") or 20000) / 1000.0
        idle = (self.conf.get("idle_timeout_ms") or SESSION_DEFAULTS["idle_timeout_seconds"] * 1000) / 1000.0
        threading.Thread(target=self.reader, daemon=True).start()
        worker = threading.Thread(target=self.answerer, daemon=True)
        worker.start()
        now = time.time()
        last_frame = now
        last_inbound = now
        next_ping = now + ping_every
        code, reason = None, ""
        echo = False
        try:
            while True:
                now = time.time()
                if self.ses.get("close_request"):
                    code, reason = self.ses["close_request"]
                    break
                if self.close_request:
                    code, reason = self.close_request
                    break
                if self.state.now(self.ses["sid"]) >= self.ses["expires_at"]:
                    code, reason = 4410, "the session reached expires_at"
                    break
                if now - last_frame >= idle:
                    code, reason = 4408, "no frame for idle_timeout_seconds"
                    break
                if now - last_inbound >= SESSION_PING_TIMEOUT_S:
                    code = 1006
                    break
                if now >= next_ping:
                    self.conn.send(0x9, os.urandom(8))
                    next_ping = now + ping_every
                try:
                    kind, payload = self.events.get(timeout=0.02)
                except queue.Empty:
                    continue
                last_inbound = time.time()
                if kind == "inbound":
                    continue
                if kind == "eof":
                    code = None
                    break
                if kind == "close":
                    code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1000
                    reason = "closed by the client"
                    echo = True
                    break
                if kind == "too_big":
                    code, reason = 1009, "message over 1 MiB"
                    break
                last_frame = time.time()
                if kind == "binary":
                    self.record("WS", "")
                    self.frame_error(None, "api_usage_error", "Frames are text messages holding one JSON object.")
                    continue
                text = payload.decode("utf-8", errors="replace")
                self.record("WS", text)
                fr, err = self.validate_frame(text)
                if err is not None:
                    error_type = err[2] if len(err) > 2 else "api_usage_error"
                    self.frame_error(err[0], error_type, err[1])
                    continue
                planned = self.frame_errors.get(str(fr["seq"]))
                if planned is not None and planned.get("error_type") == "rate_limit_error":
                    self.frame_error(fr["seq"], "rate_limit_error", "The input-token rate is used up. Retry after retry_after_ms.", {"retry_after_ms": int(planned.get("retry_after_ms", 1000))})
                    continue
                self.last_seq = fr["seq"]
                self.accept(fr)
        except Exception:  # noqa: BLE001 - close the socket as the API would on a fault
            mock_log(self.h.server, "mock fault in a session:\n" + traceback.format_exc())
            code, reason, echo = 1011, "the mock server failed", False
        finally:
            with self.cond:
                self.stop = True
                pending = (self.waiting["dropped"] + [self.waiting["seq"]]) if self.waiting is not None else []
                self.cond.notify_all()
            with self.state.lock:
                self.ses["dropped"] += len(pending) + len(self.carry)
                self.ses["ended"] = True
        if echo:
            self.conn.close(code if code not in (1005, 1006) else 1000, "")
            self.conn.drop()
            return
        if code is None:
            self.conn.drop()
            return
        self.h.ws_finish(self.conn, code, reason, self.events)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # The listen backlog. The standard library's default of 5 refuses connections when
    # an SDK opens 8 or more at once (decide_many, parallel PUTs, sync and async runs).
    request_queue_size = 128
    verbose = False
    log_file = None

    def handle_error(self, request, client_address):
        # Quiet unless --verbose or --log-file: a traceback written to a stderr pipe that
        # the harness never reads can fill it and stall every thread that logs.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)):
            return
        mock_log(self, "mock fault serving %s:\n%s" % (client_address, traceback.format_exc()))


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="DecisionNode mock API server for SDK conformance tests.")
    parser.add_argument("--port", type=int, default=8787, help="port to listen on; 0 picks a free port")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cases", default=os.path.join(os.path.dirname(here), "cases"), help="cases directory whose request, response, error and behaviour cases become scenarios")
    parser.add_argument("--api-key", default=None, help="accept only this key (default: any key not starting with 'invalid')")
    parser.add_argument("--no-safety", action="store_true", help="answer like a self-hosted server: no x-decisionnode-safety header")
    parser.add_argument("--verbose", action="store_true", help="log every request and fault to stderr")
    parser.add_argument("--log-file", default=None, help="append every request and fault to this file instead (stderr stays silent)")
    args = parser.parse_args(argv)

    httpd = Server((args.host, args.port), Handler)
    httpd.state = State()
    httpd.verbose = args.verbose
    httpd.log_file = args.log_file
    httpd.safety = not args.no_safety
    httpd.api_key = args.api_key
    if os.path.isdir(args.cases):
        load_cases(httpd.state, args.cases)
    port = httpd.server_address[1]
    sys.stdout.write("listening on http://%s:%d\n" % (args.host, port))
    sys.stdout.flush()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
