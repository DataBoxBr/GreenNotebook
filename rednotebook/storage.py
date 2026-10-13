# -----------------------------------------------------------------------
# Copyright (c) 2008-2024 Jendrik Seipp
#
# RedNotebook is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# RedNotebook is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------

import codecs
import copy
import json
import logging
import os
import re
import shutil
import stat
import sys

from rednotebook import sync
from rednotebook.data import Day, Month


try:
    import yaml
except ImportError:
    logging.error("PyYAML not found. Please install it (python3-yaml).")
    sys.exit(1)

try:
    from yaml import CSafeDumper as Dumper
    from yaml import CSafeLoader as Loader

    logging.info("Using LibYAML")
except ImportError:
    from yaml import SafeDumper as Dumper
    from yaml import SafeLoader as Loader

    logging.info("Using PyYAML")


CACHE_MAGIC = "# greennotebook-cache-v1 "


def format_year_and_month(year, month):
    return f"{year:04d}-{month:02d}"


def get_journal_files(data_dir):
    # Format: 2010-05.txt
    date_exp = re.compile(r"(\d{4})-(\d{2})\.txt$")

    for file in sorted(os.listdir(data_dir)):
        if match := date_exp.match(file):
            year = int(match[1])
            month = int(match[2])
            if not sync.valid_month(f"{year:04d}-{month:02d}"):
                logging.warning("Ignoring invalid month filename %s", file)
                continue
            path = os.path.join(data_dir, file)
            yield (path, year, month)
        else:
            logging.debug(f"{file} is not a valid month filename")


def _load_month_from_disk(path, year_number, month_number):
    """
    Load the month file at path and return a month object

    If an error occurs, return None
    """
    try:
        # Try to read the contents of the file.
        with codecs.open(path, "rb", encoding="utf-8") as month_file:
            logging.debug(f'Loading file "{path}"')
            month_contents = yaml.load(month_file, Loader=Loader)
            return Month(
                year_number,
                month_number,
                month_contents,
                os.path.getmtime(path),
            )
    except yaml.YAMLError as exc:
        logging.error(f"Error in file {path}:\n{exc}")
    except OSError:
        # If that fails, there is nothing to load, so just display an error message.
        logging.error(f"Error: The file {path} could not be read")
    except Exception:
        logging.error(f"An error occurred while reading {path}:")
        raise
    # If we continued here, the possibly corrupted file would be overwritten.
    sys.exit(1)


def load_all_months_from_disk(data_dir):
    """
    Load all months and return a directory mapping year-month values
    to month objects. All peers must be upgraded: unannotated files are legacy
    inputs, whereas annotated monthly files are never imported as new roots.
    """
    months = {}

    graph = sync.read_graph(data_dir)
    headers = _cache_headers(data_dir)
    keys = set(graph.observed) | set(headers)
    for path, year_number, month_number in get_journal_files(data_dir):
        key = format_year_and_month(year_number, month_number)
        if key in graph.observed or key in headers or graph.unknown_pending:
            keys.add(key)
            continue
        month = _load_month_from_disk(path, year_number, month_number)
        snapshot = sync.normalize_snapshot(key, _get_dict(month))
        root = sync.make_revision(key, [], {}, snapshot, event=sync.LEGACY_IMPORT_EVENT)
        sync.write_revision(data_dir, root)
        _set_baseline(month, snapshot, [root["id"]], data_dir)
        _update_days(month, snapshot)
        try:
            _save_month_to_disk(month, data_dir, heads=[root["id"]])
        except OSError:
            month.edited = True
            month._sync_cache_failed = True
            raise
        months[key] = month
    graph = sync.read_graph(data_dir)
    keys.update(graph.observed)
    for key in sorted(keys):
        if _waiting(graph, key, headers):
            logging.warning(
                "Deferring month %s until its authoritative revisions are complete", key
            )
            months[key] = _pending_month(key, headers.get(key, ()))
            continue
        year, number = map(int, key.split("-"))
        snapshot = graph.materialize(graph.heads(key))
        path = os.path.join(data_dir, f"{key}.txt")
        month = Month(
            year,
            number,
            copy.deepcopy(snapshot),
            os.path.getmtime(path) if os.path.exists(path) else 0,
        )
        _save_month_to_disk(month, data_dir, heads=graph.heads(key))
        _set_baseline(month, snapshot, graph.heads(key), data_dir)
        months[key] = month
    return months


