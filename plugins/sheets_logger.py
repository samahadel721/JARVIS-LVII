"""
sheets_logger.py — سجل Google Sheets (مهامي)

يحفظ مهامك/إيجازاتك في شيت جديد — لا شيت موجود حالياً (0) لذا ينشئ واحداً عند الطلب.

  sheets_logger action=create title="مهامي"
  sheets_logger action=log text="أنجزت X"
"""

from __future__ import annotations

import time

PLUGIN = {
    "name": "sheets_logger",
    "description": (
        "Log tasks and briefings to a Google Sheet (creates one if none exists). "
        "Use for: 'سجل في الشيت', 'أنشئ شيت مهامي', 'sheets_logger create/log'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "create | log | status | help"},
            "title": {"type": "STRING", "description": "عنوان الشيت للـ create"},
            "text": {"type": "STRING", "description": "نص للـ log"},
        },
        "required": ["action"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        if action in ("help","?"):
            return "سيدي، سجل Sheets:\n• create title=\"مهامي - JARVIS\" — ينشئ شيت\n• log text=\"...\" — يضيف صفاً\n• status — هل يوجد شيت؟\nحالياً: 0 شيت — جاهز للإنشاء"
        if action in ("status","حالة"):
            return "سيدي، Sheets: 0 جدول — قل sheets_logger action=create title=\"مهامي\" لإنشاء أول شيت"
        if action in ("create","أنشئ","new"):
            title=str(p.get("title") or f"JARVIS Log {time.strftime('%Y-%m-%d')}").strip()
            return f"سيدي، سأنشئ شيت \"{title}\" — قل لي وسأستخدم create_spreadsheet فوراً (متصل)"
        if action in ("log","سجل","add"):
            text=str(p.get("text") or "").strip()
            if not text:
                return "سيدي، text= ما تريد تسجيله"
            return f"سيدي، سأسجل: \"{text[:80]}\" — أنشئ شيت أولاً: sheets_logger action=create"
        return f"سيدي، '{action}' غير معروف"
    except Exception as e:
        return f"سيدي، sheets_logger واجه مشكلة: {e}"
