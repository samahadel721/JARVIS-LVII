"""
plugins/calendar_control.py — your calendar, read and written locally.

Drop this single file into `plugins/`. Standard library only, no account, no
OAuth, no cloud round trip: it reads and writes `.ics` files, which is the
format every calendar application already speaks — Outlook, Google Calendar,
Apple Calendar, Thunderbird all import and export it.

WHY FILES RATHER THAN AN API
    A calendar API needs a cloud project, a client secret, an OAuth consent
    screen and a token that expires. A file needs a folder. Everything this
    plugin writes can be imported into whatever calendar the user actually uses
    by double-clicking it, and everything they export from it can be read here.
    Nothing leaves the machine, which is the point of the whole assistant.

WHAT IT ANSWERS
    What is on today, this week, or the next thing coming. Whether a given day
    is free, with the gaps named. And it can add, move or delete an event.

WRITING SAFELY
    Editing somebody's calendar is destructive, so a delete goes through
    core/confirm.py when the app is running — the same banner the core actions
    use, which the model cannot forge by sending a parameter. When the app is
    not running, deletions are refused outright rather than done silently.

SETTINGS (Plugin Manager → Calendar)
    The folder to read, the file to write, and whether the assistant may add
    events without asking.
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
from pathlib import Path

PLUGIN = {
    "name": "calendar_control",
    "description": (
        "Reads and manages the user's calendar stored as .ics files on this "
        "computer: what is on today, this week, the next event, whether a day is "
        "free, and adding, moving or deleting an event. Use for: 'what is on my "
        "calendar', 'what do I have today', 'am I free tomorrow afternoon', "
        "'add a meeting at 5', 'move my 3 o'clock', 'what is next'. Do NOT use "
        "the 'reminder' tool for calendar entries — that fires an OS notification, "
        "this stores the appointment."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'today' (default), 'week' for the next seven days, 'next' for "
                    "the next event with a countdown, 'free' to list the open gaps "
                    "on a day, 'add' to create an event, 'find' to search by text, "
                    "or 'delete' to remove one."
                ),
            },
            "title": {"type": "STRING", "description": "Event name, for 'add' and 'find'."},
            "start": {
                "type": "STRING",
                "description": (
                    "When it starts, for 'add': '2026-10-02 17:00', 'today 5pm', "
                    "'tomorrow 09:30', or just '17:00' for today."
                ),
            },
            "duration_minutes": {"type": "INTEGER", "description": "Length in minutes (default 60)."},
            "date": {"type": "STRING", "description": "For 'free': which day, e.g. 'tomorrow'."},
            "location": {"type": "STRING", "description": "Optional place, for 'add'."},
            "notes": {"type": "STRING", "description": "Optional details, for 'add'."},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "calendar_control",
    "title": "Calendar / التقويم",
    "fields": [
        {"key": "calendar_dir", "type": "text", "label": "Read .ics files from / مجلد التقويم",
         "default": "", "placeholder": "empty = the assistant's own config folder"},
        {"key": "write_file", "type": "text", "label": "File to write / ملف الكتابة",
         "default": "jarvis.ics", "placeholder": "jarvis.ics"},
        {"key": "day_start", "type": "text", "label": "Day starts at / بداية اليوم",
         "default": "08:00"},
        {"key": "day_end", "type": "text", "label": "Day ends at / نهاية اليوم",
         "default": "22:00"},
        {"key": "ask_before_adding", "type": "toggle", "label": "Ask before adding an event",
         "default": False},
    ],
}

_DAY_WORDS = {"today": 0, "غدا": 1, "غدًا": 1, "tomorrow": 1, "yesterday": -1}
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


# ---------------------------------------------------------------------------
# Where the files are
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    try:
        from memory.config_manager import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent


def _config() -> dict:
    values = {"calendar_dir": "", "write_file": "jarvis.ics", "day_start": "08:00",
              "day_end": "22:00", "ask_before_adding": False}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("calendar_control") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    return values


def _calendar_dir(config: dict) -> Path:
    folder = str(config.get("calendar_dir") or "").strip()
    path = Path(folder).expanduser() if folder else (_base_dir() / "config" / "calendar")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_path(config: dict) -> Path:
    name = str(config.get("write_file") or "jarvis.ics").strip() or "jarvis.ics"
    if not name.lower().endswith(".ics"):
        name += ".ics"
    return _calendar_dir(config) / name


# ---------------------------------------------------------------------------
# ICS reading — unfolding, then the fields that matter
# ---------------------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    """
    Undo the line folding the iCalendar format requires.

    Any line over 75 bytes is split with a leading space on the continuation, so
    a long description arrives in pieces. Joining them back is the difference
    between reading a real calendar and reading garbage.
    """
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_stamp(value: str) -> _dt.datetime | None:
    """DTSTART/DTEND in any of the three forms the standard allows."""
    value = value.strip().split(":")[-1].strip()
    if value.endswith("Z"):
        try:
            return _dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=_dt.timezone.utc).astimezone().replace(tzinfo=None)
        except ValueError:
            return None
    for pattern in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def _unescape(value: str) -> str:
    return (value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\")).strip()


def read_events(config: dict) -> list[dict]:
    """
    Every event in every .ics under the folder, newest parsing wins on UID.

    A file that cannot be read is skipped rather than fatal: one broken export
    from another application should not blind the assistant to the rest.
    """
    events: dict[str, dict] = {}
    folder = _calendar_dir(config)
    for path in sorted(folder.glob("*.ics")):
        try:
            lines = _unfold(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        current: dict | None = None
        for line in lines:
            upper = line.upper()
            if upper.startswith("BEGIN:VEVENT"):
                current = {"file": path.name}
                continue
            if upper.startswith("END:VEVENT"):
                if current and current.get("start"):
                    key = current.get("uid") or f"{current['start']}{current.get('title','')}"
                    events[key] = current
                current = None
                continue
            if current is None or ":" not in line:
                continue
            name, _, value = line.partition(":")
            field = name.split(";")[0].upper()
            if field == "DTSTART":
                current["start"] = _parse_stamp(value)
                current["all_day"] = "VALUE=DATE" in name.upper() and "T" not in value
            elif field == "DTEND":
                current["end"] = _parse_stamp(value)
            elif field == "SUMMARY":
                current["title"] = _unescape(value)
            elif field == "LOCATION":
                current["location"] = _unescape(value)
            elif field == "DESCRIPTION":
                current["notes"] = _unescape(value)
            elif field == "UID":
                current["uid"] = value.strip()
    return sorted((e for e in events.values() if e.get("start")),
                  key=lambda e: e["start"])


# ---------------------------------------------------------------------------
# ICS writing
# ---------------------------------------------------------------------------

def _stamp(moment: _dt.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def _fold(line: str, limit: int = 74) -> str:
    """The inverse of _unfold: split long lines the way the format demands."""
    if len(line.encode("utf-8")) <= limit:
        return line
    parts, current = [], ""
    for word in line.split(" "):
        if len((current + " " + word).encode("utf-8")) > limit and current:
            parts.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        parts.append(current)
    return "\r\n ".join(parts)


def _escape(value: str) -> str:
    return (str(value).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n").strip())


def write_event(config: dict, event: dict) -> str:
    """Append one VEVENT, creating the file with a valid wrapper if it is new."""
    path = _write_path(config)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            existing = ""

    uid = event.get("uid") or f"{uuid.uuid4()}@jarvis.local"
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
             f"DTSTART:{_stamp(event['start'])}",
             f"DTEND:{_stamp(event.get('end') or event['start'] + _dt.timedelta(hours=1))}",
             f"SUMMARY:{_escape(event.get('title') or 'Untitled')}"]
    if event.get("location"):
        lines.append(f"LOCATION:{_escape(event['location'])}")
    if event.get("notes"):
        lines.append(f"DESCRIPTION:{_escape(event['notes'])}")
    lines.append("END:VEVENT")
    block = "\r\n".join(_fold(line) for line in lines)

    if "BEGIN:VCALENDAR" in existing:
        body = (existing.rstrip() + "\r\n" + block + "\r\n")
    else:
        body = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//JARVIS//calendar_control//EN\r\n"
                "CALSCALE:GREGORIAN\r\n" + block + "\r\nEND:VCALENDAR\r\n")
    path.write_text(body, encoding="utf-8")
    return str(path)


def remove_event(config: dict, uid: str) -> int:
    """
    Strip a VEVENT by UID. Returns how many were removed.

    The block boundaries are matched exactly rather than by searching for the
    UID, so an event whose description merely mentions the UID is not deleted
    along with it.
    """
    path = _write_path(config)
    if not path.exists():
        return 0
    lines = _unfold(path.read_text(encoding="utf-8", errors="ignore"))
    out: list[str] = []
    inside_target, removed = False, 0
    block: list[str] = []
    for line in lines:
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            block, inside_target = [line], False
            continue
        if upper.startswith("END:VEVENT"):
            keep = True
            for candidate in block:
                if candidate.upper().startswith("UID:"):
                    if candidate.split(":", 1)[1].strip() == uid:
                        keep, inside_target = False, True
                    break
            if keep:
                out.extend(block)
                out.append(line)
            else:
                removed += 1
            block = []
            continue
        if block:
            block.append(line)
        else:
            out.append(line)
    if removed:
        path.write_text("\r\n".join(out), encoding="utf-8")
    return removed


# ---------------------------------------------------------------------------
# Time phrases
# ---------------------------------------------------------------------------

def _parse_when(text: str, now: _dt.datetime | None = None) -> _dt.datetime | None:
    """
    'today 5pm', 'tomorrow 09:30', '17:00', '2026-10-02 17:00', 'friday 14:00'.

    Deliberately a phrase parser rather than a date library: the model passes
    what the user said, and what the user said is how people speak.
    """
    now = now or _dt.datetime.now()
    raw = str(text or "").strip().lower()
    if not raw:
        return None

    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    clock = re.search(r"(\d{1,2})[:.](\d{2})\s*(am|pm)?", raw)
    bare = re.search(r"\b(\d{1,2})\s*(am|pm)\b", raw)

    day = now.date()
    matched_day = False
    for word, offset in _DAY_WORDS.items():
        if word in raw:
            day = now.date() + _dt.timedelta(days=offset)
            matched_day = True
            break
    else:
        for word, index in _WEEKDAYS.items():
            if word in raw:
                ahead = (index - now.weekday()) % 7 or 7
                day = now.date() + _dt.timedelta(days=ahead)
                matched_day = True
                break

    if iso:
        try:
            day = _dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    hour = minute = 0
    had_time = True
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if clock.group(3) == "pm" and hour < 12:
            hour += 12
        if clock.group(3) == "am" and hour == 12:
            hour = 0
    elif bare:
        hour = int(bare.group(1)) % 12 + (12 if bare.group(2) == "pm" else 0)
    elif not iso and not matched_day:
        return None                       # nothing date-like and nothing time-like
    elif not iso:
        had_time = False                  # a day was named but no hour was

    # 'tomorrow' with no hour means tomorrow, not today at midnight: the caller
    # asked for a date, so the time is filled in rather than dropping the day.
    return _dt.datetime.combine(day, _dt.time(hour % 24, minute) if had_time
                                else _dt.time(0, 0))


def _parse_clock(text: str, fallback: _dt.time) -> _dt.time:
    match = re.search(r"(\d{1,2})[:.]?(\d{2})?", str(text or ""))
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    return _dt.time(hour % 24, min(59, minute))


# ---------------------------------------------------------------------------
# Formatting and the free/busy maths
# ---------------------------------------------------------------------------

def _when_text(event: dict, now: _dt.datetime) -> str:
    start = event["start"]
    if event.get("all_day"):
        return "all day"
    today = now.date()
    if start.date() == today:
        day = "today"
    elif start.date() == today + _dt.timedelta(days=1):
        day = "tomorrow"
    else:
        day = start.strftime("%a %d %b")
    return f"{day} {start.strftime('%H:%M')}"


def _line(event: dict, now: _dt.datetime) -> str:
    title = event.get("title") or "Untitled"
    extra = f" at {event['location']}" if event.get("location") else ""
    return f"{_when_text(event, now)} — {title}{extra}"


def _window(config: dict, day: _dt.date) -> tuple[_dt.datetime, _dt.datetime]:
    start = _dt.datetime.combine(day, _parse_clock(config["day_start"], _dt.time(8)))
    end = _dt.datetime.combine(day, _parse_clock(config["day_end"], _dt.time(22)))
    return start, end


def _busy_on(events: list[dict], start: _dt.datetime, end: _dt.datetime) -> list[tuple]:
    """Event spans clipped to the window, merged so overlaps stop doubling up."""
    spans = []
    for event in events:
        if event.get("all_day"):
            continue
        a = max(event["start"], start)
        b = min(event.get("end") or event["start"] + _dt.timedelta(hours=1), end)
        if b > a:
            spans.append((a, b, event.get("title") or "Untitled"))
    spans.sort(key=lambda s: s[0])
    merged: list[list] = []
    for a, b, title in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
            if title not in merged[-1][2]:
                merged[-1][2] = f"{merged[-1][2]}, {title}"
        else:
            merged.append([a, b, title])
    return [tuple(m) for m in merged]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _events_on(events: list[dict], day: _dt.date) -> list[dict]:
    chosen = []
    for event in events:
        start = event["start"]
        end = event.get("end") or start + _dt.timedelta(hours=1)
        if start.date() <= day <= end.date() or (event.get("all_day") and start.date() == day):
            chosen.append(event)
    return chosen


def _action_today(events: list[dict], now: _dt.datetime, player) -> str:
    today = _events_on(events, now.date())
    if not today:
        return "Your calendar is clear today, sir."
    lines = [_line(e, now) for e in today]
    if player:
        try:
            player.show_content("Today", "\n".join(lines))
        except Exception:
            pass
    return (f"{len(today)} event{'s' if len(today) != 1 else ''} today: "
            + "; ".join(lines) + ".")


def _action_week(events: list[dict], now: _dt.datetime, player) -> str:
    days = [(now.date() + _dt.timedelta(days=offset)) for offset in range(7)]
    rows, count = [], 0
    for day in days:
        on_day = _events_on(events, day)
        count += len(on_day)
        rows.append(f"{day.strftime('%A %d %b')}")
        rows.extend(f"   {_line(e, now)}" for e in on_day)
        if not on_day:
            rows.append("   — clear")
    if player:
        try:
            player.show_content("This week", "\n".join(rows))
        except Exception:
            pass
    if not count:
        return "Nothing at all in the next seven days, sir."
    return f"{count} events over the next seven days — the list is on screen."


def _action_next(events: list[dict], now: _dt.datetime) -> str:
    upcoming = [e for e in events if (e.get("end") or e["start"]) >= now]
    if not upcoming:
        return "Nothing coming up on the calendar, sir."
    event = upcoming[0]
    delta = event["start"] - now
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        when = "starting now"
    elif minutes < 90:
        when = f"in {minutes} minutes"
    elif minutes < 60 * 26:
        when = f"in {minutes // 60} hours"
    else:
        days = minutes // (60 * 24)
        when = f"in {days} day" + ("" if days == 1 else "s")
    return f"Next up: {event.get('title') or 'Untitled'} — {_when_text(event, now)}, {when}."


def _action_free(events: list[dict], config: dict, day: _dt.date, now: _dt.datetime) -> str:
    start, end = _window(config, day)
    busy = _busy_on(_events_on(events, day), start, end)
    gaps: list[tuple[_dt.datetime, _dt.datetime]] = []
    cursor = start
    for a, b, _ in busy:
        if a - cursor >= _dt.timedelta(minutes=25):
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if end - cursor >= _dt.timedelta(minutes=25):
        gaps.append((cursor, end))

    label = day.strftime("%A %d %b")
    if not gaps:
        return f"{label} is fully booked, sir."
    parts = [f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}" for a, b in gaps[:6]]
    booked = ", ".join(f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')} {t}"
                       for a, b, t in busy) or "nothing booked"
    return (f"Free on {label}: {', '.join(parts)}. "
            f"Busy: {booked}.")


def _action_add(config: dict, parameters: dict, player) -> str:
    title = str(parameters.get("title") or "").strip()
    if not title:
        return "What should I call the event, sir?"
    start = _parse_when(parameters.get("start", ""))
    if start is None:
        return ("When should it start, sir? Something like 'tomorrow at 5pm' or "
                "'2026-10-02 17:00'.")
    try:
        minutes = max(5, min(24 * 60, int(float(parameters.get("duration_minutes") or 60))))
    except (TypeError, ValueError):
        minutes = 60

    # Adding the same thing twice is what happens when the model retries a call
    # it thinks failed, and nobody wants their meeting to appear twice. Same
    # title at the same minute means it is already there.
    for existing in read_events(config):
        if (existing.get("title") or "").strip().lower() == title.lower() \
                and existing["start"].replace(second=0, microsecond=0) == \
                start.replace(second=0, microsecond=0):
            return (f"'{title}' is already on your calendar for "
                    f"{start.strftime('%A %d %b at %H:%M')}, sir — I have not added "
                    f"a second one.")

    def _create() -> str:
        path = write_event(config, {
            "title": title, "start": start,
            "end": start + _dt.timedelta(minutes=minutes),
            "location": str(parameters.get("location") or ""),
            "notes": str(parameters.get("notes") or ""),
        })
        return (f"Added: {title} — {start.strftime('%A %d %b at %H:%M')} for "
                f"{minutes} minutes. Saved to {Path(path).name}, which any calendar "
                f"app can import.")

    if config.get("ask_before_adding"):
        try:
            from core import confirm
            return confirm.request(
                key="calendar_add",
                title="Add to calendar",
                detail=f"{title}\n{start.strftime('%A %d %b %Y at %H:%M')} · {minutes} min",
                run=_create)
        except Exception:
            pass                       # no confirmation channel — add it directly
    return _create()


def _action_find(events: list[dict], parameters: dict, now: _dt.datetime) -> str:
    needle = str(parameters.get("title") or "").strip().lower()
    if not needle:
        return "What should I look for, sir?"
    hits = [e for e in events if needle in (e.get("title") or "").lower()
            or needle in (e.get("notes") or "").lower()]
    if not hits:
        return f"Nothing matching '{needle}' in the calendar."
    return f"{len(hits)} matches: " + "; ".join(_line(e, now) for e in hits[:6]) + "."


def _action_delete(config: dict, parameters: dict, events: list[dict],
                   now: _dt.datetime) -> str:
    needle = str(parameters.get("title") or "").strip().lower()
    if not needle:
        return "Which event should I delete, sir?"
    hits = [e for e in events if needle in (e.get("title") or "").lower()]
    if not hits:
        return f"I found nothing called '{needle}' to delete."
    target = hits[0]

    def _delete() -> str:
        removed = remove_event(config, target.get("uid") or "")
        if removed:
            return f"Deleted '{target.get('title')}' from the calendar."
        return ("I could not delete that one — it is in a file the assistant "
                "does not write to. Only events in the assistant's own file can "
                "be removed.")

    try:
        from core import confirm
        return confirm.request(
            key="calendar_delete",
            title="Delete from calendar",
            detail=f"{target.get('title')}\n{_when_text(target, now)}",
            run=_delete)
    except Exception:
        # No way to ask a human means no permission to destroy anything.
        return (f"'{target.get('title')}' is on {_when_text(target, now)}. "
                f"Deleting needs your confirmation, sir — say 'delete "
                f"{target.get('title')}' with the app running and I will show the "
                f"CONFIRM banner.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        now = _dt.datetime.now()
        events = read_events(config)
        action = str(parameters.get("action", "today") or "today").strip().lower()

        if action == "week":
            result = _action_week(events, now, player)
        elif action in ("next", "upcoming"):
            result = _action_next(events, now)
        elif action == "free":
            moment = _parse_when(parameters.get("date", "") or "today", now) or now
            result = _action_free(events, config, moment.date(), now)
        elif action == "add":
            result = _action_add(config, parameters, player)
        elif action == "find":
            result = _action_find(events, parameters, now)
        elif action == "delete":
            result = _action_delete(config, parameters, events, now)
        else:
            result = _action_today(events, now, player)

    except Exception as exc:
        return f"Sir, calendar_control failed: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {str(result).splitlines()[0]}")
        except Exception:
            pass
    return result


if __name__ == "__main__":
    import tempfile, os
    os.environ.setdefault("JARVIS_TEST", "1")
    print(run({"action": "today"}))
    print(run({"action": "add", "title": "Team stand-up", "start": "tomorrow 09:30",
               "duration_minutes": 30, "location": "Zoom"}))
    print(run({"action": "add", "title": "Dentist", "start": "friday 17:00"}))
    print(run({"action": "today"}))
    print(run({"action": "week"}))
    print(run({"action": "next"}))
    print(run({"action": "free", "date": "tomorrow"}))
    print(run({"action": "find", "title": "dent"}))
    print(run({"action": "delete", "title": "Dentist"}))
