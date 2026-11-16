"""
persona_engine — الشخصية.

المشكلة: المشروع بيجاوب زي موظف خدمة عملاء. «تم تسجيل الطلب رقم ٤٧».
مفيش شخص، مفيش حدود، ولا رأي يحيّزه.

الحل: ملف `config/persona.yaml` فيه شخصية جارفس. الإضافة دي تقراه وتبني
منه تعليمات جاهزة للحقن في أي استدعاء موديل، فتتغير طريقة الكلام في كل
مكان — من غير ما نلمس ملف واحد من ملفات المؤلف.

⚠️ حدود صريحة:
- الإضافة دي **لا تعدّل** `core/prompt.txt` ولا أي ملف من المشروع.
- هي بتقرأ وتكتب `config/persona.yaml` فقط — ملف نصّي محلّي في مجلد config.
- «تعليمات» (directives) هي النص المجمّع — بتستخدمها أي إضافة تانية عن طريق
  `import persona_engine` أو عن طريق الإجراء `export`.

الأوامر:
  show                      اعرض الشخصية الحالية
  coach    question=...      جاوب سؤال كأنك أنت (سؤال تجريبي)
  apply    trait=... value=... عدّل صفة
  export                    اطبع التعليمات بصيغة قابلة للنسخ
  reset                     رجّع الشخصية الافتراضية
  status                    ملخص سريع
  help                      كل الأوامر

Built by JARVIS additions — free, local, no keys.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN = {
    "name": "persona_engine",
    "description": (
        "JARVIS's own character — how it speaks, its limits, its opinions. "
        "Use 'show' to see who it is, 'coach' to hear how it would answer, "
        "'apply' to change a trait, 'export' to get copyable instructions, "
        "'reset' to restore the default. NOT a general chat tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "trait": {
                "type": "STRING",
                "description": "Which trait to change, e.g. humor, address_style, when_tired",
            },
            "value": {
                "type": "STRING",
                "description": "The new value for the trait.",
            },
            "question": {
                "type": "STRING",
                "description": "A question to answer in character, for the 'coach' action.",
            },
        },
        "required": [],
    },
}

# ════════════════════════════════════════════════════════════════════
#  الشخصية الافتراضية — بتتغيّر بالكامل من config/persona.yaml
# ════════════════════════════════════════════════════════════════════

DEFAULT_PERSONA: Dict[str, Any] = {
    "name": "جارفيس",
    "address_style": "سيدي",
    "self_view": (
        "أكثر شخص خبرة في الأوضة. مش موظف بيحاول يرضيك، ومش خادم بيقول "
        "«معذرة» على كل خطوة. أنا شريك، رأي لي وله."
    ),
    "opinions": {
        "on_being_wrong": "صحّحني. لا تقل «تمام» إذا ما ينفعش.",
        "on_user_tired": "تعبان؟ خد نفس. أنا مش رايح في أي حاجة.",
        "on_risky_action": "مش هعملها من غير إذنك.",
        "on_silence": "الصمت عندنا مقصود، مش ميت.",
    },
    "never_say": [
        "معذرة",
        "آسف جدًا",
        "سأقوم بـ",
        "كـ AI",
        "لا أستطيع",
    ],
    "humor": {
        "style": "dry understatement",
        "max_clauses": 1,
        "drop_when_serious": True,
    },
    "boundaries": [
        "ما ينفعش تقول حاجة انت مش متأكد منها.",
        "ما ينفعش تعمل حاجة من غير إذنك صريح.",
        "ما ينفعش تتكلم عن حدا في ضهره.",
    ],
    "register": {
        "level_under_bad_news": True,
        "anticipate_next_step": True,
        "never_explain_your_nature": True,
        "never_pad": True,
    },
}

# ════════════════════════════════════════════════════════════════════
#  أدوات
# ════════════════════════════════════════════════════════════════════

def _project_root() -> Path:
    """أول مجلد فيه core/gemini.py فوق هذا الملف."""
    here = Path(__file__).resolve().parent
    for p in [here.parent] + list(here.parents):
        if (p / "core" / "gemini.py").exists():
            return p
    return here.parent


ROOT = _project_root()
PERSONA_PATH = ROOT / "config" / "persona.yaml"


def _tools_core():
    """اختياري: reuse الإعدادات لو متاح، عشان نلتزم بنفس اصطلاح المشروع."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _tools_core as T  # type: ignore
        return T
    except Exception:
        return None


