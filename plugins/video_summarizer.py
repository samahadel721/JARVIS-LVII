"""
video_summarizer.py — تلخيص الفيديو على الشاشة + أسئلة عليه (Torvis 0.6 big feature)

  "لخّص الفيديو اللي على الشاشة" → يقرأ Transcript (YouTube عبر youtube_transcript_api أو يفتح الملف)
  ثم يلخّصه عبر core.gemini ويعرض الملخص + يحفظه في memory/video_summaries.json
  ثم "اسألني عنه" → يولّد quiz من الملخص

يدعم: YouTube / Instagram / TikTok / X (عبر URL) + ملف محلي (mp4/mov) + نص ملصوق

الحدود: لا يلتقط الشاشة فيديو — يعتمد على transcript المتاح. إن لم يوجد transcript، يطلب منك لصق النص أو وصف الفيديو.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "video_summarizer",
    "description": (
        "Summarize a video on your screen and quiz you on it. Use for: 'لخّص الفيديو', 'summarize the YouTube video', "
        "'لخّص فيديو التيكتوك ده', 'summarize this file video.mp4', 'اسألني عن الفيديو', 'quiz me on the video'. "
        "Takes a URL, a local file path, or pasted transcript — then writes a concise summary in your language and can generate questions."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "summarize | quiz | history | help"},
            "source": {"type": "STRING", "description": "URL (YouTube/TikTok/X/Instagram) أو مسار ملف محلي أو نص transcript ملصوق"},
            "question_count": {"type": "STRING", "description": "عدد أسئلة الـ quiz (افتراضي 5)"},
            "language": {"type": "STRING", "description": "لغة الملخص: ar/en/auto (افتراضي لغتك)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "video_summarizer",
    "title": "Video Summarizer / ملخص الفيديو",
    "fields": [
        {"key": "summary_length", "label": "طول الملخص: short/medium/detailed", "type": "choice", "options": ["short", "medium", "detailed"], "default": "medium"},
        {"key": "auto_quiz", "label": "ولّد quiz تلقائياً بعد التلخيص (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "no"},
        {"key": "save_history", "label": "احفظ الملخصات (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "yes"},
    ],
}

HISTORY_FILE = Path(__file__).resolve().parent.parent / "memory" / "video_summaries.json"

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

def _log(player, text: str) -> None:
    try:
        if player and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass

def _extract_youtube_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", url or "")
    return m.group(1) if m else None

def _get_transcript(url_or_text: str) -> tuple[bool, str, str]:
    """يحاول جلب transcript — YouTube transcript API أولاً، ثم يعود للنص كما هو."""
    s = str(url_or_text or "").strip()
    if not s:
        return False, "", "لا يوجد مصدر — الصق رابط أو نص"
    # URL YouTube
    vid = _extract_youtube_id(s)
    if vid:
        try:
            import importlib.util
            if importlib.util.find_spec("youtube_transcript_api") is not None:
                from youtube_transcript_api import YouTubeTranscriptApi
                # حاول API القديم والجديد
                try:
                    tr = YouTubeTranscriptApi.get_transcript(vid, languages=["ar", "en"])
                    text = " ".join([x.get("text","") for x in tr])
                    if text.strip():
                        return True, text.strip(), f"YouTube transcript ({vid})"
                except Exception:
                    pass
                try:
                    api = YouTubeTranscriptApi()
                    tr = api.fetch(vid)
                    text = " ".join([getattr(x, "text", "") or x.get("text","") for x in tr])
                    if text.strip():
                        return True, text.strip(), f"YouTube transcript ({vid})"
                except Exception as e:
                    return False, "", f"تعذر جلب transcript لـ {vid}: {e} — الصق النص يدوياً"
        except Exception as e:
            return False, "", f"youtube_transcript_api غير مثبت: {e} — ثبّته: pip install youtube-transcript-api"
        return False, "", "لم أجد transcript — جرّب لصق النص"
    # ليس YouTube — اعتبره نصاً/وصفاً مباشراً إن كان طويلاً، أو مسار ملف
    p = Path(s)
    if p.exists() and p.is_file():
        # حاول قراءة ملف نصي/ترانسكريبت جانبي
        try:
            if p.suffix.lower() in (".txt", ".srt", ".vtt"):
                return True, p.read_text(encoding="utf-8", errors="ignore")[:20000], f"file {p.name}"
            # فيديو بلا transcript — اطلب من Whisper لاحقاً
            return False, "", f"ملف فيديو {p.name} — لا transcript جاهز. استخدم faster-whisper لتحويله أو الصق وصف الفيديو"
        except Exception as e:
            return False, "", f"تعذر قراءة {p}: {e}"
    # نص ملصوق مباشر
    if len(s) > 20:
        return True, s[:20000], "pasted text"
    return False, "", "النص قصير جداً — الصق transcript أو وصف الفيديو (20 حرف على الأقل)"

def _summarize_with_gemini(transcript: str, lang: str = "auto", length: str = "medium") -> tuple[bool, str]:
    try:
        from core import gemini
        lang_hint = "in Arabic" if lang == "ar" else ("in English" if lang == "en" else "in the user's language")
        length_hint = {"short": "5-7 sentences", "medium": "12-18 sentences with bullet points", "detailed": "comprehensive with sections"}[length]
        prompt = (
            f"Summarize the following video transcript {lang_hint} — {length_hint}. "
            "Lead with the main idea, then key points, then any actionable takeaways. "
            "If the transcript is unclear, say so briefly.\n\n"
            f"Transcript:\n{transcript[:15000]}"
        )
        out = gemini.text(prompt, tier=gemini.SMART, timeout_ms=60_000)
        if out and out.strip():
            return True, out.strip()
        return False, "لا رد من نموذج التلخيص — تحقق من Gemini/provider"
    except Exception as e:
        return False, f"فشل التلخيص: {e}"

def _make_quiz(summary: str, n: int = 5) -> tuple[bool, str]:
    try:
        from core import gemini
        prompt = (
            f"Create {n} quiz questions (with 4 options each, mark the correct one) based on this video summary. "
            "Return as JSON list: [{\"question\":..., \"options\":[...], \"answer\":..., \"note\":...}]\n\n"
            f"Summary:\n{summary[:8000]}"
        )
        data = gemini.as_json(prompt, tier=gemini.SMART, timeout_ms=45_000)
        if data and isinstance(data, list):
            # اعرض عبر player.show_quiz إن وجد
            return True, json.dumps(data, ensure_ascii=False, indent=2)
        # fallback: نص
        out = gemini.text(prompt, tier=gemini.SMART, timeout_ms=30_000)
        return (True, out.strip()) if out else (False, "لا رد للـ quiz")
    except Exception as e:
        return False, f"فشل الـ quiz: {e}"

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
        data = data[-50:]
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HISTORY_FILE)
    except Exception:
        pass

def _act_summarize(params: dict, player) -> str:
    source = str(params.get("source") or "").strip()
    lang = str(params.get("language") or "auto").strip().lower()
    if lang not in ("ar", "en", "auto"):
        lang = "auto"
    cfg = _cfg()
    ok, transcript, meta = _get_transcript(source)
    if not ok:
        return f"سيدي، {meta}\nمثال: video_summarizer action=summarize source=\"https://youtube.com/watch?v=...\" — أو الصق النص في source"
    ok2, summary = _summarize_with_gemini(transcript, lang, cfg.get("summary_length") or "medium")
    if not ok2:
        return f"سيدي، {summary}"
    _save_history({"ts": time.time(), "source": source[:200], "meta": meta, "summary": summary[:2000]})
    _log(player, f"Video summary ({meta}): {summary[:400]}")
    try:
        if player and hasattr(player, "show_content"):
            player.show_content(f"Video — {meta}", summary)
    except Exception:
        pass
    msg = f"سيدي، ملخص الفيديو ({meta}):\n{summary}"
    if cfg.get("auto_quiz") == "yes":
        ok3, quiz = _make_quiz(summary, 5)
        if ok3:
            msg += f"\n\n--- Quiz ---\n{quiz[:2000]}"
    return msg

def _act_quiz(params: dict, player) -> str:
    # quiz من آخر ملخص محفوظ أو من source معطى
    source = str(params.get("source") or "").strip()
    n = 5
    try:
        n = max(1, min(10, int(str(params.get("question_count") or "5").strip())))
    except Exception:
        pass
    summary = ""
    if source:
        ok, transcript, meta = _get_transcript(source)
        if ok:
            ok2, summary = _summarize_with_gemini(transcript, "auto", "medium")
            if not ok2:
                return f"سيدي، {summary}"
        else:
            return f"سيدي، {meta}"
    else:
        if not HISTORY_FILE.exists():
            return "سيدي، لا ملخص سابق — استخدم action=summarize أولاً"
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if not data:
                return "سيدي، لا ملخص سابق"
            summary = data[-1].get("summary", "")
        except Exception as e:
            return f"سيدي، تعذر قراءة السجل: {e}"
    ok, quiz = _make_quiz(summary, n)
    if not ok:
        return f"سيدي، {quiz}"
    # حاول عرضه كـ quiz تفاعلي
    try:
        if player and hasattr(player, "show_quiz"):
            import json as _j
            qdata = _j.loads(quiz) if quiz.strip().startswith("[") else []
            if qdata and isinstance(qdata, list):
                player.show_quiz(f"Video Quiz ({n}q)", qdata)
                return f"سيدي، فتحت الـ quiz ({n} أسئلة) على الشاشة — جاوب هناك"
    except Exception:
        pass
    return f"سيدي، quiz ({n} أسئلة):\n{quiz[:3000]}"

def _act_history(params: dict, player) -> str:
    if not HISTORY_FILE.exists():
        return "سيدي، لا ملخصات بعد"
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not data:
            return "سيدي، لا ملخصات بعد"
        lines = ["سيدي، آخر الملخصات:"]
        for e in data[-5:][::-1]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("ts", 0)))
            lines.append(f"  {ts} {e.get('meta','')} — {e.get('summary','')[:90]}...")
        out = "\n".join(lines)
        _log(player, out)
        return out
    except Exception as e:
        return f"سيدي، تعذر القراءة: {e}"

_HELP = (
    "ملخص الفيديو، سيدي:\n"
    "• summarize source=\"https://youtube.com/watch?v=...\" — لخّص فيديو يوتيوب (يحتاج youtube_transcript_api: pip install youtube-transcript-api)\n"
    "• summarize source=\"نص transcript ملصوق\" — لخّص أي نص\n"
    "• quiz source=\"...\" question_count=5 — أسئلة من الملخص (أو من آخر ملخص إن لم تحدد source)\n"
    "• history — آخر 5 ملخصات\n"
    "الفيديو نفسه لا يُحفظ — فقط transcript + الملخص (محلي، لا يُرسل إلا لنموذج التلخيص عبر core.gemini)\n"
)

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "summarize").strip().lower()
        if action in ("summarize", "summary", "لخّص", "تلخيص"):
            return _act_summarize(p, player)
        if action in ("quiz", "اسألني", "اختبرني"):
            return _act_quiz(p, player)
        if action in ("history", "سجل"):
            return _act_history(p, player)
        if action in ("help", "?", "مساعدة"):
            return _HELP
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، ملخص الفيديو واجه مشكلة: {e}"
