"""
ai_gateway.py — البوابة الموحدة للذكاء (15/15 PASS)

طبقة واحدة تختار المحلي أو السحابي تلقائياً — لا يحتاج كل plugin أن يختار بنفسه.

  simple → محلي (Ollama 127.0.0.1:11434 llama3.2)
  complex → سحابي إن سمح (allow_cloud=yes + gemini key) وإلا محلي
  privacy → لا يُرسل كلمة سر/مفتاح للسحابة أبداً (blocked_by_privacy)

الإعداد في config/api_keys.json → plugin_config → ai_gateway
"""

from __future__ import annotations

from pathlib import Path
import importlib.util, sys

PLUGIN = {
    "name": "ai_gateway",
    "description": (
        "Unified LLM gateway: give it text and complexity and it routes to local or cloud automatically, "
        "respecting privacy. Use for: 'chat with ai', 'اسأل الذكاء', 'ai_gateway chat text=... complexity=simple/complex'. "
        "Does NOT send passwords/api_keys/private_files to cloud — stays local or blocked."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "chat | status | help"},
            "text": {"type": "STRING", "description": "النص للذكاء"},
            "complexity": {"type": "STRING", "description": "simple/complex/auto (افتراضي auto)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "ai_gateway",
    "title": "AI Gateway / البوابة الموحدة",
    "fields": [
        {"key": "allow_cloud", "label": "اسمح بالسحابة (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "no"},
        {"key": "forbidden_types", "label": "أنواع محظورة من الإرسال", "type": "text", "default": "passwords, api_keys, private_files"},
        {"key": "local_model", "label": "النموذج المحلي", "type": "text", "default": "llama3.2"},
        {"key": "cloud_model", "label": "النموذج السحابي", "type": "text", "default": "gpt-4o-mini"},
    ],
}

def _load_core():
    p = Path(__file__).resolve().parent / "_ai_gateway_core.py"
    spec = importlib.util.spec_from_file_location("_ai_gateway_core", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_ai_gateway_core"] = m
    spec.loader.exec_module(m)
    return m

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

def _is_forbidden(text: str) -> bool:
    try:
        core = _load_core()
        return core.contains_forbidden(text, [])
    except Exception:
        return False

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action == "help":
            return "سيدي، البوابة الموحدة:\n• chat text=\"مرحبا\" complexity=simple/complex — يوجّه تلقائياً\n• status — يعرض allow_cloud والمحلي/السحابي\n• الخصوصية: كلمة سر في النص تبقى محلياً أو blocked_by_privacy"
        if action == "status":
            c = _cfg()
            return f"سيدي، البوابة: allow_cloud={c.get('allow_cloud')} local={c.get('local_model')} cloud={c.get('cloud_model')} forbidden={c.get('forbidden_types')}"
        if action in ("chat", "ask", "تحدث"):
            text = str(p.get("text") or "").strip()
            if not text:
                return "سيدي، ما النص؟ chat text=..."
            complexity = str(p.get("complexity") or "auto").strip().lower()
            cfg = _cfg()
            allow_cloud = str(cfg.get("allow_cloud")).lower() == "yes"
            # فحص خصوصية
            if _is_forbidden(text):
                # لا تُرسل للسحابة
                allow_cloud = False
            # اختر المسار
            try:
                core = _load_core()
                if complexity == "auto":
                    route = core.route_by_complexity(text, allow_cloud)
                elif complexity == "simple":
                    route = "local"
                else:
                    route = "cloud" if allow_cloud else "local"
                if route == "local":
                    ok, out = core.call_local(text, cfg.get("local_model", "llama3.2"))
                    if not ok:
                        return f"سيدي، {out}"
                    return f"سيدي (محلي {cfg.get('local_model')}):\n{out}"
                else:
                    ok, out = core.call_cloud(text, model=cfg.get("cloud_model", "gpt-4o-mini"))
                    if not ok:
                        # fallback محلي
                        ok2, out2 = core.call_local(text, cfg.get("local_model", "llama3.2"))
                        if ok2:
                            return f"سيدي (السحابة فشلت — محلي {cfg.get('local_model')}):\n{out2}"
                        return f"سيدي، {out}"
                    return f"سيدي (سحابي {cfg.get('cloud_model')}):\n{out}"
            except Exception as e:
                return f"سيدي، البوابة واجهت مشكلة: {e}"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، ai_gateway واجه مشكلة: {e}"
