"""
agi_core.py — نواة AGI العملية فوق LVII (ليس AGI خارق، بل Agent عام)

تجمع: التخطيط الهرمي + الذاكرة طويلة المدى + التفكير النقدي + التحقق البرمجي + التعلم من التجربة
مبنية فوق ما لدينا: background_tasks + deep_research_v2 + routines + file_processor_plus

  agi_core action=think goal="صمم متجر إلكتروني للكتب" → يفكك إلى خطة شجرة + يوزع على background_tasks
  agi_core action=reflect → يراجع آخر المهام: ماذا نجح/فشل ولماذا
  agi_core action=learn task_id="..." → يحفظ درس مستفاد في الذاكرة الدائمة
  agi_core action=status → ذاكرة AGI + مهام نشطة + دروس

مبدأ AGI هنا: لا ينتظر أمراً مفصلاً — يحدد هو الخطوات الفرعية ويصحح نفسه.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

PLUGIN = {
    "name": "agi_core",
    "description": (
        "AGI-like orchestrator: breaks a high-level goal into a tree of sub-tasks, runs them via background_tasks, "
        "reflects on results and learns. Use for: 'فكر في ...', 'خطط لمشروع ...', 'راجع ما تعلمته', 'agi_core think/reflect/learn/status'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "think | reflect | learn | status | help"},
            "goal": {"type": "STRING", "description": "الهدف العام للـ think (مثال: ابنِ نظام حجز)"},
            "task_id": {"type": "STRING", "description": "لـ learn: معرّف المهمة لاستخلاص الدرس"},
        },
        "required": ["action"],
    },
}

AGI_MEMORY = Path(__file__).resolve().parent.parent / "memory" / "agi_memory.json"
AGI_LESSONS = Path(__file__).resolve().parent.parent / "memory" / "agi_lessons.jsonl"

def _load_memory() -> dict:
    try:
        if AGI_MEMORY.exists():
            return json.loads(AGI_MEMORY.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"goals": [], "learnings": [], "created": time.time()}

def _save_memory(data: dict) -> None:
    try:
        AGI_MEMORY.parent.mkdir(parents=True, exist_ok=True)
        AGI_MEMORY.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        import os; os.chmod(AGI_MEMORY, 0o600)
    except Exception:
        pass

def _append_lesson(lesson: dict) -> None:
    try:
        AGI_LESSONS.parent.mkdir(parents=True, exist_ok=True)
        with AGI_LESSONS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(lesson, ensure_ascii=False) + "\n")
        import os; os.chmod(AGI_LESSONS, 0o600)
    except Exception:
        pass

def _decompose(goal: str) -> list[str]:
    """تفكيك بدائي هرمي — يستخدم LLM الحقيقي عند توفره، وإلا قواعد."""
    g = goal.strip()
    # حاول عبر gemini ladder أولاً
    try:
        from core import gemini
        prompt = (
            f"قسّم هذا الهدف الكبير إلى 3-5 خطوات فرعية مرتبة، كل خطوة جملة واحدة واضحة:\n"
            f"الهدف: {g}\n"
            f"أجب كقائمة مرقمة فقط، بلا مقدمات."
        )
        out = gemini.text(prompt, tier=gemini.SMART, timeout_ms=20_000)
        if out and len(out) > 20:
            lines = [l.strip(" 0123456789.-•) ") for l in out.splitlines() if l.strip()]
            steps = [l for l in lines if len(l) > 8][:5]
            if steps:
                return steps
    except Exception:
        pass
    # fallback: قواعد عامة
    if "متجر" in g or "shop" in g.lower() or "store" in g.lower():
        return ["حلّل المتطلبات والجمهور", "صمّم قاعدة البيانات والواجهات", "ابنِ الواجهة الأمامية", "ابنِ الخلفية والدفع", "اختبر وأطلق"]
    if "بحث" in g or "research" in g.lower():
        return ["حدد الأسئلة الفرعية", "اجمع المصادر واقرأها كاملة", "قارن المصادر وحدد التضارب", "اكتب التقرير الموثق", "راجع الاقتباسات برمجياً"]
    # عام
    return [f"حلّل: {g}", f"خطط: {g}", f"نفّذ: {g}", f"تحقق: {g}", f"وثّق: {g}"]

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?","مساعدة"):
            return (
                "سيدي، نواة AGI العملية:\n"
                "• think goal=\"...\" — يفكك الهدف إلى شجرة مهام ويُشغّلها في الخلفية\n"
                "• reflect — يراجع آخر المهام: نجح/فشل ولماذا\n"
                "• learn task_id=\"...\" — يحفظ درساً في الذاكرة الدائمة\n"
                "• status — ذاكرة AGI + الدروس + المهام النشطة\n"
                "هذا ليس AGI خارق — بل Agent عام يخطط ويتعلم ويصحح نفسه فوق LVII"
            )
        if action in ("status","حالة"):
            mem = _load_memory()
            try:
                from core.tasks.store import TaskStore
                n = len(TaskStore().list_tasks())
            except Exception:
                n = "?"
            lessons = 0
            try:
                if AGI_LESSONS.exists():
                    lessons = len([l for l in AGI_LESSONS.read_text(encoding="utf-8").splitlines() if l.strip()])
            except Exception:
                pass
            return (
                f"سيدي، AGI Core: {len(mem.get('goals',[]))} هدف محفوظ، {lessons} درس، {n} مهمة نشطة\n"
                f"الذاكرة: memory/agi_memory.json (0600) — الدروس: memory/agi_lessons.jsonl"
            )
        if action in ("think","فكر","خطط","plan"):
            goal = str(p.get("goal") or "").strip()
            if not goal:
                return "سيدي، goal= الهدف الكبير — مثال: agi_core action=think goal=\"ابنِ نظام حجز مواعيد\""
            steps = _decompose(goal)
            # احفظ الهدف
            mem = _load_memory()
            mem.setdefault("goals", []).append({"goal": goal, "steps": steps, "ts": time.time()})
            mem["goals"] = mem["goals"][-20:]  # آخر 20
            _save_memory(mem)
            # شغّل كل خطوة كـ background task (LVII Engine)
            tids=[]
            try:
                from core.tasks.engine import TaskEngine
                from pathlib import Path as _P
                # استخدم Engine الحقيقي إن كان متاحاً، وإلا TaskStore fallback
                try:
                    engine = TaskEngine(root=_P(__file__).resolve().parent.parent, reports_dir=_P(__file__).resolve().parent.parent/"reports", agents_dirs=[_P(__file__).resolve().parent.parent/"agents"], max_parallel=2)
                    for i, step in enumerate(steps, 1):
                        info = engine.start(task=f"[AGI] {goal} — خطوة {i}/{len(steps)}: {step}", agent="general")
                        tids.append(info.get("id", f"task-{i}"))
                except Exception:
                    from core.tasks.store import TaskStore
                    store = TaskStore()
                    for i, step in enumerate(steps, 1):
                        tid = store.create(title=f"[AGI] خطوة {i}: {step}", goal=step)
                        tids.append(tid)
            except Exception as e:
                return f"سيدي، فككت الهدف إلى {len(steps)} خطوات لكن التشغيل فشل: {e}\nخطواتي:\n" + "\n".join(f"  {i}. {s}" for i,s in enumerate(steps,1))
            lines=[f"سيدي، ⚡ فككت \"{goal}\" إلى {len(steps)} خطوة وشغّلتها في الخلفية:"]
            for i, s in enumerate(steps,1):
                lines.append(f"  {i}. {s} → {tids[i-1] if i-1 < len(tids) else 'pending'}")
            lines.append("تابع من TASKS panel — قل agi_core action=reflect بعد الانتهاء للمراجعة")
            # اعرض في الهود إن أمكن
            try:
                if player and hasattr(player, "show_content"):
                    player.show_content(f"AGI: {goal[:30]}", "\n".join(lines))
            except Exception:
                pass
            return "\n".join(lines)
        if action in ("reflect","راجع","review"):
            mem = _load_memory()
            try:
                from core.tasks.store import TaskStore
                tasks = TaskStore().list_tasks()
                # صنّف حسب الحالة
                by_status={}
                for t in tasks:
                    by_status.setdefault(getattr(t,"status","?"), []).append(t)
                lines=[f"سيدي، مراجعة AGI — {len(tasks)} مهمة:"]
                for st, lst in by_status.items():
                    lines.append(f"  {st}: {len(lst)}")
                # آخر أهداف
                if mem.get("goals"):
                    lines.append(f"آخر هدف: {mem['goals'][-1]['goal'][:60]}")
                if not tasks:
                    lines.append("لا مهام لمراجعتها بعد")
                lines.append("قل agi_core action=learn task_id=\"...\" لحفظ درس من مهمة محددة")
                return "\n".join(lines)
            except Exception as e:
                return f"سيدي، تعذر المراجعة: {e}"
        if action in ("learn","تعلم","درس"):
            tid = str(p.get("task_id") or "").strip()
            if not tid:
                return "سيدي، task_id= معرّف المهمة — خذه من TASKS أو agi_core action=reflect"
            try:
                from core.tasks.store import TaskStore
                t = TaskStore().get(tid)
                if not t:
                    return f"سيدي، لا مهمة {tid}"
                lesson = {
                    "task_id": tid,
                    "title": getattr(t, "title", ""),
                    "status": getattr(t, "status", ""),
                    "goal": getattr(t, "goal", "")[:300],
                    "ts": time.time(),
                }
                # استخلص درساً عبر gemini إن أمكن
                try:
                    from core import gemini
                    prompt = f"استخلص درساً واحداً مفيداً (جملة واحدة) من هذه المهمة:\nالعنوان: {lesson['title']}\nالحالة: {lesson['status']}\nالهدف: {lesson['goal']}"
                    out = gemini.text(prompt, tier=gemini.FAST, timeout_ms=12_000)
                    if out:
                        lesson["insight"] = out.strip()[:300]
                except Exception:
                    pass
                _append_lesson(lesson)
                mem = _load_memory()
                mem.setdefault("learnings", []).append(lesson)
                mem["learnings"] = mem["learnings"][-50:]
                _save_memory(mem)
                return f"سيدي، حفظت درساً من {tid} [{lesson['status']}]: {lesson.get('insight', lesson['title'][:60])}"
            except Exception as e:
                return f"سيدي، تعذر الحفظ: {e}"
        return f"سيدي، '{action}' غير معروف — think/reflect/learn/status"
    except Exception as e:
        return f"سيدي، agi_core واجه مشكلة: {e}"
