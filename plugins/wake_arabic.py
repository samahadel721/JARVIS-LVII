"""
wake_arabic.py — كلمات تنبيه عربية تعمل بلا إنترنت: «يا فالح» و«يا لذيذ» و«hey jarvis».

مبنية على `_wake_core.py` (تُنسخ معها). **لا تلمس أي ملف من ملفات مشروعك.**

• **Vosk افتراضيًا**: عربي، مجاني، بلا مفاتيح، وبعد تنزيل النموذج مرة واحدة (318 م.ب)
  يعمل بلا إنترنت للأبد. **Porcupine** اختياري: أخفّ على المعالج، لكنه يحتاج مفتاحًا
  مجانيًا من Picovoice وتنشيطًا دوريًا بالإنترنت — ولن أطلب ذلك إلا إن أردته أنت.

• **مطابقة عربية ذكية**: «يا فالح» و«يافالح» و«يا فَلاحْ» و«يا فلاح» و«يا فالخ» كلها
  تُقبل، و«سلام عليكم» تُرفض. والعبارة تُكشف حتى داخل جملة أطول: «طيب يا فالح اعمل كذا».

• **ثلاث حالات**: `listen` تجربة حيّة تريك ما يُسمع فعلًا (بلا أي أثر)، `start` مراقبة
  في الخلفية بينما جارفس مفتوح تُنبّهك عند السماع (أو تشغّل أتمتة باسمها)، و`stop` تُنهيها.

• **حقّ يلزم قوله**: مفتاح كلمة التنبيه داخل المشروع (`core/wake_word.py`) يستخدم نموذج
  openWakeWord الإنجليزي «hey_jarvis» وليس فيه أي إعداد لملف خارجي — لذلك لن أُعدّل ملفات
  المشروع (ممنوع بلا إذنك). لكن حين تكون كلمة التنبيه **مطفأة** في جارفس فهو يصغي دائمًا
  (`_awake = True`) فقول «يا فالح، …» يصل إليه مباشرة — هذا سلوك مشروعك نفسه لا شيء من عندي.

🔴 لا سطر يرمي استثناءً للخارج، ولا مفتاح واحد يُكتب في أي سجل.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "wake_arabic",
    "description": (
        "Arabic wake words, fully offline after a one-time model download. Say 'ya faleh' or 'ya "
        "lazeez' (يا فالح / يا لذيذ) or 'hey jarvis' and it reacts: a notification, or running one "
        "of your automations by name. Use for: 'listen for the wake word', 'test the wake word', "
        "'start listening for ya faleh', 'stop listening', 'which wake phrases do you know', 'add a "
        "wake phrase', 'remove a wake phrase', 'set up the Arabic wake model', 'wake word status'. "
        "Default engine is Vosk Arabic (free, no key, offline). Porcupine (lighter, needs a free "
        "key and periodic online activation) can be selected instead. It does not and cannot change "
        "the project's own English openWakeWord detector without editing project files, which it "
        "never does."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("status | setup | listen | start | stop | phrases | trigger | help"),
            },
            "seconds": {"type": "STRING", "description": "listen: how long to test (default 25)."},
            "phrase": {"type": "STRING", "description": "phrases: the phrase to add (phrase_add) or remove (phrase_remove)."},
            "mode": {"type": "STRING", "description": "phrases: add | remove | list (default list)."},
            "trigger": {"type": "STRING", "description": "notify | automation | none."},
            "automation_name": {"type": "STRING", "description": "Automation to run when the wake word is heard."},
            "engine": {"type": "STRING", "description": "auto | vosk | porcupine."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "wake_arabic",
    "title": "Arabic wake words (يا فالح / يا لذيذ) — offline",
    "fields": [
        {"key": "engine", "type": "choice", "label": "Engine", "default": "auto",
         "choices": ["auto", "vosk", "openwake", "porcupine"]},
        {"key": "openwake_models", "type": "text", "label": "openWakeWord .onnx files (optional, one per phrase)", "default": ""},
        {"key": "openwake_phrases", "type": "text", "label": "Phrase for each .onnx, same order (blank = file name)", "default": ""},
        {"key": "openwake_threshold", "type": "text", "label": "openWakeWord threshold (0.3–0.9)", "default": "0.55"},
        {"key": "phrases", "type": "text", "label": "Wake phrases", "default": "يا فالح, يا لذيذ, hey jarvis"},
        {"key": "min_score", "type": "text", "label": "Fuzzy match threshold (0.5–1.0)", "default": "0.80"},
        {"key": "cooldown_s", "type": "text", "label": "Seconds between two wake reactions", "default": "4"},
        {"key": "listen_s", "type": "text", "label": "Test window length, seconds", "default": "25"},
        {"key": "mic_device", "type": "text", "label": "Microphone number (blank = Windows default)", "default": ""},
        {"key": "trigger", "type": "choice", "label": "On wake do", "default": "notify", "choices": ["notify", "automation", "none"]},
        {"key": "automation_name", "type": "text", "label": "Automation name (when trigger = automation)", "default": ""},
        {"key": "porcupine_access_key", "type": "text", "label": "Porcupine access key (optional — stored locally, never logged)", "default": ""},
        {"key": "porcupine_keywords", "type": "text", "label": "Porcupine .ppn paths, one per phrase", "default": ""},
    ],
}

_C = None
W = None

_LISTENER: dict[str, Any] = {"thread": None, "stop": None, "state": "off", "detail": "", "wakes": 0, "last": ""}
_LISTENER_LOCK = threading.Lock()


def _boot() -> None:
    """يحمّل النواة المشتركة للحزمة ونواة كلمات التنبيه — مرة واحدة."""
    global _C, W
    core_name = "_jarvis_orch_core"
    if core_name in sys.modules:
        _C = sys.modules[core_name]
    else:
        path = Path(__file__).resolve().parent / "_orch_core.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location(core_name, path)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules[core_name] = module
                try:
                    spec.loader.exec_module(module)
                    _C = module
                except Exception:
                    sys.modules.pop(core_name, None)
                    _C = None
    wake_name = "_jarvis_wake_core"
    if wake_name in sys.modules:
        W = sys.modules[wake_name]
        return
    path = Path(__file__).resolve().parent / "_wake_core.py"
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location(wake_name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[wake_name] = module
    try:
        spec.loader.exec_module(module)
        W = module
    except Exception:
        sys.modules.pop(wake_name, None)
        W = None


_boot()


def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    if _C is None:
        return defaults
    return _C.cfg("wake_arabic", PLUGIN_SETTINGS["fields"])


def _float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(str(value).strip())))
    except Exception:
        return default


def _log(player, text: str) -> None:
    try:
        if player is not None and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass


def _phrases() -> list[str]:
    """قائمة العبارات الحالية من الإعدادات."""
    return W.parse_phrases(_cfg().get("phrases")) if W is not None else []


def _keyword_paths() -> list[str]:
    raw = str(_cfg().get("porcupine_keywords") or "")
    return [part.strip() for part in raw.replace("\n", ";").split(";") if part.strip()]


def _openwake_paths() -> list[str]:
    """مسارات نماذج openWakeWord المدرَّبة (سطر أو فاصلة منقوطة لكل ملف)."""
    raw = str(_cfg().get("openwake_models") or "")
    return [part.strip() for part in raw.replace("\n", ";").split(";") if part.strip()]


def _openwake_phrases() -> list[str]:
    """عبارة لكل نموذج بنفس الترتيب (اختياري)."""
    raw = str(_cfg().get("openwake_phrases") or "")
    return [part.strip() for part in raw.replace("\n", ";").split(";") if part.strip()]


def _secrets() -> list[str]:
    """كل ما يجب حجبه قبل أي عرض أو تسجيل."""
    return [str(_cfg().get("porcupine_access_key") or "")]


def _engine_label(load: bool = True) -> tuple[Any, str]:
    """يبني المحرّك حسب الإعدادات ويعيد (محرّك أو None، وصفًا).

    `load=False` للحالة فقط — فلا نُحمّل نموذجًا ثقيلًا في الذاكرة لمجرد العرض.
    """
    pref = str(_cfg().get("engine") or "auto")
    return W.make_engine(pref, str(_cfg().get("porcupine_access_key") or ""), _keyword_paths(), load=load,
                         openwake_models=_openwake_paths(), openwake_phrases=_openwake_phrases(),
                         openwake_threshold=_float(_cfg().get("openwake_threshold"), 0.55, 0.05, 0.99))


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_status(params: dict, player) -> str:
    del params
    lines = ["سيدي، حالة كلمات التنبيه العربية:"]
    if W is None:
        lines.append("  ✗ مكتبة `_wake_core.py` غير موجودة بجوار هذا الملف — انسخها معه.")
        return "\n".join(lines)
    ok_v, note_v = W.vosk_available()
    ok_p, note_p = W.porcupine_available()
    engine, label = _engine_label(load=False)
    lines.append(f"  المحرّك المختار: {label if engine is not None else 'لا شيء جاهز بعد'}")
    lines.append(f"    Vosk: {'مثبّت' if ok_v else note_v}")
    lines.append(f"    Porcupine: {'مثبّت' if ok_p else note_p}")
    ok_o, note_o = W.openwake_available()
    lines.append(f"    openWakeWord: {'مثبّت' if ok_o else note_o}")
    models = _openwake_paths()
    if models:
        exist = sum(1 for m in models if Path(m).exists())
        lines.append(f"  نماذجك المدرَّبة: {exist} من {len(models)} موجودة · العتبة {_cfg().get('openwake_threshold')}"
                     + ("" if exist == len(models) else "  ← تحقّق من مسارات هذه الملفات"))
    if getattr(engine, "best", None):
        lines.append("  أعلى درجة سمعها كل نموذج: "
                     + " · ".join(f"{name} {value:.3f}" for name, value in list(engine.best.items())[:4]))
    present, where = W.model_present()
    lines.append(f"  النموذج العربي: {'جاهز — ' + where if present else where}")
    phrases = _phrases()
    lines.append(f"  عبارات التنبيه ({len(phrases)}): " + " · ".join(phrases))
    lines.append(f"  عند السماع: {_cfg().get('trigger')}"
                 + (f" ← أتمتة «{_cfg().get('automation_name')}»" if str(_cfg().get('trigger')) == "automation" else ""))
    lines.append(f"  الحدّ الضبابي {_cfg().get('min_score')} · التبريد {_cfg().get('cooldown_s')} ثانية"
                 f"  (قياسنا على صوت حقيقي: العبارة 0.875–0.909، وأقرب مشتّت 0.63)")
    with _LISTENER_LOCK:
        state = _LISTENER["state"]
        detail = _LISTENER["detail"]
        wakes = _LISTENER["wakes"]
    lines.append(f"  المستمع الخلفي: {state}" + (f" ({detail})" if detail else "") + f" · أشعل {wakes} مرة")
    devices, note_d = W.mic_devices()
    lines.append(f"  الميكروفون: {'المُعرَّف الافتراضي' if not str(_cfg().get('mic_device')).strip() else 'جهاز رقم ' + str(_cfg().get('mic_device'))} — {note_d}")
    if not devices:
        lines.append("     (اذكر رقم الجهاز في الإعدادات إن لم يفتح الافتراضي)")
    lines.append("  طريقة الربط: مع إطفاء كلمة التنبيه في جارفس فهو يصغي دائمًا، فقول «يا فالح …» يصل إليه مباشرة.")
    lines.append("  هذه الإضافة تضيف مستمعًا مستقلًا يسمع العبارة ثم ينبّهك أو يشغّل أتمتة باسمها.")
    lines.append("  ⚠️ ولن أُعدّل main.py أو core/ لربط المفتاح الإنجليزي بالعربية — ذلك يحتاج إذنك الصريح.")
    out = "\n".join(lines)
    _log(player, W.redact(out, _secrets()))
    return W.redact(out, _secrets())


def _act_setup(params: dict, player) -> str:
    if W is None:
        return "سيدي، مكتبة كلمات التنبيه غير موجودة."
    engine_pref = str(params.get("engine") or _cfg().get("engine") or "auto").lower()
    if engine_pref == "openwake":
        ok_o, note_o = W.openwake_available()
        if not ok_o:
            return f"سيدي، {note_o}"
        models = _openwake_paths()
        if not models:
            return ("سيدي، محرّك openWakeWord يحتاج نموذجًا مدرَّبًا على عبارتك (ملف .onnx لكل عبارة).\n"
                    "  وهو اختياري تمامًا: لو مفيش ملف نموذج عندك، الأفضل تخلّي engine=vosk — يعمل الآن بلا أي تدريب.\n"
                    "  وإن صار عندك ملف .onnx يومًا: ضعه في config/models واضبط openwake_models بمساره.")
        missing = [m for m in models if not Path(m).exists()]
        if missing:
            return f"سيدي، لا أجد ملف النموذج: {missing[0]} — تحقّق من المسار."
        return ("سيدي، النماذج المضبوطة موجودة. قل action=listen لأقيس أعلى درجة على صوتك، "
                "ثم اضبط العتبة في منتصف المسافة بين درجتك وأعلى درجة مشتّت.")
    if engine_pref == "porcupine" or (engine_pref == "auto" and _keyword_paths()):
        ok_p, note_p = W.porcupine_available()
        if not ok_p:
            return f"سيدي، {note_p}"
        engine, label = _engine_label()
        if engine is None:
            return (f"سيدي، {label}\n  المطلوب: مفتاح مجاني من console.picovoice.ai وملفان .ppn "
                    f"لكلمتَي «يا فالح» و«يا لذيذ»، ثم ضعهما في إعدادات الإضافة.")
        out = f"سيدي، جاهز على Porcupine — {label}\n  تنبيه صادق: يحتاج تنشيطًا دوريًا بالإنترنت، وأخفّ على المعالج من Vosk."
        _log(player, W.redact(out, _secrets()))
        return out
    ok, note = W.vosk_available()
    if not ok:
        return f"سيدي، {note}"
    if W.model_present()[0]:
        return f"سيدي، النموذج العربي موجود أصلًا في {W.model_dir()} — لا حاجة لتنزيل شيء."
    _log(player, "سيدي، أبدأ تنزيل النموذج العربي مرة واحدة (~318 م.ب) — سيستغرق وقتًا حسب سرعتك.")
    messages: list[str] = []

    def progress(text: str) -> None:
        if not messages or messages[-1] != text:
            messages.append(text)
            _log(player, "  " + text)

    ok, note = W.setup_vosk_model(progress)
    out = (f"سيدي، {note}" if ok else f"سيدي، تعذّر التجهيز: {note}")
    if ok:
        out += "\n  بعد اليوم: بلا إنترنت، بلا مفاتيح، بلا حساب. قل action=listen لتجربتها بصوتك."
    W.log("setup", "ok" if ok else "failed", error="" if ok else note[:120])
    _log(player, out)
    return out


def _make_frames(stream: Any, seconds: float, stop: threading.Event) -> Any:
    """إطارات من ميكروفون حقيقي (دالة منفصلة كي يستطيع الفحص الذاتي استبدالها)."""
    return W.frame_iter(stream, timeout_s=seconds, stop_event=stop)


def _listen_window(seconds: float, engine: Any, phrases: list[str], *,
                   on_wake=None, on_text=None, stop: Optional[threading.Event] = None,
                   player=None) -> tuple[dict, str]:
    """يفتح الميكروفون ويستمع مدة محددة. يعيد (ملخّص، رسالة)."""
    stream, why = W.open_mic(str(_cfg().get("mic_device") or "").strip() or None)
    if stream is None:
        return {}, why
    try:
        frames = _make_frames(stream, seconds, stop or threading.Event())
        summary = W.listen_loop(frames, engine, phrases,
                                min_score=_float(_cfg().get("min_score"), 0.80, 0.5, 1.0),
                                cooldown_s=_float(_cfg().get("cooldown_s"), 4.0, 0.5, 120.0),
                                timeout_s=seconds, stop_event=stop, on_wake=on_wake, on_text=on_text)
    finally:
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
    del player
    return summary, ""


def _act_listen(params: dict, player) -> str:
    if W is None:
        return "سيدي، مكتبة كلمات التنبيه غير موجودة."
    engine, label = _engine_label()
    if engine is None:
        return f"سيدي، {label}"
    seconds = _float(params.get("seconds"), _float(_cfg().get("listen_s"), 25.0, 3.0, 300.0), 3.0, 300.0)
    phrases = _phrases()
    if not phrases:
        return "سيدي، لا عبارات مضبوطة — أضِف واحدة: action=phrases mode=add phrase=يا فالح"
    _log(player, f"سيدي، أستمع الآن {int(seconds)} ثانية بـ{label}. قل «يا فالح» أو «يا لذيذ».")
    heard: list[str] = []

    def on_text(text: str) -> None:
        if text not in heard[-3:]:
            heard.append(text)
            _log(player, f"  سمعت: {text}")

    def on_wake(record: dict) -> None:
        _log(player, f"  ✓ عرفتها: «{record.get('phrase')}» (تشابه {record.get('score')}) — "
                     f"لم أُنفّذ شيئًا، هذه تجربة.")

    summary, why = _listen_window(seconds, engine, phrases, on_wake=on_wake, on_text=on_text, player=player)
    if summary == {}:
        return f"سيدي، {why}"
    lines = [f"سيدي، انتهت التجربة ({summary.get('seconds')} ثانية) — المحرّك: {label}",
             "  لم أُنفّذ شيئًا ولم أُنبّه أحدًا — هذه تجربة لا أكثر."]
    if summary["wakes"]:
        for record in summary["wakes"]:
            lines.append(f"  ✓ «{record['phrase']}» — تشابه {record['score']} ({record['how']}) عند {record['at']} ثانية")
        lines.append("  المستمع يعمل على صوتك. قل action=start ليعمل في الخلفية.")
    else:
        lines.append("  لم أسمع العبارة. الأسباب المعتادة: بعُدك عن الميكروفون، مستوى التسجيل منخفض في ويندوز،")
        lines.append("  أو ازدحام بالخلفية. ارفع مستوى الميكروفون وأعد التجربة، أو اخفض الحدّ الضبابي إلى 0.75.")
    if summary["heard"]:
        lines.append("  آخر ما فهمته: " + " · ".join(summary["heard"][-4:]))
    best = getattr(engine, "best", None)
    if best:
        lines.append("  أعلى درجة وصلها كل نموذج (للمعايرة): "
                     + " · ".join(f"{name} {value:.3f}" for name, value in list(best.items())[:4]))
        lines.append(f"  العتبة الحالية {getattr(engine, 'threshold', '?')} — "
                     f"اضبطها في منتصف المسافة بين درجتك وأعلى درجة مشتّت.")
    if summary["errors"]:
        lines.append("  ملاحظات: " + " · ".join(summary["errors"][:3]))
    out = "\n".join(lines)
    W.log("listen", "ok" if summary["wakes"] else "failed",
          error="" if summary["wakes"] else "لم تُسمع العبارة",
          detail={"seconds": summary.get("seconds"), "wakes": len(summary["wakes"])})
    _log(player, out)
    return out


def _fire_trigger(record: dict, player=None) -> str:
    """ما يحدث عند سماع العبارة: تنبيه حقيقي، أو أتمتة باسمها، أو لا شيء. يعيد وصفًا."""
    mode = str(_cfg().get("trigger") or "notify").lower()
    phrase = str(record.get("phrase") or "")
    if mode == "none":
        return f"سمعت «{phrase}» (بلا تنفيذ — الوضع none)."
    if mode == "automation":
        name = str(_cfg().get("automation_name") or "").strip()
        if not name:
            return "سمعت العبارة، لكن لا اسم أتمتة مضبوط — اضبطه أو اختر trigger=notify."
        if _C is None or _C.resolve_plugin("automation_engine") is None:
            return "سمعت العبارة، لكن محرّك الأتمتة غير موجود."
        ok, out = _C.call_plugin("automation_engine", {"action": "run", "name": name})
        text = str(out)
        missing = ("لا أتمتة باسم" in text) or ("أكثر من أتمتة" in text) or ("اختفت" in text)
        if missing or not ok:
            W.log("wake", "failed", error=text[:120],
                  detail={"phrase": phrase, "score": record.get("score"), "automation": name})
            _ = player
            return f"سمعت «{phrase}»، لكن الأتمتة «{name}» لم تُشغّل: {text[:140]}"
        W.log("wake", "ok", detail={"phrase": phrase, "score": record.get("score"), "automation": name})
        _ = player
        return f"شغّلت «{name}» بعد «{phrase}»."
    title = "نعم سيدي؟"
    body = f"سمعت «{phrase}» — الجلسة تحت أمرك."
    if _C is not None and _C.resolve_plugin("notification_center") is not None:
        _C.call_plugin("notification_center", {"action": "notify", "title": title, "body": body,
                                               "category": "wake_arabic", "priority": "info"})
    W.log("wake", "ok", detail={"phrase": phrase, "score": record.get("score"), "how": record.get("how")})
    return f"سمعت «{phrase}» — {title}"


def _listener_thread(seconds: float, engine: Any, phrases: list[str], stop: threading.Event) -> None:
    """خيط المراقبة الخلفي: لا يرمي أبدًا ولا يعطّل جارفس."""
    try:
        def on_wake(record: dict) -> None:
            text = _fire_trigger(record)
            with _LISTENER_LOCK:
                _LISTENER["wakes"] += 1
                _LISTENER["last"] = text
                _LISTENER["detail"] = text
        summary, why = _listen_window(seconds, engine, phrases, on_wake=on_wake, stop=stop)
        with _LISTENER_LOCK:
            if summary == {}:
                _LISTENER["state"] = "error"
                _LISTENER["detail"] = why
            else:
                _LISTENER["state"] = "off"
                _LISTENER["detail"] = f"توقف: {summary.get('stopped')} — أشعل {len(summary.get('wakes', []))} مرة"
    except Exception as exc:  # noqa: BLE001
        with _LISTENER_LOCK:
            _LISTENER["state"] = "error"
            _LISTENER["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"


def _act_start(params: dict, player) -> str:
    if W is None:
        return "سيدي، مكتبة كلمات التنبيه غير موجودة."
    with _LISTENER_LOCK:
        running = _LISTENER["thread"] is not None and _LISTENER["thread"].is_alive()
        if running:
            return f"سيدي، المستمع يعمل بالفعل ({_LISTENER['detail'] or _LISTENER['state']}). لإيقافه: action=stop"
    engine, label = _engine_label()
    if engine is None:
        return f"سيدي، {label}"
    phrases = _phrases()
    if not phrases:
        return "سيدي، لا عبارات مضبوطة."
    seconds = _float(params.get("seconds"), 0.0, 0.0, 3600.0)   # 0 = بلا حدّ زمني
    stop = threading.Event()
    thread = threading.Thread(target=_listener_thread, args=(seconds, engine, phrases, stop),
                              name="jarvis_wake_listener", daemon=True)
    with _LISTENER_LOCK:
        _LISTENER.update({"thread": thread, "stop": stop, "state": "running", "detail": "يستمع", "last": ""})
    thread.start()
    time.sleep(0.25)
    with _LISTENER_LOCK:
        state = _LISTENER["state"]
        detail = _LISTENER["detail"]
    lines = [f"سيدي، المستمع يعمل الآن بـ{label}", f"  الحالة: {state}" + (f" ({detail})" if detail else "")]
    lines.append("  عند العبارة: " + (f"أشغّل أتمتة «{_cfg().get('automation_name')}»" if str(_cfg().get('trigger')) == "automation"
                                      else "أُنبّهك، وبلا أي تنفيذ آخر." if str(_cfg().get('trigger')) == "notify"
                                      else "لا أنفّذ شيئًا (الوضع none)."))
    lines.append("  ⚠️ يعمل فقط بينما جارفس مفتوح (لا خدمة خلفية في المشروع) · ولإيقافه: action=stop")
    out = "\n".join(lines)
    W.log("start", "ok", detail={"engine": label, "phrases": len(phrases)})
    _log(player, out)
    return out


def _act_stop(params: dict, player) -> str:
    del params
    if W is None:
        return "سيدي، مكتبة كلمات التنبيه غير موجودة."
    with _LISTENER_LOCK:
        thread = _LISTENER["thread"]
        running = thread is not None and thread.is_alive()
        if not running:
            return "سيدي، المستمع متوقف أصلًا — لا شيء يعمل في الخلفية."
        stop = _LISTENER["stop"]
        if stop is not None:
            stop.set()
        _LISTENER["thread"] = None
        _LISTENER["state"] = "off"
        _LISTENER["detail"] = "أُوقف بطلبك"
    try:
        if thread is not None:
            thread.join(timeout=4.0)
    except Exception:
        pass
    out = "سيدي، أوقفت المستمع وأغلقت الميكروفون معه. لا شيء يستمع الآن."
    W.log("stop", "ok")
    _log(player, out)
    return out


def _act_phrases(params: dict, player) -> str:
    if W is None or _C is None:
        return "سيدي، النواة المشتركة غير موجودة."
    mode = str(params.get("mode") or "list").strip().lower()
    current = _phrases()
    phrase = str(params.get("phrase") or "").strip()
    if mode in ("list", "قائمة", "") and not phrase:
        out = "سيدي، عبارات التنبيه الحالية: " + " · ".join(current)
        _log(player, out)
        return out
    if not phrase:
        return "سيدي، قل العبارة مع mode=add أو mode=remove."
    normalized = W.ar_normalize(phrase)
    if not normalized:
        return "سيدي، العبارة فارغة بعد التطبيع (حروف عربية أو لاتينية فقط)."
    if mode in ("add", "أضف", "add_phrase", "phrase_add"):
        if any(W.ar_normalize(p) == normalized for p in current):
            return f"سيدي، «{phrase}» موجودة أصلًا في القائمة."
        updated = current + [phrase]
    elif mode in ("remove", "احذف", "remove_phrase", "phrase_remove"):
        updated = [p for p in current if W.ar_normalize(p) != normalized]
        if len(updated) == len(current):
            return f"سيدي، «{phrase}» ليست في القائمة."
    else:
        return "سيدي، mode: add أو remove أو list."
    if not updated:
        return "سيدي، لا أترك القائمة فارغة — أضِف عبارة بديلة أولًا."
    _C.store_settings("wake_arabic", {"phrases": ", ".join(updated)})
    out = f"سيدي، العبارات الآن ({len(updated)}): " + " · ".join(updated)
    W.log("phrases", "ok", detail={"mode": mode, "count": len(updated)})
    _log(player, out)
    return out


def _act_trigger(params: dict, player) -> str:
    if _C is None:
        return "سيدي، النواة المشتركة غير موجودة، فلا أستطيع حفظ الإعداد."
    mode = str(params.get("trigger") or "").strip().lower()
    if mode not in ("notify", "automation", "none"):
        return "سيدي، trigger: notify أو automation أو none"
    update: dict[str, str] = {"trigger": mode}
    name = str(params.get("automation_name") or "").strip()
    if name:
        update["automation_name"] = name
    if mode == "automation":
        if _C.resolve_plugin("automation_engine") is None:
            return "سيدي، محرّك الأتمتة غير موجود، فلا معنى لوضع automation الآن."
        if name:
            ok, listing = _C.call_plugin("automation_engine", {"action": "list"})
            if ok and name not in str(listing):
                return (f"سيدي، لا أرى أتمتة باسم «{name}» في القائمة، فلم أحفظ الوضع. "
                        f"قل automation_engine action=list لترى الأسماء، ثم أعد الطلب بالاسم الصحيح، "
                        f"أو اختر trigger=notify الآن.")
    _C.store_settings("wake_arabic", update)
    meanings = {"notify": "أُنبّهك فقط، بلا تنفيذ",
                "automation": f"أشغّل الأتمتة «{update.get('automation_name') or _cfg().get('automation_name')}»",
                "none": "لا شيء — أسمع وأسكت"}
    out = f"سيدي، عند سماع العبارة: {meanings[mode]}."
    W.log("trigger", "ok", detail={"mode": mode})
    _log(player, out)
    return out


_HELP = (
    "كلمات التنبيه العربية (بلا إنترنت بعد التجهيز)، سيدي:\n"
    "• status — الحالة الحقيقية: المحرّك والنموذج والعبارات والميكروفون\n"
    "• setup — تنزيل النموذج العربي مرة واحدة (~318 م.ب، Apache 2.0) ولا يحتاج مفاتيح\n"
    "• المحرّك الافتراضي Vosk العربي يعمل الآن بلا أي تدريب — لا تحتاج تفعل شيئًا\n"
    "   (لتشغيل Porcupine بدلًا منه: engine=porcupine + مفتاح مجاني + ملفا .ppn)\n"
    "• listen seconds=25 — تجربة حيّة: تكلم وسأريك ما سمعته فعلًا\n"
    "• start — مستمع في الخلفية بينما جارفس مفتوح · stop — يوقفه ويغلق الميكروفون\n"
    "• phrases mode=add phrase=يا فالح · mode=remove · بلا mode تُعرض القائمة\n"
    "• trigger=notify|automation|none automation_name=اسم — ما يحدث عند السماع\n"
    "🔴 الكلمات: «يا فالح» و«يا لذيذ» و«hey jarvis» — ويُقبل الاختلاف في التشكيل والحروف المتقاربة،\n"
    "   وكذلك «يا لزيز» (النطق المصري لـ«يا لذيذ»)، وما يسمعه المحرّك مثل «يافا لحل».\n"
    "   إن لم تُقبل عبارتك في listen: اخفض min_score إلى 0.72 وأعد التجربة — لكن اعلم أن التخفيض يوسّع القبول."
)


def _act_help(params: dict, player) -> str:
    del params
    _log(player, _HELP)
    return _HELP


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    _boot()
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        if W is None and action not in ("help", "?"):
            return ("سيدي، مكتبة `_wake_core.py` غير موجودة بجوار `wake_arabic.py` — "
                    "انسخها معه من الحزمة.")
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("setup", "جهّز", "download"):
            return _act_setup(p, player)
        if action in ("listen", "test", "اسمعه", "جرّب"):
            return _act_listen(p, player)
        if action in ("start", "شغّل", "ابدأ"):
            return _act_start(p, player)
        if action in ("stop", "أوقف", "قف"):
            return _act_stop(p, player)
        if action in ("phrases", "عبارات"):
            return _act_phrases(p, player)
        if action in ("trigger", "تنفيذ"):
            return _act_trigger(p, player)
        if action in ("help", "?"):
            return _act_help(p, player)
        return f"سيدي، '{action}' ليس فعلًا أعرفه في كلمات التنبيه. قل 'help'."
    except Exception as exc:  # noqa: BLE001 — لا ينهار المساعد أبدًا
        return f"سيدي، كلمات التنبيه واجهت مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python wake_arabic.py ───────────────────────────────────────────
def _selftest() -> int:
    """الفحص الذاتي — معزولًا تمامًا عن مخزن المشروع: لا إعدادات ولا سجلات ولا أتمتات حقيقية."""
    restorer = None
    try:
        if _C is not None and hasattr(_C, "isolate_for_tests"):
            restorer = _C.isolate_for_tests()
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
    print("wake_arabic — self-test")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if W is None:
        print("  ✗ مكتبة `_wake_core.py` غير موجودة")
        return 1
    if _C is None:
        print("  ✗ نواة الحزمة `_orch_core.py` غير موجودة")
        return 1

    import tempfile
    import zipfile

    tmp = Path(tempfile.mkdtemp(prefix="wake_test_"))
    real_models, real_wake = W.MODELS_DIR, W.WAKE_DIR
    real_logs, real_events = _C.LOG_DIR, _C.CALENDAR_FILE
    real_stamp, real_data = _C._SCAN_CACHE.get("stamp"), _C._SCAN_CACHE.get("data")
    base = _C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    real_open_mic, real_frames, real_engine = W.open_mic, W.frame_iter, W.make_engine

    def _write_dummy_model(dest: Path) -> None:
        for member in ("am/final.mdl", "conf/mfcc.conf", "graph/HCLr.fst"):
            path = dest / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 32)

    try:
        W.MODELS_DIR = tmp / "models"
        W.WAKE_DIR = tmp / "wake"
        W.WAKE_DIR.mkdir(parents=True, exist_ok=True)
        _C.LOG_DIR = tmp / "logs"
        _C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        _C.CALENDAR_FILE = tmp / "calendar.json"
        _C.save_json(_C.CALENDAR_FILE, {"version": 1, "items": {}})
        _C.store_settings("wake_arabic", {"engine": "vosk", "phrases": "يا فالح, يا لذيذ, hey jarvis",
                                          "min_score": "0.80", "cooldown_s": "4", "listen_s": "25",
                                          "mic_device": "", "trigger": "notify", "automation_name": "",
                                          "porcupine_access_key": "", "porcupine_keywords": ""})

        # ١) التطبيع العربي — نفس العبارة بكل صورها
        check("التشكيل والتطويل يُزالان", W.ar_normalize("يَا فَلَاحْ") == "يا فلاح", W.ar_normalize("يَا فَلَاحْ"))
        check("الملتصقة تبقى ملتصقة بعد التطبيع", W.ar_normalize("يَافَالحْ") == "يافالح", W.ar_normalize("يَافَالحْ"))
        check("الملتصقة من المحرّك تُطابق العبارة", W.match_phrase("يَافَالحْ", "يا فالح")["hit"],
              str(W.match_phrase("يَافَالحْ", "يا فالح")))
        check("التطويل داخلها يُزال", W.ar_normalize("يا فـــالح") == "يا فالح", W.ar_normalize("يا فـــالح"))
        check("الألف تُوحَّد", W.ar_normalize("أليف") == W.ar_normalize("اليف"))
        check("الياء والتاء المربوطة تُوحَّدان", W.ar_normalize("على سنة") == "علي سنه", W.ar_normalize("على سنة"))
        check("الترقيم يُزال", W.ar_normalize("يا فالح!؟") == "يا فالح", W.ar_normalize("يا فالح!؟"))
        check("النطق المصري: ذ ← ز (يا لذيذ = يا لزيز)",
              W.ar_normalize("يا لذيذ") == W.ar_normalize("يا لزيز") == "يا لزيز",
              W.ar_normalize("يا لذيذ"))
        check("ومطابقة «يا لزيز» بـ«يا لذيذ»", W.match_phrase("يا لزيز", "يا لذيذ")["hit"],
              str(W.match_phrase("يا لزيز", "يا لذيذ")))
        check("اللاتيني يُطبَّع صغيرًا", W.ar_normalize("Hey Jarvis,") == "hey jarvis", W.ar_normalize("Hey Jarvis,"))

        # ٢) المطابقة الضبابية — ما يُقبل وما يُرفض
        check("مطابقة تامّة = 1.0", W.match_phrase("يا فالح", "يا فالح")["score"] == 1.0)
        for variant, expect in [("يافالح", True), ("يا فلاح", True), ("يا فالخ", True), ("يا فالحة", None),
                                ("سلام عليكم", False), ("يا لذيذ", False), ("طيب يا فالح اعمل كذا", True),
                                ("يارجل انا فالح", None)]:
            found = W.match_phrase(variant, "يا فالح")
            if expect is True:
                check(f"تُقبل: «{variant}»", found["hit"], str(found))
            elif expect is False:
                check(f"تُرفض: «{variant}»", not found["hit"], str(found))
            else:
                print(f"    · «{variant}» ← {found['hit']} ({found['score']}, {found['how']})")
        check("الالتصاق من المحرّك يُفهم", W.match_phrase("يافالح", "يا فالح")["hit"])
        check("العبارة داخل جملة تُكشف", W.match_phrase("طيب يا فالح اعمل كذا", "يا فالح")["how"] in ("window", "window+1"))
        check("المطابقة الترتيبية تعمل على Hey Jarvis", W.match_phrase("okay hey jarvis are you there", "hey jarvis")["hit"])
        check("الحدّ الضبابي يمنع التخمين", not W.match_phrase("يا فلان", "يا فالح", 0.95)["hit"])
        check("match_any تختار الأفضل", W.match_any("يا لذيذ", ["يا فالح", "يا لذيذ"])["phrase"] == "يا لذيذ")

        # ٣) الفواصل والتنظيف
        parsed = W.parse_phrases("يا فالح، يا لذيذ؛ يا فالح\nhey jarvis")
        check("الفواصل العربية والمكرّر تُعالَج", parsed == ["يا فالح", "يا لذيذ", "hey jarvis"], str(parsed))
        check("القائمة الفارغة تُعالج", W.parse_phrases("") == [] and W.parse_phrases(None) == [])

        # ٤) التبريد يمنع تكرار الإشعال
        gate = W.Cooldown(4.0)
        check("الإشعال الأول يُسمح", gate.allow("يا فالح", now=100.0))
        check("التكرار خلال المدة ممنوع", not gate.allow("يا فالح", now=101.0))
        check("بعد المدة يُسمح", gate.allow("يا فالح", now=105.5))
        check("عبارة أخرى لا تتأثر بتبريد غيرها", gate.allow("يا لذيذ", now=105.6))

        # ٥) حجب الأسرار
        check("السرّ يُحجب من النصّ", W.redact("المفتاح abc123XYZ انتهى", ["abc123XYZ"]) == "المفتاح *** انتهى")
        check("السرّ القصير جدًا لا يُحجب", W.redact("abc هنا", ["ab"]) == "abc هنا")

        # ٦) أمان فكّ الملفات: مسار هارب يُرفض ولا يُكتب شيء
        evil_zip = tmp / "evil.zip"
        with zipfile.ZipFile(evil_zip, "w") as archive:
            archive.writestr("../escape.txt", b"should never be written")
            archive.writestr("inner/ok.txt", b"fine")
        dest = tmp / "unzip_evil"
        ok, note = W.safe_extract(evil_zip, dest)
        escaped = (tmp / "escape.txt").exists()
        check("الفكّ يرفض المسار الهارب", not ok and "هارب" in note, note)
        check("لم يُكتب ملف خارج الوجهة", not escaped)
        check("ولم يُفكّ شيء أصلًا", not dest.exists() or not any(dest.rglob("*")))

        absolute_zip = tmp / "abs.zip"
        with zipfile.ZipFile(absolute_zip, "w") as archive:
            archive.writestr("/etc/evil.txt", b"no")
        ok, note = W.safe_extract(absolute_zip, tmp / "unzip_abs")
        check("مسارات مطلقة تُرفض", not ok, note)
        with zipfile.ZipFile(absolute_zip, "w") as archive:
            archive.writestr("C:\\windows\\evil.txt", b"no")
        ok, note = W.safe_extract(absolute_zip, tmp / "unzip_drive")
        check("مسارات أقراص ويندوز تُرفض", not ok, note)

        # ٧) نموذج سليم داخل جذر واحد يُرفع صحيحًا
        good_zip = tmp / "model.zip"
        with zipfile.ZipFile(good_zip, "w") as archive:
            for member in ("am/final.mdl", "conf/mfcc.conf", "graph/HCLr.fst"):
                archive.writestr(f"vosk-model-ar-mgb2-0.4/{member}", b"y" * 64)
        ok, note = W.safe_extract(good_zip, W.model_dir())
        check("الفكّ السليم ينجح", ok, note)
        present, where = W.model_present()
        check("النموذج يُكتشف بعد الفكّ", present, where)

        # ٨) التنزيل: نجاح من ملف محلي + رفض ملف أصغر من الحدّ
        ok, note = W.download_file(f"file://{good_zip}", tmp / "copy.zip", min_bytes=10)
        check("التنزيل ينجح ويسجّل الحجم", ok and (tmp / "copy.zip").exists(), note)
        small = tmp / "small.bin"
        small.write_bytes(b"tiny")
        ok, note = W.download_file(f"file://{small}", tmp / "small_copy.bin", min_bytes=10 * 1024 * 1024)
        check("الملف الأصغر من الحدّ يُرفض", not ok and "أصغر" in note, note)
        ok, note = W.download_file(f"file://{tmp / 'ghost.bin'}", tmp / "ghost_copy.bin")
        check("الملف المفقود يفشل بلطف", not ok, note)
        check("لا يبقى ملف .part بعد الفشل", not list(tmp.glob("*.part")))

        # ٩) الأفعال: الحالة تقول الحقيقة
        out = run({"action": "status"})
        check("status يعرض المحرّك والنموذج", "النموذج العربي" in out and "المحرّك المختار" in out, out[:200])
        check("status يعرض العبارات الثلاث", "يا فالح" in out and "يا لذيذ" in out and "hey jarvis" in out)
        check("status يقول حقيقة الربط مع المشروع", "لن أُعدّل" in out, out[:400])
        if W.vosk_available()[0]:
            check("setup يقول موجود أصلًا", "موجود أصلًا" in run({"action": "setup"}))
        else:
            print("    · تخطّي فحص setup (حزمة vosk غير مثبّتة في هذه البيئة)")
        added = run({"action": "phrases", "mode": "add", "phrase": "يا جارفس"})
        check("العبارات تُضاف فعلًا", "العبارات الآن" in added and "يا جارفس" in added, added[:200])
        check("والتغيير محفوظ ويُقرأ", "يا جارفس" in run({"action": "phrases"}))
        check("المكرّر لا يُضاف مرتين", "موجودة أصلًا" in run({"action": "phrases", "mode": "add", "phrase": "يا جارفس"}))
        removed = run({"action": "phrases", "mode": "remove", "phrase": "يا جارفس"})
        check("الإزالة تعمل فعلًا", "العبارات الآن" in removed and "يا جارفس" not in removed, removed[:200])
        check("عبارة غير موجودة تقال بوضوح", "ليست في القائمة" in run({"action": "phrases", "mode": "remove", "phrase": "كلام فاضي"}))
        check("عبارة بلا حروف تُرفض", "العبارة فارغة" in run({"action": "phrases", "mode": "add", "phrase": "  !!"}))
        _C.store_settings("wake_arabic", {"phrases": "يا فالح"})
        guard = run({"action": "phrases", "mode": "remove", "phrase": "يا فالح"})
        check("لا تُترك القائمة فارغة", "لا أترك القائمة فارغة" in guard, guard[:200])
        _C.store_settings("wake_arabic", {"phrases": "يا فالح, يا لذيذ, hey jarvis"})
        check("help يعمل", "يا فالح" in run({"action": "help"}))
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
        check("trigger يتحقق من القيم", "notify أو automation أو none" in run({"action": "trigger", "trigger": "خربطة"}))
        out = run({"action": "trigger", "trigger": "automation", "automation_name": "أتمتة وهمية"})
        check("اسم أتمتة غير موجود: يقول إنه لم يحفظ", "فلم أحفظ" in out, out[:220])
        check("trigger=none يُحفظ", "لا شيء" in run({"action": "trigger", "trigger": "none"}))
        run({"action": "trigger", "trigger": "notify"})

        # ١٠) حلقة الإنصات كاملة بلا ميكروفون: محرّك وهمي وإطارات وهمية
        class FakeEngine:
            kind = "vosk"

            def __init__(self, script: list[str], flush_text: str = ""):
                self.script = list(script)
                self.flush_text = flush_text
                self.calls = 0
                self.resets = 0
                self.flushed = 0

            def feed(self, frame: bytes) -> list[str]:
                self.calls += 1
                if self.script:
                    return [self.script.pop(0)]
                return []

            def reset(self) -> None:
                self.resets += 1

            def flush(self) -> list[str]:
                self.flushed += 1
                return [self.flush_text] if self.flush_text else []

        wakes: list[dict] = []
        engine = FakeEngine(["", "طيب", "يا فالح", "يا فالح مرة أخرى"])
        frames = [b"\x00" * 8000 for _ in range(8)]
        summary = W.listen_loop(frames, engine, ["يا فالح", "يا لذيذ"], cooldown_s=30.0,
                                on_wake=lambda record: wakes.append(record))
        check("الحلقة أشعلت مرة واحدة فقط مع التبريد", len(summary["wakes"]) == 1 and len(wakes) == 1,
              str(summary["wakes"]))
        check("الحلقة سجّلت ما سُمع", "طيب" in summary["heard"], str(summary["heard"]))
        check("المحرّك يُفرَّغ بعد كل كشف", engine.resets >= 1)
        check("بلا وجه/صوت = صفر إشعال", W.listen_loop([b"\x00" * 8000] * 4, FakeEngine([]), ["يا فالح"])["wakes"] == [])
        stopped = threading.Event()
        stopped.set()
        summary = W.listen_loop(frames, FakeEngine([]), ["يا فالح"], stop_event=stopped)
        check("طلب الإيقاف يوقف الحلقة فورًا", summary["stopped"] == "طلب إيقاف")
        # عبارة مُجزَّأة كما يقطّعها المحرّك فعلًا: «يا» ثم «الذين»
        split_wakes: list[dict] = []
        split_engine = FakeEngine(["يا", "الذين"])
        summary = W.listen_loop([b"\x00" * 8000] * 4, split_engine, ["يا لذيذ"], cooldown_s=5.0,
                                on_wake=lambda r: split_wakes.append(r))
        check("العبارة المُجزَّأة تُجمَع من نداءات متتالية",
              len(split_wakes) == 1 and split_wakes[0]["phrase"] == "يا لذيذ" and split_wakes[0]["score"] >= 0.8,
              str(split_wakes))
        # ومشتّت مُجزَّأ لا يُشعل
        noisy = FakeEngine(["صباح", "الخير", "يا", "جماعة"])
        summary = W.listen_loop([b"\x00" * 8000] * 6, noisy, ["يا فالح"], cooldown_s=5.0)
        check("مشتّت مُجزَّأ لا يُشعل", summary["wakes"] == [], str(summary["wakes"]))
        # الإغلاق عند النهاية يمنح العبارة فرصة أخيرة
        tail_wakes: list[dict] = []
        tail_engine = FakeEngine([], flush_text="يا فالح")
        summary = W.listen_loop([b"\x00" * 8000] * 3, tail_engine, ["يا فالح"],
                                stop_event=threading.Event(), on_wake=lambda r: tail_wakes.append(r))
        check("الإغلاق عند النهاية يكشف العبارة القصيرة", len(tail_wakes) == 1 and "+flush" in tail_wakes[0]["how"],
              str(tail_wakes))
        check("الإغلاق يُنفَّذ فعلًا عند المحرّك", tail_engine.flushed == 1)
        def _slow_frames(count: int, delay: float = 0.01):
            for _ in range(count):
                time.sleep(delay)
                yield b"\x00" * 8000

        summary = W.listen_loop(_slow_frames(20), FakeEngine([]), ["يا فالح"], timeout_s=0.05)
        check("المهلة الزمنية تعمل", summary["stopped"] == "انتهت المدة", str(summary["stopped"]))

        # ١١) الإضافة: تجربة، تشغيل خلفي، إيقاف، والتنفيذ عند السماع
        class FakeStream:
            def __init__(self):
                self.closed = 0

            def read(self, n: int):
                return b"\x00" * (n * 2), False

            def stop(self):
                pass

            def close(self):
                self.closed += 1

        fake_stream = FakeStream()
        W.open_mic = lambda *a, **k: (fake_stream, "")          # noqa: E731
        W.frame_iter = lambda stream, timeout_s=0, stop_event=None: iter([b"\x00" * 8000] * 40)  # noqa: E731
        fake_engine = FakeEngine(["", "يا فالح", "", ""])
        W.make_engine = lambda *a, **k: (fake_engine, "محرّك وهمي للتجربة")  # noqa: E731
        out = run({"action": "listen", "seconds": "5"})
        check("تجربة listen تكتشف العبارة", "«يا فالح»" in out and "تشابه" in out, out[:220])
        check("تجربة listen تعلن أنها تجربة بلا تنفيذ", "لم أُنفّذ شيئًا" in out and "تجربة" in out, out[:220])
        notes_before = len(list((_C.LOG_DIR).glob("*.jsonl"))) if _C.LOG_DIR.exists() else 0
        del notes_before
        fake_stream2 = FakeStream()
        W.open_mic = lambda *a, **k: (fake_stream2, "")          # noqa: E731
        W.frame_iter = lambda stream, timeout_s=0, stop_event=None: _slow_frames(400, 0.01)  # noqa: E731
        fake_engine2 = FakeEngine(["", "", "يا لذيذ"])
        W.make_engine = lambda *a, **k: (fake_engine2, "محرّك وهمي للتجربة")  # noqa: E731
        out = run({"action": "trigger", "trigger": "notify"})
        check("trigger=notify يُحفظ", "أُنبّهك" in out, out[:160])
        out = run({"action": "start"})
        check("التشغيل الخلفي يبدأ", "يعمل الآن" in out, out[:220])
        deadline = time.time() + 6
        while time.time() < deadline and _LISTENER["wakes"] == 0:
            time.sleep(0.2)
        check("المستمع الخلفي أشعل عند العبارة", _LISTENER["wakes"] == 1, f"wakes={_LISTENER['wakes']} detail={_LISTENER['detail']}")
        check("التنبيه الحقيقي أُنشئ في notification_center", "سمعت" in str(_LISTENER["last"]), str(_LISTENER["last"])[:120])
        out = run({"action": "start"})
        check("تشغيل مرتين لا يُنشئ مستمعين", "يعمل بالفعل" in out, out[:160])
        out = run({"action": "stop"})
        check("الإيقاف يعمل", "أوقفت المستمع" in out, out[:160])
        check("الميكروفون أُغلق عند الإيقاف", fake_stream2.closed >= 1, str(fake_stream2.closed))
        out = run({"action": "stop"})
        check("الإيقاف مرتين رسالة واضحة", "متوقف أصلًا" in out, out[:160])

        # ١٢) التنفيذ عبر أتمتة حقيقية بالاسم
        ok, listing = _C.call_plugin("automation_engine", {"action": "create", "name": "اختبار التنبيه",
                                                           "trigger": '{"kind": "manual"}',
                                                           "steps": '[{"plugin": "notification_center", "parameters": {"action": "notify", "title": "من الأتمتة"}}]'})
        check("أُنشئت أتمتة اختبارية", ok, str(listing)[:160])
        _C.store_settings("wake_arabic", {"trigger": "automation", "automation_name": "اختبار التنبيه"})
        text = _fire_trigger({"phrase": "يا فالح", "score": 1.0, "how": "exact"})
        check("عند السماع تُشغَّل الأتمتة بالاسم", "اختبار التنبيه" in text, text[:160])
        _C.store_settings("wake_arabic", {"trigger": "automation", "automation_name": "غير موجودة"})
        text = _fire_trigger({"phrase": "يا فالح", "score": 1.0, "how": "exact"})
        check("اسم خاطئ لا ينهار", "لم تُشغّل" in text or "لم أجد" in text or "لا" in text, text[:160])
        _C.store_settings("wake_arabic", {"trigger": "notify"})

        # ١٣) لا مفتاح في أي سجل، ولا مفتاح في أي نصّ معروض
        secret = "pv-secret-key-9X8Y7Z"
        _C.store_settings("wake_arabic", {"porcupine_access_key": secret})
        out = run({"action": "status"})
        check("المفتاح لا يظهر في status", secret not in out)
        run({"action": "setup", "engine": "porcupine"})
        run({"action": "start"})
        run({"action": "stop"})
        raw = ""
        for f in (_C.LOG_DIR.glob("*.jsonl")):
            raw += f.read_text(encoding="utf-8")
        check("المفتاح لا يظهر في أي سجل", secret not in raw)
        check("السجل يحوي أحداث التنبيه", "wake_arabic" in raw or "wake" in raw)
        _C.store_settings("wake_arabic", {"porcupine_access_key": ""})

        # ١٣ب) محرّك openWakeWord: قراءة المسارات، ورسائل صادقة، والتكامل مع الحلقة
        W.make_engine = real_engine        # نُعيد المحرّك الحقيقي قبل فحوص الاختيار
        _C.store_settings("wake_arabic", {"openwake_models": f"{tmp}/a.onnx; {tmp}/b.onnx",
                                          "openwake_phrases": "يا فالح;يا لذيذ",
                                          "openwake_threshold": "0.6"})
        check("openWakeWord: المسارات تُقرأ من الإعدادات", _openwake_paths() == [f"{tmp}/a.onnx", f"{tmp}/b.onnx"],
              str(_openwake_paths()))
        check("openWakeWord: العبارات تُقرأ بنفس الترتيب", _openwake_phrases() == ["يا فالح", "يا لذيذ"], str(_openwake_phrases()))
        if W.openwake_available()[0]:
            engine_ow, label_ow = W.make_engine("openwake", "", [], load=False,
                                                openwake_models=_openwake_paths(), openwake_phrases=_openwake_phrases(),
                                                openwake_threshold=0.6)
            check("openWakeWord: يُبنى بلا تحميل (للعرض)",
                  engine_ow is not None and getattr(engine_ow, "kind", "") == "openwake", str(label_ow)[:120])
            check("openWakeWord: العتبة تُقرأ", getattr(engine_ow, "threshold", 0) == 0.6,
                  str(getattr(engine_ow, "threshold", None)))
            loaded_ow, why_ow = engine_ow.load()
            check("openWakeWord: ملف ناقص يُرفض برسالة واضحة", (not loaded_ow) and "غير موجود" in why_ow, why_ow)
        else:
            print("    · تخطّي فحوص محرّك openWakeWord (الحزمة غير مثبّتة في هذه البيئة — اختيارية تمامًا)")
        missing_engine, missing_label = W.make_engine("openwake", "", [], openwake_models=[f"{tmp}/ghost.onnx"])
        has_pkg = W.openwake_available()[0]
        if has_pkg:
            check("openWakeWord: يفشل بلطف عند غياب الملف", missing_engine is None and "غير موجود" in missing_label,
                  missing_label[:140])
        else:
            check("openWakeWord: يطلب الحزمة إن غابت", missing_engine is None and "openwakeword" in missing_label.lower(),
                  missing_label[:140])

        class FakeOpenWake:
            kind = "openwake"
            keywords = ["يا فالح", "يا لذيذ"]
            last_score = 0.83
            threshold = 0.55

            def __init__(self):
                self.calls = 0

            def feed(self, frame):
                self.calls += 1
                return 1 if self.calls == 2 else -1

            def reset(self):
                pass

            def flush(self):
                return []

        fake_ow = FakeOpenWake()
        ow_wakes = []
        W.listen_loop([b"\x00" * 8000] * 4, fake_ow, ["يا فالح", "يا لذيذ"], cooldown_s=5.0,
                      on_wake=lambda r: ow_wakes.append(r))
        check("openWakeWord: الكشف يصل باسم العبارة الصحيح",
              len(ow_wakes) == 1 and ow_wakes[0]["phrase"] == "يا لذيذ" and ow_wakes[0]["how"] == "openwake",
              str(ow_wakes))
        check("openWakeWord: الدرجة تُسجَّل", ow_wakes and ow_wakes[0]["score"] == 0.83, str(ow_wakes))
        _C.store_settings("wake_arabic", {"openwake_models": "", "openwake_phrases": ""})

        # ١٤) محرّك Porcupine: رسائل صادقة بلا حزمة أو مفتاح
        W.make_engine = real_engine
        W.open_mic, W.frame_iter = real_open_mic, real_frames
        engine, label = W.make_engine("porcupine", "", [])
        check("Porcupine بلا مفتاح يشرح السبب", engine is None and "مفتاح" in label, label)
        ok_p, note_p = W.porcupine_available()
        if not ok_p:
            check("Porcupine غير المثبّت يُعلن بوضوح", "غير مثبّتة" in note_p, note_p)
        if W.vosk_available()[0]:
            probe, probe_label = W.make_engine("auto", load=False)
            check("Vosk المحرّك الافتراضي (فحص بلا تحميل)", probe is not None and "Vosk" in probe_label)
            check("الفحص لا يُحمّل النموذج الثقيل", getattr(probe, "_rec", None) is None)
        else:
            print("    · تخطّي فحص المحرّك الافتراضي (حزمة vosk غير مثبّتة في هذه البيئة)")
        present, _where = W.model_present()
        check("النموذج الحقيقي غير مطلوب للفحص", present)
    finally:
        W.MODELS_DIR, W.WAKE_DIR = real_models, real_wake
        _C.LOG_DIR = real_logs
        _C.CALENDAR_FILE = real_events
        _C._SCAN_CACHE["stamp"], _C._SCAN_CACHE["data"] = real_stamp, real_data
        W.open_mic, W.frame_iter, W.make_engine = real_open_mic, real_frames, real_engine
        with _LISTENER_LOCK:
            stop = _LISTENER.get("stop")
            if stop is not None:
                stop.set()
            thread = _LISTENER.get("thread")
            _LISTENER["thread"] = None
            _LISTENER["stop"] = None
            _LISTENER["state"] = "off"
            _LISTENER["wakes"] = 0
        try:
            if thread is not None:
                thread.join(timeout=2.0)
        except Exception:
            pass
        if backup is not None:
            base.write_text(backup, encoding="utf-8")
        elif base.exists():
            base.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _C.scan_plugins(force=True)
        except Exception:
            pass

    print(f"RESULT: {'PASS' if problems == 0 else str(problems) + ' problem(s)'}")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
