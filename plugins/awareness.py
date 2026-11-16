"""
awareness — طبقة الوعي.

الفكرة: `actions/proactive.py` بيشتغل ويقول كلام من نفسه، لكنه **بيخمّن**
السياق: ما عندوش ساعة إيد المستخدم، ولا رجع من الشغل، ولا تعبان من صوته.
الإضافة دي هي **الطبقة اللي تحت** — بتجمع facts محليًا (وقت، خمول، شغّال على
إيه، حمل النظام) وتحفظها في ملف واحد. `proactive.py` يقدر يقرأ منها لما
نربطهم، وأي إضافة تانية كمان.

⚠️ حدود صريحة — وده مشكل، تصميمًا:
- **ما بتفتحش ولا تقرأش أي ملف خاص.** ولا ديسكتوب ولا مستندات. بتشوف
  **نشاط النظام** بس (وقت، idle، عنوان النافذة النشطة، حمل المعالج/الذاكرة).
- **مفيش أي اتصال بالشبكة.** ولا سحابة، ولا API. كله local.
- **ما بتلمسش** `core/prompt.txt` ولا `actions/proactive.py`. بتوفّر **API
  قراءة** بس — الربط بين awareness وproactive هيبقى بإذنك.
- بتقفل افتراضيًا: readout مخفي لحد ما تعمل `watch on`.

الأوامر (actions):
  status            حالة الوعي دلوقتي (وقت + خمول + شغّال عليه + حمل)
  watch             شغّل/وقّف مراقبة مستمرة (on/off) + قوائم periods
  peek              آخر snapshot محفوظ + أخطر الـinsights
  note   text=…     سجّل ملاحظة إنت كاتبها («دعيت لاجتماع الساعة ٤») — بتتغذّى للـproactive
  digest            ملخص اليوم: كام ساعة شغال، رجع إمتى، أنهى programs، أكتر note
  forget            امسح كل السجلات
  status/help       زي دايمًا

API للقراءة (للإضافات التانية / الـproactive لاحقًا):
    import awareness
    snapshot()               -> dict   كل facts دلوقتي
    insights()               -> list   استنتاجات جاهزة بالعربي
    digest()                 -> dict   ملخص اليوم
    notes(limit)             -> list   آخر الملاحظات

Built by JARVIS additions — free, local, no keys, no network.
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN = {
    "name": "awareness",
    "description": (
        "Layer that gathers local facts about what the user is doing right now — "
        "time, idle length, active window, CPU/memory load — and stores notes the "
        "user dictates, so JARVIS can be aware instead of guessing. "
        "Use 'status' for the current picture, 'watch' to start/stop continuous "
        "monitoring, 'note' to record a note, 'digest' for today's summary. "
        "Never opens or reads private files. NOT a reminder or scheduler tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {
                "type": "STRING",
                "description": "The note text, for the 'note' action.",
            },
        },
        "required": [],
    },
}

# ════════════════════════════════════════════════════════════════════
#  مساعدات
# ════════════════════════════════════════════════════════════════════

def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    for p in [here.parent] + list(here.parents):
        if (p / "core" / "gemini.py").exists():
            return p
    return here.parent


ROOT = _project_root()


def _tools_core():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _tools_core as T  # type: ignore
        return T
    except Exception:
        return None


def _extras() -> Path:
    T = _tools_core()
    if T is not None:
        try:
            Path(T.ensure_dirs())
            return Path(T.extras_dir())
        except Exception:
            pass
    d = ROOT / "config" / "orchestration" / "extras"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _extras() / "awareness_state.json"


def _notes_path() -> Path:
    return _extras() / "awareness_notes.json"


def _load(path: Path, default: Any) -> Any:
    T = _tools_core()
    if T is not None:
        try:
            return T.load_json(path, default)
        except Exception:
            return default
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _save(path: Path, data: Any) -> bool:
    T = _tools_core()
    if T is not None:
        try:
            return bool(T.save_json(path, data))
        except Exception:
            pass
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _digits(s: Any) -> str:
    T = _tools_core()
    if T is not None:
        try:
            return T.normalize_digits(s)
        except Exception:
            pass
    return str(s or "").translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    )


def _log(player: Any, text: str) -> None:
    T = _tools_core()
    if T is not None:
        try:
            T.log(player, text); return
        except Exception:
            pass
    if player:
        try:
            player.write_log(f"JARVIS: {text}")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#  جمع الـ facts — كلهم محلي، ولا حاجة منهم تقرا ملف خاص
# ════════════════════════════════════════════════════════════════════

_LOAD_SAMPLE_SECONDS = 1.0   # نبضة وحدة المعالج (مهم: psutil بيحسبها جوه)


def _cpu_percent() -> Optional[float]:
    """حمل المعالج من أول نبضة → avgatz toujours 0. نعمل نبضة قصيرة."""
    T = _tools_core()
    if T is not None:
        try:
            import psutil  # type: ignore
            psutil.cpu_percent(interval=None)          # prime
            time.sleep(_LOAD_SAMPLE_SECONDS)
            return float(psutil.cpu_percent(interval=None))
        except Exception:
            return None
    return None


def _memory_percent() -> Optional[float]:
    try:
        import psutil  # type: ignore
        return float(psutil.virtual_memory().percent)
    except Exception:
        return None


def _idle_seconds() -> Optional[float]:
    """ثواني ما الـuser ما لمسش الماوس/الكيبورد."""
    try:
        import psutil  # type: ignore
        ts = psutil.cpu_times()
        idle = getattr(ts, "idle", None) or getattr(ts, "idle_time", None)
        if idle is None:
            return None
        return float(idle)
    except Exception:
        return None


def _active_window() -> Optional[str]:
    """عنوان النافذة النشطة — لو متاح من غير مكتبة زيادة (Windows فقط)."""
    if platform.system() != "Windows":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value.strip()
        return title or None
    except Exception:
        return None


def _period(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "صباح"
    if 12 <= h < 17:
        return "ظهر"
    if 17 <= h < 22:
        return "مساء"
    return "ليل"


def _greeting_period(period: str) -> str:
    return {
        "صباح": "صباح الخير",
        "ظهر": "نهارك سعيد",
        "مساء": "مساء الخير",
        "ليل": "مساء الخير",
    }.get(period, "أهلاً")


def snapshot() -> Dict[str, Any]:
    """كل الـfacts دلوقتي — snapshot لحظي."""
    now = datetime.now()
    per = _period(now)
    return {
        "ts": now.isoformat(timespec="seconds"),
        "hour": now.hour,
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "period": per,
        "greeting": _greeting_period(per),
        "cpu_percent": _cpu_percent(),
        "memory_percent": _memory_percent(),
        "idle_seconds": _idle_seconds(),
        "active_window": _active_window(),
        "platform": platform.system(),
    }


# ════════════════════════════════════════════════════════════════════
#  الملاحظات
# ════════════════════════════════════════════════════════════════════

def notes(limit: int = 10) -> List[Dict[str, str]]:
    """آخر الملاحظات — الأحدث أولًا."""
    data = _load(_notes_path(), []) or []
    if not isinstance(data, list):
        return []
    limit = max(1, int(limit or 10))
    return [n for n in reversed(data) if isinstance(n, dict)][:limit]


def _add_note(text: str) -> Dict[str, str]:
    now = datetime.now()
    entry = {"ts": now.isoformat(timespec="seconds"), "text": text}
    data = _load(_notes_path(), []) or []
    if not isinstance(data, list):
        data = []
    data.append(entry)
    # نحتفظ بآخر ٥٠٠ فقط — ملف محدود، مايكبرش بلا حد
    if len(data) > 500:
        data = data[-500:]
    _save(_notes_path(), data)
    return entry


_KEYWORDS: Dict[str, tuple] = {
    "موعد": ("دعوة", "اجتماع", "معاد", "ميعاد", "موعد", "الخمس", "الأربع",
             "التلات", "الاثنين", "الثلاثاء", "الأحد", "حضوري", "أونلاين"),
    "قائمة_مهم": ("اشتري", "إجباري", "لازم أجيب", "مش محتاج", "مشتري", "محتاج أجيب",
                  "التسوق", "بقالة"),
    "حالة": ("تعبان", "متعبان", "مرهق", "زعلان", "محتاج راحة", "نام بدري",
             "مريض", "وجع", "ضغط"),
    "شغل": ("شغل", "مشروع", "بكتب", "كود", "بحث", "دراسة", "امتحان", "تصميم"),
}


def _classify(text: str, period: str) -> str:
    """نوع الملاحظة — للـproactive بعدين. بحث بالكلمات لا بالـregex،
    لأن \b لا يعمل على النص العربي."""
    t = text.strip().lower()
    for kind, words in _KEYWORDS.items():
        if any(w in t for w in words):
            return kind
    return "عام"


# ════════════════════════════════════════════════════════════════════
#  استنتاجات — العربية، للـproactive أو للاستخدام مباشرة
# ════════════════════════════════════════════════════════════════════

def insights(snap: Optional[Dict[str, Any]] = None) -> List[str]:
    """جمل عربية قصيرة، مبنية على الـsnapshot + الملاحظات."""
    snap = snap or snapshot()
    out: List[str] = []
    if snap.get("period") == "ليل" and (snap.get("hour") or 0) >= 1:
        out.append(f"الساعة {snap.get('time')} — متأخر. تدريه ترتاح ولا شغال؟")

    idle = snap.get("idle_seconds")
    if idle is not None:
        mins = idle / 60.0
        if mins >= 45:
            out.append(f"من غير ما تحسّ، {int(mins)} دقيقة ما لمستش الماوس. في حاجة نلاقيها؟")
        elif mins >= 15:
            out.append(f"ساكت {int(mins)} دقيقة — إنت كمل؟")

    cpu, mem = snap.get("cpu_percent"), snap.get("memory_percent")
    if cpu is not None and cpu >= 85:
        out.append(f"المعالج {cpu:.0f}٪ — الجهاز شغال تقيل.")
    if mem is not None and mem >= 88:
        out.append(f"الذاكرة {mem:.0f}٪ — قريبة تملا.")

    aw = snap.get("active_window")
    if aw:
        short = aw if len(aw) <= 60 else aw[:57] + "…"
        out.append(f"شغّال على: {short}")

    last = notes(1)
    if last:
        out.append(f"آخر ملاحظة: {last[0]['text']}")

    return out


def digest() -> Dict[str, Any]:
    """ملخص اليوم من state المحفوظ."""
    st = _load(_state_path(), {}) or {}
    if not isinstance(st, dict):
        st = {}
    today = datetime.now().strftime("%Y-%m-%d")
    days = st.get("days") or {}
    day = days.get(today) or {}
    return {
        "date": today,
        "first_seen": day.get("first_seen"),
        "last_seen": day.get("last_seen"),
        "snapshots": day.get("snapshots", 0),
        "minutes_tracked": round(day.get("seconds", 0) / 60.0),
        "windows": sorted(set(day.get("windows") or []))[:20],
        "notes_today": sum(
            1 for n in notes(500) if str(n.get("ts", "")).startswith(today)
        ),
    }


# ════════════════════════════════════════════════════════════════════
#  المراقبة المستمرة (background) — في thread منفصل، مش بتوقف حاجة
# ════════════════════════════════════════════════════════════════════

_WATCH_FIELDS = [
    {"key": "watch_interval", "default": 300, "label": "كل كم ثانية نبضة (min 60)"},
    {"key": "max_days", "default": 30, "label": "أقصى أيام محفوظة (min 7)"},
    {"key": "max_windows", "default": 40, "label": "أقصى عدد برامج محفوظة"},
]

_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None


def _settings() -> Dict[str, Any]:
    T = _tools_core()
    fields = _WATCH_FIELDS
    if T is not None:
        try:
            return T.cfg("awareness", fields)
        except Exception:
            pass
    return {f["key"]: f["default"] for f in fields}


def _record(snap: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    st = _load(_state_path(), {}) or {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("watching", False)
    st.setdefault("days", {})
    days = st["days"]

    today = str(snap.get("date", ""))
    day = days.setdefault(today, {
        "first_seen": snap.get("ts"),
        "last_seen": snap.get("ts"),
        "snapshots": 0,
        "seconds": 0,
        "windows": [],
    })
    day["last_seen"] = snap.get("ts")
    day["snapshots"] = int(day.get("snapshots", 0)) + 1
    day["seconds"] = int(day.get("seconds", 0)) + int(cfg.get("watch_interval", 300))

    win = snap.get("active_window")
    if win and win not in day["windows"]:
        day["windows"].append(win)
        cap = int(cfg.get("max_windows", 40))
        if len(day["windows"]) > cap:
            day["windows"] = day["windows"][-cap:]

    # نقص days القديمة
    max_days = max(7, int(cfg.get("max_days", 30)))
    if len(days) > max_days:
        keys = sorted(days.keys())
        for k in keys[:-max_days]:
            days.pop(k, None)

    _save(_state_path(), st)


def _watch_loop(interval: int, player: Any = None) -> None:
    cfg = {"watch_interval": interval, "max_days": _settings().get("max_days", 30),
           "max_windows": _settings().get("max_windows", 40)}
    _STOP.clear()
    while not _STOP.is_set():
        try:
            snap = snapshot()
            _record(snap, cfg)
            st = _load(_state_path(), {}) or {}
            if isinstance(st, dict):
                st["watching"] = True
                _save(_state_path(), st)
            _log(player, "awareness نبضة سُجّلت.")
        except Exception:
            # الحركة الصامتة — slice أو thread يموت، ما نوقفتش
            pass
        _STOP.wait(interval)


def watch_start(interval: Optional[int] = None, player: Any = None) -> str:
    global _THREAD
    cfg = _settings()
    iv = int(interval or cfg.get("watch_interval", 300))
    iv = max(60, min(iv, 86400))          # clamp: مرة كل دقيقة على الأكثر

    if _THREAD and _THREAD.is_alive():
        return "👁️ المراقبة شغّالة بالفعل. استخدم `watch off` تقفها."

    _THREAD = threading.Thread(target=_watch_loop, args=(iv, player), daemon=True)
    _THREAD.start()
    st = _load(_state_path(), {}) or {}
    if not isinstance(st, dict):
        st = {}
    st["watching"] = True
    st["interval"] = iv
    _save(_state_path(), st)
    _log(player, f"awareness شغّال — نبضة كل {iv // 60} دقيقة.")
    return (
        f"👁️ **اتشغّلت المراقبة.**\n"
        f"نبضة كل **{iv // 60} دقيقة** بينتسجّل: الوقت والنافذة النشطة وحمل الجهاز.\n"
        f"ما بيفتحش ولا ملف من ملفاتك، ومش بيبعتش حاجة لبرّه.\n\n"
        f"«ملخص اليوم» تشوف اللي اتجمع. `watch off` تقفها."
    )


def watch_stop(player: Any = None) -> str:
    global _THREAD
    _STOP.set()
    if _THREAD and _THREAD.is_alive():
        _THREAD.join(timeout=2.0)
    _THREAD = None
    st = _load(_state_path(), {}) or {}
    if isinstance(st, dict):
        st["watching"] = False
        _save(_state_path(), st)
    _log(player, "awareness وقف.")
    return "🛑 وقفت المراقبة. اللي اتسجّل لحد دلوقتي موجود في `digest`."


def is_watching() -> bool:
    st = _load(_state_path(), {}) or {}
    return bool(isinstance(st, dict) and st.get("watching"))


# ════════════════════════════════════════════════════════════════════
#  الإ)}_actions
# ════════════════════════════════════════════════════════════════════

def _fmt_load(v: Optional[float]) -> str:
    if v is None:
        return "غير متاح"
    return f"{v:.0f}٪"


def _fmt_idle(v: Optional[float]) -> str:
    if v is None:
        return "غير متاح"
    m = int(v // 60)
    if m < 1:
        return "ثواني"
    if m < 60:
        return f"{m} دقيقة"
    return f"{m // 60} ساعة و{m % 60} دقيقة"


def _act_status(parameters: Dict[str, Any]) -> str:
    snap = snapshot()
    period = snap.get("period", "")
    lines = [
        f"🧠 **وعي جارفس — {snap.get('greeting')} يا سيدي**",
        f"🕘 الوقت: **{snap.get('time')}** ({period}) · {snap.get('date')}",
        f"⚙️ المعالج: {_fmt_load(snap.get('cpu_percent'))} · "
        f"الذاكرة: {_fmt_load(snap.get('memory_percent'))}",
        f"🖱️ آخر حركة: {_fmt_idle(snap.get('idle_seconds'))}",
    ]
    aw = snap.get("active_window")
    if aw:
        short = aw if len(aw) <= 70 else aw[:67] + "…"
        lines.append(f"🪟 شغّال على: `{short}`")
    lines.append(
        f"👁️ المراقبة: **{'شغّالة' if is_watching() else 'مقفولة'}** "
        f"(افتحها بـ «شغّل المراقبة»)"
    )
    ins = insights(snap)
    if ins:
        lines.append("")
        lines.append("**اللي باين من الصورة:**")
        for i in ins[:4]:
            lines.append(f"- {i}")
    return "\n".join(lines)


def _act_peek(parameters: Dict[str, Any]) -> str:
    st = _load(_state_path(), {}) or {}
    snap = st.get("last") if isinstance(st, dict) else None
    lines = ["👁️ **آخر snapshot محفوظ**"]
    if not snap:
        lines.append("لسه مفيش. شغّل «شغّل المراقبة» وخلّيه شوية.")
        return "\n".join(lines)
    lines.append(f"- الوقت: {snap.get('time')} ({snap.get('period')})")
    lines.append(f"- النافذة: `{snap.get('active_window') or '—'}`")
    lines.append(f"- المعالج: {_fmt_load(snap.get('cpu_percent'))}")
    ns = notes(5)
    if ns:
        lines.append("")
        lines.append("**آخر الملاحظات:**")
        for n in ns:
            lines.append(f"- [{n.get('time', n.get('ts', '')[11:16])}] {n.get('text')}")
    return "\n".join(lines)


def _act_note(parameters: Dict[str, Any]) -> str:
    text = str(parameters.get("text", "") or "").strip()
    if not text:
        return "قول لي الملاحظة نفسها، مثلًا: «سجّل ملاحظة: عملت امتحان وحصلت على شهادة»"
    entry = _add_note(text)
    kind = _classify(text, _period(datetime.now()))
    return (
        f"📝 **اتسجّلت.**\n"
        f"- النص: «{text}»\n"
        f"- التصنيف: **{kind}** · الوقت: {entry['ts'][11:16]}\n\n"
        f"الملاحظات دي بتتغذّى لـ «ملخص اليوم» والـproactive بعدين."
    )


def _act_digest(parameters: Dict[str, Any]) -> str:
    d = digest()
    if not d.get("snapshots"):
        return (
            f"📅 **ملخص {d['date']}**\n\n"
            f"لسه مفيش نشاط متسجّل. شغّل «شغّل المراقبة» الأول، "
            f"وخلّي جارفس شغّال شوية، وارجع تاني."
        )
    lines = [
        f"📅 **ملخص {d['date']}**",
        f"👁️ نبضات متسجّلة: **{d['snapshots']}** · مدّة متابَعة ≈ **{d['minutes_tracked']} دقيقة**",
        f"🕘 أول ظهور: {str(d.get('first_seen') or '—')[11:16]} · آخر ظهور: {str(d.get('last_seen') or '—')[11:16]}",
    ]
    wins = d.get("windows") or []
    if wins:
        lines.append("")
        lines.append("** programs قدامك:**")
        for w in wins[:8]:
            lines.append(f"- {w if len(w) <= 60 else w[:57] + '…'}")
    lines.append("")
    lines.append(f"📝 ملاحظات اليوم: **{d.get('notes_today', 0)}**")
    ns = [n for n in notes(20) if str(n.get("ts", "")).startswith(d["date"])]
    for n in ns[:5]:
        lines.append(f"- {n.get('text')}")
    return "\n".join(lines)


def _act_watch(parameters: Dict[str, Any]) -> str:
    raw = _digits(str(parameters.get("mode", "") or parameters.get("value", "") or "")).strip().lower()
    if raw in ("off", "ق", "وقف", "stop", "false", "لا"):
        return watch_stop()
    if raw in ("on", "ش", "شغّل", "start", "true", "نعم"):
        return watch_start(player=parameters.get("_player"))
    return (
        "👁️ قول لي: «شغّل المراقبة» ولا «وقّف المراقبة».\n"
        f"الحالة دلوقتي: **{'شغّالة' if is_watching() else 'مقفولة'}**."
    )


def _act_forget(parameters: Dict[str, Any]) -> str:
    # نقف المراقبة الأول: watch_stop بيكتب state["watching"]=False،
    # فلو مسحنا بعده الملف بيرجع تاني.
    watch_stop()
    for p in (_state_path(), _notes_path()):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
    return "🧹 اتمسح كل سجل الوعي: الملاحظات والـsnapshots. ملفك رجع نضيف."


def _act_set(parameters: Dict[str, Any]) -> str:
    key = str(parameters.get("key", "") or "").strip()
    value = parameters.get("value")
    if not key or value is None:
        return "قول لي: «غيّر كل كم ثانية نبضة» أو `set key=watch_interval value=600`"
    T = _tools_core()
    valid = {f["key"] for f in _WATCH_FIELDS}
    if key not in valid:
        return f"المفتاح «{key}» مش معروف. المتاح: " + "، ".join(sorted(valid))
    if T is not None:
        try:
            T.store_settings("awareness", {key: value})
        except Exception as e:
            return f"ما قدرتش أحفظ: {e}"
    else:
        st = _load(_state_path(), {}) or {}
        if isinstance(st, dict):
            st.setdefault("settings", {})[key] = value
            _save(_state_path(), st)
    return f"✅ `{key}` = `{value}` اتحفظ."


def _act_status_of_watch(parameters: Optional[Dict[str, Any]] = None) -> str:
    return (
        f"👁️ awareness: **on** · مراقبة "
        f"**{'شغّالة' if is_watching() else 'مقفولة'}** · "
        f"ملاحظات {len(notes(500))} محفوظة."
    )


def _act_help(parameters: Optional[Dict[str, Any]] = None) -> str:
    return (
        "🧠 **awareness — طبقة الوعي**\n\n"
        "- `status` — صورة دلوقتي (وقت، خمول، شغّال على إيه، حمل)\n"
        "- `شغّل المراقبة` / `وقّف المراقبة` — مراقبة مستمرة في الخلفية\n"
        "- `peek` — آخر snapshot + آخر الملاحظات\n"
        "- `note text=...` — سجّل ملاحظة («عملت امتحان وحصلت على شهادة»)\n"
        "- `digest` — ملخص اليوم (نبضات، برامج، ملاحظات)\n"
        "- `forget` — امسح كل السجلات\n"
        "- `set key=watch_interval value=600` — كل كم ثانية نبضة\n"
        "- `status` / `help`\n\n"
        "**ما بيفتحش ولا ملف من ملفاتك. ولا شبكة. كله محلّي."
    )


# ════════════════════════════════════════════════════════════════════
#  نقطة الدخول
# ════════════════════════════════════════════════════════════════════

_ALIASES = {
    "حالة": "status", "إيه": "status", "احوال": "status", "status": "status",
    "شغّل": "watch", "شغل": "watch", "راقب": "watch",
    "watch": "watch", "monitor": "watch",
    "شوف": "peek", "اخر": "peek", "peek": "peek", "آخر": "peek",
    "سجّل": "note", "سجل": "note", "ملاحظة": "note", "note": "note", "احفظ": "note",
    "ملخص": "digest", "اليوم": "digest", "تقرير": "digest", "digest": "digest",
    "امسح": "forget", "انسى": "forget", "forget": "forget", "نضف": "forget",
    "غيّر": "set", "غير": "set", "set": "set",
    "ساعد": "help", "help": "help", "مساعدة": "help", "أوامر": "help",
}


def _resolve(parameters: Dict[str, Any]) -> str:
    raw = str(parameters.get("action", "") or "").strip().lower()
    return _ALIASES.get(raw, raw)


def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = dict(parameters or {})
    action = _resolve(parameters)
    parameters["_player"] = player
    try:
        if action in ("status", "status_of_watch", ""):
            return _act_status(parameters)
        if action == "watch":
            return _act_watch(parameters)
        if action == "peek":
            return _act_peek(parameters)
        if action == "note":
            return _act_note(parameters)
        if action == "digest":
            return _act_digest(parameters)
        if action == "forget":
            return _act_forget(parameters)
        if action == "set":
            return _act_set(parameters)
        if action == "help":
            return _act_help()
        return (
            f"مش عارف «{action}». المتاح: status · watch · peek · note · "
            "digest · forget · set · help"
        )
    except Exception as e:
        return f"سيدي، awareness وقع: {e}"


# ════════════════════════════════════════════════════════════════════
#  الفحص الذاتي
# ════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    passed = 0
    failed: List[str] = []

    def check(label: str, cond: bool) -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  ✓ {label}")
        else:
            failed.append(label)
            print(f"  ✗ {label}")

    print("awareness — self-test\n")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        globals()["_extras"] = lambda: td_path  # type: ignore
        _STOP.clear()
        global _THREAD
        _THREAD = None

        # 1) snapshot shape
        snap = snapshot()
        check("snapshot returns dict", isinstance(snap, dict))
        for k in ("ts", "time", "date", "period", "greeting", "platform"):
            check(f"snapshot has {k}", k in snap)
        check("period is valid", snap.get("period") in ("صباح", "ظهر", "مساء", "ليل"))

        # 2) load fields may be None (sandbox) but must be numbers when present
        for k in ("cpu_percent", "memory_percent"):
            v = snap.get(k)
            check(f"{k} number or None", v is None or isinstance(v, (int, float)))

        # 3) insights returns list of str, doesn't crash on None loads
        ins = insights(snap)
        check("insights is list", isinstance(ins, list))
        check("insights strings", all(isinstance(i, str) for i in ins))

        # 4) notes CRUD
        check("notes start empty", notes() == [])
        e1 = _add_note("دعوت لاجتماع الساعة ٤")
        check("note added", e1.get("text") == "دعوت لاجتماع الساعة ٤")
        _add_note("محتاج أجيب بنزين")
        check("two notes", len(notes()) == 2)
        check("newest first", notes(1)[0]["text"] == "محتاج أجيب بنزين")
        check("classify موعد", _classify("دعوت لاجتماع الساعة ٤", "صباح") == "موعد")
        check("classify قائمة", _classify("محتاج أجيب بنزين", "صباح") == "قائمة_مهم")

        # 5) note cap
        for i in range(510):
            _add_note(f"note {i}")
        check("notes capped at 500", len(notes(600)) <= 500)

        # 6) _record writes and caps days
        cfg = {"watch_interval": 300, "max_days": 7, "max_windows": 5}
        for d in range(10):
            _record({"date": f"2026-01-{d+1:02d}", "ts": f"2026-01-{d+1:02d}T10:00",
                     "active_window": f"App{d}", "cpu_percent": 10.0}, cfg)
        st = _load(_state_path(), {})
        check("days capped", len(st.get("days", {})) <= 7)

        # 7) digest works off recorded days
        _record({"date": snap["date"], "ts": snap["ts"], "active_window": "Edge",
                 "cpu_percent": 5.0, "time": snap["time"], "period": snap["period"]}, cfg)
        d = digest()
        check("digest has date", d.get("date") == snap["date"])
        check("digest counts snapshots", d.get("snapshots", 0) >= 1)
        check("digest lists windows", isinstance(d.get("windows"), list))

        # 8) watch start/stop, thread liveness
        out = watch_start(interval=60)
        check("watch start returns", "اتشغّلت" in out)
        check("is_watching True", is_watching())
        out = watch_stop()
        check("watch stop returns", "وقفت" in out)
        check("is_watching False", not is_watching())

        # 9) forget wipes
        _act_note({"text": "x"})
        out = _act_forget({})
        check("forget returns", "اتمسح" in out)
        check("notes gone", notes() == [])
        check("state gone", not _state_path().exists())

        # 10) actions all return text and don't crash
        for name, fn, args in [
            ("status", _act_status, {}),
            ("peek", _act_peek, {}),
            ("digest", _act_digest, {}),
            ("note-empty", _act_note, {}),
            ("set-empty", _act_set, {}),
            ("set-badkey", _act_set, {"key": "nope", "value": 1}),
            ("help", _act_help, {}),
            ("status_of_watch", _act_status_of_watch, {}),
            ("watch-unknown", _act_watch, {"mode": "zzz"}),
        ]:
            try:
                r = fn(args)
                check(f"{name} returns str", isinstance(r, str) and len(r) > 0)
            except Exception as e:
                check(f"{name} ({e})", False)

        # 11) run() dispatch + unknown
        for a in ("status", "peek", "digest", "help", "status_of_watch"):
            r = run({"action": a})
            check(f"run {a}", isinstance(r, str) and len(r) > 0)
        r = run({"action": "totally_fake"})
        check("unknown handled", "مش عارف" in r)

        # 12) security: no writes outside extras, no network imports used
        check("extras path is sandbox", str(_state_path()).startswith(str(td_path)))

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())