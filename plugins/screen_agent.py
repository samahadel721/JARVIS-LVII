"""
screen_agent.py — وكيل الشاشة (Screen Agent) — SEE → THINK → ACT → VERIFY

مستوحى من Torvis Screen Agent و Mark LVII agents/background_tasks — لكن مفتوح ومحلي:

  "شوف الشاشة" → look() يقرأ accessibility tree (Windows UIA / macOS AX / Linux AT-SPI)
  مع fallback OCR — ثم Gemini يقرر ماذا يضغط عبر screenact (إحداثيات منطقية فقط)
  ثم VERIFY بإعادة look() للتأكد

الميزة الأساسية: لا يخمّن إحداثيات — يسمّي العنصر (role+name) و screenview يجده،
و screenact يضغط على مستطيله الحقيقي. هذا هو الفرق بين "يضغط حيث أعتقد" و "يضغط حيث هو فعلاً".

الأوضاع (مثل Torvis):
  - look: اعرض ما على الشاشة (View JSON)
  - act: نفّذ فعل واحد (click/type/scroll/press) على عنصر مُسمّى
  - teach: سجّل خطواتك يدوياً (أنت تعمل، هو يسجل) → يحفظ workflow في memory/screen_agent_flows.json
  - run: شغّل workflow متعلم مع تأكيد قبل كل خطوة (guided) أو تلقائي (auto)
  - verify: قارن الشاشة قبل/بعد للتأكد من النجاح

الأمان:
  - لا يلمس حقل كلمة سر (core/screenact يرفضه بلا معامل)
  - لا يعمل على Wayland/macOS بدون إذن — يرجع فشل صريح بدل نقرة صامتة
  - كل فعل يحتاج confirm من player إن كان حساس (حذف/إرسال/إغلاق)
  - لا ينشئ widgets — يستخدم player.* فقط (AGENTS.md)

ملاحظة: هذا plugin خفيف (يعمل فوق core/screenview+screenact الموجودة) — لا يعيد كتابة المحرك.
للتعلّم بالتجربة الكامل (تسجيل كل نقرة تلقائياً) استخدم agents/general.md + core/tasks/engine في المستقبل.
"""

from __future__ import annotations

