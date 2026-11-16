"""
screen_reader.py — «اقرأ لي اللي على الشاشة»: لقطة ← قراءة عربية ← صوت أو حافظة.

يُكمل ثلاثًا موجودة عندك: `screen_recorder` (تصوير الشاشة) و`ocr` (قراءة الصور) و`voice_reader`
(النطق) — وهنا الخطوة الناقصة: **الثلاثة في أمر واحد**.

• **اللقطة**: `mss` ثم `PIL.ImageGrab` ثم `pyautogui` ثم دالة `ocr.py` نفسه — أيٌّ توفّر يعمل
  (كلها من متطلبات مشروعك، بلا تثبيت جديد).
• **القراءة**: `pytesseract` مباشرة، وإن غاب فإضافة `ocr` عندك تتولّى المهمة.
• **الناتج**: `speak` صوتًا (عبر `voice_reader`) · `copy` في الحافظة · `both` · `text` مكتوبًا فقط.
• **بلا تراكم**: تُحفظ آخر لقطة ونصّها في ملفين ثابتين فقط (`screen_last.*`) — لا تملأ القرص.
• **حماية**: نصّ فيه ما يشبه المفاتيح لا يُنطق بصوت عالٍ بلا `force=yes`، ولا تُرسل الصورة لأي خدمة.

🔴 لا سطر يرمي استثناءً للخارج، ولا اتصال بأي خدمة خارجية (كل شيء على جهازك).
"""
from __future__ import annotations

import importlib.util
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "screen_reader",
    "description": (
        "Reads what is written on the screen out loud (Arabic + English OCR on your own machine): "
        "screenshot → local OCR → speak it, copy it, or both. Use for: 'read what is on my screen', "
        "'read this error on the screen', 'copy the text on the screen', 'read the selected dialog', "
        "'what does this popup say', 'screen reader status'. It can also read a region or a specific "
        "monitor. Nothing is uploaded anywhere. Do NOT use it for reading files on disk (use "
        "voice_reader) or for recording video (use screen_recorder)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read | copy | status | help"},
            "mode": {"type": "STRING", "description": "read: speak | copy | both | text (default speak)"},
            "region": {"type": "STRING", "description": "Optional crop: x,y,width,height"},
            "monitor": {"type": "NUMBER", "description": "Which monitor to capture (1 = primary)"},
            "languages": {"type": "STRING", "description": "OCR languages, default ara+eng"},
            "force": {"type": "STRING", "description": "'yes' to speak text that looks like secrets"},
            "limit": {"type": "NUMBER", "description": "Maximum characters to speak, default 4000"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "screen_reader",
    "title": "Read the screen aloud (local OCR)",
    "fields": [
        {"key": "languages", "type": "text", "label": "OCR languages", "default": "ara+eng"},
        {"key": "speak_limit", "type": "text", "label": "Maximum characters to speak", "default": "4000"},
        {"key": "capture_engine", "type": "choice", "label": "Screenshot engine", "default": "auto",
         "choices": ["auto", "mss", "pillow", "pyautogui", "ocr_plugin"]},
    ],
}

T = None
_LAST_PNG = "screen_last.png"
_LAST_TXT = "screen_last.txt"


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


def _extras() -> Optional[Path]:
    if T is None:
        return None
    path = T.extras_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return path


def parse_region(value: Any) -> tuple[Optional[tuple[int, int, int, int]], str]:
    """يحلّل «x,y,w,h» إلى أربعة أرقام. يعيد (المنطقة أو None، سبب الرفض)."""
    raw = str(value or "").strip()
    if not raw:
        return None, ""
    numbers = re.findall(r"-?\d+", T.normalize_digits(raw) if T else raw)
    if len(numbers) != 4:
        return None, f"صيغة المنطقة تحتاج أربعة أرقام (x,y,العرض,الارتفاع) — وصلني «{raw}»."
    x, y, width, height = (int(n) for n in numbers)
    if width <= 0 or height <= 0 or width > 20000 or height > 20000:
        return None, "عرض أو ارتفاع المنطقة غير معقول."
    return (x, y, width, height), ""


