# Copyright (c) 2026 GreenNotebook contributors
# Distributed under the GNU General Public License, version 3 or later.
"""Immutable folder revisions. No assumption is made about delivery order.

The YAML files are caches, not inputs once a month has any revision records.
Each revision writes only leaves changed from its observed, complete baseline.
Materialization selects causal maxima per leaf, rather than folding snapshots
in arrival order. Native revisions identify distinct write events even when
their snapshots match; only legacy imports use a deterministic event identity.
Records and their parents are retained indefinitely.
"""

import calendar
import copy
import datetime
import difflib
import hashlib
import heapq
import itertools
import json
import logging
import math
import os
import re
import tempfile
import uuid
from collections import OrderedDict

from rednotebook import activities


REVISION_DIR = "greennotebook-revisions"
CONFLICT_KEY = "__greennotebook_sync_conflict_v1__"
MONTH_PATTERN = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")
ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
EVENT_PATTERN = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
LEGACY_IMPORT_EVENT = "legacy-import"
MISSING = ("missing",)
CONTAINER = ("dict",)
_GRAPH_CACHE = OrderedDict()
MAX_CACHED_FOLDERS = 4
MAX_MATERIALIZATIONS = 64


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_load(value):
    return json.loads(value, object_pairs_hook=_object)


def valid_month(month):
    return isinstance(month, str) and bool(MONTH_PATTERN.fullmatch(month)) and month[:4] != "0000"


def _validate_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("Month values must be finite JSON values with string field names")


def _activity_record(value):
    record = _json_load(value)
    if not isinstance(record, dict) or type(record.get("v")) is not int:
        raise ValueError("Invalid activity schema")
    required = ("id", "start", "end", "text" if record["v"] == 1 else "title")
    if any(not isinstance(record.get(key), str) for key in required):
        raise ValueError("Invalid activity string fields")
    if "description" in record and not isinstance(record["description"], str):
        raise ValueError("Invalid activity description")
    token = record.get("notified_for")
    if token is not None:
        if not isinstance(token, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", token):
            raise ValueError("Invalid activity notification token")
        datetime.datetime.strptime(token, "%Y-%m-%dT%H:%M")
    decoded = activities.decode_activity(value)
    normalized = _json_load(activities.encode_activity(decoded))
    known = set(normalized)
    if record["v"] == 1:
        known.update(("alert", "text"))
    normalized.update({key: item for key, item in record.items() if key not in known})
    _validate_json(normalized)
    return normalized


def normalize_snapshot(month, snapshot):
    """Validate a model/legacy snapshot and convert day/activity keys to canonical JSON."""
    if not valid_month(month) or not isinstance(snapshot, dict):
        raise ValueError("Invalid month snapshot")
    year, number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, number)[1]
    result = {}
    for day, content in snapshot.items():
        if type(day) is int:
            key = str(day)
        elif isinstance(day, str) and re.fullmatch(r"[1-9][0-9]?", day):
            key = day
        else:
            raise ValueError("Invalid day key")
        if not 1 <= int(key) <= last_day or key in result:
            raise ValueError("Invalid or duplicate day")
        if not isinstance(content, dict) or not isinstance(content.get("text"), str):
            raise ValueError("Every day must have a string text field")
        _validate_json(content)
        content = copy.deepcopy(content)
        if "Tags" in content:
            tags = content.pop("Tags") or {}
            if not isinstance(tags, dict) or "text" in tags or activities.ACTIVITIES_KEY in tags:
                raise ValueError("Invalid legacy Tags category")
            for tag in tags:
                content[tag] = None
        if activities.ACTIVITIES_KEY in content:
            records = content[activities.ACTIVITIES_KEY]
            if not isinstance(records, dict):
                raise ValueError("Activities must be an entry mapping")
            normalized = {}
            ids = set()
            for value, entry in records.items():
                if entry is not None:
                    raise ValueError("Invalid activity entry")
                record = _activity_record(value)
                if record["id"] in ids:
                    raise ValueError("Duplicate activity ID")
                ids.add(record["id"])
                normalized[canonical_json(record)] = None
            if normalized:
                content[activities.ACTIVITIES_KEY] = normalized
            else:
                del content[activities.ACTIVITIES_KEY]
        if len(content) != 1 or content["text"].strip():
            result[key] = content
    return result


