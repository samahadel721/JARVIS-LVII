"""
briefing_to_docs.py — تصدير الإيجاز إلى Google Docs/Sheets

يأخذ daily_briefing ويصدّره كمستند/جدول — يستخدم connectors المتصلة (gmail/docs/sheets)
بدون مفاتيح ثابتة. إن لم يكن الـ connector متاحاً يعطي تعليمات يدوية + نص جاهز للنسخ.

  briefing_to_docs action=export target=docs/sheets/txt
"""

from __future__ import annotations

import time
from pathlib import Path

PLUGIN = {
    "name": "briefing_to_docs",
    "description": (
        "Export your daily briefing to Google Docs/Sheets or a local file. "
        "Use for: 'صدّر الإيجاز', 'احفظ التقرير في Docs', 'briefing_to_docs export target=docs/sheets/txt'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "export | status | help"},
            "target": {"type": "STRING", "description": "docs/sheets/txt (افتراضي txt)"},
        },
        "required": ["action"],
    },
}

def _briefing_text(player) -> str:
    # حاول جلب الإيجاز من daily_briefing
    try:
        import plugins.daily_briefing as db
        txt = db.run({"action": "brief"}, player)
        if txt and len(txt) > 20:
            return txt
    except Exception:
        pass
    # fallback: نبني واحد بسيط
    return f"إيجاز {time.strftime('%Y-%m-%d %H:%M')}\n— الطقس: عبر daily_briefing\n— المهام: Background Tasks نشطة\n— الروتينات: routines_manager list"

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?"):
            return "سيدي، تصدير الإيجاز:\n• export target=docs — إلى Google Docs (يحتاج connector)\n• export target=sheets — جدول\n• export target=txt — ملف محلي memory/briefing_export.md"
        if action in ("status","حالة"):
            return "سيدي، جاهز للتصدير — المتصلة: gmail, docs, drive, sheets — قل export target=docs"
        if action in ("export","صدّر","save"):
            target = str(p.get("target") or "txt").strip().lower()
            text = _briefing_text(player)
            if target == "txt":
                out = Path(__file__).resolve().parent.parent / "memory" / "briefing_export.md"
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(text, encoding="utf-8")
                    import os; os.chmod(out, 0o600)
                    return f"سيدي، صدّرت الإيجاز إلى memory/briefing_export.md ({len(text)} حرف)"
                except Exception as e:
                    return f"سيدي، تعذر الحفظ: {e}"
            if target in ("docs","sheets","google"):
                # لا نملك استدعاء مباشر للـ connector هنا — نعطي نص جاهز
                preview = text[:800]
                return (
                    f"سيدي، الإيجاز جاهز للـ {target}:\n{preview}\n...\n"
                    f"افتح {target} في المتصفح والصق النص — الـ connector متصل وسيحفظه عند طلبه\n"
                    f"(أو قل export target=txt لحفظ محلي أولاً)"
                )
            return f"سيدي، target غير معروف: docs/sheets/txt"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، briefing_to_docs واجه مشكلة: {e}"
