# DecisionNode SDK conformance suite

[DecisionNode](https://decisionnode.com) is a decision engine: send it text, JSON or images with a question, and it answers with calibrated probabilities.

Language-neutral test cases and a mock API server. Every DecisionNode SDK runs this suite in CI. An SDK release needs every case to pass.

Suite version: 1.13, for SDK specification version 1.3 (the `SPEC_VERSION` every SDK exports). What changed in each version is in [CHANGELOG.md](CHANGELOG.md).

```
cases/
  requests/    a described SDK call -> the exact HTTP request(s) it must send
  responses/   a recorded response -> the values every field must parse to
  errors/      a recorded error response -> the error class and its fields
  behaviour/   calls and session actions against a mock scenario -> requests, waits, events, outcomes
  constants/   a public constant -> its value
schema/        JSON Schema (draft 2020-12) for each case kind, plus common.schema.json
fixtures/      files that cases refer to by path (images)
mock/server.py the mock API server, WebSocket and object store included (Python 3.10+, standard library only)
runner/        the reference runner and the minimal client it drives
tests/         unit tests of the mock
```

## Using the suite from an SDK repository

The SDK's tests find the suite through `DECISIONNODE_CONFORMANCE_DIR`. An SDK test harness:

1. starts the mock (`python3 $DECISIONNODE_CONFORMANCE_DIR/mock/server.py --port 0`) and reads the port from the first stdout line (still one file, standard library only, no dependency to install). The mock writes nothing else to stdout or stderr unless asked (`--verbose`, `--log-file`), so a harness may pipe both and never read them again,
2. loads every `cases/<kind>/*.json` file of the five kinds and makes one test per file, named by its `id`,
3. runs each case as described below and compares with `expect`.

Never skip a case. When `DECISIONNODE_CONFORMANCE_DIR` is unset the conformance tests fail; they never skip, so a CI job that forgot the checkout cannot pass. An SDK with both blocking and non-blocking forms (a sync and an async client, `Async` methods) runs the whole suite through each form.

### CI

Every SDK's workflow checks out `Bynn-Intelligence/decision-node-conformance` and runs the whole suite. The repository is public, so the checkout needs no token, and a pull request from a fork runs it too. This is the one supported setup; no submodule, no copy of the cases in the SDK repository.

The workflow keeps its supply chain fixed:

- every action is pinned to a full commit SHA, with the tag in a comment,
- every checkout sets `persist-credentials: false`, so no token stays in the working copy for later steps,
- the suite is checked out at a pinned commit (`ref:`), the full SHA of the suite version the SDK targets. Each suite version is tagged (`v1.13`), and the tag names the commit to pin. Moving to a newer suite is a reviewed change to that one line.

Copy this job, set `ref:` to the suite commit, and replace the last two steps with the language's own setup and test command (pin its setup action the same way):

```yaml
name: conformance

on:
  push:
  pull_request:

jobs:
  conformance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
        with:
          persist-credentials: false

      - name: Check out the conformance suite
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
        with:
          repository: Bynn-Intelligence/decision-node-conformance
          # The full SHA of the suite version this SDK targets (git rev-parse v1.13^{commit}).
          ref: 0000000000000000000000000000000000000000
          persist-credentials: false
          path: conformance

      - name: Python for the mock server
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: "3.12"

      - name: Set up the language
        run: echo "the language's setup step, for example actions/setup-node or actions/setup-go"

      - name: Run the conformance suite
        env:
          DECISIONNODE_CONFORMANCE_DIR: ${{ github.workspace }}/conformance
        run: echo "the SDK's conformance test command"
```

Locally, clone the suite anywhere and set `DECISIONNODE_CONFORMANCE_DIR` to it.

## Conventions in the cases

- **Neutral names.** Methods (`decide`, `decide_many`, `check`, `classify`, `score`, `gate`, `models`, `decide_raw`, and from 1.4 `number`, `upload`, `open_session`, `supervise`, `run_batch` and `batches.create`, `batches.add`, `batches.finalize`, `batches.get`, `batches.list`, `batches.results`, `batches.cancel`, from 1.6 `check_request` and `Session.check_frame`, and from 1.13 `collections.create`, `collections.list`, `collections.get`, `collections.delete`, `collections.enroll`, `collections.list_items` and `collections.delete_item`), arguments and fields use the neutral snake_case names. Each SDK maps them to its own convention (`decideMany`, `DecideMany`, `inputTokens`). The arguments `true` and `false` map to the language's names for them, such as `if_true` and `if_false`.
- **Builders.** Inside `call.args`, an object with the single key `$truth`, `$choice`, `$score`, `$number` (`instructions`, `min`, `max`, `step`), `$image`, from 1.7 `$video` and `$frame`, from 1.8 `$point` and `$box` (`instructions`, `image`, `units`; `image` and `units` passed only when present), and from 1.11 `$booster` (`name`, `images`, `require`, `options`; the last three passed only when present) stands for a builder call with those arguments. `{"$text": {"repeat": "x", "times": n}}` stands for that string repeated (for bodies of several MB without storing them). `{"$list": {"count": n, "item": v}}` (from 1.6) stands for a list of `n` copies of `v`, with `{i}` in every string of a copy replaced by its 0-based index (`"r{i}"` gives `"r0"`, `"r1"`, ...), for calls of many requests without a long case file. `{"$nest": {"depth": n, "value": v}}` (from 1.5) stands for `v` inside `n` arrays, `[[[...v...]]]`, so the nesting limit is tested without a case file nested that deep (some JSON readers stop at 128 levels). In behaviour steps, `{"$ref": "name.path"}` is a value an earlier step saved (`b.id`, `ids.0`). Every other value is plain data, passed as is (a wire-form question map is accepted wherever a builder is).
- **Images.** `$image` has `path` (relative to the suite root), `data_base64` with or without `media_type` (decode it and pass raw bytes; without `media_type`, pass the bytes alone so the SDK detects JPEG, PNG or WebP from the first bytes), `url`, or `upload_id` (`Image(id, upload_id)`, with an optional `media_type`). `pad_to: n` with `path` or `data_base64`: append zero bytes until the image is `n` bytes, so it keeps its header (type, pixel size) and has the size the case needs. A `media_type` given with bytes is passed as the declared type (`image/gif` included, for the refusal cases).
- **Videos (from 1.7).** `{"$video": {"id", "frames", "fps"?, "start"?, "end"?}}` is `Video(id, frames, fps, start, end)`: pass `fps`, `start` and `end` only when present, as given (a case may give a value the SDK must refuse). Each item of `frames` is a `$frame` or a plain wire-form frame. `{"$frame": {"t", "path" | "data_base64" | "data_text", "media_type"?}}` is `VideoFrame(t, data, media_type)` (the type is `VideoFrame` in every SDK: a session frame is already a frame): `path` (relative to the suite root) and `data_base64` are passed as raw bytes (read the file, or decode the base64), `data_text` is passed as base64 text unchanged, and `media_type` only when present. A video given as a plain map is passed as is. The request's `videos` argument, like `images`, is passed only when the case gives it (an empty list included).
- **Client.** `client` holds constructor arguments. An absent or `null` key is not passed. An empty string (`"api_key": ""`) is passed as an empty string, and the SDK must treat it as not given: the environment, then the default, applies. The same holds for an empty `model` on a call. `timeout` is in seconds. A `timeout` of 0 or below and a negative `max_retries` are passed as given and must be refused. `base_url` is usually absent: point the client at the mock. In a `base_url`, `{mock_url}` is the mock's base URL and `{mock_port}` its port. `inline_threshold` (bytes) and, from 1.6, `upload_timeout` (seconds for one storage PUT attempt, apart from `timeout`; in each language's naming) are passed to the constructor when present. `clock: "mock"` (from 1.5) asks the harness to replace the SDK's injectable clock, the time it judges upload URLs and cache entries by, with one that reads `GET /_mock/clock?session=<id>` and returns its `epoch_seconds`, so the SDK's time moves when the case moves the mock's. `http_client: "caller_following_redirects"` asks the harness for an HTTP client of its own making; see "Redirects with the caller's HTTP client" below. Before each case unset `DECISIONNODE_API_KEY`, `DECISIONNODE_BASE_URL` and `DECISIONNODE_MODEL`, then set the case's `env`, replacing `{mock_url}` with the mock's URL.
- **Numbers.** Compare numbers as IEEE-754 doubles, exactly, never with a tolerance. `0.8200000000000001` must stay `0.8200000000000001`. Use a JSON parser that keeps the shortest round-trip value of each double.
- **Maps.** JSON objects compare by key, regardless of order, unless the case lists the pointer in `ordered_keys`. Score `probabilities` and `legend` are written with string keys `"0"`..`"K-1"` and compare to the SDK's integer-keyed maps.
- **Empty.** `null` in an expectation means the language's empty value (`None`, `null`, `nil`, `undefined`, a `nil` pointer, `Option::None`). A missing `latency_ms` or token count is empty, never `0`.
- **Boosters (from 1.11).** The `boosters` argument (of `decide`, the helpers, a `decide_many` request and a batch request) is a list whose items are a booster name (a string, passed as is; a plain list of names is the normal form), a `$booster`, which is `Booster(name, images, require, options)`, or a plain wire-form object `{"name", ...}` (passed as is; the object's key is `name`). Like `images`, it is passed only when the case gives it, an empty list included.
- **Type names.** Answers are `TruthAnswer`, `ChoiceAnswer`, `ScoreAnswer`, from 1.4 `NumberAnswer`, from 1.8 `PointAnswer` (`x`, `y`, `present`, `confidence`, `image`, `width`, `height`) and `BoxAnswer` (`x1`, `y1`, `x2`, `y2` and the same five) from 1.12 `PointsAnswer` (`count`, `present`, `complete`, `points`, each `x`, `y`, `confidence`, then `image`, `width`, `height`) and `BoxesAnswer` (the same with `boxes`, each `x1`, `y1`, `x2`, `y2`, `confidence`), the answer to a point or box question asked with `max` and told by its list, and, for a type the SDK does not know, `UnknownAnswer` (answer `kind` `truth`, `choice`, `score`, `number`, `point`, `box`, `points`, `boxes`, `unknown`). A point or box answer, in either shape, with a field missing or of the wrong type is an `UnknownAnswer`. An entry of `models()` is a `ModelInfo`. One result of `decide_many` is a `DecisionResult` (a `Decision` or the error that request ended with). From 1.11 a decision's `usage.cost` is a `Cost` (`usd`, `model_usd`, `boosters_usd`, `free_tier`), read as sent: `usd` is the server's, never `model_usd` plus `boosters_usd` computed by the client; it is empty when the answer has none. A batch result line's decision and a session's open and frame replies carry it too. From 1.11 a decision's `evidence` maps each booster's name to an `Evidence` (`version`, `status`, `latency_ms`, `billed_calls`, empty when the entry has none, and `results`), and each result is a reading: `AgeReading` (`image`, `faces`, `age_years`, `low`, `high`, `confidence`), `AiGeneratedReading` (`image`, `generated`, `confidence`, `method`, `top_generator` with `name` and `probability` or null, `c2pa_signer`, `c2pa`) from 1.11 `FaceDetectionReading` (`image`, `faces`, `detections`, each a `box` of corners `x1`, `y1`, `x2`, `y2` and a `confidence`), from 1.13 `FaceRecognitionReading` (`image`, `faces`, `matches`, each a `box`, a `score`, the `collection` searched, the item's `reference`, and `name` or `label`, empty when absent; it replaces `WantedPersonReading`) and `OcrReading` (`image`, `text`, `truncated`, all three always sent) or, for a booster the SDK does not know and for a reading with a field missing or of the wrong type, a generic reading holding its raw fields (reading `kind` `age`, `ai_generated`, `unknown`). An age reading with `faces` 0 has no age fields: they are empty, and the reading is still an `AgeReading`. A booster that timed out or failed lists `{"image": id}` with no reading for each picture it was meant to read, and a picture it could not decode reads `{"image": id, "readable": false}`: both are generic readings holding their raw fields, the entry's `status` saying why. An `ai_generated` reading always carries the keys `top_generator` (null unless the picture reads as likely generated, and on every `c2pa` reading) and `c2pa_signer` (null unless the file's Content Credentials are trusted); `c2pa` is there only as `"untrusted"` (Content Credentials that validate but are not trusted; any other string is kept as received) and is empty otherwise. A field the SDK does not know is ignored, as inside an answer: the reading stays typed.
- **Argument errors.** `{"argument_error": true}` is the language's own error for a bad argument (`ValueError`, `RangeError`, `IllegalArgumentException`, `ArgumentException`, `ArgumentError`, `InvalidArgumentException`, a returned Go error, a Rust `Err` of the base kind), raised before any request is sent. It is never a DecisionNode error class. It may come from the constructor (a bad `base_url`, `timeout` or `max_retries`) or from the call, so a harness builds the client inside the same check as the call. A fault in the request's content (a question, an image, a session or batch field) is not an argument error: it raises the API's own class locally (see "Local checks" below).
- **Checks (from 1.6).** A `call` with `"check": true` (methods `decide`, `open_session`, `batches.create`, `batches.add`) is not made: the harness hands the same arguments to the SDK's `check_request(method, request)`, which runs every local check that call would run before sending, exactly as sending would (held `/v1/models` limits included), raises the same error, sends nothing and uploads nothing, and returns nothing (the language's unit) when the request would be sent. A `send` step with `"check": true` does the same through `Session.check_frame(frame, image, questions, reset, seq)`. `{"result": "checked"}` is the outcome of a check that found nothing; any other outcome is the error the call would raise. `batches.add`'s `batch_id` plays no part in the check. The neutral names are `check_request` and `check_frame` because `check(...)` is already the truth helper; each language uses its idiom (`checkRequest`, `CheckRequest`, or one method per call such as `check_decide`, as long as every listed call is reachable), and since a check makes no request it needs no async form.
- **Headers.** Header names compare case-insensitively.
- **Patterns (from 1.11).** Where an expectation is `{"$pattern": regex}` (a decision's `model`, a fallback's `served_model` and `requested_model`), any string the regular expression finds passes: the mock answers with a pinned model id, and a case checks its family prefix, `{"$pattern": "^decisionnode-moe-"}`, never its version.
- **Partial expectations.** In a `decision`, an `answer` and error `fields`, only the keys present are checked. `probabilities`, `legend`, `raw`, `entries`, `safety` and `limits` compare whole, and so do (from 1.11) a decision's `evidence` and an error's `boosters`, whose key order must also be the order given: the request's order for `evidence`, the header's for `boosters`.

Fixed by the suite, so every SDK sends the same request:

| Call | Wire form |
|---|---|
| `decide` without a state | `"state": ""` |
| `decide` without a model | the client's model, else `DECISIONNODE_MODEL`, else `decisionnode-moe`; always sent |
| model ids (from 1.11) | the family ids `decisionnode-moe` and `decisionnode-flash` mean the current release, and an SDK's defaults, constants and examples use them; a pinned id is `decisionnode-<family>-<version>`, its version read from `GET /v1/models`, never written into an SDK. `model` stays free text, so a pinned id, an older id (`decisionnode-1.0`, `decisionnode-1.0-flash`, `decisionnode-latest`, `decisionnode-flash-latest`) and a fine-tuned name are sent as given. Every answer names the pinned id that answered; a case checks its family prefix, never its version |
| `videos` (from 1.7) | left out when none or an empty list is given; each video `{"id", "fps"?, "start"?, "end"?, "frames"}` with `fps`, `start` and `end` only when given; each frame `{"t", "media_type", "data"}`, bytes as standard base64 with padding, base64 text as given, the media type as given or detected from the bytes. Frames always go inline: never uploaded, never counted toward the inline threshold. `decide`, the helpers, `decide_many` and batch lines carry videos alike |
| `point(instructions, image, units)`, `box(...)` (from 1.8) | `{"type": "point", "instructions": ...}` and `{"type": "box", ...}`, with `image` and `units` only when given. Local checks: a request with one needs an image; `image` is a string naming one of the request's images (a video's id is not one), required when the request has two or more; `units` is `pixels` or `normalized`; each fault is a `ValidationError` whose `entries` list every fault of the request in question order (within a question `image` before `units`); missing instructions is the plain `400` `Point question must have instructions: <id>`. |
| `boosters` (from 1.11) | the list as `boosters`, in the caller's order: a name as the string, `Booster(name, images, require, options)` as `{"name": name, "images"?, "require"?, "options"?}` with each argument left out when not given (a `require` given as `false` is sent), and a `Booster` with no option given as the bare name (the plain list is the normal form), a plain wire-form object as given. Left out or empty, the body has no `boosters` key. `decide`, the helpers, `decide_many` and batch lines carry it alike. Nothing about boosters is checked locally, `check_request` included: the list's shape, the names, the images a booster reads and which boosters a workspace may use are the API's to judge. The one exception is the nesting limit, which is on the whole body: a booster's `options` count toward the 128 levels like any other value |
| the body limit (from 1.11) | the encoded body, as it would be sent (inline images and frames in it, an image the SDK would upload named by its `upload_id`), is at most 26,214,400 bytes (25 MiB). Over it, `decide`, `decide_many` (for the whole call, before anything is sent), each batch line and `check_request` raise `RequestTooLargeError` with no status, no request id, `error_type` `request_too_large_error` and the API's message `The request body is over 26,214,400 bytes (25 MiB). Send large images as uploads (POST /v1/uploads) and name them by upload_id.`; nothing is sent and nothing is uploaded. `decide_raw` sends its body as given. The inline threshold stays 512 KiB, so local images above it are uploaded and a request with images stays far under the limit. JSON encoders differ by a few bytes, so the cases stay 32 bytes over and 151 bytes under the limit in any encoding; an SDK's own tests check the exact byte |
| `fallback` (from 1.11) | `decide`, the helpers, `decide_many`'s requests and batch lines take `fallback` (a boolean), sent as `"fallback": true` or `false` only when given; left out, the server's default (true) applies. `Decision.fallback` is null on an ordinary answer and `{"served_model", "requested_model"}` from `x-decisionnode-fallback` and `x-decisionnode-requested-model` on a fallback answer (a request for the MoE family answered by Flash because no worker of the model asked for was ready, each named by its pinned id); `Decision.model` stays the body's `model`, the model that answered. The `529` a `fallback: false` request gets is an `OverloadedError`, retried as every `529` |
| a fine-tuned model's name (from 1.11) | free text, like every model name: `<base>@<workspace-slug>/<name>` and the pinned `<base>@<workspace-slug>/<name>:<n>` are sent exactly as given, never parsed or checked locally. `Decision.model` is the pinned name the API answered with |
| more images or videos than a request takes (from 1.9) | the API's `422`: a `ValidationError` whose `entries` hold `{"type": "too_long", "loc": ["body", "images"], "msg": "List should have at most 16 items after validation, not 17", "input": [...], "ctx": {"field_type": "List", "max_length": 16, "actual_length": 17}}` (the held `/v1/models` limit in place of 16 when held), and the same on `["body", "videos"]` with 4; listed after the state's and the questions' entries, images before videos. `input` is the list in its wire form, except that a local image (a path, bytes or base64) is listed as `{"id", "media_type"}`, its bytes left out. `upload(images)` takes 1 to 16 images (one presign): more raise the presign's `400` `api_usage_error` `files: 1 to 16 files, got 17.` before anything is sent |
| local video checks (from 1.7) | before anything is sent, in the server's order and words (a number in a message as printf's `%g` writes it: `1.5`, `4`), `BadRequestError` `api_usage_error`: an id; ids unique across images and videos; a non-empty `frames` list; each `t` a number of at least 0, larger than the one before; a media type of `image/jpeg`, `image/png` or `image/webp`; `fps` above 0 and at most 4; `start` and `end` at least 0; `end` after `start` (0 when absent). Not checked locally: frame counts, a frame's bytes against its media type, sizes, pixels and the window (the server's) |
| `check(...)` | one question with the id `check` |
| `classify(...)` | one question with the id `classify` |
| `score(...)` | one question with the id `score` |
| `check`, `classify`, `score`, `gate` | take `images` and `model` like `decide` |
| `gate(action, context)` | state `{"action": ..., "context": ...}` and these three questions, word for word in every language: |
| | `risk`: score, "How much harm could this action cause if it is wrong?", levels `none`, `low`, `moderate`, `high`, `severe` |
| | `touches_production`: truth, "Does this action change a production system, production data or anything customers use?" |
| | `verdict`: choice, "Should this action run as it is, wait for a person to confirm it, or be blocked?", options in this order: `allow` "safe to run without asking", `confirm` "a person should confirm it first", `block` "should not run" |
| `decide_raw(body)` | the body exactly as given |
| every request | `Authorization: Bearer <key>`, `User-Agent: decisionnode-<language>/<version>` with the language one of `python`, `node`, `go`, `java`, `dotnet`, `ruby`, `php`, `rust`, `Content-Type: application/json` on a POST |
| `decide_many(requests, concurrency)` | a `concurrency` below 1 is an argument error, before anything is sent |
| `decide_many` with an invalid request | every request is checked before any is sent and nothing is sent when one fails: an empty question map is an argument error for the whole call; a fault in a request's content (an image whose bytes are no known format, a question over its limits) raises the API's class for the whole call (from 1.4) |
| `decide` with an empty question map | an argument error, before anything is sent |
| base URL | `https`; plain `http` only for the hosts `localhost`, `127.0.0.1` and `::1`, compared whole. Any other `http` base URL, given or from `DECISIONNODE_BASE_URL`, is an argument error before a request |
| `timeout`, `max_retries` | `timeout` above 0, `max_retries` 0 or more; anything else is an argument error |
| `timeout` | covers the whole attempt, from sending to the last byte of the body, not each read |
| `timeout` with ocr (from 1.11) | a `decide`, a helper or a `decide_many` request whose `boosters` name `ocr` (bare or in the object form) waits 30 s per attempt when no `timeout` was set, and exactly the `timeout` set otherwise (never raised); it is never retried after a timeout, since the server finishes that attempt and may bill it, while its other retries stay |
| `Image(id, data)` without a media type | detected from the first bytes (JPEG, PNG, WebP); any other format raises `BadRequestError` (`api_usage_error`) locally, before any request (an argument error until 1.3) |
| an answer of a known type with a missing or wrongly typed field | an `UnknownAnswer` with its type and raw fields from `decide`; the decision does not fail and the other answers parse |
| `check`, `classify`, `score` whose own answer is missing or malformed | `ServerError` with the response's `status`, `request_id` and `reason` from `x-decisionnode-error`: a helper promises a typed answer, so it never returns an `UnknownAnswer` |
| the base error | exported under the name `DecisionNodeError`; an alias is enough where the language names its base error otherwise |