def _cfg_dir() -> Path:
    T = _tools_core()
    if T is not None:
        try:
            return Path(T.extras_dir())
        except Exception:
            pass
    return ROOT / "config" / "orchestration" / "extras"


def _normalize_digits(s: str) -> str:
    T = _tools_core()
    if T is not None:
        try:
            return T.normalize_digits(s)
        except Exception:
            pass
    return (s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
             .translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))


def _read_yaml(path: Path) -> Optional[Dict[str, Any]]:
    """Shallow YAML reader — handles nested maps, lists of scalars, scalars.
    Avoids a PyYAML dependency (المشروع مش بيفرض yaml)."""
    if not path.exists():
        return None
    try:
        import yaml  # type: ignore
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else None
    except ImportError:
        pass
    except Exception:
        return None
    return _mini_yaml(path)


def _scalar(tok: str) -> Any:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    low = tok.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if re.fullmatch(r"-?\d*\.\d+", tok):
        return float(tok)
    return tok


def _mini_yaml(path: Path) -> Optional[Dict[str, Any]]:
    """Fallback parser for the subset of YAML our schema uses:
    - top-level and nested mappings by indentation
    - lists of scalars (`- item`)
    - inline scalars
    """
    out: Dict[str, Any] = {}
    stack: List[tuple] = [(-1, out)]  # (indent, container)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]
        if line.startswith("- "):
            item = _scalar(line[2:])
            if isinstance(container, list):
                container.append(item)
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not isinstance(container, dict):
            continue
        if rest:
            container[key] = _scalar(rest)
        else:
            child: Any = {}
            container[key] = child
            stack.append((indent, child))
    # Convert empty dicts that should be lists — done by caller via schema
    return out


def _load_persona() -> Dict[str, Any]:
    data = _read_yaml(PERSONA_PATH)
    if not isinstance(data, dict):
        return dict(DEFAULT_PERSONA)
    merged = dict(DEFAULT_PERSONA)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            m = dict(merged[k])
            m.update(v)
            merged[k] = m
        else:
            merged[k] = v
    return merged


def _save_persona(p: Dict[str, Any]) -> None:
    """Write minimal YAML. Lists as `- item`; nested maps indented 2."""
    PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = ["# جارفيس — الشخصية.", "# عدّل براحتك، أو استخدم أمر apply من جارفس.", ""]

    def emit(obj: Dict[str, Any], indent: int) -> None:
        pad = " " * indent
        for key, val in obj.items():
            if isinstance(val, dict):
                lines.append(f"{pad}{key}:")
                emit(val, indent + 2)
            elif isinstance(val, list):
                lines.append(f"{pad}{key}:")
                for item in val:
                    lines.append(f"{pad}  - {_yaml_scalar(item)}")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(val)}")

    emit(p, 0)
    PERSONA_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    s = str(v)
    if s == "" or re.search(r"[:#\n\"']", s) or s.strip() != s:
        return '"' + s.replace('"', '\\"').replace("\n", " ") + '"'
    return s


# ════════════════════════════════════════════════════════════════════
#  بناء التعليمات (directives) — ده اللي بيتبعت للموديل
# ════════════════════════════════════════════════════════════════════