# ── اللقطة ───────────────────────────────────────────────────────────────────
def _capture_mss(target: Path, region: Optional[tuple[int, int, int, int]], monitor: int) -> str:
    try:
        import mss  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        return f"{exc.name} غير مثبّتة (وهي في متطلبات مشروعك: python setup.py)."
    try:
        with mss.mss() as grabber:
            monitors = grabber.monitors
            index = max(1, min(len(monitors) - 1, int(monitor))) if len(monitors) > 1 else 0
            area = monitors[index]
            if region:
                x, y, width, height = region
                area = {"left": area["left"] + x, "top": area["top"] + y, "width": width, "height": height}
            shot = grabber.grab(area)
        Image.frombytes("RGB", shot.size, shot.rgb).save(str(target), "PNG")
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"تعذّرت اللقطة عبر mss ({type(exc).__name__})."


def _capture_pillow(target: Path, region: Optional[tuple[int, int, int, int]], monitor: int) -> str:
    try:
        from PIL import ImageGrab  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        return f"{exc.name} غير مثبّتة (وهي في متطلبات مشروعك)."
    try:
        box = None
        if region:
            x, y, width, height = region
            box = (x, y, x + width, y + height)
        image = ImageGrab.grab(bbox=box, all_screens=int(monitor) > 1)
        image.save(str(target), "PNG")
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"تعذّرت اللقطة عبر Pillow ({type(exc).__name__})."


def _capture_pyautogui(target: Path, region: Optional[tuple[int, int, int, int]], monitor: int) -> str:
    del monitor
    try:
        import pyautogui  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        return f"{exc.name} غير مثبّتة (وهي من متطلبات مشروعك)."
    try:
        image = pyautogui.screenshot(region=region)
        image.save(str(target), "PNG")
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"تعذّرت اللقطة عبر pyautogui ({type(exc).__name__})."


def _capture_via_ocr_plugin(target: Path) -> str:
    """يستخدم دالة `grab_screenshot` من إضافة OCR عندك — تكامل بلا تكرار كود."""
    if T is None:
        return "النواة غير محمّلة."
    module = T.sibling_module("ocr.py", "ocr")
    if module is None or not hasattr(module, "grab_screenshot"):
        return "إضافة ocr غير موجودة بجواري."
    try:
        ok, image = module.grab_screenshot()
        if not ok:
            return f"إضافة ocr لم تستطع التصوير: {str(image)[:90]}"
        image.save(str(target), "PNG")
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"تعذّر استخدام مصوّر ocr ({type(exc).__name__})."


def _capture(target: Path, region: Optional[tuple[int, int, int, int]], monitor: int) -> tuple[bool, str]:
    """يلتقط الشاشة بأول محرّك يعمل. يعيد (نجح، آخر سبب فشل)."""
    engine = str(_cfg().get("capture_engine") or "auto").lower()
    order = {
        "auto": [_capture_mss, _capture_pillow, _capture_pyautogui, _capture_via_ocr_plugin],
        "mss": [_capture_mss],
        "pillow": [_capture_pillow],
        "pyautogui": [_capture_pyautogui],
        "ocr_plugin": [_capture_via_ocr_plugin],
    }.get(engine, [_capture_mss, _capture_pillow, _capture_pyautogui, _capture_via_ocr_plugin])
    reasons: list[str] = []
    for function in order:
        try:
            why = function() if function is _capture_via_ocr_plugin else function(target, region, monitor)
        except TypeError:
            why = function(target, region, monitor)
        except Exception as exc:  # noqa: BLE001
            why = f"{function.__name__}: {type(exc).__name__}"
        if not why and target.exists() and target.stat().st_size > 0:
            return True, ""
        reasons.append(f"{function.__name__}: {why}")
    return False, " | ".join(reasons[:3])


# ── القراءة الضوئية ──────────────────────────────────────────────────────────
def _ocr_pytesseract(path: Path, languages: str) -> tuple[str, str]:
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ModuleNotFoundError:
        return "", "missing"
    try:
        from pytesseract import TesseractNotFoundError  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        TesseractNotFoundError = RuntimeError  # type: ignore[assignment,misc]
    try:
        with Image.open(str(path)) as image:
            text = pytesseract.image_to_string(image, lang=str(languages or "ara+eng"))
        return str(text or ""), ""
    except TesseractNotFoundError:
        return "", ("برنامج Tesseract نفسه غير منصَّب على ويندوز (pytesseract مجرد واجهة). "
                    "نزّله من: github.com/UB-Mannheim/tesseract/wiki  وعلّم على Arabic أثناء التثبيت.")
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if "lang" in str(exc).lower() or "data" in str(exc).lower():
            return "", ("Tesseract مثبّت لكن بيانات اللغة العربية ناقصة — أعد التثبيت وعلّم على Arabic "
                        "(اسم الحزمة ara). ولغاتك الحالية من إعدادات الإضافة: " + str(languages))
        return "", f"تعذّرت القراءة عبر pytesseract ({name}: {str(exc)[:80]})"


