"""
daily_briefing.py — الموجز اليومي (Daily Briefing).

يجمّع أقسامًا من إضافاتك الحقيقية: الوقت، الطقس، التقويم، التذكيرات، العادات،
الإشعارات، الأخبار، الأسعار، النظام، الجداول القادمة — ثم يقولها كنصّ واحد يمكن
لجارفس أن ينطقه كما هو (الفعل `run` يعيد ما يُقال).

لا يُكرّر وظيفة أي إضافة: كل قسم ينادي إضافته: `calendar_events` للتقويم،
`news_monitor` للأخبار، `system_diagnostics` للنظام، `workflow_scheduler` للجداول…
وإن كانت إضافة قسمٍ ما غير مثبّتة، يُقال ذلك بصراحة ويُكمل الموجز بقية الأقسام.

وضعان: `brief` (سطر لكل قسم — للحوار الصوتي) و`detailed` (كل التفاصيل + حفظ ملف
نصّي في مجلد مخصّص). والجدولة عبر محرّك الأتمتة: فعل `schedule` ينشئ أتمتة يومية
تنادي هذا الموجز في وقتك الذي تختاره.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "daily_briefing",
    "description": (
        "Builds a spoken daily briefing from the plugins you already have: time and "
        "date, weather, today's calendar, reminders, habits, urgent notifications, "
        "news, watched prices, system health and what is scheduled next. Use for "
        "'give me my briefing', 'what's my morning look like', 'brief me in detail', "
        "'brief me every day at 8' (creates the automation for you), 'which sections "
        "are on'. Modes: brief (one line per section, made to be spoken) and detailed "
        "(everything, saved to a text file)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "brief | sections | set | schedule | help"},
            "mode": {"type": "STRING", "description": "brief (default, short and speakable) or detailed."},
            "sections": {"type": "STRING", "description": "Only these sections, comma separated (e.g. 'weather,calendar,news')."},
            "save": {"type": "STRING", "description": "'yes' to save the full text to a file even in brief mode."},
            "city": {"type": "STRING", "description": "City for the weather section (default from settings)."},
            "time": {"type": "STRING", "description": "For schedule: the time, e.g. 'every day at 8 am'."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "daily_briefing",
    "title": "Daily briefing / الموجز اليومي",
    "fields": [
        {"key": "sec_datetime", "type": "text", "label": "Section: date & time (yes/no)", "default": "yes"},
        {"key": "sec_weather", "type": "text", "label": "Section: weather (yes/no)", "default": "yes"},
        {"key": "sec_calendar", "type": "text", "label": "Section: today's calendar (yes/no)", "default": "yes"},
        {"key": "sec_reminders", "type": "text", "label": "Section: reminders (yes/no)", "default": "yes"},
        {"key": "sec_habits", "type": "text", "label": "Section: habits (yes/no)", "default": "yes"},
        {"key": "sec_notifications", "type": "text", "label": "Section: important notifications (yes/no)", "default": "yes"},
        {"key": "sec_news", "type": "text", "label": "Section: news (yes/no)", "default": "yes"},
        {"key": "sec_prices", "type": "text", "label": "Section: watched prices (yes/no)", "default": "yes"},
        {"key": "sec_system", "type": "text", "label": "Section: system health (yes/no)", "default": "yes"},
        {"key": "sec_schedules", "type": "text", "label": "Section: scheduled tasks (yes/no)", "default": "yes"},
        {"key": "sec_files", "type": "text", "label": "Section: watched files (yes/no)", "default": "no"},
        {"key": "city", "type": "text", "label": "Weather city", "default": "Al Mansurah"},
        {"key": "weather_cache_min", "type": "text", "label": "Weather cache, minutes", "default": "30"},
        {"key": "net_timeout_s", "type": "text", "label": "Network timeout for a section, seconds", "default": "12"},
        {"key": "max_spoken", "type": "text", "label": "Most characters spoken in brief mode", "default": "1200"},
        {"key": "out_dir", "type": "text", "label": "Folder for saved briefings (blank = Documents/JARVIS_Briefings)", "default": ""},
    ],
}

_NS = "daily_briefing"
_SECTION_KEYS = ("sec_datetime", "sec_weather", "sec_calendar", "sec_reminders", "sec_habits",
                 "sec_notifications", "sec_news", "sec_prices", "sec_system", "sec_schedules", "sec_files")
_WEEKDAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
_MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو",
              "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


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
    return C.cfg(_NS, PLUGIN_SETTINGS["fields"])


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


def _call(name: str, params: dict, timeout: float = 30.0) -> str:
    """ينادي إضافة قسم بمهلة. يعيد نصًّا، أو '' إذا كانت الإضافة غائبة."""
    if C is None or C.resolve_plugin(name) is None:
        return ""
    ok, value, timed_out = C.run_with_timeout(lambda: C.call_plugin(name, params), timeout)
    if timed_out:
        return f"(تجاوزت الإضافة {name} المهلة)"
    if not ok:
        return f"(تعذّر قسم {name}: {C.clip(str(value), 90)})"
    return str(value)


def _out_dir() -> Path:
    custom = str(_cfg().get("out_dir") or "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / "Documents" / "JARVIS_Briefings"


# ── الأقسام ───────────────────────────────────────────────────────────────────
def _sec_datetime() -> tuple[str, str]:
    now_dt = C.now()
    hour = now_dt.hour
    part = ("صباحًا" if 4 <= hour < 12 else "ظهرًا" if 12 <= hour < 16
            else "مساءً" if 16 <= hour < 20 else "ليلًا")
    text = (f"{_WEEKDAYS_AR[now_dt.weekday()]} {now_dt.day} {_MONTHS_AR[now_dt.month - 1]} "
            f"{now_dt.year} — الساعة {now_dt.strftime('%H:%M')} {part}")
    return "الوقت", text


def _weather_cache() -> dict:
    return C.load_json(C.ORCH_DIR / "weather_cache.json", {})


def _sec_weather(city: str) -> tuple[str, str]:
    city = str(city or _cfg().get("city") or "Al Mansurah").strip()
    cache_min = _int(_cfg().get("weather_cache_min"), 30, 1, 720)
    cache = _weather_cache()
    cached_ts = C.from_iso(cache.get("ts"))
    if (cache.get("city") == city and cached_ts
            and (C.now() - cached_ts).total_seconds() < cache_min * 60 and cache.get("text")):
        return "الطقس", f"{city}: {cache['text']} (من ذاكرة آخر قراءة)"
    timeout = _int(_cfg().get("net_timeout_s"), 12, 3, 60)
    url = f"https://wttr.in/{city.replace(' ', '+')}?format=%C+%t+%h+%w&m&lang=ar"
    status, body = C.fetch_text(url, timeout=timeout, limit_kb=8)
    text = str(body or "").strip().replace("\n", " ")
    if status == 200 and text and len(text) < 200 and "Unknown" not in text:
        C.save_json(C.ORCH_DIR / "weather_cache.json",
                    {"ts": C.now_iso(), "city": city, "text": text})
        return "الطقس", f"{city}: {text}"
    if cache.get("city") == city and cache.get("text"):
        return "الطقس", f"{city}: {cache['text']} (آخر قراءة محفوظة — تعذّر التحديث الآن)"
    return "الطقس", f"(تعذّر جلب الطقس لـ{city} الآن — لا اتصال بوttr.in أو الخدمة مشغولة)"


def _sec_calendar() -> tuple[str, str]:
    out = _call("calendar_events", {"action": "today"}, 20)
    if not out:
        return "التقويم", ""
    if "لا يوجد شيء" in out:
        return "التقويم", "لا مواعيد اليوم"
    lines = [ln.strip() for ln in out.splitlines()[1:] if ln.strip().startswith("•")]
    return "التقويم", " · ".join(lines[:6]) if lines else C.clip(out, 200)


def _sec_reminders() -> tuple[str, str]:
    out = _call("calendar_events", {"action": "reminders", "days": "2"}, 20)
    if not out or "لا تذكيرات" in out:
        return "التذكيرات", ""
    lines = [ln.strip() for ln in out.splitlines()[1:] if ln.strip().startswith("•")]
    return "التذكيرات", " · ".join(lines[:5]) if lines else C.clip(out, 200)


def _sec_habits() -> tuple[str, str]:
    out = _call("habit_tracker", {"action": "status"}, 20)
    return ("العادات", C.clip(out, 200)) if out else ("العادات", "")


def _sec_notifications() -> tuple[str, str]:
    out = _call("notification_center", {"action": "urgent"}, 20)
    if not out or "لا " in out[:60] and "إشعار" in out[:60]:
        return "الإشعارات", ""
    return "الإشعارات", C.clip(out, 220)


def _sec_news() -> tuple[str, str]:
    out = _call("news_monitor", {"action": "latest"}, float(_int(_cfg().get("net_timeout_s"), 12, 3, 60)))
    if not out or "لم " in out[:40] or "لا " in out[:40]:
        return "الأخبار", ""
    return "الأخبار", C.clip(out, 260)


def _sec_prices() -> tuple[str, str]:
    out = _call("price_monitor", {"action": "list"}, 20)
    if not out or "لا " in out[:40]:
        return "الأسعار", ""
    return "الأسعار", C.clip(out, 220)


def _sec_system() -> tuple[str, str]:
    out = _call("system_diagnostics", {"action": "quick"}, 45)
    if not out:
        out = _call("system_health", {"action": "disk"}, 20)
    return ("النظام", C.clip(out, 240)) if out else ("النظام", "")


def _sec_schedules() -> tuple[str, str]:
    parts = []
    sched = _call("workflow_scheduler", {"action": "list"}, 20)
    if sched:
        count_line = sched.splitlines()[0] if sched.splitlines() else ""
        nxt = [ln.strip() for ln in sched.splitlines() if "القادم:" in ln][:2]
        parts.append(count_line + (" — " + " · ".join(nxt) if nxt else ""))
    autos = _call("automation_engine", {"action": "list"}, 20)
    if autos and "لا توجد" not in autos:
        parts.append(autos.splitlines()[0])
    return ("المجدول", C.clip(" · ".join(p for p in parts if p), 240)) if parts else ("المجدول", "")


def _sec_files() -> tuple[str, str]:
    out = _call("file_watcher", {"action": "list"}, 20)
    return ("الملفات", C.clip(out, 200)) if out else ("الملفات", "")


_SECTIONS = {
    "datetime": (_sec_datetime, "sec_datetime"),
    "weather": (_sec_weather, "sec_weather"),
    "calendar": (_sec_calendar, "sec_calendar"),
    "reminders": (_sec_reminders, "sec_reminders"),
    "habits": (_sec_habits, "sec_habits"),
    "notifications": (_sec_notifications, "sec_notifications"),
    "news": (_sec_news, "sec_news"),
    "prices": (_sec_prices, "sec_prices"),
    "system": (_sec_system, "sec_system"),
    "schedules": (_sec_schedules, "sec_schedules"),
    "files": (_sec_files, "sec_files"),
}


def _enabled_sections(params: dict) -> tuple[list[str], str]:
    cfg = _cfg()
    only = str(params.get("sections") or "").strip()
    if only:
        wanted = [s.strip().lower() for s in only.replace("؛", ",").split(",") if s.strip()]
        bad = [w for w in wanted if w not in _SECTIONS]
        good = [w for w in wanted if w in _SECTIONS]
        return (good, ("أقسام لا أعرفها: " + "، ".join(bad)) if bad else "")
    return [name for name in _SECTIONS if C.parse_bool(cfg.get(_SECTIONS[name][1]), True)], ""


# ── البناء ────────────────────────────────────────────────────────────────────
def _build(params: dict) -> tuple[str, dict]:
    """يبني الموجز. يعيد (النصّ الكامل, {اسم القسم: نصّه})."""
    mode = str(params.get("mode") or "brief").strip().lower()
    detailed = mode in ("detailed", "full", "مفصّل", "مفصل")
    sections, note = _enabled_sections(params)
    city = str(params.get("city") or "").strip()
    pieces: dict[str, list[str]] = {}
    missing: list[str] = []
    for name in sections:
        func, _key = _SECTIONS[name]
        try:
            title, text = func(city) if name == "weather" else func()  # type: ignore[call-arg]
        except TypeError:
            title, text = func()
        except Exception as exc:  # noqa: BLE001 — قسم فاشل لا يُسقط الموجز
            title, text = name, f"(تعذّر: {C.clip(exc, 80)})"
        if not text:
            missing.append(name)
            continue
        pieces.setdefault(title, []).append(text)
    now_dt = C.now()
    header = (f"موجز {_WEEKDAYS_AR[now_dt.weekday()]} {now_dt.day} {_MONTHS_AR[now_dt.month - 1]}"
              f" — {now_dt.strftime('%H:%M')}")
    lines = [header]
    for title, texts in pieces.items():
        body = " ".join(texts)
        if not detailed:
            body = C.clip(body, 170)
        lines.append(f"• {title}: {body}")
    if note:
        lines.append(f"({note})")
    if missing and detailed:
        lines.append("(أقسام بلا محتوى الآن: " + "، ".join(missing) + ")")
    full = "\n".join(lines)
    if not detailed:
        limit = _int(_cfg().get("max_spoken"), 1200, 200, 4000)
        if len(full) > limit:
            full = full[:limit].rstrip() + "…"
    return full, pieces


def _save_briefing(full: str) -> Path:
    folder = _out_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        return folder
    stamp = C.now().strftime("%Y-%m-%d_%H%M")
    path = folder / f"briefing_{stamp}.md"
    try:
        path.write_text("# موجز جارفس\n\n" + full + "\n", encoding="utf-8")
    except Exception:
        pass
    return path


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_brief(params: dict, player) -> str:
    full, _pieces = _build(params)
    want_save = C.parse_bool(params.get("save"), False) or \
        str(params.get("mode") or "").strip().lower() in ("detailed", "full", "مفصّل", "مفصل")
    tail = ""
    if want_save:
        path = _save_briefing(full)
        tail = f"\n(حفظت النسخة الكاملة في: {path})"
    C.log_event("daily_briefing", "brief", "ok", 0, "", {"chars": len(full)})
    spoken = full + tail
    _log(player, spoken)
    return spoken


def _act_sections(params: dict, player) -> str:
    del params
    cfg = _cfg()
    lines = ["سيدي، أقسام الموجز:"]
    for name, (_func, key) in _SECTIONS.items():
        on = C.parse_bool(cfg.get(key), True)
        lines.append(f"  {'✅' if on else '⛔'} {name}")
    lines.append("للتغيير: action=set مع مفتاح القسم (sec_news=no مثلًا).")
    text = "\n".join(lines)
    _log(player, text)
    return text


def _act_set(params: dict, player) -> str:
    updates = {}
    for name, (_func, key) in _SECTIONS.items():
        value = params.get(key) if params.get(key) is not None else params.get(name)
        if value is not None and str(value).strip() != "":
            updates[key] = "yes" if C.parse_bool(value, True) else "no"
    for key in ("city", "max_spoken", "out_dir"):
        if params.get(key) is not None and str(params.get(key)).strip() != "":
            updates[key] = str(params.get(key)).strip()
    if not updates:
        return "سيدي، لم أحدّد ما أغيّره. مثال: action=set وsec_news=no، أو city=القاهرة."
    ok = C.store_settings(_NS, updates)
    if not ok:
        return "سيدي، تعذّر حفظ الإعدادات."
    changed = "، ".join(f"{k}={v}" for k, v in updates.items())
    out = f"سيدي، حفظت: {changed}. يسري من الموجز القادم فورًا."
    _log(player, out)
    return out


def _act_schedule(params: dict, player) -> str:
    when = str(params.get("time") or params.get("when") or "").strip()
    if not when:
        return "سيدي، في أي وقت أعطيك الموجز؟ قل: 'كل يوم 8 صباحًا'."
    sched = C.parse_when(when)
    if not sched:
        return f"سيدي، لم أفهم التوقيت '{when}'. جرّب: 'كل يوم 8 صباحًا'."
    if C.resolve_plugin("automation_engine") is None:
        return "سيدي، محرّك الأتمتة غير موجود — بدونه لا أستطيع جدولة الموجز."
    steps = [{"name": "briefing", "plugin": "daily_briefing", "parameters": {"action": "brief", "mode": "brief"}}]
    if C.resolve_plugin("notification_center"):
        steps.append({"name": "notify", "plugin": "notification_center",
                      "parameters": {"action": "notify", "title": "موجز جارفس",
                                     "body": "{{steps.briefing.output}}", "category": "briefing",
                                     "priority": "info"}})
    ok, out = C.call_plugin("automation_engine", {
        "action": "create", "name": "الموجز اليومي",
        "trigger_json": json.dumps(sched, ensure_ascii=False),
        "steps_json": json.dumps(steps, ensure_ascii=False),
    })
    if not ok:
        return f"سيدي، تعذّرت الجدولة: {out}"
    return ("سيدي، جدولت الموجز: " + C.describe_schedule(sched)
            + ". ليعمل وحده يحتاج نبض المجدول (قل 'ابدأ المجدول').\n" + C.clip(out, 300))


_HELP = (
    "الموجز اليومي، سيدي:\n"
    "• brief — موجز قصير يُنطق (mode=detailed لكل التفاصيل + حفظ ملف)\n"
    "• sections — أي الأقسام مفعّلة · set — لي تغييرها (set + sec_news=no)\n"
    "• schedule — جدولة يومية: 'time': 'كل يوم 8 صباحًا'\n"
    "الأقسام: الوقت · الطقس · التقويم · التذكيرات · العادات · الإشعارات · الأخبار · "
    "الأسعار · النظام · المجدول · الملفات\n"
    "كل قسم من إضافته الحقيقية؛ وإن كانت إضافة غائبة يقولها الموجز بصراحة."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا يعمل الموجز."
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "brief").strip().lower()
        C.ensure_dirs()
        if action in ("brief", "briefing", "report", "موجز"):
            return _act_brief(p, player)
        if action in ("sections", "أقسام"):
            return _act_sections(p, player)
        if action in ("set", "configure", "غيّر"):
            return _act_set(p, player)
        if action in ("schedule", "جدولة"):
            return _act_schedule(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في الموجز. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، الموجز واجه مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python daily_briefing.py ────────────────────────────────────────
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
    print("daily_briefing — self-test")
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

    tmp = Path(tempfile.mkdtemp(prefix="db_test_"))
    real_logs, real_auto = C.LOG_DIR, C.AUTOMATIONS_FILE
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    real_fetch = getattr(C, "fetch_text", None)
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    try:
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.AUTOMATIONS_FILE = tmp / "automations.json"
        C.store_settings(_NS, {"out_dir": str(tmp / "briefings"), "city": "Al Mansurah"})

        # 1) قسم الوقت وحده: بلا شبكة، بلا إضافات أخرى
        out = run({"action": "brief", "sections": "datetime"})
        check("قسم الوقت يعمل", ("موجز" in out) and ("الساعة" in out) and str(C.now().year) in out, out[:200])
        check("قسم واحد فقط عند الطلب", "• الطقس" not in out and "• الأخبار" not in out, out[:200])
        check("نصّ قابل للنطق: بلا قوالب معلّقة", "{{" not in out and "}}" not in out)
        # 2) أقسام مجهولة تُذكر ولا تُسقط الموجز
        out = run({"action": "brief", "sections": "datetime, لا_يوجد"})
        check("قسم مجهول يُذكر بصراحة", ("لا أعرفها" in out) and ("الساعة" in out), out[:220])
        # 3) الطقس: مصدر مُستعار (بلا شبكة) ثم تخزين مؤقت
        C.fetch_text = lambda *a, **k: (200, "صافي +24°C 45% ↗ 12km/h")  # type: ignore[assignment]
        out = run({"action": "brief", "sections": "weather", "city": "المنصورة"})
        check("الطقس يُقرأ ويكتب", "+24" in out and "المنصورة" in out, out[:220])
        cached = C.load_json(C.ORCH_DIR / "weather_cache.json", {})
        check("ذاكرة الطقس حُفظت", cached.get("city") == "المنصورة", str(cached)[:120])
        out = run({"action": "brief", "sections": "weather", "city": "المنصورة"})
        check("القراءة الثانية من الذاكرة", "من ذاكرة" in out, out[:200])
        C.fetch_text = lambda *a, **k: (0, "")  # type: ignore[assignment]
        out = run({"action": "brief", "sections": "weather", "city": "طنطا"})
        check("فشل الشبكة يُعلن بلا كسر", "تعذّر جلب الطقس" in out, out[:220])
        # 4) الأقسام المركّبة من الإضافات الحقيقية (طقس معطّل، بلا شبكة)
        out = run({"action": "brief", "sections": "calendar,reminders,habits,notifications,system,schedules"})
        check("الالتقاط من الإضافات لا يرمي", isinstance(out, str) and len(out) > 20, out[:200])
        check("لا انهيار عند غياب محتوى", "موجز" in out, out[:120])
        # 5) الوضع المفصّل يحفظ ملفًا
        out = run({"action": "brief", "mode": "detailed", "sections": "datetime"})
        files = list((tmp / "briefings").glob("briefing_*.md"))
        check("الوضع المفصّل يحفظ ملفًا", len(files) == 1 and "حفظت النسخة الكاملة" in out, out[-200:])
        check("الملف فيه العنوان", "موجز جارفس" in files[0].read_text(encoding="utf-8") if files else False)
        # 6) الأقسام والإعدادات
        out = run({"action": "sections"})
        check("sections تعرض الحالة", "✅ news" in out and "⛔ files" in out, out[:200])
        out = run({"action": "set", "news": "no", "city": "القاهرة"})
        check("set يحفظ", "حفظت" in out and "sec_news=no" in out, out[:160])
        cfg_now = C.stored_settings(_NS)
        check("القيمة وصلت للمخزن", cfg_now.get("sec_news") == "no" and cfg_now.get("city") == "القاهرة", str(cfg_now)[:160])
        out = run({"action": "sections"})
        check("الحالة انعكست", "⛔ news" in out, out[:200])
        # 7) الجدولة تنشئ أتمتة فعلية
        out = run({"action": "schedule", "time": "every day at 8 am"})
        check("الجدولة تُنشئ أتمتة", "جدولت الموجز" in out, out[:240])
        autos = C.load_json(C.AUTOMATIONS_FILE, {"items": {}}).get("items", {})
        auto = list(autos.values())[0] if autos else {}
        check("الأتمتة فيها خطوتان: موجز + إشعار",
              [s.get("plugin") for s in auto.get("steps", [])][:2] == ["daily_briefing", "notification_center"],
              str(auto.get("steps", []))[:200])
        check("توقيتها يومي 08:00", (auto.get("trigger") or {}).get("time") == "08:00", str(auto.get("trigger")))
        check("بلا وقت → رسالة", "في أي وقت" in run({"action": "schedule"}))
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
        check("help يعمل", "الأقسام" in run({"action": "help"}))
    finally:
        C.LOG_DIR, C.AUTOMATIONS_FILE = real_logs, real_auto
        if real_fetch is not None:
            C.fetch_text = real_fetch  # type: ignore[assignment]
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