def pending_revisions(data_dir):
    """Return whether any known or unidentifiable revision is awaiting completion."""
    graph = sync.read_graph(data_dir)
    headers = _cache_headers(data_dir)
    return bool(graph.blocked or graph.unknown_pending) or any(
        _waiting(graph, key, headers) for key in headers
    )


def _cache_headers(data_dir):
    headers = {}
    for path, year, number in get_journal_files(data_dir):
        with open(path, "rb") as stream:
            line = stream.readline()
        if line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        line = line.lstrip(b" \t")
        if not line.startswith(b"# greennotebook-cache"):
            continue
        key = format_year_and_month(year, number)
        heads = ()
        try:
            if not line.startswith(CACHE_MAGIC.encode("ascii")):
                raise ValueError("Unknown or partial cache header")
            header = json.loads(line[len(CACHE_MAGIC) :].decode("ascii"))
            if not isinstance(header, dict) or header.get("month") != key:
                raise ValueError("Invalid cache month")
            values = header.get("heads")
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not sync.ID_PATTERN.fullmatch(value)
                for value in values
            ):
                raise ValueError("Invalid cache heads")
            heads = tuple(sorted(set(values)))
        except (ValueError, TypeError) as error:
            # A partial comment may already announce complete head IDs. Do not
            # discard those hints and roll back to an older, complete graph.
            heads = tuple(
                sorted(
                    {
                        value.decode("ascii")
                        for value in re.findall(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", line)
                    }
                )
            )
            logging.warning("Ignoring malformed cache header in %s: %s", path, error)
        # Even a damaged magic header is a cache, never a legacy root source.
        headers[key] = heads
    return headers


def _missing_heads(graph, key, heads):
    return any(head not in graph.records or graph.records[head]["month"] != key for head in heads)


def _waiting(graph, key, headers, known_heads=()):
    return (
        graph.unknown_pending
        or (key in graph.observed and not graph.complete(key))
        or (key in headers and not graph.complete(key))
        or _missing_heads(graph, key, (*headers.get(key, ()), *known_heads))
    )


def _pending_month(key, cache_heads=()):
    year, number = map(int, key.split("-"))
    month = Month(year, number)
    month._sync_pending = True
    month._sync_durable = False
    month._sync_cache_failed = False
    month._sync_cache_heads = tuple(cache_heads)
    return month


def _get_dict(month):
    return {
        day_number: copy.deepcopy(day.content)
        for day_number, day in month.days.items()
        if not day.empty
    }


def _set_baseline(month, snapshot, heads, data_dir, model_snapshot=None):
    month._sync_baseline = copy.deepcopy(snapshot)
    month._sync_model_baseline = copy.deepcopy(
        snapshot if model_snapshot is None else model_snapshot
    )
    month._sync_heads = tuple(sorted(heads))
    month._sync_directory = os.path.normcase(os.path.abspath(data_dir))
    month._sync_cache_heads = getattr(month, "_sync_cache_heads", ())
    month._sync_pending = getattr(month, "_sync_pending", False)
    month._sync_durable = True
    month._sync_cache_failed = getattr(month, "_sync_cache_failed", False)


def _update_days(month, snapshot):
    desired = {int(day): content for day, content in snapshot.items()}
    for number in list(month.days):
        if number not in desired:
            # The editor can still hold this Day. Empty it without the setter's
            # activity-preserving behavior; empty days are excluded from saves.
            month.days[number]._content = {"text": ""}
    for number, content in desired.items():
        if number in month.days:
            month.days[number]._content = copy.deepcopy(content)
        else:
            month.days[number] = Day(month, number, copy.deepcopy(content))


def _save_month_to_disk(month, journal_dir, heads=None):
    """
    Return whether data was written to disk.

    Monthly files are materialized caches. Atomic replacement has no target gap.
    """
    content = _get_dict(month)

    def get_filename(infix):
        year_and_month = format_year_and_month(month.year_number, month.month_number)
        return os.path.join(journal_dir, f"{year_and_month}{infix}.txt")

    filename = get_filename("")

    heads = tuple(sorted(set(getattr(month, "_sync_heads", ()) if heads is None else heads)))
    if any(not isinstance(head, str) or not sync.ID_PATTERN.fullmatch(head) for head in heads):
        raise OSError("Invalid materialized cache heads")
    header = CACHE_MAGIC + sync.canonical_json(
        {
            "month": format_year_and_month(month.year_number, month.month_number),
            "heads": list(heads),
        }
    )
    serialized = header + "\n" + yaml.dump(content, Dumper=Dumper, allow_unicode=True)
    if yaml.load(serialized, Loader=Loader) != content:
        raise OSError("Writing month file to disk failed")
    if os.path.exists(filename):
        with open(filename, "rb") as stream:
            if stream.read() == serialized.encode("utf-8"):
                month.mtime = os.path.getmtime(filename)
                month.edited = False
                return False

    if os.path.exists(filename):
        mtime = os.path.getmtime(filename)
        if mtime != month.mtime:
            conflict = get_filename(f".CONFLICT_BACKUP{mtime}")
            logging.debug(
                f"Last edit time of {filename} conflicts with edit time at file load\n"
                f"--> Backing up to {conflict}"
            )
            shutil.copy2(filename, conflict)
    sync.atomic_write(filename, serialized.encode("utf-8"))

    try:
        # Make file readable and writable only by the owner.
        os.chmod(filename, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    month.edited = False
    month.mtime = os.path.getmtime(filename)
    logging.info(f"Wrote file {filename}")
    return True


def save_months_to_disk(months, journal_dir, exit_imminent=False, saveas=False):
    """
    Commit local branches first, then reconcile every remote month in place.

    Return whether a local revision was written or remote model content changed.
    Cache-only repairs do not count as changes. Errors leave local models intact
    and edited; a durable local commit may advance its private baseline even if
    replacing the monthly cache subsequently fails.

    _sync_pending means authoritative revisions are incomplete. _sync_durable
    means the current local branch needs no revision write, even if edited is
    still true while awaiting reconciliation. _sync_cache_failed distinguishes
    a failed materialization from a revision write failure or pending ancestry.
    """
    changed = False
    advertised = sync.read_graph(journal_dir)
    headers = _cache_headers(journal_dir)
    directory = os.path.normcase(os.path.abspath(journal_dir))

    def remembered(month):
        if saveas:
            return ()
        return (*getattr(month, "_sync_heads", ()), *getattr(month, "_sync_cache_heads", ()))

    for key, month in months.items():
        if not saveas:
            month._sync_cache_heads = tuple(
                sorted(set(getattr(month, "_sync_cache_heads", ()) + headers.get(key, ())))
            )
        month._sync_pending = _waiting(advertised, key, headers, remembered(month))
        month._sync_durable = getattr(month, "_sync_durable", False)
        month._sync_cache_failed = getattr(month, "_sync_cache_failed", False)
    for key, month in list(months.items()):
        try:
            if key != format_year_and_month(month.year_number, month.month_number):
                raise ValueError("Month dictionary key does not match its date")
            content = sync.normalize_snapshot(key, _get_dict(month))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
            month.edited = True
            month._sync_durable = False
            raise OSError(f"Invalid local month {key}: {error}") from error
        baseline = getattr(month, "_sync_baseline", None)
        model_baseline = getattr(month, "_sync_model_baseline", baseline)
        month._sync_durable = baseline is not None and (
            sync.canonical_json(content) == sync.canonical_json(model_baseline)
        )
        if saveas:
            if _waiting(advertised, key, headers):
                month.edited = True
                month._sync_durable = False
                raise OSError(
                    f"Destination month {key} is waiting for authoritative revisions; "
                    "no root was written"
                )
            baseline, model_baseline, parents = {}, {}, []
        elif baseline is not None:
            if month._sync_directory != directory:
                month._sync_durable = False
                raise OSError("Use Save As when changing the revision destination")
            parents = month._sync_heads
        else:
            baseline, model_baseline, parents = {}, {}, []
        if not saveas and getattr(month, "_sync_baseline", None) is None:
            if key in advertised.observed or key in headers or advertised.unknown_pending:
                if month.edited or content:
                    month.edited = True
                    logging.warning(
                        "Refusing unbased local month %s; local edits retained without a root", key
                    )
                    raise OSError(
                        f"Month {key} has revisions but no observed sync baseline. "
                        "Local edits were retained and were not saved; load the complete "
                        "authoritative month before saving these edits."
                    )
                continue
        local_change = saveas or sync.canonical_json(content) != sync.canonical_json(model_baseline)
        if not saveas and _missing_heads(advertised, key, parents):
            month._sync_pending = True
            if local_change:
                month.edited = True
                month._sync_durable = False
                raise OSError(
                    f"Month {key} is waiting for previously observed revisions. "
                    "Local edits were retained and were not saved."
                )
            continue
        if local_change:
            month.edited = True
            month._sync_durable = False
            record = sync.make_revision(key, parents, baseline, content)
            try:
                advertised.verify(record)
            except (ValueError, TypeError, KeyError, RecursionError) as error:
                raise OSError(
                    f"Cannot commit month {key} against its observed revision baseline"
                ) from error
            changed |= sync.write_revision(journal_dir, record)
            # This mutation is safe only after the immutable file is durable.
            advertised.accept(record)
            local_snapshot = advertised.materialize([record["id"]])
            # The user has seen this local branch, but not any newly arrived peers.
            _set_baseline(month, local_snapshot, [record["id"]], journal_dir, content)
            if saveas:
                month._sync_cache_heads = headers.get(key, ())
            advertised = sync.read_graph(journal_dir)
        elif not parents and not content:
            _set_baseline(month, {}, [], journal_dir)
            month.edited = False
    graph = sync.read_graph(journal_dir)
    latest_headers = _cache_headers(journal_dir)
    for key, month in months.items():
        month._sync_cache_heads = tuple(
            sorted(set(month._sync_cache_heads + latest_headers.get(key, ())))
        )
        month._sync_pending = _waiting(
            graph,
            key,
            latest_headers,
            (*getattr(month, "_sync_heads", ()), *month._sync_cache_heads),
        )
    for key in sorted(set(graph.observed) | set(latest_headers)):
        month = months.get(key)
        known = (
            (*getattr(month, "_sync_heads", ()), *getattr(month, "_sync_cache_heads", ()))
            if month is not None
            else ()
        )
        if _waiting(graph, key, latest_headers, known):
            logging.warning(
                "Deferring month %s until its authoritative revisions are complete", key
            )
            if key not in months:
                months[key] = _pending_month(key, latest_headers.get(key, ()))
            continue
        if (
            month is not None
            and getattr(month, "_sync_baseline", None) is None
            and (month.edited or _get_dict(month))
        ):
            continue
        snapshot = graph.materialize(graph.heads(key))
        year, number = map(int, key.split("-"))
        candidate = Month(year, number, copy.deepcopy(snapshot), month.mtime if month else 0)
        try:
            _save_month_to_disk(candidate, journal_dir, heads=graph.heads(key))
        except OSError:
            if month is not None:
                if getattr(month, "_sync_baseline", None) is not None:
                    month.edited = True
                month._sync_cache_failed = True
            raise
        if month is None:
            month = candidate
            months[key] = month
            changed = True
        else:
            changed |= sync.canonical_json(
                sync.normalize_snapshot(key, _get_dict(month))
            ) != sync.canonical_json(snapshot)
            _update_days(month, snapshot)
            month.mtime = candidate.mtime
        _set_baseline(month, snapshot, graph.heads(key), journal_dir)
        month._sync_cache_heads = graph.heads(key)
        month._sync_pending = False
        month._sync_cache_failed = False
        month.edited = False
    return changed
