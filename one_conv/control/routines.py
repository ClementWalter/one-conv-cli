"""Routines: a prompt the workspace posts in a channel on a cron schedule, as the human would.

The post goes through the same path as a human message, so agents working in the channel are
woken and a channel nobody works in gets a session started. Each fire's message ID is derived
from the routine and its scheduled minute, so a retried fire never posts twice.
"""

import hashlib
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..live.hub import MESSAGE_ID, TOPIC, HubError

NAME_LIMIT = 80
PROMPT_LIMIT = 32_000
ROUTINES_LIMIT = 100
# A cron line with no match in this many days (like February 30) never fires.
SEARCH_DAYS = 5 * 366
FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))
WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (id TEXT PRIMARY KEY, name TEXT NOT NULL, topic TEXT NOT NULL,
    prompt TEXT NOT NULL, cron TEXT NOT NULL, tz TEXT NOT NULL, enabled INTEGER NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL, next_at REAL, last_at REAL, last_post TEXT,
    last_error TEXT NOT NULL DEFAULT '');
"""
COLUMNS = ("id", "name", "topic", "prompt", "cron", "tz", "enabled", "created", "updated",
           "next_at", "last_at", "last_post", "last_error")


class Cron:
    """Standard five-field cron (minute hour day month weekday) with lists, ranges and steps.

    As in cron, when both day and weekday are restricted a day matching either one fires.
    """

    def __init__(self, line: str):
        parts = line.split() if isinstance(line, str) else []
        if len(parts) != 5:
            raise ValueError("A schedule is five fields: minute hour day month weekday.")
        self.sets = [self._field(part, name, low, high) for part, (name, low, high) in zip(parts, FIELDS)]
        # 7 is Sunday too.
        if 7 in self.sets[4]:
            self.sets[4] = (self.sets[4] - {7}) | {0}
        self.any_day, self.any_weekday = parts[2] == "*", parts[4] == "*"

    @staticmethod
    def _field(text, name, low, high):
        values = set()
        for item in text.split(","):
            match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", item)
            if not match:
                raise ValueError(f"The {name} field {text!r} is not cron syntax.")
            span, step = match.group(1), int(match.group(2) or 1)
            start, end = (low, high) if span == "*" else (int(span.split("-")[0]), int(span.split("-")[-1]))
            if not low <= start <= end <= high or step < 1:
                raise ValueError(f"The {name} field {text!r} is outside {low}-{high}.")
            values.update(range(start, end + 1, step))
        return values

    def _day(self, day):
        in_day, in_weekday = day.day in self.sets[2], (day.isoweekday() % 7) in self.sets[4]
        if self.any_day or self.any_weekday:
            return in_day and in_weekday
        return in_day or in_weekday

    def next_after(self, after: float, tz: ZoneInfo) -> float | None:
        """The first matching minute strictly after `after` (epoch seconds), in zone `tz`."""
        start = datetime.fromtimestamp(after, tz).replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        for _ in range(SEARCH_DAYS):
            if day.month in self.sets[3] and self._day(day):
                for hour in sorted(self.sets[1]):
                    for minute in sorted(self.sets[0]):
                        moment = day.replace(hour=hour, minute=minute)
                        if moment >= start:
                            # A local time skipped by a clock change runs at the shifted instant.
                            return moment.replace(tzinfo=tz).timestamp()
            day += timedelta(days=1)
        return None


def describe(line: str) -> str:
    """Plain words for the schedules the app offers; any other line is shown as is."""
    parts = line.split()
    if len(parts) != 5:
        return line
    minute, hour, day, month, weekday = parts
    if not minute.isdigit() or day != "*" or month != "*":
        return f"cron {line}"
    if hour == "*" and weekday == "*":
        return f"Every hour at :{int(minute):02d}"
    if not hour.isdigit():
        return f"cron {line}"
    at = f"{int(hour):02d}:{int(minute):02d}"
    if weekday == "*":
        return f"Every day at {at}"
    if weekday == "1-5":
        return f"Weekdays at {at}"
    if weekday.isdigit() and int(weekday) <= 7:
        return f"{WEEKDAYS[int(weekday) % 7]}s at {at}"
    return f"cron {line}"


def zone(name: object) -> ZoneInfo:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+\-/]{1,64}", name):
        raise HubError(400, "Choose a time zone like Europe/Paris.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HubError(400, f"Unknown time zone {name!r}.") from None


def validate(payload: object) -> dict:
    """A routine to create or replace, under a stable ID chosen by the caller."""
    keys = {"id", "name", "topic", "prompt", "cron", "tz", "enabled"}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise HubError(400, "Provide id, name, topic, prompt, cron, tz and enabled.")
    if not isinstance(payload["id"], str) or not MESSAGE_ID.fullmatch(payload["id"]):
        raise HubError(400, "id must be 32 lowercase hexadecimal characters, unchanged on retry")
    name = payload["name"].strip() if isinstance(payload["name"], str) else ""
    if not name or len(name) > NAME_LIMIT:
        raise HubError(400, f"Give the routine a name of at most {NAME_LIMIT} characters.")
    if not isinstance(payload["topic"], str) or not TOPIC.fullmatch(payload["topic"]):
        raise HubError(400, "A channel name uses lowercase letters, digits and dashes.")
    if not isinstance(payload["prompt"], str) or not payload["prompt"].strip():
        raise HubError(400, "Write what the routine asks for; it is posted in the channel each time.")
    if len(payload["prompt"]) > PROMPT_LIMIT:
        raise HubError(413, "The routine prompt is too large.")
    try:
        cron = Cron(payload["cron"])
    except ValueError as error:
        raise HubError(400, str(error)) from None
    tz = zone(payload["tz"])
    if type(payload["enabled"]) is not bool:
        raise HubError(400, "enabled is true or false.")
    if cron.next_after(time.time(), tz) is None:
        raise HubError(400, "This schedule never comes round.")
    return {**payload, "name": name, "cron": " ".join(payload["cron"].split())}


def fire_id(routine: str, scheduled: float) -> str:
    return hashlib.sha256(f"routine:{routine}:{int(scheduled)}".encode()).hexdigest()[:32]


def message(row: dict) -> str:
    return f"⏰ Routine “{row['name']}” ({describe(row['cron'])}, {row['tz']})\n\n{row['prompt']}"


class Routines:
    """Routines kept in the hub's database, beside the channels they post in."""

    def __init__(self, hub, send):
        self.hub, self.send = hub, send
        with hub._db() as db:
            db.executescript(SCHEMA)

    def _rows(self, db, where="", args=()):
        rows = db.execute(f"SELECT {', '.join(COLUMNS)} FROM routines {where} ORDER BY name, id", args)
        return [dict(zip(COLUMNS, row)) for row in rows]

    def listing(self) -> list[dict]:
        with self.hub._db() as db:
            rows = self._rows(db)
        return [{**row, "enabled": bool(row["enabled"]), "schedule": describe(row["cron"])} for row in rows]

    def save(self, payload) -> dict:
        routine = validate(payload)
        now = time.time()
        upcoming = Cron(routine["cron"]).next_after(now, zone(routine["tz"])) if routine["enabled"] else None
        with self.hub.lock, self.hub._db() as db:
            existing = db.execute("SELECT created FROM routines WHERE id = ?", (routine["id"],)).fetchone()
            if existing is None and db.execute("SELECT COUNT(*) FROM routines").fetchone()[0] >= ROUTINES_LIMIT:
                raise HubError(409, f"The workspace keeps at most {ROUTINES_LIMIT} routines.")
            db.execute("INSERT INTO routines (id, name, topic, prompt, cron, tz, enabled, created, updated, next_at)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name,"
                       " topic = excluded.topic, prompt = excluded.prompt, cron = excluded.cron, tz = excluded.tz,"
                       " enabled = excluded.enabled, updated = excluded.updated, next_at = excluded.next_at,"
                       " last_error = ''",
                       (routine["id"], routine["name"], routine["topic"], routine["prompt"], routine["cron"],
                        routine["tz"], int(routine["enabled"]), existing[0] if existing else now, now, upcoming))
        return self.get(routine["id"])

    def get(self, ident: str) -> dict:
        with self.hub._db() as db:
            rows = self._rows(db, "WHERE id = ?", (ident,))
        if not rows:
            raise HubError(404, "No routine has this id.")
        return {**rows[0], "enabled": bool(rows[0]["enabled"]), "schedule": describe(rows[0]["cron"])}

    def act(self, payload) -> dict:
        """Pause, resume, run now or delete one routine."""
        if (not isinstance(payload, dict) or set(payload) != {"id", "action"}
                or payload["action"] not in ("pause", "resume", "run", "delete")):
            raise HubError(400, "Provide id and an action: pause, resume, run or delete.")
        routine = self.get(payload["id"]) if isinstance(payload["id"], str) else self.get("")
        if payload["action"] == "delete":
            with self.hub.lock, self.hub._db() as db:
                db.execute("DELETE FROM routines WHERE id = ?", (routine["id"],))
            return {"id": routine["id"], "deleted": True}
        if payload["action"] == "run":
            return self._fire(routine, time.time(), manual=True)
        enabled = payload["action"] == "resume"
        upcoming = Cron(routine["cron"]).next_after(time.time(), zone(routine["tz"])) if enabled else None
        with self.hub.lock, self.hub._db() as db:
            db.execute("UPDATE routines SET enabled = ?, next_at = ?, updated = ? WHERE id = ?",
                       (int(enabled), upcoming, time.time(), routine["id"]))
        return self.get(routine["id"])

    def _fire(self, routine: dict, scheduled: float, manual: bool = False, now: float | None = None) -> dict:
        # A manual run gets its own ID per second, so it never collides with the scheduled fire.
        ident = fire_id(routine["id"] + (":manual" if manual else ""), scheduled)
        try:
            self.send({"id": ident, "target": "#" + routine["topic"], "text": message(routine)})
        except HubError as error:
            with self.hub.lock, self.hub._db() as db:
                db.execute("UPDATE routines SET last_error = ? WHERE id = ?", (str(error)[:500], routine["id"]))
            raise
        now = time.time() if now is None else now
        upcoming = (Cron(routine["cron"]).next_after(max(now, scheduled), zone(routine["tz"]))
                    if routine["enabled"] else None)
        with self.hub.lock, self.hub._db() as db:
            if manual:
                db.execute("UPDATE routines SET last_at = ?, last_post = ?, last_error = '' WHERE id = ?",
                           (now, ident, routine["id"]))
            else:
                db.execute("UPDATE routines SET last_at = ?, last_post = ?, last_error = '', next_at = ? WHERE id = ?",
                           (now, ident, upcoming, routine["id"]))
        return self.get(routine["id"])

    def fire_due(self, now: float | None = None) -> list[str]:
        """Post every enabled routine whose time has come; a fire missed while down runs once."""
        now = time.time() if now is None else now
        with self.hub._db() as db:
            due = self._rows(db, "WHERE enabled = 1 AND next_at IS NOT NULL AND next_at <= ?", (now,))
        fired = []
        for routine in due:
            try:
                self._fire({**routine, "enabled": True}, routine["next_at"], now=now)
                fired.append(routine["id"])
            except HubError:
                continue
        return fired
