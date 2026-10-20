"""Unit tests of the mock server. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import base64
import email.utils
import http.client
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mock"))

import server  # noqa: E402

KEY = "dn_test_unit"
AUTH = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}


def start(**opts):
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    httpd.state = server.State()
    httpd.verbose = False
    httpd.safety = opts.get("safety", True)
    httpd.api_key = opts.get("api_key")
    server.load_cases(httpd.state, os.path.join(ROOT, "cases"))
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    return httpd


class Base(unittest.TestCase):
    safety = True

    @classmethod
    def setUpClass(cls):
        cls.httpd = start(safety=cls.safety)
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=AUTH if headers is None else headers)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, (json.loads(text) if text else None)

    def decide(self, body, headers=None):
        return self.call("POST", "/v1/decide", body, headers)


TICKET = {
    "state": {"ticket": "I was charged twice."},
    "questions": {
        "route": {"type": "choice", "instructions": "Where?", "criteria": {"billing": "money", "bug": "broken", "account": "login"}},
        "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["routine", "today", "urgent", "critical"]},
        "escalate": {"type": "truth", "instructions": "Escalate?"},
    },
}


class TestAnswers(Base):
    def test_shape_and_headers(self):
        status, headers, body = self.decide(TICKET)
        self.assertEqual(status, 200)
        self.assertTrue(body["model"].startswith("decisionnode-moe-"), body["model"])
        self.assertEqual(list(body["answers"]), ["route", "urgency", "escalate"])
        route = body["answers"]["route"]
        self.assertEqual(list(route), ["type", "choice", "confidence", "probabilities"])
        self.assertEqual(list(route["probabilities"]), ["account", "billing", "bug"])
        self.assertIn(route["choice"], ["billing", "bug", "account"])
        urgency = body["answers"]["urgency"]
        self.assertEqual(list(urgency), ["type", "score", "confidence", "legend", "probabilities"])
        self.assertEqual(list(urgency["probabilities"]), ["0", "1", "2", "3"])
        self.assertEqual(urgency["legend"], {"0": "routine", "1": "today", "2": "urgent", "3": "critical"})
        self.assertEqual(body["answers"]["escalate"]["type"], "truth")
        self.assertIsInstance(body["answers"]["escalate"]["truth"], float)
        self.assertEqual(body["usage"]["output_tokens"], 0)
        self.assertIsInstance(body["usage"]["input_tokens"], int)
        for h in ("x-request-id", "x-decisionnode-model", "x-decisionnode-latency-ms", "x-decisionnode-safety"):
            self.assertIn(h, headers)
        self.assertEqual(headers["x-decisionnode-model"], body["model"])
        self.assertRegex(headers["x-decisionnode-safety"], r"^harm=0\.\d{3}, self_harm=0\.\d{3}$")
        self.assertNotIn("safety", body)

    def test_deterministic(self):
        a = self.decide(TICKET)[2]
        b = self.decide(TICKET)[2]
        self.assertEqual(a, b)

    def test_questions_independent(self):
        full = self.decide(TICKET)[2]["answers"]
        for qid, q in TICKET["questions"].items():
            alone = self.decide({"state": TICKET["state"], "questions": {qid: q}})[2]["answers"][qid]
            self.assertEqual(alone, full[qid])

    def test_key_order_inside_question_irrelevant(self):
        q1 = {"type": "truth", "instructions": "x", "criteria": {"true": "a", "false": "b"}}
        q2 = {"criteria": {"true": "a", "false": "b"}, "instructions": "x", "type": "truth"}
        a = self.decide({"state": "s", "questions": {"q": q1}})[2]
        b = self.decide({"state": "s", "questions": {"q": q2}})[2]
        self.assertEqual(a, b)

    def test_model_aliases(self):
        # The family ids, the pinned ids and the older ids, each answered with the pinned id.
        for name, family in [
            ("decisionnode-moe", "decisionnode-moe-"),
            (server.MOE, "decisionnode-moe-"),
            ("decisionnode-latest", "decisionnode-moe-"),
            ("decisionnode-1.0", "decisionnode-moe-"),
            ("decisionnode-flash", "decisionnode-flash-"),
            (server.FLASH, "decisionnode-flash-"),
            ("decisionnode-flash-latest", "decisionnode-flash-"),
            ("decisionnode-1.0-flash", "decisionnode-flash-"),
        ]:
            status, headers, body = self.decide({"model": name, "state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}})
            self.assertEqual(status, 200)
            self.assertTrue(body["model"].startswith(family) and body["model"] not in ("decisionnode-moe", "decisionnode-flash"), (name, body["model"]))
            self.assertEqual(headers["x-decisionnode-model"], body["model"])
        self.assertEqual(self.decide({"model": None, "state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}})[2]["model"], server.MOE)

    def test_state_forms_accepted(self):
        q = {"q": {"type": "truth", "instructions": "?"}}
        for body in ({"questions": q}, {"state": None, "questions": q}, {"state": "", "questions": q}, {"state": [1, None], "questions": q}, {"state": {}, "questions": q}):
            self.assertEqual(self.decide(body)[0], 200, body)
        self.assertEqual(self.decide({"questions": q})[2], self.decide({"state": "", "questions": q})[2])

    def test_single_option_and_level(self):
        status, headers, body = self.decide({"state": "s", "questions": {
            "c": {"type": "choice", "criteria": {"only": None}},
            "s": {"type": "score", "criteria": ["one"]},
        }})
        self.assertEqual(status, 200)
        self.assertEqual(body["answers"]["c"], {"type": "choice", "choice": "only", "confidence": 1.0, "probabilities": {"only": 1.0}})
        self.assertEqual(body["answers"]["s"], {"type": "score", "score": 0.0, "confidence": 1.0, "legend": {"0": "one"}, "probabilities": {"0": 1.0}})
        self.assertEqual(headers["x-decisionnode-safety"], 'unchecked;reason="no-model-question"')

    def test_score_object_form_and_structured_levels(self):
        levels = {"0": {"what": "low"}, "1": ["mid"], "2": "high"}
        status, _, body = self.decide({"state": "s", "questions": {"s": {"type": "score", "criteria": levels}}})
        self.assertEqual(status, 200)
        self.assertEqual(body["answers"]["s"]["legend"], levels)

    def test_option_names_verbatim(self):
        names = {"": None, " a ": None, "$1,315.50": None, "01": None, "Ünï": None}
        body = self.decide({"state": "s", "questions": {"c": {"type": "choice", "criteria": names}}})[2]
        self.assertEqual(set(body["answers"]["c"]["probabilities"]), set(names))
        self.assertIn(body["answers"]["c"]["choice"], names)

    def test_safety_body_on_request(self):
        for flag in ("true", "1", "YES"):
            body = self.decide(TICKET, dict(AUTH, **{"x-decisionnode-safety-body": flag}))[2]
            self.assertEqual(set(body["safety"]), {"checked", "harm", "self_harm", "flagged"})
            self.assertTrue(body["safety"]["checked"])

    def test_unknown_top_level_field_ignored(self):
        self.assertEqual(self.decide({"state": "s", "temperature": 0.2, "questions": {"q": {"type": "truth", "instructions": "?", "extra": 1}}})[0], 200)

    def test_images(self):
        with open(os.path.join(ROOT, "fixtures", "images", "pixel.png"), "rb") as fh:
            png = base64.b64encode(fh.read()).decode()
        ok = {"state": "", "images": [{"id": "a", "media_type": "image/png", "data": png}, {"id": "b", "url": "https://example.com/x.jpg"}], "questions": {"q": {"type": "truth", "instructions": "?"}}}
        status, _, body = self.decide(ok)
        self.assertEqual(status, 200)
        self.assertGreater(body["usage"]["input_tokens"], 2 * server.IMAGE_TOKENS)
        bad = [
            [{"id": "a", "media_type": "image/jpeg", "data": png}],
            [{"id": "a", "media_type": "image/png", "data": "not base64!"}],
            [{"id": "a", "url": "http://example.com/x.jpg"}],
            [{"id": "a"}],
            [{"id": "a", "url": "https://e.com/1"}, {"id": "a", "url": "https://e.com/2"}],
        ]
        for images in bad:
            status, headers, body = self.decide(dict(ok, images=images))
            self.assertEqual(status, 400, images)
            self.assertEqual(body["detail"]["error_type"], "api_usage_error")
            self.assertIn("x-decisionnode-error", headers)
        self.assertEqual(self.decide(dict(ok, images=None))[0], 200)
        self.assertEqual(self.decide(dict(ok, images=[]))[0], 200)
        sixteen = [{"id": str(i), "media_type": "image/png", "data": png} for i in range(16)]
        self.assertEqual(self.decide(dict(ok, images=sixteen))[0], 200)
        seventeen = sixteen + [{"id": "x", "url": "https://e.com/x"}]
        status, headers, body = self.decide(dict(ok, images=seventeen))
        self.assertEqual(status, 422)
        self.assertEqual(
            body["detail"],
            [{"type": "too_long", "loc": ["body", "images"], "msg": "List should have at most 16 items after validation, not 17", "input": seventeen, "ctx": {"field_type": "List", "max_length": 16, "actual_length": 17}}],
        )

    def test_points_and_boxes(self):
        belt = {"id": "belt", "url": "https://example.com/belt.jpg"}
        dock = {"id": "dock", "url": "https://example.com/dock.jpg"}
        q = {"pick": {"type": "point", "instructions": "?"}, "damage": {"type": "box", "instructions": "?", "units": "normalized"}}
        status, _, body = self.decide({"state": "", "images": [belt], "questions": q})
        self.assertEqual(status, 200, body)
        pick, damage = body["answers"]["pick"], body["answers"]["damage"]
        self.assertEqual(sorted(pick), sorted(["type", "x", "y", "present", "confidence", "image", "width", "height"]))
        self.assertTrue(isinstance(pick["x"], int) and isinstance(pick["y"], int))
        self.assertEqual((pick["image"], pick["width"], pick["height"]), ("belt", 1920, 1080))
        self.assertTrue(0 <= damage["x1"] <= damage["x2"] <= 1 and 0 <= damage["y1"] <= damage["y2"] <= 1)
        cases = [
            ({"state": "", "questions": {"pick": {"type": "point", "instructions": "?"}}}, "image_required", ["body", "questions", "pick", "point"]),
            ({"state": "", "images": [belt, dock], "questions": {"pick": {"type": "point", "instructions": "?"}}}, "missing", ["body", "questions", "pick", "point", "image"]),
            ({"state": "", "images": [belt], "questions": {"pick": {"type": "point", "instructions": "?", "image": "x"}}}, "unknown_image", ["body", "questions", "pick", "point", "image"]),
            ({"state": "", "images": [belt], "questions": {"pick": {"type": "point", "instructions": "?", "image": 1}}}, "string_type", ["body", "questions", "pick", "point", "image"]),
            ({"state": "", "images": [belt], "questions": {"pick": {"type": "box", "instructions": "?", "units": "cm"}}}, "literal_error", ["body", "questions", "pick", "box", "units"]),
        ]
        for request, kind, loc in cases:
            status, _, body = self.decide(request)
            self.assertEqual(status, 422, request)
            self.assertEqual([(e["type"], e["loc"]) for e in body["detail"]], [(kind, loc)])
        status, _, body = self.decide({"state": "", "images": [belt], "questions": {"pick": {"type": "point"}}})
        self.assertEqual((status, body["detail"]), (400, "Point question must have instructions: pick"))

    def test_videos(self):
        with open(os.path.join(ROOT, "fixtures", "images", "pixel.jpg"), "rb") as fh:
            jpg = base64.b64encode(fh.read()).decode()

        def frames(*ts):
            return [{"t": t, "media_type": "image/jpeg", "data": jpg} for t in ts]

        q = {"q": {"type": "truth", "instructions": "?"}}
        status, _, body = self.decide({"state": "", "videos": [{"id": "cam", "frames": frames(0, 0.5, 1)}], "questions": q})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["usage"]["video_frames"], body["usage"]["video_seconds"]), (3, 1.5))
        self.assertNotIn("warnings", body)
        # fps caps the rate read: frames 0.25 s apart at the default 2 a second, every other one
        status, _, body = self.decide({"state": "", "videos": [{"id": "cam", "frames": frames(0, 0.25, 0.5, 0.75)}], "questions": q})
        self.assertEqual((status, body["usage"]["video_frames"]), (200, 2))
        status, _, body = self.decide({"state": "", "videos": [{"id": "cam", "fps": 4, "frames": frames(0, 0.25, 0.5, 0.75)}], "questions": q})
        self.assertEqual((status, body["usage"]["video_frames"]), (200, 4))
        # no video: no video usage
        status, _, body = self.decide({"state": "", "questions": q})
        self.assertNotIn("video_frames", body["usage"])
        bad = [
            ([{"id": "", "frames": frames(0)}], "Video 0 needs an id."),
            ([{"id": "cam", "frames": frames(0)}, {"id": "cam", "frames": frames(0)}], "Video id 'cam' is used twice (ids are unique across images and videos)."),
            ([{"id": "cam", "frames": []}], "Video 'cam': frames must be a non-empty list of frames."),
            ([{"id": "cam", "frames": frames(1.5, 1.5)}], "Video 'cam': frame 1: t must be larger than the frame before it (1.5)."),
            ([{"id": "cam", "frames": frames(-1)}], "Video 'cam': frame 0: t must be a number of seconds, at least 0."),
            ([{"id": "cam", "frames": [{"t": 0, "media_type": "image/gif", "data": jpg}]}], "Video 'cam': frame 0: media_type must be one of image/jpeg, image/png, image/webp."),
            ([{"id": "cam", "fps": 5, "frames": frames(0)}], "Video 'cam': fps must be a number above 0 and at most 4."),
            ([{"id": "cam", "end": 0, "frames": frames(0)}], "Video 'cam': end must be after start."),
            ([{"id": "cam", "start": -1, "frames": frames(0)}], "Video 'cam': start must be a number of seconds, at least 0."),
            ([{"id": "cam", "media_type": "video/mp4", "data": "AAAA"}], "Video 'cam': video files and URLs are not taken: send the video as frames with timestamps."),
            ([{"id": "cam", "frames": frames(*[i * 0.5 for i in range(129)])}], "The videos send 129 frames, the limit is 128 a request (over all its videos): send fewer frames, a shorter window or a lower rate."),
            ([{"id": "cam", "frames": [{"t": 0, "media_type": "image/png", "data": jpg}]}], "Video 'cam': frame 0: the data is not image/png."),
            ([{"id": "cam", "start": 2, "end": 4, "frames": frames(1000, 1009.5)}], "Video 'cam': no frame lies in the window [2, 4) s (the frames run from 1000 to 1009.5 s)."),
        ]
        for videos, message in bad:
            status, headers, body = self.decide({"state": "", "videos": videos, "questions": q})
            self.assertEqual((status, body["detail"]["error_type"], body["detail"]["message"]), (400, "api_usage_error", message), videos)
            self.assertIn("x-decisionnode-error", headers)
        five = [{"id": "v%d" % i, "frames": frames(0)} for i in range(5)]
        status, _, body = self.decide({"state": "", "videos": five, "questions": q})
        self.assertEqual(status, 422)
        self.assertEqual([(e["type"], e["loc"], e["ctx"]) for e in body["detail"]], [("too_long", ["body", "videos"], {"field_type": "List", "max_length": 4, "actual_length": 5})])
        status, _, body = self.decide({"state": "", "images": [{"id": "cam", "url": "https://e.com/x"}], "videos": [{"id": "cam", "frames": frames(0)}], "questions": q})
        self.assertEqual((status, body["detail"]["message"]), (400, "Video id 'cam' is used twice (ids are unique across images and videos)."))
        # a frame outside the window is not read, so its bytes are not checked
        status, _, body = self.decide({"state": "", "videos": [{"id": "cam", "start": 1, "frames": [{"t": 0, "media_type": "image/png", "data": jpg}] + frames(1)}], "questions": q})
        self.assertEqual((status, body["usage"]["video_frames"]), (200, 1))


class TestRounding(unittest.TestCase):
    def test_sums_to_one_and_long_floats_appear(self):
        rng = random.Random(7)
        long_seen = False
        for _ in range(3000):
            k = rng.randint(2, 12)
            w = [rng.random() for _ in range(k)]
            t = sum(w)
            raw = [x / t for x in w]
            out = server.round_probabilities(raw)
            self.assertAlmostEqual(sum(out), 1.0, places=9)
            self.assertTrue(all(v >= 0 for v in out))
            if any(len(repr(v)) > 5 for v in out):
                long_seen = True
        self.assertTrue(long_seen)

    def test_confidence_formulas(self):
        stream_q = {"type": "choice", "criteria": {"a": None, "b": None, "c": None}}
        ans = server.answer("seed", stream_q)
        self.assertGreaterEqual(ans["confidence"], 0.0)
        self.assertLessEqual(ans["confidence"], 1.0)
        top = max(ans["probabilities"].values())
        self.assertLessEqual(abs(ans["probabilities"][ans["choice"]] - top), 0.0100001)
        s = server.answer("seed", {"type": "score", "criteria": ["a", "b", "c", "d"]})
        self.assertGreaterEqual(s["score"], 0.0)
        self.assertLessEqual(s["score"], 3.0)


class TestValidation(Base):
    def assertRefused(self, body, status, headers=None):
        st, hd, out = self.decide(body, headers)
        self.assertEqual(st, status, out)
        self.assertIn("x-request-id", hd)
        if status != 401:
            self.assertIn("x-decisionnode-error", hd)
            self.assertRegex(hd["x-decisionnode-error"], r"^[ -~]+$")
        self.assertNotIn("x-decisionnode-safety", hd)
        return out

    def test_limits(self):
        many = {"o%d" % i: None for i in range(255)}
        self.assertEqual(self.decide({"state": "", "questions": {"q": {"type": "choice", "criteria": many}}})[0], 200)
        many["extra"] = None
        out = self.assertRefused({"state": "", "questions": {"q": {"type": "choice", "criteria": many}}}, 400)
        self.assertEqual(out, {"detail": "Too many choices. Must have at most 255 choices."})
        ten = ["l%d" % i for i in range(10)]
        self.assertEqual(self.decide({"state": "", "questions": {"q": {"type": "score", "criteria": ten}}})[0], 200)
        out = self.assertRefused({"state": "", "questions": {"q": {"type": "score", "criteria": ten + ["x"]}}}, 400)
        self.assertEqual(out, {"detail": "Too many score levels. Must have at most 10 levels."})
        out = self.assertRefused({"state": "", "questions": {"q": {"type": "choice", "criteria": {}}}}, 400)
        self.assertEqual(out, {"detail": "Choice question must have at least one choice: q"})

    def test_truth_needs_text(self):
        out = self.assertRefused({"state": "", "questions": {"q": {"type": "truth"}}}, 400)
        self.assertEqual(out, {"detail": "Truth question must have criteria or instructions: q"})
        self.assertRefused({"state": "", "questions": {"q": {"type": "truth", "criteria": {}}}}, 400)
        self.assertRefused({"state": "", "questions": {"q": {"type": "truth", "criteria": None, "instructions": None}}}, 400)
        self.assertEqual(self.decide({"state": "", "questions": {"q": {"type": "truth", "criteria": {"false": "no"}}}})[0], 200)

    def test_unknown_type_and_model(self):
        out = self.assertRefused({"state": "", "questions": {"q": {"type": "rank", "instructions": "?"}}}, 400)
        self.assertEqual(out, {"detail": {"error_type": "api_usage_error", "message": "Invalid request."}})
        out = self.assertRefused({"model": "decisionnode-9", "state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}}, 400)
        self.assertEqual(out, {"detail": {"error_type": "api_usage_error", "message": "Unknown model: decisionnode-9"}})

    def test_422s(self):
        cases = [
            ({"state": 5, "questions": {"q": {"type": "truth", "instructions": "?"}}}, 3),
            ({"state": True, "questions": {"q": {"type": "truth", "instructions": "?"}}}, 3),
            ({"state": ""}, 1),
            ({"state": "", "questions": []}, 1),
            ({"state": "", "questions": {}}, 1),
            ({"state": "", "questions": {"q": "text"}}, 1),
            ({"state": "", "questions": {"q": {"instructions": "?"}}}, 1),
            ({"state": "", "questions": {"q": {"type": "choice"}}}, 1),
            ({"state": "", "questions": {"q": {"type": "choice", "criteria": ["a"]}}}, 1),
            ({"state": "", "questions": {"q": {"type": "score"}}}, 1),
            ({"state": "", "questions": {"q": {"type": "score", "criteria": []}}}, 1),
            ({"state": "", "questions": {"q": {"type": "score", "criteria": ["a", None]}}}, 3),
            ({"state": "", "questions": {"q": {"type": "score", "criteria": {"1": "a"}}}}, 1),
            ({"state": "", "questions": {"q": {"type": "truth", "instructions": 7}}}, 3),
            ({"state": "", "questions": {"q": {"type": "truth", "instructions": "?", "criteria": "x"}}}, 1),
            ({"model": 3, "state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}}, 1),
            ({"state": "", "images": "x", "questions": {"q": {"type": "truth", "instructions": "?"}}}, 1),
            ([1, 2], 1),
        ]
        for body, n in cases:
            out = self.assertRefused(body, 422)
            self.assertEqual(len(out["detail"]), n, (body, out))
            for e in out["detail"]:
                self.assertEqual(set(e), {"type", "loc", "msg", "input"})
                self.assertEqual(e["loc"][0], "body")

    def test_422_lists_every_field(self):
        out = self.assertRefused({"state": 1, "questions": {"a": {"type": "choice"}, "b": {"type": "score"}}}, 422)
        self.assertEqual(len(out["detail"]), 5)

    def test_invalid_json(self):
        st, hd, out = self.call("POST", "/v1/decide", headers=AUTH, raw=b'{"state": ')
        self.assertEqual(st, 422)
        self.assertEqual(out["detail"][0]["type"], "json_invalid")
        self.assertEqual(out["detail"][0]["ctx"], {"error": "Expecting value"})

    def test_token_limit(self):
        big = "word " * 9000
        body = {"model": "decisionnode-flash", "state": big, "questions": {"q": {"type": "truth", "instructions": "?"}}}
        out = self.assertRefused(body, 400)
        self.assertEqual(out, {"detail": {"error_type": "max_tokens_exceeded"}})
        self.assertEqual(self.decide(dict(body, model="decisionnode-moe"))[0], 200)

    def test_auth(self):
        q = {"state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}}
        for headers in ({"Content-Type": "application/json"}, {"Authorization": "Bearer "}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer invalid_key"}):
            st, hd, out = self.decide(q, headers)
            self.assertEqual(st, 401)
            self.assertEqual(out["detail"]["error_type"], "authentication_error")
            self.assertIn("x-request-id", hd)

    def test_404_405_models(self):
        st, _, out = self.call("GET", "/v1/nope")
        self.assertEqual((st, out["detail"]["error_type"]), (404, "not_found_error"))
        st, hd, _ = self.call("GET", "/v1/decide")
        self.assertEqual((st, hd.get("allow")), (405, "POST"))
        st, hd, out = self.call("GET", "/v1/models")
        self.assertEqual(st, 200)
        names = [m["name"] for m in out["models"]]
        # Each family id, then its pinned ids served; the older ids are accepted, never listed.
        self.assertEqual(names, ["decisionnode-moe", server.MOE, "decisionnode-flash", server.FLASH])
        family = {m["name"]: m for m in out["models"]}
        self.assertEqual(family["decisionnode-moe"]["current_version"], server.MOE)
        self.assertNotIn("current_version", family[server.MOE])
        for m in out["models"]:
            self.assertRegex(m["release_date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertIn("max_input_tokens", m)


class TestNoSafety(Base):
    safety = False

    def test_no_header(self):
        st, hd, body = self.decide(TICKET, dict(AUTH, **{"x-decisionnode-safety-body": "true"}))
        self.assertEqual(st, 200)
        self.assertNotIn("x-decisionnode-safety", hd)
        self.assertNotIn("safety", body)


class TestScenarios(Base):
    def register(self, scenario):
        st, _, out = self.call("POST", "/_mock/scenarios", scenario, headers={"Content-Type": "application/json"})
        self.assertEqual(st, 200, out)

    def h(self, session, scenario=None):
        out = dict(AUTH, **{"x-mock-session": session})
        if scenario:
            out["x-mock-scenario"] = scenario
        return out

    def test_steps_then_normal(self):
        self.register({"name": "t-steps", "steps": [{"status": 529, "headers": {"retry-after-ms": "5"}, "body": {"detail": {"error_type": "overloaded_error"}}}, {"status": 429, "body_text": "{\"x\": 0.8200000000000001}"}]})
        body = {"state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}}
        st1, hd1, _ = self.decide(body, self.h("s1", "t-steps"))
        st2, _, _ = self.call("POST", "/v1/decide", body, self.h("s1", "t-steps"))
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/v1/decide", body=json.dumps(body), headers=self.h("s1", "t-steps"))
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        r.read()
        conn.close()
        self.assertEqual((st1, st2), (529, 429))
        self.assertEqual(hd1["retry-after-ms"], "5")
        self.assertIn("x-request-id", hd1)
        # another session starts from the first step
        self.assertEqual(self.decide(body, self.h("s2", "t-steps"))[0], 529)
        log = self.call("GET", "/_mock/requests?session=s1")[2]
        self.assertEqual([r["status"] for r in log["requests"]], [529, 429, 200])
        self.assertEqual([r["attempt"] for r in log["requests"]], [1, 2, 3])
        self.assertEqual(log["requests"][0]["body"], body)

    def test_body_text_is_byte_exact(self):
        self.register({"name": "t-text", "steps": [{"status": 200, "body_text": "{\"p\":0.8200000000000001,\"w\":1.0}"}]})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/v1/decide", body=b"{}", headers=self.h("t", "t-text"))
        self.assertEqual(conn.getresponse().read(), b"{\"p\":0.8200000000000001,\"w\":1.0}")
        conn.close()

    def test_repeat_last_and_reset(self):
        self.register({"name": "t-rep", "steps": [{"status": 503, "body": {}}], "after": "repeat_last"})
        for _ in range(3):
            self.assertEqual(self.decide({}, self.h("r", "t-rep"))[0], 503)
        self.call("POST", "/_mock/reset?session=r", headers={})
        self.assertEqual(self.call("GET", "/_mock/requests?session=r")[2]["requests"], [])

    def test_rules(self):
        self.register({"name": "t-rules", "rules": [{"match": {"/state": "bad"}, "steps": [{"status": 422, "body": {"detail": []}}]}]})
        q = {"q": {"type": "truth", "instructions": "?"}}
        self.assertEqual(self.decide({"state": "bad", "questions": q}, self.h("u", "t-rules"))[0], 422)
        self.assertEqual(self.decide({"state": "good", "questions": q}, self.h("u", "t-rules"))[0], 200)
        self.assertEqual(self.decide({"state": "bad", "questions": q}, self.h("u", "t-rules"))[0], 200)

    def test_close(self):
        self.register({"name": "t-close", "steps": [{"action": "close"}]})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/v1/decide", body=b"{}", headers=self.h("c", "t-close"))
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionError, http.client.BadStatusLine)):
            conn.getresponse()
        conn.close()

    def test_http_date_header(self):
        self.register({"name": "t-date", "steps": [{"status": 429, "headers": {"retry-after": "{http_date+30}"}, "body": {}}]})
        before = time.time()
        _, hd, _ = self.decide({}, self.h("d", "t-date"))
        when = email.utils.parsedate_to_datetime(hd["retry-after"]).timestamp()
        self.assertRegex(hd["retry-after"], r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$")
        self.assertGreaterEqual(when, int(before) + 29)
        self.assertLessEqual(when, time.time() + 30)

    def test_requests_across_sessions_by_path(self):
        self.call("POST", "/v1/elsewhere-a", {}, self.h("p1"))
        self.call("POST", "/v1/elsewhere-a", {}, self.h("p2"))
        self.call("POST", "/v1/elsewhere-b", {}, self.h("p2"))
        every = self.call("GET", "/_mock/requests?session=*&path=/v1/elsewhere-a")[2]["requests"]
        self.assertEqual(len(every), 2)
        one = self.call("GET", "/_mock/requests?session=p2&path=/v1/elsewhere-b")[2]["requests"]
        self.assertEqual([r["path"] for r in one], ["/v1/elsewhere-b"])
        none = self.call("GET", "/_mock/requests?session=*&path=/v1/never")[2]["requests"]
        self.assertEqual(none, [])

    def test_trickle_sends_headers_first_then_the_body_slowly(self):
        self.register({"name": "t-trickle", "steps": [{"action": "normal", "trickle": {"chunks": 4, "interval_ms": 150}}]})
        body = json.dumps({"state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        started = time.monotonic()
        conn.request("POST", "/v1/decide", body=body, headers=self.h("tr", "t-trickle"))
        resp = conn.getresponse()
        headers_at = time.monotonic() - started
        text = resp.read()
        body_at = time.monotonic() - started
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertLess(headers_at, 0.3)
        self.assertGreaterEqual(body_at, 0.55)
        self.assertIn("q", json.loads(text)["answers"])

    def test_unknown_scenario(self):
        st, _, out = self.decide({}, self.h("x", "no-such-scenario"))
        self.assertEqual(st, 400)
        self.assertEqual(out["detail"]["error_type"], "mock_error")

    def test_cases_loaded(self):
        names = self.call("GET", "/_mock/scenarios")[2]["scenarios"]
        self.assertIn("response/probabilities-long-floats", names)
        self.assertIn("error/403-refusal-harm", names)
        self.assertIn("behaviour/retry-529-honours-retry-after-ms", names)

    def test_chunked_body(self):
        body = json.dumps({"state": "", "questions": {"q": {"type": "truth", "instructions": "?"}}}).encode()
        sock = socket.create_connection(("127.0.0.1", self.port))
        head = "POST /v1/decide HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer k\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        mid = len(body) // 2
        chunks = b"%x\r\n%s\r\n%x\r\n%s\r\n0\r\n\r\n" % (mid, body[:mid], len(body) - mid, body[mid:])
        sock.sendall(head.encode() + chunks)
        data = b""
        while True:
            part = sock.recv(65536)
            if not part:
                break
            data += part
        sock.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:80])


class TestCommandLine(unittest.TestCase):
    def test_port_zero_prints_first_line(self):
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "mock", "server.py"), "--port", "0"], stdout=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline().strip()
            self.assertRegex(line, r"^listening on http://127\.0\.0\.1:\d+$")
            port = int(line.rsplit(":", 1)[1])
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/_mock/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
            conn.close()
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            proc.stdout.close()


# =========================================================================== suite 1.4

sys.path.insert(0, os.path.join(ROOT, "runner"))
import hashlib  # noqa: E402
import struct  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import reference_client  # noqa: E402

PNG = open(os.path.join(ROOT, "fixtures", "images", "pixel.png"), "rb").read()
JPG = open(os.path.join(ROOT, "fixtures", "images", "pixel.jpg"), "rb").read()


def b64(data):
    return base64.b64encode(data).decode()


def file_of(data, ctype="image/png", sha=True):
    f = {"content_type": ctype, "byte_size": len(data), "checksum": b64(hashlib.md5(data).digest())}
    if sha:
        f["sha256"] = b64(hashlib.sha256(data).digest())
    return f


class Spec11(Base):
    def h(self, session, scenario=None, key=KEY):
        out = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "x-mock-session": session}
        if scenario:
            out["x-mock-scenario"] = scenario
        return out

    def raw_call(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, text

    def presign(self, session, files, key=KEY):
        st, hd, out = self.call("POST", "/v1/uploads", {"files": files}, self.h(session, key=key))
        self.assertEqual(st, 201, out)
        return out["uploads"]

    def put(self, up, data, headers=None):
        url = urlsplit(up["url"])
        return self.raw_call("PUT", url.path + "?" + url.query, data, up["headers"] if headers is None else headers)

    def upload(self, session, data=PNG, ctype="image/png", key=KEY):
        up = self.presign(session, [file_of(data, ctype)], key)[0]
        self.assertEqual(self.put(up, data)[0], 200)
        return up

    def control(self, path, session, body):
        return self.call("POST", "%s?session=%s" % (path, session), body, {"Content-Type": "application/json"})


class TestUploads(Spec11):
    def test_presign_shape(self):
        st, hd, out = self.call("POST", "/v1/uploads", {"files": [file_of(PNG), file_of(JPG, "image/jpeg", sha=False)]}, self.h("u1"))
        self.assertEqual(st, 201)
        self.assertIn("x-request-id", hd)
        self.assertEqual(out["expires_in"], 300)
        a, b = out["uploads"]
        self.assertRegex(a["upload_id"], r"^upl_[0-9a-f]{32}$")
        self.assertEqual(a["method"], "PUT")
        self.assertEqual(a["headers"], {"Content-Type": "image/png", "Content-MD5": file_of(PNG)["checksum"], "If-None-Match": "*", "x-amz-checksum-sha256": file_of(PNG)["sha256"]})
        self.assertNotIn("x-amz-checksum-sha256", b["headers"])
        self.assertIn("X-Amz-Expires=300", a["url"])
        self.assertIn("X-Amz-Signature=", a["url"])
        self.assertTrue(a["url"].startswith("http://127.0.0.1:%d/_mock/s3/uploads/default/" % self.port))
        self.assertRegex(a["expires_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_presign_sixteen_files(self):
        files = [dict(file_of(PNG + b"\0" * i)) for i in range(16)]
        st, _, out = self.call("POST", "/v1/uploads", {"files": files}, self.h("u16"))
        self.assertEqual(st, 201, out)
        self.assertEqual(len(out["uploads"]), 16)

    def test_presign_refusals(self):
        good = file_of(PNG)
        bad = [
            {"files": []},
            {"files": [good] * 17},
            {"files": [dict(good, content_type="image/gif")]},
            {"files": [dict(good, byte_size=0)]},
            {"files": [dict(good, byte_size=10485761)]},
            {"files": [dict(good, byte_size=True)]},
            {"files": [dict(good, checksum="abc")]},
            {"files": [dict(good, sha256="abc")]},
            {"nope": 1},
            [1],
        ]
        for body in bad:
            st, hd, out = self.call("POST", "/v1/uploads", body, self.h("u2"))
            self.assertEqual(st, 400, body)
            self.assertEqual(out["detail"]["error_type"], "api_usage_error")
            self.assertIn("x-decisionnode-error", hd)
        st, _, out = self.call("POST", "/v1/uploads", None, self.h("u2"), raw=b"x" * 70000)
        self.assertEqual((st, out["detail"]["error_type"]), (413, "request_too_large_error"))
        st, hd, _ = self.call("GET", "/v1/uploads")
        self.assertEqual((st, hd.get("allow")), (405, "POST"))
        st, _, _ = self.call("POST", "/v1/uploads", {"files": [good]}, {"Content-Type": "application/json"})
        self.assertEqual(st, 401)

    def test_put_checks_signed_headers_and_bytes(self):
        up = self.presign("u3", [file_of(PNG)])[0]
        hdrs = dict(up["headers"])
        for change in ({"Content-Type": "image/jpeg"}, {"Content-MD5": file_of(JPG)["checksum"]}, {"If-None-Match": "x"}, {"x-amz-checksum-sha256": file_of(JPG)["sha256"]}):
            st, hd, text = self.put(up, PNG, dict(hdrs, **change))
            self.assertEqual(st, 403, change)
            self.assertIn(b"<Code>SignatureDoesNotMatch</Code>", text)
            self.assertEqual(hd["content-type"], "application/xml")
        st, _, text = self.put(up, PNG + b"x")
        self.assertEqual(st, 403)
        forged = bytearray(PNG)
        forged[-1] ^= 1
        st, _, text = self.put(up, bytes(forged))
        self.assertEqual(st, 400)
        self.assertIn(b"<Code>BadDigest</Code>", text)
        url = urlsplit(up["url"])
        st, _, text = self.raw_call("PUT", url.path + "?X-Amz-Signature=wrong", PNG, hdrs)
        self.assertEqual(st, 403)
        st, hd, _ = self.put(up, PNG)
        self.assertEqual(st, 200)
        self.assertIn("etag", hd)
        st, _, text = self.put(up, PNG)
        self.assertEqual(st, 412)
        self.assertIn(b"PreconditionFailed", text)
        st, _, _ = self.raw_call("GET", url.path + "?" + url.query)
        self.assertEqual(st, 405)
        log = self.call("GET", "/_mock/requests?session=u3")[2]["requests"]
        # the PUT with a forged signature names no upload, so it is recorded under the default session
        self.assertEqual([r["method"] for r in log], ["POST"] + ["PUT"] * 8 + ["GET"])
        self.assertEqual(log[-2]["body_base64"], b64(PNG))
        ups = self.call("GET", "/_mock/uploads?session=u3")[2]["uploads"]
        self.assertEqual((ups[0]["stored"], ups[0]["authorization_seen"]), (True, False))

    def test_put_records_authorization(self):
        up = self.presign("u4", [file_of(PNG)])[0]
        self.put(up, PNG, dict(up["headers"], Authorization="Bearer leaked"))
        self.assertTrue(self.call("GET", "/_mock/uploads?session=u4")[2]["uploads"][0]["authorization_seen"])

    def test_url_expires_after_five_minutes(self):
        up = self.presign("u5", [file_of(PNG)])[0]
        self.control("/_mock/clock", "u5", {"advance_seconds": 301})
        st, _, text = self.put(up, PNG)
        self.assertEqual(st, 403)
        self.assertIn(b"AccessDenied", text)

    def decide_with(self, session, image, key=KEY, path="/v1/decide", body_extra=None):
        body = {"model": "decisionnode-latest", "state": "s", "images": [image], "questions": {"q": {"type": "truth" if path == "/v1/decide" else "noul", "instructions": "?"}}}
        body.update(body_extra or {})
        return self.call("POST", path, body, self.h(session, key=key))

    def test_reference_answers_like_data(self):
        up = self.upload("u6")
        a = self.decide_with("u6", {"id": "r", "upload_id": up["upload_id"]})
        b = self.decide_with("u6", {"id": "r", "media_type": "image/png", "data": b64(PNG)})
        self.assertEqual(a[0], 200)
        self.assertEqual(a[2], b[2])
        self.assertEqual(self.decide_with("u6", {"id": "r", "upload_id": up["upload_id"], "media_type": "image/png"})[0], 200)
        # reused by another key of the workspace and again within the hour
        self.assertEqual(self.decide_with("u6", {"id": "r", "upload_id": up["upload_id"]}, key="dn_test_other")[0], 200)

    def assertUploadError(self, out, error_type):
        st, hd, body = out
        self.assertEqual(st, 400, body)
        self.assertEqual(body["detail"]["error_type"], error_type)
        self.assertEqual(hd["x-decisionnode-error"], body["detail"]["message"])

    def test_reference_errors(self):
        self.assertUploadError(self.decide_with("u7", {"id": "r", "upload_id": "upl_00000000000000000000000000000000"}), "upload_not_found")
        self.assertUploadError(self.decide_with("u7", {"id": "r", "upload_id": "nonsense"}), "upload_not_found")
        up = self.upload("u7")
        self.assertUploadError(self.decide_with("u7", {"id": "r", "upload_id": up["upload_id"]}, key="dn_test_ws-other_k"), "upload_not_found")
        self.assertUploadError(self.decide_with("u7", {"id": "r", "upload_id": up["upload_id"], "media_type": "image/jpeg"}), "upload_media_type_mismatch")
        pending = self.presign("u7", [file_of(PNG)])[0]
        self.assertUploadError(self.decide_with("u7", {"id": "r", "upload_id": pending["upload_id"]}), "upload_incomplete")
        wrong = self.presign("u7", [file_of(JPG, "image/png")])[0]
        self.assertEqual(self.put(wrong, JPG)[0], 200)
        self.assertUploadError(self.decide_with("u7", {"id": "r", "upload_id": wrong["upload_id"]}), "upload_media_type_mismatch")
        with self.httpd.state.lock:
            obj = self.httpd.state.uploads[up["upload_id"]]["object_key"]
            self.httpd.state.objects[obj] = PNG[:-1]
        self.assertUploadError(self.decide_with("u7", {"id": "r", "upload_id": up["upload_id"]}), "upload_mismatch")
        st, _, body = self.decide_with("u7", {"id": "r", "upload_id": up["upload_id"], "data": b64(PNG)})
        self.assertEqual((st, body["detail"]["error_type"]), (400, "api_usage_error"))

    def test_expiry_by_clock(self):
        up = self.upload("u8")
        self.control("/_mock/clock", "u8", {"advance_seconds": 3599})
        self.assertEqual(self.decide_with("u8", {"id": "r", "upload_id": up["upload_id"]})[0], 200)
        self.control("/_mock/clock", "u8", {"advance_seconds": 2})
        self.assertUploadError(self.decide_with("u8", {"id": "r", "upload_id": up["upload_id"]}), "upload_expired")

    def test_zero_retention_consumes(self):
        self.control("/_mock/workspace", "u9", {"zero_retention": True})
        up = self.upload("u9")
        self.assertEqual(self.decide_with("u9", {"id": "r", "upload_id": up["upload_id"]})[0], 200)
        self.assertUploadError(self.decide_with("u9", {"id": "r", "upload_id": up["upload_id"]}), "upload_consumed")
        # a refused request whose upload failed to resolve does not consume the others
        up2 = self.upload("u9")
        self.assertUploadError(self.decide_with("u9", {"id": "r", "upload_id": "upl_00000000000000000000000000000000"}), "upload_not_found")
        self.assertEqual(self.decide_with("u9", {"id": "r", "upload_id": up2["upload_id"]})[0], 200)

    def test_put_scenario_follows_presign(self):
        self.register({"name": "t-put", "rules": [{"match": {"$method": "PUT"}, "steps": [{"status": 500, "body_text": "<Error/>", "after_normal": True}]}]})
        st, _, out = self.call("POST", "/v1/uploads", {"files": [file_of(PNG)]}, self.h("u10", "t-put"))
        up = out["uploads"][0]
        self.assertEqual(self.put(up, PNG)[0], 500)
        self.assertEqual(self.put(up, PNG)[0], 412)
        log = self.call("GET", "/_mock/requests?session=u10")[2]["requests"]
        self.assertEqual([(r["method"], r["status"]) for r in log], [("POST", 201), ("PUT", 500), ("PUT", 412)])

    def register(self, scenario):
        st, _, out = self.call("POST", "/_mock/scenarios", scenario, headers={"Content-Type": "application/json"})
        self.assertEqual(st, 200, out)


class TestNumber(Spec11):
    def ask(self, q, path="/v1/decide", session="n"):
        return self.call("POST", path, {"model": "decisionnode-latest", "state": "s", "questions": {"cars": q}}, self.h(session))

    def test_answer_encoding(self):
        st, _, out = self.ask({"type": "number", "instructions": "How many?", "min": 0, "max": 50})
        self.assertEqual(st, 200)
        a = out["answers"]["cars"]
        self.assertEqual(list(a), ["type", "number", "expected", "confidence", "probabilities"])
        self.assertIsInstance(a["number"], int)
        self.assertIsInstance(a["expected"], float)
        keys = list(a["probabilities"])
        self.assertEqual(keys, sorted(keys, key=float))
        self.assertIn(str(a["number"]), keys)
        self.assertEqual(max(a["probabilities"].values()), a["probabilities"][str(a["number"])])
        self.assertLessEqual(sum(a["probabilities"].values()), 1 + 1e-9)
        for v in a["probabilities"].values():
            self.assertEqual(round(v, 3), v)

    def test_decimal_grids_and_keys(self):
        st, _, out = self.ask({"type": "number", "instructions": "?", "min": 0.25, "max": 2, "step": 0.5})
        a = out["answers"]["cars"]
        self.assertTrue(set(a["probabilities"]) <= {"0.25", "0.75", "1.25", "1.75"})
        self.assertEqual(json.dumps(a["number"]), [k for k in a["probabilities"] if float(k) == a["number"]][0])
        st, _, out = self.ask({"type": "number", "instructions": "?", "min": 0, "max": 0.3, "step": 0.1})
        self.assertTrue(set(out["answers"]["cars"]["probabilities"]) <= {"0", "0.1", "0.2", "0.3"})
        st, _, out = self.ask({"type": "number", "instructions": "?", "min": 5, "max": 5})
        self.assertEqual(out["answers"]["cars"], {"type": "number", "number": 5, "expected": 5.0, "confidence": 1.0, "probabilities": {"5": 1.0}})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/v1/decide", body=b'{"state": "", "questions": {"c": {"type": "number", "instructions": "?", "min": 1e0, "max": 2.50, "step": 0.50}}}', headers=AUTH)
        a = json.loads(conn.getresponse().read())["answers"]["c"]
        conn.close()
        self.assertTrue(set(a["probabilities"]) <= {"1", "1.5", "2", "2.5"})
        st, _, out = self.ask({"type": "number", "instructions": "?", "min": 0, "max": 4095})
        self.assertEqual(st, 200)

    def test_deterministic_and_independent(self):
        q = {"type": "number", "instructions": "How many?", "min": 0, "max": 20}
        a = self.ask(q)[2]
        self.assertEqual(a, self.ask(q)[2])
        both = self.call("POST", "/v1/decide", {"model": "decisionnode-latest", "state": "s", "questions": {"cars": q, "t": {"type": "truth", "instructions": "?"}}}, AUTH)[2]
        self.assertEqual(both["answers"]["cars"], a["answers"]["cars"])
        with_criteria = self.ask(dict(q, criteria={"ignored": True}))[2]
        self.assertEqual(with_criteria["answers"]["cars"], a["answers"]["cars"])

    def test_sum_cap_spares_number(self):
        grid = {"m": 1, "s": 1, "n": 3, "d": 0, "min": "1", "max": "3", "step": "1"}
        orig = server.Stream
        try:
            out = server.number_answer("seed", grid)
        finally:
            server.Stream = orig
        self.assertLessEqual(sum(out["probabilities"].values()), 1 + 1e-9)
        self.assertEqual(server.half_up(0.0625, 3), 0.063)
        self.assertEqual(server.half_up(0.805, 2), 0.81)
        self.assertEqual(server.round_expected(-2.345, 2), -2.34)
        self.assertEqual(server.dec_text(server.Decimal("-0.0")), "0")
        self.assertEqual(server.dec_text(server.Decimal("1E-6")), "0.000001")

    def test_422s(self):
        cases = [
            ({"min": "5"}, "float_type", ["min"]),
            ({"max": True}, "float_type", ["max"]),
            ({"min": -9007199254740993}, "greater_than_equal", ["min"]),
            ({"max": 9007199254740993}, "less_than_equal", ["max"]),
            ({"step": 0}, "greater_than", ["step"]),
            ({"min": 0.0000001, "max": 1}, "decimal_max_places", ["min"]),
            ({"min": 60, "max": 50}, "less_than_equal", ["min"]),
            ({"min": 0, "max": 1000, "step": 0.1}, "too_long", []),
            ({"min": 0, "max": 1000000000, "step": 0.000001}, "less_than_equal", ["max"]),
        ]
        for fields, kind, tail in cases:
            st, hd, out = self.ask(dict({"type": "number", "instructions": "?"}, **fields))
            self.assertEqual(st, 422, (fields, out))
            e = out["detail"][0]
            self.assertEqual(e["type"], kind, fields)
            self.assertEqual(e["loc"], ["body", "questions", "cars", "number"] + tail)
            self.assertTrue(hd["x-decisionnode-error"].startswith("question 'cars': "))
        st, hd, out = self.ask({"type": "number", "min": "a", "max": "b"})
        self.assertEqual(len(out["detail"]), 2)
        self.assertTrue(hd["x-decisionnode-error"].endswith(" and 1 more"))
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/v1/decide", body=b'{"state": "", "questions": {"c": {"type": "number", "instructions": "?", "step": NaN}}}', headers=AUTH)
        r = conn.getresponse()
        self.assertEqual(json.loads(r.read())["detail"][0]["type"], "finite_number")
        conn.close()

    def test_400s(self):
        for instr in (None, ""):
            q = {"type": "number", "min": 0, "max": 5}
            if instr is not None:
                q["instructions"] = instr
            st, hd, out = self.ask(q)
            self.assertEqual((st, out), (400, {"detail": "Number question must have instructions: cars"}))
            self.assertEqual(hd["x-decisionnode-error"], "question 'cars': a number question needs instructions")

    def test_models_fields(self):
        m = self.call("GET", "/v1/models")[2]["models"][0]
        self.assertEqual((m["number_one_question_values"], m["number_max_grid_values"], m["number_default_range"], m["number_default_step"]), (256, 4096, [0, 255], 1))
        self.assertEqual(m["upload_max_bytes"], 10485760)
        self.assertEqual(len(m["sessions"]), 11)
        # Suite 1.17: the batch price is the entry's batch_price_factor, never the batch
        # object's, which states limits only.
        self.assertNotIn("price_factor", m["batch"])
        self.assertNotIn("price_per_million_input_tokens", m["batch"])
        prices = {k: v for k, v in m.items() if k.startswith("price_per_million_") or k == "batch_price_factor"}
        self.assertEqual(prices, {"price_per_million_input": 0.042, "price_per_million_output": 0, "price_per_million_image": 0.042,
                                  "price_per_million_video": 0.042, "batch_price_factor": 0.7})
        flash = next(e for e in self.call("GET", "/v1/models")[2]["models"] if e["name"] == "decisionnode-flash")
        self.assertEqual(flash["batch_price_factor"], 0.5)


class TestTruth(Spec11):
    def test_noul_alias_same_value(self):
        t = self.call("POST", "/v1/decide", {"state": "s", "questions": {"q": {"type": "truth", "instructions": "x"}}})[2]["answers"]["q"]
        n = self.call("POST", "/v1/decide", {"state": "s", "questions": {"q": {"type": "noul", "instructions": "x"}}})[2]["answers"]["q"]
        self.assertEqual(n, {"type": "noul", "noul": t["truth"]})
        st, _, out = self.call("POST", "/v1/decide", {"state": "s", "questions": {"q": {"type": "noul"}}})
        self.assertEqual((st, out), (400, {"detail": "Noul question must have criteria or instructions: q"}))

    def test_legacy_switch(self):
        self.control("/_mock/truth", "lg", {"legacy_noul": True})
        out = self.call("POST", "/v1/decide", {"state": "s", "questions": {"q": {"type": "truth", "instructions": "x"}}}, self.h("lg"))[2]
        self.assertEqual(list(out["answers"]["q"]), ["type", "noul"])
        out = self.call("POST", "/v1/decide", {"state": "s", "questions": {"q": {"type": "truth", "instructions": "x"}}}, self.h("other"))[2]
        self.assertEqual(out["answers"]["q"]["type"], "truth")


class TestSessions(Spec11):
    QS = {"abort": {"type": "truth", "instructions": "Abort?"}, "mode": {"type": "choice", "criteria": {"go": None, "stop": None}}}

    def open(self, session, scenario=None, **extra):
        body = dict({"questions": self.QS}, **extra)
        st, hd, out = self.call("POST", "/v1/sessions", body, self.h(session, scenario))
        self.assertEqual(st, 200, out)
        return out, hd

    def ws(self, out, session, key=KEY):
        return reference_client._WebSocket(out["stream_url"], {"Authorization": "Bearer " + key, "x-mock-session": session}, 5)

    def next(self, sock, timeout=5):
        kind, value = sock.events.get(timeout=timeout)
        return (kind, json.loads(value)) if kind == "text" else (kind, value)

    def test_open_shape_and_headers(self):
        out, hd = self.open("s1", state={"m": 1}, instructions="sup", window=3, ttl_seconds=60)
        self.assertEqual(list(out), ["session_id", "model", "stream_url", "window", "ttl_seconds", "expires_at", "limits", "questions", "prefix_tokens", "usage", "answers", "safety"])
        self.assertRegex(out["session_id"], r"^ses_[0-9a-f]{24}$")
        self.assertEqual(out["stream_url"], "ws://127.0.0.1:%d/v1/sessions/%s/stream" % (self.port, out["session_id"]))
        self.assertEqual((out["window"], out["ttl_seconds"], out["questions"]), (3, 60, ["abort", "mode"]))
        self.assertEqual(hd["x-decisionnode-billable-tokens"], str(out["usage"]["input_tokens"]))
        self.assertEqual(out["safety"]["checked"], True)
        self.assertEqual(out["answers"]["abort"]["type"], "truth")

    def test_open_refusals(self):
        for body, status in [
            ({"questions": self.QS, "images": []}, 400),
            ({"questions": self.QS, "temperature": 1}, 400),
            ({"questions": self.QS, "model": "decisionnode-9"}, 400),
            ({"questions": self.QS, "window": 0}, 422),
            ({"questions": self.QS, "window": 65}, 422),
            ({"questions": self.QS, "ttl_seconds": 3601}, 422),
            ({"questions": self.QS, "window": "8"}, 422),
            ({}, 422),
            ({"questions": {}}, 422),
        ]:
            st, hd, out = self.call("POST", "/v1/sessions", body, self.h("s2"))
            self.assertEqual(st, status, body)
            self.assertEqual(hd["x-decisionnode-billable-tokens"], "0")
        st, _, out = self.call("POST", "/v1/sessions", {"questions": self.QS, "images": []}, self.h("s2"))
        self.assertEqual(out["detail"]["message"], "images: send images in frames, not when opening.")

    def test_session_limit_per_key(self):
        for _ in range(4):
            self.open("s3")
        st, hd, out = self.call("POST", "/v1/sessions", {"questions": self.QS}, self.h("s3"))
        self.assertEqual((st, out["detail"]["error_type"]), (429, "session_limit_exceeded"))
        self.assertIn("retry-after", hd)
        self.assertIn("retry-after-ms", hd)

    def test_stream_requires_upgrade(self):
        out, _ = self.open("s4")
        st, hd, body = self.call("GET", "/v1/sessions/%s/stream" % out["session_id"], headers=self.h("s4"))
        self.assertEqual((st, hd.get("upgrade")), (426, "websocket"))

    def test_frames_replies_and_latest_wins(self):
        self.control("/_mock/sessions", "s5", {"hold_until_frames": 3})
        out, _ = self.open("s5")
        sock = self.ws(out, "s5")
        for i, f in enumerate(["a", "b", "c"], start=1):
            sock.send(1, json.dumps({"seq": i, "frame": f}).encode())
        k1, r1 = self.next(sock)
        k2, r2 = self.next(sock)
        self.assertEqual((r1["seq"], "dropped" in r1), (1, False))
        self.assertEqual((r2["seq"], r2["dropped"]), (3, [2]))
        self.assertEqual(list(r2), ["seq", "answers", "dropped", "usage", "latency_ms", "safety"])
        sock.send(1, json.dumps({"frame": "d", "questions": ["abort"]}).encode())
        _, r3 = self.next(sock)
        self.assertEqual((r3["seq"], list(r3["answers"])), (4, ["abort"]))
        st, _, summary = self.call("DELETE", "/v1/sessions/" + out["session_id"], headers=self.h("s5"))
        self.assertEqual(st, 200)
        self.assertEqual((summary["frames_answered"], summary["frames_dropped"], summary["last_seq"], summary["status"]), (3, 1, 4, "ended"))
        kind, (code, _) = self.next(sock)
        self.assertEqual((kind, code), ("close", 1000))
        st, _, _ = self.call("DELETE", "/v1/sessions/" + out["session_id"], headers=self.h("s5"))
        self.assertEqual(st, 404)
        kinds = [r["method"] for r in self.call("GET", "/_mock/requests?session=s5")[2]["requests"]]
        self.assertEqual(kinds, ["POST", "GET", "WS", "WS", "WS", "WS", "DELETE", "DELETE"])

    def test_frame_validation_errors(self):
        out, _ = self.open("s6")
        sock = self.ws(out, "s6")
        for msg, seq_, text in [
            (b"not json", None, "Frames are text messages holding one JSON object."),
            (json.dumps({"seq": 1, "frame": "a", "extra": 1}).encode(), 1, "extra: this field is not taken in a frame."),
            (json.dumps({"seq": 2}).encode(), 2, "frame: a frame needs a frame value or an image."),
            (json.dumps({"seq": 3, "frame": ""}).encode(), 3, "frame: must be a string, an object or an array, not empty."),
            (json.dumps({"seq": 4, "frame": "a", "questions": ["speed"]}).encode(), 4, "questions: unknown question id 'speed'"),
            (json.dumps({"seq": 5, "frame": "a", "reset": "yes"}).encode(), 5, "reset: must be true or false."),
            (json.dumps({"seq": 6, "frame": "x" * 7000}).encode(), 6, "The frame is 1750 tokens, the limit is 1535."),
            (json.dumps({"seq": -1, "frame": "a"}).encode(), None, "seq: must be an integer from 0 to 9007199254740991."),
            (json.dumps({"seq": 7, "image": {"upload_id": "upl_00000000000000000000000000000000"}}).encode(), 7, None),
        ]:
            sock.send(1, msg)
            _, err = self.next(sock)
            self.assertEqual(err["seq"], seq_, msg)
            if text is not None:
                self.assertEqual(err["error"], {"error_type": "api_usage_error", "message": text})
            else:
                self.assertEqual(err["error"]["error_type"], "upload_not_found")
        sock.send(2, b"\x00\x01")
        _, err = self.next(sock)
        self.assertEqual(err["seq"], None)
        sock.send(1, json.dumps({"seq": 8, "image": {"media_type": "image/png", "data": b64(PNG)}}).encode())
        _, rep = self.next(sock)
        self.assertEqual(rep["seq"], 8)
        self.assertGreater(rep["usage"]["input_tokens"], server.IMAGE_TOKENS)
        sock.close()

    def test_connect_refusals(self):
        out, _ = self.open("s7")
        bad = self.ws(out, "s7", key="invalid_key")
        self.assertEqual(self.next(bad)[1][0], 4401)
        first = self.ws(out, "s7")
        second = self.ws(out, "s7")
        self.assertEqual(self.next(second)[1][0], 4409)
        first.send(1, json.dumps({"seq": 1, "frame": "a"}).encode())
        self.assertEqual(self.next(first)[0], "text")
        first.close()
        self.assertEqual(self.next(first)[0], "close")
        again = self.ws(out, "s7")
        self.assertEqual(self.next(again)[1][0], 4404)
        conn = reference_client._WebSocket("ws://127.0.0.1:%d/v1/sessions/ses_000000000000000000000000/stream" % self.port, {"Authorization": "Bearer " + KEY}, 5)
        self.assertEqual(self.next(conn)[1][0], 4404)

    def test_end_right_after_connect_closes_1000(self):
        # The socket is attached before the handshake is answered: a DELETE sent the
        # moment the client is connected closes it with 1000, never a refused 4404.
        for i in range(20):
            out, _ = self.open("s7e%d" % i)
            sock = self.ws(out, "s7e%d" % i)
            st, _, _ = self.call("DELETE", "/v1/sessions/" + out["session_id"], headers=self.h("s7e%d" % i))
            self.assertEqual(st, 200)
            kind, value = self.next(sock)
            self.assertEqual((kind, value[0]), ("close", 1000), i)

    def test_timers_and_pings(self):
        self.control("/_mock/sessions", "s8", {"idle_timeout_ms": 300, "ping_interval_ms": 50})
        out, _ = self.open("s8")
        sock = self.ws(out, "s8")
        kind, (code, _) = self.next(sock)
        self.assertEqual((kind, code), ("close", 4408))
        log = self.call("GET", "/_mock/requests?session=s8")[2]["requests"]
        self.assertGreaterEqual(sum(1 for r in log if r["method"] == "PONG"), 2)
        out, _ = self.open("s9", ttl_seconds=1)
        sock = self.ws(out, "s9")
        kind, (code, _) = self.next(sock, timeout=5)
        self.assertEqual((kind, code), ("close", 4410))

    def test_scenario_closes_and_errors(self):
        self.control("/_mock/sessions", "s10", {"frame_errors": {"1": {"error_type": "refusal_error", "category": "self_harm"}, "2": {"error_type": "rate_limit_error"}}, "close": {"after_replies": 1, "code": 4402}})
        out, _ = self.open("s10")
        sock = self.ws(out, "s10")
        sock.send(1, json.dumps({"seq": 1, "frame": "a"}).encode())
        _, e1 = self.next(sock)
        self.assertEqual(list(e1), ["seq", "error", "usage", "safety"])
        self.assertEqual((e1["error"]["category"], e1["safety"]["blocked"]), ("self_harm", "self_harm"))
        sock.send(1, json.dumps({"seq": 2, "frame": "a"}).encode())
        _, e2 = self.next(sock)
        self.assertEqual((e2["error"]["error_type"], e2["retry_after_ms"]), ("rate_limit_error", 1000))
        sock.send(1, json.dumps({"seq": 3, "frame": "a"}).encode())
        self.assertEqual(self.next(sock)[1]["seq"], 3)
        self.assertEqual(self.next(sock)[1][0], 4402)
        self.control("/_mock/sessions", "s11", {"close": {"on_connect": 1013}})
        out, _ = self.open("s11")
        self.assertEqual(self.next(self.ws(out, "s11"))[1][0], 1013)
        self.control("/_mock/sessions", "s11", {"close": {}})
        sock = self.ws(out, "s11")  # 1013 keeps the session
        sock.send(1, json.dumps({"seq": 1, "frame": "a"}).encode())
        self.assertEqual(self.next(sock)[0], "text")


class TestBatches(Spec11):
    LINE = '{"custom_id": "%s", "state": "%s", "questions": {"q": {"type": "truth", "instructions": "?"}}}'

    def post(self, path, text, session, headers=None):
        h = self.h(session)
        h["Content-Type"] = "application/x-ndjson"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=text.encode() if isinstance(text, str) else text, headers=h)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body

    def get(self, path, session):
        return self.call("GET", path, headers=self.h(session))

    def test_create_formats_and_refusals(self):
        st, _, b = self.post("/v1/batches", "", "b1")
        self.assertEqual((st, b["state"], b["counters"]["requests"]), (201, "draft", 0))
        self.assertRegex(b["id"], r"^batch_[0-9A-HJKMNP-TV-Z]{10}$")
        self.assertEqual(list(b), ["id", "state", "counters", "created_at", "finalized_at", "expires_at", "ended_at", "results_url", "results_expire_at", "usage", "error"])
        st, _, b = self.post("/v1/batches", "\ufeff" + self.LINE % ("a", "x") + "\r\n\n" + self.LINE % ("b", "y"), "b1")
        self.assertEqual(b["counters"]["requests"], 2)
        st, _, b = self.post("/v1/batches", "[" + self.LINE % ("a", "x") + "]", "b1")
        self.assertEqual(b["counters"]["requests"], 1)
        for text, msg in [
            ("nope", "The upload is neither JSON Lines nor a JSON array."),
            ("[1]", "Line 1 is not a JSON object."),
            ('{"state": "x"}', "Line 1 has no custom_id."),
            ('{"custom_id": ""}', "Line 1: custom_id must be a string of 1 to 512 characters."),
            ('{"custom_id": 5}', "Line 1: custom_id must be a string of 1 to 512 characters."),
            (self.LINE % ("a", "x") + "\n" + self.LINE % ("a", "y"), 'Line 2: custom_id "a" is already in the batch.'),
        ]:
            st, hd, out = self.post("/v1/batches", text, "b1x")
            self.assertEqual((st, out["detail"]["message"]), (400, msg))
            self.assertIn("x-decisionnode-error", hd)
        st, _, out = self.post("/v1/batches", "", "b1x", {"Content-Encoding": "gzip"})
        self.assertEqual(out["detail"]["message"], "Compressed uploads are not accepted.")
        for _ in range(7):
            self.post("/v1/batches", "", "b1")
        st, hd, out = self.post("/v1/batches", "", "b1")
        self.assertEqual((st, out["detail"]["error_type"], hd["retry-after"]), (429, "batch_limit_exceeded", "30"))

    def test_batch_line_pays_its_models_share(self):
        """Suite 1.17: a batch line pays its model's batch_price_factor of the live price,
        as /v1/models states it: MoE 0.7, Flash 0.5."""
        q = {"q": {"type": "truth", "instructions": "Is it fine?"}}
        lines = [{"custom_id": m, "model": m, "state": "the same state", "questions": q} for m in ("decisionnode-moe", "decisionnode-flash")]
        st, _, b = self.post("/v1/batches", "\n".join(json.dumps(x) for x in lines), "b9")
        self.post("/v1/batches/%s/finalize" % b["id"], "", "b9")
        self.get("/v1/batches/" + b["id"], "b9")
        self.assertEqual(self.get("/v1/batches/" + b["id"], "b9")[2]["state"], "completed")
        st, _, raw = self.raw_call("GET", "/v1/batches/%s/results" % b["id"], None, self.h("b9"))
        got = {x["custom_id"]: x["body"]["usage"]["cost"]["model_usd"] for x in map(json.loads, raw.decode().splitlines())}
        factors = {e["name"]: e["batch_price_factor"] for e in self.call("GET", "/v1/models", headers=self.h("b9"))[2]["models"] if "batch_price_factor" in e}
        for m in ("decisionnode-moe", "decisionnode-flash"):
            live = self.call("POST", "/v1/decide", {"model": m, "state": "the same state", "questions": q}, self.h("b9"))[2]["usage"]["cost"]["model_usd"]
            self.assertAlmostEqual(got[m], live * factors[m], places=9, msg=m)
        self.assertEqual((factors["decisionnode-moe"], factors["decisionnode-flash"]), (0.7, 0.5))

    def test_lifecycle_results_and_list(self):
        text = "\n".join([self.LINE % ("ok", "x"), '{"custom_id": "bad", "model": "decisionnode-0.1", "questions": {"q": {"type": "truth", "instructions": "?"}}}', self.LINE % ("ok2", "y")])
        st, _, b = self.post("/v1/batches", text, "b2")
        self.assertEqual(b["counters"]["failed"], 1)
        bid = b["id"]
        st, _, out = self.post("/v1/batches/%s/requests" % bid, self.LINE % ("ok", "z"), "b2")
        self.assertEqual(st, 400)
        st, _, out = self.post("/v1/batches/%s/requests" % bid, "", "b2")
        self.assertEqual(out["detail"]["message"], "The call holds no requests.")
        st, _, b = self.post("/v1/batches/%s/requests" % bid, self.LINE % ("ok3", "z"), "b2")
        self.assertEqual(b["counters"]["requests"], 4)
        st, _, out = self.get("/v1/batches/%s/results" % bid, "b2")
        self.assertEqual((st, out["detail"]["error_type"]), (409, "results_not_ready"))
        st, _, b = self.post("/v1/batches/%s/finalize" % bid, "", "b2")
        self.assertEqual((b["state"], b["counters"]["queued"]), ("queued", 3))
        self.assertIsNotNone(b["expires_at"])
        st, _, out = self.post("/v1/batches/%s/finalize" % bid, "", "b2")
        self.assertEqual((st, out["detail"]["error_type"]), (409, "invalid_batch_state"))
        st, _, out = self.post("/v1/batches/%s/requests" % bid, self.LINE % ("late", "z"), "b2")
        self.assertEqual(st, 409)
        self.assertEqual(self.get("/v1/batches/" + bid, "b2")[2]["state"], "running")
        b = self.get("/v1/batches/" + bid, "b2")[2]
        self.assertEqual((b["state"], b["counters"]["completed"], b["counters"]["failed"]), ("completed", 3, 1))
        self.assertGreater(b["usage"]["input_tokens"], 0)
        self.assertTrue(b["results_url"].endswith("/v1/batches/%s/results" % bid))
        st, hd, raw = self.raw_call("GET", "/v1/batches/%s/results" % bid, None, self.h("b2"))
        self.assertEqual((st, hd["content-type"]), (200, "application/x-ndjson"))
        self.assertEqual(hd["etag"], '"%s"' % hashlib.sha256(raw).hexdigest())
        lines = [json.loads(x) for x in raw.decode().splitlines()]
        self.assertEqual([(x["custom_id"], x["status"]) for x in lines], [("ok", 200), ("bad", 400), ("ok2", 200), ("ok3", 200)])
        self.assertEqual(list(lines[0]), ["custom_id", "status", "body", "headers"])
        self.assertEqual(lines[1]["headers"], {"x-decisionnode-error": "unknown model 'decisionnode-0.1'; GET /v1/models lists the models"})
        st, _, page = self.get("/v1/batches/%s/results?format=json&offset=1&limit=2" % bid, "b2")
        self.assertEqual((len(page["results"]), page["total"], page["next_offset"]), (2, 4, 3))
        st, _, out = self.post("/v1/batches/%s/cancel" % bid, "", "b2")
        self.assertEqual((st, out["detail"]["message"]), (409, "Batch %s has already ended completed." % bid))
        self.control("/_mock/clock", "b2", {"advance_seconds": 8 * 86400})
        st, _, out = self.get("/v1/batches/%s/results" % bid, "b2")
        self.assertEqual((st, out["detail"]["error_type"]), (410, "results_expired"))
        self.post("/v1/batches", "", "b2")
        st, _, page = self.get("/v1/batches?limit=1", "b2")
        self.assertEqual((page["total"], page["limit"], page["next_offset"], page["batches"][0]["state"]), (2, 1, 1, "draft"))
        for q in ("limit=0", "limit=101", "offset=-1", "limit=x"):
            self.assertEqual(self.get("/v1/batches?" + q, "b2")[0], 400)
        self.assertEqual(self.get("/v1/batches", "other")[2]["total"], 0)

    def test_cancel_expiry_and_not_found(self):
        st, _, b = self.post("/v1/batches", self.LINE % ("a", "x"), "b3")
        st, _, b = self.post("/v1/batches/%s/cancel" % b["id"], "", "b3")
        self.assertEqual((b["state"], b["counters"]["cancelled"]), ("cancelled", 1))
        self.assertEqual(self.post("/v1/batches/%s/cancel" % b["id"], "", "b3")[0], 200)
        st, _, b = self.post("/v1/batches", self.LINE % ("a", "x"), "b3")
        self.post("/v1/batches/%s/finalize" % b["id"], "", "b3")
        self.control("/_mock/clock", "b3", {"advance_seconds": 86401})
        b = self.get("/v1/batches/" + b["id"], "b3")[2]
        self.assertEqual((b["state"], b["counters"]["expired"]), ("expired", 1))
        st, _, out = self.post("/v1/batches", "", "b3")
        self.assertEqual(self.post("/v1/batches/%s/finalize" % out["id"], "", "b3")[2]["detail"]["message"], "Batch %s holds no requests." % out["id"])
        for path in ("/v1/batches/batch_0000000000", "/v1/batches/nonsense", "/v1/batches/%s" % out["id"]):
            sess = "b3" if path.endswith("nonsense") or "0000" in path else "someone-else"
            st, _, body = self.get(path, sess)
            self.assertEqual((st, body["detail"]["error_type"]), (404, "not_found_error"))
        st, hd, _ = self.call("DELETE", "/v1/batches/" + out["id"], headers=self.h("b3"))
        self.assertEqual((st, hd["allow"]), (405, "GET"))

    def test_upload_reference_copied_at_add(self):
        up = self.upload("b4")
        line = '{"custom_id": "img", "images": [{"id": "r", "upload_id": "%s"}], "questions": {"q": {"type": "truth", "instructions": "?"}}}' % up["upload_id"]
        st, _, b = self.post("/v1/batches", line, "b4")
        self.assertEqual(b["counters"]["failed"], 0)
        self.post("/v1/batches/%s/finalize" % b["id"], "", "b4")
        self.control("/_mock/clock", "b4", {"advance_seconds": 7200})  # the upload has expired; the batch has its copy
        self.get("/v1/batches/" + b["id"], "b4")
        self.assertEqual(self.get("/v1/batches/" + b["id"], "b4")[2]["counters"]["completed"], 1)
        bad = '{"custom_id": "img", "images": [{"id": "r", "upload_id": "upl_00000000000000000000000000000000"}], "questions": {"q": {"type": "truth", "instructions": "?"}}}'
        st, _, b = self.post("/v1/batches", bad, "b4")
        self.assertEqual(b["counters"]["failed"], 1)


class TestControl(Spec11):
    def test_clock_and_reset(self):
        st, _, out = self.control("/_mock/clock", "c1", {"offset_seconds": -100})
        self.assertEqual(out["offset_seconds"], -100.0)
        st, _, out = self.control("/_mock/clock", "c1", {"advance_seconds": 40})
        self.assertEqual(out["offset_seconds"], -60.0)
        self.upload("c1")
        self.call("POST", "/_mock/reset?session=c1", headers={})
        self.assertEqual(self.call("GET", "/_mock/uploads?session=c1")[2]["uploads"], [])
        self.assertEqual(self.control("/_mock/clock", "c1", {})[2]["offset_seconds"], 0.0)

    def test_scenario_settings_registration(self):
        st, _, _ = self.call("POST", "/_mock/scenarios", {"name": "t-settings", "truth_as_noul": True}, {"Content-Type": "application/json"})
        self.assertEqual(st, 200)
        out = self.call("POST", "/v1/decide", {"state": "s", "questions": {"q": {"type": "truth", "instructions": "x"}}}, self.h("c2", "t-settings"))[2]
        self.assertEqual(out["answers"]["q"]["type"], "noul")

    def test_request_cases_register_scenarios(self):
        names = self.call("GET", "/_mock/scenarios")[2]["scenarios"]
        self.assertIn("request/batch-get", names)
        self.assertIn("behaviour/session-latest-wins-dropped", names)



class TestSuite15(Spec11):
    """The mock changes of suite 1.5: robustness, the clock hooks and the new fixed rules."""

    TRUTH = {"q": {"type": "truth", "instructions": "Does the receipt total equal the claim?"}}

    def test_listen_backlog_and_idle_timeout(self):
        self.assertEqual(server.Server.request_queue_size, 128)
        self.assertEqual(server.Handler.timeout, 60)
        # 64 clients connect at the same moment, before the mock accepts any: none refused.
        socks = []
        try:
            for _ in range(64):
                socks.append(socket.create_connection(("127.0.0.1", self.port), timeout=10))
            for s in socks:
                s.sendall(b"GET /_mock/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            for s in socks:
                data = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:40])
        finally:
            for s in socks:
                s.close()

    def test_clock_read(self):
        st, _, out = self.call("GET", "/_mock/clock?session=k1", headers={})
        self.assertEqual((st, out["offset_seconds"]), (200, 0.0))
        self.assertAlmostEqual(out["epoch_seconds"], time.time(), delta=5)
        self.control("/_mock/clock", "k1", {"advance_seconds": 301})
        out = self.call("GET", "/_mock/clock?session=k1", headers={})[2]
        self.assertEqual(out["offset_seconds"], 301.0)
        self.assertAlmostEqual(out["epoch_seconds"], time.time() + 301, delta=5)

    def test_step_advances_the_clock_before_it_acts(self):
        scenario = {"name": "t-advance", "rules": [{"match": {"$method": "PUT"}, "steps": [{"action": "normal", "advance_clock_seconds": 301}]}]}
        self.assertEqual(self.call("POST", "/_mock/scenarios", scenario, {"Content-Type": "application/json"})[0], 200)
        st, _, out = self.call("POST", "/v1/uploads", {"files": [file_of(PNG)]}, self.h("k2", "t-advance"))
        self.assertEqual(st, 201)
        up = out["uploads"][0]
        st, _, text = self.put(up, PNG)
        self.assertEqual(st, 403)
        self.assertIn(b"<Code>AccessDenied</Code>", text)
        self.assertEqual(self.call("GET", "/_mock/clock?session=k2", headers={})[2]["offset_seconds"], 301.0)
        # The next PUT uses the rule's default (normal, no advance): a fresh URL is accepted.
        up2 = self.call("POST", "/v1/uploads", {"files": [file_of(PNG)]}, self.h("k2", "t-advance"))[2]["uploads"][0]
        self.assertEqual(self.put(up2, PNG)[0], 200)

    def test_image_url_rules(self):
        def ask(url):
            st, hd, out = self.decide({"state": "s", "images": [{"id": "u", "url": url}], "questions": self.TRUTH})
            return st, (out.get("detail") or {}).get("error_type") if isinstance(out.get("detail"), dict) else None

        base = "https://files.example.com/"
        exact = base + "a" * (2048 - len(base))
        self.assertEqual(ask(exact)[0], 200)
        self.assertEqual(ask(exact + "a"), (400, "image_url_refused"))
        self.assertEqual(ask("http://files.example.com/r.jpg"), (400, "api_usage_error"))
        for url in (
            "https://files.example.com:8443/r.jpg",
            "https://user:pw@files.example.com/r.jpg",
            "https://169.254.169.254/r.jpg",
            "https://10.1.2.3/r.jpg",
            "https://[::1]/r.jpg",
            "https://[::ffff:192.168.0.1]/r.jpg",
            "https://[64:ff9b::a00:1]/r.jpg",
            "https://[fd00:ec2::254]/r.jpg",
        ):
            self.assertEqual(ask(url), (400, "image_url_refused"), url)
        self.assertEqual(ask("https://files.example.com:443/r.jpg")[0], 200)
        self.assertEqual(ask("https://8.8.8.8/r.jpg")[0], 200)

    def test_nesting_limit(self):
        def nest(n):
            v = "s"
            for _ in range(n):
                v = [v]
            return v

        self.assertEqual(self.decide({"state": nest(127), "questions": self.TRUTH})[0], 200)
        st, hd, out = self.decide({"state": nest(128), "questions": self.TRUTH})
        self.assertEqual((st, out), (400, {"detail": {"error_type": "api_usage_error", "message": "Invalid request."}}))
        self.assertIn("129", hd["x-decisionnode-error"])
        deep = ("[" * 5000 + "]" * 5000).encode()
        st, _, out = self.call("POST", "/v1/decide", raw=b'{"state": ' + deep + b', "questions": {"q": {"type": "truth", "instructions": "x"}}}')
        self.assertEqual((st, out["detail"]["error_type"]), (400, "api_usage_error"))
        st, _, out = self.call("POST", "/v1/sessions", {"state": nest(128), "questions": self.TRUTH}, self.h("k3"))
        self.assertEqual((st, out["detail"]["message"]), (400, "Invalid request."))

    def test_unknown_type_reason_lists_number(self):
        st, hd, out = self.decide({"state": "s", "questions": {"q": {"type": "rank", "instructions": "?"}}})
        self.assertEqual(st, 400)
        self.assertEqual(hd["x-decisionnode-error"], "question 'q': unknown type \"rank\"; the types are truth, noul, choice, score, number, point and box")

    def test_internal_fault_is_a_500_not_a_drop(self):
        original = server.Handler.api_models

        def broken(self, *a):
            raise RuntimeError("boom")

        server.Handler.api_models = broken
        try:
            st, hd, out = self.call("GET", "/v1/models")
        finally:
            server.Handler.api_models = original
        self.assertEqual((st, out["detail"]["error_type"]), (500, "mock_error"))
        self.assertIn("boom", out["detail"]["message"])
        self.assertEqual(self.call("GET", "/_mock/health", headers={})[0], 200)

    def test_errors_are_quiet_without_verbose(self):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                raise RuntimeError("a handler fault")
            except RuntimeError:
                self.httpd.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")

    def test_log_file_option(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "mock.log")
            proc = subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "mock", "server.py"), "--port", "0", "--log-file", log],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                line = proc.stdout.readline().strip()
                port = int(line.rsplit(":", 1)[1])
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/_mock/health")
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
                time.sleep(0.1)
                with open(log, encoding="utf-8") as fh:
                    self.assertIn("/_mock/health", fh.read())
            finally:
                proc.terminate()
                _, err = proc.communicate(timeout=10)
            self.assertEqual(err, "")

    def open_session(self, session):
        st, _, out = self.call("POST", "/v1/sessions", {"questions": self.TRUTH}, self.h(session))
        self.assertEqual(st, 200, out)
        return out

    def test_message_over_1_mib_closes_1009(self):
        out = self.open_session("k4")
        sock = reference_client._WebSocket(out["stream_url"], {"Authorization": "Bearer " + KEY, "x-mock-session": "k4"}, 5)
        try:
            sock.send(1, json.dumps({"seq": 1, "frame": "x" * (1024 * 1024)}).encode())
            kind, value = sock.events.get(timeout=5)
            self.assertEqual((kind, value[0]), ("close", 1009))
        finally:
            sock.sock.close()

    def test_frame_nesting_is_a_frame_error(self):
        out = self.open_session("k5")
        sock = reference_client._WebSocket(out["stream_url"], {"Authorization": "Bearer " + KEY, "x-mock-session": "k5"}, 5)
        try:
            frame = "s"
            for _ in range(128):
                frame = [frame]
            sock.send(1, json.dumps({"seq": 1, "frame": frame}).encode())
            kind, value = sock.events.get(timeout=5)
            self.assertEqual(json.loads(value)["error"]["error_type"], "api_usage_error")
            sock.send(1, json.dumps({"seq": 2, "frame": "ok"}).encode())
            kind, value = sock.events.get(timeout=5)
            self.assertEqual(json.loads(value)["seq"], 2)
        finally:
            sock.sock.close()

    def test_bad_control_frame_ends_the_socket_not_the_mock(self):
        out = self.open_session("k6")
        sock = reference_client._WebSocket(out["stream_url"], {"Authorization": "Bearer " + KEY, "x-mock-session": "k6"}, 5)
        try:
            # A ping that claims 2^62 bytes: the mock treats the socket as broken.
            sock.sock.sendall(bytes([0x89, 0x80 | 127]) + struct.pack(">Q", 2**62) + b"\0\0\0\0")
            kind, value = sock.events.get(timeout=5)
            self.assertEqual(kind, "close")
        finally:
            sock.sock.close()
        self.assertEqual(self.call("GET", "/_mock/health", headers={})[0], 200)
        st, _, _ = self.call("DELETE", "/v1/sessions/%s" % out["session_id"], headers=self.h("k6"))
        self.assertIn(st, (200, 404))

    def test_client_drop_mid_stream_keeps_the_mock(self):
        self.control("/_mock/sessions", "k7", {"frame_delay_ms": 200})
        out = self.open_session("k7")
        sock = reference_client._WebSocket(out["stream_url"], {"Authorization": "Bearer " + KEY, "x-mock-session": "k7"}, 5)
        sock.send(1, json.dumps({"seq": 1, "frame": "a"}).encode())
        sock.sock.shutdown(socket.SHUT_RDWR)
        sock.sock.close()
        time.sleep(0.5)
        self.assertEqual(self.call("GET", "/_mock/health", headers={})[0], 200)
        self.assertEqual(self.open_session("k7b")["model"], server.MOE)



class TestSuite16(Spec11):
    def test_models_state_version_and_alias(self):
        st, _, out = self.call("GET", "/v1/models")
        self.assertEqual(st, 200)
        by_name = {m["name"]: m for m in out["models"]}
        for name, version, alias in (
            ("decisionnode-moe", server.MOE, "decisionnode-moe"),
            (server.MOE, server.MOE, "decisionnode-moe"),
            ("decisionnode-flash", server.FLASH, "decisionnode-flash"),
            (server.FLASH, server.FLASH, "decisionnode-flash"),
        ):
            self.assertEqual((by_name[name]["version"], by_name[name]["alias"]), (version, alias))
            self.assertEqual(list(by_name[name])[:5], ["name", "description", "release_date", "version", "alias"])

    def test_slow_put_step(self):
        scenario = {"name": "t-slow-put", "rules": [{"match": {"$method": "PUT"}, "steps": [{"action": "normal", "delay_ms": 300}]}]}
        self.assertEqual(self.call("POST", "/_mock/scenarios", scenario, {"Content-Type": "application/json"})[0], 200)
        up = self.call("POST", "/v1/uploads", {"files": [file_of(PNG)]}, self.h("m1", "t-slow-put"))[2]["uploads"][0]
        started = time.monotonic()
        self.assertEqual(self.put(up, PNG)[0], 200)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)



class TestSuite111(Spec11):
    """Suite 1.11 (SDK specification 1.3): boosters and fine-tuned model names."""

    IMAGES = [{"id": "person", "url": "https://files.example.com/person.jpg"}, {"id": "poster", "url": "https://files.example.com/poster.jpg"}]
    Q = {"adult": {"type": "truth", "instructions": "Is the person an adult?"}}
    ACME = "dn_test_ws-acme"

    def boosted(self, boosters, session="t1", images=None, questions=None, key=KEY):
        body = {"images": self.IMAGES if images is None else images, "questions": questions or self.Q, "boosters": boosters}
        return self.call("POST", "/v1/decide", body, self.h(session, key=key))

    def test_evidence_usage_and_header(self):
        st, hd, out = self.boosted(["ai_generated", {"name": "age", "images": ["person"]}])
        self.assertEqual(st, 200, out)
        self.assertEqual(list(out), ["model", "answers", "evidence", "usage"])
        self.assertEqual(list(out["evidence"]), ["ai_generated", "age"])
        self.assertEqual([r["image"] for r in out["evidence"]["ai_generated"]["results"]], ["person", "poster"])
        self.assertEqual([r["image"] for r in out["evidence"]["age"]["results"]], ["person"])
        for name, e in out["evidence"].items():
            self.assertEqual((e["version"], e["status"]), (name + "-v1", "ok"))
        self.assertEqual(hd["x-decisionnode-boosters"], "ai_generated=ok:340,age=ok:212")
        self.assertEqual(out["usage"]["boosters"], 3)
        self.assertEqual([e["billed_calls"] for e in out["evidence"].values()], [2, 1])
        self.assertEqual(list(out["evidence"]["age"]), ["version", "status", "latency_ms", "billed_calls", "results"])
        self.assertEqual(out["usage"]["booster_tokens"], 2 * server.BOOSTER_ENTRY_TOKENS + 3 * server.BOOSTER_READING_TOKENS)
        plain = self.call("POST", "/v1/decide", {"images": self.IMAGES, "questions": self.Q}, self.h("t1"))[2]
        self.assertEqual(out["usage"]["input_tokens"] - plain["usage"]["input_tokens"], out["usage"]["booster_tokens"])
        self.assertEqual(out["answers"], plain["answers"])
        again = self.boosted(["ai_generated", {"name": "age", "images": ["person"]}])[2]
        self.assertEqual(again["evidence"], out["evidence"])
        reading = out["evidence"]["ai_generated"]["results"][0]
        self.assertEqual(list(reading), ["image", "generated", "confidence", "method", "top_generator", "c2pa_signer"])

    def test_top_generator_only_when_likely_generated(self):
        readings = [server.reading("ai_generated", "i%d" % i, "d%d" % i) for i in range(200)]
        for r in readings:
            self.assertEqual(r["top_generator"] is not None, r["generated"] > server.LIKELY_GENERATED, r)
            self.assertIsNone(r["c2pa_signer"])
            self.assertNotIn("c2pa", r)
        self.assertTrue(any(r["top_generator"] for r in readings) and any(r["top_generator"] is None for r in readings))

    def test_body_limit_25_mib(self):
        over = json.dumps({"state": "a" * server.MAX_BODY_BYTES, "questions": self.Q}).encode()
        st, hd, out = self.raw_call("POST", "/v1/decide", over, self.h("t13"))
        self.assertEqual(st, 413)
        self.assertEqual(json.loads(out)["detail"], {"error_type": "request_too_large_error", "message": server.BODY_TOO_LARGE})
        pad = server.MAX_BODY_BYTES - len(json.dumps({"state": "", "questions": self.Q}).encode())
        exact = json.dumps({"state": "a" * pad, "questions": self.Q}).encode()
        self.assertEqual(len(exact), server.MAX_BODY_BYTES)
        st, hd, out = self.raw_call("POST", "/v1/decide", exact, self.h("t13"))
        self.assertEqual((st, json.loads(out)["detail"]["error_type"]), (400, "max_tokens_exceeded"))

    def test_fallback_to_flash(self):
        q = {"questions": self.Q, "state": "s"}
        self.control("/_mock/workers", "t14", {"cold": ["decisionnode-moe"]})
        # x-decisionnode-requested-model: a family id or any alias of one is the family id,
        # a pinned id stays that pinned id.
        for model, requested in (("decisionnode-moe", "decisionnode-moe"), ("decisionnode-1.0", "decisionnode-moe"), ("decisionnode-latest", "decisionnode-moe")):
            st, hd, out = self.call("POST", "/v1/decide", dict(q, model=model), self.h("t14"))
            self.assertEqual((st, out["model"], hd["x-decisionnode-model"]), (200, server.FLASH, server.FLASH))
            self.assertEqual((hd["x-decisionnode-fallback"], hd["x-decisionnode-requested-model"]), (server.FLASH, requested), model)
        # A pinned id never falls back: answered by that version only, else the 529.
        st, hd, out = self.call("POST", "/v1/decide", dict(q, model=server.MOE), self.h("t14"))
        self.assertEqual((st, out["detail"]["error_type"]), (529, "overloaded_error"))
        self.assertNotIn("x-decisionnode-fallback", hd)
        st, hd, out = self.call("POST", "/v1/decide", dict(q, model="decisionnode-moe", fallback=False), self.h("t14"))
        self.assertEqual((st, out["detail"]["error_type"], hd["retry-after"], hd["retry-after-ms"]), (529, "overloaded_error", "1", "50"))
        self.assertNotIn("x-decisionnode-fallback", hd)
        st, hd, out = self.call("POST", "/v1/decide", dict(q, model="decisionnode-flash"), self.h("t14"))
        self.assertEqual(st, 200)
        self.assertNotIn("x-decisionnode-fallback", hd)
        self.control("/_mock/workers", "t15", {"cold": ["decisionnode-flash"]})
        self.assertEqual(self.call("POST", "/v1/decide", dict(q, model="decisionnode-flash"), self.h("t15"))[0], 529)
        st, hd, out = self.call("POST", "/v1/decide", dict(q, model="decisionnode-moe"), self.h("t16"))
        self.assertNotIn("x-decisionnode-fallback", hd)
        st, hd, out = self.call("POST", "/v1/decide", dict(q, fallback="no"), self.h("t16"))
        self.assertEqual((st, out["detail"][0]["type"], out["detail"][0]["loc"]), (422, "bool_type", ["body", "fallback"]))

    def test_image_id_at_most_64_characters(self):
        url = "https://files.example.com/a.jpg"
        st, hd, out = self.call("POST", "/v1/decide", {"images": [{"id": "é" * 64, "url": url}], "questions": self.Q}, self.h("t17"))
        self.assertEqual(st, 200)
        st, hd, out = self.call("POST", "/v1/decide", {"images": [{"id": "a", "url": url}, {"id": "é" * 65, "url": url}], "questions": self.Q}, self.h("t17"))
        self.assertEqual((st, out["detail"]), (400, {"error_type": "api_usage_error", "message": "Image 1: its id is longer than 64 characters."}))

    def test_cost_on_every_answer(self):
        # usage.cost: usd is model_usd plus boosters_usd, never a string.
        st, _, out = self.boosted(["age"], "t18")
        cost = out["usage"]["cost"]
        self.assertEqual(list(cost), ["usd", "model_usd", "boosters_usd", "free_tier"])
        self.assertEqual(cost["boosters_usd"], round(out["usage"]["boosters"] * server.BOOSTER_CALL_USD, 9))
        self.assertEqual(cost["model_usd"], round(out["usage"]["input_tokens"] * server.PRICE_PER_MILLION["decisionnode-moe"] / 1e6, 9))
        self.assertEqual(cost["usd"], round(cost["model_usd"] + cost["boosters_usd"], 9))
        self.assertIs(cost["free_tier"], False)
        st, _, flash = self.call("POST", "/v1/decide", {"model": "decisionnode-flash", "questions": self.Q}, self.h("t18"))
        self.assertEqual(flash["usage"]["cost"]["model_usd"], round(flash["usage"]["input_tokens"] * 0.021 / 1e6, 9))
        self.assertEqual(flash["usage"]["cost"]["boosters_usd"], 0)

    def test_no_boosters(self):
        for boosters in (None, []):
            st, hd, out = self.boosted(boosters)
            self.assertEqual(st, 200)
            self.assertNotIn("evidence", out)
            self.assertEqual(list(out["usage"]), ["input_tokens", "output_tokens", "cost"])
            self.assertNotIn("x-decisionnode-boosters", hd)

    def test_refusals_before_any_booster_runs(self):
        for boosters, status, error_type, reason in [
            ("age", 400, "api_usage_error", "boosters: must be a list of booster names or objects"),
            (["age", "ai_generated", "x", "y", "z"], 400, "api_usage_error", "boosters: at most 4 boosters, got 5"),
            (["age", {"name": "age"}], 400, "api_usage_error", "boosters: 'age' is named twice"),
            ([5], 400, "api_usage_error", "boosters: boosters[0] is neither a booster name nor an object with name"),
            ([{"images": ["person"]}], 400, "api_usage_error", "boosters: boosters[0] is neither a booster name nor an object with name"),
            ([{"name": "age", "options": {"k": 1}}], 400, "api_usage_error", "boosters[0]: \"options\": 'k' is not an option"),
            ([{"name": "age", "images": "person"}], 400, "api_usage_error", "boosters: boosters[0].images must be a list of image ids"),
            ([{"name": "age", "require": "yes"}], 400, "api_usage_error", "boosters: boosters[0].require must be true or false"),
            ([{"booster": "age"}], 400, "api_usage_error", "boosters: boosters[0] is neither a booster name nor an object with name"),
            ([{"name": "age", "priority": 1}], 400, "api_usage_error", "boosters: boosters[0]: 'priority' is not a key of a booster object"),
            (["example_booster"], 400, "booster_unknown", "'example_booster' is not a booster; the boosters are age, ai_generated, bynn_depth, bynn_segment, face_recognition, knowledge"),
            ([{"name": "age", "images": ["x9"]}], 400, "booster_input_mismatch", "Booster 'age': 'x9' is not an image of the request."),
        ]:
            st, hd, out = self.boosted(boosters, "t2")
            self.assertEqual((st, out["detail"]["error_type"], hd["x-decisionnode-error"]), (status, error_type, reason), boosters)
            self.assertNotIn("x-decisionnode-boosters", hd)
        st, hd, out = self.boosted(["age"], "t2", images=[])
        self.assertEqual(out["detail"], {"error_type": "booster_input_mismatch", "message": "The booster 'age' needs at least one image."})
        self.control("/_mock/boosters", "t3", {"disabled": ["age"]})
        st, hd, out = self.boosted(["ai_generated", "age"], "t3")
        self.assertEqual((st, out["detail"]["error_type"]), (403, "booster_not_enabled"))
        self.assertEqual(out["detail"]["message"], "The booster 'age' is not enabled for this workspace.")
        self.assertEqual(self.boosted(["ai_generated"], "t3")[0], 200)
        self.assertEqual(self.boosted(["age"], "t3-other")[0], 200)

    def test_statuses_and_required_boosters(self):
        self.control("/_mock/boosters", "t4", {"status": {"age": "timeout", "ai_generated": "error"}})
        st, hd, out = self.boosted(["age", "ai_generated"], "t4")
        self.assertEqual(st, 200)
        self.assertEqual(hd["x-decisionnode-boosters"], "age=timeout:800,ai_generated=error:5")
        unread = [{"image": "person"}, {"image": "poster"}]
        self.assertEqual([(e["status"], e["results"], e["billed_calls"]) for e in out["evidence"].values()], [("timeout", unread, 0), ("error", unread, 0)])
        self.assertEqual(out["usage"]["boosters"], 0)
        for boosters, words in (([{"name": "age", "require": True}], "did not answer in time."), (["age", {"name": "ai_generated", "require": True}], "failed.")):
            st, hd, out = self.boosted(boosters, "t4")
            self.assertEqual((st, out["detail"]["error_type"]), (529, "booster_unavailable"))
            self.assertTrue(out["detail"]["message"].endswith(words), out)
            self.assertEqual((hd["retry-after"], hd["retry-after-ms"]), ("1", "50"))
            self.assertIn("x-decisionnode-boosters", hd)
        st, hd, out = self.boosted([{"name": "age", "images": [], "require": True}], "t5")
        self.assertEqual((st, out["evidence"]["age"]["status"], hd["x-decisionnode-boosters"]), (200, "skipped", "age=skipped:0"))

    def test_header_on_later_refusals(self):
        bad = {"q": {"type": "choice", "instructions": "?", "criteria": {}}}
        st, hd, out = self.boosted(["age"], "t6", questions=bad)
        self.assertEqual(st, 400)
        self.assertEqual(hd["x-decisionnode-boosters"], "age=ok:212")
        st, hd, out = self.call("POST", "/v1/decide", {"model": "nope", "images": self.IMAGES, "questions": self.Q, "boosters": ["age"]}, self.h("t6"))
        self.assertEqual(st, 400)
        self.assertNotIn("x-decisionnode-boosters", hd)

    def test_sessions_take_no_boosters(self):
        st, hd, out = self.call("POST", "/v1/sessions", {"questions": self.Q, "boosters": ["age"]}, self.h("t7"))
        self.assertEqual((st, out["detail"]), (400, {"error_type": "api_usage_error", "message": "boosters are not available on sessions"}))

    def test_batch_lines_take_boosters(self):
        lines = [
            {"custom_id": "a", "images": self.IMAGES[:1], "questions": self.Q, "boosters": ["age"]},
            {"custom_id": "b", "images": self.IMAGES[:1], "questions": self.Q, "boosters": ["example_booster"]},
        ]
        h = dict(self.h("t8"), **{"Content-Type": "application/x-ndjson"})
        text = "\n".join(json.dumps(x) for x in lines)
        st, _, b = self.raw_call("POST", "/v1/batches", text.encode(), h)
        b = json.loads(b)
        self.assertEqual((st, b["counters"]["failed"]), (201, 1))
        self.call("POST", "/v1/batches/%s/finalize" % b["id"], None, self.h("t8"))
        self.call("GET", "/v1/batches/" + b["id"], headers=self.h("t8"))
        self.call("GET", "/v1/batches/" + b["id"], headers=self.h("t8"))
        st, _, raw = self.raw_call("GET", "/v1/batches/%s/results" % b["id"], None, self.h("t8"))
        out = [json.loads(x) for x in raw.decode().splitlines()]
        self.assertEqual([(x["custom_id"], x["status"]) for x in out], [("a", 200), ("b", 400)])
        self.assertEqual(out[0]["headers"]["x-decisionnode-boosters"], "age=ok:212")
        self.assertEqual(out[0]["body"]["usage"]["boosters"], 1)
        self.assertEqual(out[1]["body"]["detail"]["error_type"], "booster_unknown")

    def test_fine_tuned_names(self):
        q = {"questions": self.Q}
        for name, key, want in [
            ("decisionnode-moe@acme/fraud", self.ACME, "decisionnode-moe@acme/fraud:3"),
            ("decisionnode-moe@acme/fraud:3", self.ACME, "decisionnode-moe@acme/fraud:3"),
            ("decisionnode-1.0@acme/fraud", self.ACME, "decisionnode-moe@acme/fraud:3"),
            ("decisionnode-1.0@acme/fraud:3", self.ACME, "decisionnode-moe@acme/fraud:3"),
            ("decisionnode-moe@acme/fraud:2", self.ACME, None),
            ("decisionnode-flash@acme/triage:1", self.ACME, None),
            ("decisionnode-flash@acme/triage", self.ACME, None),
            ("decisionnode-moe@acme/fraud", KEY, None),
            ("decisionnode-moe@acme/fraud", "dn_test_ws-other", None),
            ("decisionnode-latest@acme/fraud", self.ACME, None),
            ("decisionnode-moe@acme/fraud:03", self.ACME, None),
            ("decisionnode-moe@acme/Fraud", self.ACME, None),
        ]:
            st, hd, out = self.call("POST", "/v1/decide", dict(q, model=name), self.h("t9", key=key))
            if want is None:
                self.assertEqual((st, out["detail"]), (400, {"error_type": "api_usage_error", "message": "Unknown model: " + name}), name)
            else:
                self.assertEqual((st, out["model"], hd["x-decisionnode-model"]), (200, want, want), name)

    def test_models_list_fine_tuned_entries(self):
        mine = self.call("GET", "/v1/models", headers=self.h("t10", key=self.ACME))[2]["models"]
        others = self.call("GET", "/v1/models", headers=self.h("t10"))[2]["models"]
        self.assertEqual(len(others), 4)
        self.assertTrue(all("fine_tuned" not in m for m in others))
        self.assertEqual(mine[:4], others)
        extra = {m["name"]: m for m in mine[4:]}
        self.assertEqual(sorted(extra), ["decisionnode-flash@acme/triage:1", "decisionnode-moe@acme/fraud", "decisionnode-moe@acme/fraud:3"])
        plain = extra["decisionnode-moe@acme/fraud"]
        self.assertEqual(
            (plain["fine_tuned"], plain["base"], plain["family"], plain["adapter_version"], plain["status"], plain["version"]),
            (True, "decisionnode-moe", "decisionnode-moe", 3, "deployed", "decisionnode-moe@acme/fraud:3"),
        )
        self.assertEqual(extra["decisionnode-flash@acme/triage:1"]["status"], "ready")
        flash = next(m for m in others if m["name"] == "decisionnode-flash")
        # Its base's limits; its own prices (the base's plus the premium) and no batch factor.
        priced = ("batch_price_factor", "price_per_million_input", "price_per_million_output", "price_per_million_image", "price_per_million_video")
        base = {k: v for k, v in flash.items() if k not in ("name", "description", "release_date", "version", "alias", "current_version") + priced}
        ready = extra["decisionnode-flash@acme/triage:1"]
        self.assertEqual({k: ready[k] for k in base}, base)
        self.assertNotIn("batch_price_factor", ready)
        self.assertEqual((ready["price_per_million_input"], ready["price_per_million_output"]), (0.041, 0))

    def test_boosters_scenario_settings(self):
        scenario = {"name": "t-boosters-off", "boosters": {"disabled": ["ai_generated"]}}
        self.assertEqual(self.call("POST", "/_mock/scenarios", scenario, {"Content-Type": "application/json"})[0], 200)
        st, _, out = self.boosted(["ai_generated"], "t11")
        self.assertEqual(st, 200)
        st, _, out = self.call("POST", "/v1/decide", {"images": self.IMAGES, "questions": self.Q, "boosters": ["ai_generated"]}, self.h("t12", "t-boosters-off"))
        self.assertEqual((st, out["detail"]["error_type"]), (403, "booster_not_enabled"))



class TestSuite121(Spec11):
    """Suite 1.21: Depth Vision (bynn_depth) and Segment Vision (bynn_segment), their
    options refused in the API's words and their readings."""

    IMAGES, Q, boosted = TestSuite111.IMAGES, TestSuite111.Q, TestSuite111.boosted

    def refused(self, boosters, session="v1"):
        st, hd, out = self.boosted(boosters, session)
        self.assertEqual((st, out["detail"]), (400, {"error_type": "api_usage_error", "message": "Invalid request."}), boosters)
        self.assertNotIn("x-decisionnode-boosters", hd)
        return hd["x-decisionnode-error"]

    def test_option_refusals_in_the_apis_words(self):
        phrases = "\"options\": 'prompt' must be a string, or an array of 1 to 4 strings, each of at most 120 characters"
        for boosters, reason in [
            ([{"name": "bynn_depth", "options": {"grid_size": 32}}], "\"options\": 'grid_size' must be an integer from 2 to 16"),
            ([{"name": "bynn_depth", "options": {"grid_size": 1}}], "\"options\": 'grid_size' must be an integer from 2 to 16"),
            ([{"name": "bynn_depth", "options": {"grid_size": 8.0}}], "\"options\": 'grid_size' must be an integer from 2 to 16"),
            ([{"name": "bynn_depth", "options": {"grid_size": True}}], "\"options\": 'grid_size' must be an integer from 2 to 16"),
            ([{"name": "bynn_depth", "options": {"prompt": "door"}}], "\"options\": 'prompt' is not an option"),
            ([{"name": "bynn_depth", "options": {"depth": True}}], "\"options\": 'depth' is not an option"),
            ([{"name": "bynn_depth", "options": {"scale": 2}}], "\"options\": 'scale' is not an option"),
            (["bynn_segment"], "\"options\": 'prompt' is required"),
            ([{"name": "bynn_segment"}], "\"options\": 'prompt' is required"),
            ([{"name": "bynn_segment", "options": {"max_instances": 4}}], "\"options\": 'prompt' is required"),
            ([{"name": "bynn_segment", "options": {"prompt": " | \t"}}], "\"options\": 'prompt' must name something to find"),
            ([{"name": "bynn_segment", "options": {"prompt": ["", "|", "\u200e"]}}], "\"options\": 'prompt' must name something to find"),
            ([{"name": "bynn_segment", "options": {"prompt": []}}], phrases),
            ([{"name": "bynn_segment", "options": {"prompt": ["a", "b", "c", "d", "e"]}}], phrases),
            ([{"name": "bynn_segment", "options": {"prompt": "x" * 121}}], phrases),
            ([{"name": "bynn_segment", "options": {"prompt": "x" * 2001}}], phrases),
            ([{"name": "bynn_segment", "options": {"prompt": ["gate", 5]}}], phrases),
            ([{"name": "bynn_segment", "options": {"prompt": 5}}], phrases),
            ([{"name": "bynn_segment", "options": {"prompt": None}}], phrases),
            ([{"name": "bynn_segment", "options": {"prompt": "gate", "max_instances": 17}}], "\"options\": 'max_instances' must be an integer from 1 to 16"),
            ([{"name": "bynn_segment", "options": {"prompt": "gate", "min_score": 1.5}}], "\"options\": 'min_score' must be a number from 0 to 1"),
            ([{"name": "bynn_segment", "options": {"prompt": "gate", "polygon_points": 2}}], "\"options\": 'polygon_points' must be an integer from 3 to 64"),
            ([{"name": "bynn_segment", "options": {"prompt": "gate", "depth": "no"}}], "\"options\": 'depth' must be true or false"),
            ([{"name": "bynn_segment", "options": {"prompt": "gate", "grid_size": 8}}], "\"options\": 'grid_size' is not an option"),
            ([{"name": "bynn_segment", "options": {"prompt": "gate", "threshold": 0.5}}], "\"options\": 'threshold' is not an option"),
            # Each key in the order sent, the first problem the reason; a key's own value
            # before what the booster takes; the phrases last.
            ([{"name": "bynn_segment", "options": {"min_score": 2, "max_instances": 0, "prompt": ""}}], "\"options\": 'min_score' must be a number from 0 to 1"),
            ([{"name": "bynn_segment", "options": {"max_instances": 0, "min_score": 2}}], "\"options\": 'max_instances' must be an integer from 1 to 16"),
            ([{"name": "bynn_segment", "options": {"prompt": "", "grid_size": 8}}], "\"options\": 'grid_size' is not an option"),
            ([{"name": "bynn_depth", "options": {"prompt": "a", "grid_size": 40}}], "\"options\": 'grid_size' must be an integer from 2 to 16"),
            ([{"name": "bynn_depth", "options": {"k" * 70: 1}}], "\"options\": '%s' is not an option" % ("k" * 64)),
            (["face_recognition"], "\"options\": 'collection' is required"),
            ([{"name": "face_recognition", "options": {"collection": 7}}], "\"options\": 'collection' must be a string"),
            ([{"name": "face_recognition", "options": {"collection": "celebrities", "threshold": 2}}], "\"options\": 'threshold' must be a number from 0 to 1"),
            ([{"name": "age", "options": {"collection": "celebrities"}}], "\"options\": 'collection' is not an option"),
            ([{"name": "ai_generated", "options": {"prompt": "x"}}], "\"options\": 'prompt' is not an option"),
            ([{"name": "age", "options": "x"}], "\"options\" must be an object"),
        ]:
            self.assertEqual(self.refused(boosters), "boosters[0]: " + reason, boosters)
        # The entry's index, and the entries before it judged first.
        self.assertEqual(self.refused(["age", {"name": "bynn_depth", "options": {"grid_size": 0}}]), "boosters[1]: \"options\": 'grid_size' must be an integer from 2 to 16")
        self.assertEqual(self.refused(["bynn_segment", "example_booster"]), "boosters[0]: \"options\": 'prompt' is required")
        # A name that is no booster takes any known key: it is booster_unknown.
        st, _, out = self.boosted([{"name": "example_booster", "options": {"prompt": 5, "grid_size": 3}}], "v1")
        self.assertEqual((st, out["detail"]["error_type"]), (400, "booster_unknown"))

    def test_phrases_cleaned_as_the_api_cleans_them(self):
        prompt = ["  Gate\u200f  | frame ", "gate frame", "GATE   FRAME", "pad\x07", "x" * 120]
        st, _, out = self.boosted([{"name": "bynn_segment", "options": {"prompt": prompt[:4]}}], "v2")
        self.assertEqual(st, 200, out)
        self.assertEqual(out["evidence"]["bynn_segment"]["results"][0]["prompts"], ["Gate frame", "pad"])
        st, _, out = self.boosted([{"name": "bynn_segment", "options": {"prompt": prompt[4]}}], "v2")
        self.assertEqual(out["evidence"]["bynn_segment"]["results"][0]["prompts"], ["x" * 120])

    def test_depth_reading(self):
        for options, n in ((None, 8), ({"grid_size": 2}, 2), ({"grid_size": 16}, 16)):
            entry = {"name": "bynn_depth"} if options is None else {"name": "bynn_depth", "options": options}
            st, hd, out = self.boosted([entry], "v3")
            self.assertEqual(st, 200, out)
            self.assertEqual(hd["x-decisionnode-boosters"], "bynn_depth=ok:24")
            e = out["evidence"]["bynn_depth"]
            self.assertEqual((e["version"], e["status"], e["billed_calls"]), ("bynn_depth-v1", "ok", 2))
            for r, iid in zip(e["results"], ("person", "poster")):
                self.assertEqual(list(r), ["image", "ahead", "directions", "ground", "grid_rows", "grid_cols", "grid", "depth_min", "depth_max", "depth_median", "width", "height"])
                self.assertEqual((r["image"], r["grid_rows"], r["grid_cols"], r["width"], r["height"]), (iid, n, n, 1920, 1080))
                self.assertEqual([len(row) for row in r["grid"]], [n] * n)
                cells = [v for row in r["grid"] for v in row]
                self.assertEqual((r["depth_min"], r["depth_max"]), (min(cells), max(cells)))
                self.assertTrue(r["depth_min"] <= r["depth_median"] <= r["depth_max"])
                self.assertEqual(len(r["directions"]), 7)
                for d in r["directions"] + [r["ahead"]]:
                    self.assertTrue(d is None or (isinstance(d, float) and 0 < d <= 15), d)
                self.assertIsInstance(r["ground"], bool)
            self.assertEqual(out["usage"]["boosters"], 2)
            self.assertEqual(out["usage"]["cost"]["boosters_usd"], 0.002)
        # The same request reads the same.
        self.assertEqual(self.boosted(["bynn_depth"], "v3")[2]["evidence"], self.boosted(["bynn_depth"], "v3")[2]["evidence"])

    def test_segment_reading(self):
        keys = ["label", "score", "box", "polygon", "area", "centroid", "opening", "depth", "size_m"]
        options = {"prompt": ["gate", "landing pad", "person", "door"], "max_instances": 3, "min_score": 0.5, "polygon_points": 6}
        st, hd, out = self.boosted([{"name": "bynn_segment", "options": options}, "age"], "v4")
        self.assertEqual(st, 200, out)
        self.assertEqual(hd["x-decisionnode-boosters"], "bynn_segment=ok:75,age=ok:212")
        e = out["evidence"]["bynn_segment"]
        self.assertEqual((e["version"], e["status"], e["billed_calls"]), ("bynn_segment-v1", "ok", 2))
        found = 0
        for r in e["results"]:
            self.assertEqual(list(r), ["image", "width", "height", "prompts", "instances", "depth_source", "assumed_hfov_deg"])
            self.assertEqual((r["prompts"], r["depth_source"], r["assumed_hfov_deg"]), (options["prompt"], "bynn_depth", 65.0))
            self.assertLessEqual(len(r["instances"]), 3)
            scores = [i["score"] for i in r["instances"]]
            self.assertEqual(scores, sorted(scores, reverse=True))
            for i in r["instances"]:
                found += 1
                self.assertEqual(list(i), keys)
                self.assertIn(i["label"], options["prompt"])
                self.assertTrue(0.5 <= i["score"] <= 1)
                self.assertEqual(len(i["polygon"]), 6)
                x1, y1, x2, y2 = i["box"]
                self.assertTrue(0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1, i["box"])
                self.assertEqual(list(i["depth"]), ["median", "opening_median", "opening_rim_median"])
                self.assertEqual(list(i["size_m"]), ["width", "height", "opening"])
                if i["opening"] is None:
                    self.assertEqual((i["depth"]["opening_median"], i["size_m"]["opening"]), (None, None))
                else:
                    self.assertEqual(list(i["opening"]), ["polygon", "centroid", "width", "height", "offset", "area"])
                    # The opening's centroid less the centre of the match's box, as the model writes it.
                    (ox, oy), box = i["opening"]["centroid"], i["box"]
                    self.assertEqual(i["opening"]["offset"], [round(ox - (box[0] + box[2]) / 2, 4), round(oy - (box[1] + box[3]) / 2, 4)])
                    self.assertNotEqual(i["opening"]["offset"], [0, 0])
        self.assertGreater(found, 0)
        # Free: two pictures read are two calls, at no cost; age's are priced.
        self.assertEqual(out["usage"]["boosters"], 4)
        self.assertEqual(out["usage"]["cost"]["boosters_usd"], 0.004)
        # Depth off: no distances, no sizes, no depth_source; the defaults otherwise.
        st, _, out = self.boosted([{"name": "bynn_segment", "options": {"prompt": "gate", "depth": False}}], "v4")
        for r in out["evidence"]["bynn_segment"]["results"]:
            self.assertIsNone(r["depth_source"])
            self.assertLessEqual(len(r["instances"]), 8)
            for i in r["instances"]:
                self.assertEqual((i["depth"], i["size_m"], len(i["polygon"])), (None, None, 24))
                self.assertGreaterEqual(i["score"], 0.25)
        self.assertEqual(out["usage"]["cost"]["boosters_usd"], 0)

    def test_segment_unavailable_when_required(self):
        self.control("/_mock/boosters", "v5", {"status": {"bynn_segment": "error"}})
        st, hd, out = self.boosted([{"name": "bynn_segment", "options": {"prompt": "gate"}, "require": True}], "v5")
        self.assertEqual((st, out["detail"]), (529, {"error_type": "booster_unavailable", "message": "The booster 'bynn_segment' failed."}))
        self.assertEqual(hd["retry-after"], "1")
        st, hd, out = self.boosted([{"name": "bynn_segment", "options": {"prompt": "gate"}}], "v5")
        self.assertEqual((st, out["evidence"]["bynn_segment"]["results"], out["usage"]["boosters"]), (200, [{"image": "person"}, {"image": "poster"}], 0))