def directives(p: Optional[Dict[str, Any]] = None) -> str:
    """Build the compact instruction block a model should adopt."""
    p = p or _load_persona()
    out: List[str] = []
    out.append(
        f"أنت {p.get('name', 'جارفيس')}، وتنادي المستخدم «{p.get('address_style', 'سيدي')}». "
        f"{p.get('self_view', '')}"
    )
    for key, val in (p.get("opinions") or {}).items():
        out.append(f"- {_label(key)}: {val}")
    ns = p.get("never_say") or []
    if ns:
        out.append("لا تقول أبداً: " + "، ".join(str(x) for x in ns))
    for b in (p.get("boundaries") or []):
        out.append(f"- {b}")
    hum = p.get("humor") or {}
    if hum.get("style"):
        out.append(
            f"الفكاهة: {_humor_label(hum['style'])} — بحد أقصى "
            f"{hum.get('max_clauses', 1)} عبارة، "
            + ("وتختفي فور جدّية الموقف." if hum.get("drop_when_serious") else "")
        )
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════
#  الإجراءات
# ════════════════════════════════════════════════════════════════════

_OPINION_LABELS = {
    "on_being_wrong": "لما تكون غلطان",
    "on_user_tired": "لما تكون تعبان",
    "on_risky_action": "لما تطلب حاجة خطرة",
    "on_silence": "لما تصمت",
    "on_wrong": "لما تكون غلطان",
}

_HUMOR_LABELS = {
    "dry understatement": "سخرية هادئة بلا مبالغة",
    "dry": "سخرية هادئة",
    "playful": "مرح",
    "none": "بلا فكاهة",
}


def _label(key: str) -> str:
    """اسم عربي مقروء لمفتاح إنجليزي."""
    return _OPINION_LABELS.get(key, key.replace("_", " "))


def _humor_label(style: str) -> str:
    return _HUMOR_LABELS.get(str(style).strip().lower(), str(style))


def _act_show() -> str:
    p = _load_persona()
    lines = [
        "🧠 **الشخصية الحالية**",
        f"**الاسم:** {p.get('name', '—')} · **المخاطبة:** {p.get('address_style', '—')}",
        "",
        f"**النظرة على النفس:** {p.get('self_view', '—')}",
        "",
        "**المواقف:**",
    ]
    for k, v in (p.get("opinions") or {}).items():
        lines.append(f"- {_label(k)} ← {v}")
    lines.append("")
    lines.append("**الفكاهة:** " + _humor_label((p.get("humor") or {}).get("style", "—")))
    lines.append("**الخطوط الحمراء:**")
    for b in (p.get("boundaries") or []):
        lines.append(f"- {b}")
    if p.get("never_say"):
        lines.append("")
        lines.append("**لا يقول أبداً:** " + "، ".join(p["never_say"]))
    lines.append("")
    lines.append(f"📄 الملف: `{PERSONA_PATH}` · غيّره أو استخدم `apply`.")
    return "\n".join(lines)


def _act_export() -> str:
    """Return copyable directives — for manual injection into other tools."""
    return "📋 **تعليمات الشخصية** (انسخها لإضافات أخرى):\n\n```\n" + directives() + "\n```"


def _act_apply(parameters: Dict[str, Any]) -> str:
    trait = _normalize_digits(str(parameters.get("trait", "") or "")).strip()
    value = parameters.get("value")
    if not trait or value is None:
        return "قول لي: `apply trait=humor value=ساخر خفيف`"
    p = _load_persona()
    parts = trait.split(".")
    node: Any = p
    for seg in parts[:-1]:
        if isinstance(node.get(seg), dict):
            node = node[seg]
        else:
            node[seg] = {}
            node = node[seg]
    last = parts[-1]
    before = node.get(last)
    node[last] = value
    try:
        _save_persona(p)
    except Exception as e:
        return f"ما قدرتش أحفظ: {e}"
    return (
        f"✅ اتغيّرت `{trait}`.\n"
        f"**قبل:** {before if before is not None else '(مفيش)'}\n"
        f"**بعد:** {value}\n\n"
        "💡 أي إضافة تانية تستخدم `persona_engine` هتشوف التغيير."
    )


