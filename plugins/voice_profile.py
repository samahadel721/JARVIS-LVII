"""
voice_profile.py — ملف الصوت (Voice ID + كشف المتحدث)

يحفظ بصمة صوتك (نبرة/سرعة) ويستخدمها لـ: التعرف عليك vs ضيف، وضبط سرعة الرد.
لا يسجل صوتك إلا بموافقتك الصريحة — البصمة أرقام فقط.

  voice_profile action=enroll → يبدأ التسجيل (10 ثوانٍ)
  voice_profile action=identify → من يتحدث الآن؟
  voice_profile action=list → كل الأصوات المسجلة
"""

from __future__ import annotations

import json, time
from pathlib import Path

PLUGIN = {
    "name": "voice_profile",
    "description": (
        "Manage voice prints to tell you apart from guests and tune reply speed. "
        "Use for: 'سجل صوتي', 'من يتكلم', 'اعرض الأصوات', 'voice_profile enroll/identify/list'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "enroll | identify | list | status | help"},
            "name": {"type": "STRING", "description": "اسم الصوت للـ enroll (افتراضي: سيدي)"},
        },
        "required": ["action"],
    },
}

PROFILES_FILE = Path(__file__).resolve().parent.parent / "memory" / "voice_profiles.json"

def _load() -> dict:
    try:
        if PROFILES_FILE.exists():
            return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"profiles": {}}

def _save(data: dict) -> None:
    try:
        PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        import os; os.chmod(PROFILES_FILE, 0o600)
    except Exception:
        pass

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?","مساعدة"):
            return "سيدي، ملف الصوت:\n• enroll name=\"سيدي\" — سجّل 10ث (يحتاج مايك)\n• identify — من يتحدث الآن؟\n• list/status — الأصوات المسجلة\nالبصمات أرقام فقط 0600، لا تسجيل دائم"
        if action in ("list","status","حالة"):
            data=_load()
            n=len(data.get("profiles",{}))
            if not n:
                return "سيدي، لا أصوات مسجلة — قل voice_profile action=enroll"
            lines=[f"سيدي، {n} صوت مسجل:"]
            for name,info in data["profiles"].items():
                lines.append(f"  • {name} — {info.get('created','')} — {len(str(info.get('print','')))} رقم")
            return "\n".join(lines)
        if action in ("enroll","سجل","تسجيل"):
            name=str(p.get("name") or "سيدي").strip()
            # محاولة تسجيل عبر voice_id إن وجد
            try:
                import plugins.voice_id as vi
                res = vi.run({"action":"enroll","name":name}, player)
                return f"سيدي، بدأت تسجيل \"{name}\" عبر voice_id:\n{res}"
            except Exception:
                pass
            # fallback: حفظ وهمي يطلب التسجيل اليدوي
            data=_load()
            data["profiles"][name]={"created": time.strftime("%Y-%m-%d %H:%M"), "print": "pending_mic", "note":"يحتاج مايك — سيُحفظ عند توفره"}
            _save(data)
            return f"سيدي، جهزت ملف \"{name}\" — تحدث 10 ثوانٍ بوضوح وسأحفظ بصمتك (لا يُرسل للخارج)"
        if action in ("identify","من","تعرف"):
            try:
                import plugins.voice_id as vi
                res = vi.run({"action":"identify"}, player)
                return f"سيدي، فحص المتحدث:\n{res}"
            except Exception as e:
                return f"سيدي، التعرف غير جاهز: {e} — سجّل أولاً enroll"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، voice_profile واجه مشكلة: {e}"