def _validate_event(event, parents, base):
    if event == LEGACY_IMPORT_EVENT:
        if parents or base:
            raise ValueError("A legacy import event must be a root with an empty base")
    elif not isinstance(event, str) or not EVENT_PATTERN.fullmatch(event):
        raise ValueError("Invalid revision event UUID")


def make_revision(month, parents, base, content, *, event=None):
    """Create one write event; identical native edits are not the same observation.

    Supply an explicit UUID only to recreate that same event (for example in a
    test). The reserved legacy-import event is deterministic and root-only.
    """
    payload = {
        "version": 1,
        "event": uuid.uuid4().hex if event is None else event,
        "month": month,
        "parents": sorted(set(parents)),
        "base": normalize_snapshot(month, base),
        "content": normalize_snapshot(month, content),
    }
    _validate_event(payload["event"], payload["parents"], payload["base"])
    return {"id": hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(), **payload}


def validate_revision(record):
    if not isinstance(record, dict) or set(record) != {
        "id",
        "version",
        "event",
        "month",
        "parents",
        "base",
        "content",
    }:
        raise ValueError("Invalid revision schema")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("Unsupported revision version")
    if not isinstance(record["id"], str) or not ID_PATTERN.fullmatch(record["id"]):
        raise ValueError("Invalid revision ID")
    parents = record["parents"]
    if not isinstance(parents, list) or any(
        not isinstance(parent, str) or not ID_PATTERN.fullmatch(parent) for parent in parents
    ):
        raise ValueError("Invalid parents")
    if parents != sorted(set(parents)) or record["id"] in parents:
        raise ValueError("Noncanonical or circular parents")
    for key in ("base", "content"):
        if normalize_snapshot(record["month"], record[key]) != record[key]:
            raise ValueError("Noncanonical snapshot")
    _validate_event(record["event"], parents, record["base"])
    payload = {key: value for key, value in record.items() if key != "id"}
    if hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest() != record["id"]:
        raise ValueError("Revision payload hash mismatch")
    if not parents and record["base"]:
        raise ValueError("A root must have an empty base")
    return record