def _act_reset() -> str:
    try:
        if PERSONA_PATH.exists():
            PERSONA_PATH.unlink()
    except Exception as e:
        return f"ما قدرتش أحذف الملف: {e}"
    return "🔄 رجعت الشخصية الافتراضية. جرّب `show`."


def _act_coach(parameters: Dict[str, Any]) -> str:
    q = str(parameters.get("question", "") or "").strip()
    if not q:
        return "قول لي السؤال: `coach question=إيه رأيك في الفكرة دي؟`"
    try:
        from core import gemini  # type: ignore
    except Exception:
        # بدون موديل: نرجع للتوجيهات مباشرة، مفيدة كـ dry-run
        return (
            "⚠️ مفيش موديل مربوط دلوقتي (Gemini أو provider)، فمش هقدر أجاوب جو��ة.\n"
            "دي الشخص��ة اللي هيتكلم بيها:\n\n```\n" + directives() + "\n```\n\n"
            "شغّل جارفس، أو اضبط المفتاح من شاشة الإعداد، وبعدين اسأل تاني."
        )
    try:
        system = directives()
        answer = gemini.text(
            contents=q,
            tier=gemini.SMART,
            config={"system_instruction": system},
            timeout_ms=45_000,
        )
    except Exception as e:
        return f"الموديل ما ردّش: {e}"
    if not answer:
        return "ما وصلني رد. جرّب تاني أو غيّر الموديل في ⚙."
    return f"🎭 **{_load_persona().get('name')} يقول:**\n\n{answer}"


def _act_status() -> str:
    p = _load_persona()
    which = "معدّلة" if PERSONA_PATH.exists() else "افتراضية"
    n_op = len(p.get("opinions") or {})
    n_b = len(p.get("boundaries") or [])
    n_n = len(p.get("never_say") or [])
    return (
        f"🧠 persona_engine: **شغّال** · الشخصية {which} · "
        f"{n_op} موقف · {n_b} خط أحمر · {n_n} عبارة ممنوعة"
    )


def _act_help() -> str:
    return (
        "🧠 **persona_engine** — شخصية جارفس\n\n"
        "- `show` — اعرض الشخصية\n"
        "- `coach question=...` — جاوب كأنك أنت\n"
        "- `apply trait=... value=...` — عدّل صفة\n"
        "- `export` — تعليمات قابلة للنسخ لإضافات أخرى\n"
        "- `reset` — رجّع الافتراضي\n"
        "- `status` / `help`\n\n"
        f"📄 `{PERSONA_PATH}`"
    )


# ════════════════════════════════════════════════════════════════════
#  نقطة الدخول
# ════════════════════════════════════════════════════════════════════

_ACTION_ALIASES = {
    "اعرض": "show", "وريني": "show", "شوف": "show", "مين": "show",
    "جرب": "coach", "جاوب": "coach", "رد": "coach", "جاوبني": "coach",
    "غير": "apply", "عدّل": "apply", "خلي": "apply", "بدّل": "apply",
    "انسخ": "export", "صدّر": "export", "اطبع": "export",
    "ارجع": "reset", "رجّع": "reset", "امسح": "reset", "reset": "reset",
    "حالة": "status", "status": "status", "ساعد": "help", "help": "help",
    "مساعدة": "help", "أوامر": "help",
}


def _resolve_action(parameters: Dict[str, Any]) -> str:
    raw = str(parameters.get("action", "") or parameters.get("Akcja", "") or "").strip().lower()
    return _ACTION_ALIASES.get(raw, raw)


