"""
weekly_report.py — «التقرير الأسبوعي الشامل»: جارفس يتصرّف كمدير، لا منفّذ أوامر فقط.

يجمع في تقرير واحد كل ما تعرفه إضافاتك أصلًا — بلا أن يخترع شيئًا:
• حالة الجهاز (`system_health`) · مواعيدك (`calendar_events`) · عاداتك (`habit_tracker`)
• أهم ملفاتك الجديدة آخر أسبوع (يمشي على مستنداتك بنفسه)
• مصاريفك (`money_tracker`) · مساحة مهدورة (`duplicate_finder`) · متابعاتك الإخبارية (`news_monitor`)

ثم: **يحفظه** في ملف بتاريخه، **يُنبّهك** في جارفس و/أو **يرسله على تليجرام** (بإعدادك)،
ويمكن **جدولته** كل جمعة عبر `workflow_scheduler` الموجود في مشروعك بأمر واحد.

الصدق أولًا: أي إضافة غائبة تُذكر بالنص «غير متاحة» — لا أرقام مُختلقة ولا أقسام وهمية.
التقارير القديمة: أحدث ١٢ تبقى، وما زاد يُنقل إلى سلة ويندوز (قابل للاسترجاع) لا يُحذف نهائيًا.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "weekly_report",
    "description": (
        "Builds one comprehensive weekly report from the other plugins and your files: system health, "
        "upcoming calendar, habit streaks, the most important new files of the week, spending, wasted "
        "disk space from duplicates, and followed news — then saves it, notifies you, and can send it "
        "to Telegram or schedule itself for every Friday. Use for: 'give me my weekly report', "
        "'weekly summary', 'what happened this week', 'send me the weekly report on Telegram', "
        "'schedule the weekly report every Friday', 'preview the weekly report'. Do NOT use it for a "
        "quick question about one thing — use that plugin directly."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "now | preview | send | schedule | status | help"},
            "send": {"type": "STRING", "description": "now: also deliver it ('yes' = use the configured channel)"},
            "chat": {"type": "STRING", "description": "send: Telegram chat id (else from settings)"},
            "when": {"type": "STRING", "description": "schedule: time phrase, default 'كل جمعة 9 صباحًا'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "weekly_report",
    "title": "Weekly report — sections, delivery and schedule",
    "fields": [
        {"key": "channel", "type": "choice", "label": "How to deliver", "default": "notify",
         "choices": ["notify", "telegram", "both", "none"]},
        {"key": "telegram_chat", "type": "text", "label": "Telegram chat id", "default": ""},
        {"key": "recent_days", "type": "text", "label": "How many days back for new files", "default": "7"},
        {"key": "recent_limit", "type": "text", "label": "Maximum recent files to list", "default": "10"},
        {"key": "keep_reports", "type": "text", "label": "How many saved reports to keep", "default": "12"},
        {"key": "folders", "type": "text", "label": "Folders to watch for new files (semicolon separated)",
         "default": "Desktop;Documents;Downloads;Pictures"},
    ],
}

T = None
_REPORTS = "reports"
_SECTIONS = ("system_health", "calendar_events", "habit_tracker", "news_monitor")


def _boot() -> None:
    """يحمّل النواة المساعدة — مرة واحدة."""
    global T
    name = "_jarvis_tools_core"
    if name in sys.modules:
        T = sys.modules[name]
        return
    path = Path(__file__).resolve().parent / "_tools_core.py"
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        T = module
    except Exception:
        sys.modules.pop(name, None)
        T = None


_boot()


def _cfg() -> dict:
    if T is None:
        return {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    return T.cfg(PLUGIN_SETTINGS["namespace"], PLUGIN_SETTINGS["fields"])


def _limit(default: int, value: Any, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(str(value)))))
    except Exception:
        return default


# ── جمع الأقسام (بالصدق: الغائب يُقال) ───────────────────────────────────────
def _ask(plugin: str, attempts: list[dict], timeout: float = 25.0) -> tuple[str, str]:
    """ينادي إضافة بترتيب محاولات الأفعال. يعيد (النص أو ""، سبب الفشل)."""
    if T is None:
        return "", "النواة غير محمّلة."
    last = ""
    for parameters in attempts:
        ok, result = T.call_plugin(plugin, parameters, timeout_s=timeout)
        if ok and str(result or "").strip():
            return str(result), ""
        last = str(result or "")
    return "", last or "لا استجابة"


def _section_health() -> str:
    text, why = _ask("system_health", [{"action": "summary"}])
    return text if text else f"غير متاحة ({why[:80]})"


def _section_calendar() -> str:
    text, why = _ask("calendar_events", [{"action": "upcoming"}, {"action": "today"}])
    return text if text else f"غير متاحة ({why[:80]})"


def _section_habits() -> str:
    text, why = _ask("habit_tracker", [{"action": "status"}])
    return text if text else f"غير متاحة ({why[:80]})"


def _section_news() -> str:
    text, why = _ask("news_monitor", [{"action": "latest"}, {"action": "check"}])
    return text if text else f"غير متاحة ({why[:80]})"


def _section_money() -> str:
    text, why = _ask("money_tracker", [{"action": "report"}])
    return text if text else f"غير متاحة ({why[:80]})"


def _section_duplicates() -> str:
    text, why = _ask("duplicate_finder", [{"action": "status"}])
    return text if text else f"غير متاحة ({why[:80]})"


def _recent_files() -> str:
    """أهم الملفات الجديدة آخر أسبوع — يمشي بنفسه على مجلداتك (بلا أي إضافة أخرى)."""
    if T is None:
        return "غير متاحة (النواة غير محمّلة)."
    days = _limit(7, _cfg().get("recent_days"), 1, 90)
    cap = _limit(10, _cfg().get("recent_limit"), 1, 40)
    folders_raw = str(_cfg().get("folders") or "")
    names = [part.strip() for part in folders_raw.replace("\n", ";").split(";") if part.strip()]
    home = Path.home()
    cutoff = time.time() - days * 86400
    found: list[tuple[float, Path, int]] = []
    for name in names:
        folder = Path(name)
        if not folder.is_absolute():
            folder = home / name
        if not folder.is_dir() or not T.within(folder, [home]):
            continue
        try:
            for path in folder.rglob("*"):
                try:
                    if not path.is_file() or path.name.startswith("."):
                        continue
                    stat = path.stat()
                except Exception:
                    continue
                if stat.st_mtime >= cutoff:
                    found.append((stat.st_mtime, path, stat.st_size))
                if len(found) > 400:      # سقف حماية
                    break
        except Exception:
            continue
    if not found:
        return f"لا ملفات جديدة في آخر {days} أيام داخل مجلداتك المعتادة."
    found.sort(reverse=True)
    lines = [f"أهم {min(cap, len(found))} ملفًا جديدًا في آخر {days} أيام:"]
    for stamp, path, size in found[:cap]:
        when = time.strftime("%a %d %m · %H:%M", time.localtime(stamp))
        lines.append(f"  · {when} · {T.human_bytes(size):>9} · {path.name}")
    if len(found) > cap:
        lines.append(f"  … و{len(found) - cap} ملفًا آخر")
    return "\n".join(lines)


def build_report(include_files: bool = True) -> tuple[str, str]:
    """يبني نص التقرير كاملًا. يعيد (النص، سبب الفشل)."""
    if T is None:
        return "", "النواة _tools_core.py غير موجودة بجواري."
    end = datetime.now()
    start = end - timedelta(days=7)
    weekday_names = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    lines = [
        "═" * 46,
        f"  التقرير الأسبوعي — {start.strftime('%Y-%m-%d')} إلى {end.strftime('%Y-%m-%d')}",
        f"  ({weekday_names[end.weekday()]} {end.strftime('%d/%m/%Y · %H:%M')})",
        "═" * 46,
        "",
        "🖥️ حالة الجهاز",
        _indent(_section_health()),
        "",
        "🗓️ مواعيدك القادمة",
        _indent(_section_calendar()),
        "",
        "🔁 عاداتك",
        _indent(_section_habits()),
        "",
        "📁 أهم ملفاتك الجديدة",
        _indent(_recent_files() if include_files else "أُهمل بطلبك."),
        "",
        "💰 مصاريفك",
        _indent(_section_money()),
        "",
        "🧹 مساحة قابلة للاستعادة",
        _indent(_section_duplicates()),
        "",
        "📰 متابعاتك",
        _indent(_section_news()),
        "",
        "═" * 46,
        "  تقرير محلي بالكامل — جمعه جارفس من إضافاتك أنت، وما كان غائبًا قيل بصراحة.",
    ]
    return "\n".join(lines), ""


def _indent(text: str, prefix: str = "   ") -> str:
    body = str(text or "").strip() or "—"
    return "\n".join(prefix + line if line.strip() else "" for line in body.splitlines())


# ── الحفظ والتنظيف ───────────────────────────────────────────────────────────
def _reports_dir() -> Optional[Path]:
    if T is None:
        return None
    folder = T.extras_dir() / _REPORTS
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return folder


def _save_report(text: str) -> tuple[Optional[Path], str]:
    """يحفظ التقرير باسم تاريخه، ويُنقل الأقدم لسلة ويندوز لو زاد العدد. يعيد (الملف، ملاحظة)."""
    folder = _reports_dir()
    if folder is None:
        return None, "تعذّر تجهيز مجلد التقارير."
    target = folder / f"تقرير_أسبوعي_{datetime.now().strftime('%Y-%m-%d')}.txt"
    try:
        target.write_text(text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّرت الكتابة ({type(exc).__name__})."
    keep = _limit(12, _cfg().get("keep_reports"), 1, 60)
    note = ""
    try:
        saved = sorted(folder.glob("تقرير_أسبوعي_*.txt"), key=lambda p: p.name, reverse=True)
        extra = saved[keep:]
        if extra and T is not None:
            moved, where, errors = T.recycle_paths(extra)
            note = (f"نُقل {len(moved)} تقريرًا قديمًا إلى {where or 'سلة ويندوز'} "
                    f"(تبقى أحدث {keep})." if moved else "")
            if errors:
                note += " " + errors[0][:80]
    except Exception:
        note = ""
    return target, note


def _deliver(text: str, chat_override: str = "") -> str:
    """يُوصل التقرير حسب الإعداد: إشعار · تليجرام · الاثنان. يعيد وصف ما جرى (بالصدق)."""
    if T is None:
        return "النواة غير محمّلة."
    channel = str(_cfg().get("channel") or "notify").lower()
    chat = str(chat_override or _cfg().get("telegram_chat") or "").strip()
    parts: list[str] = []
    if channel in ("notify", "both"):
        ok, note = T.notify("التقرير الأسبوعي جاهز", _short(text))
        parts.append("إشعار داخل جارفس ✔" if ok else f"الإشعار: {note[:70]}")
    if channel in ("telegram", "both"):
        if not chat:
            parts.append("تليجرام: لا محادثة مضبوطة (telegram_chat) — لم أُرسل")
        else:
            ok, note = T.call_plugin("telegram_control",
                                     {"action": "send", "to": chat, "message": _short(text, 3000)},
                                     timeout_s=45)
            parts.append("تليجرام ✔" if ok else f"تليجرام: {note[:70]}")
    if not parts:
        parts.append("التوصيل مطفأ في الإعدادات (channel=none) — التقرير محفوظ فقط")
    return " · ".join(parts)


def _short(text: str, limit: int = 700) -> str:
    """مقتطف صالح للإشعارات ورسائل تليجرام (بحد أقصى آمن)."""
    clean = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()
    clean = clean.replace("═" * 46, "").strip()
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_now(params: dict, player: Any) -> str:
    del player
    text, why = build_report()
    if not text:
        return f"سيدي، {why}"
    target, note = _save_report(text)
    head = ["سيدي، التقرير الأسبوعي جاهز."]
    if target is not None:
        head.append(f"  محفوظ في: {target}")
    if note:
        head.append(f"  · {note}")
    if str(params.get("send") or "").strip().lower() in ("yes", "true", "1", "نعم"):
        head.append("  التوصيل: " + _deliver(text))
    head.append("")
    head.append(text)
    return "\n".join(head)


def _act_preview(params: dict, player: Any) -> str:
    del params, player
    text, why = build_report()
    if not text:
        return f"سيدي، {why}"
    return "سيدي، معاينة بلا حفظ ولا إرسال:\n\n" + text


def _act_send(params: dict, player: Any) -> str:
    del player
    text, why = build_report()
    if not text:
        return f"سيدي، {why}"
    target, _note = _save_report(text)
    result = _deliver(text, str(params.get("chat") or ""))
    return ("سيدي، التقرير الأسبوعي:\n"
            f"  أُرسل: {result}\n"
            + (f"  ونسخة محفوظة في: {target}" if target is not None else ""))


def _act_schedule(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، المكتبة `_tools_core.py` غير موجودة بجواري."
    when = str(params.get("when") or "كل جمعة 9 صباحًا").strip()
    ok, result = T.call_plugin(
        "workflow_scheduler",
        {"action": "create", "name": "التقرير الأسبوعي", "target_kind": "plugin",
         "target_name": "weekly_report", "parameters": '{"action":"now","send":"yes"}', "when": when},
        timeout_s=25)
    if ok:
        return (f"سيدي، جدولت التقرير الأسبوعي: «{when}».\n"
                f"  المجدول يقول: {str(result)[:300]}\n"
                "  (المجدول في مشروعك يحتاج تشغيله بـ action=start ليعمل تلقائيًا.)")
    return ("سيدي، إضافة workflow_scheduler غير متاحة الآن: " + str(result)[:160] + "\n"
            "  يمكنك بدلًا من ذلك قول هذا لجارفس في أي وقت: «أضف مهمة أسبوعية كل جمعة 9 صباحًا "
            "تنفّذ weekly_report» — أو شغّل action=now يدويًا كل جمعة.")


def _act_status(params: dict, player: Any) -> str:
    del params, player
    folder = _reports_dir()
    saved: list[Path] = []
    if folder is not None:
        try:
            saved = sorted(folder.glob("تقرير_أسبوعي_*.txt"), reverse=True)
        except Exception:
            saved = []
    lines = ["سيدي، حالة التقرير الأسبوعي:",
             f"  التوصيل: {_cfg().get('channel')} · محادثة تليجرام: "
             f"{'مضبوطة' if str(_cfg().get('telegram_chat') or '').strip() else 'غير مضبوطة'}",
             f"  التقارير المحفوظة: {len(saved)}",
             f"  مجلدات الملفات الجديدة: {_cfg().get('folders')}",
             "  الأقسام التي يجمعها: حالة الجهاز · المواعيد · العادات · الملفات · المصاريف · المساحة · الأخبار",
             "  أي إضافة غائبة تُكتب «غير متاحة» — لا أرقام مُختلقة."]
    if saved:
        lines.append(f"  آخر تقرير: {saved[0].name}")
    return "\n".join(lines)


def _act_help(params: dict, player: Any) -> str:
    del params, player
    return ("سيدي، التقرير الأسبوعي:\n"
            "  • now [send=yes]  — ابنِ التقرير واحفظه (وأوصله إن أمرت)\n"
            "  • preview         — اعرضه فقط بلا حفظ أو إرسال\n"
            "  • send [chat=]    — ابنِه وأرسله على تليجرام\n"
            "  • schedule [when=كل جمعة 9 صباحًا] — جدوله تلقائيًا\n"
            "  • status · help\n"
            "  يجمع: حالة الجهاز · مواعيدك · عاداتك · أهم ملفاتك · مصاريفك · مساحة المكرر · أخبارك.")


_ACTIONS = {"now": _act_now, "preview": _act_preview, "send": _act_send,
            "schedule": _act_schedule, "status": _act_status, "help": _act_help}


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    """ينفّذ فعلًا واحدًا. لا يرمي أبدًا."""
    del session_memory
    try:
        params = parameters or {}
        action = str(params.get("action") or "now").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return "سيدي، هذا ليس فعلًا من أفعال التقرير الأسبوعي — المتاح: " + " | ".join(sorted(_ACTIONS))
        return handler(params, player)
    except Exception as exc:  # noqa: BLE001
        if T is not None:
            T.log(player, f"weekly_report error: {exc}")
        return f"سيدي، حدث خطأ غير متوقع في التقرير الأسبوعي ({type(exc).__name__})."


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import shutil
    import tempfile

    print("weekly_report — فحص ذاتي")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if T is None:
        print("  ✗ النواة _tools_core.py غير محمّلة")
        print("RESULT: 1 problem(s)")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="weekly_test_"))
    original_extras = T.extras_dir
    original_scratch = T.scratch_dir
    original_call = T.call_plugin
    original_notify = T.notify
    original_home = Path.home
    T.extras_dir = lambda: tmp / "extras"            # type: ignore[assignment]
    T.scratch_dir = lambda: tmp / "trash"            # type: ignore[assignment]

    # عزل الإعدادات: الفحص لا يلمس مخزن إعداداتك الحقيقي أبدًا
    _orig_cfg, _orig_store, _orig_log = T.cfg, T.store_settings, T.log_event
    _store: dict[str, dict] = {}
    T.cfg = lambda ns, fields: {**T.defaults(fields), **_store.get(ns, {})}       # type: ignore[assignment]
    T.store_settings = lambda ns, values: (_store.setdefault(ns, {}).update(values), True)[1]  # type: ignore[assignment]
    T.log_event = lambda *a, **k: None                                            # type: ignore[assignment]
    calls: list[tuple[str, dict]] = []
    replies = {
        "system_health": "المعالج 12% · الذاكرة 60% · القرص ممتلئ 71%",
        "calendar_events": "غدًا 10:00 مراجعة المشروع",
        "habit_tracker": "القراءة: 5 أيام متتالية · الرياضة: يومان",
        "news_monitor": "· تطورات جديدة في أخبار التقنية",
        "money_tracker": "الإجمالي: 500 ج.م من 4 عمليات",
        "duplicate_finder": "مجموعات 3 · مهدر 2.1 ج.ب",
        "telegram_control": "أُرسلت الرسالة",
        "workflow_scheduler": "أُنشئ الجدول: التقرير الأسبوعي — الجمعة 9:00",
    }

    def fake_call(name, parameters=None, timeout_s=25.0):
        calls.append((name, dict(parameters or {})))
        if name in replies:
            return True, replies[name]
        return False, f"الإضافة {name} غير موجودة في مجلد plugins."
    T.call_plugin = fake_call                        # type: ignore[assignment]
    notifications: list[tuple[str, str]] = []
    T.notify = lambda title, body, urgent=False: (notifications.append((title, body)), (True, "أُرسل"))[1]  # type: ignore[assignment]
    try:
        # مجلد ملفات جديد للاختبار
        fake_home = tmp / "home"
        (fake_home / "Desktop").mkdir(parents=True)
        fresh = fake_home / "Desktop" / "عقد_جديد.pdf"
        fresh.write_bytes(b"x" * 2048)
        old = fake_home / "Desktop" / "قديم.txt"
        old.write_text("قديم", encoding="utf-8")
        import os
        os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
        Path.home = classmethod(lambda cls: fake_home)  # type: ignore[assignment]
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"folders": "Desktop", "recent_days": "7"})

        # ١) بناء التقرير وأقسامه
        text, why = build_report()
        check("التقرير يُبنى", bool(text) and not why, why[:80])
        for section in ("حالة الجهاز", "مواعيدك القادمة", "عاداتك", "أهم ملفاتك الجديدة",
                        "مصاريفك", "مساحة قابلة للاستعادة", "متابعاتك"):
            check(f"القسم موجود: {section}", section in text)
        check("بيانات الأقسام تُضمَّن فعلًا", "المعالج 12%" in text and "مراجعة المشروع" in text)
        check("الملف الجديد يظهر", "عقد_جديد.pdf" in text, text[text.find("ملفاتك"):][:200])
        check("الملف القديم لا يظهر", "قديم.txt" not in text)
        check("التاريخ والمدة صحيحان", datetime.now().strftime("%Y-%m-%d") in text and "إلى" in text)

        # ٢) إضافة غائبة تُقال بصراحة
        T.call_plugin = lambda name, parameters=None, timeout_s=25.0: (
            (True, replies[name]) if name in ("money_tracker",) else (False, f"الإضافة {name} غير موجودة."))  # type: ignore[assignment]
        text2, _ = build_report(include_files=False)
        check("الغائب يُذكر «غير متاحة»", "غير متاحة" in text2, text2[:150])
        check("وغير المتاح لا يُختلق", "المعالج" not in text2)
        check("وموجوده يظل يظهر", "500 ج.م" in text2)
        T.call_plugin = fake_call                        # type: ignore[assignment]

        # ٣) now: يحفظ ويحفظ مرة واحدة
        result = _act_now({}, None)
        folder = T.extras_dir() / "reports"
        saved = list(folder.glob("تقرير_أسبوعي_*.txt"))
        check("now يحفظ ملفًا واحدًا", len(saved) == 1, str(saved))
        check("ويذكر المسار", "محفوظ في" in result)
        result2 = _act_now({}, None)
        check("التكرار لا يُنشئ ملفًا ثانيًا", len(list(folder.glob("تقرير_أسبوعي_*.txt"))) == 1)
        check("والتقرير داخل الرد نفسه", "حالة الجهاز" in result2)

        # ٤) التوصيل: إشعار / تليجرام / بدون
        notifications.clear()
        _act_now({"send": "yes"}, None)
        check("التوصيل الافتراضي إشعار", bool(notifications), str(notifications[:1]))
        notifications.clear()
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"channel": "telegram", "telegram_chat": "12345"})
        sent = _act_send({}, None)
        check("تليجرام يُرسل للمحادثة المضبوطة", "تليجرام ✔" in sent and "12345" in str(calls[-1]), sent[:140])
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"channel": "telegram", "telegram_chat": ""})
        no_chat = _act_send({}, None)
        check("بلا محادثة يقول بصراحة", "لا محادثة" in no_chat, no_chat[:140])
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"channel": "none"})
        quiet = _act_send({}, None)
        check("والمطفأ يذكر أنه مطفأ", "مطفأ" in quiet, quiet[:140])
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"channel": "notify"})

        # ٥) معاينة بلا حفظ
        before = len(list(folder.glob("*.txt")))
        preview = _act_preview({}, None)
        check("المعاينة لا تحفظ", len(list(folder.glob("*.txt"))) == before and "بلا حفظ" in preview)

        # ٦) الجدولة
        scheduled = _act_schedule({}, None)
        check("الجدولة تنشئ مهمة الجمعة", "جمعة" in scheduled and any(
            call[0] == "workflow_scheduler" and call[1].get("target_name") == "weekly_report" for call in calls),
            scheduled[:150])
        check("الجدولة تشرح تشغيل المجدول", "action=start" in scheduled)
        T.call_plugin = lambda name, parameters=None, timeout_s=25.0: (False, "الإضافة workflow_scheduler غير موجودة.")  # type: ignore[assignment]
        fallback = _act_schedule({}, None)
        check("بلا مجدول تعطي البديل اليدوي", "غير متاحة" in fallback and "يدويًا" in fallback, fallback[:150])
        T.call_plugin = fake_call                        # type: ignore[assignment]

        # ٧) الحد الأقصى للتقارير (نقل لا حذف)
        for day in ("2026-09-01", "2026-09-08", "2026-09-15"):
            (folder / f"تقرير_أسبوعي_{day}.txt").write_text("تقرير قديم", encoding="utf-8")
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"keep_reports": "2"})
        _act_now({}, None)
        remaining = list(folder.glob("*.txt"))
        check("الحد الأقصى يُحترم (٢ فقط)", len(remaining) == 2, str([p.name for p in remaining]))
        check("والقديم نُقل لا حُذف", len(list((tmp / "trash").rglob("*.txt"))) >= 2,
              str(list((tmp / "trash").rglob("*.txt"))))
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"keep_reports": "12"})

        # ٨) الواجهة والحالة
        check("الحالة تصف الأقسام", "الأقسام" in _act_status({}, None) and "غير مضبوطة" in _act_status({}, None))
        check("فعل مجهول يُرفض بلطف", "ليس فعلًا" in run({"action": "monthly"}))
        check("run بلا معاملات لا ينهار", isinstance(run({}), str))
        check("help يشرح الآن", "now" in _act_help({}, None))
        check("المقتطف قصير للإشعار", len(_short(text, 200)) <= 200)
    finally:
        T.extras_dir = original_extras               # type: ignore[assignment]
        T.scratch_dir = original_scratch             # type: ignore[assignment]
        T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
        T.call_plugin = original_call                # type: ignore[assignment]
        T.notify = original_notify                   # type: ignore[assignment]
        Path.home = original_home                    # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