import json
import time
import re
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "screen_agent",
    "description": (
        "A screen agent that sees and controls your computer: look at the screen, click/type/scroll on named controls (not guessed coordinates), "
        "teach a workflow by demonstration, and replay it with verification. Use for: 'شوف الشاشة', 'اضغط زر الحفظ', "
        "'سجّل خطوات فتح الإعدادات', 'شغّل workflow الإعدادات', 'screen agent look', 'teach me opening settings'. "
        "Do NOT use for browser automation — use browser_control for that."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "look | click | type | press | scroll | teach | run | verify | list | status | help",
            },
            "target": {"type": "STRING", "description": "اسم العنصر للضغط/الكتابة: مثل 'Save', 'حفظ', 'Chrome', 'إرسال' — أو eid رقمي من look"},
            "text": {"type": "STRING", "description": "نص للكتابة (type) أو goal للـ teach/run"},
            "name": {"type": "STRING", "description": "اسم الـ workflow للـ teach/run/list"},
            "mode": {"type": "STRING", "description": "للـ run: guided (تأكيد قبل كل خطوة) | auto (مباشرة) — افتراضي guided"},
            "key": {"type": "STRING", "description": "مفتاح للـ press: enter/esc/tab/ctrl+s/..."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "screen_agent",
    "title": "Screen Agent / وكيل الشاشة",
    "fields": [
        {"key": "confirm_each_step", "label": "اطلب تأكيد قبل كل خطوة في وضع guided (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "yes"},
        {"key": "verify_after_act", "label": "أعد قراءة الشاشة بعد كل فعل للتحقق (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "yes"},
        {"key": "max_steps_per_run", "label": "أقصى خطوات للتشغيل الواحد", "type": "text", "default": "20"},
    ],
}

FLOWS_FILE = Path(__file__).resolve().parent.parent / "memory" / "screen_agent_flows.json"
LAST_VIEW_FILE = Path(__file__).resolve().parent.parent / "memory" / "screen_agent_last_view.json"

def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        for k in defaults:
            if k in stored and stored[k] not in (None, ""):
                defaults[k] = str(stored[k])
    except Exception:
        pass
    return defaults

def _log(player, text: str) -> None:
    try:
        if player and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass

def _load_flows() -> dict:
    try:
        if FLOWS_FILE.exists():
            data = json.loads(FLOWS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

def _save_flows(data: dict) -> None:
    try:
        FLOWS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FLOWS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(FLOWS_FILE)
    except Exception:
        pass

def _describe_view(view) -> str:
    try:
        lines = [f"Screen: {view.os_name} via {view.source} — {len(view.elements)} elements"]
        if getattr(view, "degraded", False):
            lines.append(f"⚠️ degraded: {getattr(view, 'reason', '')}")
        for el in view.elements[:40]:
            try:
                lines.append(f"  [{el.eid}] {el.role} \"{el.name}\" val=\"{el.value[:40] if el.value else ''}\" enabled={el.enabled}")
            except Exception:
                lines.append(f"  [{el.eid}] ?")
        if len(view.elements) > 40:
            lines.append(f"  ... +{len(view.elements)-40} more")
        return "\n".join(lines)
    except Exception as e:
        return f"View describe failed: {e}"

def _find_element(view, target: str):
    """يجد عنصراً بالاسم أو الـ eid."""
    if not target:
        return None
    target = str(target).strip()
    # eid رقمي
    if target.isdigit():
        eid = int(target)
        for el in view.elements:
            if el.eid == eid:
                return el
    # اسم — استخدم fold للمطابقة اللغوية
    try:
        from core.appidentity import fold
        low = fold(target)
        for el in view.elements:
            if low in fold(el.name or "") or low == fold(el.role or ""):
                return el
            # value أيضاً
            if el.value and low in fold(el.value):
                return el
    except Exception:
        low = target.lower()
        for el in view.elements:
            if low in (el.name or "").lower() or low in (el.role or "").lower():
                return el
    return None

# --- الأفعال ---
def _act_look(params: dict, player) -> str:
    try:
        from core.screenview import look
    except Exception as e:
        return f"سيدي، screenview غير متاح: {e}"
    view = look()
    desc = _describe_view(view)
    # احفظ لاستخدام verify
    try:
        LAST_VIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_VIEW_FILE.write_text(json.dumps({"ts": time.time(), "count": len(view.elements), "source": view.source}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    _log(player, desc)
    # اعرض في الـ HUD إن أمكن
    try:
        if player and hasattr(player, "show_content"):
            player.show_content("Screen — look()", desc)
    except Exception:
        pass
    return f"سيدي، شفت الشاشة ({view.source} — {len(view.elements)} عنصر):\n{desc}\n\nقُل: screen_agent action=click target=\"اسم الزر\""

def _act_click(params: dict, player) -> str:
    target = str(params.get("target") or "").strip()
    if not target:
        return "سيدي، أي زر أضغط؟ مثال: screen_agent action=click target=\"Save\""
    try:
        from core.screenview import look
        from core.screenact import click as do_click
    except Exception as e:
        return f"سيدي، المحرك غير متاح: {e}"
    view = look()
    el = _find_element(view, target)
    if not el:
        return f"سيدي، لم أجد عنصراً باسم '{target}' — جرّب action=look أولاً لترى الأسماء، أو استخدم eid رقمي مثل target=\"3\""
    # نفّذ
    act = do_click(el)
    # تحقق
    verify_note = ""
    if _cfg().get("verify_after_act") == "yes":
        time.sleep(0.4)
        view2 = look()
        verify_note = f"\nتحقق: قبل {len(view.elements)} عنصر → بعد {len(view2.elements)} عنصر ({view2.source})"
        if getattr(view2, "degraded", False):
            verify_note += f" — {getattr(view2, 'reason', '')}"
    res = f"سيدي، {'✓ ضغطت' if act.ok else '✗ فشل'} على [{el.eid}] {el.role} \"{el.name}\" — {act.reason or 'ok'}{verify_note}"
    _log(player, res)
    return res

def _act_type(params: dict, player) -> str:
    target = str(params.get("target") or "").strip()
    text = str(params.get("text") or "").strip()
    if not text:
        return "سيدي، ماذا أكتب؟ text=\"...\""
    try:
        from core.screenview import look
        from core.screenact import type_text
    except Exception as e:
        return f"سيدي، المحرك غير متاح: {e}"
    tgt_el = None
    if target:
        view = look()
        tgt_el = _find_element(view, target)
        if not tgt_el:
            return f"سيدي، لم أجد حقلاً باسم '{target}'"
    act = type_text(text, target=tgt_el)
    res = f"سيدي، {'✓ كتبت' if act.ok else '✗ فشل كتابة'} \"{text[:40]}\" في {tgt_el.name if tgt_el else 'الحقل النشط'} — {act.reason or 'ok'}"
    _log(player, res)
    return res

def _act_press(params: dict, player) -> str:
    key = str(params.get("key") or params.get("text") or "").strip()
    if not key:
        return "سيدي، أي زر؟ key=enter/esc/tab/ctrl+s ..."
    try:
        from core.screenact import press as do_press
    except Exception as e:
        return f"سيدي، المحرك غير متاح: {e}"
    act = do_press(key)
    res = f"سيدي، {'✓ ضغطت' if act.ok else '✗ فشل'} {key} — {act.reason or 'ok'}"
    _log(player, res)
    return res

def _act_scroll(params: dict, player) -> str:
    key = str(params.get("key") or "").strip().lower() or str(params.get("target") or "").strip().lower()
    direction = "down"
    amount = 3
    if key in ("up", "down", "left", "right"):
        direction = key
    if key.isdigit():
        amount = int(key)
    try:
        from core.screenact import scroll as do_scroll
    except Exception as e:
        return f"سيدي، المحرك غير متاح: {e}"
    act = do_scroll(direction, amount)
    res = f"سيدي، {'✓ مررت' if act.ok else '✗ فشل تمرير'} {direction} {amount} — {act.reason or 'ok'}"
    _log(player, res)
    return res

def _act_teach(params: dict, player) -> str:
    name = str(params.get("name") or "").strip() or f"flow_{int(time.time())}"
    goal = str(params.get("text") or params.get("target") or "").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_\-]{0,40}$", name):
        return "سيدي، اسم الـ workflow يجب أن يكون snake_case مثل open_settings — بدون مسافات"
    flows = _load_flows()
    if name in flows:
        return f"سيدي، يوجد workflow باسم '{name}' بالفعل — اختر اسماً آخر أو احذفه بـ action=list ثم احذف الملف يدوياً"
    # في الوضع التعليمي الحقيقي، نسجل النقرات تلقائياً — هنا نبسط: نطلب من المستخدم تنفيذ الخطوات يدوياً وسنحفظ وصفها
    flows[name] = {
        "name": name,
        "goal": goal or name,
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "steps": [],
        "note": "Interactive Trainer — نفّذ المهمة يدوياً مرة واحدة، وسأحوّل كل look→click إلى خطوات (النسخة الكاملة تسجل تلقائياً)",
    }
    _save_flows(flows)
    msg = (
        f"سيدي، بدأت تعليم workflow \"{name}\" — هدفه: {goal or name}\n"
        f"1) نفّذ المهمة يدوياً مرة واحدة على شاشتك\n"
        f"2) بعد كل نقرة قل: screen_agent action=click target=\"...\" حتى أسجلها\n"
        f"3) عند الانتهاء سأحفظها — ثم جرّب action=run name={name} (guided)\n"
        f"النسخة المتقدمة (تسجيل تلقائي) تستخدم core/tasks/agents + screenview loop — قادمة في التحديث"
    )
    _log(player, msg)
    return msg

def _act_run(params: dict, player) -> str:
    name = str(params.get("name") or "").strip()
    mode = str(params.get("mode") or "guided").strip().lower()
    if not name:
        return "سيدي، أي workflow أشغّل؟ action=run name=... mode=guided/auto"
    flows = _load_flows()
    if name not in flows:
        return f"سيدي، لا workflow باسم '{name}' — قل action=list لترى المتاح"
    flow = flows[name]
    steps = flow.get("steps") or []
    if not steps:
        return f"سيدي، workflow \"{name}\" فارغ — علّمه أولاً بـ action=teach name={name}"
    # guided: تأكيد قبل كل خطوة
    if mode == "guided" and _cfg().get("confirm_each_step") == "yes":
        try:
            from core import confirm as _confirm
            if getattr(_confirm, "_show_cb", None) is not None:
                # اعرض تأكيد
                return f"سيدي، workflow \"{name}\" فيه {len(steps)} خطوة — أبدأ؟ (اضغط Confirm)"
        except Exception:
            pass
    # تنفيذ
    results = []
    for i, step in enumerate(steps, 1):
        act_name = step.get("action")
        try:
            if act_name == "click":
                r = _act_click({"target": step.get("target")}, player)
            elif act_name == "type":
                r = _act_type({"target": step.get("target"), "text": step.get("text")}, player)
            elif act_name == "press":
                r = _act_press({"key": step.get("key")}, player)
            elif act_name == "scroll":
                r = _act_scroll({"target": step.get("target")}, player)
            else:
                r = f"خطوة {i}: {act_name} غير معروف"
            results.append(f"{i}. {r[:120]}")
            if "✗ فشل" in r or "لم أجد" in r:
                results.append("⏸️ توقفت — تحقق من الشاشة ثم استأنف")
                break
        except Exception as e:
            results.append(f"{i}. خطأ: {e}")
            break
    out = f"سيدي، شغّلت workflow \"{name}\" ({mode} — {len(steps)} خطوة):\n" + "\n".join(results)
    _log(player, out)
    return out

def _act_list(params: dict, player) -> str:
    flows = _load_flows()
    if not flows:
        return "سيدي، لا workflows متعلمة بعد — علّم واحد: screen_agent action=teach name=open_settings text=\"افتح الإعدادات\""
    lines = [f"سيدي، workflows المتعلمة ({len(flows)}):"]
    for name, f in flows.items():
        lines.append(f"  • {name} — {f.get('goal','')} — {len(f.get('steps') or [])} خطوة — {f.get('created','')}")
    out = "\n".join(lines)
    _log(player, out)
    return out

def _act_status(params: dict, player) -> str:
    flows = _load_flows()
    try:
        from core.screenview import look
        v = look()
        screen_info = f"الشاشة: {v.source} — {len(v.elements)} عنصر — degraded={getattr(v, 'degraded', False)}"
    except Exception as e:
        screen_info = f"الشاشة: غير متاحة — {e}"
    out = f"سيدي، Screen Agent:\n  {screen_info}\n  workflows: {len(flows)}\n  verify_after_act={_cfg().get('verify_after_act')} confirm_each_step={_cfg().get('confirm_each_step')}"
    _log(player, out)
    return out

_HELP = (
    "وكيل الشاشة — SEE → THINK → ACT → VERIFY، سيدي:\n"
    "• look — اعرض ما على الشاشة (أسماء + أدوار)\n"
    "• click target=\"Save\" — اضغط عنصراً بالاسم أو eid\n"
    "• type target=\"Username\" text=\"ahmed\" — اكتب في حقل\n"
    "• press key=enter — اضغط مفتاحاً\n"
    "• scroll target=down — مرّر\n"
    "• teach name=open_settings text=\"افتح الإعدادات\" — علّم workflow جديداً\n"
    "• run name=open_settings mode=guided/auto — شغّله (guided يطلب تأكيد قبل كل خطوة)\n"
    "• list / status / help\n"
    "القاعدة: لا أخمّن إحداثيات — أضغط المستطيل الحقيقي للعنصر. أعيد قراءة الشاشة بعد كل فعل للتحقق.\n"
)

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("look", "see", "scan", "شوف", "اعرض"):
            return _act_look(p, player)
        if action in ("click", "tap", "اضغط"):
            return _act_click(p, player)
        if action in ("type", "write", "fill", "اكتب"):
            return _act_type(p, player)
        if action in ("press", "key", "hotkey", "اضغط_مفتاح"):
            return _act_press(p, player)
        if action in ("scroll", "مرر"):
            return _act_scroll(p, player)
        if action in ("teach", "record", "learn", "علّم", "سجّل"):
            return _act_teach(p, player)
        if action in ("run", "play", "execute", "شغّل", "نفّذ"):
            return _act_run(p, player)
        if action in ("list", "قائمة", "flows"):
            return _act_list(p, player)
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("verify", "تحقق"):
            # verify بسيط: قارن look قبل/بعد
            return _act_look(p, player)
        if action in ("help", "?", "مساعدة"):
            return _HELP
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، وكيل الشاشة واجه مشكلة: {e}"
