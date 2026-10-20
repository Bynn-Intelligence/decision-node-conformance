# Changelog

## 1.23 (for SDK specification 1.4)

Knowledge: a workspace's own documents in collections of type `text`, the passages a decision applies, and search. `SPEC_VERSION` is `"1.4"`.

- Calls: `collections.create(name, type, scope_key)` takes `type` `text` and a `scope_key`; `collections.add_document(collection_id, document, title, reference, pinned, pin_rank, label, metadata, wait)` and `collections.replace_document(collection_id, item_id, ...)`, each with its own `Idempotency-Key` and `?wait=true` when asked; `collections.update_item` takes `pinned`, `pin_rank` and `title`; `collections.get_item(collection_id, item_id, include_text)`; `collections.list_chunks(collection_id, item_id, offset, limit)`; `collections.search(collection_id, query, scope, match, filter, top_k, pinned, max_tokens)`; `collections.wait_until_ready(collection_id, item_id, timeout, poll_interval)`; `upload_documents(documents)`. Each returns the API's object as sent; a search returns a `KnowledgeReading`.
- Documents: `Document(text, format)` inline whatever its size; a text or Markdown file under the inline threshold, in UTF-8, inline too (its title the file name's stem unless given); every other file uploaded with `"purpose": "knowledge"` (its type from `content_type`, the extension or the first bytes; up to 52,428,800 bytes; the PUT 120 s per attempt unless `upload_timeout` is set) and named by `upload_id`. A stale upload the call placed is uploaded again once and the item call sent with a new `Idempotency-Key`.
- The `knowledge` builder: `{"name": "knowledge", "require"?: true, "options": {"collection", ...}}`, every option sent as given and never checked locally.
- Readings: `KnowledgeReading` (`collection`, `collection_name`, `corpus`, `query`, `budget`, `scope`, `warnings`, `passages`, `omitted`, `pending_items`, `search`, `truncated`) with `KnowledgePassage`s (`n`, `pinned`, `item`, `reference`, `title`, `heading`, `chunk`, `byte_start`, `byte_end`, `tokens`, `score`, `lexical`, `rendered`, `text`), every value as sent; a reading or a passage of the wrong shape is generic.
- Local checks: a collection type other than `face` or `text`, a `scope_key`, a title, an empty text, `format`, `pin_rank`, a reference, a label and metadata as before, a file's type and size (the presign's `400` in its words), `list_chunks`' query, `wait_until_ready`'s arguments.
- Errors and retries: every `409` the base error with `details` (`knowledge_collection_empty`, `reference_taken`, `pinned_budget_exceeded`, `chunks_limit_reached`, `not_an_article`, `not_a_file`); `item_too_large` a `RequestTooLargeError`; `invalid_text` a `ValidationError`; `upload_purpose_mismatch` a `BadRequestError`, never uploaded again; a `529` `booster_unavailable` with `details.reason` `ingestion_delayed` never retried; the ingestion `429` and the other `529`s retried as before; `wait_until_ready` returns a failed item as a value.
- Changed: `local-collections-type-not-face` expects "Input should be 'face' or 'text'"; `400-booster-unknown` lists `knowledge` among the boosters; `spec-version` is `"1.4"`.
- Case format: `$knowledge` and `$document`; methods `collections.add_document`, `collections.replace_document`, `collections.get_item`, `collections.list_chunks`, `collections.search`, `collections.wait_until_ready` and `upload_documents`; results `chunk_page` and `knowledge`; the reading kind `knowledge`; `distinct_header` in a behaviour case's expectations.
- Mock: text collections, their items (text and by knowledge upload), chunks, search and the `knowledge` booster with deterministic readings; knowledge uploads; ingestion moved by reads, so waiting needs no clock; the scenario setting `knowledge`.

Cases: 979 in all (requests 167, responses 140, errors 169, behaviour 501, constants 2): 124 new (25 requests, 18 responses, 28 errors, 53 behaviour), 3 changed.

## 1.22 (for SDK specification 1.3)

A Segment Vision opening's `offset` corrected. `SPEC_VERSION` stays `"1.3"`; no rule changes.