def _ocr_via_plugin(path: Path, languages: str) -> tuple[str, str]:
    """يطلب من إضافة ocr عندك أن تقرأ الصورة — نفس جودة المشروع وبلا تكرار."""
    if T is None:
        return "", "النواة غير محمّلة."
    ok, result = T.call_plugin("ocr", {"action": "read", "path": str(path), "languages": languages},
                               timeout_s=90)
    if not ok:
        return "", result
    text = str(result or "")
    marker = "ما استطعت قراءته"
    if marker in text:
        text = text.split(":", 1)[-1]
    return text, ""


def ocr_image(path: Path, languages: str) -> tuple[str, str]:
    """سلسلة القراءة: pytesseract ثم إضافة ocr ثم رسالة تثبيت واضحة."""
    text, why = _ocr_pytesseract(path, languages)
    if text.strip():
        return text, ""
    first_why = why
    if why == "missing" or why:
        plugin_text, plugin_why = _ocr_via_plugin(path, languages)
        if plugin_text.strip():
            return plugin_text, ""
        if plugin_why and "غير موجودة" not in plugin_why:
            return "", f"{first_why or ''} · {plugin_why}".strip(" ·")
    if first_why == "missing":
        return "", ("قراءة الشاشة تحتاج محرّكًا ضوئيًا: إمّا pytesseract (pip install pytesseract) مع برنامج "
                    "Tesseract، أو إضافة ocr عندك — لم أجد أيًّا منهما.")
    return "", first_why or "لم أقرأ نصًّا في اللقطة."


def _looks_secretish(text: str) -> int:
    tokens = re.findall(r"\b[A-Za-z0-9_\-]{24,}\b", text or "")
    return len([t for t in tokens if not t.isdigit()])


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _do_read(params: dict, player: Any, forced_mode: str = "") -> str:
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري — انسخها معي."
    mode = str(forced_mode or params.get("mode") or "speak").strip().lower()
    if mode not in ("speak", "copy", "both", "text"):
        return "سيدي، الوضع يقبل: speak أو copy أو both أو text."
    languages = str(params.get("languages") or _cfg().get("languages") or "ara+eng")
    region, region_why = parse_region(params.get("region"))
    if region_why:
        return f"سيدي، {region_why}"
    try:
        monitor = int(float(str(params.get("monitor") or 1)))
    except Exception:
        monitor = 1
    extras = _extras()
    if extras is None:
        return "سيدي، تعذّر تجهيز مجلد الإعدادات لآخر لقطة."
    target = extras / _LAST_PNG
    ok, why = _capture(target, region, monitor)
    if not ok:
        return ("سيدي، لم أستطع أخذ لقطة للشاشة:\n  " + why[:300] +
                "\n  (هذا يحدث في جلسة بلا شاشة — جرّب على جهازك مباشرة.)")
    text, ocr_why = ocr_image(target, languages)
    if not text.strip():
        return (f"سيدي، أخذت اللقطة ({T.human_bytes(target.stat().st_size)}) لكني لم أجد نصًّا مقروءًا فيها."
                + (f"\n  السبب: {ocr_why[:250]}" if ocr_why else ""))
    text = text.strip()
    try:
        (extras / _LAST_TXT).write_text(text, encoding="utf-8")
    except Exception:
        pass
    words = len(re.findall(r"\S+", text))
    head = f"سيدي، قرأت {words} كلمة من الشاشة."
    speak_target = str(params.get("limit") or _cfg().get("speak_limit") or 4000)
    try:
        speak_cap = max(200, min(50000, int(float(T.normalize_digits(speak_target)))))
    except Exception:
        speak_cap = 4000
    secretish = _looks_secretish(text) >= 1
    allowed_force = str(params.get("force") or "").strip().lower() in ("yes", "نعم", "true", "1")
    if mode in ("speak", "both") and secretish and not allowed_force:
        mode = "copy" if mode == "speak" else "copy"
        head += "\n  ⚠️ النص فيه ما يشبه مفتاحًا سرّيًا — لن أنطقه بصوت عالٍ (force=yes إن كنت وحدك)."
    lines = [head]
    if mode in ("copy", "both"):
        copied = False
        try:
            import pyperclip  # noqa: PLC0415
            pyperclip.copy(text)
            copied = True
        except ModuleNotFoundError:
            lines.append("  · النسخ يحتاج pyperclip (في متطلبات مشروعك): python setup.py")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  · تعذّر النسخ ({type(exc).__name__}).")
        if copied:
            lines.append("  ✔ النص كله في الحافظة الآن.")
    if mode in ("speak", "both"):
        to_speak = text[:speak_cap]
        ok_voice, voice_note = T.call_plugin("voice_reader", {"action": "say", "text": to_speak}, timeout_s=20)
        if ok_voice:
            lines.append(f"  🔊 {voice_note.splitlines()[0][:90]}")
            if len(text) > speak_cap:
                lines.append(f"  · نصّ الشاشة أطول من حد النطق ({speak_cap} حرفًا) — أقرأ أوله.")
        else:
            lines.append(f"  · إضافة voice_reader غير متاحة ({voice_note[:70]}) — النص جاهز مكتوبًا بالأسفل.")
    if mode == "text" or mode in ("speak", "both", "copy"):
        preview = text[:600] + ("…" if len(text) > 600 else "")
        lines.append("\n── النص المقروء ──\n" + preview)
    T.log_event("screen_reader", "read", "ok",
                detail=f"mode={mode} · words={words}")
    return "\n".join(lines)


