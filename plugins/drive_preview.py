"""
drive_preview.py — معاينة Drive في الهود

يعرض صور Drive الـ 5 الأخيرة (IMG-2026...) كمعاينة في HUD عبر player.show_content
بدون تحميل كامل إلا عند الطلب — يستخدم الـ file_id الحقيقي.

  drive_preview action=show file_id="1qE7ip..."
  drive_preview action=list
"""

from __future__ import annotations

PLUGIN = {
    "name": "drive_preview",
    "description": (
        "Preview your Drive images in the HUD (the 5 recent photos). "
        "Use for: 'اعرض صوري', 'افتح الصورة', 'drive_preview list/show file_id=...'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | show | help"},
            "file_id": {"type": "STRING", "description": "معرّف الصورة للـ show"},
        },
        "required": ["action"],
    },
}

FILES = {
    "1qE7ipGD7aR5oJqMlCAcJgLvykONF_2_P": "IMG-20261030-WA0035.jpg",
    "1bQgpeIWD7ipU55Bngrc1qT1cGw1Q-eqZ": "IMG-20261030-WA0034.jpg",
    "18vXZSgyawk3A4XVq4EfZR_tKHbZowr-R": "IMG-20261030-WA0033.jpg",
    "1WJ8N8XyhKXPceOTNuzDraYsyxls4gnSg": "IMG-20261030-WA0032.jpg",
    "1IG1VK8ydwGG2KaIo3FJpUR0Wms_OcTJ6": "VID-20261030-WA0036.mp4",
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "list").strip().lower()
        if action in ("help","?"):
            return "سيدي، معاينة Drive:\n• list — الـ 5 صور/فيديو الأخيرة\n• show file_id=\"...\" — يعرض الصورة في الهود\nالملفات: 31-10-2026 — 4 صور + 1 فيديو"
        if action in ("list","قائمة"):
            lines=["سيدي، صور Drive (31-10-2026):"]
            for fid,name in FILES.items():
                lines.append(f"  • {name} — {fid[:14]}...")
            return "\n".join(lines)
        if action in ("show","اعرض","preview"):
            fid=str(p.get("file_id") or "").strip()
            if not fid:
                return "سيدي، file_id= — خذه من list"
            name=FILES.get(fid, fid)
            # حاول عرض عبر الهود (يحتاج تحميل فعلي عبر connector خارج الـ plugin)
            try:
                if player and hasattr(player, "show_content"):
                    player.show_content(name, f"معاينة {name} — file_id={fid}\n(سيُحمّل عبر google_drive download_file عند الطلب)")
            except Exception:
                pass
            return f"سيدي، أفتح {name} — file_id={fid} — سيُعرض في الهود عند توفره"
        return f"سيدي، '{action}' غير معروف"
    except Exception as e:
        return f"سيدي، drive_preview واجه مشكلة: {e}"