class TestSeveralPlaces(Spec11):
    """Max on a point or box question, the list shape, the 422s,
    the capability at GET /v1/models and the API's not_supported."""

    LOT = {"id": "lot", "url": "https://example.com/lot.jpg"}

    def decide_in(self, session, body, scenario=None):
        return self.call("POST", "/v1/decide", body, self.h(session, scenario))

    def test_list_shape(self):
        q = {"cars": {"type": "box", "instructions": "Every car.", "max": 8}, "people": {"type": "point", "instructions": "Every person in the picture.", "units": "normalized", "max": 6}, "one": {"type": "box", "instructions": "?"}}
        st, _, out = self.decide_in("p1", {"state": "", "images": [self.LOT], "questions": q})
        self.assertEqual(st, 200, out)
        cars, people, one = out["answers"]["cars"], out["answers"]["people"], out["answers"]["one"]
        self.assertEqual(list(cars), ["type", "count", "present", "complete", "boxes", "image", "width", "height"])
        self.assertEqual(list(people), ["type", "count", "present", "complete", "points", "image", "width", "height"])
        self.assertNotIn("x1", cars)
        self.assertIn("x1", one)
        for answer, key, most in ((cars, "boxes", 8), (people, "points", 6)):
            self.assertTrue(0 <= answer["count"] <= most)
            self.assertEqual(len(answer[key]), answer["count"])
        centres = [((b["x1"] + b["x2"]) / 2, (b["y1"] + b["y2"]) / 2) for b in cars["boxes"]]
        self.assertEqual(centres, sorted(centres))
        self.assertTrue(all(isinstance(b["x1"], int) for b in cars["boxes"]))
        self.assertTrue(all(0 <= p["x"] <= 1 for p in people["points"]))

    def test_max_entries(self):
        loc = ["body", "questions", "cars", "box", "max"]
        for most, kind, ctx in ((1, "greater_than_equal", {"ge": 2}), (17, "less_than_equal", {"le": 16}), (2.5, "int_type", None), ("8", "int_type", None), (True, "int_type", None)):
            st, _, out = self.decide_in("p2", {"state": "", "images": [self.LOT], "questions": {"cars": {"type": "box", "instructions": "?", "max": most}}})
            self.assertEqual(st, 422, most)
            entry = out["detail"][0]
            self.assertEqual((entry["type"], entry["loc"], entry["input"], entry.get("ctx")), (kind, loc, most, ctx))
        st, _, _ = self.decide_in("p2", {"state": "", "images": [self.LOT], "questions": {"cars": {"type": "box", "instructions": "?", "max": None}}})
        self.assertEqual(st, 200)

    def test_capability_off(self):
        scenario = {"name": "t-places-off", "places": {"max_places": 0}}
        self.assertEqual(self.call("POST", "/_mock/scenarios", scenario, {"Content-Type": "application/json"})[0], 200)
        on = self.call("GET", "/v1/models", headers=self.h("p3"))[2]["models"]
        self.assertTrue(all(m["places"] == {"max_places": 16} for m in on))
        off = self.call("GET", "/v1/models", headers=self.h("p4", "t-places-off"))[2]["models"]
        self.assertTrue(all("places" not in m for m in off))
        st, _, out = self.decide_in("p4", {"state": "", "images": [self.LOT], "questions": {"cars": {"type": "box", "instructions": "?", "max": 20}, "one": {"type": "box", "instructions": "?"}}}, "t-places-off")
        self.assertEqual(st, 422)
        self.assertEqual(out["detail"], [{"type": "not_supported", "loc": ["body", "questions", "cars", "box", "max"], "msg": "Several places are not supported by this model version", "input": 20}])
        st, _, out = self.decide_in("p4", {"state": "", "images": [self.LOT], "questions": {"one": {"type": "box", "instructions": "?"}}}, "t-places-off")
        self.assertEqual(st, 200, out)