def _act_read(params: dict, player: Any) -> str:
    return _do_read(params, player)


def _act_copy(params: dict, player: Any) -> str:
    return _do_read(params, player, forced_mode="copy")


def _act_status(params: dict, player: Any) -> str:
    del params, player
    def has(module: str) -> str:
        try:
            return "✔" if importlib.util.find_spec(module) is not None else "✗"
        except Exception:
            return "؟"
    extras = _extras()
    last = ""
    if extras is not None:
        png, txt = extras / _LAST_PNG, extras / _LAST_TXT
        if png.exists():
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(png.stat().st_mtime))
            words = len(re.findall(r"\S+", txt.read_text(encoding="utf-8"))) if txt.exists() else 0
            last = f"آخر لقطة: {stamp} · {words} كلمة · {T.human_bytes(png.stat().st_size)}"
    return ("سيدي، حالة قراءة الشاشة:\n"
            f"  التصوير: mss {has('mss')} · Pillow {has('PIL')} · pyautogui {has('pyautogui')} · مصوّر ocr {has('pytesseract')}\n"
            f"  القراءة: pytesseract {has('pytesseract')} · إضافة ocr {'✔' if T and T.sibling_module('ocr.py', 'ocr_gate') else '✗'} · "
            f"النطق: إضافة voice_reader {'✔' if T and T.sibling_module('voice_reader.py', 'voice_gate') else '✗'}\n"
            f"  اللغات: {_cfg().get('languages')} · محرّك اللقطة: {_cfg().get('capture_engine')}\n"
            f"  {last or 'لم ألتقط شيئًا بعد.'}\n"
            "  كل شيء يجري على جهازك: لا صورة ولا نصّ يُرسل لأي خدمة.")


def _act_help(params: dict, player: Any) -> str:
    del params, player
    return ("سيدي، قراءة الشاشة:\n"
            "  • read             — لقطة ← قراءة ← نطق (mode=speak افتراضيًا)\n"
            "  • read mode=copy   — نفس الشيء لكن في الحافظة بدل الصوت (أسرع)\n"
            "  • mode=both · mode=text\n"
            "  • region=\"x,y,عرض,ارتفاع\" لقصّ منطقة، و monitor=2 للشاشة الثانية\n"
            "  • languages=ara+eng (الافتراضي) · force=yes لنطق نص فيه مفاتيح (حماية افتراضية)\n"
            "  • status · help\n"
            "  ملاحظة: يحتاج محرّك OCR — pytesseract مع برنامج Tesseract، أو إضافة ocr عندك.")


