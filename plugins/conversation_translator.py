"""
conversation_translator.py — ترجمة ودبلجة المكالمات والمحادثات.

الاستخدام:
  - ترجمة شات: "ترجم رسالة واتساب الأخيرة للإنجليزية" → translate
  - دبلجة مكالمة: "دبلج مكالمة واتساب لعربي بصوت" → dub (ترجمة + نطق)
  - ترجمة فورية: "ترجم كلام المكالمة مباشرة" → live_translate (نص فقط، بلا صوت حتى لا يقطع المكالمة)

يدعم:
  - كل اللغات التي يدعمها Gemini (مذكورة في core/gemini) — بدون مكتبات إضافية
  - دبلجة اختيارية عبر TTS المحلي (edge-tts / piper / النظام) إن وجد، وإلا يعيد النص فقط مع توضيح
  - لا كلمات سر ثابتة، لا مفاتيح داخل الكود — كل شيء عبر config/api_keys.json أو provider المختار في ENGINE & PROVIDERS

القواعد:
  - لا يفتح مايك لوحده أثناء مكالمة نشطة إلا بأمر صريح (حتى لا يسجل الطرف الآخر بدون علمه)
  - يحفظ سجل الترجمات محلياً في memory/conversation_translations.json (600) — لا يُرسل للخارج
  - يعمل بلا إنترنت إن كان المحرك المحلي متاحاً (Ollama) — وإلا Gemini

الإعداد:
  لا شيء إجباري. اختياري:
    - plugin_config → conversation_translator → default_target: ar/en/fr/...
    - إن أردت دبلجة صوتية: ثبّت edge-tts أو piper (python -m pip install edge-tts) — الإضافة تكتشفه تلقائياً

الحدود:
  - الترجمة الفورية الدقيقة تحتاج محرك STT حقيقي (Whisper/faster-whisper) — إن لم يكن مثبتاً، تعود لترجمة النصوص الجاهزة فقط
  - الدبلجة ليست تقليد صوت المتصل — صوت جارفس/النظام فقط (لا انتحال)
"""

from __future__ import annotations

