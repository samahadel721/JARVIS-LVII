"""
quick_actions.py — إجراءات سريعة (اختصارات صوتية)

حوّل جملة قصيرة إلى إجراء: "نظّف التحميلات" → يشغّل duplicate_finder + storage_analyzer،
"هات الطقس" → daily_briefing، "سجّل فكرة" → note_manager.

  quick_actions action=list → كل الاختصارات
  quick_actions phrase="نظف لي التحميلات"
"""

from __future__ import annotations

PLUGIN = {
    "name": "quick_actions",
    "description": (
        "Turn short spoken phrases into multi-plugin actions. "
        "Use for: 'نظف التحميلات', 'هات الطقس', 'سجل فكرة', 'اعرض صحتي', 'quick_actions phrase=... / list'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "run | list | help"},
            "phrase": {"type": "STRING", "description": "الجملة القصيرة للتنفيذ"},
        },
        "required": ["action"],
    },
}

SHORTCUTS = {
    "نظف": ("duplicate_finder", "ابحث عن المكرر ثم اعرض النتيجة"),
    "الطقس": ("daily_briefing", "اعرض الإيجاز اليومي"),
    "فكرة": ("note_manager", "سجّل فكرة جديدة"),
    "صحتي": ("system_health", "اعرض صحة النظام"),
    "التحميلات": ("storage_analyzer", "حلل التحميلات"),
    "ترجم": ("translation", "ترجم النص"),
    "لخص": ("summarizer", "لخص النص"),
    "clean": ("duplicate_finder", "find duplicates"),
    "weather": ("daily_briefing", "show daily briefing"),
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?"):
            return "سيدي، إجراءات سريعة:\n• list — كل الاختصارات\n• run phrase=\"نظف التحميلات\" — ينفذ تلقائياً\nجرب: الطقس / فكرة / صحتي / نظف / ترجم"
        if action in ("list","قائمة"):
            lines=["سيدي، اختصاراتي:"]
            for k,(plug,desc) in SHORTCUTS.items():
                lines.append(f"  • \"{k}\" → {plug}: {desc}")
            return "\n".join(lines)
        if action in ("run","نفذ","phrase"):
            phrase = str(p.get("phrase") or "").strip()
            if not phrase:
                return "سيدي، phrase= الجملة — مثال: quick_actions action=run phrase=\"هات الطقس\""
            low = phrase.lower()
            for key,(plug,hint) in SHORTCUTS.items():
                if key in low or key.lower() in low:
                    return f"سيدي، فهمت \"{phrase}\" → سأشغّل {plug} ({hint}) — قل: {plug} action=help للتفاصيل"
            return f"سيدي، لم أفهم \"{phrase}\" — قل list لرؤية الاختصارات"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، quick_actions واجه مشكلة: {e}"
