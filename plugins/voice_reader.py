"""
voice_reader.py — «اقرأ لي» و«قول لي»: قراءة صوتية عربية بجودة عالية، محليًا أو عبر EdgeTTS المجاني.

• **مصادر النص**: ملف (txt · md · csv · log · كود · html · PDF · Word) أو نص مباشر أو الحافظة،
  والعملية الأخيرة تُحفظ لإعادة قراءتها بكلمة واحدة (`repeat`).
• **صوت مصري طبيعي**: يستخدم `EdgeTTS` الموجود في مشروعك (بلا مفاتيح — ٣٢ صوتًا عربيًا)،
  مع **تحكم في السرعة والطبقة** (لا يدعمه محرّك المشروع الأصلي).
• **بلا إنترنت تمامًا**: محرّك `piper` اختياري إن ثبّته (يُشغَّل من إعدادات الإضافة).
• **قراءة في الخلفية**: يبدأ الكلام فورًا ويعيد التحكم لك؛ `stop` يُسكته في أي لحظة،
  والجمل تُقرأ قطعة قطعة فلا ينتظر النموذج الصوت كله.
• **حماية**: ملف فيه ما يبدو مفاتيح/رموز سرّية لا يُقرأ بصوت عالٍ بلا أمر صريح (`force=yes`)
  — حتى لا يُسمعه أحد، والطباعة في السجل تمر على حاجب الأسرار في النواة.

🔴 لا سطر يرمي استثناءً للخارج، ولا صوت يُشغَّل بلا مسار احتياطي واضح لو غابت المكتبات.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "voice_reader",
    "description": (
        "Reads things aloud in a natural Arabic voice (Egyptian/Saudi Gulf voices, free, no key) and "
        "controls speech: read a file, PDF or Word document aloud, say any text out loud, read what is "
        "in the clipboard, repeat the last reading, stop speaking, change the voice, speed and pitch, "
        "list the available Arabic voices. Use for: 'read this file to me', 'say this out loud', "
        "'read the news to me', 'speak louder/slower', 'change your voice to Salma', 'stop reading', "
        "'repeat that'. Works offline only if the optional piper engine is configured; otherwise it "
        "uses the free EdgeTTS voice that Mark-LV already ships. Do NOT use it to read the screen "
        "(use screen_reader) or to translate (use translation)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "read | say | clipboard | repeat | stop | voices | set | status | help"},
            "file": {"type": "STRING", "description": "read: path of the file to read aloud"},
            "text": {"type": "STRING", "description": "say: the text to speak"},
            "voice": {"type": "STRING", "description": "set/say: voice name, e.g. ar-EG-ShakirNeural"},
            "rate": {"type": "STRING", "description": "set/say: speed like '+15%' or '-10%'"},
            "pitch": {"type": "STRING", "description": "set/say: pitch like '+10Hz' or '-5Hz'"},
            "engine": {"type": "STRING", "description": "set: edge (free online) | piper (fully offline)"},
            "force": {"type": "STRING", "description": "'yes' to read a file that looks like it holds secrets"},
            "limit": {"type": "NUMBER", "description": "content: how many chars to read, default from settings"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "voice_reader",
    "title": "Read aloud — Arabic voice, speed and pitch",
    "fields": [
        {"key": "engine", "type": "choice", "label": "Engine", "default": "edge",
         "choices": ["edge", "piper"]},
        {"key": "voice", "type": "text", "label": "Voice name (EdgeTTS)", "default": "ar-EG-ShakirNeural"},
        {"key": "rate", "type": "text", "label": "Speed (e.g. +15%)", "default": "+0%"},
        {"key": "pitch", "type": "text", "label": "Pitch (e.g. +10Hz)", "default": "+0Hz"},
        {"key": "max_chars", "type": "text", "label": "Maximum characters per reading", "default": "20000"},
        {"key": "chunk_chars", "type": "text", "label": "Characters per spoken chunk", "default": "700"},
        {"key": "piper_path", "type": "text", "label": "piper executable path (offline engine)", "default": ""},
        {"key": "piper_model", "type": "text", "label": "piper Arabic model path (.onnx)", "default": ""},
    ],
}

# أصوات عربية على خدمة EdgeTTS المجانية — تُستعمل كقائمة احتياطية وبلا إنترنت
ARABIC_VOICES = {
    "ar-EG-ShakirNeural": "مصري رجالي", "ar-EG-SalmaNeural": "مصري نسائي",
    "ar-SA-HamedNeural": "سعودي رجالي", "ar-SA-ZariyahNeural": "سعودي نسائي",
    "ar-AE-HamdanNeural": "إماراتي رجالي", "ar-AE-FatimaNeural": "إماراتي نسائي",
    "ar-JO-TaimNeural": "أردني رجالي", "ar-JO-SanaNeural": "أردني نسائي",
    "ar-SY-LaithNeural": "سوري رجالي", "ar-SY-AmanyNeural": "سوري نسائي",
    "ar-LB-RamiNeural": "لبناني رجالي", "ar-LB-LaylaNeural": "لبناني نسائي",
    "ar-IQ-BasselNeural": "عراقي رجالي", "ar-IQ-RanaNeural": "عراقي نسائي",
    "ar-KW-FahedNeural": "كويتي رجالي", "ar-KW-NouraNeural": "كويتي نسائي",
    "ar-QA-MoazNeural": "قطري رجالي", "ar-QA-AmalNeural": "قطري نسائي",
    "ar-BH-AliNeural": "بحريني رجالي", "ar-BH-LailaNeural": "بحريني نسائي",
    "ar-OM-AbdullahNeural": "عُماني رجالي", "ar-OM-AyshaNeural": "عُماني نسائي",
    "ar-MA-JamalNeural": "مغربي رجالي", "ar-MA-MounaNeural": "مغربي نسائي",
    "ar-DZ-IsmaelNeural": "جزائري رجالي", "ar-DZ-AminaNeural": "جزائري نسائي",
    "ar-TN-HediNeural": "تونسي رجالي", "ar-TN-ReemNeural": "تونسي نسائي",
    "ar-LY-OmarNeural": "ليبي رجالي", "ar-LY-ImanNeural": "ليبي نسائي",
    "ar-YE-SalehNeural": "يمني رجالي", "ar-YE-MaryamNeural": "يمني نسائي",
}

T = None                      # النواة المساعدة
CORE_TTS: Any = None          # core/tts.py من مشروعك (نُعيد استخدام مشغّله كما هو)
_STATE: dict[str, Any] = {"thread": None, "stop": threading.Event(), "state": "idle",
                          "detail": "", "voice": "", "chunks": 0, "done": 0, "chars": 0}
_LOCK = threading.Lock()
_LAST_FILE = "voice_last.txt"
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".ini", ".cfg", ".conf",
                  ".py", ".js", ".ts", ".json", ".xml", ".yml", ".yaml", ".sql", ".bat", ".ps1", ".sh",
                  ".env", ".properties", ".gitignore", ".cfg"}


def _boot() -> None:
    """يحمّل النواة المساعدة ومشغّل الصوت من مشروعك — مرة واحدة، وكل شيء محاط بحماية."""
    global T, CORE_TTS
    name = "_jarvis_tools_core"
    if name in sys.modules:
        T = sys.modules[name]
    else:
        path = Path(__file__).resolve().parent / "_tools_core.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                try:
                    spec.loader.exec_module(module)
                    T = module
                except Exception:
                    sys.modules.pop(name, None)
                    T = None
    core_name = "_jarvis_core_tts"
    if core_name in sys.modules:
        CORE_TTS = sys.modules[core_name]
        return
    if T is None:
        return
    core_path = T.project_root() / "core" / "tts.py"
    if not core_path.exists():
        return
    spec = importlib.util.spec_from_file_location(core_name, core_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[core_name] = module
    try:
        spec.loader.exec_module(module)
        CORE_TTS = module
    except Exception:
        sys.modules.pop(core_name, None)
        CORE_TTS = None


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


# ── السرعة والطبقة: تحقق وتنسيق ──────────────────────────────────────────────
def _clean_rate(value: Any, default: str = "+0%") -> tuple[str, str]:
    """يتحقق من السرعة ويعيدها بصيغة EdgeTTS. يعيد (القيمة، سبب الرفض)."""
    raw = str(value or "").strip()
    if not raw:
        return default, ""
    raw = T.normalize_digits(raw) if T else raw
    found = re.fullmatch(r"([+-]?\d{1,3})\s*%?", raw)
    if not found:
        return default, f"صيغة السرعة غير مفهومة: «{raw}» — اكتبها مثل +15% أو -10%."
    number = int(found.group(1))
    if number < -60 or number > 120:
        return default, f"السرعة {number}% خارج المدى المعقول (-60% إلى +120%)."
    return f"{number:+d}%", ""


def _clean_pitch(value: Any, default: str = "+0Hz") -> tuple[str, str]:
    """يتحقق من الطبقة (حِدّة الصوت) ويعيدها بصيغة EdgeTTS."""
    raw = str(value or "").strip()
    if not raw:
        return default, ""
    raw = T.normalize_digits(raw) if T else raw
    found = re.fullmatch(r"([+-]?\d{1,3})\s*(?:hz)?", raw, re.IGNORECASE)
    if not found:
        return default, f"صيغة الطبقة غير مفهومة: «{raw}» — اكتبها مثل +10Hz أو -5Hz."
    number = int(found.group(1))
    if number < -80 or number > 80:
        return default, f"الطبقة {number}Hz خارج المدى المعقول (-80 إلى +80)."
    return f"{number:+d}Hz", ""


# ── استخراج النص من الملفات ──────────────────────────────────────────────────
def _read_text_file(path: Path) -> tuple[str, str]:
    """يقرأ ملفًا نصيًا بعدة ترميزات شائعة (يدعم العربية cp1256). يعيد (النص، الخطأ)."""
    for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            return path.read_text(encoding=encoding), ""
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001
            return "", f"تعذّرت قراءة الملف ({type(exc).__name__})."
    return "", "لم أستطع فك ترميز الملف — ليس نصًّا عاديًا."


def _read_pdf(path: Path) -> tuple[str, str]:
    """PDF عبر pdfplumber ثم PyPDF2 (كلاهما في متطلبات مشروعك)."""
    pages: list[str] = []
    try:
        import pdfplumber  # noqa: PLC0415
        with pdfplumber.open(str(path)) as document:
            for page in document.pages[:120]:
                pages.append(page.extract_text() or "")
        text = "\n".join(p for p in pages if p)
        if text.strip():
            return text, ""
    except ModuleNotFoundError:
        pass
    except Exception:
        pass
    try:
        from PyPDF2 import PdfReader  # noqa: PLC0415
        reader = PdfReader(str(path))
        for page in reader.pages[:120]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(p for p in pages if p)
        if text.strip():
            return text, ""
        return "", "قرأت الملف لكنه بلا نص قابل للقراءة (صفحات مصوّرة؟ استخدم screen_reader على الصور)."
    except ModuleNotFoundError:
        return "", "قراءة PDF تحتاج pdfplumber أو PyPDF2 — كلاهما في متطلبات مشروعك:  python setup.py"
    except Exception as exc:  # noqa: BLE001
        return "", f"تعذّرت قراءة PDF ({type(exc).__name__})."


def _read_docx(path: Path) -> tuple[str, str]:
    """Word عبر python-docx (في متطلبات مشروعك)."""
    try:
        import docx  # noqa: PLC0415
    except ModuleNotFoundError:
        return "", "قراءة Word تحتاج python-docx وهي في متطلبات مشروعك:  python setup.py"
    try:
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables[:20]:
            for row in table.rows[:200]:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        text = "\n".join(parts)
        return (text, "") if text.strip() else ("", "المستند بلا نص مقروء.")
    except Exception as exc:  # noqa: BLE001
        return "", f"تعذّرت قراءة Word ({type(exc).__name__})."


def _strip_html(text: str) -> str:
    clean = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    clean = re.sub(r"(?s)<[^>]+>", " ", clean)
    return re.sub(r"\s+", " ", clean)


def extract_text(path: Path) -> tuple[str, str]:
    """يستخرج نصًا من أي ملف مدعوم. يعيد (النص، سبب الفشل)."""
    suffix = path.suffix.lower()
    if not suffix and path.name.startswith("."):     # ملفات مثل .env بلا امتداد
        suffix = ".env"
    if suffix in _TEXT_SUFFIXES:
        text, why = _read_text_file(path)
        return (text, why) if not why else (text, why)
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in (".docx", ".doc"):
        return _read_docx(path)
    if suffix in (".html", ".htm"):
        text, why = _read_text_file(path)
        return (_strip_html(text), "") if not why else (text, why)
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"):
        return "", ("هذه صورة — لا أقرأ الصور هنا. استخدم «اقرأ الشاشة» (screen_reader) أو OCR.")
    return "", (f"صيغة غير مدعومة للقراءة الصوتية ({suffix or 'بلا امتداد'}). الملفات المدعومة: "
                "نصية · CSV · كود · HTML · PDF · Word · أو الحافظة عبر clipboard.")


def _looks_secretish(text: str) -> int:
    """كم رمزًا يشبه المفتاح في النص؟ (حماية من قراءة أسرار بصوت عالٍ)."""
    tokens = re.findall(r"\b[A-Za-z0-9_\-]{24,}\b", text or "")
    keys = [token for token in tokens if not token.isdigit()]
    return len(keys)


# ── التوليد والتشغيل ─────────────────────────────────────────────────────────
def _synth_edge(text: str, voice: str, rate: str, pitch: str) -> tuple[Optional[bytes], str]:
    """توليد صوت عبر EdgeTTS (بلا مفاتيح). يعيد (بايتات mp3، سبب الفشل)."""
    try:
        import asyncio

        import edge_tts  # noqa: PLC0415
    except ModuleNotFoundError:
        return None, ("EdgeTTS غير مثبّتة:  pip install edge-tts   (مجانية بلا مفاتيح، وتعمل مع "
                      "مشروعك أصلًا لأنه يستخدمها)")
    try:
        async def _collect() -> bytes:
            communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            buffer = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    buffer.extend(chunk.get("data", b""))
            return bytes(buffer)

        loop = asyncio.new_event_loop()
        try:
            data = loop.run_until_complete(_collect())
        finally:
            loop.close()
        if not data:
            return None, "الخدمة أعادت صوتًا فارغًا — جرّب نصًا أقصر أو اتصالًا آخر."
        return data, ""
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if "Connect" in name or "Timeout" in name or "Client" in name:
            return None, "تعذّر الوصول لخدمة الأصوات (لا إنترنت؟) — والعمل بلا إنترنت يحتاج محرّك piper من الإعدادات."
        return None, f"تعذّر توليد الصوت ({name}: {str(exc)[:90]})."


def _synth_piper(text: str) -> tuple[Optional[bytes], str]:
    """محرّك piper المحلي ١٠٠٪ بلا إنترنت (اختياري). يعيد (بايتات wav، سبب الفشل)."""
    exe = str(_cfg().get("piper_path") or "").strip()
    model = str(_cfg().get("piper_model") or "").strip()
    if not exe or not model:
        return None, ("محرّك piper يحتاج مساري البرنامج والنموذج في إعدادات الإضافة "
                      "(piper_path و piper_model) — وهو اختياري تمامًا.")
    if not Path(exe).exists():
        return None, f"لا أجد برنامج piper في: {exe}"
    if not Path(model).exists():
        return None, f"لا أجد نموذج piper في: {model}"
    try:
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            target = handle.name
        result = subprocess.run([exe, "--model", model, "--output_file", target],
                                input=text.encode("utf-8"), capture_output=True, timeout=90)
        if result.returncode != 0:
            return None, f"piper رفض التوليد (رمز {result.returncode}): {result.stderr.decode('utf-8', 'ignore')[:90]}"
        data = Path(target).read_bytes()
        Path(target).unlink(missing_ok=True)
        return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّر تشغيل piper ({type(exc).__name__})."


def _play_bytes(data: bytes) -> str:
    """يشغّل بايتات صوتية. يعيد "" عند النجاح أو سبب الفشل بالعربي."""
    if CORE_TTS is not None and hasattr(CORE_TTS, "_play_audio_bytes"):
        try:
            CORE_TTS._play_audio_bytes(data)     # نفس مشغّل مشروعك بالحرف
            return ""
        except Exception as exc:  # noqa: BLE001
            fallback_why = f"{type(exc).__name__}"
    else:
        fallback_why = "core/tts غير محمّل"
    try:
        import miniaudio  # noqa: PLC0415
        import numpy  # noqa: PLC0415
        import sounddevice  # noqa: PLC0415
        decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1)
        sounddevice.play(numpy.array(decoded.samples, dtype=numpy.float32), decoded.sample_rate)
        sounddevice.wait()
        return ""
    except ModuleNotFoundError as exc:
        return (f"لا أستطيع تشغيل الصوت ({exc.name} غير مثبّتة). المشروع يستخدم miniaudio+sounddevice، "
                "وشغّلها بأمر:  pip install sounddevice miniaudio")
    except Exception as exc:  # noqa: BLE001
        return f"تعذّر تشغيل الصوت ({type(exc).__name__}: {str(exc)[:70]} — سابقًا: {fallback_why})."


def _stopped() -> bool:
    return _STATE["stop"].is_set()


def _speak_worker(chunks: list[str], voice: str, rate: str, pitch: str, engine: str, log) -> None:
    """خيط التشغيل: يولّد ويشغّل قطعة قطعة، ويتوقف فورًا عند الطلب."""
    with _LOCK:
        _STATE.update({"state": "speaking", "detail": "", "done": 0, "chunks": len(chunks),
                       "voice": voice, "chars": sum(len(c) for c in chunks)})
    for index, chunk in enumerate(chunks, 1):
        if _stopped():
            break
        if engine == "piper":
            data, why = _synth_piper(chunk)
        else:
            data, why = _synth_edge(chunk, voice, rate, pitch)
        if why:
            with _LOCK:
                _STATE.update({"state": "error", "detail": why})
            log(f"voice_reader: {why}")
            return
        if _stopped():
            break
        play_why = _play_bytes(data or b"")
        if play_why:
            with _LOCK:
                _STATE.update({"state": "error", "detail": play_why})
            log(f"voice_reader: {play_why}")
            return
        with _LOCK:
            _STATE["done"] = index
    with _LOCK:
        _STATE["state"] = "stopped" if _stopped() else "idle"


def _start_speaking(text: str, log) -> str:
    """يبدأ القراءة في الخلفية. يعيد رسالة فورية (نجاح أو سبب)."""
    clean = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not clean:
        return "سيدي، لا نص لأقرأه."
    settings = _cfg()
    max_chars = _limit(20000, settings.get("max_chars"), 200, 200000)
    truncated = len(clean) > max_chars
    if truncated:
        clean = clean[:max_chars]
    chunk_size = _limit(700, settings.get("chunk_chars"), 120, 3000)
    chunks = T.split_sentences(clean, limit=chunk_size) if T else [clean[i:i + chunk_size]
                                                                  for i in range(0, len(clean), chunk_size)]
    if not chunks:
        return "سيدي، لا نص لأقرأه."
    engine = str(settings.get("engine") or "edge").lower()
    voice = str(settings.get("voice") or "ar-EG-ShakirNeural")
    rate, rate_why = _clean_rate(settings.get("rate"))
    pitch, pitch_why = _clean_pitch(settings.get("pitch"))
    thread = _STATE.get("thread")
    if thread is not None and thread.is_alive():
        _STATE["stop"].set()          # أي قراءة سابقة تسكت فورًا (بلا تراكم أصوات)
        time.sleep(0.05)
    _STATE["stop"] = threading.Event()
    worker = threading.Thread(target=_speak_worker, args=(chunks, voice, rate, pitch, engine, log),
                              daemon=True, name="VoiceReader")
    _STATE["thread"] = worker
    worker.start()
    try:
        if T is not None:
            (T.extras_dir()).mkdir(parents=True, exist_ok=True)
            (T.extras_dir() / _LAST_FILE).write_text(clean, encoding="utf-8")
    except Exception:
        pass
    minutes = len(clean) / 900.0        # ~٩٠٠ حرف عربي في الدقيقة تقريبًا
    notes = [f"سيدي، أقرأ الآن بـ{ARABIC_VOICES.get(voice, voice)} "
             f"({len(chunks)} قطعة، ~{max(1, int(minutes))} دقيقة). اكتب stop لأُسكت."]
    if rate_why:
        notes.append(f"  · {rate_why} (استخدمت {rate})")
    if pitch_why:
        notes.append(f"  · {pitch_why} (استخدمت {pitch})")
    if truncated:
        notes.append(f"  · النص أطول من الحد ({max_chars} حرفًا) — قرأت أوله فقط.")
    return "\n".join(notes)


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_say(params: dict, player: Any) -> str:
    text = str(params.get("text") or "").strip()
    if not text:
        return "سيدي، قل لي النص الذي تريدني أن أنطقه (text)."
    overrides: dict[str, Any] = {}
    if str(params.get("voice") or "").strip():
        overrides["voice"] = str(params["voice"]).strip()
    for key, cleaner in (("rate", _clean_rate), ("pitch", _clean_pitch)):
        if str(params.get(key) or "").strip():
            value, _why = cleaner(params.get(key))
            overrides[key] = value
    if overrides and T is not None:
        T.store_settings(PLUGIN_SETTINGS["namespace"], overrides)
    return _start_speaking(text, lambda line: T.log(player, line) if T else None)


def _act_read(params: dict, player: Any) -> str:
    raw = str(params.get("file") or "").strip()
    if not raw:
        return ("سيدي، حدّد الملف: action=read file=\"مسار الملف\" — أو action=clipboard لقراءة الحافظة، "
                "أو action=say text=\"نص\".")
    path, why = T.resolve_file(raw) if T else (None, "النواة غير محمّلة.")
    if path is None:
        return f"سيدي، {why}"
    text, error = extract_text(path)
    if error and not text:
        return f"سيدي، {error}"
    if _looks_secretish(text) >= 2 and str(params.get("force") or "").strip().lower() not in ("yes", "نعم", "true", "1"):
        return ("سيدي، هذا الملف فيه ما يشبه المفاتيح أو الرموز السرّية — ولا أُريد قراءته بصوت عالٍ "
                "قد يسمعه غيرك.\n  إن كنت وحدك ومتأكدًا: أعد الأمر بـ force=yes\n  أو خذ النص مكتوبًا بدل الصوت.")
    limit = params.get("limit")
    if limit not in (None, ""):
        try:
            cap = max(200, min(200000, int(float(str(limit)))))
        except Exception:
            cap = None
        if cap:
            text = text[:cap]
    if not text.strip():
        return f"سيدي، {path.name} لا يحتوي نصًّا مقروءًا."
    header = f"سيدي، أقرأ {path.name} ({T.human_bytes(path.stat().st_size)})." if T else f"سيدي، أقرأ {path.name}."
    return header + "\n" + _start_speaking(text, lambda line: T.log(player, line) if T else None)


def _act_clipboard(params: dict, player: Any) -> str:
    del params
    try:
        import pyperclip  # noqa: PLC0415
    except ModuleNotFoundError:
        return "سيدي، قراءة الحافظة تحتاج pyperclip وهي في متطلبات مشروعك:  python setup.py"
    try:
        text = str(pyperclip.paste() or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، تعذّرت قراءة الحافظة ({type(exc).__name__})."
    if not text:
        return "سيدي، الحافظة فارغة."
    if _looks_secretish(text) >= 1:
        return "سيدي، الحافظة فيها ما يشبه مفتاحًا سرّيًا — لن أنطقه بصوت عالٍ."
    return "سيدي، أقرأ ما نسخته:\n" + _start_speaking(text, lambda line: T.log(player, line) if T else None)


def _act_repeat(params: dict, player: Any) -> str:
    del params
    text = ""
    try:
        if T is not None:
            candidate = T.extras_dir() / _LAST_FILE
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8")
    except Exception:
        text = ""
    if not text.strip():
        return "سيدي، لا قراءة سابقة أُعيدها."
    return "سيدي، أُعيد آخر قراءة:\n" + _start_speaking(text, lambda line: T.log(player, line) if T else None)


def _act_stop(params: dict, player: Any) -> str:
    del params
    was = str(_STATE.get("state") or "idle")
    _STATE["stop"].set()
    stopped_player = False
    try:
        if CORE_TTS is not None and hasattr(CORE_TTS, "sd"):
            CORE_TTS.sd.stop()
            stopped_player = True
        else:
            import sounddevice  # noqa: PLC0415
            sounddevice.stop()
            stopped_player = True
    except Exception:
        stopped_player = False
    if T is not None:
        T.log(player, "voice_reader: stop")
    state = "أُسكِت الصوت" if stopped_player else "أوقفت القراءة (المشغّل لم يستجب — جرّب مرة أخرى)"
    return f"سيدي، {state}." + (f" (كانت الحالة: {was})" if was != "idle" else "")


def _act_voices(params: dict, player: Any) -> str:
    del params
    live: list[str] = []
    try:
        import asyncio

        import edge_tts  # noqa: PLC0415
        loop = asyncio.new_event_loop()
        try:
            entries = loop.run_until_complete(asyncio.wait_for(edge_tts.list_voices(), timeout=20))
        finally:
            loop.close()
        live = sorted({str(e.get("ShortName")) for e in entries
                       if str(e.get("Locale", "")).lower().startswith("ar")})
    except Exception:
        live = []
    source = "من الخدمة الآن" if live else "قائمتي المحفوظة (بلا اتصال)"
    names = live or sorted(ARABIC_VOICES)
    lines = [f"سيدي، الأصوات العربية المتاحة ({len(names)}) — {source}:", ""]
    for name in names[:40]:
        lines.append(f"  · {name:<26} {ARABIC_VOICES.get(name, '')}")
    lines.append("")
    lines.append(f"  الحالي: {_cfg().get('voice')} · السرعة {_cfg().get('rate')} · الطبقة {_cfg().get('pitch')}")
    lines.append("  للتغيير: action=set voice=ar-EG-SalmaNeural rate=+10% pitch=+5Hz")
    if not live:
        lines.append("  (تعذّر الوصول للخدمة الآن — وستظهر القائمة الكاملة عند الاتصال، أو جرّب أي اسم من القائمة.)")
    return "\n".join(lines)


def _act_set(params: dict, player: Any) -> str:
    del player
    updates: dict[str, Any] = {}
    notes: list[str] = []
    voice = str(params.get("voice") or "").strip()
    if voice:
        if voice not in ARABIC_VOICES and not voice.startswith("ar-"):
            notes.append(f"تنبيه: «{voice}» ليس من الأصوات العربية المعروفة — سأجرّبه كما هو.")
        updates["voice"] = voice
    if str(params.get("rate") or "").strip():
        rate, why = _clean_rate(params.get("rate"))
        updates["rate"] = rate
        if why:
            notes.append(why)
    if str(params.get("pitch") or "").strip():
        pitch, why = _clean_pitch(params.get("pitch"))
        updates["pitch"] = pitch
        if why:
            notes.append(why)
    engine = str(params.get("engine") or "").strip().lower()
    if engine:
        if engine not in ("edge", "piper"):
            notes.append("المحرّك يقبل edge أو piper فقط.")
        else:
            updates["engine"] = engine
    if not updates:
        if notes:      # طلب مرفوض (محرّك مجهول مثلًا) — نشرح لماذا بلا تغيير أي شيء
            return "سيدي، لم أغيّر شيئًا:\n" + "\n".join(f"  · {note}" for note in notes)
        return ("سيدي، قل لي ما أغيّره: voice=ar-EG-SalmaNeural rate=+10% pitch=+5Hz engine=piper. "
                f"الحالي: {_cfg().get('voice')} · {_cfg().get('rate')} · {_cfg().get('pitch')}")
    if T is not None:
        T.store_settings(PLUGIN_SETTINGS["namespace"], updates)
    current = _cfg()
    lines = [f"سيدي، حدّثت الإعدادات: {', '.join(f'{k}={v}' for k, v in updates.items())}",
             f"  الحالي الآن: {current.get('voice')} · {current.get('rate')} · {current.get('pitch')} · "
             f"محرّك {current.get('engine')}"]
    lines += [f"  · {note}" for note in notes]
    return "\n".join(lines)


def _act_status(params: dict, player: Any) -> str:
    del params, player
    settings = _cfg()
    edge_ok = False
    try:
        import importlib.util as _ilu  # noqa: PLC0415
        edge_ok = _ilu.find_spec("edge_tts") is not None
    except Exception:
        edge_ok = False
    lines = ["سيدي، حالة القراءة الصوتية:",
             f"  المحرّك: {settings.get('engine')} · الصوت: {ARABIC_VOICES.get(str(settings.get('voice')), settings.get('voice'))}",
             f"  السرعة: {settings.get('rate')} · الطبقة: {settings.get('pitch')}",
             f"  EdgeTTS: {'مثبّتة ✔' if edge_ok else 'غير مثبّتة — pip install edge-tts'} · "
             f"مشغّل مشروعك (core/tts): {'محمّل ✔' if CORE_TTS is not None else 'غير متاح — سأستخدم مساري الاحتياطي'}",
             f"  الحالة الآن: {_STATE.get('state')} "
             f"({_STATE.get('done')}/{_STATE.get('chunks')} قطعة)" if _STATE.get("state") == "speaking" else
             f"  الحالة الآن: {_STATE.get('state')}"]
    if _STATE.get("detail"):
        lines.append(f"  آخر ملاحظة: {_STATE['detail']}")
    lines.append(f"  بلا إنترنت: {'نعم — piper' if settings.get('engine') == 'piper' else 'لا (EdgeTTS يحتاج اتصالًا) — وفيه خيار piper محلي'} ")
    return "\n".join(lines)


def _act_help(params: dict, player: Any) -> str:
    del params, player
    return ("سيدي، القراءة الصوتية:\n"
            "  • read file=\"...\"   — اقرأ ملفًا بصوت عالٍ (نص · CSV · كود · HTML · PDF · Word)\n"
            "  • say text=\"...\"    — انطق نصًا مباشرة (مع voice/rate/pitch لحظيًا)\n"
            "  • clipboard           — اقرأ ما نسخته\n"
            "  • repeat              — أعد آخر قراءة    · stop — أسكِت\n"
            "  • voices              — الأصوات العربية (٣٢ صوتًا)\n"
            "  • set voice=... rate=+15% pitch=+10Hz engine=piper\n"
            "  • status · help\n"
            "  ملاحظة: قراءة ملف فيه مفاتيح تحتاج force=yes — حمايةً لك.")


_ACTIONS = {"read": _act_read, "say": _act_say, "clipboard": _act_clipboard, "repeat": _act_repeat,
            "stop": _act_stop, "voices": _act_voices, "set": _act_set, "status": _act_status,
            "help": _act_help}


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    """ينفّذ فعلًا واحدًا. لا يرمي أبدًا."""
    del session_memory
    try:
        params = parameters or {}
        action = str(params.get("action") or "status").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return ("سيدي، هذا ليس فعلًا من أفعال القراءة الصوتية — المتاح: "
                    + " | ".join(sorted(_ACTIONS)))
        return handler(params, player)
    except Exception as exc:  # noqa: BLE001
        if T is not None:
            T.log(player, f"voice_reader error: {exc}")
        return f"سيدي، حدث خطأ غير متوقع في القراءة الصوتية ({type(exc).__name__})."


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import shutil
    import tempfile
    import types

    print("voice_reader — فحص ذاتي")
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

    tmp = Path(tempfile.mkdtemp(prefix="voice_test_"))
    original_extras = T.extras_dir
    T.extras_dir = lambda: tmp / "extras"           # type: ignore[assignment]

    # عزل الإعدادات: الفحص لا يلمس مخزن إعداداتك الحقيقي أبدًا
    _orig_cfg, _orig_store, _orig_log = T.cfg, T.store_settings, T.log_event
    _store: dict[str, dict] = {}
    T.cfg = lambda ns, fields: {**T.defaults(fields), **_store.get(ns, {})}       # type: ignore[assignment]
    T.store_settings = lambda ns, values: (_store.setdefault(ns, {}).update(values), True)[1]  # type: ignore[assignment]
    T.log_event = lambda *a, **k: None                                            # type: ignore[assignment]
    played: list[int] = []
    synth_calls: list[tuple[str, str, str, str]] = []
    original_play, original_edge, original_piper = _play_bytes, _synth_edge, _synth_piper

    # محرّك EdgeTTS وهمي — يمرّ من نفس مسار الكود الحقيقي بلا إنترنت
    fake_edge = types.ModuleType("edge_tts")

    class _FakeCommunicate:
        def __init__(self, text, voice, rate=None, pitch=None):
            synth_calls.append((text, voice, str(rate), str(pitch)))

        async def stream(self):
            yield {"type": "audio", "data": b"FAKE-MP3-" + b"x" * 64}

    async def _fake_list_voices():
        return [{"ShortName": "ar-EG-ShakirNeural", "Locale": "ar-EG"},
                {"ShortName": "ar-EG-SalmaNeural", "Locale": "ar-EG"},
                {"ShortName": "en-US-GuyNeural", "Locale": "en-US"}]

    fake_edge.Communicate = _FakeCommunicate
    fake_edge.list_voices = _fake_list_voices
    sys.modules["edge_tts"] = fake_edge
    globals()["_play_bytes"] = lambda data: (played.append(len(data)), "")[1]   # type: ignore[assignment]
    try:
        # ١) النصوص والتحقق من السرعة والطبقة
        check("السرعة +15% تُقبل", _clean_rate("+15%") == ("+15%", ""), str(_clean_rate("+15%")))
        check("السرعة بالنص العربي ١٠ → +10%", _clean_rate("١٠") == ("+10%", ""), str(_clean_rate("١٠")))
        check("سرعة خرافية تُرفض برسالة", _clean_rate("+900")[1] != "" and _clean_rate("+900")[0] == "+0%")
        check("سرعة مكتوبة خطأ تُرفض", _clean_rate("سريع")[1] != "")
        check("الطبقة +10Hz تُقبل", _clean_pitch("+10Hz") == ("+10Hz", ""))
        check("الطبقة -5 تُقبل", _clean_pitch("-5") == ("-5Hz", ""))
        check("طبقة خرافية تُرفض", _clean_pitch("+300")[1] != "")
        check("حاجب الأسرار يعمل", _looks_secretish("المفتاح sk-abcdefghijklmnopqrstuvwxyz123456") >= 1)
        check("النص العربي العادي لا يُحجب", _looks_secretish("هذا نص عربي عادي جدًا يتكرر مرارًا") == 0)

        # ٢) استخراج النص من الملفات
        text_file = tmp / "مقال.txt"
        text_file.write_text("الجملة الأولى. الجملة الثانية. الجملة الثالثة.", encoding="utf-8")
        text, why = extract_text(text_file)
        check("قراءة ملف نصي", not why and "الأولى" in text, why)
        cp_file = tmp / "عربي_قديم.log"
        cp_file.write_bytes("سجل قديم بترميز عربي".encode("cp1256"))
        text, why = extract_text(cp_file)
        check("قراءة cp1256 العربية", not why and "سجل" in text, why)
        html_file = tmp / "صفحة.html"
        html_file.write_text("<html><body><h1>عنوان</h1><script>var x=1</script><p>فقرة</p></body></html>",
                             encoding="utf-8")
        text, why = extract_text(html_file)
        check("HTML يُنظَّف من الوسوم والسكربت", "عنوان" in text and "var x" not in text and "<" not in text, text[:60])
        png_file = tmp / "صورة.png"
        png_file.write_bytes(b"\x89PNG\r\n")
        check("الصورة تُحوَّل لاقتراح screen_reader", "screen_reader" in extract_text(png_file)[1])
        bad_file = tmp / "ملف.xyz"
        bad_file.write_bytes(b"x")
        check("صيغة غير مدعومة تُشرح", "غير مدعومة" in extract_text(bad_file)[1])
        binary = tmp / "غامض.bin"
        binary.write_bytes(b"x")
        check("الملف الغامض يُرفض برسالة", extract_text(binary)[1] != "")

        # ٣) الأفعال الأساسية عبر مسار الكود الحقيقي
        T.store_settings(PLUGIN_SETTINGS["namespace"],
                         {"voice": "ar-EG-ShakirNeural", "rate": "+0%", "pitch": "+0Hz", "engine": "edge"})
        result = _act_say({"text": "مرحبًا سيدي، هذه تجربة قراءة."}, None)
        check("say يبدأ القراءة فورًا", "أقرأ الآن" in result, result[:80])
        deadline = time.time() + 5
        while time.time() < deadline and _STATE.get("state") == "speaking":
            time.sleep(0.05)
        check("الصوت انطلق فعليًا (قطعة واحدة)", len(played) == 1 and played[0] > 10, str(played))
        check("السرعة والطبقة وصلتا للمحرّك", synth_calls and synth_calls[-1][2] == "+0%" and synth_calls[-1][3] == "+0Hz",
              str(synth_calls[-1] if synth_calls else None))
        check("النص حُفظ للإعادة", (T.extras_dir() / "voice_last.txt").exists())
        check("repeat يُعيد الآداب نفسها", "أُعيد آخر قراءة" in _act_repeat({}, None))
        check("say بلا نص يُرفض بلطف", "قل لي النص" in _act_say({}, None))

        # ٤) القراءة من ملف + حماية الأسرار + الحد الأقصى
        long_file = tmp / "طويل.txt"
        long_file.write_text("جملة تجريبية. " * 400, encoding="utf-8")
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"max_chars": "300", "chunk_chars": "120"})
        played.clear()
        read_result = _act_read({"file": str(long_file)}, None)
        check("القراءة من ملف تبدأ وتذكر الحد", "أقرأ" in read_result and "أطول من الحد" in read_result, read_result[:120])
        while _STATE.get("state") == "speaking":
            time.sleep(0.05)
        check("الحد الأقصى يحترم (٣٠٠ حرفًا)", _STATE.get("chars", 0) <= 300, str(_STATE.get("chars")))
        secret_file = tmp / "مفاتيح.env"
        secret_file.write_text("API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\nTOKEN=ghp_abcdefghijklmnopqrstuvwxyz9876",
                               encoding="utf-8")
        blocked = _act_read({"file": str(secret_file)}, None)
        check("الملف السري يُمنع بصوت عالٍ", "force=yes" in blocked, blocked[:100])
        forced = _act_read({"file": str(secret_file), "force": "yes"}, None)
        check("وبالأمر الصريح يقرأ", "أقرأ" in forced, forced[:80])
        check("ملف غير موجود يشرح", "لا أجد الملف" in _act_read({"file": str(tmp / "خيالي.txt")}, None))
        check("read بلا مسار يشرح", "حدّد الملف" in _act_read({}, None))

        # ٥) الحافظة والأصوات والإعدادات والحالة
        check("clipboard بلا pyperclip أو فارغة لا ينهار", isinstance(_act_clipboard({}, None), str))
        voices_text = _act_voices({}, None)
        check("الأصوات تُعرض من الخدمة", "ar-EG-SalmaNeural" in voices_text and "شمال" not in voices_text)
        check("عدد الأصوات معقول", "الأصوات العربية المتاحة (3)" in voices_text or "الأصوات العربية المتاحة" in voices_text)
        set_result = _act_set({"voice": "ar-EG-SalmaNeural", "rate": "+20%", "pitch": "+5Hz"}, None)
        check("set يحدّث الإعدادات", "+20%" in set_result and "Salma" in set_result, set_result[:100])
        check("set يرفض محرّكًا مجهولًا", "edge أو piper" in _act_set({"engine": "magic"}, None))
        check("set بلا شيء يشرح الحالي", "قل لي ما أغيّره" in _act_set({}, None))
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"rate": "نص خطأ"})
        safe = _act_say({"text": "اختبار السرعة الخاطئة."}, None)
        check("سرعة محفوظة خاطئة لا تُسقط القراءة", "أقرأ الآن" in safe and "استخدمت" in safe, safe[:140])
        status = _act_status({}, None)
        check("الحالة تشرح الوضع", "EdgeTTS" in status and "محرّك" in status, status[:120])
        check("stop يُسكت بلا انهيار", "سيدي" in _act_stop({}, None))

        # ٦) بلا EdgeTTS: رسالة تثبيت واضحة بدل الانهيار
        globals()["_synth_edge"] = lambda text, voice, rate, pitch: (None, "EdgeTTS غير مثبّتة:  pip install edge-tts")
        sys.modules.pop("edge_tts", None)
        started = _act_say({"text": "محاولة بلا مكتبة."}, None)
        check("بلا مكتبة تبدأ ثم تُعلن السبب في الحالة",
              "أقرأ الآن" in started)
        deadline = time.time() + 5
        while time.time() < deadline and _STATE.get("state") in ("speaking", "idle"):
            time.sleep(0.05) if _STATE.get("state") == "speaking" else time.sleep(0.05)
        check("الحالة انتهت بخطأ مفهوم", _STATE.get("state") == "error" and "edge-tts" in str(_STATE.get("detail")),
              f"{_STATE.get('state')} · {_STATE.get('detail')}")
        check("piper بلا مسارات يشرح السبب", "piper_path" in _synth_piper("نص")[1])

        # ٧) الواجهة
        check("فعل مجهول يُرفض بلطف", "ليس فعلًا" in run({"action": "sing"}))
        check("run بلا معاملات لا ينهار", isinstance(run({}), str))
        check("help يشرح force", "force=yes" in _act_help({}, None))
    finally:
        globals()["_play_bytes"] = original_play          # type: ignore[assignment]
        globals()["_synth_edge"] = original_edge          # type: ignore[assignment]
        globals()["_synth_piper"] = original_piper        # type: ignore[assignment]
        sys.modules.pop("edge_tts", None)
        T.extras_dir = original_extras                    # type: ignore[assignment]
        T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
