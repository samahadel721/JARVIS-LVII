"""
deep_research_v2.py — البحث العميق مع التحقق البرمجي (LVII style)

LVII يقرأ كل مصدر كاملاً، يسجل الاقتباس الحرفي، ويتحقق منه بالكود من الصفحة المحفوظة — لا بالسؤال عن رأي الموديل.
هذه الإضافة تغلف core/tasks/research.py + agents/researcher.md لتوفير نفس الميزة كـ plugin بسيط.

  deep_research_v2 action=research topic="الذكاء الاصطناعي في التعليم" → تقرير موثّق + اقتباسات مُحققة
"""

from __future__ import annotations

PLUGIN = {
    "name": "deep_research_v2",
    "description": (
        "Deep research with code-verified quotes: plans questions, reads sources in full, checks every quote against the saved page in code, and writes a cited report. "
        "Use for: 'بحث عميق عن ...', 'deep research on ... with verified quotes', 'اكتب تقرير موثق'. "
        "Slower but every sentence traces to a page you read — cites are renumbered and [?] marks anything uncited."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "research | status | help"},
            "topic": {"type": "STRING", "description": "السؤال/الموضوع للبحث العميق"},
            "minutes": {"type": "STRING", "description": "ميزانية الوقت بالدقائق (افتراضي 10)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "deep_research_v2",
    "title": "Deep Research V2 / البحث العميق المحقق",
    "fields": [
        {"key": "default_minutes", "label": "وقت البحث الافتراضي (دقائق)", "type": "text", "default": "10"},
        {"key": "verify_quotes", "label": "تحقق من الاقتباسات بالكود (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "yes"},
    ],
}

def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        for k in defaults:
            if k in stored and str(stored[k]).strip():
                defaults[k] = str(stored[k])
    except Exception:
        pass
    return defaults

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help", "?", "مساعدة"):
            return "سيدي، البحث العميق المحقق:\n• research topic=\"...\" minutes=10 — يخطط أسئلة، يقرأ كل مصدر كامل، يتحقق من كل اقتباس بالكود، يكتب تقريراً موثقاً\n• status — يعرض المهام الجارية في الخلفية (TASKS panel)"
        if action == "status":
            try:
                from core.tasks.store import TaskStore
                store = TaskStore()
                tasks = store.list_tasks()
                bg = [t for t in tasks if getattr(t, "agent", "") == "researcher"]
                if not bg:
                    return "سيدي، لا أبحاث جارية"
                lines = [f"سيدي، أبحاث نشطة ({len(bg)}):"]
                for t in bg[:5]:
                    lines.append(f"  • {t.id} [{t.status}] {t.title[:50]} — {len(getattr(t,'turns',[]))} جولات")
                return "\n".join(lines)
            except Exception as e:
                return f"سيدي، تعذر قراءة الحالة: {e}"
        if action in ("research", "بحث", "deep"):
            topic = str(p.get("topic") or "").strip()
            if not topic:
                return "سيدي، ما الموضوع؟ topic=\"...\""
            minutes = str(p.get("minutes") or _cfg().get("default_minutes") or "10").strip()
            try:
                mins = max(2, min(30, int(minutes)))
            except Exception:
                mins = 10
            # استخدم محرك LVII الخلفي
            try:
                from core.tasks.engine import create_task
                tid = create_task(f"Deep research: {topic} — verified quotes, cited report in Arabic. Time budget: {mins} minutes. Read each source in full, verify every quote in code against saved page, renumber cites, [?] for uncited.", agent="researcher")
                return f"سيدي، ⚡ On it — بدأت بحثاً عميقاً عن \"{topic}\" ({mins}د) — رقم المهمة {tid}\nتابع من TASKS panel — كل اقتباس سيُتحقق بالكود قبل التقرير النهائي"
            except Exception as e:
                return f"سيدي، تعذر بدء البحث: {e}"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، deep_research_v2 واجه مشكلة: {e}"
