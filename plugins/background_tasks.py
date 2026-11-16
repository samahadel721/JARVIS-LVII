"""
background_tasks.py — مهام الخلفية (LVII) — تشتغل جنب المحادثة

LVII ينقل الشغل الطويل خارج المحادثة: checkpoint كل جولة، يستأنف بعد إعادة التشغيل،
يتوقف/ينتهي مبكراً/يغيّر مساره بصوتك. هذه الإضافة تتحكم فيه من الشات.

  background_tasks action=list → كل المهام
  background_tasks action=cancel id=... finish=yes/no
  background_tasks action=steer id=... text="غيّر إلى التركيز على..."
  background_tasks action=create goal="اكتب مسودة..." agent=general/researcher
"""

from __future__ import annotations

PLUGIN = {
    "name": "background_tasks",
    "description": (
        "Control long-running background tasks that run beside the conversation — checkpointed every round, resumed after restart. "
        "Use for: 'اعرض المهام في الخلفية', 'أوقف المهمة', 'غيّر مسار البحث', 'أنشئ مهمة خلفية', 'background tasks list/cancel/steer/create'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | status | cancel | steer | create | help"},
            "id": {"type": "STRING", "description": "معرّف المهمة للـ cancel/steer"},
            "text": {"type": "STRING", "description": "لـ steer: تعليمات جديدة / لـ create: هدف المهمة"},
            "agent": {"type": "STRING", "description": "لـ create: general/researcher (افتراضي general)"},
            "finish": {"type": "STRING", "description": "لـ cancel: yes=اكتب تقرير الآن, no=أوقف بدون تقرير"},
        },
        "required": ["action"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "list").strip().lower()
        if action in ("help","?","مساعدة"):
            return "سيدي، مهام الخلفية:\n• list — كل المهام\n• create text=\"...\" agent=general/researcher — أنشئ مهمة جديدة\n• cancel id=... finish=yes/no — أوقف (مع تقرير أو بدون)\n• steer id=... text=\"...\" — غيّر مسار مهمة جارية\n• status id=... — تفاصيل مهمة"
        try:
            from core.tasks.store import TaskStore
            from core.tasks.engine import cancel_task, steer_task, create_task
            store = TaskStore()
        except Exception as e:
            return f"سيدي، محرك المهام غير متاح: {e}"
        if action in ("list","قائمة"):
            tasks = store.list_tasks()
            if not tasks:
                return "سيدي، لا مهام في الخلفية"
            lines=[f"سيدي، مهام الخلفية ({len(tasks)}):"]
            for t in tasks[:12]:
                turns = len(getattr(t, "turns", []) or [])
                lines.append(f"  • {t.id} [{t.status}] {getattr(t,'agent','?')} — {t.title[:45]} — {turns} جولات")
            return "\n".join(lines)
        if action in ("status","حالة"):
            tid = str(p.get("id") or p.get("name") or "").strip()
            if not tid: return "سيدي، id= معرّف المهمة"
            t = store.get(tid)
            if not t: return f"سيدي، لا مهمة {tid}"
            return f"سيدي، {t.id} [{t.status}] {t.title}\nالهدف: {t.goal[:300]}\nجولات: {len(getattr(t,'turns',[]))} — agent: {getattr(t,'agent','')}"
        if action in ("create","أنشئ"):
            goal = str(p.get("text") or "").strip()
            if not goal: return "سيدي، text= هدف المهمة"
            agent = str(p.get("agent") or "general").strip().lower()
            if agent not in ("general","researcher"): agent="general"
            tid = create_task(goal, agent=agent)
            return f"سيدي، ⚡ أنشأت مهمة خلفية {tid} ({agent}) — \"{goal[:50]}\" — تابع من TASKS panel"
        if action in ("cancel","أوقف","ألغ"):
            tid = str(p.get("id") or "").strip()
            if not tid: return "سيدي، id= معرّف المهمة"
            finish = str(p.get("finish") or "no").strip().lower() in ("yes","true","1","نعم")
            cancel_task(tid, finish=finish)
            return f"سيدي، أرسلت إيقاف لـ {tid} ({'مع تقرير' if finish else 'بدون تقرير'})"
        if action in ("steer","غيّر","وجّه"):
            tid = str(p.get("id") or "").strip()
            txt = str(p.get("text") or "").strip()
            if not tid or not txt: return "سيدي، id= و text= مطلوبان"
            steer_task(tid, txt)
            return f"سيدي، غيّرت مسار {tid} → \"{txt[:60]}\""
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، background_tasks واجه مشكلة: {e}"
