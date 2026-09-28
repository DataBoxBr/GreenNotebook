# -----------------------------------------------------------------------
# Copyright (c) 2026 GreenNotebook contributors
#
# This file is part of GreenNotebook and is distributed under the terms
# of the GNU General Public License, version 3 or later.
# -----------------------------------------------------------------------

import datetime
import json
import re
import uuid
from dataclasses import dataclass, replace
from typing import Optional


ACTIVITIES_KEY = "__greennotebook_activities_v1__"
ACTIVITY_VERSION = 2
ALERT_MINUTES_OPTIONS = (0, 5, 10, 15, 30)
MAX_DESCRIPTION_LENGTH = 500
CLIPBOARD_FORMAT = "greennotebook-activities"
STARTUP_ALERT_GRACE = datetime.timedelta(minutes=2)
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_time(value):
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
        raise ValueError("Time must use the HH:MM format")
    hour, minute = (int(part) for part in value.split(":"))
    return datetime.time(hour, minute)


@dataclass(frozen=True)
class Activity:
    id: str
    title: str
    description: str
    start: str
    end: str
    alert_minutes_before: Optional[int] = None
    notified_for: Optional[str] = None

    def __post_init__(self):
        activity_id = str(self.id).strip()
        title = str(self.title).strip()
        description = str(self.description).strip()
        if not activity_id:
            raise ValueError("Activity ID cannot be empty")
        if not title:
            raise ValueError("Activity title cannot be empty")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise ValueError("Activity description is too long")

        start = parse_time(self.start)
        end = parse_time(self.end)
        if end <= start:
            raise ValueError("Activity end time must be later than its start time")
        if self.alert_minutes_before is not None and (
            type(self.alert_minutes_before) is not int
            or self.alert_minutes_before not in ALERT_MINUTES_OPTIONS
        ):
            raise ValueError("Unsupported alert interval")

        object.__setattr__(self, "id", activity_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)


def create_activity(title, description, start, end, alert_minutes_before=None):
    return Activity(
        uuid.uuid4().hex,
        title,
        description,
        start,
        end,
        alert_minutes_before,
    )


def encode_activity(activity):
    record = {
        "alert_minutes_before": activity.alert_minutes_before,
        "description": activity.description,
        "end": activity.end,
        "id": activity.id,
        "notified_for": activity.notified_for,
        "start": activity.start,
        "title": activity.title,
        "v": ACTIVITY_VERSION,
    }
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def decode_activity(value):
    record = json.loads(value)
    if not isinstance(record, dict) or record.get("v") not in (1, ACTIVITY_VERSION):
        raise ValueError("Unsupported activity record")
    if record["v"] == 1:
        alert_minutes_before = _get_legacy_alert_minutes(record["start"], record.get("alert"))
        title = record["text"]
        description = ""
    else:
        alert_minutes_before = record.get("alert_minutes_before")
        title = record["title"]
        description = record.get("description", "")
    return Activity(
        id=record["id"],
        title=title,
        description=description,
        start=record["start"],
        end=record["end"],
        alert_minutes_before=alert_minutes_before,
        notified_for=record.get("notified_for"),
    )


def _get_legacy_alert_minutes(start, alert):
    if not alert:
        return None
    start_time = parse_time(start)
    alert_time = parse_time(alert)
    start_minutes = start_time.hour * 60 + start_time.minute
    alert_minutes = alert_time.hour * 60 + alert_time.minute
    difference = max(0, start_minutes - alert_minutes)
    return min(ALERT_MINUTES_OPTIONS, key=lambda option: (abs(option - difference), option))


def decode_activities(records):
    if not isinstance(records, dict):
        return []

    decoded = []
    for value in records:
        try:
            decoded.append(decode_activity(value))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(decoded, key=lambda item: (item.start, item.end, item.title.casefold(), item.id))


def upsert_activity(records, activity):
    updated = dict(records) if isinstance(records, dict) else {}
    for value in list(updated):
        try:
            existing = decode_activity(value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if existing.id == activity.id:
            del updated[value]
    updated[encode_activity(activity)] = None
    return updated


def remove_activity(records, activity_id):
    updated = dict(records) if isinstance(records, dict) else {}
    for value in list(updated):
        try:
            activity = decode_activity(value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if activity.id == activity_id:
            del updated[value]
    return updated


def notification_token(date, activity):
    scheduled = get_scheduled_alert(date, activity)
    return scheduled.strftime("%Y-%m-%dT%H:%M") if scheduled else None


def get_scheduled_alert(date, activity):
    if activity.alert_minutes_before is None:
        return None
    start = datetime.datetime.combine(date, parse_time(activity.start))
    return start - datetime.timedelta(minutes=activity.alert_minutes_before)


def mark_notified(records, activity_id, token):
    for activity in decode_activities(records):
        if activity.id == activity_id:
            return upsert_activity(records, replace(activity, notified_for=token))
    return dict(records) if isinstance(records, dict) else {}


def get_due_activities(days, previous_check, now):
    window_start = previous_check or now - STARTUP_ALERT_GRACE
    if window_start > now:
        window_start = now

    due = []
    for day in days:
        for activity in day.activities:
            scheduled = get_scheduled_alert(day.date, activity)
            if scheduled is None:
                continue
            token = notification_token(day.date, activity)
            if window_start < scheduled <= now and activity.notified_for != token:
                due.append((scheduled, day, activity, token))

    due.sort(key=lambda item: (item[0], item[2].start, item[2].id))
    return [(day, activity, token) for _, day, activity, token in due]


def serialize_activities_for_clipboard(items):
    records = [
        {
            "alert_minutes_before": activity.alert_minutes_before,
            "description": activity.description,
            "end": activity.end,
            "start": activity.start,
            "title": activity.title,
        }
        for activity in items
    ]
    payload = {"activities": records, "format": CLIPBOARD_FORMAT, "version": 1}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def deserialize_activities_from_clipboard(value):
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("format") != CLIPBOARD_FORMAT:
        return []
    if payload.get("version") != 1 or not isinstance(payload.get("activities"), list):
        return []

    decoded = []
    for record in payload["activities"]:
        try:
            decoded.append(
                create_activity(
                    title=record["title"],
                    description=record.get("description", ""),
                    start=record["start"],
                    end=record["end"],
                    alert_minutes_before=record.get("alert_minutes_before"),
                )
            )
        except (KeyError, TypeError, ValueError):
            return []
    return decoded