Fixed by the suite from 1.4 (spec 1.1):

| Call | Rule |
|---|---|
| local checks | before any request, every call that carries questions or images (`decide`, `decide_many`, the helpers, `open_session`, session frames, batch requests; not `decide_raw`) checks the API's fixed rules and raises the API's own class, and nothing is sent. From 1.5 the list is every fixed rule, with the exact `error_type`: see "Local checks" below. An empty question map stays an argument error |
| images | URL images pass through as `{id, url}`, never fetched. Local bytes go inline as `{id, media_type, data}` when the request's local images total under the inline threshold (`DEFAULT_INLINE_THRESHOLD`, 524,288 bytes; the client's `inline_threshold` overrides it); at or above it they are uploaded and sent as `{id, upload_id}` |
| upload | the request's images not in the cache in one `POST /v1/uploads` (`content_type`, `byte_size`, `checksum` = base64 MD5, `sha256` = base64 SHA-256), one `PUT` each to the returned `url` with exactly the returned `headers` and the bytes (no `Authorization`, none of the client's `headers`, no redirect followed), then the request. `upload(images)` always uploads |
| PUT failures | retried on 5xx and connection errors with the retry policy; a `412` on a retry is success; a `403` is never retried on the same URL, and from 1.5 a `403` once the URL's `expires_in` (300 s) has passed, by the client's clock since the presign answered, is presigned again once (a second such `403` raises); anything else raises the base `DecisionNodeError` with the storage status, `error_type` `upload_failed` and the storage's `<Code>` in `reason` |
| upload cache | by SHA-256 of the bytes and media type, in the client's memory, until 5 minutes before `expires_at` by the client's clock; a cached upload is named by `upload_id` whatever its size |
| stale references | a `400` `upload_expired`, `upload_consumed`, `upload_not_found` or `upload_incomplete` on a request naming uploads the SDK placed: drop them, upload again, resend once (not a retry); a second one raises `BadRequestError` |
| `number(...)` | one `number` question with the id `number`; the builder leaves out `min`, `max` and `step` not given |
| answers | `noul` parses into `TruthAnswer` (its value from `noul`); `number` into `NumberAnswer` (`number`, `expected`, `confidence`, `probabilities` keyed by the server's strings); a malformed one is `UnknownAnswer` |
| `open_session(...)` | `POST /v1/sessions` with `model` always sent and only the fields given; a `429` `session_limit_exceeded` is never retried (a `RateLimitError`; an SDK's `SessionLimitError` subclass passes) |
| `Session.send` | one JSON text message; `seq` counted from 1 when not given; an image inline (`{media_type, data}`) under the threshold, else `{upload_id}`. From 1.5 a frame that breaks a fixed rule is refused by `send` itself (it raises; see "Local checks"), nothing goes on the socket and the refused frame takes no `seq` |
| `Session.end()` | `DELETE /v1/sessions/{id}`; a `404` is success |
| `batches.create/add` | JSON Lines (`application/x-ndjson`), one decide body per request with its `custom_id`, `model` always sent, an absent state as `""`; `add` splits into calls of at most 10,485,760 bytes in order |
| batch retries | `get`, `list`, `results`, `cancel` as any call; `create`, `add`, `finalize` only on `429` and `529`, never after a timeout, a lost answer or a 5xx; a finalize answered `409 invalid_batch_state` is read with `get` and returns the batch when it is no longer a draft |
| `run_batch` | create an empty draft, add, finalize, poll `get` every `poll_interval` (at least 10 s, else an argument error; the server's `Retry-After` when longer), then results; an add that fails cancels the draft and raises |
| collections (from 1.13) | `create(name, type)` is `POST /v1/collections` with `{"type", "name"}` (`face` by default); `list()` is `GET /v1/collections` and returns the body's `collections` list in order; `get` and `delete` take the id in the path as given (a public collection's name included); `enroll(collection_id, image, reference, label)` is `POST /v1/collections/{id}/items` with the image as a decision sends it and `label` only when given; `list_items` sends `offset` and `limit` only when given; `delete_item` is `DELETE /v1/collections/{id}/items/{item_id}`. Each returns the API's object as sent. `create` and `enroll` send `Idempotency-Key`, a new UUID (version 4) per call and the same on every attempt of it, so the server creates, enrolls and bills once however often the call is tried; they retry as any call, and also on a `409` `idempotency_in_progress` after its `Retry-After`. A `409` `idempotency_key_reused` is raised, never retried. The reads and deletes carry no key and retry as any call |

Fixed by the suite from 1.6 (for consistency across the SDKs):

| Call | Rule |
|---|---|
| `check_request(method, request)`, `Session.check_frame(...)` | public and pure: every local check of the call, the same error, no request, no upload; nothing returned when the request would be sent (see "Checks" above) |
| held limits by alias | a limit held from `/v1/models` applies to the model a request names by its entry's `name`, else by the entry that states that name as its `alias` or its `version` (every entry names its pinned `version` and its `alias`); a name the held list does not state holds nothing and its checks are left to the server |
| `upload_timeout` | each storage PUT attempt has its own timeout, default 60 s, apart from the API attempt's `timeout`; a PUT that outlives it is a `TimeoutError` (after the PUT retries) |
| `batches.results` | reads the whole JSON Lines file line by line, however many lines, and yields every result in upload order (a results file is not paged by the SDK) |

### Local checks (from 1.5)

The server never sees a request the client could have refused. Every fixed rule the API documents is checked before anything is sent, in the server's order (the nesting limit, then every `422` entry of the request, then the first `400` in question order, then the images), and a failure raises what the server would answer: the same class, the same `error_type`, the documented message where the API documents one, and for a `422` a `ValidationError` whose `entries` are exactly the server's list. A local error has no `status` and no `request_id`; its `reason` may say what `x-decisionnode-error` would. A rule whose value only `/v1/models` or the session's open response states is checked against the value the client already holds (from an earlier `models()` call on the same client, found by the model's name or, from 1.6, by an alias or pinned version an entry states; or from the `Session`) and skipped when it holds none; where `/v1/models` restates a documented bound (`sessions.max_window`, `max_choice_options`), a held value replaces the documented one. Token counts, the model's own limits counted in tokens, upload references and anything else that depends on server state stay the server's.

| Rule (source) | Raised as |
|---|---|
| an unknown question type: not `truth`, `noul`, `choice`, `score`, `number`, `point`, `box` on `/v1/decide` and in sessions and batches | `BadRequestError`, `api_usage_error`, "Invalid request." |
| a question that is not an object or has no `type`; `instructions`, a state (an `open_session` `state` or `instructions` too), an option description, a score level or a truth side that is not a string, an object or an array; a choice without `criteria` or whose `criteria` is not an object; a score without `criteria`, with an empty list, or with levels that are not a list (an object keyed `"0"` to `"K-1"` is a list on `/v1/decide` only) | `ValidationError`, `error_type` null, the server's entries |
| a choice with no option or over 255; a score over 10 levels; a truth (or `noul`) with neither instructions nor criteria; a number without instructions | `BadRequestError`, `error_type` null (the API's plain-string `400` detail), the documented message: "Choice question must have at least one choice: <id>", "Too many choices. Must have at most 255 choices.", "Too many score levels. Must have at most 10 levels.", "Truth question must have criteria or instructions: <id>" ("Noul question ..." for a question sent as `noul`), "Number question must have instructions: <id>" |
| a number grid: `min`, `max` or `step` not a number (`float_type`) or not finite, beyond plus or minus 2^53, `step` not above 0, more than 6 decimals in `min` or `step` (counted on the shortest decimal form of the number), beyond the decimal bound `10^(15-d) - 10^-d`, `min` above `max`, more than 4,096 values | `ValidationError`, `error_type` null, the API's entries in its order |
| a number question to a model whose `number_max_grid_values` the client holds as 0 | `BadRequestError`, `api_usage_error` |
| a point or box question's `max` (from 1.12): not an integer, under 2 or over 16. The capability is never judged locally, not even from a held `/v1/models` entry without `places.max_places`: the API's `422` `not_supported` is only ever the server's | `ValidationError`, `error_type` null, the API's entries (`int_type`, `greater_than_equal` with `ctx` `ge` 2, `less_than_equal` with `ctx` `le` 16) |
| more than 128 objects and arrays inside each other, the body's own object included, `boosters` and their `options` too (from 1.11) | `BadRequestError`, `api_usage_error`, "Invalid request." |
| images: an empty or repeated id; from 1.11 an id over 64 characters (counted as Unicode code points, never UTF-16 units or bytes, the message `Image <n>: its id is longer than 64 characters.` with the image counted from 0; image by image, the empty id, then the length, then a repeated id; on every body that carries the ids: `decide`, the helpers, `decide_many`, batch lines and `check_request`, not a session frame's image, which goes without its id, and not `upload`); local bytes not JPEG, PNG or WebP by their magic bytes or not of a declared type; over 10,485,760 bytes; over 40 megapixels by the header; an upload reference whose `media_type` is not one of the three (more than 16 images is the `422` `too_long`, from 1.9) | `BadRequestError`, `api_usage_error` |
| an image URL that is not `https` | `BadRequestError`, `api_usage_error` (as the mock answers it) |
| an `https` image URL over 2,048 characters, with user information, on a port other than 443, without a host, or whose host is a literal address in a blocked range (IPv4-mapped and NAT64 addresses by the IPv4 address they embed; a DNS name is the server's to resolve) | `BadRequestError`, `image_url_refused` |
| `open_session`: `window` outside 1 to 64 and `ttl_seconds` outside 1 to 3,600 (a held `sessions.max_window` or `max_ttl_seconds` replaces the bound), not an integer | `ValidationError`, `error_type` null, the server's entries |
| `open_session` on a model whose `sessions.enabled` the client holds as false | `BadRequestError`, `api_usage_error` |
| `Session.send`: `seq` outside 0 to 9,007,199,254,740,991; neither `frame` nor `image`; a `frame` that is not a string, an object or an array, or is empty; `questions` empty or naming an id the open response did not list; `reset` not a boolean; a frame nested over 128 levels; an image by URL; an image that breaks the image rules | `BadRequestError`, `api_usage_error` (the frame error's type), "questions: unknown question id '<id>'" for an unknown id |
| `batches.list`: `offset` below 0, `limit` outside 1 to 100 | `BadRequestError`, `api_usage_error` |
| `collections.list_items` (from 1.13): `offset` below 0, `limit` outside 1 to 100, with the messages of `batches.list` | `BadRequestError`, `api_usage_error` |
| `collections.create` and `collections.enroll` (from 1.13, the limits a client may hold): a type other than `face`; a name not 1 to 80 characters; a reference over 64 characters (`string_too_long`) or not matching `^[A-Za-z0-9_.:-]{1,64}$` (`string_pattern_mismatch`, an empty one included); a label over 128 characters; the image's URL as a decision's (`image_url_refused`). A name's or a label's control characters and every other fault of the picture (the `422` `image_unreadable`) are the server's | `ValidationError`, `error_type` null, the API's entries at `["body", <field>]` (`literal_error`, `string_too_short`, `string_too_long`, `string_pattern_mismatch`) |
| `batches.create`, `batches.add` (and `run_batch`): a request with no `custom_id`, one that is not a string or is empty, one repeated within the call; an `add` with no requests; with a held `batch` object, more requests than `requests_per_batch` or a `custom_id` longer than `max_custom_id_chars` (lines numbered from 1 within the call) | `BadRequestError`, `api_usage_error`, the documented messages "Line <n> has no custom_id.", "Line <n>: custom_id must be a string of 1 to 512 characters.", "Line <n>: custom_id <id as JSON> is already in the batch.", "The call holds no requests." |

The behaviour cases named `local-*` check each row with `attempts` 0 (or only the `GET /v1/models` that made a value known), the class and these fields; `url-2048-characters-sent` and `nesting-128-sent` check that the bounds themselves pass, and the `*-check-skipped-without-models` cases that a check whose value is not held is left to the server.

Which class a response gives:

| Response | Class |
|---|---|
| 200 to 299 with a JSON body | success; nothing else is a success |
| 200 to 299 whose body is not JSON | `ServerError` |
| 300 to 399 | `DecisionNodeError` with the status. Redirects are never followed, so a key never goes to another URL or over plain HTTP |
| 400 | `BadRequestError`; `ContextLimitError` when `error_type` is `max_tokens_exceeded` |
| 401, 402, 404, 422, 429, 529 | `AuthenticationError`, `InsufficientCreditError`, `NotFoundError`, `ValidationError`, `RateLimitError`, `OverloadedError` |
| 413 | from 1.11 `RequestTooLargeError` (derived from `DecisionNodeError`), never retried |
| 403 | `RefusalError` when `error_type` is `refusal_error`, else `PermissionDeniedError` (`booster_not_enabled` and `workspace_suspended` included) |
| other 5xx | `ServerError` |
| any other status (405, 409, 418, 408 once its retries are spent) | the base `DecisionNodeError` with the status; there are no other classes |

Errors whose body is not JSON (from 1.11). The class follows the status alone (the table above: a 413 `RequestTooLargeError`, a 408 the base class, a 400 `BadRequestError` and never `ContextLimitError`, a 403 `PermissionDeniedError` and never `RefusalError`, a 422 `ValidationError` with empty `entries`), `error_type` is empty, `request_id` and `reason` come from their headers when sent, and retries follow the status as always (a 408 is retried, a 413 never). The status text, the same in every language and never the HTTP library's wording:

| Status | Text | Status | Text |
|---|---|---|---|
| 400 | Bad Request | 413 | Content Too Large |
| 401 | Unauthorized | 422 | Unprocessable Content |
| 402 | Payment Required | 429 | Too Many Requests |
| 403 | Forbidden | 500 | Internal Server Error |
| 404 | Not Found | 502 | Bad Gateway |
| 405 | Method Not Allowed | 503 | Service Unavailable |
| 408 | Request Timeout | 504 | Gateway Timeout |
| 409 | Conflict | 529 | Overloaded |
| 410 | Gone | | |

A JSON body without a message keeps the empty message, and a 2xx body that is not JSON stays a `ServerError`. A 429 whose body is not JSON is a `RateLimitError`, never a `SessionLimitError` (only the body's `error_type` `session_limit_exceeded` tells them apart), so it is retried as any 429. A batch result line without a `body` is read as a JSON null body: it is JSON, so its error keeps the empty message and the class its status gives.

Rules for reading an error response:

| Field | Source |
|---|---|
| `status` | the HTTP status |
| `error_type` | `detail.error_type` when `detail` is an object, else empty |
| `message` | the error's own message (the one every error of the language carries; there is no second message field): `detail.message` when `detail` is an object, `detail` itself when it is a string, the client's description when there was no response. When the server sent no message it is the empty string `""`, never null. From 1.11, when the body of an error status is not JSON (an edge's or a proxy's HTML or text page, or no body): `HTTP <status> <status text>`, then ` (request id <id>)` when `x-request-id` was sent; the status text from the fixed table below, `HTTP <status>` alone for a status not in it. The body is never read into any field |
| `entries` | `detail` when it is a list (422) |
| `category` | `detail.category` (403 refusal) |
| `request_id` | the `x-request-id` header |
| `reason` | the `x-decisionnode-error` header, as received |
| `retry_after` | seconds: `retry-after-ms` / 1000 when sent, else `Retry-After` in seconds or as an HTTP date, else empty; capped at 60 |
| `safety` | the parsed `x-decisionnode-safety` header |
| `boosters` | from 1.11: the `x-decisionnode-boosters` header (`age=ok:212,ai_generated=timeout:800`) parsed, each booster's name to `{"status", "latency_ms"}` in header order; empty when the header is absent. A required booster that failed is the `529` `booster_unavailable`, an `OverloadedError` retried as every `529` |

The safety header is a list of `name=value` members with optional `;parameters`. `decision.safety` is `{"checked": true, "harm": <number>, "self_harm": <number>, "flagged": [...]}`, where `flagged` lists, in header order, the categories that carry the `flagged` or `blocked` parameter. A header of the form `unchecked;reason="<code>"` gives `{"checked": false, "reason": "<code>"}`. No header gives an empty `safety`.

## Running each kind against an SDK

Every run uses a fresh session id (any unique string), sent as the client header `x-mock-session`. Add it, and `x-mock-scenario` where named below, through the client's `headers` argument.

**requests.** Build the client from `client` and `env`, add `x-mock-session` (and `x-mock-scenario: request/<id>` when the case has `mock`), make the call, then read `GET /_mock/requests?session=<id>`. With `expect.request` exactly one request must be recorded; with `expect.requests` the recorded requests must match the list in order, except that consecutive entries marked `parallel` (the PUTs of one upload) may match in any order. Match them by assignment, each entry to a recorded request not yet taken, backtracking when a later entry finds none (from 1.7): never by trying every order, since a case may hold sixteen parallel PUTs. Compare `method`, `path` or `path_pattern`, `query` (the parsed query string), `headers` (exact values), `header_patterns` (regular expressions), `absent_headers`, and the body: `body` (the recorded `body_text` parsed as JSON), `body_checks` (pointers with `equals`, `type`, `pattern`, `keys` or `absent`), `body_base64` or `body_sha256` (the raw bytes, from the record's `body_base64`), or `jsonl`, `jsonl_count`, `jsonl_checks` (a JSON Lines body, or a JSON array). An entry with none of them expects no body. The PUTs to the mock's storage are recorded in the session of the presign that issued their URL. The mock answers normally (or from the case's scenario), so the call itself succeeds.

**responses.** Make the call with the client headers `x-mock-scenario: response/<id>` and a session. The mock replays `response` byte for byte. Compare the result with `expect.decision` (`decide`), `expect.answer` (for `check`, `classify`, `score`, `number`), `expect.models` (each model's `name`, `description`, `release_date`, and `limits`: every other field of the entry, objects such as `sessions` and `batch` included; from 1.11 also `fine_tuned`, `base`, `adapter_version` and `status`, the fields of a fine-tuned model's entry, when the case lists them, empty for a catalogue entry), `expect.raw` (for `decide_raw`), `expect.batch` (the `Batch`, every field of the batch object), `expect.batch_page` (`batches.list`), `expect.batch_results` (one `BatchResult` per line: `custom_id`, `status`, `headers`, `decision` compared as a decision, `error` as an error value with `error` and `fields`, `no_decision`) or `expect.session` (the `Session`'s fields with its answers typed).

**errors.** Same with `x-mock-scenario: error/<id>` and the case's `client` (`max_retries: 0`). The call must raise exactly the class in `expect.error.class` with the listed `fields`. `natural_request` is for the mock's own tests and can be ignored by an SDK. The mock replays the recorded response whatever the request, so where the request that gets it is one the SDK now refuses locally, the case's call is `decide_raw` with that body, or the same method with arguments that pass the local checks; `natural_request` keeps the request the server answers that way.

When `expect.error` has `catch_as` (always `DecisionNodeError`), the harness catches the error with a catch clause written in source against that exported name, not with a catch-all followed by a name comparison, so the test does not compile or does not catch when the name is missing or is not a base of the class. Then it checks `class` and `fields` as usual. For example:

| Language | Catch by the name |
|---|---|
| Python | `except decisionnode.DecisionNodeError as err` |
| Node | `err instanceof DecisionNodeError` with `DecisionNodeError` imported by name |
| Go | `var e *decisionnode.DecisionNodeError; errors.As(err, &e)` (a type alias is enough) |
| Java, Kotlin | `catch (DecisionNodeError e)` |
| .NET | `catch (DecisionNodeError e)` (C# has no exported alias, so the base type carries that name) |
| Ruby | `rescue DecisionNode::DecisionNodeError => e` |
| PHP | `catch (\DecisionNode\DecisionNodeError $e)` |
| Rust | `let e: decisionnode::DecisionNodeError = result.unwrap_err();` (a `pub type` alias is enough) |

**behaviour.** Build the client from `client`, add a session and, when the case has `mock`, `x-mock-scenario: behaviour/<id>`. Time the call. A case has `call`, or `steps`: several actions on the same client, in order:

| Step | Action |
|---|---|
| `{"call": ..., "save_as": "b", "outcome": ...}` | make the call; check `outcome` when given (else it must not raise); keep the result as `b` for later `$ref`s |
| `{"connect": "s"}` | `Session.connect()` on the session saved as `s`. A refused handshake may raise `SessionClosed` here or come as the first event: both are recorded as the same event |
| `{"send": {"session": "s", "frame", "image", "questions", "reset", "seq"}}` | `Session.send(...)` with the arguments given (builders decoded). With `outcome` (from 1.5) the frame must be refused locally: `send` raises that error and nothing goes on the socket. With `"check": true` (from 1.6) the harness calls `Session.check_frame(...)` with the same arguments instead, and `outcome` is `{"result": "checked"}` or the error `send` would raise |
| `{"read": {"session": "s", "count": n}}` | read `n` events from `Session.replies()`; without `count`, read until the session closes |
| `{"end": "s"}` | `Session.end()`; recorded as the event `{"ended": true}` |
| `{"mock_clock": {"advance_seconds": n}}` | `POST /_mock/clock?session=<id>` with that body |
| `{"sleep_ms": n}` | wait |

Events are normalised as `{"reply": {seq, answers, dropped, usage, latency_ms, safety}}`, `{"frame_error": {seq, error_type, message, category, retry_after_ms, usage, safety}}`, `{"closed": {"code", "reopen"}}` and `{"ended": true}`. A `supervise` call takes `body`, `feed` (a list of readings, each the arguments of `send`) and `feed_interval_ms`: the harness passes a feed that yields the readings that far apart, and an `on_reply` that collects. Then read `GET /_mock/requests?session=<id>` and check:

- `attempts`: the number of recorded requests (API calls, storage PUTs, the WebSocket handshake and each message the client sent; pongs are not counted),
- `request_sequence`: every recorded request in order, by `method` (`WS` for a message on the socket) and `path` or `path_pattern`, with `status` and the checks of a request entry where given,
- `request_counts`: how many recorded requests have a method and path,
- `session_events`: every event in order, compared by the keys given,
- `pongs`: the number of pongs the mock received (`method` `PONG` in the record),
- `outcome`: `{"result": "decision"}` (optionally with `decision` fields), or `answer`, `batch`, `batch_page`, `batch_results`, `batch_run` (the `Batch` and its results from `run_batch`; from 1.6 either may give `count`, the number of results, and `batch_results_sample`, results checked by their 0-based `index`, in place of the whole list), `checked` (from 1.6: a check that found nothing), `session`, `upload_ids` (`count`), `supervised` (`replies` range, `closed`), an error class with `fields` (with `subclass_ok` any subclass of the class passes), or `{"argument_error": true}`,
- `one_of`: in place of `attempts` and `outcome` where the rule leaves the SDK a choice, a list of `{attempts, outcome}` pairs; the case passes when one pair holds whole,
- `results` (`decide_many`): one entry per request in input order,
- `gaps_ms`: the gaps between consecutive recorded `time` values, in milliseconds,
- `elapsed_ms`: the time the call took,
- `max_in_flight`: the peak number of concurrent requests the mock saw in the session,
- `scheduled_waits_ms`: the waits the client scheduled between attempts (and, from 1.4, between PUT retries, `run_batch` polls and `supervise` reopens). For these cases the harness replaces the client's wait with a recorder that notes each duration and returns at once (an injectable sleep or clock, which every SDK keeps for its own tests). That is how the 60 s cap is checked without the test taking 60 s. `gaps_ms` and `elapsed_ms` are not used in the same case,
- `unrequested_paths`: paths no request may have reached, in any session: `GET /_mock/requests?session=*&path=<path>` must return no requests. The redirect cases use it to prove the `Location` was not requested.

**Redirects with the caller's HTTP client.** A case whose `client` has `http_client: "caller_following_redirects"` checks that a redirect is never followed even through a client the caller made. The harness builds an HTTP client of the kind the SDK's `http_client` argument takes, set to follow redirects, and passes it, for example:

| Language | A client that follows redirects |
|---|---|
| Python | `httpx.Client(follow_redirects=True)` (or `httpx.AsyncClient(...)` for the async form) |
| Node | a `fetch` function that calls the global `fetch` with `redirect: "follow"` |
| Go | `&http.Client{}` (follows by default) |
| Java, Kotlin | `HttpClient.newBuilder().followRedirects(HttpClient.Redirect.ALWAYS).build()` |
| .NET | `new HttpClient(new HttpClientHandler { AllowAutoRedirect = true })` |
| Ruby | the HTTP client object the SDK accepts, with redirect following switched on |
| PHP | `new GuzzleHttp\Client(['allow_redirects' => true])` or the PSR-18 client the SDK accepts, set to follow |
| Rust | `reqwest::Client::builder().redirect(reqwest::redirect::Policy::limited(10)).build()` |

The SDK passes when it wraps or copies that client so the 3xx comes back as a `DecisionNodeError` with its status after one request, or refuses the client with an argument error before any request; either way nothing reaches the `Location`. A client or handler of a type the SDK cannot inspect counts as one that follows redirects and is refused. A caller's transport settings may not turn off TLS or certificate checks for an `https` base URL; the SDK refuses such a client with an argument error. An SDK that takes no HTTP client leaves the argument out and runs the case with its default client, which must give the first outcome. The reference runner passes a `urllib.request` opener, which follows redirects by default.

The wait the server asks for is `retry-after-ms`, else `Retry-After` in seconds or as an HTTP date, capped at 60 s. Without one, the wait is exponential backoff from 0.5 s with jitter, capped at 8 s.

**constants.** The SDK exports a public constant named `constant` (or the name in `names` for the language: `SpecVersion` and `DefaultInlineThreshold` in Go and C#) whose value equals `equals`. No request is sent.

## The mock server

```
python3 mock/server.py --port 0
listening on http://127.0.0.1:53817
```

The first stdout line is always `listening on http://<host>:<port>`, and nothing else is written to stdout. Options: `--port` (default 8787, `0` picks a free port), `--host`, `--cases <dir>` (default: this suite's `cases/`), `--api-key <key>` (accept only that key), `--no-safety` (answer like a self-hosted server, without `x-decisionnode-safety`), `--verbose` (log every request and every fault to stderr), `--log-file <path>` (append them to that file instead; stderr stays silent). Without either the mock writes nothing to stderr, its own faults included.

Built to stay up through a whole run (from 1.5): a listen backlog of 128, so many simultaneous connects are never refused; keep-alive connections idle for 60 s are closed, so the pools of many short-lived clients do not pile up threads (a WebSocket is exempt after the upgrade); a fault inside the mock answers `500` with `{"detail": {"error_type": "mock_error", ...}}` instead of dropping the connection, and a fault on a session's socket closes it with `1011`; a client that drops a socket mid-stream, or sends a malformed or oversized frame, ends that socket only.

**`POST /v1/decide`** validates like the hosted API and answers deterministically: each answer is derived from a hash of the resolved model, the state, the images and the question itself, so the same request always gets the same answer and a question's answer does not depend on the others in the request.

- Question types `truth`, `choice` (1 to 255 options), `score` (1 to 10 levels, as a list or an object keyed `"0"`..`"K-1"`). A truth question needs instructions or criteria.
- From 1.8, `point` and `box` on `/v1/decide` and batch lines: a request with one needs an image, `image` names one of the request's images (required with two or more), `units` is `pixels` or `normalized`, each fault a `422` entry (`image_required`, `missing`, `string_type`, `unknown_image`, `literal_error`); without instructions the plain `400`. The answers are deterministic places on an image the mock takes as 1920 by 1080 pixels: integers in pixels, fractions with 4 decimals when normalized, `present` and `confidence` with 2 decimals. A session's open takes them without the image rules (its images come with the frames). From 1.12 a point or box question takes `max`, an integer from 2 to 16 (`int_type`, `greater_than_equal`, `less_than_equal` at the question's `max`), and is answered in the several-places shape: `count` places in left-to-right order by the centre, with `present` and `complete`. `GET /v1/models` lists `places: {"max_places": 16}` on every entry; the scenario setting `places` (`{"max_places": 0}`) turns the capability off, and then the listing leaves `places` out and a question with `max` is the gateway's `422` `not_supported`, on `/v1/decide` and batch lines alike.
- `state` is a string, an object, an array, `null` or absent (both read as `""`); anything else is a 422.
- `questions` must be a non-empty object; `images` up to 16 (from 1.7; 4 before; from 1.9 more is the `422` `too_long` on `["body", "images"]`, and more than 4 videos on `["body", "videos"]`), each with `data` and `media_type` (`image/jpeg`, `image/png`, `image/webp`, checked against the bytes) or an `https` `url` (from 1.5 also refused with `image_url_refused` when over 2,048 characters, with user information, on a port other than 443 or on a literal address in a blocked range; the URL is never fetched).
- From 1.5: a body nested more than 128 objects and arrays deep is the 400 `Invalid request.`, on every path that takes a body and in session frames; the reason for an unknown question type lists `truth, noul, choice, score and number`.
- Models (from 1.11): `decisionnode-moe` and the older `decisionnode-1.0` and `decisionnode-latest` resolve to the mock's pinned MoE id, `decisionnode-flash` and the older `decisionnode-1.0-flash` and `decisionnode-flash-latest` to its pinned Flash id (`decisionnode-<family>-<version>`, the version the mock's own; no case asserts it), and every answer names the pinned id. A missing model is `decisionnode-moe`; an unknown one is a 400.
- Fine-tuned models (from 1.11): the workspace `acme` (a key holding `ws-acme`) has `decisionnode-moe@acme/fraud` with version 3 deployed, so `decisionnode-moe@acme/fraud` and `decisionnode-moe@acme/fraud:3` (and the older base, `decisionnode-1.0@acme/fraud`) are answered with `model` and `x-decisionnode-model` the pinned `decisionnode-moe@acme/fraud:3`, and `decisionnode-flash@acme/triage` with version 1 ready (trained, not deployed). Any other version, a ready one, and every fine-tuned name sent by a key of another workspace are the 400 `Unknown model: <name>`.
- Boosters (from 1.11): `age` and `ai_generated` read the request's images (all of them, or the booster's `images`) and answer fixed readings derived from each image, so the same request always gets the same `evidence`. An `ai_generated` reading is read from the pixels only (`method` `inference`, no signer, no `c2pa` field) and names a `top_generator` only when `generated` is above 0.69. The checks run before any booster, in this order: the list's shape (more than 4 entries, a booster named twice, an entry that is neither a name nor an object with `name`, a key of the object other than `name`, `images`, `require` and `options`, `images` that is not a list of ids, a `require` that is not a boolean, any key in `options`: the 400 `api_usage_error` `Invalid request.`, the reason in `x-decisionnode-error`), then each entry in order: an unknown name (`400` `booster_unknown`), a booster the session's settings turn off (`403` `booster_not_enabled`), a request without images or an `images` entry that is no image of the request (`400` `booster_input_mismatch`). The settings may make a booster `timeout` or `error`: its entry lists `{"image": id}` for each picture it was meant to read, bills 0, and the decision runs without its reading, unless the booster has `require: true`, which makes it the `529` `booster_unavailable` with `Retry-After` and `retry-after-ms`. A booster given `images: []` is `skipped`. The response adds `evidence` (in the request's order; each entry's `billed_calls` is the images it read with status `ok`; a reading reused from a cache is billed too) and `usage.booster_tokens` (the mock's estimate, part of `input_tokens`) and `usage.boosters` (one per booster and image read with status `ok`). Once the list passed its checks, every response carries `x-decisionnode-boosters`, a later refusal included. Batch lines take boosters as a live request does; `POST /v1/sessions` refuses them with the 400 `api_usage_error` `boosters are not available on sessions`.
- From 1.7, `videos` (on `/v1/decide` and batch lines) in the frames form: up to 4 videos, ids unique across images and videos, frames `{t, media_type, data}` with times strictly increasing, `fps` above 0 and at most 4, `start` and `end` of at least 0 and `end` after `start`, each refused with the API's 400 `api_usage_error` and its message; a video file or URL is refused (`video files and URLs are not taken`); more than 128 frames sent over a request's videos is a 400 (the API's limit, which no SDK checks; 64 in suites 1.7 and 1.8); each frame inside the window `[start, end)` must be valid base64 whose first bytes are its `media_type`, and a window without a frame is a 400. Frames are read by the API's sampling rule (a frame is read when it is the first in the window or at least 1/fps seconds, less a millisecond, after the frame read before it; `fps` 2 by default), and the response adds `usage.video_frames` (frames read) and `usage.video_seconds` (from the window's first frame, or `start`, to `end`, or the last frame plus the median gap between frames). The mock sends `warnings` only from a scenario.
- The token count is the mock's own estimate (about four bytes a token, 765 per image, 65 per video frame read); over the model's limit is a 400 `max_tokens_exceeded`.
- Error bodies: 422 `{"detail": [entries]}`, 400 `{"detail": "<text>"}` or `{"detail": {"error_type": ..., "message": ...}}`, 401 `authentication_error`, 404 `not_found_error`, 405 with `Allow`. Every refusal carries `x-decisionnode-error`.
- Answers follow the API's number rules: two decimals, probability lists adjusted to sum to 1, with the adjusted entries keeping the raw float of the addition.
- From 1.11 every answer's `usage` carries `cost` (`usd`, `model_usd`, `boosters_usd`, `free_tier`) at the mock's own prices: MoE 0.042 and Flash 0.021 USD per million input tokens, a batch line at half that, a fine-tuned model 0.02 more, 0.002 a booster call billed, and no free tier.
- Every response carries `x-request-id`. A decision carries `x-decisionnode-model`, `x-decisionnode-latency-ms` and `x-decisionnode-safety`; the request header `x-decisionnode-safety-body: true` adds a `safety` field to the body.
- Any non-empty `Bearer` key is accepted, except keys starting with `invalid`, which get a 401.
- From 1.11 a `/v1/decide` body over 26,214,400 bytes (25 MiB) is the `413` `request_too_large_error`, and the connection is closed.

- Question types on `/v1/decide`: `truth`, its alias `noul` (answered `{"type": "noul", "noul": x}`, the same value as `truth`), `choice`, `score` and `number`: the grid built in exact decimals from the JSON text, its 422 entries and 400s, and answers encoded as the API writes them (`number` as the grid writes it, `expected` to the grid's decimals, `probabilities` above 0.001 to 3 decimals in ascending order, the sum cap). A truth question's answer is a `noul` answer while `/_mock/truth` (or the scenario's `truth_as_noul`) is on.
- An image may name an upload: `{"id", "upload_id"}`, resolved as the API resolves it, with its errors (`upload_not_found` for an unknown id or another workspace's, `upload_consumed`, `upload_expired` by the session's clock, `upload_media_type_mismatch`, `upload_incomplete`, `upload_mismatch`). A key holding `ws-<name>` is in workspace `<name>`; every other key is in `default`. Under zero retention an upload is consumed by the first request the model answered.


**`GET /v1/models`** lists the family ids and the pinned ids served (from 1.11; the older ids are accepted, never listed) with `name`, `description`, `release_date`, from 1.6 `version` (the pinned id the name resolves to) and `alias` (the family id that moves with releases), and the mock's limits (`max_input_tokens`, `max_choice_options`, `max_score_levels`, `max_images`, `max_image_bytes`), the number question fields, `upload_max_bytes`, `upload_ttl_seconds`, `upload_media_types`, and the `sessions` and `batch` objects. The values are the mock's. From 1.11 a key of the workspace `acme` also gets that workspace's fine-tuned models after the catalogue: the plain name of a model with a deployed version, every deployed or ready version under its pinned name, each with its base's limits, `version` the pinned name it resolves to, and `fine_tuned` (`true`), `base`, `adapter_version` and `status` (`deployed` or `ready`).

**`POST /v1/uploads`** checks the files as the API does (1 to 16 files from 1.7; 1 to 4 before) and answers `201` with URLs on the mock itself: `PUT /_mock/s3/uploads/<workspace>/<key>?...&X-Amz-Signature=<token>`. The storage checks the token, that the URL is under 300 s old (by the session's clock), `Content-Type`, `Content-MD5`, `If-None-Match: *`, `x-amz-checksum-sha256` when signed and `Content-Length` against the presign (`403 SignatureDoesNotMatch`), the MD5 and SHA-256 of the bytes (`400 BadDigest`), and refuses a second write (`412 PreconditionFailed`), with S3's XML error bodies. It records whether an `Authorization` header arrived (`GET /_mock/uploads`).

**Sessions**: `POST /v1/sessions` (validation, the open response, `429 session_limit_exceeded` at four open sessions per key and mock session), `GET /v1/sessions/{id}/stream` (a WebSocket written on the standard library: the handshake, then `4401`, `4404` or `4409` at once when refused, frames and their validation errors, replies, latest wins with `dropped`, server pings every 20 s, the idle close `4408`, the TTL close `4410`) and `DELETE /v1/sessions/{id}`. A message over 1 MiB closes the socket with `1009` (from 1.5; 16 MiB before). The scenario's `session` object sets `max_per_key`, `frame_delay_ms`, `hold_until_frames` (hold answers until that many frames arrived, so latest wins is deterministic), `ping_interval_ms`, `idle_timeout_ms`, `frame_errors` (by `seq`: `rate_limit_error`, `overloaded_error`, `api_error`, `refusal_error` with `category`) and `close` (`{"after_replies": n, "code": c}` or `{"on_connect": c}`; `1006` drops the connection without a close frame).

**Batches**: the seven endpoints with JSON Lines or array uploads, every `custom_id` rule, lines refused at upload kept as failed lines with their live body, upload references copied at add, ten active batches per key, and results as JSON Lines (with `ETag`) or one page with `format=json`. The lifecycle is driven by retrieves, so a test never waits: after finalize, the first `GET` makes the batch `running` (half its lines answered) and the second ends it `completed`; the scenario's `batch` object (or `POST /_mock/batches`) sets `polls_to_running`, `polls_to_end`, `end_state` (`completed`, `failed`, `expired`, `cancelled`), `running_in_progress` (lines left in progress while running, for a cancel's wind-down) and `get_retry_after`. Expiry (24 h after finalize) and the results' 7 days follow the session's clock.

**Collections** (from 1.13): the seven calls of a workspace's face collections, kept per mock session and workspace (another workspace's id answers as an unknown one, `404` `collection_not_found`). `celebrities` is always listed after the workspace's own collections, `wanted-persons` only when the scenario setting `collections` says `{"wanted_persons": true}`; both are read-only (`403` `collection_read_only` for an enrollment, an items list or a delete). An enrollment finds 1 to 3 faces, or the number the setting's `faces` gives (0: the `422` `no_face`), and answers a deterministic box; names, references and labels are checked as the API does (`literal_error`, `string_too_short`, `string_too_long`, `name_taken`, `string_pattern_mismatch`). The two collections POSTs take `Idempotency-Key` (1 to 255 printable ASCII characters, the workspace's, kept 24 hours, bound to the route and the body's SHA-256): a repeat replays the first answer with `x-decisionnode-idempotent-replay: true`, enrolling nothing; only the `201`s and a billed `422` `no_face` are kept, any other refusal lets the key go; the same key while the first is running is the `409` `idempotency_in_progress` with `Retry-After: 1`, with another body or route the `409` `idempotency_key_reused`, and a key not 1 to 255 printable ASCII characters, or sent twice, the `400` `api_usage_error`, each with the API's message. The mock serves the `face_recognition` booster: `collection` is required (a public collection's name or one of the workspace's own ids; an unknown or foreign one is the `404` `collection_not_found`, `wanted-persons` where it is not enabled the `403` `collection_not_enabled`, refused before any reading), `threshold` a number from 0 to 1 (default 0.5); anything else is the `400` `api_usage_error`. Its reading finds one or two faces per picture, the first a close match and the second a weak one, each kept at the threshold or above: an own collection's match carries an enrolled item's `reference` and its `label`, a celebrity's a `name`, a wanted person's a reference only. A collection's id is derived from the workspace and the name, and an item's from the collection, the reference and its order, so a case can expect them. Every other booster still takes no option.

### Scenarios

A request with the header `x-mock-scenario: <name>` is answered from that scenario instead of the normal handler. Progress through a scenario is kept per `x-mock-session` (default `default`), so parallel tests do not interfere.

Every case file becomes a scenario at start-up: `response/<id>` and `error/<id>` replay the recorded response on every request; `behaviour/<id>` is the case's `mock.scenario`. More can be registered at run time:

```
POST /_mock/scenarios
{"name": "overloaded-twice",
 "steps": [
   {"status": 529, "headers": {"retry-after": "1", "retry-after-ms": "200"}, "body": {"detail": {"error_type": "overloaded_error", "message": "Overloaded."}}},
   {"status": 529, "headers": {"retry-after-ms": "200"}, "body": {"detail": {"error_type": "overloaded_error", "message": "Overloaded."}}}
 ],
 "after": "normal"}
```

A step has `action`: `respond` (the default when `status` is given: send `status`, `headers` and `body` or the exact `body_text`), `normal` (answer like the API) or `close` (drop the connection with no response), an optional `after_normal: true` (do the work as the API would, then answer with this step: a stored object or a finalized batch whose answer was lost), an optional `no_request_id: true` (from 1.11: send no `x-request-id`, as an edge's own page has none; a step whose headers name none otherwise gets one, and a recorded response or error case is replayed as recorded), an optional `delay_ms` before it acts, an optional `advance_clock_seconds` (from 1.5: before the step acts, the session's clock moves forward that many seconds, so a PUT meets an upload URL presigned that long ago), and an optional `trickle` `{"chunks": n, "interval_ms": m}`: the status and headers go out at once, then the body in `n` pieces with `m` ms before each, which is how the suite proves the timeout covers the whole attempt and not each read. In a header value, `{mock_url}` becomes the mock's base URL and `{http_date+N}` becomes the HTTP date N seconds after the mock answers (`{http_date-N}` before), which is how `Retry-After` as a date is tested. Steps are used one per request in arrival order. When they run out, `after` decides: `normal` (default) or `repeat_last`.

A scenario with `rules` matches each request by JSON pointers into its body, for example `{"match": {"/state": "b"}, "steps": [...]}`, and by `$method`, `$path`, `$path_prefix` and `$path_suffix`, with its own step sequence per rule. A request no rule matches uses `otherwise`, or is answered normally. `decide_many` cases use rules to give each request its own response; upload cases use them to answer the presign, the PUT or the decision. A PUT to the mock's storage uses the scenario of the presign that issued its URL, since the PUT carries none of the client's headers.

A scenario may also hold settings, applied to its mock session from the first request that names it: `clock_offset_seconds`, `workspace` (`{"zero_retention": true}`), `truth_as_noul`, `batch`, `session` (above) and, from 1.11, `boosters` (`{"disabled": ["age"], "status": {"ai_generated": "timeout"}}`: boosters turned off for the workspace, and a booster's outcome, `timeout` or `error`). A scenario of settings only answers every request normally. From 1.12 also `places` (`{"max_places": 0}`: the models answer one place only). From 1.11 also `workers` (`{"cold": ["decisionnode-moe"], "retry_after_ms": 50}`, a model named by any id it is accepted under): models with no ready worker. A live `/v1/decide` for a cold MoE model (by `decisionnode-moe` or an older id) is answered by the pinned Flash id, with `x-decisionnode-fallback` (the pinned id that answered) and `x-decisionnode-requested-model` (the canonical form of what was asked: a family id or any alias of one gives the family id, a pinned id stays that pinned id); with `"fallback": false`, for a pinned id (answered only by that version, never by a fallback), or for any other cold model, it is the `529` `overloaded_error` with `Retry-After` and `retry-after-ms`. Batch lines and sessions never fall back.

### Control endpoints

| Endpoint | |
|---|---|
| `GET /_mock/health` | `{"ok": true, ...}` |
| `GET /_mock/requests?session=<id>[&path=<path>]` | `{"requests": [{"time", "method", "path", "headers", "body_text", "body", "scenario", "attempt", "status"}], "max_in_flight": n}`; `time` is in seconds, `headers` lower-cased, `body_base64` the raw body (from 1.4). WebSocket messages the client sent are recorded with `method` `WS`, pongs with `PONG`. `session=*` reads every session; `path` keeps only the requests at that path |
| `POST /_mock/reset[?session=<id>]` | forget recorded requests, scenario progress and the session's uploads, sessions, batches, clock and flags |
| `GET /_mock/scenarios`, `POST /_mock/scenarios` | list and register scenarios |
| `POST /_mock/clock?session=<id>` | `{"advance_seconds": n}` or `{"offset_seconds": n}`: the session's clock (upload and URL expiry, batch times, session TTL) |
| `GET /_mock/clock?session=<id>` | from 1.5: `{"epoch_seconds", "offset_seconds", "now"}`, the session's clock; what a harness hands an SDK as its clock for a case with `client.clock: "mock"` |
| `POST /_mock/workspace?session=<id>` | `{"zero_retention": true}` |
| `POST /_mock/truth?session=<id>` | `{"legacy_noul": true}`: truth questions answered as `{"type": "noul", "noul": x}` |
| `POST /_mock/batches?session=<id>`, `POST /_mock/sessions?session=<id>`, `POST /_mock/boosters?session=<id>`, `POST /_mock/workers?session=<id>` | the batch lifecycle, session behaviour and (from 1.11) booster and worker settings, as in a scenario |
| `GET /_mock/uploads?session=<id>` | the session's uploads: state, puts, whether the bytes are stored, whether an `Authorization` header reached the storage |

The control endpoints need no key. The `x-mock-*` headers are the mock's only; the hosted API ignores unknown headers.

## The reference runner

```
python3 runner/run.py                  # start a mock, validate every case file against its schema, run all cases
python3 runner/run.py --only behaviour --case retry-408 -v
python3 runner/run.py --mock-url http://127.0.0.1:8787
python3 -m unittest discover -s tests  # the mock's unit tests
```

`runner/reference_client.py` is a minimal client in plain Python that implements the neutral surface of spec 1.3 (its WebSocket client is written on the standard library too). It exists to prove the cases are consistent and runnable, and is a worked example of the comparisons above. The runner also checks every error case that has a `natural_request`: the mock must produce the same status, body and reason (and `x-decisionnode-boosters` where the case records it) on its own.

## Adding a case

Add a JSON file whose name is its `id`, valid against the kind's schema, and run `python3 runner/run.py`. A change to the SDK surface lands as cases here first, so every language picks it up in the same release.