- An opening's `offset` is its centroid less the centre of the match's `box` (`[dx, dy]`, 4 decimals), as the model writes it, not its distance from the middle of the picture. The mock's readings, the README and the reference client's description say so.
- Values: the openings in `evidence-segment-reading`, `evidence-segment-depth-off`, `evidence-segment-malformed-is-generic`, `decide-booster-segment-options-sent` and `boosters-vision-answered-by-mock` now carry offsets measured from their boxes (the opening's outline and centroid moved with it). Every SDK keeps an opening's `offset` as sent, so no client changes.

Cases: 855 in all (requests 142, responses 122, errors 141, behaviour 448, constants 2): none new, 5 with new values.

## 1.21 (for SDK specification 1.3)

Depth Vision (`bynn_depth`) and Segment Vision (`bynn_segment`), two boosters listed in production. `SPEC_VERSION` stays `"1.3"`.

- Readings: `DepthReading` (`image`, `ahead`, `directions`, `ground`, `grid_rows`, `grid_cols`, `grid`, `depth_min`, `depth_max`, `depth_median`, `width`, `height`; `ahead` and each of the seven directions empty when nothing was found) and `SegmentReading` (`image`, `width`, `height`, `prompts`, `instances`, `depth_source`, `assumed_hfov_deg`), each instance a `SegmentInstance` (`label`, `score`, `box`, `polygon`, `area`, `centroid`, and `opening`, `depth` and `size_m`, each empty when null or absent). A reading with a field missing or of the wrong shape, one wrong instance included, and a picture the booster could not decode are generic readings. Every number as sent.
- Requests: each booster's options are sent in the object form as given (`grid_size`; `prompt`, `max_instances`, `min_score`, `polygon_points`, `depth`), never checked or cleaned locally, `check_request` included.
- Errors: the API's `400` `api_usage_error` "Invalid request." for a bad option, its reason in the API's words (`boosters[<i>]: "options": 'grid_size' must be an integer from 2 to 16`, `'prompt' is required`, `'prompt' must name something to find`, the prompt's shape, `max_instances`, `min_score`, `polygon_points`, `depth`, and `'<key>' is not an option`), each key judged in the order sent, the first problem the reason.
- Case format: the reading kinds `bynn_depth` and `bynn_segment`.
- Mock: both boosters served with fixed readings per image (Depth Vision at its `grid_size`; Segment Vision's phrases cleaned as the API cleans them, its matches with outlines, openings and, with depth on, distances and sizes); every booster's options judged as the API judges them, in its words (`age`'s unknown option now reads `boosters[0]: "options": 'k' is not an option`); booster calls priced per booster (Depth Vision 0.001 USD a picture, Segment Vision free); `booster_unknown`'s reason lists the five boosters.

Cases: 855 in all (requests 142, responses 122, errors 141, behaviour 448, constants 2): 15 new (2 requests, 5 responses, 6 errors, 2 behaviour); `400-booster-unknown` and `nesting-128-in-boosters-sent` record the new reasons.

## 1.20 (for SDK specification 1.3)

A keys key's decide keys, `/v1/keys`. `SPEC_VERSION` stays `"1.3"`.

- Calls: `keys.create(name, device_id, labels, limits, daily_spend_cap, expires_at, may_search_collections, idempotency_key)`, with an `Idempotency-Key` (the one given, else a new UUID per call, the same on every attempt), so a retry or a repeat answers the same key and secret; `keys.list(labels, device_id, status, limit, cursor)` and `keys.all(...)`, which follows `next_cursor`; `keys.update(key_id, ...)`, a field given as null clearing it; `keys.revoke(key_id)`; `keys.revoke_by_labels(labels, device_ids, dry_run)`. Each returns the API's object as sent.
- Local checks: a name's length, `device_id`, the labels, the shape of `limits`, the daily spend cap, an update with nothing to change, the Idempotency-Key, `keys.list`'s filters and limit, a bulk revoke without a label or with over 1,000 devices, each as the API answers it. A limit's ceiling, the expiry's window and a name's characters are the server's.
- Retries: a `429` `key_spend_cap_reached` is never retried (the cap resets at 00:00 UTC), on every call; a create answered the `409` `idempotency_in_progress` waits its `Retry-After` and tries again with its key.
- `GET /v1/keys/{id}` is not a call: the API has no such route.
- Errors: the `403` `key_scope_denied` for a full, a decide or a publishable key; the `404` for a key the keys key does not manage; the `409`s `already_revoked`, `key_expired`, `key_not_replayable` and `idempotency_key_reused`; the `422`s `name_taken`, `device_id_taken`, a limit over the workspace's and an expiry out of its window; the `429` `rate_limit_error` with `details.reason` `key_creation_rate`; the `429` `key_spend_cap_reached` with its `details`.
- Case format: the methods `keys.*`; the results `created_key`, `api_key`, `key_page`, `api_keys`, `key_revoked` and `keys_revoked`; a natural request's method `PATCH`.
- Mock: `/v1/keys` for a key holding `scope-keys`, per session and workspace, seeded with the usage fixture's decide keys.

Cases: 840 in all (requests 140, responses 117, errors 135, behaviour 446, constants 2): 46 new (7 requests, 2 responses, 14 errors, 23 behaviour).

## 1.19 (for SDK specification 1.3)

Publishable keys. `SPEC_VERSION` stays `"1.3"`.

- Errors: a publishable key (`dn_pub_`) outside decisions, its usage and the models list included, is the `403` `key_scope_denied` "A publishable key may only make decisions."; from an origin it does not list, or without one when it allows no native app, the `403` `origin_not_allowed`; asking a model it does not list, the `403` `model_not_allowed` with `details.allowed`; its rate per address and its calls in flight, the `429` `rate_limit_error` with `details.reason` `client_rate` or `in_flight`, retried as every `429`. Each is a `PermissionDeniedError` or a `RateLimitError` read as sent; nothing is checked locally.
- Mock: a key starting `dn_pub_` is publishable, held to its origin and, holding `flash-only`, to Flash.
- The README says the versioning rule: every change is the next minor version, never a patch.

Cases: 794 in all (requests 133, responses 115, errors 121, behaviour 423, constants 2): 11 new (8 errors, 3 behaviour).

## 1.18 (for SDK specification 1.3)

Per-key usage. `SPEC_VERSION` stays `"1.3"`.

- Calls: `usage.key(key_id, start, end, granularity, group_by)` (`GET /v1/keys/{key_id}/usage`), `usage.list(start, end, granularity, group_by, labels, device_id, key_ids, scope, status, include_empty, limit, cursor)` (`GET /v1/usage`) and `usage.all(...)`, which follows `next_cursor` until it is null, since a page can hold fewer keys than `limit`. Each returns the API's object as sent; reads retry as any call.
- Local checks: the rules the API checks as it reads the query (`granularity`, `group_by`, the label filter, `device_id`, a key id and at most 100 of them, `scope`, `status`, `limit`), each the `400` `api_usage_error` with the API's message. The period's rules, the cursor and the key's `404` are the server's.
- Errors: the `403` `key_scope_denied` for a decide key; the `404` `not_found_error` "Not found." for a key outside the caller's set, another workspace's, unknown or malformed; the `429` `rate_limit_error` of the reads' own rate; the `529` `overloaded_error`.
- Case format: the methods `usage.key`, `usage.list` and `usage.all`; the results `key_usage`, `usage_page` and `usage_items`; a request's `query_all`, every parameter with all its values.
- Mock: both routes over a fixed set of keys and hourly usage per workspace, as the API checks and sums them; a key holding `scope-keys` is a keys key, refused with "This key may only manage keys." on every other route.

Cases: 783 in all (requests 133, responses 115, errors 113, behaviour 420, constants 2): 48 new (5 requests, 2 responses, 11 errors, 30 behaviour).

## 1.17 (for SDK specification 1.3)

The batch price is each model's, never the batch's. `SPEC_VERSION` stays `"1.3"`.

- Types: `Batch` has no `price_factor`. The API does not send it, the batch price being each `/v1/models` entry's `batch_price_factor`, and each line's charge is in its usage. A `ModelInfo` carries the entry's prices as sent: `price_per_million_input`, `price_per_million_output`, `price_per_million_image`, `price_per_million_video` and `batch_price_factor` (none on a fine-tuned model).
- Case format: an expected `{"$absent": true}`, the key must not be there at all.
- Mock: `price_factor` gone from the batch object and from `/v1/models`' batch object, with `price_per_million_input_tokens`; each `/v1/models` entry states the prices under the API's names, `batch_price_factor` MoE 0.7 and Flash 0.5 (none on a fine-tuned entry, whose prices carry its premium); a batch line pays its own model's factor (MoE lines paid 0.5 before).
- Cases: every `price_factor` gone from the batch cases and the recorded `/v1/models` answers.

Cases: 735 in all (requests 128, responses 113, errors 102, behaviour 390, constants 2): 2 new (a batch object with no `price_factor`, the prices of `/v1/models`), 21 without `price_factor`.

## 1.16 (for SDK specification 1.3)

The `face_recognition` reading names its collection and carries an own item's metadata. `SPEC_VERSION` stays `"1.3"`.

- Types: a `face_recognition` match gains `collection_name` (an own collection's display name, or its id; a public collection's id) and `metadata` (an own item's, an object as sent); the reading gains `collection_name` (an own collection's display name). Each is empty when absent, as in an answer from before these fields; one of the wrong type makes the reading generic.
- Mock: the reading sends the three fields; a collection name or an item label holding a control or invisible character is the `422` `string_pattern_mismatch`, as the API answers.

Cases: 733 in all (requests 128, responses 111, errors 102, behaviour 390, constants 2): 4 new (a reading with its collection's name and an item's metadata, a public match's name, an own collection without a display name, the new fields malformed); the face_recognition readings now expect the three fields, empty in the recorded answers from before them.

## 1.15 (for SDK specification 1.3)

Items found by reference, edited and described, the enrollment's 529 reasons, decide keys, and `wanted-persons` no longer offered. `SPEC_VERSION` stays `"1.3"`.

- Calls: `collections.update_item(collection_id, item_id, reference, label, metadata)`, `PATCH /v1/collections/{id}/items/{item_id}` with only the fields given (`label` given as null clears it, `metadata` replaces the whole object), carrying an `Idempotency-Key` and retried as `create` and `enroll`; `collections.enroll` takes `metadata`; `collections.list_items` takes `reference` and `reference_prefix`. Each result stays the API's object as sent: an item now carries `metadata`, an items page `total_capped`, `reference` and `reference_prefix`.
- Local checks: `update_item` with no field, a reference and a label as `enroll` checks them; `metadata` not an object, over 20 keys, a key not 1 to 40 of `a-z`, `0-9` and `_`, a text over 500 characters, a value that is not a text, a number or a boolean, each the API's `value_error` entry; `list_items` with both `reference` and `reference_prefix`, or either not 1 to 64 of `[A-Za-z0-9_.:-]`, the `400` `api_usage_error` with the API's message.
- Errors: a new field `details`, `detail.details` when it is an object. An enrollment's `529` with `detail.details.reason` `enrollment_unpriced` (no `Retry-After`) is never retried; `face_model_unavailable` is retried as every `529`. The `403` `key_scope_denied` of a decide key, on a route other than a decision's and the models list or on its `face_recognition`, is a `PermissionDeniedError`.
- `wanted-persons` is not offered: absent from `collections.list()`, a read of it or of its items the `404` `collection_not_found`, a `face_recognition` naming it the `403` `collection_not_enabled`. The scenario setting `wanted_persons` is gone.
- Mock: item metadata checked as the API checks it, `PATCH` on an item, the items list's `reference` and `reference_prefix` (a filtered page in reference order, its count capped at 10,000), the setting `enrollment` (`unpriced` or `face_model_unavailable`), decide keys (a key holding `scope-decide`, with `may-search` to search collections), and a recorded request's method `PATCH`.

Cases: 729 in all (requests 128, responses 107, errors 102, behaviour 390, constants 2): 32 new, `collections-get-wanted-persons-not-enabled` replaced by `collections-get-wanted-persons-not-found`, `face-recognition-wanted-persons-by-mock` removed, and the public collections' cases without `wanted-persons`.

## 1.14 (for SDK specification 1.3)

Collections' `422`s as the API answers them. `SPEC_VERSION` stays `"1.3"`.

- Local checks: an empty reference, or an empty label when one is given, is `string_too_short` (`min_length` 1), the length being checked before the pattern; a too-long name, reference or label echoes its first 64 characters with `...` added in the entry's `input`, as the API does; an entry never echoes an object or an array (null), so a missing image's `input` is null.
- Mock: a collections POST's body is checked first, so its `422` comes before an `Idempotency-Key`'s `400` or `409` and before an enrollment's `404` or `403`, and never takes the key; then the key, then the collection, the region (an enrollment's `403` `collections_region_unavailable` too) and the limits. Each `422` input is echoed within those bounds; `name_taken` carries the API's message.

Cases: 698 in all (requests 122, responses 104, errors 97, behaviour 373, constants 2): `local-collections-label-empty` new; `local-collections-reference-empty` is `string_too_short`; the three too-long cases echo 64 characters and `...`.

## 1.13 (for SDK specification 1.3)

Collections and face recognition. `SPEC_VERSION` stays `"1.3"`, unreleased, so the change folds into it.

- Calls: `collections.create(name, type)`, `collections.list()`, `collections.get(collection_id)`, `collections.delete(collection_id)`, `collections.enroll(collection_id, image, reference, label)`, `collections.list_items(collection_id, offset, limit)` and `collections.delete_item(collection_id, item_id)`, each returning the API's object as sent; `list()` returns the body's list. `list_items` checks `offset` and `limit` locally, as `batches.list` does.
- Local checks, the limits a client may hold: a type other than `face`, a name not 1 to 80 characters, a reference not 1 to 64 of `[A-Za-z0-9_.:-]`, a label over 128 characters, each a `ValidationError` with the API's entry before any request; the enrollment's image is checked as a decision's.
- Idempotency: `create` and `enroll` send `Idempotency-Key`, a new UUID (version 4) per call, the same on every attempt, and so retry like any call, a `409` `idempotency_in_progress` too (after its `Retry-After`); a `409` `idempotency_key_reused` is raised.
- The idempotency messages: the 400 for a malformed key or one sent twice, the 409s with their sentences in `x-decisionnode-error`, and `idempotency_in_progress` with `Retry-After: 1`; a recorded `idempotency_in_progress` case.
- A reference is checked against `^[A-Za-z0-9_.:-]{1,64}$` after its length, so an empty one is `string_pattern_mismatch`; an enrollment's picture is checked locally only for its URL, every other fault being the server's `422` `image_unreadable` (never billed); `wanted-persons` named where it is not enabled, a `GET` of it included, is the `403` `collection_not_enabled`; a create or an enrollment the workspace cannot pay is the `402` `insufficient_credit` (`InsufficientCreditError`). Cases: the two 402s, `image_unreadable` recorded and from the mock, the `GET` of `wanted-persons`, a refused enrollment URL; the reference cases follow the pattern.
- Errors read as sent, with their messages: `404` `collection_not_found` and `item_not_found` (`NotFoundError`), `403` `collection_read_only`, `collection_not_enabled` and `collections_region_unavailable` (`PermissionDeniedError`), `400` `collection_type_mismatch` (`BadRequestError`), `409` `collections_limit_reached` and `items_limit_reached` (the base error, as every `409`), `422` `no_face` (`ValidationError`).
- The booster `face_recognition` replaces `wanted_person`, which was never released: options `collection` and `threshold`, sent as given; `FaceRecognitionReading` (`image`, `faces`, `matches`, each a `box`, a `score`, the `collection`, the `reference`, and `name` or `label`, empty when absent). The `wanted_person` cases became `face_recognition` cases; `WantedPersonReading` is gone.
- Case format: the methods `collections.*`; the expectations `collection`, `collections`, `item`, `item_page` and `deleted`; the scenario setting `collections` (`wanted_persons`, `faces`, `max_collections`, `max_items`, `region_unavailable`).
- Mock: the seven routes, kept per mock session and workspace; the public `celebrities` and, when the scenario says so, `wanted-persons`, both read-only.
- Mock: batches, collections and items are ordered by a creation counter, never a clock reading. Then the `face_recognition` booster, served: `collection` and `threshold` checked before any reading (the `400` `api_usage_error`, the `404` `collection_not_found`, the `403` `collection_not_enabled`), and a deterministic reading per picture against the collection named; collection and item ids derived from their names, so cases can expect them; the `booster_unknown` reason lists `face_recognition`. The collections POSTs take `Idempotency-Key`: a repeat replays the first answer, a key reused with another request or still running is its `409`. Case format: the behaviour expectation `same_header` (one value on every attempt). Cases on the key: the three POST requests carry it and a read and a delete do not; `create` and `enroll` retried with one key after a `500`; an `idempotency_in_progress` retried; an enrollment's lost answer replayed, the face enrolled once; `idempotency_key_reused` raised. Eight booster cases: an own collection's match with its label, celebrities with and without a threshold, wanted persons enabled and not, an unknown collection, no collection, a threshold out of range.

Cases: 697 in all (requests 122, responses 104, errors 97, behaviour 372, constants 2): 62 new, 3 renamed from `wanted_person`.

| Area | New cases |
|---|---|
| Requests: each of the seven calls on the wire, an enrollment without a label, `list_items` with its query, `face_recognition`'s options | 9 |
| Responses: a collection, one marked for deletion, a created one, the list (own then public), an enrolled item, a page of items, a delete; `face_recognition` readings for an own collection, for the public ones (a name, a reference only) and malformed ones as generic | 10 |
| Errors: `collection_not_found`, `collection_read_only`, `item_not_found`, `no_face`, `collection_not_enabled`, `collection_type_mismatch`, `collections_limit_reached`, `items_limit_reached`, `collections_region_unavailable` | 9 |
| Behaviour: a whole lifecycle against the mock, the list with the public collections, the public ones read-only, no face found, `list_items`' local checks (three), `create` and `enroll` not retried on a `500`, `create` retried on a `529`, a read retried on a `500`; the mock's collection limit; the name, type, reference and label checked locally (seven) | 19 |

## 1.12 (for SDK specification 1.3)

Several places in one answer: a `point` or `box` question takes `max`, an integer from 2 to 16, and is then answered with up to `max` places. `SPEC_VERSION` stays `"1.3"`: the SDKs have not released it yet, so the change folds into it, as the point and box types folded into 1.2 with suite 1.8.

- Builders: `$point` and `$box` take `max`, sent in the question only when given.
- Answers: `PointsAnswer` (`count`, `present`, `complete`, `points` with `x`, `y`, `confidence`, `image`, `width`, `height`) and `BoxesAnswer` (the same with `boxes`, each `x1`, `y1`, `x2`, `y2`, `confidence`), told by the answer's own list, so a decision mixes both shapes and a batch line's decision is typed alike. The places keep the response's order (left to right by the centre, ties from the top) and their numbers as sent. A several-places answer with a field missing or of the wrong type is an `UnknownAnswer`.
- Local checks: `max` not an integer, under 2 or over 16 is a `ValidationError` with the API's entries before any request. The capability is the server's alone: an SDK never refuses `max` on it, not even from a held `/v1/models` entry without `places`, since the gateway judges it per server and version; its `422` `not_supported` comes back as a `ValidationError`.
- Mock: answers the several-places shape for a question with `max`; lists `places: {"max_places": 16}` on every model entry; the scenario setting `places` (`{"max_places": 0}`) turns the capability off, and then the listing leaves `places` out and a question with `max` is the `422` `not_supported` on `/v1/decide` and on batch lines.

Cases: 635 in all (requests 114, responses 96, errors 83, behaviour 340, constants 2): 19 new.

| Area | New cases |
|---|---|
| Requests: `max` on a box (with its image) and on a point (with normalized units), in `decide_many` beside a request without it, and on a batch line | 4 |
| Responses: a `BoxesAnswer` beside a `BoxAnswer` in one decision, a `PointsAnswer` in normalized units, nothing found (count 0, an empty list), and five malformed several-places answers as `UnknownAnswer` | 4 |
| Errors: `max` over 16 answered by the server (sent with `decide_raw`) | 1 |
| Behaviour: `max` under 2, over 16 and not an integer refused locally; the mock's several-places answers beside a single box; the mock's `not_supported` with the capability off; sent after `models()` without `places` and answered by that `422`, and after `models()` with it; in `decide_many` and in `run_batch`; a session's open left to the server | 10 |

## 1.11 (for SDK specification 1.3)

SDK specification 1.3: boosters and fine-tuned models. `SPEC_VERSION` is `"1.3"`.

Readings of three more boosters: `face_detection`, `wanted_person` and `ocr`. Cases send ocr's `prompt` and wanted_person's `threshold` as given, and type their readings: `FaceDetectionReading` (`faces` and `detections`, each a `box` `{x1, y1, x2, y2}` and a `confidence`; pixels, or fractions kept as sent when every place question asks for normalized units), `WantedPersonReading` (`faces` and `matches`, each a `box`, a `score` and a `reference`) and `OcrReading` (`text`, `truncated`), a malformed one of each generic. The mock reads only `age` and `ai_generated` and answers any other name with `booster_unknown`.

OCR timeout: a decision whose `boosters` name `ocr` waits 30 s per attempt when the client's `timeout` is not set, and the `timeout` as set otherwise; it is never retried after a timeout, and its other retries stay. Five behaviour cases: an explicit 1 s timeout used as given and not retried, the object form on a helper, the 30 s default (an answer in 11 s), a 529 still retried, and the rule per item in `decide_many`.

Usage cost: every answer's `usage` carries `cost`, `{usd, model_usd, boosters_usd, free_tier}`, read as sent and never summed by the client (`usd` is the server's). Cases for a plain answer, a boosted answer with the split, a free-tier answer, an answer without `cost` (empty) and a batch result line; the mock writes `cost` on every answer, a batch line, a session's open and its frame replies.

Model ids: the family ids `decisionnode-moe` and `decisionnode-flash` mean the current release, and every case sends them; the default model is `decisionnode-moe` (it was `decisionnode-latest`). A pinned id is `decisionnode-<family>-<version>` and every answer names one; a case that checks the mock's answer checks the family prefix with the new `{"$pattern": regex}` expectation, never a version. The older ids `decisionnode-1.0`, `decisionnode-1.0-flash`, `decisionnode-latest` and `decisionnode-flash-latest`, and the older fine-tuned base `decisionnode-1.0@`, stay accepted: one case each sends one unchanged and expects the family's pinned id. The mock lists each family id (its `version` and `current_version` the current pinned id) followed by its pinned ids, resolves the older ids without listing them, and writes fine-tuned names on the family base, each entry naming its `family`; `responses/models-by-family` records the listing as the API sends it, a family with no version served included.

Cases: 616 in all (requests 110, responses 92, errors 82, behaviour 330, constants 2): 117 new; every case that named a model moved to the family ids (and its recorded answers to pinned ids). Every other 1.10 case is byte-identical.

| Area | New cases |
|---|---|
| Requests: `boosters` by name, as `Booster` objects (with `images`, `require`, `options`), a plain wire-form object beside a `Booster` with `require` false (order kept), an empty list left out, on `check`, per request in `decide_many`, on a batch line (and absent on a line without); a fine-tuned model name, plain and pinned, sent as given, also as the client's model; `fallback` false and true on `decide`, false on a helper and on a batch line; a `Booster` with no option sent as the bare name, so a list of them is the plain list | 16 |
| Responses: `evidence` with an age reading, an age reading with no face, an `ai_generated` reading by inference with a top generator, a photograph (`top_generator` null), a trusted `c2pa` reading with a signer and no top generator, one with `"c2pa": "untrusted"` and an unknown `c2pa` value kept, a reading with unknown fields still typed, wrongly shaped `ai_generated` readings as generic readings, a `timeout` and a `skipped` entry, the response's order and an unknown status kept, an unknown booster's and malformed readings as generic readings, `usage.booster_tokens` and `usage.boosters` present and absent; `Decision.model` as a pinned fine-tuned name; `models()` with fine-tuned entries (`fine_tuned`, `base`, `adapter_version`, `status`) beside a catalogue entry; a fallback answer into `Decision.fallback`, and an answer without one; `billed_calls` per entry (2 and 1, 0 on a timeout and a skip, a reading reused from the cache billed like any other, empty when absent) summing to `usage.boosters`; the pictures a timed-out or failed booster did not read, and an undecodable picture, as generic readings; a batch result line without a body read as a JSON null body (empty message, the class by status) | 20 |
| Errors: `booster_unknown`, `booster_input_mismatch` (no image, an image that is not the request's), a malformed list as `api_usage_error` with its reason, `booster_not_enabled` and `workspace_suspended` as `PermissionDeniedError`, `booster_unavailable` as `OverloadedError` with `boosters` parsed from `x-decisionnode-boosters` in header order, a refusal of a boosted request still a `RefusalError`, boosters on a session open refused, another workspace's fine-tuned model and an undeployed version as the 400 `Unknown model`; the 413 `request_too_large_error` as `RequestTooLargeError`; a 413 HTML page from the edge as `RequestTooLargeError` with the client's own message; a 429 page that is not JSON as `RateLimitError`, never `SessionLimitError` | 15 |
| Behaviour: the mock's readings, a booster that times out or fails without `require` (the decision stands) and with it (the `529`, retried twice, then raised), a booster turned off and an unknown one not retried, a list over four and a duplicate left to the server, `check_request` checking no booster, a fine-tuned model answered and refused by the mock, a batch line with boosters; a booster's `options` nested past the 128 levels refused locally, and at exactly 128 sent; the 25 MiB body limit: a body over it refused locally on `decide`, `decide_many`, a batch line and `check_request`, one under it sent, a 413 not retried, large images uploaded so the body stays under it; the mock's fallback to Flash, the `529` of `fallback: false` retried then raised; a booster object with the old key `booster` left to the server; an image id over 64 characters (code points) refused locally on `decide`, a helper, `decide_many` (the whole call), a batch line and `check_request`, before a duplicate check, a session frame's image not checked (it goes without its id), and ids of 64 accented and of 64 non-BMP characters sent; a 413 HTML page not retried, a 408 HTML page retried then raised as the base class, and a 408 page then success; a 429 page on `open_session` retried as any 429 | 38 |

Changed: `constants/spec-version` is `"1.3"`; `errors/413-presign-request-too-large-error` and `errors/413-batch-add-too-large` expect `RequestTooLargeError` (the base `DecisionNodeError` before), since every 413 is one; `errors/502-non-json-body` and `errors/418-unmapped-status`, whose bodies are not JSON, now expect the client's own message (`HTTP 502 Bad Gateway`, `HTTP 418 (request id req_err_0001)`).

Case format: the error class `RequestTooLargeError`; a mock step's `no_request_id`; the `$booster` builder (`name`, `images`, `require`, `options`), whose wire form is `{"name", "images"?, "require"?, "options"?}` (the key is `name`); a decision's `fallback`; the scenario setting `workers`; a decision's `evidence` (entries with `billed_calls`; readings of kind `age`, `ai_generated` with `c2pa`, or `unknown`) and `usage.booster_tokens` and `usage.boosters`; an error's `boosters`; `fine_tuned`, `base`, `adapter_version` and `status` on an expected `models()` entry; the scenario setting `boosters` (`disabled`, `status`). `evidence` and `boosters` compare whole and in their order. The nesting limit is on the whole body, so `boosters` and their `options` count toward it in every SDK's local check; nothing else about boosters is checked locally.

Mock: an image id over 64 characters is the 400 `Image <n>: its id is longer than 64 characters.` (counted from 0); a recorded response or error case is replayed without an `x-request-id` when it has none; each `evidence` entry has `billed_calls`, and a booster that timed out or failed lists `{"image": id}` for each picture it was meant to read; a booster object is `{"name", ...}`, and one without `name` or with another key is the 400 `api_usage_error`; a live `/v1/decide` for a model the `workers` setting makes cold falls back from `decisionnode-1.0` to Flash with the two headers, or answers the `529` (`fallback: false`, or no fallback); a `/v1/decide` body over 26,214,400 bytes (25 MiB; 64 MB before) is the `413` `request_too_large_error`; a session's socket is attached before the handshake is answered, so an `end()` right after `connect()` always closes it with `1000` (it could be refused with `4404` before). `boosters` on `/v1/decide` and batch lines, checked before any booster runs, with fixed readings per image (`top_generator` only above 0.69, never a signer or `c2pa`), `evidence`, the booster usage and `x-decisionnode-boosters`; the settings `boosters` (in a scenario or `POST /_mock/boosters`) turn a booster off or make it time out or fail; `POST /v1/sessions` refuses boosters. Fine-tuned models of the workspace `acme` (keys holding `ws-acme`): resolved to their pinned name, listed by `/v1/models` with their base's limits, and the 400 `Unknown model` for every other workspace, version or name.

## 1.10 (for SDK specification 1.2)

Every decision goes to `POST /v1/decide`. `SPEC_VERSION` stays `"1.2"`.

Cases: 499 in all (requests 92, responses 58, errors 67, behaviour 280, constants 2): 19 removed, for a call no SDK offers; none new or changed. Every other 1.9 case is byte-identical.

## 1.9 (for SDK specification 1.2)

The SDKs follow the API's documentation, which answers more than 16 images, or more than 4 videos, with a `422` `too_long` on `["body", "images"]` or `["body", "videos"]`. `SPEC_VERSION` stays `"1.2"`.

Cases: 518 in all (requests 97, responses 59, errors 72, behaviour 288, constants 2): 3 new, 6 changed. Every other 1.8 case is byte-identical.

New: the count entries after a question's entry in one list; seventeen images and five videos as two entries; `upload()` of seventeen images refused with the presign's `400`.

Changed: `behaviour/local-image-too-many`, `local-image-too-many-with-urls`, `local-image-in-helper`, `local-video-too-many` and `local-video-in-batch-create` expect the `ValidationError` with the `too_long` entry (a `BadRequestError` `api_usage_error` before).

Changed too: `behaviour/video-frames-over-limit-left-to-server` sends 129 frames, over the documented 128 a request (65 over a provisional 64 before); the SDKs still never count frames.

Mock: more than 16 images or 4 videos is the `422` `too_long`, its `input` the list as received; the frame limit is the documented 128 a request.

## 1.8 (for SDK specification 1.2)

The point and box question types, added to SDK specification 1.2 before any SDK release. `SPEC_VERSION` stays `"1.2"`.

Cases: 515 in all (requests 97, responses 59, errors 72, behaviour 285, constants 2): 22 new, 1 changed. Every other 1.7 case is byte-identical.

| Area | New cases |
|---|---|
| The builders' wire forms on one and on two images (`image` and `units` left out and given, a plain wire-form question), and in a batch line | 3 |
| Answers: `PointAnswer` and `BoxAnswer` in pixels and normalized, a low `present`, a malformed answer and a boolean coordinate as `UnknownAnswer` | 5 |
| Local checks with the API's `422` entries: no image in the request, no `image` with two images, an unknown image id, a video's id, an `image` that is not a string, unknown `units`, several faults in one list, a fault in a batch line and through `check_request`; missing instructions (`400`) | 11 |
| A clean check, the mock's answers, and a point among a session's questions checked at open | 3 |

Changed 1.7 case: `errors/400-unknown-question-type` records the reason that lists `point` and `box` among the types.

Case format, additions: the `$point` and `$box` builders; answer kinds `point` and `box` with their fields.

Mock: `point` and `box` on `/v1/decide` and batch lines, with the local rules' `422` entries and the missing-instructions `400`, answered as deterministic places on an image of 1920 by 1080 pixels; the unknown-type reason lists them.

## 1.7 (for SDK specification 1.2)

SDK specification 1.2: up to 16 images a request (4 before) and videos sent as frames with their times. `SPEC_VERSION` is `"1.2"`.

Cases: 493 in all (requests 94, responses 54, errors 72, behaviour 271, constants 2): 45 new, 7 changed.

| Area | New cases |
|---|---|
| Sixteen images: sent inline in one request, uploaded through one presign of sixteen files, accepted by the mock; a check that finds sixteen images and four videos clean | 4 |
| The videos request body, exactly: fps, start and end given and left out, frames from bytes (media type detected or given) and from base64 text, images beside videos, a plain wire-form video, an empty list left out | 6 |
| Videos on `check`, `number` and a batch line | 4 |
| Local video checks, each with the server's message and no request: more than 4 videos, an empty id, a repeated id, an id shared with an image, no frames, a negative time, a repeated and a decreasing time, a GIF media type, bytes of no known format, base64 text without a media type, fps 5 and 0, a negative start, an end not after the start, `end: 0` alone, and the checks in a helper, `decide_many`, a batch line and `check_request` | 22 |
| Left to the server: 65 frames (over the provisional 64) and a frame whose bytes are not its media type, each sent once and the server's 400 raised; a frame outside the window not checked | 3 |
| The mock's video usage: three frames read, 1.5 seconds | 1 |
| Responses: `usage.video_frames`, `usage.video_seconds` (every digit kept) and `warnings` parsed; `warnings` an empty list and the usage fields empty when absent | 4 |
| Errors: a video file refused, a window without a frame | 2 |

Changed 1.6 cases:

- `behaviour/local-image-too-many`, `local-image-too-many-with-urls` and `local-image-in-helper`: seventeen images (fifteen local and two URLs in the second), refused with `Too many images: 17, the limit is 16.`; the first two now also check the message.
- `errors/400-presign-api-usage-error`: the natural request presigns seventeen files and the recorded message is `files: 1 to 16 files, got 17.`
- `requests/upload-four-images-one-presign`: the description no longer calls four files the maximum. The case is otherwise unchanged.
- `constants/spec-version`: `"1.2"`.

Case format, additions: the `$video` and `$frame` builders (`Video` and `VideoFrame`); a decision expectation's `usage.video_frames`, `usage.video_seconds` and `warnings`.

Harnesses: consecutive `parallel` request entries are matched by assignment (each entry to a recorded request not yet taken, backtracking when a later one finds none), never by trying every order: `upload-sixteen-images-one-presign` holds sixteen parallel PUTs.

Mock: 16 images a request and 1 to 16 files a presign; `videos` on `/v1/decide` and batch lines in the frames form, with the API's form checks and messages, the provisional limit of 64 frames a request, each frame inside the window checked against its media type, the sampling rule, and `usage.video_frames` and `usage.video_seconds` in the response; the too-many-images message is the API's (`Too many images: 17, the limit is 16.`).

## 1.6 (for SDK specification 1.1)

Consistency across the SDKs. `SPEC_VERSION` is unchanged.

Cases: 448 in all (requests 82, responses 50, errors 70, behaviour 244, constants 2): 17 new. Every 1.5 case is byte-identical.

| Area | New cases |
|---|---|
| `check_request` and `Session.check_frame`: valid requests check clean with no request (decide with an image sending would upload, open_session, batches.create, a frame); a 422 entry, a plain-string 400, an image fault, a URL refusal, an open_session 422, a repeated `custom_id` and an unknown frame question id raise what sending raises, with no request | 11 |
| Held `/v1/models` limits found by an alias the list states (sessions and number questions), and a model the list does not state left to the server | 3 |
| The storage PUT's own timeout: a slow PUT inside it succeeds under a 1 s API timeout; `upload_timeout` bounds it | 2 |
| Batch results of 1,200 lines read whole | 1 |

Case format, additions: `"check": true` on a call and on a `send` step, with the outcome `{"result": "checked"}`; `$list` (`{count, item}`, `{i}` replaced by the index); the client's `upload_timeout`; `count` and `batch_results_sample` for `batch_results` and `batch_run` outcomes.

Mock: every `/v1/models` entry names its pinned `version` and its `alias`. A slow PUT is a scenario step with `delay_ms` on a `$method` `PUT` rule, as before.

## 1.5 (for SDK specification 1.1)

The goal: maximum throughput and minimum response time; the server never sees a request the client could have refused. The SDK specification stays 1.1: `SPEC_VERSION` is unchanged.

Cases: 431 in all (requests 82, responses 50, errors 70, behaviour 227, constants 2): 52 new, 32 changed, 1 removed. Every other 1.4 case is byte-identical.

| Area | New cases |
|---|---|
| Question rules checked locally (unknown type, no type, no criteria, wrong field types, empty levels, state type) | 11 |
| Number grid rules checked locally (`float_type`, 2^53, decimals, the decimal bound) | 4 |
| Image rules checked locally (repeated ids, an upload reference's media type, URL port, user information, blocked literal addresses) and the 2,048-character bound passing | 7 |
| The 128-level nesting limit, refused and passing | 2 |
| `open_session` window and ttl_seconds | 5 |
| Frames refused by `Session.send` | 7 |
| `batches.list` paging, `custom_id` rules, an empty add | 7 |
| Limits held from `/v1/models` (sessions served, `max_window`, number support, `requests_per_batch`, `max_custom_id_chars`) and the checks skipped when none is held | 7 |
| A PUT answered 403 after its URL's 5 minutes: presigned again once, and raised the second time | 2 |

Changed 1.4 cases:

- Every 1.4 local case (`local-*`, `image-bytes-unknown-format-refused`, `decide-many-invalid-request-image`) now checks the `error_type` the server would send: null for the faults the API answers with a plain-string 400 (choice, score, truth, number instructions), with the documented message; `api_usage_error` for an image; `image_url_refused` for a URL over 2,048 characters; `api_usage_error` for a URL that is not https; and for the number grid faults the API's 422 `entries` exactly.
- `behaviour/session-frame-error-validation`: the server-side frame error is now a frame over `max_frame_tokens` (a token count only the server makes); the unknown question id it used is refused locally (`local-frame-unknown-question-id`).
- `errors/400-unknown-question-type`, `422-missing-criteria`: the call is `decide_raw` with the same wire body; the reason for an unknown type now lists `number`.
- `errors/422-session-window-out-of-range`, `400-batch-list-bad-limit`: the call passes the local checks (no `window`, no `limit`); the mock replays the recorded response, and `natural_request` keeps the request that gets it.
- `responses/unknown-answer-type`, `unknown-type-beside-number`: the question is sent as a known type; the recorded answer is still of a type the SDK does not know.

Case format, additions: `$nest` (`{depth, value}`); the client's `clock: "mock"` (the SDK's injectable clock reads `GET /_mock/clock`); `outcome` on a `send` step (the frame must be refused locally); the scenario step's `advance_clock_seconds`.

Mock (still one standard-library file, started the same way):

- Stays up through a whole run: a listen backlog of 128 (the default of 5 refused connections when 8 or more arrived at once); keep-alive connections idle for 60 s are closed, so the idle pools of many short-lived clients do not pile up threads; nothing is written to stderr unless asked (`--verbose`, or the new `--log-file <path>`), so a harness that pipes stderr without reading it can no longer stall the mock on a full pipe; a fault inside a handler answers `500 mock_error` instead of dropping the connection; a fault on a session's socket closes it with `1011`; a client that drops a socket mid-stream, a malformed control frame or a body nested thousands of levels deep (which crashed the request recorder) ends that request or socket only.
- `GET /_mock/clock?session=<id>` reads the session's clock; a scenario step's `advance_clock_seconds` moves it before the step acts.
- The API's rules it did not check: the 128-level nesting limit (400 `Invalid request.`) on every body and in frames; image URLs over 2,048 characters, with user information, on a port other than 443 or on a literal blocked address (400 `image_url_refused`).
- The reason for an unknown question type lists `truth, noul, choice, score and number`; a WebSocket message over 1 MiB closes with `1009` (it was 16 MiB).

The 1.4 cases pass against this mock except one check of the mock's own (`errors/400-unknown-question-type` compares its natural reason, which now lists `number`); an SDK harness ignores `natural_request`.

## 1.4 (for SDK specification 1.1)

Every public call: uploads, number questions, truth read from `noul`, sessions and the Batch API, with the rules on images (local checks before any request, inline under a threshold, uploads at or above it).

Cases: 380 in all, 210 new (requests 47 to 83, responses 27 to 50, errors 37 to 70, behaviour 58 to 175, constants 1 to 2).

| Area | New cases |
|---|---|
| Uploads and images (inline threshold, presign, PUT, cache, stale references, storage failures, recorded upload errors) | 59 |
| Local request checks (no request reaches the mock) | 10 |
| Number questions | 20 |
| Truth and the `noul` alias | 8 |
| Sessions (open, stream, frame errors, every close code, pings, timers, `supervise`) | 46 |
| Batches (the seven calls, retry rules, splitting, lifecycle, `run_batch`) | 54 |
| `/v1/models` fields of spec 1.1 | 1 |
| `DEFAULT_INLINE_THRESHOLD` | 1 |

Changed 1.3 cases, each because spec 1.1 changed the rule it checks:

- `constants/spec-version`: `"1.1"`.
- `errors/400-choice-without-options`, `400-too-many-levels`, `400-too-many-options`, `400-truth-without-text`, `reason-escaped-ascii`: the call is `decide_raw` with the same wire body, since the builders now refuse these requests locally; the recorded response and the expected error are unchanged.
- `behaviour/image-bytes-unknown-format-refused`, `decide-many-invalid-request-image`: bytes that are not JPEG, PNG or WebP raise `BadRequestError` locally, no longer an argument error.

Every other 1.3 case is unchanged. Small images still go inline, so the 1.3 image request cases hold under spec 1.1.

Case format, all additions (the 1.3 forms keep their meaning): `$number`, `$text`, `$ref`; `$image` with `upload_id` and `pad_to`; the client's `inline_threshold`; request cases with `mock` and `expect.requests`, `path_pattern`, `query`, `body_base64`, `body_sha256`, `jsonl`, `jsonl_count`, `jsonl_checks`, `parallel`, and body checks with `pattern` and `absent`; response cases with `batch`, `batch_page`, `batch_results` and `session`; behaviour cases with `steps` (calls, session actions, the mock clock) and the expectations `request_sequence`, `request_counts`, `session_events` and `pongs`; the answer kind `number`.

Mock (still one standard-library file, started the same way): `POST /v1/uploads` with an object store at `/_mock/s3/`, upload references on every path, number and `noul` answers, the session endpoints with a WebSocket, the seven batch endpoints, the control endpoints `/_mock/clock`, `/_mock/workspace`, `/_mock/truth`, `/_mock/batches`, `/_mock/sessions` and `/_mock/uploads`, scenario rules by method and path, scenario settings, `after_normal` steps and `{mock_url}` in step headers. The 1.3 cases pass unchanged against this mock.

## 1.3

The first published suite, for SDK specification 1.0: `POST /v1/decide` and `GET /v1/models`.
