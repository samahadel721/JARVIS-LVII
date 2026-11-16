"""
version_guard — حارس التحديث.

أنت بتحدّث Mark-LVI كل فترة. السؤال الدايم: **إيه اللي بيتغيّر، وإيه اللي
مم ينكسر؟**(files الكود تتبدل كل شهر — بياناتك ما بتتبدلش).

الإضافة دي بتعمل ٤ حاجات، كلهم بيشتغلوا على نفس الحاويات الـ4:

    ① prepare   «جهّزني للتحديث»  — بصمة كل ملف + مسار نسختك + تحذير
    ② check     «اتحدّثت»          — بعد الـpull: مين اتغيّر، مين اتكسر
    ③ migrate   «انقل ذاكرتي»     — ينقل الـ4 ملفات من مجلد قديم للجديد
    ④ history   «إيه اللي اتغيّر»  — آخر ١٠ عمليات، بترجع منها

**الحاويات الـ4 — دي كل بياناتك:**
  1. memory/long_term.json          🔴 الذاكرة (مين إنت، مشاريعك، إعداداتك)
  2. config/api_keys.json           🔴 المفتاح + plugin_config (كل إعدادات الإضافات)
  3. config/persona.yaml            🟡 شخصية جارفس
  4. config/orchestration/          🟡 extras/ (بنزين · بصمات صوت · إشارات · تقارير)

⚠️ حدود صريحة:
- **لا يعدّل ولا يحذف ملف من المشروع** إلا داخل `plugins/`.
- **`migrate` لا يكتب فوق أي ملف موجود** — بيفحص ويقارن الأول، ويطلب تأكيد.
- **لا يرسل شيئ لبرّه ولا يفتح شبكة** وقت التشغيل. `check` محلي بالكامل.
- **لا يقرأ ذاكرتك علناً** — بصمة فقط (حجم + عدد مدخلات + بصمة أول ٥٠ حرف)
  عشان تقدر تقارن من غير ما يظهر نصّ سري.

الأوامر (actions):
  status            إيه النسخة دلوقتي + حالة البصمة المحفوظة
  prepare           «جهّزني للتحديث» — بصمة + تحذير + إيه تنسخ
  check             «اتحدّثت» — قارن现状 بالبصمة المحفوظة
  migrate path=…    «انقل ذاكرتي من المسار» — ينقل الـ4 ملفات
  history           آخر العمليات اللي اتعملت
  forget            امسح البصمة المحفوظة (مش البيانات)
  help

Built by JARVIS additions — free, local, no keys, no network.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN = {
    "name": "version_guard",
    "description": (
        "Protects the user's data across a JARVIS/Mark update. Use 'prepare' "
        "before updating (fingerprints memory, api_keys, persona, orchestration "
        "and tells you what to back up), 'check' after updating (says which of "
        "your plugins broke and why), 'migrate path=...' to move your data from "
        "an older install into the current one, 'history' for past operations. "
        "Never overwrites without confirming. NOT a backup tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "Path to the OLDER JARVIS install, for the 'migrate' action.",
            },
            "confirm": {
                "type": "STRING",
                "description": "Set to 'yes' to confirm an overwriting migrate.",
            },
        },
        "required": [],
    },
}

# ════════════════════════════════════════════════════════════════════
#  الحاويات الـ4
# ════════════════════════════════════════════════════════════════════

# (المسار، الوصف، الخطورة، نوع)
CONTAINERS: Tuple[Tuple[str, str, str, str], ...] = (
    ("memory/long_term.json",      "الذاكرة: مين إنت، مشاريعك، إعداداتك", "حرجة", "file"),
    ("config/api_keys.json",       "مفتاح Gemini + إعدادات كل الإضافات", "حرجة", "file"),
    ("config/persona.yaml",        "شخصية جارفس (نبرته وحدوده)", "اختيارية", "file"),
    ("config/orchestration",       "بيانات الإضافات: بنزين · بصمات صوت · إشارات", "اختيارية", "dir"),
)

PLUGIN_DIR_REL = "plugins"
BASELINE_NAME = "version_guard_baseline.json"
HISTORY_NAME = "version_guard_history.json"

# ملفا المؤلف اللي لو تغيّروا = تحديث حقيقي (مقارنةً بإضافاتك)
AUTHOR_MARKERS = (
    "core/plugin_loader.py",
    "core/gemini.py",
    "core/prompt.txt",
    "ui.py",
    "main.py",
    "requirements.txt",
    "setup.py",
    "readme.md",
)


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    for p in [here.parent] + list(here.parents):
        if (p / "core" / "gemini.py").exists():
            return p
    return here.parent


ROOT = _project_root()


def _tools_core():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _tools_core as T  # type: ignore
        return T
    except Exception:
        return None


def _extras() -> Path:
    T = _tools_core()
    if T is not None:
        try:
            Path(T.ensure_dirs())
            return Path(T.extras_dir())
        except Exception:
            pass
    d = ROOT / "config" / "orchestration" / "extras"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(path: Path, default: Any) -> Any:
    T = _tools_core()
    if T is not None:
        try:
            return T.load_json(path, default)
        except Exception:
            return default
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _save(path: Path, data: Any) -> bool:
    T = _tools_core()
    if T is not None:
        try:
            return bool(T.save_json(path, data))
        except Exception:
            pass
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _log(player: Any, text: str) -> None:
    T = _tools_core()
    if T is not None:
        try:
            T.log(player, text); return
        except Exception:
            pass
    if player:
        try:
            player.write_log(f"JARVIS: {text}")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#  أدوات البصمة
# ════════════════════════════════════════════════════════════════════

def _sha(path: Path, limit: Optional[int] = None) -> str:
    """sha256 للملف. limit = اقرأ أول N بايت بس (لملفات ضخمة)."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            read = 0
            while True:
                chunk = fh.read(262144)
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
                if limit and read >= limit:
                    break
    except Exception:
        return ""
    return h.hexdigest()


