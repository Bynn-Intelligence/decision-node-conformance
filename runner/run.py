#!/usr/bin/env python3
"""Reference runner: checks every case against the mock with the reference client.

    python3 runner/run.py                 start a mock on a free port and run everything
    python3 runner/run.py --mock-url URL  use a mock that is already running
    python3 runner/run.py --only behaviour --case retry-408

Exit status 0 when every case passes. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid
from urllib.parse import quote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import reference_client as dn  # noqa: E402
from minischema import Validator  # noqa: E402

KINDS = ("requests", "responses", "errors", "behaviour", "constants")
SCHEMAS = {
    "requests": "request-case.schema.json",
    "responses": "response-case.schema.json",
    "errors": "error-case.schema.json",
    "behaviour": "behaviour-case.schema.json",
    "constants": "constant-case.schema.json",
}
ENV_VARS = ("DECISIONNODE_API_KEY", "DECISIONNODE_BASE_URL", "DECISIONNODE_MODEL")
EXACT = {"probabilities", "legend", "raw", "entries", "safety", "body", "limits", "evidence", "boosters", "fallback", "cost"}
# Compared whole and in their key order too (suite 1.11): a decision's evidence is in the
# request's order, an error's boosters in the header's.
ORDERED = {"evidence", "boosters"}


class Fail(Exception):
    pass


# --------------------------------------------------------------------------- comparison


def is_pattern(v) -> bool:
    return isinstance(v, dict) and len(v) == 1 and isinstance(v.get("$pattern"), str)


def same(a, b) -> bool:
    """JSON equality with bools kept apart from numbers and floats compared exactly.
    An expected {"$pattern": regex} (suite 1.11) matches a string the regex finds."""
    if is_pattern(a):
        import re

        return isinstance(b, str) and re.search(a["$pattern"], b) is not None
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def match(expected, actual, path: str) -> None:
    """Every key of expected must be in actual with the same value. Keys in EXACT compare whole."""
    if isinstance(expected, dict) and isinstance(actual, dict) and not is_pattern(expected):
        for k, v in expected.items():
            if k not in actual:
                raise Fail("%s.%s: missing (expected %s)" % (path, k, short(v)))
            if k in EXACT:
                if not same(v, actual[k]):
                    raise Fail("%s.%s: expected %s, got %s" % (path, k, short(v), short(actual[k])))
                if k in ORDERED and isinstance(v, dict) and list(v) != list(actual[k]):
                    raise Fail("%s.%s: expected the order %s, got %s" % (path, k, list(v), list(actual[k])))
            else:
                match(v, actual[k], "%s.%s" % (path, k))
        return
    if not same(expected, actual):
        raise Fail("%s: expected %s, got %s" % (path, short(expected), short(actual)))


def short(v) -> str:
    text = json.dumps(v, ensure_ascii=False)
    return text if len(text) < 160 else text[:157] + "..."


def pointer(doc, ptr: str):
    if ptr == "":
        return True, doc
    cur = doc
    for raw in ptr[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


JSON_TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


# --------------------------------------------------------------------------- normalisation


def norm_answer(a) -> dict:
    if isinstance(a, dn.NumberAnswer):
        return {"kind": "number", "number": a.number, "expected": a.expected, "confidence": a.confidence, "probabilities": a.probabilities}
    if isinstance(a, dn.TruthAnswer):
        return {"kind": "truth", "truth": a.truth}
    if isinstance(a, dn.ChoiceAnswer):
        return {"kind": "choice", "choice": a.choice, "confidence": a.confidence, "probabilities": a.probabilities}
    if isinstance(a, dn.ScoreAnswer):
        return {
            "kind": "score",
            "score": a.score,
            "confidence": a.confidence,
            "probabilities": {str(k): v for k, v in a.probabilities.items()},
            "legend": {str(k): v for k, v in a.legend.items()},
        }
    if isinstance(a, (dn.PointAnswer, dn.BoxAnswer)):
        return dict({"kind": a.kind}, **{name: getattr(a, name) for name in a.FIELDS})
    if isinstance(a, dn.PointsAnswer):  # BoxesAnswer too
        out = {"kind": a.kind}
        for name in a.FIELDS:
            out[name] = [{n: getattr(p, n) for n in a.PLACE} for p in getattr(a, name)] if name == a.LIST else getattr(a, name)
        return out
    if isinstance(a, dn.UnknownAnswer):
        return {"kind": "unknown", "type": a.type, "raw": a.raw}
    raise Fail("answer of class %s is none of TruthAnswer, ChoiceAnswer, ScoreAnswer, NumberAnswer, PointAnswer, BoxAnswer, PointsAnswer, BoxesAnswer, UnknownAnswer" % type(a).__name__)


def norm_reading(r) -> dict:
    if isinstance(r, dn.UnknownReading):
        return {"kind": "unknown", "raw": r.raw}
    if isinstance(r, (dn.AgeReading, dn.AiGeneratedReading, dn.OcrReading)):
        return dict({"kind": r.kind}, **{name: getattr(r, name) for name in r.FIELDS})
    if isinstance(r, dn.FaceDetectionReading):
        return {"kind": r.kind, "image": r.image, "faces": r.faces, "detections": [{"box": norm_box(d.box), "confidence": d.confidence} for d in r.detections]}
    if isinstance(r, dn.FaceRecognitionReading):
        return {
            "kind": r.kind,
            "image": r.image,
            "faces": r.faces,
            "matches": [{"box": norm_box(m.box), "score": m.score, "collection": m.collection, "reference": m.reference, "name": m.name, "label": m.label} for m in r.matches],
        }
    raise Fail("reading of class %s is none of AgeReading, AiGeneratedReading, FaceDetectionReading, FaceRecognitionReading, OcrReading, UnknownReading" % type(r).__name__)


def norm_box(b) -> dict:
    return {"x1": b.x1, "y1": b.y1, "x2": b.x2, "y2": b.y2}


def norm_evidence(evidence):
    if evidence is None:
        return None
    return {
        name: {"version": e.version, "status": e.status, "latency_ms": e.latency_ms, "billed_calls": e.billed_calls, "results": [norm_reading(r) for r in e.results]}
        for name, e in evidence.items()
    }


def norm_decision(d) -> dict:
    return {
        "model": d.model,
        "request_id": d.request_id,
        "latency_ms": d.latency_ms,
        "usage": {
            "input_tokens": d.input_tokens,
            "output_tokens": d.output_tokens,
            "video_frames": d.video_frames,
            "video_seconds": d.video_seconds,
            "booster_tokens": d.booster_tokens,
            "boosters": d.booster_calls,
            "cost": None if d.cost is None else {"usd": d.cost.usd, "model_usd": d.cost.model_usd, "boosters_usd": d.cost.boosters_usd, "free_tier": d.cost.free_tier},
        },
        "evidence": norm_evidence(d.evidence),
        "fallback": d.fallback,
        "warnings": d.warnings,
        "safety": d.safety,
        "answers": {k: norm_answer(v) for k, v in d.answers.items()},
        "raw": d.raw,
    }


def norm_error(e) -> dict:
    return {
        "status": getattr(e, "status", None),
        "error_type": getattr(e, "error_type", None),
        # The error's own message (str(e) here), never a second field, and "" when the
        # server sent none.
        "message": str(e),
        "request_id": getattr(e, "request_id", None),
        "reason": getattr(e, "reason", None),
        "category": getattr(e, "category", None),
        "safety": getattr(e, "safety", None),
        "entries": getattr(e, "entries", None),
        "retry_after": getattr(e, "retry_after", None),
        "boosters": getattr(e, "boosters", None),
        # Suite 1.15: detail.details when it is an object, else empty.
        "details": getattr(e, "details", None),
    }


def check_error(spec: dict, err, path: str) -> None:
    cls = spec["error"] if "error" in spec else spec["class"]
    names = [c.__name__ for c in type(err).__mro__]
    if spec.get("subclass_ok"):
        if cls not in names:
            raise Fail("%s: expected %s or a subclass, got %s" % (path, cls, type(err).__name__))
    elif type(err).__name__ != cls:
        raise Fail("%s: expected %s, got %s (%s)" % (path, cls, type(err).__name__, err))
    match(spec.get("fields", {}), norm_error(err), path + ".fields")


# --------------------------------------------------------------------------- calls


def image_bytes(args: dict) -> bytes:
    import base64

    if "path" in args:
        with open(os.path.join(ROOT, args["path"]), "rb") as fh:
            data = fh.read()
    else:
        data = base64.b64decode(args["data_base64"])
    if "pad_to" in args:
        # The image's own bytes followed by zero bytes: the header (type, pixels) is
        # unchanged and the size is what the case needs.
        data = data + b"\0" * max(0, args["pad_to"] - len(data))
    return data


def decode(value, ctx=None):
    if isinstance(value, dict) and len(value) == 1:
        (key, args), = value.items()
        if key == "$ref":
            return resolve_ref(args, ctx or {})
        if key == "$text":
            return args["repeat"] * args["times"]
        if key == "$list":
            # count copies of item, with {i} in its strings replaced by the 0-based index
            return [decode(with_index(args["item"], i), ctx) for i in range(args["count"])]
        if key == "$nest":
            # value inside depth arrays: [[[...value...]]]
            out = decode(args["value"], ctx)
            for _ in range(args["depth"]):
                out = [out]
            return out
        if key == "$number":
            return dn.number(args.get("instructions"), args.get("min"), args.get("max"), args.get("step"))
        if key == "$truth":
            return dn.truth(args.get("instructions"), if_true=args.get("true"), if_false=args.get("false"))
        if key == "$choice":
            return dn.choice(args.get("instructions"), args["options"])
        if key == "$score":
            return dn.score(args.get("instructions"), args["levels"])
        if key == "$point":
            return dn.point(args.get("instructions"), args.get("image"), args.get("units"), args.get("max"))
        if key == "$box":
            return dn.box(args.get("instructions"), args.get("image"), args.get("units"), args.get("max"))
        if key == "$booster":
            args = {k: decode(v, ctx) for k, v in args.items()}
            return dn.Booster(args["name"], **{k: args[k] for k in ("images", "require", "options") if k in args})
        if key == "$video":
            args = {k: decode(v, ctx) for k, v in args.items()}
            extra = {k: args[k] for k in ("fps", "start", "end") if k in args}
            return dn.Video(args["id"], args["frames"], **extra)
        if key == "$frame":
            args = {k: decode(v, ctx) for k, v in args.items()}
            if "path" in args:
                with open(os.path.join(ROOT, args["path"]), "rb") as fh:
                    data = fh.read()
            elif "data_base64" in args:
                data = base64.b64decode(args["data_base64"])
            else:
                data = args["data_text"]
            return dn.VideoFrame(args["t"], data, args.get("media_type"))
        if key == "$image":
            args = {k: decode(v, ctx) for k, v in args.items()}
            if "url" in args:
                return dn.Image(args["id"], url=args["url"])
            if "upload_id" in args:
                return dn.Image(args["id"], upload_id=args["upload_id"], media_type=args.get("media_type"))
            if "path" in args and "pad_to" not in args:
                return dn.Image(args["id"], path=os.path.join(ROOT, args["path"]))
            # Without media_type the client is given bytes only and must detect the format.
            return dn.Image(args["id"], data=image_bytes(args), media_type=args.get("media_type"))
    if isinstance(value, dict):
        return {k: decode(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v, ctx) for v in value]
    return value


def with_index(value, i: int):
    if isinstance(value, str):
        return value.replace("{i}", str(i))
    if isinstance(value, dict):
        return {k: with_index(v, i) for k, v in value.items()}
    if isinstance(value, list):
        return [with_index(v, i) for v in value]
    return value


def resolve_ref(path: str, ctx: dict):
    name, _, rest = path.partition(".")
    cur = ctx[name]
    for part in rest.split(".") if rest else []:
        if isinstance(cur, dict):
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            cur = cur[int(part)]
        else:
            cur = getattr(cur, part)
    return cur


def invoke(client, call: dict, ctx=None):
    method, args = call["method"], call["args"]
    if method == "decide_raw":
        return client.decide_raw(args["body"])
    a = {k: decode(v, ctx) for k, v in args.items()}
    if "true" in a:
        a["if_true"] = a.pop("true")
    if "false" in a:
        a["if_false"] = a.pop("false")
    if call.get("check"):
        # Suite 1.6: the same request handed to the SDK's check instead of being sent.
        a.pop("batch_id", None)
        return client.check_request(method, **a)
    if method.startswith("batches."):
        return getattr(client.batches, method.split(".", 1)[1])(**a)
    if method.startswith("collections."):
        # Create, list, get, delete, enroll, list_items, delete_item, from 1.15 update_item.
        return getattr(client.collections, method.split(".", 1)[1])(**a)
    if method == "supervise":
        replies = []
        interval = a.pop("feed_interval_ms", 0) / 1000.0
        readings = a.pop("feed")

        def feed():
            for i, reading in enumerate(readings):
                if i:
                    time.sleep(interval)
                yield reading

        closed = client.supervise(a["body"], feed(), replies.append)
        return {"supervised": True, "replies": replies, "closed": closed}
    return getattr(client, method)(**a)


def make_client(case: dict, mock_url: str, extra_headers: dict):
    for var in ENV_VARS:
        os.environ.pop(var, None)
    for var, val in (case.get("env") or {}).items():
        os.environ[var] = val.replace("{mock_url}", mock_url)
    spec = dict(case.get("client") or {})
    kwargs = {}
    for k in ("api_key", "model", "timeout", "max_retries", "upload_timeout"):
        if spec.get(k) is not None:
            kwargs[k] = spec[k]
    if "base_url" in spec:
        if spec["base_url"] is not None:
            port = str(urlsplit(mock_url).port)
            kwargs["base_url"] = spec["base_url"].replace("{mock_url}", mock_url).replace("{mock_port}", port)
    else:
        kwargs["base_url"] = mock_url
    headers = dict(spec.get("headers") or {})
    headers.update(extra_headers)
    kwargs["headers"] = headers
    if spec.get("inline_threshold") is not None:
        kwargs["inline_threshold"] = spec["inline_threshold"]
    if spec.get("http_client") == "caller_following_redirects":
        # A client the caller made: urllib's default opener follows redirects.
        kwargs["http_client"] = urllib.request.build_opener()
    return dn.DecisionNode(**kwargs)


# --------------------------------------------------------------------------- the mock


class Mock:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        parts = urlsplit(self.url)
        self.host, self.port = parts.hostname, parts.port

    def raw(self, method: str, path: str, headers=None, body: bytes | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, text
        finally:
            conn.close()

    def requests(self, session: str, path: str | None = None) -> dict:
        query = "session=" + quote(session, safe="")
        if path is not None:
            query += "&path=" + quote(path, safe="")
        _, _, text = self.raw("GET", "/_mock/requests?" + query)
        out = json.loads(text)
        # Pongs on a session's socket are recorded for the pong cases, not as requests.
        out["pongs"] = sum(1 for r in out["requests"] if r["method"] == "PONG")
        out["requests"] = [r for r in out["requests"] if r["method"] != "PONG"]
        return out

    def clock(self, session: str, body: dict) -> None:
        self.raw("POST", "/_mock/clock?session=" + quote(session, safe=""), {"Content-Type": "application/json"}, json.dumps(body).encode())

    def clock_now(self, session: str) -> float:
        """The session's clock in epoch seconds: what a case with client.clock "mock" hands
        the SDK as its injectable clock."""
        _, _, text = self.raw("GET", "/_mock/clock?session=" + quote(session, safe=""))
        return float(json.loads(text)["epoch_seconds"])


def start_mock(cases_dir: str) -> tuple:
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "mock", "server.py"), "--port", "0", "--cases", cases_dir],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    line = proc.stdout.readline().strip()
    if not line.startswith("listening on "):
        proc.kill()
        raise SystemExit("the mock did not start: %r" % line)
    return proc, line[len("listening on "):]


# --------------------------------------------------------------------------- the four kinds


def run_request(case: dict, mock: Mock) -> None:
    session = uuid.uuid4().hex
    extra = {"x-mock-session": session}
    if case.get("mock"):
        extra["x-mock-scenario"] = "request/" + case["id"]
    client = make_client(case, mock.url, extra)
    invoke(client, case["call"])
    time.sleep(0.02)
    log = mock.requests(session)["requests"]
    exp = case["expect"]
    if "request" in exp:
        if len(log) != 1:
            raise Fail("expected 1 request, the mock received %d" % len(log))
        check_request(exp["request"], log[0], "request")
    else:
        check_request_list(exp["requests"], log)


def check_request_list(expected: list, log: list) -> None:
    if len(log) != len(expected):
        raise Fail("expected %d requests, the mock received %d: %s" % (len(expected), len(log), [r["method"] + " " + r["path"] for r in log]))
    i = 0
    while i < len(expected):
        if not expected[i].get("parallel"):
            check_request(expected[i], log[i], "requests[%d]" % i)
            i += 1
            continue
        j = i
        while j < len(expected) and expected[j].get("parallel"):
            j += 1
        # Requests sent in parallel may arrive in any order: one assignment must hold.
        # Found by backtracking over the requests not yet assigned, never by trying every
        # order (a case may hold sixteen parallel PUTs: 16! orders).
        reasons: list = []
        used: set = set()

        def assign(k: int, i: int = i, j: int = j) -> bool:
            if k == j:
                return True
            for g in range(i, j):
                if g in used:
                    continue
                try:
                    check_request(expected[k], log[g], "requests[%d]" % k)
                except Fail as exc:
                    reasons.append(str(exc))
                    continue
                used.add(g)
                if assign(k + 1):
                    return True
                used.discard(g)
            return False

        if not assign(i):
            raise Fail("no order of the parallel requests matches: " + reasons[0])
        i = j


def parse_jsonl(text: str) -> list:
    stripped = text.strip()
    if stripped == "":
        return []
    if stripped.startswith("["):
        return json.loads(stripped)
    return [json.loads(line) for line in text.split("\n") if line.strip()]


def check_request(exp: dict, got: dict, where: str) -> None:
    import re

    if got["method"] != exp["method"]:
        raise Fail("%s method: expected %s, got %s %s" % (where, exp["method"], got["method"], got["path"]))
    if "path" in exp and got["path"] != exp["path"]:
        raise Fail("%s path: expected %s, got %s" % (where, exp["path"], got["path"]))
    if "path_pattern" in exp and not re.search(exp["path_pattern"], got["path"]):
        raise Fail("%s path: %s does not match %s" % (where, got["path"], exp["path_pattern"]))
    if "query" in exp:
        from urllib.parse import parse_qs

        q = {k: v[0] for k, v in parse_qs(got.get("query") or "", keep_blank_values=True).items()}
        if q != exp["query"]:
            raise Fail("%s query: expected %s, got %s" % (where, exp["query"], q))
    if "status" in exp and got.get("status") != exp["status"]:
        raise Fail("%s status: the mock answered %s, expected %s" % (where, got.get("status"), exp["status"]))
    headers = got["headers"]
    for name, value in exp.get("headers", {}).items():
        if headers.get(name.lower()) != value:
            raise Fail("%s header %s: expected %r, got %r" % (where, name, value, headers.get(name.lower())))
    for name, pattern in exp.get("header_patterns", {}).items():
        val = headers.get(name.lower())
        if val is None or not re.search(pattern, val):
            raise Fail("%s header %s: %r does not match %s" % (where, name, val, pattern))
    for name in exp.get("absent_headers", []):
        if name.lower() in headers:
            raise Fail("%s header %s must not be sent" % (where, name))
    if "body_base64" in exp:
        if got.get("body_base64") != exp["body_base64"]:
            raise Fail("%s body: the bytes differ from the expected %d bytes" % (where, len(exp["body_base64"]) * 3 // 4))
        return
    if "body_sha256" in exp:
        import base64
        import hashlib

        digest = hashlib.sha256(base64.b64decode(got.get("body_base64") or "")).hexdigest()
        if digest != exp["body_sha256"]:
            raise Fail("%s body: sha256 %s, expected %s" % (where, digest, exp["body_sha256"]))
        return
    if "jsonl" in exp:
        lines = parse_jsonl(got["body_text"])
        if not same(exp["jsonl"], lines):
            raise Fail("%s body lines: expected %s, got %s" % (where, short(exp["jsonl"]), short(lines)))
        return
    if "jsonl_count" in exp or "jsonl_checks" in exp:
        lines = parse_jsonl(got["body_text"])
        if "jsonl_count" in exp and len(lines) != exp["jsonl_count"]:
            raise Fail("%s body: expected %d lines, got %d" % (where, exp["jsonl_count"], len(lines)))
        for check in exp.get("jsonl_checks", []):
            if check["line"] >= len(lines):
                raise Fail("%s body: no line %d" % (where, check["line"]))
            check_value(check, lines[check["line"]], "%s line %d" % (where, check["line"]))
        return
    body = json.loads(got["body_text"]) if got["body_text"] else None
    if "body" in exp:
        if not same(exp["body"], body):
            raise Fail("%s body: expected %s, got %s" % (where, short(exp["body"]), short(body)))
    elif "body_checks" not in exp and got["body_text"] and not exp.get("any_body"):
        raise Fail("%s expected no body, got %s" % (where, short(got["body_text"])))
    for check in exp.get("body_checks", []):
        check_value(check, body, where)
    for ptr in exp.get("ordered_keys", []):
        _, want = pointer(exp["body"], ptr)
        order = ordered_keys_of(got["body_text"], ptr)
        if order != list(want.keys()):
            raise Fail("%s body%s: key order expected %s, got %s" % (where, ptr, list(want.keys()), order))


def check_value(check: dict, body, where: str) -> None:
    import re

    found, val = pointer(body, check["pointer"])
    if check.get("absent"):
        if found:
            raise Fail("%s body%s: must be absent, got %s" % (where, check["pointer"], short(val)))
        return
    if not found:
        raise Fail("%s body%s: missing" % (where, check["pointer"]))
    if "equals" in check and not same(check["equals"], val):
        raise Fail("%s body%s: expected %s, got %s" % (where, check["pointer"], short(check["equals"]), short(val)))
    if "type" in check and not JSON_TYPES[check["type"]](val):
        raise Fail("%s body%s: expected a %s" % (where, check["pointer"], check["type"]))
    if "pattern" in check and (not isinstance(val, str) or not re.search(check["pattern"], val)):
        raise Fail("%s body%s: %s does not match %s" % (where, check["pointer"], short(val), check["pattern"]))
    if "keys" in check and (not isinstance(val, dict) or list(val.keys()) != check["keys"]):
        raise Fail("%s body%s: keys expected %s, got %s" % (where, check["pointer"], check["keys"], list(val.keys()) if isinstance(val, dict) else val))


def ordered_keys_of(text: str, ptr: str) -> list:
    doc = json.loads(text, object_pairs_hook=lambda pairs: {"__keys__": [k for k, _ in pairs], **{k: v for k, v in pairs}})
    found, node = pointer(doc, ptr)
    return node["__keys__"] if found and isinstance(node, dict) else []


def run_response(case: dict, mock: Mock) -> None:
    session = uuid.uuid4().hex
    client = make_client({"client": {"api_key": "dn_test_conformance"}}, mock.url, {"x-mock-session": session, "x-mock-scenario": "response/" + case["id"]})
    result = invoke(client, case["call"])
    exp = case["expect"]
    if "decision" in exp:
        match(exp["decision"], norm_decision(result), "decision")
    elif "answer" in exp:
        match(exp["answer"], norm_answer(result), "answer")
    elif "models" in exp:
        # The four fine-tuned fields (suite 1.11) are compared where the case lists them.
        typed = ("fine_tuned", "base", "adapter_version", "status")
        got = [
            dict({"name": m.name, "description": m.description, "release_date": m.release_date, "limits": m.limits}, **{k: getattr(m, k) for k in typed if k in e})
            for m, e in zip(result, exp["models"] + [{}] * max(0, len(result) - len(exp["models"])))
        ]
        if not same(exp["models"], got):
            raise Fail("models: expected %s, got %s" % (short(exp["models"]), short(got)))
    elif "raw" in exp:
        if not same(exp["raw"], result):
            raise Fail("raw: expected %s, got %s" % (short(exp["raw"]), short(result)))
    elif "batch" in exp:
        match(exp["batch"], result, "batch")
    elif "batch_page" in exp:
        match(exp["batch_page"], result, "batch_page")
    elif any(k in exp for k in COLLECTION_KINDS):
        kind = next(k for k in COLLECTION_KINDS if k in exp)
        check_collection_result(kind, exp[kind], result, kind)
    elif "batch_results" in exp:
        check_batch_results(exp["batch_results"], result, "batch_results")
    elif "session" in exp:
        match(exp["session"], norm_session(result), "session")
    elif "upload_ids" in exp:
        if not same(exp["upload_ids"], list(result)):
            raise Fail("upload_ids: expected %s, got %s" % (exp["upload_ids"], result))


# The collections calls' results, each the API's object as sent (an
# SDK's typed object is compared by its fields' wire names). collections is the list.
COLLECTION_KINDS = ("collection", "collections", "item", "item_page", "deleted")


def check_collection_result(kind: str, expected, result, path: str) -> None:
    if kind == "collections":
        if not isinstance(result, list):
            raise Fail("%s: expected a list of collections, got %s" % (path, type(result).__name__))
        if len(expected) != len(result):
            raise Fail("%s: expected %d collections, got %d" % (path, len(expected), len(result)))
        for i, (e, r) in enumerate(zip(expected, result)):
            match(e, r, "%s[%d]" % (path, i))
        return
    match(expected, result, path)


def norm_session(s) -> dict:
    out = dict(s.raw)
    out["answers"] = {k: norm_answer(v) for k, v in s.answers.items()}
    return out


def norm_batch_result(r) -> dict:
    out = {"custom_id": r.custom_id, "status": r.status, "headers": r.headers}
    if r.decision is not None:
        out["decision"] = norm_decision(r.decision)
    if r.error is not None:
        out["error"] = r.error
    return out


def check_results_spec(spec: dict, results, path: str) -> None:
    """batch_results whole, or (suite 1.6) count with batch_results_sample by index."""
    results = list(results)
    if "batch_results" in spec:
        check_batch_results(spec["batch_results"], results, path + ".batch_results")
    if "count" in spec and len(results) != spec["count"]:
        raise Fail("%s: expected %d results, got %d" % (path, spec["count"], len(results)))
    for sample in spec.get("batch_results_sample", []):
        i = sample["index"]
        if i >= len(results):
            raise Fail("%s: no result %d of %d" % (path, i, len(results)))
        check_batch_result({k: v for k, v in sample.items() if k != "index"}, results[i], "%s.batch_results[%d]" % (path, i))


def check_batch_results(expected: list, results, path: str) -> None:
    results = list(results)
    if len(results) != len(expected):
        raise Fail("%s: expected %d results, got %d" % (path, len(expected), len(results)))
    for i, (spec, res) in enumerate(zip(expected, results)):
        check_batch_result(spec, res, "%s[%d]" % (path, i))


def check_batch_result(spec: dict, res, where: str) -> None:
    got = norm_batch_result(res)
    for key in ("custom_id", "status", "headers"):
        if key in spec:
            if key == "headers":
                match(spec[key], got[key], where + ".headers")
            elif not same(spec[key], got[key]):
                raise Fail("%s.%s: expected %s, got %s" % (where, key, short(spec[key]), short(got[key])))
    if "decision" in spec:
        if "decision" not in got:
            raise Fail("%s: expected a decision, got none" % where)
        match(spec["decision"], got["decision"], where + ".decision")
    if "error" in spec:
        if "error" not in got:
            raise Fail("%s: expected an error value, got none" % where)
        check_error(spec["error"], got["error"], where + ".error")
    if spec.get("no_decision") and "decision" in got:
        raise Fail("%s: expected no decision" % where)


def run_error(case: dict, mock: Mock) -> None:
    session = uuid.uuid4().hex
    client = make_client(case, mock.url, {"x-mock-session": session, "x-mock-scenario": "error/" + case["id"]})
    spec = case["expect"]["error"]
    if "catch_as" in spec:
        # Catch by the exported name, as a harness writes `except DecisionNodeError`.
        base = getattr(dn, spec["catch_as"], None)
        if not (isinstance(base, type) and issubclass(base, BaseException)):
            raise Fail("the client exports no error class named %s" % spec["catch_as"])
    else:
        base = dn.DecisionNodeError
    try:
        invoke(client, case["call"])
    except base as err:
        check_error(spec, err, "error")
    except Exception as err:  # noqa: BLE001 - any other exception is a failure of the case
        raise Fail("raised %s (%s), which %s does not catch" % (type(err).__name__, err, base.__name__))
    else:
        raise Fail("no error was raised")
    log = mock.requests(session)["requests"]
    if len(log) != 1:
        raise Fail("expected 1 attempt with max_retries 0, got %d" % len(log))

    nat = case.get("natural_request")
    if nat:
        if "body_text" in nat:
            data = nat["body_text"].encode("utf-8")
        elif "body" in nat:
            data = json.dumps(nat["body"], ensure_ascii=False).encode("utf-8")
        else:
            data = None
        status, headers, text = mock.raw(nat["method"], nat["path"], nat["headers"], data)
        rec = case["response"]
        if status != rec["status"]:
            raise Fail("natural request: status %d, the case records %d (%s)" % (status, rec["status"], text[:200]))
        if not same(json.loads(text), json.loads(rec["body_text"])):
            raise Fail("natural request: body %s, the case records %s" % (short(text), short(rec["body_text"])))
        rec_headers = {k.lower(): v for k, v in rec["headers"].items()}
        for name in ("x-decisionnode-error", "x-decisionnode-boosters"):
            if name in rec_headers and headers.get(name) != rec_headers[name]:
                raise Fail("natural request: %s %r, the case records %r" % (name, headers.get(name), rec_headers[name]))


def check_outcome(spec: dict, result, path: str) -> None:
    if isinstance(result, dn.SessionClosed) and "closed" not in spec:
        raise Fail("%s: the call raised SessionClosed(%s)" % (path, result.code))
    if spec.get("argument_error"):
        if not isinstance(result, ValueError):
            raise Fail("%s: expected an argument error, got %s" % (path, type(result).__name__ if isinstance(result, BaseException) else "a result"))
        return
    if isinstance(result, ValueError) and not isinstance(result, dn.DecisionNodeError):
        raise Fail("%s: unexpected argument error: %s" % (path, result))
    if "result" in spec:
        if isinstance(result, BaseException):
            raise Fail("%s: expected %s, got %s: %s" % (path, spec["result"], type(result).__name__, result))
        kind = spec["result"]
        if kind == "decision":
            if not isinstance(result, dn.Decision):
                raise Fail("%s: expected a Decision, got %s" % (path, type(result).__name__))
            if "decision" in spec:
                match(spec["decision"], norm_decision(result), path + ".decision")
        elif kind == "answer":
            match(spec.get("answer", {}), norm_answer(result), path + ".answer")
        elif kind == "batch":
            match(spec.get("batch", {}), result, path + ".batch")
        elif kind == "batch_page":
            match(spec.get("batch_page", {}), result, path + ".batch_page")
        elif kind in COLLECTION_KINDS:
            check_collection_result(kind, spec.get(kind, {}), result, path + "." + kind)
        elif kind == "batch_results":
            check_results_spec(spec, result, path)
        elif kind == "batch_run":
            batch, results = result
            match(spec.get("batch", {}), batch, path + ".batch")
            check_results_spec(spec, results, path)
        elif kind == "checked":
            if result is not None:
                raise Fail("%s: check returned %r; a request that passes every check returns nothing" % (path, result))
        elif kind == "session":
            match(spec.get("session", {}), norm_session(result), path + ".session")
        elif kind == "upload_ids":
            import re

            if not isinstance(result, list) or len(result) != spec.get("count", len(result)):
                raise Fail("%s: expected %s upload ids, got %r" % (path, spec.get("count"), result))
            for uid in result:
                if not re.fullmatch(r"upl_[0-9a-f]{32}", uid or ""):
                    raise Fail("%s: %r is not an upload id" % (path, uid))
            if spec.get("distinct") is not None and (len(set(result)) == len(result)) != spec["distinct"]:
                raise Fail("%s: upload ids %s" % (path, result))
        elif kind == "supervised":
            if not isinstance(result, dict) or not result.get("supervised"):
                raise Fail("%s: expected supervise to return" % path)
            replies = [ev for ev in result["replies"] if isinstance(ev, dn.Reply)]
            in_range(spec.get("replies", {}), len(replies), "replies")
            if "closed" in spec:
                closed = result["closed"]
                got = None if closed is None else {"code": closed.code, "reopen": closed.reopen}
                if not same(spec["closed"], got):
                    raise Fail("%s.closed: expected %s, got %s" % (path, spec["closed"], got))
    else:
        if not isinstance(result, dn.DecisionNodeError):
            raise Fail("%s: expected %s, got a result" % (path, spec["error"]))
        check_error(spec, result, path)


def in_range(r: dict, value: float, what: str) -> None:
    if "min" in r and value < r["min"]:
        raise Fail("%s %.0f is below %s" % (what, value, r["min"]))
    if "max" in r and value > r["max"]:
        raise Fail("%s %.0f is above %s" % (what, value, r["max"]))


def norm_event(ev) -> dict:
    if isinstance(ev, dn.Reply):
        out = {"seq": ev.seq, "answers": {k: norm_answer(v) for k, v in ev.answers.items()}, "usage": ev.usage, "latency_ms": ev.latency_ms, "safety": ev.safety}
        if ev.dropped is not None:
            out["dropped"] = ev.dropped
        return {"reply": out}
    if isinstance(ev, dn.FrameError):
        out = {"seq": ev.seq, "error_type": ev.error_type, "message": ev.message}
        for k in ("category", "retry_after_ms", "usage", "safety"):
            if getattr(ev, k) is not None:
                out[k] = getattr(ev, k)
        return {"frame_error": out}
    if isinstance(ev, dn.SessionClosed):
        return {"closed": {"code": ev.code, "reopen": ev.reopen}}
    raise Fail("unknown session event %r" % (ev,))


def run_steps(case: dict, client, mock: Mock, session: str, events: list) -> None:
    ctx = {}
    for i, step in enumerate(case["steps"]):
        where = "steps[%d]" % i
        if "call" in step:
            try:
                result = invoke(client, step["call"], ctx)
            except (dn.DecisionNodeError, ValueError, dn.SessionClosed) as err:
                result = err
            if "outcome" in step:
                check_outcome(step["outcome"], result, where + ".outcome")
            elif isinstance(result, BaseException):
                raise Fail("%s: the call raised %s: %s" % (where, type(result).__name__, result))
            if "save_as" in step:
                ctx[step["save_as"]] = result
        elif "connect" in step:
            try:
                ctx[step["connect"]].connect()
            except dn.SessionClosed as closed:
                # A refused handshake may surface here or as the first event: both count.
                events.append(norm_event(closed))
        elif "send" in step:
            args = dict(step["send"])
            name = args.pop("session")
            session = ctx[name]
            act = session.check_frame if step.get("check") else session.send
            try:
                result = act(**{k: decode(v, ctx) for k, v in args.items()})
            except (dn.DecisionNodeError, ValueError) as err:
                # A frame that breaks a fixed rule is refused locally and nothing is sent.
                if "outcome" not in step:
                    raise
                check_outcome(step["outcome"], err, where + ".outcome")
            else:
                if step.get("check") and "outcome" in step:
                    check_outcome(step["outcome"], result, where + ".outcome")
                elif "outcome" in step:
                    raise Fail("%s: expected %s, the frame was sent" % (where, step["outcome"].get("error", step["outcome"])))
        elif "read" in step:
            spec = step["read"]
            s = ctx[spec["session"]]
            count = spec.get("count")
            while True:
                try:
                    ev = s.next_event(timeout=spec.get("timeout_ms", 10000) / 1000.0)
                except Exception:  # noqa: BLE001 - a missing event is reported below
                    raise Fail("%s: no session event arrived in time (%d read so far)" % (where, len(events)))
                events.append(norm_event(ev))
                if isinstance(ev, dn.SessionClosed):
                    break
                if count is not None:
                    count -= 1
                    if count == 0:
                        break
        elif "end" in step:
            try:
                ctx[step["end"]].end()
                events.append({"ended": True})
            except dn.DecisionNodeError as err:
                events.append({"end_error": {"class": type(err).__name__, "status": err.status}})
        elif "close" in step:
            ctx[step["close"]].close()
        elif "mock_clock" in step:
            mock.clock(session, step["mock_clock"])
        elif "sleep_ms" in step:
            time.sleep(step["sleep_ms"] / 1000.0)
        else:
            raise Fail("%s: unknown step %s" % (where, list(step)))


def check_sequence(expected: list, log: list) -> None:
    import re

    got = [r["method"] + " " + r["path"] for r in log]
    if len(expected) != len(log):
        raise Fail("request sequence: expected %d requests, the mock received %d: %s" % (len(expected), len(log), got))
    for i, (spec, rec) in enumerate(zip(expected, log)):
        if rec["method"] != spec["method"]:
            raise Fail("request sequence[%d]: expected %s, got %s" % (i, spec["method"], got[i]))
        if "path" in spec and rec["path"] != spec["path"]:
            raise Fail("request sequence[%d]: expected %s %s, got %s" % (i, spec["method"], spec["path"], got[i]))
        if "path_pattern" in spec and not re.search(spec["path_pattern"], rec["path"]):
            raise Fail("request sequence[%d]: %s does not match %s" % (i, got[i], spec["path_pattern"]))
        if "status" in spec and rec.get("status") != spec["status"]:
            raise Fail("request sequence[%d]: %s answered %s, expected %s" % (i, got[i], rec.get("status"), spec["status"]))
        checks = {k: v for k, v in spec.items() if k in ("headers", "header_patterns", "absent_headers", "body_checks", "jsonl_count", "jsonl_checks", "body_base64", "body_sha256", "query")}
        if checks:
            check_request(dict(checks, method=spec["method"], any_body=True), rec, "request sequence[%d]" % i)


def check_counts(expected: list, log: list) -> None:
    import re

    for spec in expected:
        n = sum(
            1
            for r in log
            if r["method"] == spec["method"] and ("path" not in spec or r["path"] == spec["path"]) and ("path_pattern" not in spec or re.search(spec["path_pattern"], r["path"]))
        )
        what = "%s %s" % (spec["method"], spec.get("path") or spec.get("path_pattern"))
        if "count" in spec and n != spec["count"]:
            raise Fail("%s: expected %d requests, got %d" % (what, spec["count"], n))
        in_range({k: spec[k] for k in ("min", "max") if k in spec}, n, what)


def check_events(expected: list, events: list) -> None:
    if len(expected) != len(events):
        raise Fail("session events: expected %d, got %d: %s" % (len(expected), len(events), short(events)))
    for i, (spec, ev) in enumerate(zip(expected, events)):
        if list(spec) != list(ev):
            raise Fail("session events[%d]: expected %s, got %s" % (i, list(spec)[0], short(ev)))
        key = list(spec)[0]
        if isinstance(spec[key], dict):
            match(spec[key], ev[key], "session events[%d].%s" % (i, key))
        elif not same(spec[key], ev[key]):
            raise Fail("session events[%d]: expected %s, got %s" % (i, short(spec), short(ev)))


def run_behaviour(case: dict, mock: Mock) -> None:
    session = uuid.uuid4().hex
    extra = {"x-mock-session": session}
    if case.get("mock"):
        extra["x-mock-scenario"] = "behaviour/" + case["id"]
    exp = case["expect"]
    scheduled = []
    events = []
    started = time.monotonic()
    try:
        # An argument error may come from the constructor (a bad base_url, timeout or
        # max_retries) or from the call; both count, as long as nothing was sent.
        client = make_client(case, mock.url, extra)
        if (case.get("client") or {}).get("clock") == "mock":
            # The SDK's injectable clock reads the mock's clock for this session, so a
            # case that moves the mock's time moves the SDK's with it.
            client._clock = lambda: mock.clock_now(session)
        if "scheduled_waits_ms" in exp:
            # Replace the client's wait between attempts with a recorder that returns at
            # once, so a capped 60 s wait is checked without waiting 60 s.
            client._sleep = lambda seconds: scheduled.append(seconds * 1000)
        if "steps" in case:
            run_steps(case, client, mock, session, events)
            result = None
        else:
            result = invoke(client, case["call"])
    except (dn.DecisionNodeError, ValueError) as err:
        result = err
    elapsed = (time.monotonic() - started) * 1000
    time.sleep(0.05)  # let the mock finish recording dropped connections
    info = mock.requests(session)
    log = info["requests"]
    if "one_of" in exp:
        # The rule allows more than one way to comply; one of them must hold whole.
        reasons = []
        for i, alt in enumerate(exp["one_of"]):
            try:
                if len(log) != alt["attempts"]:
                    raise Fail("attempts: expected %d, the mock received %d" % (alt["attempts"], len(log)))
                check_outcome(alt["outcome"], result, "outcome")
                break
            except Fail as exc:
                reasons.append("one_of[%d]: %s" % (i, exc))
        else:
            raise Fail("; ".join(reasons))
    elif "attempts" in exp and len(log) != exp["attempts"]:
        raise Fail("attempts: expected %d, the mock received %d: %s" % (exp["attempts"], len(log), [r["method"] + " " + r["path"] for r in log]))
    if "same_header" in exp:
        # Suite 1.13: every attempt of a call carries the same value,
        # an Idempotency-Key kept across retries.
        name = exp["same_header"]["name"].lower()
        spec = exp["same_header"]
        hits = [r for r in log if (spec.get("path_suffix") is None or r["path"].endswith(spec["path_suffix"])) and (spec.get("method") is None or r["method"] == spec["method"])]
        values = [r["headers"].get(name) for r in hits]
        if len(hits) < exp["same_header"].get("min", 2) or None in values or len(set(values)) != 1:
            raise Fail("same_header %s: expected one value on every attempt, got %s" % (name, values))
        pattern = exp["same_header"].get("pattern")
        if pattern and not re.search(pattern, values[0]):
            raise Fail("same_header %s: %r does not match %s" % (name, values[0], pattern))
    if "request_sequence" in exp:
        check_sequence(exp["request_sequence"], log)
    if "request_counts" in exp:
        check_counts(exp["request_counts"], log)
    if "session_events" in exp:
        check_events(exp["session_events"], events)
    if "pongs" in exp:
        in_range(exp["pongs"], info["pongs"], "pongs")
    if "outcome" in exp:
        check_outcome(exp["outcome"], result, "outcome")
    if "results" in exp:
        if not isinstance(result, list) or len(result) != len(exp["results"]):
            raise Fail("results: expected %d results, got %r" % (len(exp["results"]), result))
        for i, (spec, res) in enumerate(zip(exp["results"], result)):
            check_outcome(spec, res, "results[%d]" % i)
    if "gaps_ms" in exp:
        times = [r["time"] for r in log]
        gaps = [(b - a) * 1000 for a, b in zip(times, times[1:])]
        for i, r in enumerate(exp["gaps_ms"]):
            in_range(r, gaps[i], "gap %d (ms)" % (i + 1))
    if "elapsed_ms" in exp:
        in_range(exp["elapsed_ms"], elapsed, "elapsed (ms)")
    if "max_in_flight" in exp:
        in_range(exp["max_in_flight"], info["max_in_flight"], "max in flight")
    if "scheduled_waits_ms" in exp:
        if len(scheduled) != len(exp["scheduled_waits_ms"]):
            raise Fail("scheduled waits: expected %d, the client scheduled %d" % (len(exp["scheduled_waits_ms"]), len(scheduled)))
        for i, r in enumerate(exp["scheduled_waits_ms"]):
            in_range(r, scheduled[i], "scheduled wait %d (ms)" % (i + 1))
    if "steps" in case and isinstance(result, BaseException):
        raise Fail("the steps raised %s: %s" % (type(result).__name__, result))
    for path in exp.get("unrequested_paths", []):
        hits = mock.requests("*", path)["requests"]
        if hits:
            raise Fail("%s was requested %d time(s); it must not be requested at all" % (path, len(hits)))


def run_constant(case: dict, mock: Mock) -> None:
    name = case["constant"]
    if not hasattr(dn, name):
        raise Fail("the client has no constant %s" % name)
    if not same(case["equals"], getattr(dn, name)):
        raise Fail("%s: expected %s, got %s" % (name, short(case["equals"]), short(getattr(dn, name))))


RUNNERS = {"requests": run_request, "responses": run_response, "errors": run_error, "behaviour": run_behaviour, "constants": run_constant}


# --------------------------------------------------------------------------- main


def load_cases(cases_dir: str) -> dict:
    out = {}
    for kind in KINDS:
        folder = os.path.join(cases_dir, kind)
        items = []
        for fname in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if fname.endswith(".json"):
                with open(os.path.join(folder, fname), "r", encoding="utf-8") as fh:
                    items.append((fname, json.load(fh)))
        out[kind] = items
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the conformance cases against the mock with the reference client.")
    parser.add_argument("--mock-url", help="a mock that is already running; default: start one on a free port")
    parser.add_argument("--cases", default=os.path.join(ROOT, "cases"))
    parser.add_argument("--only", choices=KINDS, action="append", help="run only these kinds")
    parser.add_argument("--case", action="append", help="run only cases with these ids")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    validator = Validator(os.path.join(ROOT, "schema"))
    schema_failures = 0
    for kind, items in cases.items():
        for fname, case in items:
            errors = validator.validate(case, SCHEMAS[kind])
            if case.get("id") != fname[:-5]:
                errors.append("id %r does not match the file name" % case.get("id"))
            if errors:
                schema_failures += 1
                print("SCHEMA FAIL %s/%s" % (kind, fname))
                for e in errors[:5]:
                    print("    " + e)

    proc = None
    if args.mock_url:
        url = args.mock_url
    else:
        proc, url = start_mock(args.cases)
    mock = Mock(url)
    saved_env = {k: os.environ.get(k) for k in ENV_VARS}
    totals = {}
    failed = []
    try:
        for kind in KINDS:
            if args.only and kind not in args.only:
                continue
            passed = 0
            count = 0
            for fname, case in cases[kind]:
                if args.case and case["id"] not in args.case:
                    continue
                count += 1
                t0 = time.monotonic()
                try:
                    RUNNERS[kind](case, mock)
                    passed += 1
                    if args.verbose:
                        print("ok   %s/%s (%.0f ms)" % (kind, case["id"], (time.monotonic() - t0) * 1000))
                except Fail as exc:
                    failed.append("%s/%s" % (kind, case["id"]))
                    print("FAIL %s/%s: %s" % (kind, case["id"], exc))
                except Exception:  # noqa: BLE001 - report and continue
                    failed.append("%s/%s" % (kind, case["id"]))
                    print("ERROR %s/%s" % (kind, case["id"]))
                    traceback.print_exc()
            totals[kind] = (passed, count)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)

    all_cases = sum(len(v) for v in cases.values())
    print("schema: %d of %d case files valid" % (all_cases - schema_failures, all_cases))
    for kind, (p, c) in totals.items():
        print("%-10s %d/%d passed" % (kind, p, c))
    ok = not failed and schema_failures == 0
    print("PASS" if ok else "FAIL (%d)" % (len(failed) + schema_failures))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