def atomic_write(path, content):
    """Replace in one operation, including on Windows; never unlink the target."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def write_revision(data_dir, record):
    validate_revision(record)
    directory = os.path.join(data_dir, REVISION_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{record['month']}.{record['id']}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            try:
                existing = validate_revision(_json_load(stream.read()))
            except (ValueError, TypeError, KeyError) as error:
                raise OSError(
                    f"Refusing to overwrite malformed immutable revision {path}"
                ) from error
        if existing != record:
            raise OSError(f"Refusing to overwrite immutable revision {path}")
        return False
    atomic_write(path, (canonical_json(record) + "\n").encode("utf-8"))
    invalidate_graph(data_dir)
    return True


def _tree(snapshot):
    tree = copy.deepcopy(snapshot)
    for content in tree.values():
        if activities.ACTIVITIES_KEY in content:
            content[activities.ACTIVITIES_KEY] = {
                record["id"]: record
                for record in map(_activity_record, content[activities.ACTIVITIES_KEY])
            }
    return tree


def _flatten(value, path=()):
    result = {}
    if isinstance(value, dict):
        result[path] = CONTAINER
        for key, child in value.items():
            result.update(_flatten(child, path + (key,)))
    else:
        result[path] = ("value", value)
    return result


def _writes(record):
    before = _flatten(_tree(record["base"]))
    after = _flatten(_tree(record["content"]))
    return {
        key: after.get(key, MISSING)
        for key in before.keys() | after.keys()
        if canonical_json(before.get(key, MISSING)) != canonical_json(after.get(key, MISSING))
    }


def _text_conflict(versions):
    sections = []
    for index, (ids, value) in enumerate(versions):
        marker = "<<<<<<<" if not index else "======="
        sections.append(f"{marker} revision {', '.join(item[:12] for item in ids)}\n")
        sections.append(value)
        if not value.endswith("\n"):
            sections.append("\n")
    sections.append(">>>>>>> sync conflict\n")
    return "".join(sections)


def _merge_text(base, versions):
    patches = set()
    for _ids, value in versions:
        matcher = difflib.SequenceMatcher(None, base, value, autojunk=False)
        for tag, start, end, other_start, other_end in matcher.get_opcodes():
            if tag != "equal":
                patches.add((start, end, value[other_start:other_end]))
    patches = sorted(patches)
    for index, (start, end, _value) in enumerate(patches):
        for other_start, other_end, _other_value in patches[index + 1 :]:
            if (
                max(start, other_start) < min(end, other_end)
                or start == end == other_start == other_end
                or start < other_start < end
                or other_start < start < other_end
            ):
                return _text_conflict(versions)
    for start, end, value in reversed(patches):
        base = base[:start] + value + base[end:]
    return base


class CausalSet:
    """Compact immutable revision-ID set backed by a graph-local integer bitset."""

    __slots__ = ("graph", "bits")

    def __init__(self, graph, bits=0):
        self.graph = graph
        self.bits = bits

    def __contains__(self, revision):
        index = self.graph._indices.get(revision)
        return index is not None and bool(self.bits & (1 << index))

    def __iter__(self):
        bits = self.bits
        while bits:
            bit = bits & -bits
            yield self.graph._ids[bit.bit_length() - 1]
            bits ^= bit

    def __len__(self):
        return bin(self.bits).count("1")

    def __bool__(self):
        return bool(self.bits)

    def _bits(self, other):
        if isinstance(other, CausalSet):
            if other.graph is not self.graph:
                raise ValueError("Causal sets belong to different graphs")
            return other.bits
        bits = 0
        for revision in other:
            bits |= 1 << self.graph._indices[revision]
        return bits

    def __or__(self, other):
        return CausalSet(self.graph, self.bits | self._bits(other))

    def __and__(self, other):
        return CausalSet(self.graph, self.bits & self._bits(other))

    def __sub__(self, other):
        return CausalSet(self.graph, self.bits & ~self._bits(other))


class RevisionGraph:
    """A validated graph plus advertised months that must not be seeded from YAML."""

    def __init__(self):
        self.records = {}
        self.ancestors = {}
        self.writes = {}
        self.observed = set()
        self.blocked = set()
        self.unknown_pending = False
        self._indices = {}
        self._ids = []
        self._materialized = OrderedDict()
        self._maximal_writes = {(): {}}
        self._trees = {}

    def closure(self, heads):
        bits = 0
        for head in heads:
            bits |= (1 << self._indices[head]) | self.ancestors[head].bits
        return CausalSet(self, bits)

    def heads(self, month):
        ids = {key for key, record in self.records.items() if record["month"] == month}
        return self._heads(ids)

    def _heads(self, ids):
        return tuple(
            sorted(ids - {parent for key in ids for parent in self.records[key]["parents"]})
        )

    def _normal_heads(self, heads):
        if isinstance(heads, CausalSet):
            return self._heads(heads)
        heads = set(heads)
        return tuple(
            sorted(
                head for head in heads if not any(head in self.ancestors[other] for other in heads)
            )
        )

    def complete(self, month):
        return month in self.observed and month not in self.blocked and not self.unknown_pending

    def verify(self, record):
        validate_revision(record)
        if any(self.records[parent]["month"] != record["month"] for parent in record["parents"]):
            raise ValueError("Parents belong to another month")
        if canonical_json(self.materialize(record["parents"])) != canonical_json(record["base"]):
            raise ValueError("Base is not the complete observed parent snapshot")

    def accept(self, record):
        self.verify(record)
        key = record["id"]
        if key in self.records:
            return
        ancestors = self.closure(record["parents"])
        writes = _writes(record)
        maxima = dict(self._maxima(record["parents"]))
        self._indices[key] = len(self._ids)
        self._ids.append(key)
        self.records[key] = record
        self.ancestors[key] = ancestors
        self.writes[key] = writes
        self.observed.add(record["month"])
        for path, value in writes.items():
            maxima[path] = [(key, value)]
        self._maximal_writes[(key,)] = maxima

    def materialize(self, heads):
        key = self._normal_heads(heads)
        if key not in self._materialized:
            self._materialized[key] = self._merge(key)
            if len(self._materialized) > MAX_MATERIALIZATIONS:
                self._materialized.popitem(last=False)
        self._materialized.move_to_end(key)
        return copy.deepcopy(self._materialized[key])

    def _maxima(self, heads):
        key = self._normal_heads(heads)
        if key in self._maximal_writes:
            return self._maximal_writes[key]
        result = {}
        for head in key:
            for path, writes in self._maximal_writes[(head,)].items():
                previous = result.setdefault(path, [])
                for revision, value in writes:
                    if any(
                        revision == old or revision in self.ancestors[old] for old, _ in previous
                    ):
                        continue
                    previous[:] = [
                        item for item in previous if item[0] not in self.ancestors[revision]
                    ]
                    previous.append((revision, value))
        self._maximal_writes[key] = result
        return result

    def _record_tree(self, revision):
        if revision not in self._trees:
            self._trees[revision] = _tree(self.records[revision]["content"])
        return self._trees[revision]

    def _versions(self, writes):
        groups = {}
        for revision, node in writes:
            if node == MISSING:
                continue
            key = canonical_json(node)
            ids, value = groups.setdefault(key, ([], node))
            ids.append(revision)
        return sorted((sorted(ids), value) for ids, value in groups.values())

    def _resolve(self, path, writes):
        versions = self._versions(writes)
        if not versions:
            return MISSING
        if len(versions) == 1:
            return versions[0][1]
        if len(path) == 2 and path[1] == "text":
            sets = [self.ancestors[item] for ids, _ in versions for item in ids]
            common = sets[0]
            for ancestors in sets[1:]:
                common = common & ancestors
            baseline = self.materialize(common).get(path[0], {}).get("text", "")
            return ("value", _merge_text(baseline, [(ids, node[1]) for ids, node in versions]))
        if CONTAINER in (node for _ids, node in versions):
            return CONTAINER
        return versions[0][1]

    def _activity(self, path, maxima, build):
        fields = {}
        conflicts = False
        invalid = False
        contributors = {
            revision
            for field_path, writes in maxima.items()
            if field_path[:3] == path and len(field_path) >= 4
            for revision, node in writes
            if node != MISSING
        }
        field_names = {key[3] for key in maxima if key[:3] == path and len(key) >= 4}
        for field in field_names:
            field_path = path + (field,)
            if any(key[:4] == field_path and len(key) > 4 for key in maxima) or any(
                node == CONTAINER for _revision, node in maxima.get(field_path, [])
            ):
                value = build(field_path)
                if value is not MISSING:
                    fields[field] = [value]
        for field_path, writes in maxima.items():
            if field_path[:3] != path or len(field_path) != 4:
                continue
            versions = self._versions(writes)
            values = [node[1] for _revisions, node in versions if node[0] == "value"]
            if not values:
                continue
            field = field_path[-1]
            if field in fields:
                continue
            if field == "notified_for":
                fields[field] = [
                    max((value for value in values if value is not None), default=None)
                ]
            else:
                fields[field] = values
                conflicts |= len(values) > 1
        if not fields:
            return None
        editors = {
            revision
            for revision in contributors
            if not any(revision in self.ancestors[other] for other in contributors)
        }

        def get_sources(revisions=None, expand=False):
            sources = []
            for revision in sorted(editors if revisions is None else revisions):
                tree = self._record_tree(revision)
                record = tree.get(path[0], {}).get(path[1], {}).get(path[2])
                if record is not None:
                    if expand:
                        base = _tree(self.records[revision]["base"])
                        previous = base.get(path[0], {}).get(path[1], {}).get(path[2], {})
                        recovered = False
                        for metadata in (record.get(CONFLICT_KEY), previous.get(CONFLICT_KEY)):
                            if not isinstance(metadata, dict) or not isinstance(
                                metadata.get("variants"), list
                            ):
                                continue
                            references = metadata.get("revisions")
                            if (
                                not isinstance(references, list)
                                or not references
                                or any(
                                    not isinstance(value, str) or not ID_PATTERN.fullmatch(value)
                                    for value in references
                                )
                            ):
                                continue
                            for variant in metadata["variants"]:
                                try:
                                    variant = _activity_record(canonical_json(variant))
                                    if variant["id"] != path[2]:
                                        raise ValueError(
                                            "Conflict variant belongs to another activity"
                                        )
                                    sources.append(variant)
                                    recovered = True
                                except (ValueError, TypeError, KeyError):
                                    continue
                        if recovered:
                            record = {
                                key: value for key, value in record.items() if key != CONFLICT_KEY
                            }
                    sources.append(record)
            return sources

        # A concurrent deletion can tombstone unchanged required fields. Recover
        # their context from maximal editors, but still merge every live write.
        required = {"v", "title", "start", "end"}
        sources = get_sources() if required - fields.keys() else []
        contexts = sources if required - fields.keys() else [{}]
        variants = {}
        keys = sorted(fields)
        # Complete valid variants are explicit metadata on one reminder ID, so
        # conflicts do not create duplicate scheduled notifications.
        for values in itertools.product(*(fields[key] for key in keys)):
            for context in contexts:
                record = {**context, **dict(zip(keys, values)), "id": path[-1]}
                try:
                    record = _activity_record(canonical_json(record))
                except (ValueError, TypeError, KeyError):
                    conflicts = True
                    invalid = True
                    continue
                variants[canonical_json(record)] = record
        if not variants or invalid:
            # A notifier descendant is not a replacement for the actual schedule
            # writers. Retain their complete valid versions and inherited variants.
            schedule_writers = {
                revision
                for field in ("start", "end")
                for revision, value in maxima.get(path + (field,), [])
                if value != MISSING
            }
            for source in get_sources(editors | schedule_writers, expand=True):
                record = copy.deepcopy(source)
                for field, values in fields.items():
                    if field not in ("start", "end", CONFLICT_KEY) and len(values) == 1:
                        record[field] = values[0]
                record = _activity_record(canonical_json(record))
                variants.setdefault(canonical_json(record), record)
        if not variants:
            return None
        record = copy.deepcopy(variants[min(variants)])
        if conflicts:
            record[CONFLICT_KEY] = {
                "revisions": sorted(contributors),
                "variants": [variants[key] for key in sorted(variants)],
            }
            if not record["title"].startswith("[Sync conflict] "):
                record["title"] = "[Sync conflict] " + record["title"]
        return record

    def _merge(self, ids):
        maxima = self._maxima(ids)
        children = {}
        conflicts = {}
        for path in maxima:
            for index in range(len(path)):
                children.setdefault(path[:index], set()).add(path[index])

        def archive(path, contributors):
            versions = {}
            for revision in sorted(contributors):
                value = self._record_tree(revision)
                for key in path:
                    value = value[key]
                key = canonical_json(value)
                entry = versions.setdefault(key, {"revisions": [], "value": value})
                entry["revisions"].append(revision)
            if len(versions) > 1:
                conflicts.setdefault(path[0], {})[path] = {
                    "path": list(path[1:]),
                    "versions": sorted(versions.values(), key=canonical_json),
                }

        def build(path):
            if len(path) == 3 and path[1] == activities.ACTIVITIES_KEY:
                return self._activity(path, maxima, build)
            writes = maxima.get(path, [])
            node = self._resolve(path, writes)
            if len(path) >= 2 and path[1] != "text" and len(self._versions(writes)) > 1:
                archive(path, {revision for revision, value in writes if value != MISSING})
            descendants = {}
            for key in sorted(children.get(path, ())):
                value = build(path + (key,))
                if value is not MISSING:
                    descendants[key] = value
            if descendants or node == CONTAINER:
                if node != MISSING and node != CONTAINER:
                    contributors = {
                        revision
                        for key, values in maxima.items()
                        if key[: len(path)] == path
                        for revision, value in values
                        if value != MISSING
                    }
                    archive(path, contributors)
                return descendants
            return node[1] if node != MISSING else MISSING

        tree = build(())
        if tree is MISSING:
            return {}
        for day, content in tree.items():
            content.setdefault("text", "")
            if activities.ACTIVITIES_KEY in content:
                records = content[activities.ACTIVITIES_KEY]
                content[activities.ACTIVITIES_KEY] = {
                    canonical_json(record): None
                    for record in records.values()
                    if record is not None
                }
                if not content[activities.ACTIVITIES_KEY]:
                    del content[activities.ACTIVITIES_KEY]
            if day in conflicts:
                existing = content.get(CONFLICT_KEY)
                if (
                    isinstance(existing, dict)
                    and existing.get("version") == 1
                    and isinstance(existing.get("conflicts"), list)
                ):
                    metadata = copy.deepcopy(existing)
                else:
                    metadata = {"version": 1, "conflicts": []}
                    if CONFLICT_KEY in content:
                        metadata["original"] = existing
                entries = {canonical_json(entry): entry for entry in metadata["conflicts"]}
                entries.update({canonical_json(entry): entry for entry in conflicts[day].values()})
                metadata["conflicts"] = [entries[key] for key in sorted(entries)]
                content[CONFLICT_KEY] = metadata
        if ids:
            month = self.records[next(iter(ids))]["month"]
            return normalize_snapshot(month, tree)
        return tree


def invalidate_graph(data_dir):
    """Invalidate the scan signature, retaining immutable parsed records for reuse."""
    key = os.path.normcase(os.path.abspath(data_dir))
    if key in _GRAPH_CACHE:
        _GRAPH_CACHE[key]["signature"] = None


def read_graph(data_dir):
    """Reuse unchanged graphs; reparse changed files and rebuild after removals.

    Only files actually present and validated can extend a cached graph. Callers
    must not speculatively accept revisions into this shared object before their
    immutable file is durably written.
    """
    key = os.path.normcase(os.path.abspath(data_dir))
    directory = os.path.join(key, REVISION_DIR)
    cached = _GRAPH_CACHE.get(key)
    previous = cached["files"] if cached else {}
    stats = {}
    scan_error = None
    try:
        if os.path.isdir(directory):
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.lower().endswith(".json"):
                        continue
                    try:
                        info = entry.stat()
                        stats[entry.path] = (
                            info.st_mtime_ns,
                            info.st_size,
                            info.st_ctime_ns,
                            info.st_ino,
                            info.st_dev,
                        )
                    except OSError:
                        stats[entry.path] = None
    except OSError as error:
        scan_error = str(error)
    signature = (tuple(sorted(stats.items())), scan_error)
    if cached and cached["signature"] == signature and not cached["retry"]:
        _GRAPH_CACHE.move_to_end(key)
        return cached["graph"]
    files = {}
    retry = scan_error is not None or any(stamp is None for stamp in stats.values())
    for path, stamp in sorted(stats.items()):
        old = previous.get(path)
        if old and stamp is not None and old[0] == stamp and not old[4]:
            files[path] = old
            continue
        filename = os.path.basename(path)
        month = filename[:7] if valid_month(filename[:7]) else None
        record, error_text, io_error = None, None, False
        try:
            with open(path, encoding="utf-8") as stream:
                record = _json_load(stream.read())
            if isinstance(record, dict) and valid_month(record.get("month")):
                month = record["month"]
            validate_revision(record)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            record = None
            error_text = str(error)
            io_error = isinstance(error, OSError)
            retry |= io_error
            logging.warning("Deferring revision %s: %s", path, error)
        files[path] = (stamp, record, month, error_text, io_error)
    # Appends cannot invalidate previously verified causal state. Replacements
    # and removals can, so they rebuild using the still-valid parsed file cache.
    extend = (
        cached
        and not scan_error
        and all(path in stats and stats[path] == item[0] for path, item in previous.items())
    )
    available = {item[1]["id"] for item in files.values() if item[1] is not None}
    extend = extend and cached["graph"].records.keys() <= available
    graph = cached["graph"] if extend else RevisionGraph()
    graph.observed = {item[2] for item in files.values() if item[2]}
    graph.blocked = {item[2] for item in files.values() if item[3] and item[2]}
    graph.unknown_pending = bool(scan_error) or any(
        item[3] and not item[2] for item in files.values()
    )
    pending = {
        item[1]["id"]: item[1]
        for item in files.values()
        if item[1] is not None and item[1]["id"] not in graph.records
    }
    waiting = {
        revision: sum(parent not in graph.records for parent in record["parents"])
        for revision, record in pending.items()
    }
    children = {}
    for key, record in pending.items():
        for parent in record["parents"]:
            children.setdefault(parent, []).append(key)
    ready = [key for key, count in waiting.items() if not count]
    heapq.heapify(ready)
    while ready:
        key = heapq.heappop(ready)
        record = pending.pop(key)
        try:
            graph.accept(record)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            graph.blocked.add(record["month"])
            logging.warning("Deferring revision %s: %s", key, error)
            continue
        for child in children.get(key, ()):
            waiting[child] -= 1
            if not waiting[child]:
                heapq.heappush(ready, child)
    for key, record in pending.items():
        graph.blocked.add(record["month"])
        logging.warning("Deferring revision %s: missing, invalid or circular parents", key)
    cache_key = os.path.normcase(os.path.abspath(data_dir))
    _GRAPH_CACHE[cache_key] = {
        "signature": signature,
        "files": files,
        "graph": graph,
        "retry": retry,
    }
    _GRAPH_CACHE.move_to_end(cache_key)
    while len(_GRAPH_CACHE) > MAX_CACHED_FOLDERS:
        _GRAPH_CACHE.popitem(last=False)
    return graph