class TestCollections(Spec11):
    """The collections calls, each workspace's own, and the public ones."""

    ACME = "dn_test_ws-acme"
    BADGE = {"id": "badge", "url": "https://example.com/badge.jpg"}

    def test_lifecycle_and_isolation(self):
        st, _, col = self.call("POST", "/v1/collections", {"type": "face", "name": "Staff"}, self.h("c1"))
        self.assertEqual((st, col["items"], col["deletion"], col["public"]), (201, 0, None, False))
        self.assertRegex(col["id"], r"^col_[0-9a-f]{32}$")
        st, _, item = self.call("POST", "/v1/collections/%s/items" % col["id"], {"image": self.BADGE, "reference": "emp-1042"}, self.h("c1"))
        self.assertEqual(st, 201, item)
        self.assertEqual((item["collection"], item["reference"], item["label"], item["usage"]["enrollments"]), (col["id"], "emp-1042", None, 1))
        self.assertTrue(1 <= item["faces_found"] <= 3)
        # Another workspace sees nothing of it: the same 404 as an unknown id.
        st, _, out = self.call("GET", "/v1/collections/%s" % col["id"], headers=self.h("c1", key=self.ACME))
        self.assertEqual((st, out["detail"]["error_type"]), (404, "collection_not_found"))
        st, _, out = self.call("GET", "/v1/collections", headers=self.h("c1", key=self.ACME))
        self.assertEqual([c["id"] for c in out["collections"]], ["celebrities"])
        st, _, page = self.call("GET", "/v1/collections/%s/items?limit=1" % col["id"], headers=self.h("c1"))
        self.assertEqual((page["total"], page["limit"], page["next_offset"]), (1, 1, None))
        st, _, out = self.call("DELETE", "/v1/collections/%s/items/itm_x" % col["id"], headers=self.h("c1"))
        self.assertEqual((st, out["detail"]["error_type"]), (404, "item_not_found"))
        st, _, out = self.call("DELETE", "/v1/collections/%s" % col["id"], headers=self.h("c1"))
        self.assertEqual((st, out), (200, {"id": col["id"], "deleted": True}))

    def test_validation(self):
        st, _, out = self.call("POST", "/v1/collections", {"type": "face", "name": "x" * 81}, self.h("c2"))
        self.assertEqual((st, out["detail"][0]["type"]), (422, "string_too_long"))
        self.assertEqual(out["detail"][0]["input"], "x" * 64 + "...")
        self.call("POST", "/v1/collections", {"type": "face", "name": "Staff"}, self.h("c2"))
        st, _, out = self.call("POST", "/v1/collections", {"type": "face", "name": "Staff"}, self.h("c2"))
        self.assertEqual((st, out["detail"][0]["type"]), (422, "name_taken"))
        st, _, out = self.call("POST", "/v1/collections/col_nope/items", {"reference": "emp-1", "label": ""}, self.h("c2"))
        self.assertEqual((st, out["detail"]), (422, [
            {"type": "missing", "loc": ["body", "image"], "msg": "Field required", "input": None},
            {"type": "string_too_short", "loc": ["body", "label"], "msg": "String should have at least 1 character", "input": "", "ctx": {"min_length": 1}},
        ]))
        col = self.call("POST", "/v1/collections", {"type": "face", "name": "Other"}, self.h("c2"))[2]
        st, _, out = self.call("POST", "/v1/collections/%s/items" % col["id"], {"image": self.BADGE, "reference": "bad ref!"}, self.h("c2"))
        self.assertEqual((st, out["detail"][0]["type"]), (422, "string_pattern_mismatch"))
        scenario = {"name": "t-no-face", "collections": {"faces": 0}}
        self.assertEqual(self.call("POST", "/_mock/scenarios", scenario, {"Content-Type": "application/json"})[0], 200)
        st, _, out = self.call("POST", "/v1/collections/%s/items" % col["id"], {"image": self.BADGE, "reference": "emp-1"}, self.h("c2", "t-no-face"))
        self.assertEqual((st, out["detail"]), (422, [{"type": "no_face", "loc": ["body", "image"], "msg": "No face was found in the image.", "input": "badge"}]))
        for method, path in (("DELETE", "/v1/collections/celebrities"), ("GET", "/v1/collections/celebrities/items"), ("POST", "/v1/collections/celebrities/items")):
            st, _, out = self.call(method, path, {"image": self.BADGE, "reference": "emp-1"} if method == "POST" else None, self.h("c2"))
            self.assertEqual((st, out["detail"]["error_type"]), (403, "collection_read_only"), path)


    def test_face_recognition(self):
        gate = {"id": "lobby", "url": "https://files.example.com/lobby/cam-6.jpg"}
        q = {"q": {"type": "truth", "instructions": "?"}}
        col = self.call("POST", "/v1/collections", {"type": "face", "name": "Staff"}, self.h("c3"))[2]
        self.call("POST", "/v1/collections/%s/items" % col["id"], {"image": self.BADGE, "reference": "emp-1042", "label": "Reception"}, self.h("c3"))

        def decide(options, session="c3", scenario=None):
            return self.call("POST", "/v1/decide", {"images": [gate], "questions": q, "boosters": [{"name": "face_recognition", "options": options}]}, self.h(session, scenario))

        st, _, out = decide({"collection": col["id"]})
        self.assertEqual(st, 200, out)
        reading = out["evidence"]["face_recognition"]["results"][0]
        self.assertEqual(reading["faces"], 2)
        self.assertEqual([m["reference"] for m in reading["matches"]], ["emp-1042", "emp-1042"])
        self.assertEqual(reading["matches"][0]["label"], "Reception")
        scores = [m["score"] for m in reading["matches"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        st, _, out = decide({"collection": "celebrities", "threshold": 0.6})
        matches = out["evidence"]["face_recognition"]["results"][0]["matches"]
        self.assertEqual(len(matches), 1)
        self.assertIn("name", matches[0])
        for options, status, error_type in (({}, 400, "api_usage_error"), ({"collection": "celebrities", "threshold": 2}, 400, "api_usage_error"),
                                            ({"collection": "wanted-persons"}, 403, "collection_not_enabled"), ({"collection": "col_nope"}, 404, "collection_not_found")):
            st, _, out = decide(options)
            self.assertEqual((st, out["detail"]["error_type"]), (status, error_type), options)
        # wanted-persons is not offered: absent from the list, a read of it unknown.
        st, _, out = self.call("GET", "/v1/collections", headers=self.h("c3"))
        self.assertNotIn("wanted-persons", [c["id"] for c in out["collections"]])
        for path in ("/v1/collections/wanted-persons", "/v1/collections/wanted-persons/items"):
            st, _, out = self.call("GET", path, headers=self.h("c3"))
            self.assertEqual((st, out["detail"]["error_type"]), (404, "collection_not_found"), path)
        # Every other booster still takes no option.
        st, _, out = self.call("POST", "/v1/decide", {"images": [gate], "questions": q, "boosters": [{"name": "age", "options": {"collection": "x"}}]}, self.h("c3"))
        self.assertEqual((st, out["detail"]["error_type"]), (400, "api_usage_error"))


    def test_items_found_edited_and_described(self):
        """Suite 1.15: an item's metadata, its edit, and items found by reference."""
        h = self.h("c8")
        col = self.call("POST", "/v1/collections", {"type": "face", "name": "Staff"}, h)[2]
        items = "/v1/collections/%s/items" % col["id"]
        st, _, first = self.call("POST", items, {"image": self.BADGE, "reference": "emp-1042", "metadata": {"department": "Reception", "level": 2}}, h)
        self.assertEqual((st, first["metadata"]), (201, {"department": "Reception", "level": 2}))
        self.call("POST", items, {"image": self.BADGE, "reference": "emp-1043"}, h)
        self.call("POST", items, {"image": self.BADGE, "reference": "vis-0001"}, h)
        st, _, page = self.call("GET", items + "?reference_prefix=emp-", headers=h)
        self.assertEqual(([i["reference"] for i in page["items"]], page["total"], page["total_capped"], page["reference"], page["reference_prefix"]),
                         (["emp-1042", "emp-1043"], 2, False, None, "emp-"))
        st, _, page = self.call("GET", items, headers=h)
        self.assertEqual(([i["reference"] for i in page["items"]], page["items"][2]["metadata"], page["items"][0]["metadata"]),
                         (["vis-0001", "emp-1043", "emp-1042"], {"department": "Reception", "level": 2}, {}))
        for query, message in (("?reference=a&reference_prefix=b", "Send query parameter reference or reference_prefix, not both."),
                               ("?reference=emp%201042", "Query parameter reference must be 1 to 64 characters of A-Z, a-z, 0-9, _, ., : and -, got emp 1042.")):
            st, _, out = self.call("GET", items + query, headers=h)
            self.assertEqual((st, out["detail"]["message"]), (400, message), query)
        # An edit: the fields sent, label null clearing it, metadata replaced whole.
        st, _, edited = self.call("PATCH", "%s/%s" % (items, first["id"]), {"reference": "emp-2042", "label": "Anna", "metadata": {}}, h)
        self.assertEqual((st, edited["reference"], edited["label"], edited["metadata"]), (200, "emp-2042", "Anna", {}))
        st, _, edited = self.call("PATCH", "%s/%s" % (items, first["id"]), {"label": None}, h)
        self.assertEqual((edited["reference"], edited["label"]), ("emp-2042", None))
        st, _, out = self.call("PATCH", "%s/%s" % (items, first["id"]), {}, h)
        self.assertEqual((st, out["detail"]), (422, [{"type": "missing", "loc": ["body"], "msg": "Send reference, label, metadata or any of them", "input": None}]))
        st, _, out = self.call("PATCH", "%s/itm_nope" % items, {"label": "x"}, h)
        self.assertEqual((st, out["detail"]["error_type"]), (404, "item_not_found"))
        st, _, out = self.call("PATCH", "/v1/collections/celebrities/items/itm_x", {"label": "x"}, h)
        self.assertEqual((st, out["detail"]["error_type"]), (403, "collection_read_only"))
        # Metadata as the API checks it.
        for metadata, loc, msg in (([1], ["body", "metadata"], "Metadata is an object of keys and values."),
                                   ({"k%d" % n: n for n in range(21)}, ["body", "metadata"], "Metadata holds at most 20 keys."),
                                   ({"Dept": 1}, ["body", "metadata"], 'A metadata key is 1 to 40 characters of a-z, 0-9 and _: "Dept".'),
                                   ({"note": "x" * 501}, ["body", "metadata", "note"], "A metadata text is at most 500 characters."),
                                   ({"note": "a\u200bb"}, ["body", "metadata", "note"], "A metadata text holds no control or invisible characters."),
                                   ({"n": 1e15}, ["body", "metadata", "n"], "A metadata number is under 10^15 in size, and 0 or at least 10^-6."),
                                   ({"n": None}, ["body", "metadata", "n"], "A metadata value is a text, a number or true or false."),
                                   ({"k%d" % n: "x" * 250 for n in range(20)}, ["body", "metadata"], "Metadata is at most 4096 bytes as JSON.")):
            st, _, out = self.call("POST", items, {"image": self.BADGE, "reference": "emp-9", "metadata": metadata}, h)
            self.assertEqual((st, [(e["loc"], e["msg"]) for e in out["detail"]]), (422, [(loc, msg)]), msg)

    def test_enrollment_unavailable(self):
        """Suite 1.15: an enrollment's 529 says why in detail.details.reason."""
        for name, reason, retry in (("t-unpriced", "unpriced", None), ("t-no-face-model", "face_model_unavailable", "1")):
            scenario = {"name": name, "collections": {"enrollment": reason}}
            self.assertEqual(self.call("POST", "/_mock/scenarios", scenario, {"Content-Type": "application/json"})[0], 200)
            h = self.h("c9-" + name, name)
            col = self.call("POST", "/v1/collections", {"type": "face", "name": "Staff"}, h)[2]
            st, hd, out = self.call("POST", "/v1/collections/%s/items" % col["id"], {"image": self.BADGE, "reference": "emp-1"}, h)
            self.assertEqual((st, out["detail"]["error_type"], out["detail"]["details"], hd.get("retry-after")),
                             (529, "overloaded_error", {"reason": "enrollment_unpriced" if reason == "unpriced" else reason}, retry))

    def test_face_recognition_names_and_metadata(self):
        """Suite 1.16: the mock sends 1.16's fields: collection_name on an own
        collection's result and on every match, an own item's metadata on its match."""
        h = self.h("c11")
        col = self.call("POST", "/v1/collections", {"type": "face", "name": "  Front desk   staff "}, h)[2]
        self.call("POST", "/v1/collections/%s/items" % col["id"], {"image": self.BADGE, "reference": "emp-1", "metadata": {"department": "Reception"}}, h)
        q = {"ok": {"type": "truth", "instructions": "Is someone there?"}}

        def search(collection):
            body = {"images": [self.BADGE], "questions": q, "boosters": [{"name": "face_recognition", "options": {"collection": collection, "threshold": 0}}]}
            st, _, out = self.call("POST", "/v1/decide", body, h)
            self.assertEqual(st, 200)
            return out["evidence"]["face_recognition"]["results"][0]

        own = search(col["id"])
        self.assertEqual(own["collection_name"], "Front desk staff")
        self.assertTrue(own["matches"])
        for m in own["matches"]:
            self.assertEqual((m["collection_name"], m["metadata"]), ("Front desk staff", {"department": "Reception"}))
        st, _, out = self.call("POST", "/v1/collections", {"type": "face", "name": "Front\tdesk"}, h)
        self.assertEqual((st, out["detail"][0]["type"], out["detail"][0]["msg"]), (422, "string_pattern_mismatch", "A name holds no control or invisible characters"))
        st, _, out = self.call("POST", "/v1/collections/%s/items" % col["id"], {"image": self.BADGE, "reference": "emp-2", "label": "Anna\u200b"}, h)
        self.assertEqual((st, out["detail"][0]["loc"], out["detail"][0]["msg"]), (422, ["body", "label"], "A label holds no control or invisible characters"))
        public = search("celebrities")
        self.assertNotIn("collection_name", public)
        self.assertTrue(public["matches"] and all(m["collection_name"] == "celebrities" and "metadata" not in m for m in public["matches"]))

    def test_decide_key(self):
        """Suite 1.15: a decide key decides and lists the models, nothing else."""
        def h(key):
            return {"Authorization": "Bearer " + key, "Content-Type": "application/json", "x-mock-session": "c10"}
        decide_key, searching = "dn_test_scope-decide", "dn_test_scope-decide_may-search"
        self.assertEqual(self.call("GET", "/v1/models", headers=h(decide_key))[0], 200)
        for method, path, body in (("GET", "/v1/collections", None), ("POST", "/v1/batches", {}), ("POST", "/v1/uploads", {}), ("POST", "/v1/sessions", {})):
            st, hd, out = self.call(method, path, body, h(decide_key))
            self.assertEqual((st, out["detail"]), (403, {"error_type": "key_scope_denied", "message": "This key may only make decisions."}), path)
        q = {"ok": {"type": "truth", "instructions": "Is it fine?"}}
        st, _, out = self.call("POST", "/v1/decide", {"images": [self.BADGE], "questions": q}, h(decide_key))
        self.assertEqual(st, 200)
        search = [{"name": "face_recognition", "options": {"collection": "celebrities"}}]
        st, _, out = self.call("POST", "/v1/decide", {"images": [self.BADGE], "questions": q, "boosters": search}, h(decide_key))
        self.assertEqual((st, out["detail"]), (403, {"error_type": "key_scope_denied", "message": "This key may not search collections."}))
        st, _, out = self.call("POST", "/v1/decide", {"images": [self.BADGE], "questions": q, "boosters": search}, h(searching))
        self.assertEqual(st, 200)

    def test_idempotency_key(self):
        def post(path, body, key, session="c6", scenario=None):
            h = self.h(session, scenario)
            h["Idempotency-Key"] = key
            return self.call("POST", path, body, h)

        st, hd, first = post("/v1/collections", {"type": "face", "name": "Staff"}, "k-1")
        self.assertEqual(st, 201)
        st, hd, again = post("/v1/collections", {"type": "face", "name": "Staff"}, "k-1")
        self.assertEqual((st, again, hd.get("x-decisionnode-idempotent-replay")), (201, first, "true"))
        st, _, out = post("/v1/collections", {"type": "face", "name": "Other"}, "k-1")
        self.assertEqual((st, out["detail"]["error_type"]), (409, "idempotency_key_reused"))
        item = {"image": self.BADGE, "reference": "emp-1"}
        st, _, out = post("/v1/collections/%s/items" % first["id"], item, "k-1")
        self.assertEqual((st, out["detail"]["error_type"]), (409, "idempotency_key_reused"))
        # A refusal that changed nothing lets the key go: the retry runs again.
        st, _, out = post("/v1/collections/col_nope/items", item, "k-2")
        self.assertEqual(st, 404)
        st, _, out = post("/v1/collections/%s/items" % first["id"], item, "k-2")
        self.assertEqual(st, 201)
        st, _, page = self.call("GET", "/v1/collections/%s/items" % first["id"], headers=self.h("c6"))
        self.assertEqual(page["total"], 1)
        st, _, out = post("/v1/collections", {"type": "face", "name": "X"}, "")
        self.assertEqual((st, out["detail"]["error_type"]), (400, "api_usage_error"))
        # The body is checked before the key and before the collection: a 422 first.
        st, _, out = post("/v1/collections", {"type": "face", "name": "x" * 81}, "")
        self.assertEqual((st, out["detail"][0]["type"]), (422, "string_too_long"))
        st, _, out = post("/v1/collections/col_nope/items", {"image": self.BADGE, "reference": ""}, "k-5")
        self.assertEqual((st, out["detail"][0]["type"]), (422, "string_too_short"))
        st, _, out = post("/v1/collections/col_nope/items", item, "k-5")
        self.assertEqual((st, out["detail"]["error_type"]), (404, "collection_not_found"))
        # A billed no_face is kept and replayed.
        scenario = {"name": "t-idem-no-face", "collections": {"faces": 0}}
        self.assertEqual(self.call("POST", "/_mock/scenarios", scenario, {"Content-Type": "application/json"})[0], 200)
        col = post("/v1/collections", {"type": "face", "name": "NF"}, "k-3", "c7", "t-idem-no-face")[2]
        st, _, first_nf = post("/v1/collections/%s/items" % col["id"], item, "k-4", "c7", "t-idem-no-face")
        st2, hd2, again_nf = post("/v1/collections/%s/items" % col["id"], item, "k-4", "c7", "t-idem-no-face")
        self.assertEqual((st, st2, again_nf, hd2.get("x-decisionnode-idempotent-replay")), (422, 422, first_nf, "true"))

class TestUsage(Spec11):
    """Suite 1.18: per-key usage, its figures checked by hand against the fixture."""

    K1, K4, KA = "key_00000000000000000000000000000d01", "key_00000000000000000000000000000f04", "key_00000000000000000000000000000a01"
    SEPT = "start=2026-09-01T00:00:00Z&end=2026-10-01T00:00:00Z"

    def get(self, path, key="dn_test_conformance"):
        return self.call("GET", path, headers={"Authorization": "Bearer " + key, "x-mock-session": "u1"})

    def test_totals_sum_the_hours_of_the_period(self):
        st, hd, out = self.get("/v1/keys/%s/usage?%s" % (self.K1, self.SEPT))
        self.assertEqual((st, hd.get("cache-control")), (200, "private, no-store"))
        # Sept 3 09h, 10h, Sept 4 14h, 15h (free); the hour of August 31 left out.
        self.assertEqual(out["totals"], {"requests": 10, "free_tier_requests": 4, "input_tokens": 7400, "image_tokens": 1200, "video_tokens": 0,
                                         "booster_calls": 1, "cost": {"usd": 0.024213, "model_usd": 0.000213, "boosters_usd": 0.024}})
        self.assertEqual((out["period"], out["as_of"]), ({"start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}, "2026-10-01T00:00:00Z"))
        self.assertEqual(list(out), ["key", "period", "as_of", "totals"])
        st, _, out = self.get("/v1/keys/%s/usage?start=2026-09-03T11:30:00%%2B02:00&end=2026-09-04T16:10:00%%2B02:00&granularity=day" % self.K1)
        self.assertEqual(out["period"], {"start": "2026-09-03T09:00:00Z", "end": "2026-09-04T15:00:00Z"})
        self.assertEqual([(b["start"], b["end"], b["totals"]["requests"]) for b in out["buckets"]],
                         [("2026-09-03T09:00:00Z", "2026-09-04T00:00:00Z", 5), ("2026-09-04T00:00:00Z", "2026-09-04T15:00:00Z", 1)])
        st, _, out = self.get("/v1/keys/%s/usage?%s&group_by=channel" % (self.K4, self.SEPT))
        self.assertEqual([(c["channel"], c["totals"]["requests"]) for c in out["by_channel"]], [("live", 1), ("batch", 100), ("session", 0)])

    def test_who_sees_what(self):
        st, _, out = self.get("/v1/usage?" + self.SEPT)
        self.assertEqual([i["key"]["name"] for i in out["items"]], ["Drone 1", "Drone 2", "Drone 3", "Ingest server"])
        self.assertEqual([i["key"]["status"] for i in out["items"]], ["active", "revoked", "expired", "active"])
        st, _, out = self.get("/v1/usage?" + self.SEPT, "dn_test_scope-keys")
        self.assertEqual([i["key"]["scope"] for i in out["items"]], ["decide"] * 3)
        for path, key, want in (("/v1/keys/%s/usage" % self.K4, "dn_test_scope-keys", (404, "not_found_error")),
                                ("/v1/keys/%s/usage" % self.KA, "dn_test_conformance", (404, "not_found_error")),
                                ("/v1/usage?" + self.SEPT, "dn_test_scope-decide", (403, "key_scope_denied"))):
            st, _, out = self.get(path, key)
            self.assertEqual((st, out["detail"]["error_type"]), want, path)
        st, _, out = self.call("POST", "/v1/batches", {}, {"Authorization": "Bearer dn_test_scope-keys", "Content-Type": "application/json"})
        self.assertEqual((st, out["detail"]["message"]), (403, "This key may only manage keys."))

    def test_query_checked_in_order(self):
        for path, msg in (("/v1/keys/not-a-key/usage?bogus=1", "Unknown query parameter: bogus."),
                          ("/v1/keys/not-a-key/usage?granularity=week", "granularity is none, hour, day or month."),
                          ("/v1/usage?start=2026-09-01T00:00:00Z", "start and end are required."),
                          ("/v1/usage?%s&granularity=day&limit=101" % self.SEPT, "limit is at most 100 with a granularity other than none."),
                          ("/v1/usage?%s&limit=0" % self.SEPT, "limit is a whole number from 1 to 1000."),
                          ("/v1/usage?%s&label=customer" % self.SEPT, "A label filter is name:value.")):
            st, hd, out = self.get(path)
            self.assertEqual((st, out["detail"]["message"], hd.get("x-decisionnode-error")), (400, msg, msg), path)
        # The query before the key: a malformed id with a good query is the 404.
        self.assertEqual(self.get("/v1/keys/not-a-key/usage")[0], 404)

    def test_cursor_and_budget(self):
        st, _, page = self.get("/v1/usage?%s&limit=3" % self.SEPT)
        self.assertEqual(len(page["items"]), 3)
        st, _, rest = self.get("/v1/usage?%s&limit=3&cursor=%s" % (self.SEPT, page["next_cursor"]))
        self.assertEqual(([i["key"]["name"] for i in rest["items"]], rest["next_cursor"]), (["Ingest server"], None))
        # A 400-day period is the longest: 9,600 hours, which still fit these four keys
        # (the read's budget of 150,000 rows is 15 keys of one model there); one hour more
        # is the 400.
        st, _, wide = self.get("/v1/usage?start=2025-08-27T00:00:00Z&end=2026-10-01T00:00:00Z&limit=1000")
        self.assertEqual((st, len(wide["items"]), wide["next_cursor"]), (200, 4, None))
        st, _, out = self.get("/v1/usage?start=2025-08-26T23:00:00Z&end=2026-10-01T00:00:00Z")
        self.assertEqual((st, out["detail"]["message"]), (400, "A period is at most 400 days."))

class TestPublishable(Spec11):
    """Suite 1.19: a publishable key decides from its origins or a native app, and nothing else."""

    def post(self, key, origin=None, model="decisionnode-flash"):
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "x-mock-session": "p1"}
        if origin:
            h["Origin"] = origin
        return self.call("POST", "/v1/decide", {"model": model, "questions": {"q": {"type": "truth", "instructions": "Is it fine?"}}}, h)

    def test_origins_models_and_routes(self):
        self.assertEqual(self.post("dn_pub_test", "https://app.example.com")[0], 200)
        self.assertEqual(self.post("dn_pub_test")[0], 200)
        for key, origin, model, want in (("dn_pub_test", "https://other.example.com", "decisionnode-flash", "origin_not_allowed"),
                                         ("dn_pub_test_web-only", None, "decisionnode-flash", "origin_not_allowed"),
                                         ("dn_pub_test_flash-only", None, "decisionnode-moe", "model_not_allowed")):
            st, _, out = self.post(key, origin, model)
            self.assertEqual((st, out["detail"]["error_type"]), (403, want), key)
        self.assertEqual(self.post("dn_pub_test_web-only", "https://app.example.com")[0], 200)
        st, _, out = self.call("GET", "/v1/models", headers={"Authorization": "Bearer dn_pub_test"})
        self.assertEqual((st, out["detail"]["message"]), (403, "A publishable key may only make decisions."))


class TestKeys(Spec11):
    """Suite 1.20: a keys key's decide keys."""

    def h(self, key="dn_test_scope-keys", idem=None):
        out = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "x-mock-session": "k1"}
        if idem:
            out["Idempotency-Key"] = idem
        return out

    def test_create_replays_its_secret_and_refuses_others(self):
        body = {"name": "Gate 9", "device_id": "gate-9"}
        st, _, first = self.call("POST", "/v1/keys", body, self.h(idem="prov-9"))
        self.assertEqual((st, first["key"]["created_by_key"] is not None, first["secret"].startswith("dn_live_")), (201, True, True))
        st, hd, again = self.call("POST", "/v1/keys", body, self.h(idem="prov-9"))
        self.assertEqual((st, again["secret"], again["key"]["id"], hd.get("x-decisionnode-idempotent-replay")), (201, first["secret"], first["key"]["id"], "true"))
        st, _, out = self.call("POST", "/v1/keys", dict(body, name="Gate 10"), self.h(idem="prov-9"))
        self.assertEqual((st, out["detail"]["error_type"]), (409, "idempotency_key_reused"))
        st, _, out = self.call("POST", "/v1/keys", body, self.h("dn_test_scope-keys-other", idem="prov-9"))
        self.assertEqual((st, out["detail"]["error_type"]), (409, "idempotency_key_reused"))
        self.assertEqual(self.call("DELETE", "/v1/keys/" + first["key"]["id"], None, self.h())[0], 200)
        st, _, out = self.call("POST", "/v1/keys", body, self.h(idem="prov-9"))
        self.assertEqual((st, out["detail"]["error_type"]), (409, "key_not_replayable"))
        # Without a key, every create is a new key with a new secret.
        a = self.call("POST", "/v1/keys", {"name": "Gate 11"}, self.h())[2]
        st, _, out = self.call("POST", "/v1/keys", {"name": "Gate 11"}, self.h())
        self.assertEqual((st, out["detail"][0]["type"]), (422, "name_taken"))
        self.assertNotEqual(a["secret"], first["secret"])

    def test_fields_and_errors(self):
        for body, loc, msg in (({}, ["body", "name"], "Field required"),
                               ({"name": "Gate", "limits": {"tokens_per_second": 0}}, ["body", "limits", "tokens_per_second"], "A key's tokens a second is a whole number from 1 to the workspace's 200000, or null."),
                               ({"name": "Gate", "colour": "red"}, ["body", "colour"], "Extra inputs are not permitted"),
                               ({"name": "Gate", "may_search_collections": "yes"}, ["body", "may_search_collections"], "Input should be a valid boolean")):
            st, _, out = self.call("POST", "/v1/keys", body, self.h())
            self.assertEqual((st, out["detail"][0]["loc"], out["detail"][0]["msg"]), (422, loc, msg), body)
        st, _, out = self.call("PATCH", "/v1/keys/key_00000000000000000000000000000d01", {}, self.h())
        self.assertEqual((st, out["detail"]["message"]), (400, "Nothing to change."))
        st, _, out = self.call("PATCH", "/v1/keys/key_00000000000000000000000000000d01", {"device_id": "x"}, self.h())
        self.assertEqual((st, out["detail"][0]["msg"]), (422, "A key's device_id is set when it is made: revoke it and make another."))
        st, _, out = self.call("GET", "/v1/keys?limit=0", None, self.h())
        self.assertEqual((st, out["detail"]["message"]), (400, "limit is a whole number from 1 to 200."))
        st, _, out = self.call("POST", "/v1/keys/revoke", {"labels": {"customer": "acme"}, "dry_run": "true"}, self.h())
        self.assertEqual((st, out["detail"][0]["type"]), (422, "bool_type"))
        for key, msg in (("dn_test_conformance", "Keys are managed with a keys key, made in the console."), ("dn_pub_x", "A publishable key may only make decisions.")):
            st, _, out = self.call("GET", "/v1/keys", None, self.h(key))
            self.assertEqual((st, out["detail"]["message"]), (403, msg))


class TestKnowledge(Spec11):
    """Suite 1.23: collections of type text, knowledge uploads, search and the knowledge
    booster."""

    def h(self, session, key="dn_test_conformance", **extra):
        out = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "x-mock-session": session}
        out.update(extra)
        return out

    def base(self, session, scope_key=None):
        body = {"type": "text", "name": "Claims policy"}
        if scope_key:
            body["scope_key"] = scope_key
        st, _, col = self.call("POST", "/v1/collections", body, self.h(session))
        self.assertEqual((st, col["type"], col["scope_key"], col["version"]), (201, "text", scope_key, 0))
        return col["id"]

    def test_text_items_ingest_by_reads_and_wait(self):
        cid = self.base("kn1", "mission")
        items = "/v1/collections/%s/items" % cid
        st, _, it = self.call("POST", items + "?wait=true", {"title": "Policy", "text": "# Policy\n\n## Receipts\n\nReject any claim over 500 SEK without a receipt."}, self.h("kn1"))
        self.assertEqual((st, it["status"], it["serving"], it["chunks"], it["reference"]), (201, "ready", "current", 1, it["id"]))
        st, _, it = self.call("POST", items, {"title": "Brief", "text": "Drones fly low.", "metadata": {"mission": "M-42"}}, self.h("kn1"))
        self.assertEqual((st, it["status"]), (202, "queued"))
        path = items + "/" + it["id"]
        self.assertEqual(self.call("GET", path, None, self.h("kn1"))[2]["status"], "processing")
        self.assertEqual(self.call("GET", path, None, self.h("kn1"))[2]["status"], "ready")
        st, _, got = self.call("GET", path + "?include=text", None, self.h("kn1"))
        self.assertEqual(got["text"], "Drones fly low.")
        st, _, ch = self.call("GET", items + "/" + it["id"] + "/chunks", None, self.h("kn1"))
        self.assertEqual((ch["total"], ch["chunks"][0]["byte_start"], ch["chunks"][0]["byte_end"]), (1, 0, 15))
        st, _, col = self.call("GET", "/v1/collections/" + cid, None, self.h("kn1"))
        self.assertEqual((col["items"], col["status_counts"]["ready"], col["chunks"]), (2, 2, 2))

    def test_item_body_refusals(self):
        cid = self.base("kn2")
        items = "/v1/collections/%s/items" % cid
        st, _, out = self.call("POST", items, {"text": "x"}, self.h("kn2"))
        self.assertEqual((st, out["detail"][0]["loc"]), (422, ["body", "title"]))
        st, _, out = self.call("POST", items, {"title": "T", "text": "a\u0000b"}, self.h("kn2"))
        self.assertEqual((st, out["detail"]["error_type"]), (422, "invalid_text"))
        st, _, out = self.call("POST", items, {"title": "T", "text": "a" * (512 * 1024 + 1)}, self.h("kn2"))
        self.assertEqual((st, out["detail"]["error_type"]), (413, "item_too_large"))
        st, _, out = self.call("POST", items, {"title": "T", "text": "x", "upload_id": "upl_" + "0" * 32}, self.h("kn2"))
        self.assertEqual((st, out["detail"][0]["loc"]), (422, ["body", "text"]))
        self.call("POST", items, {"title": "T", "text": "x", "reference": "r1"}, self.h("kn2"))
        st, _, out = self.call("POST", items, {"title": "U", "text": "y", "reference": "r1"}, self.h("kn2"))
        self.assertEqual((st, out["detail"]["error_type"], out["detail"]["details"]["item_id"].startswith("itm_")), (409, "reference_taken", True))

    def test_knowledge_uploads(self):
        pdf = b"%PDF-1.4\n" + b"x" * 100
        f = dict(file_of(pdf, "application/pdf"), purpose="knowledge", filename="policy.pdf")
        st, _, out = self.call("POST", "/v1/uploads", {"files": [f]}, self.h("kn3"))
        self.assertEqual((st, out["uploads"][0]["purpose"]), (201, "knowledge"))
        st, _, out = self.call("POST", "/v1/uploads", {"files": [dict(f, filename="policy.docx")]}, self.h("kn3"))
        self.assertEqual(st, 400)
        st, _, out = self.call("POST", "/v1/uploads", {"files": [dict(f, byte_size=52428801)]}, self.h("kn3"))
        self.assertEqual(out["detail"]["message"], "files[0].byte_size: 52428801 is over the limit of 52428800 bytes.")
        st, _, out = self.call("POST", "/v1/uploads", {"files": [f]}, self.h("kn3", "dn_test_scope-decide"))
        self.assertEqual((st, out["detail"]["error_type"]), (403, "key_scope_denied"))

    def test_search_and_booster(self):
        cid = self.base("kn4", "mission")
        items = "/v1/collections/%s/items" % cid
        st, _, out = self.call("POST", "/v1/collections/%s/search" % cid, {"query": "receipts"}, self.h("kn4"))
        self.assertEqual((st, out["detail"]["error_type"], out["detail"]["details"]), (409, "knowledge_collection_empty", {"items": 0, "failed": 0}))
        self.call("POST", items, {"title": "Rule", "text": "Always ask for the claim number.", "pinned": True}, self.h("kn4"))
        st, hd, out = self.call("POST", "/v1/collections/%s/search" % cid, {"query": "receipts"}, self.h("kn4"))
        self.assertEqual((st, out["detail"]["error_type"], hd.get("retry-after")), (529, "booster_unavailable", "5"))
        self.call("POST", items + "?wait=true", {"title": "Policy", "text": "Reject any claim over 500 SEK without a receipt."}, self.h("kn4"))
        st, hd, out = self.call("POST", "/v1/collections/%s/search" % cid, {"query": "claim without receipt", "top_k": 1}, self.h("kn4"))
        self.assertEqual((st, hd["x-decisionnode-boosters"], [p["pinned"] for p in out["passages"]], out["warnings"]), (200, "knowledge=ok:141", [False], ["scope_not_named"]))
        body = {"state": {"claim": 1}, "questions": {"q": {"type": "truth", "instructions": "Approve?"}}, "boosters": [{"name": "knowledge", "options": {"collection": cid, "scope": "M-42"}}]}
        st, _, out = self.call("POST", "/v1/decide", body, self.h("kn4"))
        reading = out["evidence"]["knowledge"]["results"][0]
        self.assertEqual((st, reading["truncated"], [p["rendered"] for p in reading["passages"]][:1], out["usage"]["boosters"]), (200, False, [True], 1))
        body["boosters"] = [{"name": "knowledge", "options": {"collection": cid, "match": {"mission": "M-42"}}}]
        st, _, out = self.call("POST", "/v1/decide", body, self.h("kn4"))
        self.assertEqual((st, out["detail"]["error_type"]), (400, "api_usage_error"))
        body["boosters"] = [{"name": "knowledge", "options": {"collection": cid, "include_text": True}}]
        st, _, out = self.call("POST", "/v1/decide", body, self.h("kn4", "dn_test_scope-decide"))
        self.assertEqual((st, out["detail"]["message"]), (403, "include_text needs a full-scope key."))

    def test_items_q_and_sort(self):
        cid = self.base("kn5")
        items = "/v1/collections/%s/items" % cid
        for title, ref in (("b rules", "r-1"), ("A claims", "r-2"), ("c claims", "x-claims")):
            self.call("POST", items, {"title": title, "text": "Text.", "reference": ref}, self.h("kn5"))
        st, _, page = self.call("GET", items + "?q=CLAIMS&sort=title", None, self.h("kn5"))
        self.assertEqual((st, [i["title"] for i in page["items"]], page["total"], page["q"], page["sort"]), (200, ["A claims", "c claims"], 2, "CLAIMS", "title"))
        st, _, page = self.call("GET", items + "?sort=oldest&limit=2", None, self.h("kn5"))
        self.assertEqual(([i["title"] for i in page["items"]], page["next_offset"], page["q"]), (["b rules", "A claims"], 2, None))
        st, _, page = self.call("GET", items, None, self.h("kn5"))
        self.assertEqual(([i["title"] for i in page["items"]], page["sort"]), (["c claims", "A claims", "b rules"], "newest"))
        st, _, out = self.call("GET", items + "?sort=Title", None, self.h("kn5"))
        self.assertEqual((st, out["detail"]["message"]), (400, "Query parameter sort must be newest, oldest or title, got Title."))
        st, _, out = self.call("GET", items + "?q=", None, self.h("kn5"))
        self.assertEqual((st, out["detail"]["message"]), (400, "Query parameter q must be 1 to 200 characters."))


if __name__ == "__main__":
    unittest.main()