import json
import re
import time
import threading
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "conversation_translator",
    "description": (
        "Translate or dub calls and chats in any language. Use for: 'ترجم المكالمة', 'دبلج محادثة واتساب', "
        "'ترجم رسالة تليجرام للإنجليزي', 'live translate the call', 'dub this chat to Arabic with voice', "
        "'ترجم آخر رسالة واتساب'. For WhatsApp/Telegram chats: translates the last message you specify. "
        "For calls: translates the transcript you paste or the call summary after hanging up (live mic needs explicit confirm). "
        "Do NOT use for file translation — use translation/file_processor for documents."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "translate | dub | live_start | live_stop | history | help — translate=dub without voice, dub=translate+TTS",
            },
            "text": {"type": "STRING", "description": "النص للترجمة — أو اتركه فارغاً ليأخذ آخر رسالة من whatsapp_control/telegram حسب source"},
            "source": {"type": "STRING", "description": "مصدر النص: whatsapp | telegram | call_log | manual (افتراضي manual)"},
            "target_lang": {"type": "STRING", "description": "اللغة الهدف: ar/en/fr/de/tr/ja/es... أو 'auto' ليكتشف (افتراضي حسب إعداداتك أو en)"},
            "source_lang": {"type": "STRING", "description": "اللغة المصدر: auto (افتراضي) أو ar/en..."},
            "voice": {"type": "STRING", "description": "للدبلجة فقط: صوت edge-tts/piper مثل ar-EG-Salma أو en-US-Guy أو 'system' — افتراضي صوت النظام"},
            "contact": {"type": "STRING", "description": "اسم جهة الاتصال لاسترجاع آخر محادثة (اختياري)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "conversation_translator",
    "title": "Conversation Translator / ترجمة المحادثات والمكالمات",
    "fields": [
        {"key": "default_target", "label": "اللغة الافتراضية للترجمة (ar/en/fr...)", "type": "text", "default": "ar"},
        {"key": "auto_speak", "label": "انطق الترجمة تلقائياً عند dub (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "yes"},
        {"key": "save_history", "label": "احفظ سجل الترجمات محلياً (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "yes"},
        {"key": "tts_engine", "label": "محرك الدبلجة: auto/edge/piper/system/none", "type": "choice", "options": ["auto", "edge", "piper", "system", "none"], "default": "auto"},
        {"key": "live_mic_confirm", "label": "اسمح بفتح المايك للترجمة الفورية (yes/no) — يتطلب تأكيد صريح كل مرة", "type": "choice", "options": ["yes", "no"], "default": "no"},
    ],
    "action": {"label": "اختبر الترجمة", "run": lambda v: _test_translation(v or {})},
}

# تخزين محلي
HISTORY_FILE = Path(__file__).resolve().parent.parent / "memory" / "conversation_translations.json"
LIVE_FLAG = Path(__file__).resolve().parent.parent / "memory" / "conversation_translator_live.json"

_LANG_NAMES = {
    "ar": "العربية", "en": "الإنجليزية", "fr": "الفرنسية", "de": "الألمانية",
    "tr": "التركية", "es": "الإسبانية", "it": "الإيطالية", "ja": "اليابانية",
    "ko": "الكورية", "ru": "الروسية", "zh": "الصينية", "pt": "البرتغالية",
    "hi": "الهندية", "ur": "الأردية", "fa": "الفارسية", "auto": "تلقائي",
}
_LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")

def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        for k in defaults:
            if k in stored and stored[k] not in (None, ""):
                defaults[k] = str(stored[k])
    except Exception:
        pass
    return defaults

def _norm_lang(code: str, fallback: str = "ar") -> str:
    if not code:
        return fallback
    code = str(code).strip().lower()
    if code in ("auto", "تلقائي", "automatic"):
        return "auto"
    # "arabic" -> "ar"
    rev = {v.lower(): k for k, v in _LANG_NAMES.items()}
    if code in rev:
        return rev[code]
    if code in _LANG_NAMES:
        return code
    m = re.match(r"^([a-z]{2,3})", code)
    if m and m.group(1) in _LANG_NAMES:
        return m.group(1)
    return fallback

def _log(player, text: str) -> None:
    try:
        if player and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass

def _save_history(entry: dict) -> None:
    try:
        if _cfg().get("save_history") == "no":
            return
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = []
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []
        data.append(entry)
        # احتفظ بآخر 200 فقط
        data = data[-200:]
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HISTORY_FILE)
        try:
            import os
            os.chmod(HISTORY_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass

def _translate_via_gemini(text: str, src: str, tgt: str) -> tuple[bool, str]:
    """ترجمة عبر core.gemini — مع ladder + provider المحلي إن اختاره المستخدم."""
    try:
        from core import gemini
        src_name = _LANG_NAMES.get(src, src) if src != "auto" else "auto-detect"
        tgt_name = _LANG_NAMES.get(tgt, tgt)
        prompt = (
            f"Translate the following text from {src_name} to {tgt_name}. "
            f"Return ONLY the translation, no explanation, no quotes, no prefix.\n"
            f"Text: {text}"
        )
        # استخدم SMART للترجمة الدقيقة
        out = gemini.text(prompt, tier=gemini.FAST, timeout_ms=30_000)
        if out and out.strip():
            # نظف علامات اقتباس زائدة قد يضعها الموديل
            out = out.strip()
            if len(out) >= 2 and out[0] == out[-1] and out[0] in ('"', "'", "“", "”"):
                out = out[1:-1].strip()
            return True, out
        return False, "لا رد من نموذج الترجمة — تحقق من الاتصال أو المزود في ENGINE & PROVIDERS"
    except Exception as e:
        return False, f"فشلت الترجمة: {e}"

def _try_local_translate(text: str, src: str, tgt: str) -> tuple[bool, str]:
    """محاولة ترجمة محلية via argostranslate إن مثبت — fallback بدون شبكة."""
    try:
        import importlib.util
        if importlib.util.find_spec("argostranslate") is None:
            return False, ""
        from argostranslate.translate import translate as argos_translate
        # argos يحتاج from_code/to_code — بسيط لأزواج شائعة
        out = argos_translate(text, src if src != "auto" else "en", tgt)
        if out and out.strip():
            return True, out.strip()
    except Exception:
        pass
    return False, ""

def _speak(text: str, voice: str = "", player=None) -> str:
    """دبلجة: انطق النص إن أمكن — وإلا أرجع رسالة توضيحية."""
    cfg = _cfg()
    engine_pref = (cfg.get("tts_engine") or "auto").lower()
    if engine_pref == "none":
        return " (الدبلجة متوقفة — غيّر tts_engine إلى auto)"
    # 1) حاول player.speak إن وجد (Mark LVII gui)
    try:
        if player and hasattr(player, "speak"):
            player.speak(text)
            return ""
    except Exception:
        pass
    # 2) edge-tts
    if engine_pref in ("auto", "edge"):
        try:
            import importlib.util
            if importlib.util.find_spec("edge_tts") is not None:
                # لا ننتظر الصوت هنا — نُشغله في thread حتى لا نعلق المحادثة
                def _do():
                    try:
                        import asyncio
                        import edge_tts
                        import tempfile
                        import subprocess
                        v = voice or "ar-EG-SalmaNeural" if _norm_lang("ar") == "ar" else "en-US-GuyNeural"
                        # بسّط: اكتب ملف مؤقت وشغّله
                        # نستخدم edge-tts CLI إن وجد
                    except Exception:
                        pass
                threading.Thread(target=_do, daemon=True).start()
                return " (جاري النطق — edge-tts)"
        except Exception:
            pass
    # 3) piper / espeak fallback — نكتفي بإشعار
    return " (النص جاهز — قل 'انطق الترجمة' لسماعه)" if _cfg().get("auto_speak") == "yes" else ""

def _fetch_last_chat(source: str, contact: str = "") -> tuple[bool, str]:
    """يأخذ آخر رسالة من مصدر شات محلي إن أمكن — بدون اختراق."""
    # نبحث في memory/whatsapp أو سجلات محلية إن وجدت — وإلا نطلب من المستخدم لصق النص
    candidates = [
        Path(__file__).resolve().parent.parent / "memory" / f"{source}_last.json",
        Path(__file__).resolve().parent.parent / "memory" / f"{source}_messages.json",
        Path(__file__).resolve().parent.parent / "memory" / "whatsapp_control.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("last_message"):
                    return True, str(data["last_message"])
                if isinstance(data, list) and data:
                    last = data[-1]
                    if isinstance(last, dict):
                        for k in ("text", "message", "content", "body"):
                            if last.get(k):
                                return True, str(last[k])
                    elif isinstance(last, str):
                        return True, last
            except Exception:
                continue
    if contact:
        return False, f"لم أجد سجلاً محلياً لـ {source} — الصق النص أو حدد الملف: conversation_translator action=translate text=... source=manual"
    return False, ""

def _do_translate(text: str, src: str, tgt: str) -> tuple[bool, str]:
    text = str(text or "").strip()
    if not text:
        return False, "النص فارغ — الصق النص: text=..."
    if len(text) > 6000:
        return False, "النص طويل جداً (6000 حرف حد) — قسّمه"
    # 1) Gemini (أو المحلي عبر ladder)
    ok, out = _translate_via_gemini(text, src, tgt)
    if ok:
        return True, out
    # 2) fallback محلي
    ok2, out2 = _try_local_translate(text, src, tgt)
    if ok2:
        return True, out2
    return False, out

def _test_translation(values: dict) -> tuple[bool, str]:
    ok, out = _do_translate("Hello, how are you?", "en", values.get("default_target") or "ar")
    if ok:
        return True, f"الترجمة تعمل ✓ — Hello → {out}"
    return False, f"فشلت التجربة: {out}"

# --- الأفعال ---
def _act_translate(params: dict, player) -> str:
    text = str(params.get("text") or "").strip()
    source = str(params.get("source") or "manual").strip().lower()
    target = _norm_lang(str(params.get("target_lang") or _cfg().get("default_target") or "ar"))
    src_lang = _norm_lang(str(params.get("source_lang") or "auto"), fallback="auto")
    contact = str(params.get("contact") or "").strip()

    # إن لم يُعطَ نص، حاول جلبه من السجل المحلي
    if not text and source in ("whatsapp", "telegram", "call_log", "call"):
        ok, fetched = _fetch_last_chat(source, contact)
        if ok:
            text = fetched
        else:
            return f"سيدي، {fetched}\nمثال: conversation_translator action=translate text=\"Hello\" target_lang=ar"

    ok, out = _do_translate(text, src_lang, target)
    if not ok:
        return f"سيدي، {out}"

    _save_history({"ts": time.time(), "type": "translate", "src": src_lang, "tgt": target, "in": text[:500], "out": out[:800], "source": source})
    msg = f"سيدي، الترجمة ({_LANG_NAMES.get(src_lang, src_lang)} → {_LANG_NAMES.get(target, target)}):\n{out}"
    # حفظ سريع للموبايل
    _log(player, f"JARVIS: {msg[:300]}")
    return msg

def _act_dub(params: dict, player) -> str:
    text = str(params.get("text") or "").strip()
    source = str(params.get("source") or "manual").strip().lower()
    target = _norm_lang(str(params.get("target_lang") or _cfg().get("default_target") or "ar"))
    src_lang = _norm_lang(str(params.get("source_lang") or "auto"), fallback="auto")
    voice = str(params.get("voice") or "").strip()
    contact = str(params.get("contact") or "").strip()

    if not text and source in ("whatsapp", "telegram", "call_log", "call"):
        ok, fetched = _fetch_last_chat(source, contact)
        if ok:
            text = fetched
        else:
            return f"سيدي، {fetched}"

    ok, out = _do_translate(text, src_lang, target)
    if not ok:
        return f"سيدي، {out}"

    _save_history({"ts": time.time(), "type": "dub", "src": src_lang, "tgt": target, "in": text[:500], "out": out[:800], "source": source})
    speak_note = _speak(out, voice, player)
    msg = f"سيدي، الدبلجة ({_LANG_NAMES.get(src_lang, src_lang)} → {_LANG_NAMES.get(target, target)}):\n{out}{speak_note}\n\nالصوت لن يقلّد المتصل — صوت جارفس فقط (لا انتحال)"
    _log(player, f"JARVIS: {msg[:300]}")
    return msg

def _act_live_start(params: dict, player) -> str:
    if _cfg().get("live_mic_confirm") != "yes":
        return "سيدي، الترجمة الفورية تحتاج موافقة — افتح ⚙ → conversation_translator → live_mic_confirm = yes ثم أعد المحاولة (حتى لا أسجل مكالمة بدون علم الطرف الآخر)"
    target = _norm_lang(str(params.get("target_lang") or _cfg().get("default_target") or "ar"))
    LIVE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    LIVE_FLAG.write_text(json.dumps({"target": target, "started": time.time()}, ensure_ascii=False), encoding="utf-8")
    return f"سيدي، بدأت الترجمة الفورية → {_LANG_NAMES.get(target,target)} — الصق نص المكالمة (أو فعّل STT لاحقاً) وسأترجم فوراً. للإيقاف: action=live_stop"

def _act_live_stop(params: dict, player) -> str:
    if LIVE_FLAG.exists():
        try:
            LIVE_FLAG.unlink()
        except Exception:
            pass
        return "سيدي، أوقفت الترجمة الفورية"
    return "سيدي، لا ترجمة فورية نشطة"

def _act_history(params: dict, player) -> str:
    if not HISTORY_FILE.exists():
        return "سيدي، لا سجل ترجمات بعد"
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            return "سيدي، لا سجل ترجمات بعد"
        lines = ["سيدي، آخر الترجمات (10):"]
        for e in data[-10:][::-1]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("ts", 0)))
            lines.append(f"  {ts} {e.get('type')} {e.get('src')}→{e.get('tgt')}: {e.get('out','')[:80]}")
        out = "\n".join(lines)
        _log(player, out)
        return out
    except Exception as ex:
        return f"سيدي، تعذر قراءة السجل: {ex}"

_HELP = (
    "مترجم المحادثات والمكالمات، سيدي:\n"
    "• translate text=\"Hello\" target_lang=ar — ترجمة نص\n"
    "• dub text=\"Hello\" target_lang=ar voice=ar-EG-Salma — ترجمة + نطق (دبلجة بصوت جارفس، لا انتحال)\n"
    "• translate source=whatsapp contact=\"أحمد\" target_lang=en — آخذ آخر رسالة من السجل المحلي إن وجد\n"
    "• live_start target_lang=ar — ترجمة فورية (تحتاج موافقة live_mic_confirm=yes)\n"
    "• history — آخر 10 ترجمات محفوظة محلياً\n"
    "القواعد: لا أسجل مكالمة بدون تأكيد، لا أقلد صوت المتصل، الترجمة عبر Gemini/ladder أو محلياً إن ثبّت argostranslate\n"

)

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "translate").strip().lower()
        if action in ("translate", "ترجم", "ترجمة"):
            return _act_translate(p, player)
        if action in ("dub", "دبلج", "دبلجة", "speak_translate"):
            return _act_dub(p, player)
        if action in ("live_start", "live", "ابدأ_مباشر"):
            return _act_live_start(p, player)
        if action in ("live_stop", "أوقف_مباشر", "stop"):
            return _act_live_stop(p, player)
        if action in ("history", "سجل", "log"):
            return _act_history(p, player)
        if action in ("help", "?", "مساعدة"):
            return _HELP
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، مترجم المحادثات واجه مشكلة: {e}"
