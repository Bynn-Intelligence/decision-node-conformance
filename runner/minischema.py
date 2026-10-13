"""A small JSON Schema validator: the subset of draft 2020-12 the suite's schemas use.

Supported: type, enum, const, properties, required, additionalProperties,
propertyNames, items, minItems, minimum, maximum, minProperties, maxProperties,
pattern, anyOf, oneOf, $ref (local "#/$defs/..." and "<file>#/$defs/..." within
the schema directory). Annotations such as description are ignored.
"""

from __future__ import annotations

import json
import os
import re

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class Validator:
    def __init__(self, schema_dir: str):
        self.dir = schema_dir
        self.docs: dict = {}

    def load(self, name: str) -> dict:
        if name not in self.docs:
            with open(os.path.join(self.dir, name), "r", encoding="utf-8") as fh:
                self.docs[name] = json.load(fh)
        return self.docs[name]

    def validate(self, instance, schema_file: str) -> list:
        errors: list = []
        self._check(instance, self.load(schema_file), schema_file, "$", errors)
        return errors

    def _resolve(self, ref: str, current: str):
        file_part, _, frag = ref.partition("#")
        doc_name = file_part or current
        node = self.load(doc_name)
        for part in [p for p in frag.split("/") if p]:
            node = node[part]
        return node, doc_name

    def _check(self, v, s, doc: str, path: str, errors: list) -> None:
        if s is True or s == {}:
            return
        if s is False:
            errors.append("%s: no value is allowed here" % path)
            return
        if "$ref" in s:
            target, tdoc = self._resolve(s["$ref"], doc)
            self._check(v, target, tdoc, path, errors)
        if "type" in s:
            types = s["type"] if isinstance(s["type"], list) else [s["type"]]
            if not any(_TYPES[t](v) for t in types):
                errors.append("%s: expected %s" % (path, " or ".join(types)))
                return
        if "enum" in s and not any(_same(v, e) for e in s["enum"]):
            errors.append("%s: %r is not one of %r" % (path, v, s["enum"]))
        if "const" in s and not _same(v, s["const"]):
            errors.append("%s: must be %r" % (path, s["const"]))
        if "pattern" in s and isinstance(v, str) and not re.search(s["pattern"], v):
            errors.append("%s: %r does not match %s" % (path, v, s["pattern"]))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in s and v < s["minimum"]:
                errors.append("%s: below %s" % (path, s["minimum"]))
            if "maximum" in s and v > s["maximum"]:
                errors.append("%s: above %s" % (path, s["maximum"]))
        if isinstance(v, dict):
            for key in s.get("required", []):
                if key not in v:
                    errors.append("%s: missing %s" % (path, key))
            if "minProperties" in s and len(v) < s["minProperties"]:
                errors.append("%s: needs at least %d properties" % (path, s["minProperties"]))
            if "maxProperties" in s and len(v) > s["maxProperties"]:
                errors.append("%s: takes at most %d properties" % (path, s["maxProperties"]))
            props = s.get("properties", {})
            for key, val in v.items():
                if "propertyNames" in s:
                    self._check(key, s["propertyNames"], doc, "%s.<key %s>" % (path, key), errors)
                if key in props:
                    self._check(val, props[key], doc, "%s.%s" % (path, key), errors)
                elif "additionalProperties" in s:
                    self._check(val, s["additionalProperties"], doc, "%s.%s" % (path, key), errors)
        if isinstance(v, list):
            if "minItems" in s and len(v) < s["minItems"]:
                errors.append("%s: needs at least %d items" % (path, s["minItems"]))
            if "items" in s:
                for i, item in enumerate(v):
                    self._check(item, s["items"], doc, "%s[%d]" % (path, i), errors)
        for key in ("anyOf", "oneOf"):
            if key in s:
                passing = 0
                last: list = []
                for sub in s[key]:
                    errs: list = []
                    self._check(v, sub, doc, path, errs)
                    if not errs:
                        passing += 1
                    else:
                        last = errs
                if passing == 0:
                    errors.append("%s: matches none of %s (%s)" % (path, key, "; ".join(last[:2])))
                elif key == "oneOf" and passing > 1:
                    errors.append("%s: matches more than one of oneOf" % path)


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b