_ACTIONS = {"read": _act_read, "copy": _act_copy, "status": _act_status, "help": _act_help}


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    """ينفّذ فعلًا واحدًا. لا يرمي أبدًا."""
    del session_memory
    try:
        params = parameters or {}
        action = str(params.get("action") or "read").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return "سيدي، هذا ليس فعلًا من أفعال قراءة الشاشة — المتاح: " + " | ".join(sorted(_ACTIONS))
        return handler(params, player)
    except Exception as exc:  # noqa: BLE001
        if T is not None:
            T.log(player, f"screen_reader error: {exc}")
        return f"سيدي، حدث خطأ غير متوقع في قراءة الشاشة ({type(exc).__name__})."


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import shutil
    import tempfile
    import types

    print("screen_reader — فحص ذاتي")
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

    tmp = Path(tempfile.mkdtemp(prefix="screen_test_"))
    original_extras = T.extras_dir
    T.extras_dir = lambda: tmp / "extras"            # type: ignore[assignment]

    # عزل الإعدادات: الفحص لا يلمس مخزن إعداداتك الحقيقي أبدًا
    _orig_cfg, _orig_store, _orig_log = T.cfg, T.store_settings, T.log_event
    _store: dict[str, dict] = {}
    T.cfg = lambda ns, fields: {**T.defaults(fields), **_store.get(ns, {})}       # type: ignore[assignment]
    T.store_settings = lambda ns, values: (_store.setdefault(ns, {}).update(values), True)[1]  # type: ignore[assignment]
    T.log_event = lambda *a, **k: None                                            # type: ignore[assignment]
    original_capture, original_ocr = _capture, ocr_image
    calls: dict[str, Any] = {"plugin": [], "clipboard": [], "spoken": []}

    def fake_capture_factory(ok: bool):
        def fake(target: Path, region, monitor):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG" + b"0" * 500)
            return (True, "") if ok else (False, "mss: لا شاشة في هذه البيئة | pillow: كذلك")
        return fake

    def fake_ocr_factory(text: str):
        return lambda path, languages: (text, "")
    try:
        # ١) قصّ المنطقة
        region, why = parse_region("100,200,300,400")
        check("المنطقة تُقرأ", region == (100, 200, 300, 400) and not why, str(region))
        check("المنطقة بأرقام عربية تُقرأ", parse_region("١٠,٢٠,٣٠,٤٠")[0] == (10, 20, 30, 40))
        check("المنطقة بلا أربعة أرقام تُرفض", parse_region("10,20,30")[1] != "")
        check("منطقة بأبعاد مستحيلة تُرفض", parse_region("0,0,99999,10")[1] != "")
        check("بلا منطقة = الشاشة كاملة", parse_region("") == (None, ""))

        # ٢) الوضع الافتراضي: لقطة ← قراءة ← نطق عبر voice_reader
        globals()["_capture"] = fake_capture_factory(True)          # type: ignore[assignment]
        globals()["ocr_image"] = fake_ocr_factory("صفحة فيها نص عربي واضح للقراءة.")   # type: ignore[assignment]
        original_tools_call = T.call_plugin

        def fake_call(name, parameters=None, timeout_s=25.0):
            calls["plugin"].append((name, dict(parameters or {})))
            if name == "voice_reader":
                calls["spoken"].append((parameters or {}).get("text", ""))
                return True, "سيدي، أقرأ الآن بـمصري رجالي (1 قطعة)."
            return False, "غير موجودة"
        T.call_plugin = fake_call                             # type: ignore[assignment]
        result = _act_read({}, None)
        check("القراءة الافتراضية تنطق", "قرأت" in result and "🔊" in result, result[:120])
        check("النص أُرسل للنطق كاملًا", calls["spoken"] and "نص عربي واضح" in calls["spoken"][0], str(calls["spoken"]))
        check("آخر لقطة محفوظة في ملف واحد", (T.extras_dir() / "screen_last.png").exists())
        check("آخر نص محفوظ", "نص عربي" in (T.extras_dir() / "screen_last.txt").read_text(encoding="utf-8"))
        check("الملفان فقط — بلا تراكم", len(list((T.extras_dir()).glob("screen_last*"))) == 2)

        # ٣) وضع النسخ: الحافظة بدل الصوت
        original_pyperclip = sys.modules.get("pyperclip")
        fake_pyperclip = types.ModuleType("pyperclip")
        fake_pyperclip.copy = lambda text: calls["clipboard"].append(text)
        fake_pyperclip.paste = lambda: ""
        sys.modules["pyperclip"] = fake_pyperclip
        calls["spoken"].clear()
        copied = _act_copy({}, None)
        check("وضع النسخ يملأ الحافظة", calls["clipboard"] and "نص عربي" in calls["clipboard"][-1], str(calls["clipboard"])[:80])
        check("ولا ينطق شيئًا", not calls["spoken"])
        check("يذكر أن النص في الحافظة", "الحافظة" in copied, copied[:100])

        # ٤) حماية الأسرار: لا نطق لنص فيه مفتاح
        globals()["ocr_image"] = fake_ocr_factory(   # type: ignore[assignment]
            "token=ghp_abcdefghijklmnopqrstuvwxyz012345 and key=sk-abcdefghijklmnopqrstuvwxyz")
        calls["spoken"].clear()
        guarded = _act_read({"mode": "speak"}, None)
        check("المفتاح المكشوف يُمنع من النطق", "لن أنطقه" in guarded or "مفتاحًا" in guarded, guarded[:140])
        check("ولم يُرسل شيء للنطق", not calls["spoken"], str(calls["spoken"]))
        forced = _act_read({"mode": "speak", "force": "yes"}, None)
        check("وبالأمر الصريح يقرأ", bool(calls["spoken"]), forced[:100])

        # ٥) حدود النطق
        long_text = ("جملة طويلة للاختبار. " * 400)
        globals()["ocr_image"] = fake_ocr_factory(long_text)   # type: ignore[assignment]
        calls["spoken"].clear()
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"speak_limit": "300"})
        limited = _act_read({"mode": "speak"}, None)
        check("حد النطق يُحترم", calls["spoken"] and len(calls["spoken"][0]) <= 300, str(len(calls["spoken"][0]) if calls["spoken"] else 0))
        check("ويُذكر أن النص أطول", "أطول من حد النطق" in limited, limited[:120])

        # ٦) الفشل بلطف: بلا شاشة · بلا نص · بلا OCR
        globals()["_capture"] = fake_capture_factory(False)     # type: ignore[assignment]
        failed = _act_read({}, None)
        check("فشل اللقطة يُشرح بصراحة", "لم أستطع أخذ لقطة" in failed and "شاشة" in failed, failed[:120])
        globals()["_capture"] = fake_capture_factory(True)      # type: ignore[assignment]
        globals()["ocr_image"] = fake_ocr_factory("")           # type: ignore[assignment]
        empty = _act_read({}, None)
        check("لقطة بلا نص تُقال بصراحة", "لم أجد نصًّا" in empty, empty[:110])
        globals()["ocr_image"] = original_ocr                   # type: ignore[assignment]
        text, ocr_why = ocr_image(T.extras_dir() / "screen_last.png", "ara+eng")
        check("سلسلة OCR الحقيقية تعطي رسالة واضحة بلا محرّك",
              (not text.strip()) and ocr_why.strip() != "", ocr_why[:120])

        # ٧) الوضع غير الصحيح والواجهة
        check("وضع مجهول يُرفض", "الوضع يقبل" in _do_read({"mode": "shout"}, None))
        check("الحالة تعرض المحرّكات", "pytesseract" in _act_status({}, None) and "جهازك" in _act_status({}, None))
        check("فعل مجهول يُرفض بلطف", "ليس فعلًا" in run({"action": "paint"}))
        check("run بلا معاملات لا ينهار", isinstance(run({}), str))
        check("help يشرح region", "region" in _act_help({}, None))
        T.call_plugin = original_tools_call                     # type: ignore[assignment]
        if original_pyperclip is not None:
            sys.modules["pyperclip"] = original_pyperclip
        else:
            sys.modules.pop("pyperclip", None)
    finally:
        globals()["_capture"] = original_capture               # type: ignore[assignment]
        globals()["ocr_image"] = original_ocr                  # type: ignore[assignment]
        T.extras_dir = original_extras                         # type: ignore[assignment]
        T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