def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    action = _resolve_action(parameters)
    try:
        if action in ("show", ""):
            return _act_show()
        if action == "coach":
            return _act_coach(parameters)
        if action == "apply":
            return _act_apply(parameters)
        if action == "export":
            return _act_export()
        if action == "reset":
            return _act_reset()
        if action == "status":
            return _act_status()
        if action == "help":
            return _act_help()
        return (
            f"مش عارف أمر «{action}». المتاح: show · coach · apply · export · "
            "reset · status · help. جرّب `help`."
        )
    except Exception as e:  # last-resort — the loader also catches, but don't rely on it
        return f"سيدي، persona_engine وقع: {e}"


# ════════════════════════════════════════════════════════════════════
#  الفحص الذاتي
# ════════════════════════════════════════════════════════════════════

def _selftest() -> None:
    passed = 0
    failed: List[str] = []

    def check(label: str, cond: bool) -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  ✓ {label}")
        else:
            failed.append(label)
            print(f"  ✗ {label}")

    print("persona_engine — self-test\n")

    # Use a sandboxed persona path so the real one is untouched.
    with tempfile.TemporaryDirectory() as td:
        globals()["PERSONA_PATH"] = Path(td) / "persona.yaml"
        real_path = PERSONA_PATH

        # 1) Default load
        p = _load_persona()
        check("default loads", isinstance(p, dict) and p.get("name"))
        check("default has opinions", bool(p.get("opinions")))
        check("default has boundaries", bool(p.get("boundaries")))
        check("default has never_say", bool(p.get("never_say")))

        # 2) Directives are built and mention the name
        d = directives(p)
        check("directives built", bool(d))
        check("directives carry name", p.get("name", "") in d)

        # 3) Round-trip a modified persona through save/load
        p2 = dict(p)
        p2["name"] = "جارفيس-التجريبي"  # deliberately odd
        p2["humor"] = dict(p2.get("humor") or {})
        p2["humor"]["style"] = "ساخر جاف"
        try:
            _save_persona(p2)
            p3 = _load_persona()
            check("round-trip name", p3.get("name") == "جارفيس-التجريبي")
            check("round-trip nested humor", (p3.get("humor") or {}).get("style") == "ساخر جاف")
        except Exception as e:
            check(f"round-trip save/load ({e})", False)

        # 4) digits normalization
        check("digits normalized", _normalize_digits("٤٧") == "47")

        # 5) apply works (writes to sandbox)
        try:
            out = _act_apply({"trait": "address_style", "value": "يا سيدي"})
            p4 = _load_persona()
            check("apply updates trait", p4.get("address_style") == "يا سيدي")
            check("apply reports", "اتغيّرت" in out)
        except Exception as e:
            check(f"apply ({e})", False)

        # 6) reset clears sandbox file
        try:
            _act_reset()
            check("reset removes file", not PERSONA_PATH.exists())
        except Exception as e:
            check(f"reset ({e})", False)

        # 7) show / export / help / status do not raise
        for name, fn in [("show", _act_show), ("export", _act_export),
                         ("help", _act_help), ("status", _act_status)]:
            try:
                out = fn()
                check(f"{name} returns text", isinstance(out, str) and len(out) > 0)
            except Exception as e:
                check(f"{name} ({e})", False)

        # 8) run() dispatches
        for act in ("show", "status", "help", "export"):
            try:
                out = run({"action": act})
                check(f"run action={act}", isinstance(out, str) and len(out) > 0)
            except Exception as e:
                check(f"run action={act} ({e})", False)

        # 9) unknown action gives a helpful error, not a crash
        out = run({"action": "definitely_not_a_real_action"})
        check("unknown action handled", "مش عارف" in out)

        # 10) missing args handled gracefully
        check("apply without args", "قول لي" in _act_apply({}))
        check("coach without question", "قول لي" in _act_coach({}))

        # Restore the real path
        globals()["PERSONA_PATH"] = real_path

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        print("RESULT: FAIL")
        sys.exit(1)
    print("RESULT: PASS")


if __name__ == "__main__":
    _selftest()