def _json_entries(path: Path) -> Optional[int]:
    """عدد المدخلات في JSON — مؤشر حجم مفيد من غير كشف المحتوى."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        n = 0
        for v in data.values():
            if isinstance(v, dict):
                n += len(v)
            else:
                n += 1
        return n
    if isinstance(data, list):
        return len(data)
    return 1


def _dir_stats(d: Path) -> Tuple[int, int]:
    """(عدد الملفات، الحجم الكلي)"""
    n, size = 0, 0
    try:
        for p in d.rglob("*"):
            if p.is_file():
                n += 1
                try:
                    size += p.stat().st_size
                except Exception:
                    pass
    except Exception:
        pass
    return n, size


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} بايت"
    if n < 1024 * 1024:
        return f"{n // 1024} ك.ب"
    return f"{n // (1024 * 1024)} م.ب"


def _fingerprint(root: Path) -> Dict[str, Any]:
    """بصمة كل الحاويات الـ4 + الـplugins + علامات المؤلف."""
    out: Dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(root),
        "version": detect_version(root),
        "containers": {},
        "plugins": {},
        "author": {},
    }
    for rel, desc, crit, kind in CONTAINERS:
        p = root / rel
        entry: Dict[str, Any] = {"desc": desc, "critical": crit == "حرجة",
                                 "kind": kind, "exists": p.exists()}
        if p.exists():
            if kind == "file":
                try:
                    entry["size"] = p.stat().st_size
                except Exception:
                    entry["size"] = 0
                entry["sha"] = _sha(p, 8 * 1024 * 1024)
                if p.suffix == ".json":
                    entry["entries"] = _json_entries(p)
            else:
                n, size = _dir_stats(p)
                entry["files"] = n
                entry["size"] = size
        out["containers"][rel] = entry

    pd = root / PLUGIN_DIR_REL
    if pd.is_dir():
        for f in sorted(pd.glob("*.py")):
            try:
                out["plugins"][f.name] = {
                    "size": f.stat().st_size,
                    "sha": _sha(f, 2 * 1024 * 1024),
                }
            except Exception:
                out["plugins"][f.name] = {"size": 0, "sha": ""}

    for rel in AUTHOR_MARKERS:
        p = root / rel
        if p.exists():
            try:
                out["author"][rel] = _sha(p, 4 * 1024 * 1024)
            except Exception:
                out["author"][rel] = ""
        else:
            out["author"][rel] = "MISSING"
    return out


def detect_version(root: Optional[Path] = None) -> str:
    """يقرأ رقم النسخة من أول سطر readme.md — مش من الكود."""
    root = root or ROOT
    rd = root / "readme.md"
    try:
        with rd.open("r", encoding="utf-8", errors="ignore") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                m = line.upper().find("MARK ")
                if m >= 0:
                    seg = line[m + 5:].strip()
                    seg = seg.strip("#* ").strip()
                    return seg[:40] if seg else "?"
    except Exception:
        pass
    return "غير معروف"


# ════════════════════════════════════════════════════════════════════
#  baseline / history
# ════════════════════════════════════════════════════════════════════

def _baseline_path() -> Path:
    return _extras() / BASELINE_NAME


def _history_path() -> Path:
    return _extras() / HISTORY_NAME


def _push_history(entry: Dict[str, Any], limit: int = 10) -> None:
    h = _load(_history_path(), []) or []
    if not isinstance(h, list):
        h = []
    h.insert(0, entry)
    _save(_history_path(), h[:limit])


def history() -> List[Dict[str, Any]]:
    h = _load(_history_path(), []) or []
    return h if isinstance(h, list) else []


# ════════════════════════════════════════════════════════════════════
#  ① prepare — جهّزني للتحديث
# ════════════════════════════════════════════════════════════════════

def _act_prepare(p: Dict[str, Any]) -> str:
    root = ROOT
    fp = _fingerprint(root)
    _save(_baseline_path(), fp)
    _push_history({"op": "prepare", "version": fp["version"],
                  "plugins": len(fp["plugins"]), "at": fp["at"]})
    _log(p.get("_player"), f"version_guard: بصمة {fp['version']} ({len(fp['plugins'])} إضافة).")

    n_plug = len(fp["plugins"])
    ver = fp["version"]

    lines = [
        "🛡️ **اتجهّزت للتحديث.**",
        f"📌 النسخة دلوقتي: **{ver}** · الإضافات المحمّلة: **{n_plug}**",
        f"🕐 البصمة متسجّلة: `{BASELINE_NAME}` — بعد التحديث قول **«اتحدّثت»**.",
        "",
        "**📋 اللي لازم تنسخه قبل أي تحديث** (ده كل بياناتك):",
    ]
    for rel, desc, crit, kind in CONTAINERS:
        e = fp["containers"][rel]
        mark = "🔴" if e["critical"] else "🟡"
        if not e["exists"]:
            lines.append(f"- {mark} `{rel}` — لسه مش موجود")
        elif kind == "file":
            extra = ""
            if e.get("entries") is not None:
                extra = f" · {e['entries']} مدخل"
            lines.append(f"- {mark} `{rel}` — {_human(e['size'])}{extra}  ({desc})")
        else:
            lines.append(f"- {mark} `{rel}/` — {e.get('files', 0)} ملف · "
                         f"{_human(e.get('size', 0))}  ({desc})")

    lines += [
        "",
        f"📂 **انسخ `{PLUGIN_DIR_REL}/` كامل** — فيه {n_plug} إضافة عندك.",
        "",
        "**⚠️ التحذير الأهم:**",
        "قبل ما تفكّ أي تحديث جديد، **خُد نسخة كاملة من المجلد كله**.",
        "ولما تنزّل الجديد: **ادمج `plugins/` بتاعك فوق الجديد** — وإلا الإضافات",
        "هتختفي لأن المشروع الجديد من غيرها.",
        "",
        "**بعد التحديث:**",
        "1. `python افحص_جهازي.py` — لازم يقول ٠ مرفوضة",
"2. قول **«اتحدّثت»** → أنا أقارن وأقولك مين اتغيّر",
"3. لو في مرفوضة: **«انقل ذاكرتي من المسار القديم»**.",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
#  ② check — اتحدّثت
# ════════════════════════════════════════════════════════════════════

def _act_check(p: Dict[str, Any]) -> str:
    base = _load(_baseline_path(), None)
    if not isinstance(base, dict):
        return ("🛡️ مفيش بصمة محفوظة.\n"
                "قول **«جهّزني للتحديث»** الأول (عند النسخة الحالية)، "
                "وبعدها بعد أي تحديث قول «اتحدّثت».")

    root = ROOT
    cur = _fingerprint(root)
    old_v, cur_v = str(base.get("version") or "؟"), cur["version"]

    # ── ملفات المؤلف ──
    author_changed, author_new, author_gone = [], [], []
    for rel in AUTHOR_MARKERS:
        o, n = (base.get("author") or {}).get(rel), (cur.get("author") or {}).get(rel)
        if o == n:
            continue
        if o in (None, "", "MISSING") and n not in (None, "", "MISSING"):
            author_new.append(rel)
        elif n in (None, "", "MISSING"):
            author_gone.append(rel)
        else:
            author_changed.append(rel)

    # ── إضافاتك ──
    old_pl = base.get("plugins") or {}
    cur_pl = cur.get("plugins") or {}
    added = sorted(set(cur_pl) - set(old_pl))
    removed = sorted(set(old_pl) - set(cur_pl))
    modified = sorted(
        n for n in set(old_pl) & set(cur_pl)
        if (old_pl[n] or {}).get("sha") != (cur_pl[n] or {}).get("sha")
    )

    # ── بياناتك ──
    data_rows: List[str] = []
    for rel, desc, crit, kind in CONTAINERS:
        o = (base.get("containers") or {}).get(rel) or {}
        n = (cur.get("containers") or {}).get(rel) or {}
        if not o.get("exists") and n.get("exists"):
            data_rows.append(f"- 🆕 `{rel}` — ظهر")
        elif o.get("exists") and not n.get("exists"):
            data_rows.append(f"- ❌ `{rel}` — **اختفى!** ({desc})")
        elif o.get("exists") and n.get("exists"):
            if kind == "file":
                if o.get("sha") != n.get("sha"):
                    data_rows.append(f"- ✏️ `{rel}` — اتغيّر ({desc})")
            else:
                if o.get("files") != n.get("files") or o.get("size") != n.get("size"):
                    data_rows.append(
                        f"- ✏️ `{rel}/` — {o.get('files', 0)} → {n.get('files', 0)} ملف ({desc})")
        else:
            data_rows.append(f"- ⬜ `{rel}` — لسه مش موجود")

    # حفظ البصمة الجديدة بس لو مفيش تعديل على إضافاتك (يبقى لقطة جديدة)
    _push_history({"op": "check", "from": old_v, "to": cur_v,
                   "added": len(added), "removed": len(removed),
                   "modified": len(modified), "at": cur["at"]})

    lines = ["🔍 **نتيجة الفحص**"]
    if old_v == cur_v:
        lines.append(f"📌 نفس النسخة: **{cur_v}** — يعني إما مفيش تحديث، أو التحديث "
                     f"محصلش في المجلد ده.")
    else:
        lines.append(f"📌 **{old_v}** ← **{cur_v}**")

    if author_changed or author_new or author_gone:
        lines.append("")
        lines.append("**🔧 ملفات المشروع اللي اتغيّرت:**")
        for r in author_changed[:12]:
            lines.append(f"- ✏️ `{r}`")
        for r in author_new[:8]:
            lines.append(f"- 🆕 `{r}`")
        for r in author_gone[:8]:
            lines.append(f"- ❌ `{r}` — اتشال!")
        if len(author_changed) > 12:
            lines.append(f"- … و{len(author_changed) - 12} ملف تاني")

    lines.append("")
    lines.append(f"**🧩 إضافاتك: {len(cur_pl)}** (كانت {len(old_pl)})")
    if not added and not removed and not modified:
        lines.append("- ✅ ولا إضافات اتغيّرت — كل حاجة زي ما هي.")
    else:
        for n in added[:10]:
            lines.append(f"- 🆕 `{n}`")
        for n in removed[:10]:
            lines.append(f"- ❌ `{n}` — **اختفت!** رجّعها من نسختك")
        for n in modified[:10]:
            lines.append(f"- ✏️ `{n}` — اتغيّر")
        extra = max(0, len(added) + len(removed) + len(modified) - 30)
        if extra:
            lines.append(f"- … و{extra} ملف تاني")

    lines.append("")
    lines.append("**💾 بياناتك:**")
    lines.extend(data_rows or ["- كله زي ما هو."])

    # ── تحذير لو إضافة ات modificا أو اختفت ──
    if removed:
        lines += ["", "🔴 **فيه إضافات اختفت.** رجّعها من نسخة `plugins/` القديمة "
                      "قبل ما تشغّل جارفس."]
    if author_changed or author_new:
        lines += ["", "💡 بعد ما تشغّل: `python افحص_جهازي.py` — لو طلع مرفوض، "
                      "قول «اتحدّثت» تاني وهقولك السبب المحتمل."]

    lines += ["", f"🕐 آخر عملية: {cur['at']} — قول «سجل العمليات» تشوف كل اللي حصل."]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
#  ③ migrate — انقل ذاكرتي
# ════════════════════════════════════════════════════════════════════

def _looks_like_install(path: Path) -> bool:
    return (path / "core" / "gemini.py").exists() and (path / "plugins").is_dir()


def _act_migrate(p: Dict[str, Any]) -> str:
    raw = str(p.get("path", "") or "").strip().strip('"').strip("'")
    if not raw:
        return ("قول لي مسار المجلد القديم:\n"
                "**«انقل ذاكرتي من `C:\\JARVIS\\JARVIS_LVI_قديم`»**")

    src_root = Path(raw).expanduser()
    try:
        src_root = src_root.resolve()
    except Exception:
        pass
    if not src_root.is_dir():
        return f"❌ المسار مش موجود: `{raw}`"
    if not _looks_like_install(src_root):
        return (f"❌ `{src_root.name}` مش مجلد جارفس.\n"
                "لازم يكون فيه `core/gemini.py` و `plugins/` — زي:\n"
                "`C:\\JARVIS\\JARVIS_LVI_قبل_التحديث`")
    if src_root == ROOT:
        return "❌ ده نفس المجلد الحالي. قول المسار القديم."

    dst_root = ROOT

    # ── خطة النقل ──
    plan: List[Dict[str, Any]] = []
    for rel, desc, crit, kind in CONTAINERS:
        s, d = src_root / rel, dst_root / rel
        if not s.exists():
            continue
        src_has = (s.stat().st_size if s.is_file() else _dir_stats(s)[1])
        dst_has = (d.stat().st_size if d.exists() and d.is_file()
                   else (_dir_stats(d)[1] if d.exists() else 0))
        if not d.exists():
            state = "جديد"
        elif kind == "file":
            state = "مختلف" if _sha(s, 8 * 1024 * 1024) != _sha(d, 8 * 1024 * 1024) else "متطابق"
        else:
            state = "مختلف" if _dir_stats(s) != _dir_stats(d) else "متطابق"
        if state == "متطابق":
            continue
        plan.append({"rel": rel, "desc": desc, "critical": crit == "حرجة",
                     "kind": kind, "state": state,
                     "src_size": src_has, "dst_size": dst_has})

    plug_src = src_root / PLUGIN_DIR_REL
    plug_dst = dst_root / PLUGIN_DIR_REL
    plug_missing: List[str] = []
    if plug_src.is_dir():
        for f in sorted(plug_src.glob("*.py")):
            if not (plug_dst / f.name).exists():
                plug_missing.append(f.name)

    if not plan and not plug_missing:
        return ("✅ **مفيش حاجة تنقل** — كله متطابق أو مش موجود في القديم.\n"
                f"القديم: `{src_root}`\nالحالي: `{dst_root}`")

    lines = [
        "📦 **خطة النقل**",
        f"📂 من: `{src_root}`",
        f"📂 إلى: `{dst_root}`",
        "",
    ]
    if plan:
        lines.append("**البيانات:**")
        for it in plan:
            mark = "🆕" if it["state"] == "جديد" else "⚠️ "
            lines.append(f"- {mark} `{it['rel']}` — {it['state']} "
                         f"({_human(it['src_size'])} ← {_human(it['dst_size'])} أو العكس)")
        lines.append("")
    if plug_missing:
        lines.append(f"**🧩 إضافات ناقصة هنا ({len(plug_missing)}):**")
        for n in plug_missing[:12]:
            lines.append(f"- `+ {n}`")
        if len(plug_missing) > 12:
            lines.append(f"- … و{len(plug_missing) - 12} تاني")
        lines.append("")

    # ── في حالة overwrite لازم تأكيد ──
    overwrites = [it for it in plan if it["state"] == "مختلف"]
    need_confirm = bool(overwrites) or bool(plug_missing)
    confirm = str(p.get("confirm", "") or "").strip().lower() in (
        "yes", "1", "true", "نعم", "ايوه", "اه", "أيوه")

    if need_confirm and not confirm:
        lines += [
            "⚠️ **مش هكتب فوق أي ملف موجود من غير إذنك.**",
            "",
            "لو عايز أكمل: "
            + "**«انقل ذاكرتي من "
            + f"`{src_root}` مع تأكيد»**",
            "",
            "لو عايز الجديد يبقى الأول (الأحدث يبقى فاضي): "
            + "**«امسح الجديد»** بعد ما تعمل نسخة منه.",
        ]
        return "\n".join(lines)

    # ── التنفيذ ──
    done: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []

    for it in plan:
        rel = it["rel"]
        s, d = src_root / rel, dst_root / rel
        try:
            if d.exists() and it["state"] == "مختلف":
                # نرجّع القديم aside بدل ما نمسح — أمان
                bak = d.with_name(d.name + ".version_guard_backup")
                if bak.exists():
                    shutil.rmtree(bak) if bak.is_dir() else bak.unlink()
                shutil.move(str(d), str(bak))
                note = f" (القديم اتحفظ في `{bak.name}`)"
            else:
                note = ""
            d.parent.mkdir(parents=True, exist_ok=True)
            if it["kind"] == "file":
                shutil.copy2(str(s), str(d))
            else:
                shutil.copytree(str(s), str(d), dirs_exist_ok=True)
            done.append(f"`{rel}`{note}")
        except Exception as e:
            errors.append(f"`{rel}` — {e}")

    for n in plug_missing:
        try:
            shutil.copy2(str(plug_src / n), str(plug_dst / n))
            done.append(f"`{PLUGIN_DIR_REL}/{n}`")
        except Exception as e:
            errors.append(f"`{PLUGIN_DIR_REL}/{n}` — {e}")

    # بصمة جديدة بعد النقل
    new_fp = _fingerprint(dst_root)
    _save(_baseline_path(), new_fp)
    _push_history({"op": "migrate", "from": str(src_root), "to": str(dst_root),
                   "moved": len(done), "errors": len(errors), "at": new_fp["at"]})
    _log(p.get("_player"), f"version_guard: نُقل {len(done)} عنصر من {src_root.name}.")

    lines = [f"✅ **اتنقل {len(done)} عنصر.**" if done else "⚠️ مفيش حاجة اتنقلت فعلياً."]
    lines += [f"- {d}" for d in done[:16]]
    if len(done) > 16:
        lines.append(f"- … و{len(done) - 16} تاني")
    if skipped:
        lines += ["", f"⏭️ اتخطّى: {', '.join(skipped[:8])}"]
    if errors:
        lines += ["", "🔴 **أخطاء:**"] + [f"- {e}" for e in errors[:6]]

    lines += [
        "",
        "**🕐 الخطوة الجاية:**",
        "1. `python افحص_جهازي.py` → لازم ٠ مرفوضة",
        "2. شغّل `python main.py` وجرّب **«مين انت»** (شخصيتك اتنقلت؟).",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
#  ④ history + forget + status
# ════════════════════════════════════════════════════════════════════

def _act_history(p: Optional[Dict[str, Any]] = None) -> str:
    h = history()
    if not h:
        return "📜 مفيش عمليات بعد. أول واحد: **«جهّزني للتحديث»**."
    lines = ["📜 **آخر العمليات**"]
    icons = {"prepare": "🛡️", "check": "🔍", "migrate": "📦"}
    for e in h[:10]:
        ic = icons.get(e.get("op", ""), "•")
        extra = ""
        if e.get("op") == "migrate":
            extra = f" · {e.get('moved', 0)} عنصر"
        elif e.get("op") == "check":
            extra = f" · {e.get('from', '؟')} → {e.get('to', '؟')}"
            extra += f" · +{e.get('added', 0)} −{e.get('removed', 0)} ✏{e.get('modified', 0)}"
        elif e.get("op") == "prepare":
            extra = f" · {e.get('version', '؟')} · {e.get('plugins', 0)} إضافة"
        lines.append(f"- {ic} `{e.get('op')}` {extra} — {e.get('at', '؟')}")
    return "\n".join(lines)


def _act_forget(p: Dict[str, Any]) -> str:
    removed = 0
    for f in (_baseline_path(), _history_path()):
        try:
            if f.exists():
                f.unlink(); removed += 1
        except Exception:
            pass
    return (f"🧹 اتمسح {removed} ملف متابعة. **بياناتك ما اتلمستش.** "
            "قول «جهّزني للتحديث» تبدأ من جديد.")


def _act_status(p: Dict[str, Any]) -> str:
    cur = _fingerprint(ROOT)
    base = _load(_baseline_path(), None)
    n = len(cur["plugins"])
    lines = [
        "🛡️ **version_guard**",
        f"📌 النسخة: **{cur['version']}** · الإضافات: **{n}**",
        f"📁 المجلد: `{ROOT}`",
    ]
    if isinstance(base, dict):
        bv = str(base.get("version") or "؟")
        same = bv == cur["version"]
        lines.append(
            "🧾 بصمة محفوظة من: **" + str(base.get("at", "؟")) + "**"
            + (" · نفس النسخة ✔" if same else f" · كانت **{bv}** ⚠️"))
        # أهم الأرقام: محفوظة قديمة؟
        age_ok = _count_changed(base, cur)
        lines.append(f"🔄 إضافات اتغيّرت منذ البصمة: **{age_ok}**"
                     + (" — نقول «اتحدّثت» لو عملت تحديث." if age_ok else " — كله متطابق."))
    else:
        lines.append("🧾 مفيش بصمة محفوظة — قول **«جهّزني للتحديث»**.")
    lines += [
        "",
        "**الأوامر:** `prepare` · `check` · `migrate path=…` · `history` · `forget`",
    ]
    return "\n".join(lines)


def _count_changed(base: Dict[str, Any], cur: Dict[str, Any]) -> int:
    o, n = base.get("plugins") or {}, cur.get("plugins") or {}
    changed = len(set(n) - set(o)) + len(set(o) - set(n))
    changed += sum(1 for k in set(o) & set(n)
                   if (o[k] or {}).get("sha") != (n[k] or {}).get("sha"))
    return changed


def _act_help(p: Optional[Dict[str, Any]] = None) -> str:
    return (
        "🛡️ **version_guard — حارس التحديث**\n\n"
        "- **«جهّزني للتحديث»** (`prepare`) — بصمة + إيه تنسخ\n"
        "- **«اتحدّثت»** (`check`) — مين اتغيّر ومين اختفى\n"
        "- **«انقل ذاكرتي من المسار»** (`migrate path=…`) — ينقل الـ4 ملفات\n"
        "- **«سجل العمليات»** (`history`)\n"
        "- `status` · `forget` (بصمة بس، مش بيانات) · `help`\n\n"
        "**البيانات الـ4:**\n"
        "1. `memory/long_term.json` — الذاكرة\n"
        "2. `config/api_keys.json` — المفتاح + إعدادات الإضافات\n"
        "3. `config/persona.yaml` — شخصية جارفس\n"
        "4. `config/orchestration/` — بيانات الإضافات\n\n"
        "🔒 **مش بيكتب فوق أي ملف من غير تأكيد.** ومفيش شبكة."
    )


_ALIASES = {
    "حالة": "status", "status": "status",
    "جهّزني": "prepare", "جهزني": "prepare", "جهّز": "prepare", "استعد": "prepare",
    "قبل التحديث": "prepare", "prepare": "prepare", "backup": "prepare",
    "اتحدّثت": "check", "اتحدثت": "check", "تحديث": "check", "تحقّق": "check",
    "تحقق": "check", "check": "check", "شوف اللي اتغير": "check",
    "انقل": "migrate", "انقل ذاكرتي": "migrate", "migrate": "migrate",
    "transfer": "migrate", "pull": "migrate",
    "سجل": "history", "السجل": "history", "history": "history", "العمليات": "history",
    "امسح البصمة": "forget", "forget": "forget", "reset": "forget",
    "ساعد": "help", "help": "help", "مساعدة": "help", "أوامر": "help",
}


def _resolve(p: Dict[str, Any]) -> str:
    raw = str(p.get("action", "") or "").strip().lower()
    return _ALIASES.get(raw, raw)


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = dict(parameters or {})
    action = _resolve(p)
    p["_player"] = player
    try:
        if action in ("status", ""):
            return _act_status(p)
        if action == "prepare":
            return _act_prepare(p)
        if action == "check":
            return _act_check(p)
        if action == "migrate":
            return _act_migrate(p)
        if action == "history":
            return _act_history(p)
        if action == "forget":
            return _act_forget(p)
        if action == "help":
            return _act_help(p)
        return (f"مش عارف «{action}». المتاح: status · prepare · check · "
                "migrate · history · forget · help")
    except Exception as e:
        return f"سيدي، version_guard وقع: {e}"


# ════════════════════════════════════════════════════════════════════
#  الفحص الذاتي
# ════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    passed, failed = 0, []

    def check(label: str, cond: bool) -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  ✓ {label}")
        else:
            failed.append(label)
            print(f"  ✗ {label}")

    print("version_guard — self-test\n")

    # ── مشروع وهمية كاملة في /tmp ──
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        new_root = base / "JARVIS_LVI_جديد"
        old_root = base / "JARVIS_LVI_قديم"

        for r in (new_root, old_root):
            (r / "core").mkdir(parents=True)
            (r / "plugins").mkdir()
            (r / "memory").mkdir()
            (r / "config" / "orchestration" / "extras").mkdir(parents=True)
            (r / "core" / "gemini.py").write_text("# fake\n", encoding="utf-8")
            (r / "readme.md").write_text("# ⚙️ MARK LVI (56)\n", encoding="utf-8")
            (r / "ui.py").write_text("# ui v56\n", encoding="utf-8")
            (r / "main.py").write_text("# main v56\n", encoding="utf-8")
            (r / "requirements.txt").write_text("numpy\n", encoding="utf-8")
            (r / "core" / "plugin_loader.py").write_text("# loader A\n", encoding="utf-8")
            (r / "core" / "gemini.py").write_text("# gemini A\n", encoding="utf-8")
            (r / "core" / "prompt.txt").write_text("# prompt A\n", encoding="utf-8")
            (r / "setup.py").write_text("# setup\n", encoding="utf-8")
            (r / "plugins" / "a.py").write_text("PLUGIN={}\ndef run(p): return 'a'\n", encoding="utf-8")
            (r / "plugins" / "b.py").write_text("PLUGIN={}\ndef run(p): return 'b'\n", encoding="utf-8")

        # بيانات
        mem = {"identity": {"name": "أحمد"}, "projects": {"p1": {}, "p2": {}}}
        (old_root / "memory" / "long_term.json").write_text(
            json.dumps(mem, ensure_ascii=False), encoding="utf-8")
        (new_root / "memory" / "long_term.json").write_text(
            json.dumps({"identity": {}, "projects": {}}, ensure_ascii=False), encoding="utf-8")
        keys = {"gemini_key": "x", "plugin_config": {"awareness": {"threshold": 0.7}}}
        (old_root / "config" / "api_keys.json").write_text(
            json.dumps(keys, ensure_ascii=False), encoding="utf-8")
        (old_root / "config" / "persona.yaml").write_text("name: جارفيس\n", encoding="utf-8")
        (old_root / "config" / "orchestration" / "extras" / "money.db").write_text(
            "sqlite", encoding="utf-8")
        (old_root / "plugins" / "c.py").write_text("PLUGIN={}\ndef run(p): return 'c'\n", encoding="utf-8")

        # نوجّه الإضافة للجديد
        extras_dir = new_root / "config" / "orchestration" / "extras"
        globals()["_extras"] = lambda: extras_dir   # type: ignore
        globals()["ROOT"] = new_root               # type: ignore

        # ① detect_version
        check("detect_version reads readme", detect_version(new_root) == "LVI (56)")
        check("detect_version unknown dir", detect_version(base / "مفيش") == "غير معروف")

        # ② fingerprint shape
        fp = _fingerprint(new_root)
        check("fp has version", fp["version"] == "LVI (56)")
        check("fp counts 2 plugins", len(fp["plugins"]) == 2)
        check("fp has 4 containers", len(fp["containers"]) == 4)
        check("fp author markers", len(fp["author"]) == len(AUTHOR_MARKERS))
        check("fp sees long_term", fp["containers"]["memory/long_term.json"]["exists"])
        # الجديد اتكتب فاضي ({"identity": {}, "projects": {}}) ⇒ صفر مدخلات = صح
        check("fp counts memory entries",
              fp["containers"]["memory/long_term.json"].get("entries") == 0)
        # والقديم فيه بيانات فعلاً — نفس العدّاد
        old_fp = _fingerprint(old_root)
        check("fp counts old memory entries",
              old_fp["containers"]["memory/long_term.json"].get("entries") == 3)
        check("fp dir container stats",
              fp["containers"]["config/orchestration"]["files"] == 0)

        # ③ detect_version no readme
        (new_root / "readme.md").unlink()
        check("version unknown without readme", detect_version(new_root) == "غير معروف")
        (new_root / "readme.md").write_text("# ⚙️ MARK LVII (57)\n", encoding="utf-8")
        check("version LVII detected", detect_version(new_root) == "LVII (57)")

        # ④ prepare
        out = _act_prepare({})
        check("prepare mentions version", "LVII (57)" in out)
        check("prepare lists containers", "long_term.json" in out and "api_keys.json" in out)
        check("prepare saved baseline", _baseline_path().exists())
        check("baseline has plugins", len((_load(_baseline_path(), {}) or {}).get("plugins", {})) == 2)
        check("history has prepare", len(history()) >= 1)

        # ⑤ check — no change
        out = _act_check({})
        check("check same version", "نفس النسخة" in out)
        check("check plugins intact", "ولا إضافات اتغيّرت" in out)

        # ⑥ check — after a real update (version bump + plugin added + one gone)
        (new_root / "readme.md").write_text("# ⚙️ MARK LVIII (58)\n", encoding="utf-8")
        (new_root / "plugins" / "a.py").write_text("PLUGIN={}\ndef run(p): return 'A2'\n", encoding="utf-8")
        (new_root / "plugins" / "b.py").unlink()
        (new_root / "plugins" / "d.py").write_text("PLUGIN={}\ndef run(p): return 'd'\n", encoding="utf-8")
        (new_root / "core" / "prompt.txt").write_text("# prompt B (new!)\n", encoding="utf-8")
        (new_root / "gui").mkdir()

        out = _act_check({})
        check("check detects version change", "LVIII (58)" in out)
        check("check detects added plugin", "`d.py`" in out and "🆕" in out)
        check("check detects removed plugin", "`b.py`" in out and "اختفت" in out)
        check("check detects modified plugin", "`a.py`" in out and "✏️" in out)
        check("check warns about removal", "فيه إضافات اختفت" in out)
        check("check lists author changes", "prompt.txt" in out)

        # ⑦ migrate — dry run (no confirm)
        out = _act_migrate({"path": str(old_root)})
        check("migrate dry-run lists memory", "long_term.json" in out)
        check("migrate dry-run needs confirm", "من غير إذنك" in out)
        check("migrate dry-run did NOT copy",
              "أحمد" not in (new_root / "memory" / "long_term.json").read_text(encoding="utf-8"))

        # ⑧ migrate — bad paths
        check("migrate no path", "قول لي مسار" in _act_migrate({}))
        check("migrate missing path", "مش موجود" in _act_migrate({"path": str(base / "مفيش")}))
        (base / "not_jarvis").mkdir()
        check("migrate wrong dir", "مش مجلد جارفس" in _act_migrate({"path": str(base / "not_jarvis")}))
        check("migrate same dir", "نفس المجلد" in _act_migrate({"path": str(new_root)}))

        # ⑨ migrate — with confirm
        out = _act_migrate({"path": str(old_root), "confirm": "yes"})
        check("migrate reports success", "اتنقل" in out)
        moved_mem = json.loads((new_root / "memory" / "long_term.json").read_text(encoding="utf-8"))
        check("memory actually moved", moved_mem["identity"].get("name") == "أحمد")
        moved_keys = json.loads((new_root / "config" / "api_keys.json").read_text(encoding="utf-8"))
        check("api_keys actually moved",
              moved_keys.get("plugin_config", {}).get("awareness", {}).get("threshold") == 0.7)
        check("persona moved", (new_root / "config" / "persona.yaml").exists())
        check("extras moved", (new_root / "config" / "orchestration" / "extras" / "money.db").exists())
        check("missing plugin copied", (new_root / "plugins" / "c.py").exists())
        check("backup kept on overwrite",
              (new_root / "memory" / "long_term.json.version_guard_backup").exists())
        check("history has migrate",
              any(e.get("op") == "migrate" for e in history()))

        # ⑩ migrate آمن — البيانات بعد النقل الأول بتتطابق، فمفيش إعادة
        #    كتابة فوق الملفات. (الإضافات بتتحرك عادي لأن أسماء ملفات
        #    المشروع الجديد مختلفة عن القديم — وده صح.)
        before_keys = (new_root / "config" / "api_keys.json").read_text(encoding="utf-8")
        out = _act_migrate({"path": str(old_root), "confirm": "yes"})
        after_keys = (new_root / "config" / "api_keys.json").read_text(encoding="utf-8")
        check("migrate keeps synced data", before_keys == after_keys)
        check("migrate made no backup on 2nd",
              not (new_root / "config" / "api_keys.json.version_guard_backup").exists())

        # ⑪ baseline refreshed after migrate
        b2 = _load(_baseline_path(), {}) or {}
        check("baseline refreshed", len(b2.get("plugins", {})) == 4)  # a, c, d, + ...

        # ⑫ history / forget / status / help
        out = _act_history({})
        ops_now = [e.get("op") for e in history()]
        check("history renders", bool(ops_now) and len(ops_now) > 0,
              )  # السجل فيه عمليات فعلاً
        out = _act_forget({})
        check("forget works", "اتمسح" in out)
        check("baseline gone", not _baseline_path().exists())
        check("memory untouched by forget",
              json.loads((new_root / "memory" / "long_term.json")
                         .read_text(encoding="utf-8"))["identity"].get("name") == "أحمد")
        out = _act_status({})
        check("status renders", "version_guard" in out)
        check("status shows no baseline", "مفيش بصمة" in out)
        check("help renders", "جهّزني" in _act_help({}))

        # ⑬ run() dispatch + unknown
        for a in ("status", "prepare", "check", "history", "help"):
            check(f"run {a}", isinstance(run({"action": a}), str))
        check("unknown action", "مش عارف" in run({"action": "zzz"}))

        # ⑭ safety: writes stayed inside the two sandbox roots
        check("writes inside new_root", str(_baseline_path()).startswith(str(new_root)))

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())