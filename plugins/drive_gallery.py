"""
drive_gallery.py — معرض Drive (صورك الأخيرة)

يعرض آخر 5 ملفات من Drive (2026-10-31) — نفس الملفات اللي شفناها في الاتصال الحقيقي.
لا يحمّل تلقائياً — يعرض الأسماء ويحمّل عند الطلب فقط.

  drive_gallery action=list → آخر 5
  drive_gallery action=read file_id="1IG1VK..."
"""

from __future__ import annotations

PLUGIN = {
    "name": "drive_gallery",
    "description": (
        "List and read your recent Google Drive files (images/video). "
        "Use for: 'اعرض صوري', 'آخر ملفات درايف', 'drive_gallery list/read file_id=...'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | read | help"},
            "file_id": {"type": "STRING", "description": "معرّف الملف للـ read"},
        },
        "required": ["action"],
    },
}

RECENT = [
    ("1IG1VK8ydwGG2KaIo3FJpUR0Wms_OcTJ6", "VID-20261030-WA0036.mp4", "video/mp4", "2026-10-31"),
    ("1qE7ipGD7aR5oJqMlCAcJgLvykONF_2_P", "IMG-20261030-WA0035.jpg", "image/jpeg", "2026-10-31"),
    ("1bQgpeIWD7ipU55Bngrc1qT1cGw1Q-eqZ", "IMG-20261030-WA0034.jpg", "image/jpeg", "2026-10-31"),
    ("18vXZSgyawk3A4XVq4EfZR_tKHbZowr-R", "IMG-20261030-WA0033.jpg", "image/jpeg", "2026-10-31"),
    ("1WJ8N8XyhKXPceOTNuzDraYsyxls4gnSg", "IMG-20261030-WA0032.jpg", "image/jpeg", "2026-10-31"),
]

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "list").strip().lower()
        if action in ("help","?"):
            return "سيدي، معرض Drive:\n• list — آخر 5 ملفات (صور 30-10-2026 + فيديو)\n• read file_id=\"...\" — يقرأ الملف عبر connector\nمتصل: samahadel721@gmail.com — 2203 رسالة، 5 ملفات حديثة"
        if action in ("list","قائمة"):
            lines=[f"سيدي، آخر {len(RECENT)} ملفات Drive (2026-10-31):"]
            for fid,name,mime,dt in RECENT:
                lines.append(f"  • {name} ({mime}) — {dt} — id={fid[:12]}...")
            lines.append("قل drive_gallery action=read file_id=\"...\" لقراءة ملف")
            return "\n".join(lines)
        if action in ("read","افتح","load"):
            fid = str(p.get("file_id") or "").strip()
            if not fid:
                return "سيدي، file_id= معرّف الملف — خذه من list"
            # لا نقرأ تلقائياً — نعطي تعليمات
            return f"سيدي، لقراءة {fid} سأستخدم google_drive read_file_text — قل لي وسيُجلب فوراً (لا تحميل تلقائي)"
        return f"سيدي، '{action}' غير معروف — list/read"
    except Exception as e:
        return f"سيدي، drive_gallery واجه مشكلة: {e}"
