"""
calendar_events.py — التقويم والأحداث والتذكيرات (محلي، بلا حساب خارجي).

ينشئ ويقرأ ويعدّل الأحداث، يبحث فيها، يوسّم، يكشف التعارض، يوسّع التكرار
(يومي/أسبوعي/شهري) بأن يعدد المواعيد الحقيقية، ويُذكّر في وقتها — والتذكير
يُنشئ أتمتة حقيقية في `automation_engine` (مرة واحدة عند الموعد) وسيط النبضة
`workflow_scheduler` هو الذي يفحص وينادي.

يعمل بلا إنترنت وبلا حساب: مصادر Google/Microsoft تحتاج OAuth، ولذلك تُعلَن
بصراحة كغير مُهيّأة حتى تضبطها أنت — ولا يُخترع شيء، ولا يُدّعى تكامل قائم.

السلامة: بياناتك في `config/orchestration/calendar.json` وحدها، ولا ملف خارجها؛
والتصدير إلى ics يكتب في مجلد مستندات جارفس باسم مُنقّى لا يقبل اجتياز مسارات.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "calendar_events",
    "description": (
        "Your own calendar and event system: create, read and update events, search them, see "
        "today and upcoming, detect conflicts between two events, repeat events (daily, weekly, "
        "monthly) with real occurrence expansion, and set reminders that fire on time through the "
        "automation engine. Use for: 'add a meeting tomorrow at 5 pm', 'what is on my calendar "
        "today', 'what's next', 'move the dentist appointment to Sunday', 'any conflict at 3 pm', "
        "'remind me 15 minutes before the meeting', 'find the flight event', 'delete the meeting' "
        "(asks first). Works fully offline; Google/Microsoft sync needs OAuth you set up yourself."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("add | today | upcoming | list | show | search | update | delete | conflicts | "
                                "reminders | schedule_reminder | providers | export_ics | help"),
            },
            "title": {"type": "STRING", "description": "Event title."},
            "when": {"type": "STRING", "description": "Start: 'غدًا 5 مساءً' / 'tomorrow 14:30' / ISO '2026-10-05 14:30'."},
            "duration_min": {"type": "STRING", "description": "Minutes (default 60)."},
            "end": {"type": "STRING", "description": "Explicit end time (overrides duration)."},
            "location": {"type": "STRING", "description": "Place."},
            "notes": {"type": "STRING", "description": "Notes."},
            "recurrence": {"type": "STRING", "description": "Text ('كل يوم'/'every monday') or JSON {\"kind\":\"weekly\",\"days\":[6]}."},
            "reminder_min": {"type": "STRING", "description": "Minutes before to remind, e.g. '15' or '15,5'."},
            "id": {"type": "STRING", "description": "Event id (for show/update/delete)."},
            "query": {"type": "STRING", "description": "Search text."},
            "days": {"type": "STRING", "description": "Range in days for upcoming/list (default 7)."},
            "date": {"type": "STRING", "description": "A specific day (ISO) for conflicts/today."},
            "fire": {"type": "STRING", "description": "reminders: 'yes' actually sends the due ones."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "calendar_events",
    "title": "Calendar & events / التقويم والأحداث",
    "fields": [
        {"key": "default_duration_min", "type": "text", "label": "Default event length, minutes", "default": "60"},
        {"key": "default_reminder_min", "type": "text", "label": "Default reminder, minutes before", "default": "15"},
        {"key": "day_start", "type": "text", "label": "Day starts at (HH:MM) for today's view", "default": "00:00"},
        {"key": "export_dir", "type": "text", "label": "Folder for .ics export", "default": ""},
        {"key": "conflict_check", "type": "text", "label": "Warn about overlaps when adding (yes/no)", "default": "yes"},
        {"key": "notify_plugin", "type": "text", "label": "Plugin that delivers the reminder", "default": "notification_center"},
    ],
}

_DAY_WORDS = {
    "monday": 0, "mon": 0, "الاثنين": 0, "الاتنين": 0,
    "tuesday": 1, "tue": 1, "الثلاثاء": 1,
    "wednesday": 2, "wed": 2, "الأربعاء": 2, "الاربعاء": 2,
    "thursday": 3, "thu": 3, "الخميس": 3,
    "friday": 4, "fri": 4, "الجمعة": 4,
    "saturday": 5, "sat": 5, "السبت": 5,
    "sunday": 6, "sun": 6, "الأحد": 6, "الاحد": 6,
}
_DAY_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def _core():
    name = "_jarvis_orch_core"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / "_orch_core.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        return None
    return module


C = _core()


def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    if C is None:
        return defaults
    return C.cfg("calendar_events", PLUGIN_SETTINGS["fields"])


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except Exception:
        return default


def _log(player, text: str) -> None:
    try:
        if player is not None and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass


def _export_dir() -> Path:
    raw = str(_cfg().get("export_dir") or "").strip()
    return Path(raw).expanduser() if raw else (Path.home() / "Documents" / "JARVIS_Calendar")


# ── المخزن ────────────────────────────────────────────────────────────────────
def _load() -> dict:
    data = C.load_json(C.CALENDAR_FILE, {"version": 1, "items": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "items": {}}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def _save(data: dict) -> bool:
    return C.save_json(C.CALENDAR_FILE, data)


def _new_id(title: str) -> str:
    stamp = C.now().strftime("%Y%m%d%H%M%S")
    slug = "".join(ch for ch in str(title) if ch.isalnum())[:10] or "ev"
    return f"ev_{slug}_{stamp}"


def _find(key: str) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    wanted = str(key or "").strip()
    if not wanted:
        return None, None, "أي حدث تقصد؟"
    data = _load()
    if wanted in data["items"]:
        return data["items"][wanted], wanted, ""
    low = wanted.lower()
    exact = [(k, i) for k, i in data["items"].items() if str(i.get("title", "")).strip().lower() == low]
    if len(exact) == 1:
        return exact[0][1], exact[0][0], ""
    partial = [(k, i) for k, i in data["items"].items() if low in str(i.get("title", "")).lower()]
    if len(partial) == 1:
        return partial[0][1], partial[0][0], ""
    if len(partial) > 1:
        return None, None, f"أكثر من حدث مطابق: {'، '.join(str(p[1].get('title')) for p in partial[:5])}"
    return None, None, f"لا حدث بهذا الوصف '{C.clip(wanted, 40)}'."


# ── الوقت ─────────────────────────────────────────────────────────────────────
def _parse_start(text: Any) -> tuple[Optional[_dt.datetime], str]:
    """يفهم: ISO · 'غدًا 5 مساءً' · 'بعد ساعتين' · 'الأحد 9' · '5:30 م' · 'today 14:30'."""
    raw = C.normalize_digits(str(text or "").strip())
    if not raw:
        return None, "متى الحدث؟ قل لي وقتًا (مثال: 'غدًا 5 مساءً')."
    now = C.now()
    # ISO: 2026-10-05 [14:30]
    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", raw)
    if iso:
        try:
            date = _dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            hour = int(iso.group(4)) if iso.group(4) else 9
            minute = int(iso.group(5)) if iso.group(5) else 0
            return _dt.datetime.combine(date, _dt.time(hour, minute)), ""
        except Exception:
            return None, f"تاريخ غير صالح: {C.clip(raw, 30)}"
    # بعد N دقيقة/ساعة/يوم
    delay = re.search(r"بعد\s*(\d{1,3})\s*(دقيقة|دقائق|دقيقه|د|ساعة|ساعات|ساعه|يوم|أيام|ايام)", raw)
    if delay:
        amount = int(delay.group(1))
        unit = delay.group(2)
        if unit.startswith("د"):
            delta = _dt.timedelta(minutes=amount)
        elif unit.startswith("س"):
            delta = _dt.timedelta(hours=amount)
        else:
            delta = _dt.timedelta(days=amount)
        return now + delta, ""
    # اليوم/غدًا
    base = now
    if re.search(r"(tomorrow|غد[ًا]?|بكرة)", raw, re.I):
        base = now + _dt.timedelta(days=1)
    elif re.search(r"(today|اليوم|النهاردة|النهارده)", raw, re.I):
        base = now
    else:
        for word, index in _DAY_WORDS.items():
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", raw, re.I):
                ahead = (index - now.weekday()) % 7
                base = now + _dt.timedelta(days=ahead if ahead else 7)
                break
    hm = C.parse_hhmm(raw)
    if hm is None:
        # تاريخ بلا وقت: نضع 09:00 ونعلن ذلك
        return _dt.datetime.combine(base.date(), _dt.time(9, 0)), "لا وقت محدّد — وضعته 9:00 صباحًا."
    start = _dt.datetime.combine(base.date(), _dt.time(hm[0], hm[1]))
    if start <= now and not re.search(r"(today|اليوم|النهاردة|النهارده)", raw, re.I):
        start += _dt.timedelta(days=1)      # وقت مضى اليوم → غدًا
    return start, ""


def _fmt(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _friendly(dt: _dt.datetime, now: Optional[_dt.datetime] = None) -> str:
    now = now or C.now()
    days = (dt.date() - now.date()).days
    label = {0: "اليوم", 1: "غدًا", 2: "بعد غد"}.get(days, f"{_DAY_AR[dt.weekday()]} {dt.strftime('%Y-%m-%d')}")
    return f"{label} {dt.strftime('%H:%M')}"


# ── التكرار والتعارض ──────────────────────────────────────────────────────────
def _strip_conjunction(text: str) -> str:
    """يزيل واو العطف الملتصقة بكلمة عربية: 'والأحد' → ' الأحد' (وإلا ضاع اسم اليوم)."""
    return re.sub(r"\sو(?=[\u0600-\u06FF])", " ", str(text or ""))


def _parse_recurrence(raw: Any) -> tuple[dict, str]:
    if raw in (None, ""):
        return {"kind": "none"}, ""
    if isinstance(raw, dict):
        return raw, ""
    text = _strip_conjunction(C.normalize_digits(str(raw)).strip().lower())
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed, ""
        except Exception as exc:
            return {}, f"تعذّرت قراءة recurrence: {C.clip(exc, 80)}"
    if re.search(r"(daily|كل يوم|يوميًا|يوميا)", text):
        until = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        return {"kind": "daily", "until": until.group(1) if until else ""}, ""
    if re.search(r"(weekly|أسبوعيًا|اسبوعيا|كل أسبوع|كل اسبوع)", text):
        days = sorted({i for w, i in _DAY_WORDS.items()
                       if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text, re.I)})
        return {"kind": "weekly", "days": days, "until": ""}, ""
    if re.search(r"(monthly|شهريًا|شهريا|كل شهر)", text):
        return {"kind": "monthly", "until": ""}, ""
    return {"kind": "none"}, ""


def _occurrences(event: dict, start: _dt.datetime, end: _dt.datetime) -> list[_dt.datetime]:
    """يعدّد مواعيد حدث (يثبّت التكرار) داخل مدى زمني — بلا حدود لانهائية."""
    base = C.from_iso(event.get("start"))
    if base is None:
        return []
    rule = event.get("recurrence") if isinstance(event.get("recurrence"), dict) else {"kind": "none"}
    kind = str(rule.get("kind") or "none")
    if kind == "none":
        return [base] if start <= base <= end else []
    until = C.from_iso(rule.get("until")) or (start + _dt.timedelta(days=366))
    limit = min(end, until)
    out: list[_dt.datetime] = []
    if kind == "daily":
        step = _dt.timedelta(days=1)
        cursor = base
    elif kind == "weekly":
        out = []
        days = [int(d) for d in (rule.get("days") or []) if str(d).lstrip("-").isdigit()]
        if not days:
            days = [base.weekday()]
        cursor = start - _dt.timedelta(days=start.weekday())
        for week in range(0, 54):
            for day in sorted(days):
                cand = cursor + _dt.timedelta(days=week * 7 + day)
                cand = cand.replace(hour=base.hour, minute=base.minute, second=0, microsecond=0)
                if cand >= base and start <= cand <= limit:
                    out.append(cand)
            if cursor + _dt.timedelta(days=week * 7) > limit:
                break
        return sorted(out)
    elif kind == "monthly":
        cursor = base
        for _ in range(0, 24):
            if start <= cursor <= limit:
                out.append(cursor)
            month = cursor.month + 1
            year = cursor.year + (1 if month > 12 else 0)
            month = 1 if month > 12 else month
            try:
                cursor = cursor.replace(year=year, month=month)
            except Exception:
                break
        return sorted(out)
    else:
        return [base] if start <= base <= end else []
    guard = 0
    while cursor <= limit and guard < 800:
        if cursor >= base and cursor >= start:
            out.append(cursor)
        cursor = cursor + step
        guard += 1
    return sorted(out)


def _events_between(start: _dt.datetime, end: _dt.datetime) -> list[tuple[_dt.datetime, dict]]:
    data = _load()
    rows: list[tuple[_dt.datetime, dict]] = []
    for event in data["items"].values():
        for moment in _occurrences(event, start, end):
            rows.append((moment, event))
    rows.sort(key=lambda row: row[0])
    return rows


def _conflicts(start: _dt.datetime, end: _dt.datetime, ignore_id: str = "") -> list[dict]:
    rows = []
    for moment, event in _events_between(start - _dt.timedelta(days=1), end + _dt.timedelta(days=1)):
        if str(event.get("id")) == str(ignore_id):
            continue
        other_start = moment
        other_end = other_start + _dt.timedelta(minutes=int(event.get("duration_min") or 60))
        if other_start < end and start < other_end:
            rows.append({"id": event.get("id"), "title": event.get("title"),
                         "start": _fmt(other_start), "end": _fmt(other_end)})
    return rows


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_add(params: dict, player) -> str:
    title = str(params.get("title") or "").strip()
    if not title:
        return "سيدي، ما عنوان الحدث؟"
    start, note = _parse_start(params.get("when") or params.get("start"))
    if start is None:
        return f"سيدي، {note}"
    duration = _int(params.get("duration_min"), _int(_cfg().get("default_duration_min"), 60, 5, 1440), 5, 1440)
    end_text = str(params.get("end") or "").strip()
    if end_text:
        end_dt, why = _parse_start(end_text)
        if end_dt is None or end_dt <= start:
            return f"سيدي، وقت النهاية غير صالح ({why or 'قبل البداية'})."
        duration = max(5, int((end_dt - start).total_seconds() // 60))
    recurrence, why = _parse_recurrence(params.get("recurrence"))
    if why:
        return f"سيدي، {why}"
    reminders_param = str(params.get("reminder_min") or "").strip()
    if reminders_param:
        minutes = []
        for chunk in reminders_param.replace("؛", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                minutes.append(_int(chunk, 15, 0, 10080))
        minutes = sorted({m for m in minutes}, reverse=True)
    else:
        minutes = [_int(_cfg().get("default_reminder_min"), 15, 0, 10080)]
    event_id = _new_id(title)
    event = {
        "id": event_id, "title": title, "start": _fmt(start), "duration_min": duration,
        "location": str(params.get("location") or "").strip(), "notes": str(params.get("notes") or "").strip(),
        "all_day": C.parse_bool(params.get("all_day"), False), "recurrence": recurrence,
        "reminders": [{"minutes_before": m, "sent": ""} for m in minutes if m > 0],
        "source": "local", "created": C.now_iso(), "updated": C.now_iso(), "tags": [],
    }
    data = _load()
    data["items"][event_id] = event
    if not _save(data):
        return "سيدي، تعذّر حفظ الحدث."
    C.log_event("calendar_events", "add", "ok", 0, "", {"id": event_id, "start": event["start"]})
    lines = [f"سيدي، أضفت '{title}' ({event_id}).",
             f"  البداية: {_friendly(start)} ({event['start']}) · المدة: {duration} دقيقة"]
    if note:
        lines.append(f"  ملاحظة: {note}")
    if recurrence.get("kind") != "none":
        lines.append(f"  التكرار: {_describe_recurrence(recurrence)}")
    if minutes:
        lines.append("  التذكير: " + "، ".join(f"قبل {m} دقيقة" for m in minutes))
    if C.parse_bool(_cfg().get("conflict_check"), True):
        clashes = _conflicts(start, start + _dt.timedelta(minutes=duration), ignore_id=event_id)
        if clashes:
            lines.append("  ⚠ تعارض مع: " + "، ".join(f"{c['title']} ({c['start']}—{c['end']})" for c in clashes))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _describe_recurrence(rule: dict) -> str:
    kind = str(rule.get("kind") or "none")
    if kind == "daily":
        return "كل يوم" + (f" حتى {rule['until']}" if rule.get("until") else "")
    if kind == "weekly":
        days = [int(d) for d in (rule.get("days") or []) if str(d).lstrip("-").isdigit()]
        names = "، ".join(_DAY_AR[d] for d in sorted(set(days)) if 0 <= d <= 6) or "نفس اليوم أسبوعيًا"
        return "أسبوعيًا (" + names + ")"
    if kind == "monthly":
        return "شهريًا"
    return "بلا تكرار"


def _row(moment: _dt.datetime, event: dict) -> str:
    duration = int(event.get("duration_min") or 60)
    end = moment + _dt.timedelta(minutes=duration)
    bits = [f"  • {moment.strftime('%H:%M')}—{end.strftime('%H:%M')} · {event.get('title')}"]
    if event.get("location"):
        bits.append(f" ({event['location']})")
    if str((event.get("recurrence") or {}).get("kind")) not in ("", "none"):
        bits.append(f" [{_describe_recurrence(event.get('recurrence') or {})}]")
    return "".join(bits)


def _window(days: int) -> tuple[_dt.datetime, _dt.datetime]:
    now = C.now()
    start = _dt.datetime.combine(now.date(), _dt.time(0, 0))
    return start, start + _dt.timedelta(days=max(1, days))


def _act_today(params: dict, player) -> str:
    del params
    now = C.now()
    start = _dt.datetime.combine(now.date(), _dt.time(0, 0))
    rows = _events_between(start, start + _dt.timedelta(days=1))
    if not rows:
        return f"سيدي، لا مواعيد اليوم ({now.strftime('%Y-%m-%d')})."
    lines = [f"سيدي، مواعيد اليوم ({len(rows)}):"]
    lines.extend(_row(m, e) for m, e in rows)
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_upcoming(params: dict, player) -> str:
    days = _int(params.get("days"), 7, 1, 365)
    start, end = _window(days)
    rows = _events_between(start, end)
    if not rows:
        return f"سيدي، لا مواعيد في {days} يومًا القادمة."
    lines = [f"سيدي، القادم في {days} يومًا ({len(rows)}):"]
    for moment, event in rows:
        if moment >= C.now() - _dt.timedelta(minutes=1):
            lines.append(_row(moment, event) + f"  ⟶ {_friendly(moment)}")
    if len(lines) == 1:
        return f"سيدي، لا مواعيد قادمة في {days} يومًا."
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_list(params: dict, player) -> str:
    data = _load()
    items = list(data["items"].values())
    if not items:
        return "سيدي، التقويم فارغ. أضف موعدًا: action=add title=... when=..."
    lines = [f"سيدي، كل الأحداث ({len(items)}):"]
    for event in sorted(items, key=lambda e: str(e.get("start", "")))[:60]:
        rule = event.get("recurrence") or {}
        lines.append(f"  • {event.get('title')} — {event.get('start')} ({event.get('duration_min')} د)"
                     + (f" · {_describe_recurrence(rule)}" if str(rule.get("kind")) not in ("", "none") else "")
                     + f" · [{event.get('id')}]")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_show(params: dict, player) -> str:
    event, key, why = _find(str(params.get("id") or params.get("query") or ""))
    if event is None:
        return f"سيدي، {why}"
    reminders = event.get("reminders") or []
    lines = [
        f"سيدي، الحدث '{event.get('title')}' ({key}):",
        f"  البداية: {event.get('start')} · المدة: {event.get('duration_min')} دقيقة "
        f"· النهاية: {_fmt(C.from_iso(event.get('start')) + _dt.timedelta(minutes=int(event.get('duration_min') or 60)))}",
        f"  التكرار: {_describe_recurrence(event.get('recurrence') or {})}",
        f"  المكان: {event.get('location') or '—'} · المصدر: {event.get('source')}",
        "  التذكيرات: " + ("، ".join(f"قبل {r.get('minutes_before')} د" +
                                    (" (انطلق)" if r.get("sent") else "") for r in reminders) or "لا شيء"),
    ]
    if event.get("notes"):
        lines.append(f"  ملاحظات: {C.clip(event['notes'], 300)}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_search(params: dict, player) -> str:
    query = str(params.get("query") or "").strip().lower()
    if not query:
        return "سيدي، ما الذي أبحث عنه؟"
    data = _load()
    rows = [e for e in data["items"].values()
            if query in str(e.get("title", "")).lower() or query in str(e.get("notes", "")).lower()
            or query in str(e.get("location", "")).lower()]
    if not rows:
        return f"سيدي، لا حدث يطابق '{C.clip(query, 40)}'."
    lines = [f"سيدي، نتائج البحث عن '{query}' ({len(rows)}):"]
    for event in sorted(rows, key=lambda e: str(e.get("start", ""))):
        lines.append(f"  • {event.get('title')} — {event.get('start')} [{event.get('id')}]"
                     + (f" · {event.get('location')}" if event.get("location") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_update(params: dict, player) -> str:
    event, key, why = _find(str(params.get("id") or params.get("name") or ""))
    if event is None:
        return f"سيدي، {why}"
    data = _load()
    live = data["items"].get(str(key))
    if live is None:
        return "سيدي، الحدث اختفى."
    changed = []
    if params.get("title"):
        live["title"] = str(params["title"]).strip()
        changed.append("العنوان")
    if params.get("when"):
        start, note = _parse_start(params.get("when"))
        if start is None:
            return f"سيدي، {note}"
        live["start"] = _fmt(start)
        changed.append("الوقت")
        if note:
            changed.append(f"({note})")
    if params.get("duration_min"):
        live["duration_min"] = _int(params.get("duration_min"), int(live.get("duration_min") or 60), 5, 1440)
        changed.append("المدة")
    if params.get("location") is not None and str(params.get("location")) != "":
        live["location"] = str(params["location"]).strip()
        changed.append("المكان")
    if params.get("notes") is not None and str(params.get("notes")) != "":
        live["notes"] = str(params["notes"]).strip()
        changed.append("الملاحظات")
    if params.get("recurrence") is not None and str(params.get("recurrence")) != "":
        rule, why2 = _parse_recurrence(params.get("recurrence"))
        if why2:
            return f"سيدي، {why2}"
        live["recurrence"] = rule
        changed.append("التكرار")
    if params.get("reminder_min"):
        minutes = [_int(x, 15, 0, 10080) for x in str(params["reminder_min"]).replace("؛", ",").split(",")
                   if x.strip()]
        live["reminders"] = [{"minutes_before": m, "sent": ""} for m in sorted(set(minutes), reverse=True) if m > 0]
        changed.append("التذكيرات")
    if not changed:
        return "سيدي، ماذا أعدّل؟ title أو when أو duration_min أو location أو notes أو recurrence أو reminder_min."
    live["updated"] = C.now_iso()
    _save(data)
    C.log_event("calendar_events", "update", "ok", 0, "", {"id": key, "changed": changed})
    lines = [f"سيدي، عدّلت {' و'.join(changed)} في '{live.get('title')}'.",
             f"  الموعد الآن: {live.get('start')} — {_friendly(C.from_iso(live.get('start')) or C.now())}"]
    start_dt = C.from_iso(live.get("start"))
    if start_dt and C.parse_bool(_cfg().get("conflict_check"), True):
        clashes = _conflicts(start_dt, start_dt + _dt.timedelta(minutes=int(live.get("duration_min") or 60)),
                             ignore_id=str(key))
        if clashes:
            lines.append("  ⚠ تعارض مع: " + "، ".join(c["title"] for c in clashes))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _do_delete(key: str, series: bool = False) -> str:
    del series
    data = _load()
    event = data["items"].pop(str(key), None)
    if event is None:
        return "سيدي، الحدث لم يعد موجودًا."
    _save(data)
    C.log_event("calendar_events", "delete", "ok", 0, "", {"id": key, "title": event.get("title")})
    return f"سيدي، حذفت '{event.get('title')}' من تقويمي. لا شيء آخر تغيّر."


def _act_delete(params: dict, player) -> str:
    event, key, why = _find(str(params.get("id") or params.get("title") or ""))
    if event is None:
        return f"سيدي، {why}"
    series = C.parse_bool(params.get("series"), False) or str((event.get("recurrence") or {}).get("kind")) != "none"
    return C.confirm_request(
        key=f"calendar_delete_{key}",
        title=f"حذف '{event.get('title')}'؟",
        detail=(f"الموعد: {event.get('start')} · المدة: {event.get('duration_min')} دقيقة\n"
                + ("هذا حدث متكرّر — حذفه يلغي التكرار كله.\n" if series else "")
                + "الحذف من calendar.json فقط، ولا يمسّ أي شيء في نظامك."),
        run=lambda: _do_delete(str(key)),
        player=player,
    )


def _act_conflicts(params: dict, player) -> str:
    date_text = str(params.get("date") or "").strip()
    if date_text:
        start, why = _parse_start(date_text)
        if start is None:
            return f"سيدي، {why}"
        start = _dt.datetime.combine(start.date(), _dt.time(0, 0))
    else:
        start = _dt.datetime.combine(C.now().date(), _dt.time(0, 0))
    end = start + _dt.timedelta(days=1)
    rows = _events_between(start, end)
    clashes: list[str] = []
    for index, (moment, event) in enumerate(rows):
        event_end = moment + _dt.timedelta(minutes=int(event.get("duration_min") or 60))
        for other_moment, other in rows[index + 1:]:
            other_end = other_moment + _dt.timedelta(minutes=int(other.get("duration_min") or 60))
            if other_moment < event_end and moment < other_end:
                clashes.append(f"  ⚠ '{event.get('title')}' ({moment.strftime('%H:%M')}) يتعارض مع "
                               f"'{other.get('title')}' ({other_moment.strftime('%H:%M')})")
    if not clashes:
        return f"سيدي، لا تعارض في {start.date()}"
    out = "\n".join([f"سيدي، تعارضات {start.date()}:"] + sorted(set(clashes)))
    _log(player, out)
    return out


def _act_reminders(params: dict, player) -> str:
    fire = C.parse_bool(params.get("fire"), False)
    now = C.now()
    horizon = _int(params.get("days"), 2, 1, 30)
    start, end = now, now + _dt.timedelta(days=horizon)
    data = _load()
    due: list[tuple[_dt.datetime, dict, dict]] = []
    for event in data["items"].values():
        for moment in _occurrences(event, start, end):
            for reminder in event.get("reminders") or []:
                if reminder.get("sent"):
                    continue
                when = moment - _dt.timedelta(minutes=int(reminder.get("minutes_before") or 0))
                if when <= now:
                    due.append((when, event, reminder))
    due.sort(key=lambda row: row[0])
    if not due:
        return "سيدي، لا تذكيرات مستحقّة الآن."
    lines = [f"سيدي، تذكيرات مستحقّة ({len(due)}):"]
    sent = 0
    notify_plugin = str(_cfg().get("notify_plugin") or "notification_center").strip()
    for when, event, reminder in due[:20]:
        start_dt = C.from_iso(event.get("start"))
        lines.append(f"  ⏰ {event.get('title')} — {_friendly(start_dt) if start_dt else event.get('start')}"
                     f" (استحق {when.strftime('%H:%M')})")
        if not fire:
            continue
        text = f"تذكير: {event.get('title')} — {_friendly(start_dt) if start_dt else event.get('start')}"
        delivered = False
        if C.resolve_plugin(notify_plugin) is not None:
            ok, _ = C.call_plugin(notify_plugin, {"action": "notify", "title": "تذكير من تقويمك",
                                                  "body": text, "category": "calendar", "priority": "info"})
            delivered = bool(ok)
        if C.resolve_plugin("automation_engine") is not None:
            C.call_plugin("automation_engine", {"action": "fire", "source_plugin": "calendar_events",
                                                "source_status": "ok", "output": text})
        data = _load()
        live = data["items"].get(str(event.get("id")))
        if live is not None:
            for row in live.get("reminders") or []:
                if int(row.get("minutes_before") or 0) == int(reminder.get("minutes_before") or 0):
                    row["sent"] = C.now_iso()
            _save(data)
        sent += int(delivered or C.resolve_plugin(notify_plugin) is None)
    if fire:
        lines.append(f"  أُرسل {sent} تذكيرًا"
                     + ("" if C.resolve_plugin(notify_plugin) else f" (تنبيه: '{notify_plugin}' غير موجود — سُجّلت فقط)"))
        C.log_event("calendar_events", "reminders", "ok", 0, "", {"due": len(due), "sent": sent})
    else:
        lines.append("  (فحص فقط — استخدم fire=yes لإرسالها)")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_schedule_reminder(params: dict, player) -> str:
    """ينشئ أتمتة حقيقية (مرة واحدة) عند موعد التذكير — تكامل مع محرّك الأتمتة."""
    event, key, why = _find(str(params.get("id") or params.get("title") or ""))
    if event is None:
        return f"سيدي، {why}"
    if C.resolve_plugin("automation_engine") is None:
        return "سيدي، محرّك الأتمتة غير موجود، فلا أستطيع جدولة تذكير حقيقي."
    minutes = _int(params.get("reminder_min"), _int(_cfg().get("default_reminder_min"), 15, 0, 10080), 1, 10080)
    start_dt = C.from_iso(event.get("start"))
    if start_dt is None:
        return "سيدي، توقيت الحدث غير صالح."
    at = start_dt - _dt.timedelta(minutes=minutes)
    if at <= C.now():
        return (f"سيدي، موعد التذكير مضى ({_fmt(at)}) — الحدث نفسه {_front(event)}. "
                f"أضف تذكيرًا لحدث قادم، أو عدّل الموعد.")
    ok, out = C.call_plugin("automation_engine", {
        "action": "create",
        "name": f"تذكير: {event.get('title')}",
        "trigger": json.dumps({"kind": "once", "at": _fmt(at)}),
        "steps": json.dumps([{"name": "تنبيه", "plugin": str(_cfg().get("notify_plugin") or "notification_center"),
                              "parameters": {"action": "notify", "title": "تذكير قادم",
                                             "body": f"{event.get('title')} — {_front(event)}",
                                             "category": "calendar", "priority": "info"}}], ensure_ascii=False),
    })
    text = (f"سيدي، جدولت تذكيرًا لـ'{event.get('title')}' قبل {minutes} دقيقة (عند {_fmt(at)}).\n"
            f"  أُنشئت أتمتة في محرّك الأتمتة، ويشغّلها المجدول عند حلول وقتها.\n  " + C.clip(out, 300)
            if ok else f"سيدي، تعذّرت الجدولة: {C.clip(out, 200)}")
    _log(player, text)
    return text


def _front(event: dict) -> str:
    start_dt = C.from_iso(event.get("start"))
    return _friendly(start_dt) if start_dt else str(event.get("start"))


def _act_providers(params: dict, player) -> str:
    del params
    out = ("سيدي، مصادر التقويم:\n"
           "  • local (يعمل الآن): كل شيء في config/orchestration/calendar.json — بلا إنترنت وبلا حساب.\n"
           "  • Google Calendar: يحتاج OAuth من حسابك (google-auth-oauthlib موجود في متطلبات المشروع، "
           "ونطاق التقويم يُضاف عند التهيئة) — لم يُهيّأ بعد، ولن أدّعي أنه يعمل.\n"
           "  • Microsoft (Outlook): نفس المبدأ، يحتاج تسجيل تطبيق وتفويضًا منك.\n"
           "  • التصدير المحلي: action=export_ics يكتب ملف .ics يمكن استيراده في أي تقويم آخر.\n"
           "  المفاتيح/التوكنات لا تُكتب في أي سجل، وتُخزَّن في مخزن إعدادات المشروع إن أضفت مزوّدًا.")
    _log(player, out)
    return out


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^\w\u0600-\u06FF .-]", "", str(text or ""))[:60]
    cleaned = re.sub(r"\.{2,}", "_", cleaned)          # لا ".." في الاسم أبدًا
    cleaned = cleaned.replace(".", "_").replace(" ", "_").strip("_")
    return cleaned or "jarvis_calendar"


def _act_export_ics(params: dict, player) -> str:
    days = _int(params.get("days"), 30, 1, 3650)
    start, end = _window(days)
    rows = _events_between(start, end)
    if not rows:
        return f"سيدي، لا أحداث في {days} يومًا لأصدّرها."
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JARVIS//calendar_events//AR", "CALSCALE:GREGORIAN"]
    for moment, event in rows:
        end_dt = moment + _dt.timedelta(minutes=int(event.get("duration_min") or 60))
        lines += ["BEGIN:VEVENT", f"UID:{event.get('id')}@jarvis.local",
                  f"DTSTAMP:{C.now().strftime('%Y%m%dT%H%M%S')}",
                  f"DTSTART:{moment.strftime('%Y%m%dT%H%M%S')}",
                  f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}",
                  f"SUMMARY:{str(event.get('title') or '').replace(chr(10), ' ')}"]
        if event.get("location"):
            lines.append(f"LOCATION:{str(event['location']).replace(chr(10), ' ')}")
        if event.get("notes"):
            lines.append(f"DESCRIPTION:{str(event['notes']).replace(chr(10), ' ')}")
        rule = event.get("recurrence") or {}
        if str(rule.get("kind")) == "daily":
            lines.append("RRULE:FREQ=DAILY")
        elif str(rule.get("kind")) == "weekly":
            days_map = {0: "MO", 1: "TU", 2: "WE", 3: "TH", 4: "FR", 5: "SA", 6: "SU"}
            byday = ",".join(days_map.get(int(d), "MO") for d in (rule.get("days") or [moment.weekday()]))
            lines.append(f"RRULE:FREQ=WEEKLY;BYDAY={byday}")
        elif str(rule.get("kind")) == "monthly":
            lines.append("RRULE:FREQ=MONTHLY")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folder = _export_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return f"سيدي، تعذّر إنشاء مجلد التصدير: {C.clip(exc, 120)}"
    target = folder / f"{_safe_name(params.get('title') or 'jarvis')}_{C.now().strftime('%Y%m%d_%H%M')}.ics"
    try:
        target.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    except Exception as exc:
        return f"سيدي، تعذّرت الكتابة: {C.clip(exc, 120)}"
    C.log_event("calendar_events", "export_ics", "ok", 0, "", {"file": target.name, "events": len(rows)})
    out = (f"سيدي، صدّرت {len(rows)} حدثًا إلى:\n  {target}\n"
           f"  يمكنك استيراده في أي تقويم آخر (Google/Outlook/هاتف) — والملف محلي عندك.")
    _log(player, out)
    return out


_HELP = (
    "التقويم، سيدي:\n"
    "• add title=اجتماع when='غدًا 5 مساءً' [duration_min=45] [location=...] [notes=...]\n"
    "   recurrence='كل يوم' أو 'أسبوعيًا الأحد' أو JSON · reminder_min='15,5'\n"
    "• today · upcoming days=7 · list · show id= · search query= · conflicts [date=YYYY-MM-DD]\n"
    "• update id= when=... / title=... · delete (بتأكيد)\n"
    "• reminders [fire=yes] — المستحقّ الآن · schedule_reminder id= reminder_min=15 (أتمتة حقيقية)\n"
    "• providers — المصادر بصراحة · export_ics days=30 — ملف .ics محلي\n"
    "كل شيء محلي في calendar.json · بلا حساب · وبلا شبكة."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا يعمل التقويم."
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "today").strip().lower()
        C.ensure_dirs()
        if action in ("add", "create", "أضف"):
            return _act_add(p, player)
        if action in ("today", "اليوم"):
            return _act_today(p, player)
        if action in ("upcoming", "next", "القادم"):
            return _act_upcoming(p, player)
        if action in ("list", "قائمة"):
            return _act_list(p, player)
        if action in ("show", "تفاصيل"):
            return _act_show(p, player)
        if action in ("search", "ابحث"):
            return _act_search(p, player)
        if action in ("update", "edit", "عدّل"):
            return _act_update(p, player)
        if action in ("delete", "remove", "احذف"):
            return _act_delete(p, player)
        if action in ("conflicts", "تعارض"):
            return _act_conflicts(p, player)
        if action in ("reminders", "تذكيرات"):
            return _act_reminders(p, player)
        if action in ("schedule_reminder", "ذكّرني"):
            return _act_schedule_reminder(p, player)
        if action in ("providers", "مصادر"):
            return _act_providers(p, player)
        if action in ("export_ics", "ics", "صدّر"):
            return _act_export_ics(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في التقويم. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، التقويم واجه مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python calendar_events.py ───────────────────────────────────────
def _selftest() -> int:
    """الفحص الذاتي — معزولًا تمامًا عن مخزن المشروع: لا إعدادات ولا سجلات ولا أتمتات حقيقية."""
    restorer = None
    try:
        if C is not None and hasattr(C, "isolate_for_tests"):
            restorer = C.isolate_for_tests()
    except Exception:
        restorer = None
    try:
        return _selftest_body()
    finally:
        if restorer is not None:
            try:
                restorer()
            except Exception:
                pass


def _selftest_body() -> int:
    print("calendar_events — self-test")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if C is None:
        print("  ✗ النواة المشتركة غير موجودة")
        return 1

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ce_test_"))
    real_files = (C.CALENDAR_FILE, C.AUTOMATIONS_FILE, C.LOG_DIR)
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    now = C.now()
    try:
        C.CALENDAR_FILE = tmp / "calendar.json"
        C.AUTOMATIONS_FILE = tmp / "automations.json"
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.save_json(C.CALENDAR_FILE, {"version": 1, "items": {}})
        C.save_json(C.AUTOMATIONS_FILE, {"version": 1, "items": {}})
        C.store_settings("calendar_events", {"default_duration_min": "60", "default_reminder_min": "15",
                                             "export_dir": str(tmp / "ics"), "conflict_check": "yes",
                                             "notify_plugin": "notification_center"})
        later = (now + _dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")

        # 1) إضافة بوقت صريح
        out = run({"action": "add", "title": "اجتماع الفريق", "when": later, "duration_min": "45",
                   "location": "المنصورة", "reminder_min": "30"})
        check("add بوقت ISO", "أضفت 'اجتماع الفريق'" in out and "45 دقيقة" in out, out[:260])
        check("الحدث محفوظ في الملف", len(_load()["items"]) == 1)
        # 2) الإضافة بجملة عربية (غدًا مساءً)
        out = run({"action": "add", "title": "زيارة الطبيب", "when": "غدًا 5 مساءً"})
        check("add بجملة عربية", "أضفت" in out and "17:00" in out, out[:260])
        # 3) وقت بلا تحديد → 9:00 مع إعلان صريح
        out = run({"action": "add", "title": "بلا وقت", "when": "غدًا"})
        check("تاريخ بلا وقت يعلن الافتراض", "9:00" in out and "لا وقت محدّد" in out, out[:260])
        # 4) "بعد ساعتين"
        out = run({"action": "add", "title": "مكالمة", "when": "بعد ساعتين"})
        check("بعد ساعتين يُفهم", "أضفت 'مكالمة'" in out, out[:220])
        # 5) التعارض عند الإضافة
        clash_time = (now + _dt.timedelta(hours=2, minutes=15)).strftime("%Y-%m-%d %H:%M")
        out = run({"action": "add", "title": "اجتماع متعارض", "when": clash_time, "duration_min": "30"})
        check("تحذير التعارض عند الإضافة", "تعارض" in out, out[:300])
        out = run({"action": "conflicts"})
        check("كشف التعارضات", "تعارض" in out and "اجتماع الفريق" in out, out[:300])
        # 6) العرض
        out = run({"action": "today"})
        check("today يعمل (فارغ أو ممتلئ)", "مواعيد اليوم" in out or "لا مواعيد اليوم" in out, out[:200])
        out = run({"action": "upcoming", "days": "3"})
        check("upcoming يعرض القادم", "القادم في 3 يومًا" in out and "زيارة الطبيب" in out, out[:300])
        out = run({"action": "list"})
        check("list يعرض الكل", "اجتماع الفريق" in out and "مكالمة" in out, out[:300])
        out = run({"action": "show", "id": "اجتماع الفريق"})
        check("show يعرض التفاصيل", "المدة: 45" in out and "التذكيرات" in out, out[:260])
        out = run({"action": "search", "query": "طبيب"})
        check("search يجد بالعنوان", "زيارة الطبيب" in out, out[:200])
        # 7) التكرار: أسبوعيًا يومان، ثم توسيع المواعيد
        out = run({"action": "add", "title": "صالة رياضية", "when": "غدًا 7 صباحًا",
                   "recurrence": "أسبوعيًا الأحد والثلاثاء"})
        check("إضافة تكرار أسبوعي", "أسبوعيًا" in out, out[:280])
        rows = _events_between(now, now + _dt.timedelta(days=21))
        weekly = [m for m, e in rows if e.get("title") == "صالة رياضية"]
        check("التوسيع يعطي 4-6 مواعيد في 3 أسابيع", 4 <= len(weekly) <= 6, str(len(weekly)))
        check("كلها في الأحد أو الثلاثاء", all(m.weekday() in (6, 1) for m in weekly),
              str(sorted({m.weekday() for m in weekly})))
        out = run({"action": "add", "title": "دواء", "when": "غدًا 8 صباحًا", "recurrence": "كل يوم"})
        daily = [m for m, e in _events_between(now, now + _dt.timedelta(days=7)) if e.get("title") == "دواء"]
        check("التوسيع اليومي يعطي 7 مواعيد", len(daily) == 7, str(len(daily)))
        # 8) التذكيرات
        soon = (now + _dt.timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M")
        run({"action": "add", "title": "تذكير قريب", "when": soon, "reminder_min": "30"})
        out = run({"action": "reminders"})
        check("التذكير المستحق يظهر", "تذكير قريب" in out and "فحص فقط" in out, out[:300])
        out = run({"action": "reminders", "fire": "yes"})
        check("الإرسال الفعلي يعمل", "أُرسل" in out, out[:300])
        out = run({"action": "reminders"})
        check("لا تكرار للتذكير بعد الإرسال", "لا تذكيرات مستحقّة" in out, out[:200])
        # 9) جدولة تذكير كأتمتة حقيقية
        target = (now + _dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
        run({"action": "add", "title": "موعد مع الأتمتة", "when": target})
        out = run({"action": "schedule_reminder", "id": "موعد مع الأتمتة", "reminder_min": "20"})
        check("schedule_reminder ينشئ أتمتة", "جدولت تذكيرًا" in out, out[:300])
        automations = C.load_json(C.AUTOMATIONS_FILE, {}).get("items", {})
        check("الأتمتة محفوظة بنوع once", any(str((a.get("trigger") or {}).get("kind")) == "once"
                                            for a in automations.values()), str(list(automations))[:120])
        out = run({"action": "schedule_reminder", "id": "اجتماع الفريق", "reminder_min": "15"})
        check("حدث ماضٍ → رسالة صريحة", "مضى" in out or "قادم" in out, out[:220])
        # 10) التعديل
        out = run({"action": "update", "id": "اجتماع الفريق", "title": "اجتماع الفريق (محدَّث)",
                   "duration_min": "90"})
        check("update يعدّل", "عدّلت" in out and "المدة" in out, out[:240])
        out = run({"action": "update", "id": "اجتماع الفريق (محدَّث)", "when": "غدًا 11 صباحًا"})
        check("update يغيّر الوقت", "11:00" in out, out[:240])
        # 11) الحذف ببوابة
        before = len(_load()["items"])
        out = run({"action": "delete", "id": "مكالمة"})
        check("delete يستأذن", len(_load()["items"]) == before, out[:220])
        event, key, why = _find("مكالمة")
        out = _do_delete(str(key))
        check("الحذف بعد الموافقة", len(_load()["items"]) == before - 1 and "حذفت" in out, out[:160])
        # 12) التصدير والمصادر والمسارات الآمنة
        out = run({"action": "export_ics", "days": "30"})
        check("export_ics يكتب ملفًا", ".ics" in out and (tmp / "ics").exists(), out[:260])
        files = list((tmp / "ics").glob("*.ics"))
        check("الملف فيه VEVENT", bool(files) and "BEGIN:VEVENT" in files[0].read_text(encoding="utf-8"),
              str(files[:1]))
        out = run({"action": "export_ics", "title": "../../../etc/passwd", "days": "30"})
        check("اسم الملف مُنقّى (لا اجتياز مسارات)", (tmp / "ics").exists()
              and all(".." not in f.name and "/" not in f.name for f in (tmp / "ics").glob("*.ics")), out[:200])
        out = run({"action": "providers"})
        check("providers يقول الحقيقة عن OAuth", "OAuth" in out and "لم يُهيّأ" in out, out[:300])
        # 13) الحمايات العامة
        out = run({"action": "add", "title": "بلا وقت"})
        check("بلا وقت → سؤال واضح", "متى الحدث" in out, out[:160])
        out = run({"action": "show", "id": "لا_يوجد_حدث"})
        check("حدث غير موجود → رسالة", "لا حدث" in out, out[:160])
        check("help يعمل", "التقويم" in run({"action": "help"}))
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
        raw = ""
        for f in C.LOG_DIR.glob("*.jsonl"):
            raw += f.read_text(encoding="utf-8")
        check("لا أسرار في السجل", "password" not in raw.lower() and "token" not in raw.lower())
    finally:
        C.CALENDAR_FILE, C.AUTOMATIONS_FILE, C.LOG_DIR = real_files
        C._SCAN_CACHE["stamp"], C._SCAN_CACHE["data"] = real_stamp, real_data
        if backup is not None:
            base.write_text(backup, encoding="utf-8")
        elif base.exists():
            base.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            C.scan_plugins(force=True)
        except Exception:
            pass

    print(f"RESULT: {'PASS' if problems == 0 else str(problems) + ' problem(s)'}")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
