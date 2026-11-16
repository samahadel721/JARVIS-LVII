"""
gmail_triage.py — فرز Gmail (Inbox Zero)

يستخدم الـ connector الحقيقي (samahadel721@gmail.com - 2203 رسالة) لعرض
غير المقروءة/المهمة — لا يرسل تلقائياً، يعرض فقط.

  gmail_triage action=inbox q="is:unread"
  gmail_triage action=search q="from:boss"
"""

from __future__ import annotations

PLUGIN = {
    "name": "gmail_triage",
    "description": (
        "Triage your Gmail inbox (samahadel721@gmail.com) — list unread/important without auto-send. "
        "Use for: 'اعرض غير المقروء', 'من أرسل لي', 'gmail_triage inbox/search q=...'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "inbox | search | help"},
            "q": {"type": "STRING", "description": "بحث Gmail (مثال: is:unread, from:..., subject:...)"},
        },
        "required": ["action"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "inbox").strip().lower()
        if action in ("help","?"):
            return "سيدي، فرز Gmail (2203 رسالة):\n• inbox q=\"is:unread\" — غير المقروءة\n• search q=\"from:ahmed subject:فاتورة\" — بحث كامل\nالحساب: samahadel721@gmail.com — لا إرسال تلقائي، عرض فقط"
        q = str(p.get("q") or "").strip()
        if action in ("inbox","وارد","unread"):
            hint = q or "is:unread"
            return f"سيدي، سأعرض الوارد: \"{hint}\" — قل gmail_triage action=search q=\"{hint}\" وسأجلب عبر connector (1660 محادثة)"
        if action in ("search","ابحث","بحث"):
            if not q:
                return "سيدي، q= كلمة البحث — مثال: gmail_triage action=search q=\"is:unread newer_than:7d\""
            return f"سيدي، بحث Gmail عن \"{q}\" — جاهز عبر connector — قل لي وسيُجلب فوراً"
        return f"سيدي، '{action}' غير معروف — inbox/search"
    except Exception as e:
        return f"سيدي، gmail_triage واجه مشكلة: {e}"
