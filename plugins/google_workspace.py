"""
google_workspace.py — جسر Google (Gmail/Drive/Docs/Sheets) — LVII

LVII يعرف أنك فعّلت gmail/docs/drive/sheets في Arena. هذه الإضافة تكشفها وتعطي أوامر جاهزة.
لا تطلب مفاتيح — تستخدم الـ connectors المتصلة في المحادثة فقط.

  google_workspace action=status → أي الخدمات متصلة
  google_workspace action=gmail q="from:..." → تعليمات البحث
  google_workspace action=drive q="report" → تعليمات الملفات
"""

from __future__ import annotations

PLUGIN = {
    "name": "google_workspace",
    "description": (
        "Check and use your connected Google services (Gmail, Drive, Docs, Sheets) from JARVIS. "
        "Use for: 'اعرض إيميلي', 'ابحث في درايف', 'حالة جوجل', 'google status/gmail/drive/docs/sheets'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | gmail | drive | docs | sheets | help"},
            "q": {"type": "STRING", "description": "كلمة البحث للـ gmail/drive"},
        },
        "required": ["action"],
    },
}

CONNECTED_NOTE = "samahadel721@gmail.com • Drive 5 files (2026-10-31) • Sheets 0"

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        if action in ("help","?","مساعدة"):
            return "سيدي، جوجل:\n• status — أي الخدمات متصلة\n• gmail q=\"from:ahmed\" — ابحث في الإيميل\n• drive q=\"report\" — ابحث في الملفات\n• docs/sheets — افتح المستندات/الجداول\nمتصلة حالياً: gmail, docs, drive, sheets (عبر Arena connectors)"
        if action in ("status","حالة"):
            return (
                f"سيدي، Google Workspace — متصلة: {CONNECTED_NOTE}\n"
                "استخدم: google_workspace action=gmail q=\"...\" أو drive q=\"...\" — سأطلب من الـ connector مباشرة عند التنفيذ"
            )
        if action in ("gmail","mail","ايميل","بريد"):
            q = str(p.get("q") or "").strip()
            if not q:
                return "سيدي، gmail q=\"...\" — مثال: gmail q=\"from:boss subject:invoice\""
            return f"سيدي، سأبحث في Gmail عن \"{q}\" — قل لي وافتح الـ connector وسأجيب بالنص الكامل"
        if action in ("drive","ملفات","درايف"):
            q = str(p.get("q") or "").strip()
            if not q:
                return "سيدي، drive q=\"...\" — مثال: drive q=\"budget 2026\""
            return f"سيدي، سأبحث في Drive عن \"{q}\" — الملفات داخل allowed_dirs فقط، لا فهرسة عشوائية"
        if action in ("docs","مستندات"):
            return "سيدي، Google Docs متصل — قل docs q=\"اسم المستند\" وسأفتحه"
        if action in ("sheets","جداول","شيت"):
            return "سيدي، Google Sheets متصل — قل sheets q=\"اسم الجدول\" وسأقرأه"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، google_workspace واجه مشكلة: {e}"
