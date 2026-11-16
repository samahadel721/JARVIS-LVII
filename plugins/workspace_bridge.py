"""
workspace_bridge.py — جسر الملفات (آمن)

يسمح بسحب/إفلات ملف على الهود أو فتح مجلد مصرح به فقط — لا فهرسة عشوائية، لا إرسال تلقائي.
يستخدم نفس قواعد remote_gateway: مسارات داخل allowed_dirs فقط + منع traversal.

  workspace_bridge action=list dir="."
  workspace_bridge action=read file="docs/report.md"
  workspace_bridge action=allowed → المجلدات المصرح بها
"""

from __future__ import annotations

from pathlib import Path

PLUGIN = {
    "name": "workspace_bridge",
    "description": (
        "Safe file bridge for the HUD: list and read files only inside allowed folders, with path-traversal protection. "
        "Use for: 'اعرض ملفاتي', 'اقرأ الملف ...', 'ما المجلدات المسموحة', 'workspace_bridge list/read/allowed'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | read | allowed | help"},
            "dir": {"type": "STRING", "description": "مجلد للـ list (نسبي للمشروع)"},
            "file": {"type": "STRING", "description": "ملف للـ read (نسبي للمشروع)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "workspace_bridge",
    "title": "Workspace Bridge / جسر الملفات",
    "fields": [
        {"key": "allowed_dirs", "label": "مجلدات مسموحة (مفصولة بفاصلة)", "type": "text", "default": "docs,memory,downloads"},
        {"key": "max_read_kb", "label": "أقصى حجم قراءة (KB)", "type": "text", "default": "512"},
    ],
}

def _root() -> Path:
    return Path(__file__).resolve().parent.parent

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

def _is_allowed(target: Path) -> bool:
    cfg = _cfg()
    allowed = [x.strip() for x in str(cfg.get("allowed_dirs") or "").split(",") if x.strip()]
    # الجذر نفسه غير مسموح إلا مجلداته المصرحة
    root = _root().resolve()
    try:
        tgt = target.resolve()
    except Exception:
        return False
    # منع traversal خارج المشروع
    try:
        tgt.relative_to(root)
    except ValueError:
        return False
    # اسمح فقط لو داخل أحد allowed_dirs أو ملف في الجذر مذكور صراحة
    for ad in allowed:
        base = (root / ad).resolve()
        try:
            tgt.relative_to(base)
            return True
        except ValueError:
            continue
        if tgt == base:
            return True
    return False

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?","مساعدة"):
            return "سيدي، جسر الملفات (آمن):\n• allowed — المجلدات المصرح بها\n• list dir=\"docs\" — قائمة ملفات\n• read file=\"docs/report.md\" — قراءة (512KB max)\nلا فهرسة خارج allowed_dirs، ولا إرسال تلقائي"
        if action in ("allowed","مسموح","dirs"):
            cfg = _cfg()
            return f"سيدي، المجلدات المسموحة: {cfg.get('allowed_dirs')} — الجذر: {_root().name}/ — max {cfg.get('max_read_kb')}KB"
        if action in ("list","قائمة","ls"):
            d = str(p.get("dir") or "").strip() or str(_cfg().get("allowed_dirs","docs").split(",")[0].strip())
            target = (_root() / d).resolve()
            if not _is_allowed(target):
                return f"سيدي، '{d}' خارج المجلدات المصرح بها — allowed: {_cfg().get('allowed_dirs')}"
            if not target.exists():
                return f"سيدي، لا يوجد {d}"
            if not target.is_dir():
                return f"سيدي، {d} ليس مجلداً"
            items = sorted(target.iterdir())
            if not items:
                return f"سيدي، {d} فارغ"
            lines=[f"سيدي، {d} ({len(items)}):"]
            for it in items[:20]:
                tag = "📁" if it.is_dir() else "📄"
                lines.append(f"  {tag} {it.name}")
            if len(items)>20:
                lines.append(f"  ... و {len(items)-20} أخرى")
            return "\n".join(lines)
        if action in ("read","اقرأ","open"):
            f = str(p.get("file") or "").strip()
            if not f:
                return "سيدي، file= مسار الملف"
            target = (_root() / f).resolve()
            if not _is_allowed(target):
                return f"سيدي، '{f}' خارج المجلدات المصرح بها"
            if not target.exists():
                return f"سيدي، لا يوجد {f}"
            if target.is_dir():
                return f"سيدي، {f} مجلد — استخدم list dir=\"{f}\""
            cfg=_cfg()
            try:
                max_kb=int(str(cfg.get("max_read_kb") or "512"))
            except Exception:
                max_kb=512
            size = target.stat().st_size
            if size > max_kb*1024:
                return f"سيدي، الملف كبير ({size//1024}KB > {max_kb}KB) — افتحه خارجياً"
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return f"سيدي، تعذر القراءة: {e}"
            preview=text[:3000]
            more = f"\n... ({len(text)-3000} حرف إضافي)" if len(text)>3000 else ""
            # اعرض في HUD إن أمكن
            try:
                if player and hasattr(player, "show_content"):
                    player.show_content(target.name, text[:8000])
            except Exception:
                pass
            return f"سيدي، {f} ({size}B):\n{preview}{more}"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، workspace_bridge واجه مشكلة: {e}"
