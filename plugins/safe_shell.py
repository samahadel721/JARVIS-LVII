"""
safe_shell.py — أوامر آمنة فقط (allowlist)

LVII ينفذ أوامر عبر core — هذه الإضافة تسمح فقط بقائمة بيضاء (ls/ps/df/git status/ping)
بدون traversal، بدون rm -rf، بدون sudo. كل أمر يُسجل في memory/shell_log.jsonl.

  safe_shell action=run cmd="ls docs"
  safe_shell action=allowlist → ما المسموح؟
"""

from __future__ import annotations

import subprocess, shlex, time, json
from pathlib import Path

PLUGIN = {
    "name": "safe_shell",
    "description": (
        "Run only allowlisted shell commands safely (no traversal, no sudo, logged). "
        "Use for: 'اعرض ملفات docs', 'حالة النظام', 'safe_shell run cmd=\"ls\" / allowlist'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "run | allowlist | help"},
            "cmd": {"type": "STRING", "description": "الأمر للتشغيل (من القائمة البيضاء فقط)"},
        },
        "required": ["action"],
    },
}

ALLOW = {"ls","pwd","whoami","ps","df","free","uptime","git","ping","echo","cat","head","wc","date","uname"}
LOG_FILE = Path(__file__).resolve().parent.parent / "memory" / "shell_log.jsonl"

def _log(cmd: str, out: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec={"ts": time.time(), "cmd": cmd, "out": out[:2000]}
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)+"\n")
    except Exception:
        pass

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?"):
            return "سيدي، أوامر آمنة:\n• allowlist — ما المسموح؟\n• run cmd=\"ls docs\" — يشغّل فقط من القائمة البيضاء\nممنوع: rm/sudo/chmod/curl خارجي — كل شيء يُسجل"
        if action in ("allowlist","قائمة","مسموح"):
            return f"سيدي، المسموح: {', '.join(sorted(ALLOW))} — مع معاملات آمنة فقط (لا .. ولا | ولا ;)"
        if action in ("run","شغل","نفذ"):
            cmd=str(p.get("cmd") or "").strip()
            if not cmd:
                return "سيدي، cmd= الأمر — مثال: safe_shell action=run cmd=\"ls docs\""
            # منع حقن
            if any(c in cmd for c in [";","|","&","`","$","..","sudo","rm "]):
                return "سيدي، الأمر مرفوض — حقن محتمل (.. ; | sudo rm)"
            parts=shlex.split(cmd)
            if not parts:
                return "سيدي، أمر فارغ"
            base=parts[0].lower()
            if base not in ALLOW:
                return f"سيدي، '{base}' غير مسموح — allowlist: {', '.join(sorted(ALLOW))}"
            # نفذ بمهلة
            try:
                r=subprocess.run(parts, capture_output=True, text=True, timeout=8, cwd=str(Path(__file__).resolve().parent.parent))
                out=(r.stdout or "")[:3000] or (r.stderr or "")[:1000] or "(لا خرج)"
                _log(cmd, out)
                code=f"[{r.returncode}]" if r.returncode else ""
                return f"سيدي {code} {cmd}:\n{out[:2000]}"
            except subprocess.TimeoutExpired:
                _log(cmd, "timeout")
                return "سيدي، انتهت المهلة (8ث)"
            except Exception as e:
                return f"سيدي، فشل التنفيذ: {e}"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، safe_shell واجه مشكلة: {e}"
