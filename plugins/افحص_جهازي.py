"""
افحص_جهازي.py — قل لي ماذا ينقص جهازك لتشغيل جارفس.

شغّله بأمر واحد من داخل مجلد جارفس:

    python افحص_جهازي.py

وهو يخبرك: نسخة بايثون، والمكتبات المثبّتة والناقصة، والإضافات التي تُحمَّل
فعليًا، والإنترنت — ثم يعطيك الأمر الجاهز للنسخ واللصق لإصلاح كل نقص.

لا يحتاج هذا الملف نفسه أي مكتبة: يعمل بالمكتبة القياسية وحدها، فيعمل حتى
لو لم يكن عندك شيء مثبّت أصلًا — وهو بالضبط الوقت الذي تحتاجه فيه.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# الحزم: (الاسم في pip، اسم الاستيراد، إلى أي مجموعة تنتمي، لماذا)
# أسماء الاستيراد مختلفة عن أسماء التثبيت في كثير من الأحيان — وهذا أشهر ما
# يُربك من يفحص جهازه بنفسه: يكتب import opencv فيخطئ، والصحيح import cv2.
# ─────────────────────────────────────────────────────────────────────────────

BOOT = "ضرورية لإقلاع جارفس"
FEATURE = "تفتح ميزة معيّنة"
PLUGIN_DEPS = "تحتاجها إضافات معيّنة"
WIN = "ويندوز فقط"
OPTIONAL = "اختيارية — ثبّتها إن أردت الميزة"

PACKAGES = [
    # (pip, import, group, note)
    ("PyQt6",                   "PyQt6",                   BOOT,    "الواجهة — بدونها لا يفتح جارفس إطلاقًا"),
    ("numpy",                   "numpy",                   BOOT,    "الحسابات والمجسّم"),
    ("sounddevice",             "sounddevice",             BOOT,    "الميكروفون والسمّاعة"),
    ("google-genai",            "google.genai",            BOOT,    "عقل جارفس — جلسة Gemini الحيّة"),
    ("requests",                "requests",                FEATURE, "الاتصال بالخدمات"),
    ("beautifulsoup4",          "bs4",                     FEATURE, "قراءة صفحات الويب"),
    ("playwright",              "playwright",              FEATURE, "تحكّم المتصفح"),
    ("yt-dlp",                  "yt_dlp",                  FEATURE, "يوتيوب"),
    ("pyautogui",               "pyautogui",               FEATURE, "التحكم بالفأرة والكيبورد"),
    ("pyperclip",               "pyperclip",               FEATURE, "الحافظة"),
    ("pillow",                  "PIL",                     FEATURE, "الصور"),
    ("opencv-python",           "cv2",                     FEATURE, "الكاميرا والرؤية"),
    ("mss",                     "mss",                     FEATURE, "تصوير الشاشة"),
    ("psutil",                  "psutil",                  FEATURE, "قراءة حالة الجهاز"),
    ("send2trash",              "send2trash",              FEATURE, "حذف قابل للتراجع"),
    ("python-pptx",             "pptx",                    FEATURE, "قراءة العروض التقديمية"),
    ("fastapi",                 "fastapi",                 FEATURE, "لوحة التحكم من الهاتف"),
    ("uvicorn",                 "uvicorn",                 FEATURE, "خادم لوحة التحكم"),
    ("cryptography",            "cryptography",            FEATURE, "تشفير لوحة التحكم"),
    ("python-multipart",        "multipart",               FEATURE, "رفع الملفات للوحة"),
    ("qrcode",                  "qrcode",                  FEATURE, "رمز QR للوحة"),
    ("pdfplumber",              "pdfplumber",              FEATURE, "قراءة PDF"),
    ("PyPDF2",                  "PyPDF2",                  FEATURE, "قراءة PDF — بديل"),
    ("python-docx",             "docx",                    FEATURE, "قراءة ملفات Word"),
    ("pynvml",                  "pynvml",                  FEATURE, "قراءة كرت الشاشة"),
    ("ddgs",                    "ddgs",                    FEATURE, "البحث في الويب"),
    ("openpyxl",                "openpyxl",                PLUGIN_DEPS,  "excel_writer"),
    ("youtube-transcript-api",  "youtube_transcript_api",  PLUGIN_DEPS,  "youtube_video"),
    ("pygetwindow",             "pygetwindow",             PLUGIN_DEPS,  "game_updater و chat_takeover"),
    ("google-api-python-client", "googleapiclient",        PLUGIN_DEPS,  "gmail_control و calendar_control"),
    ("google-auth-oauthlib",    "google_auth_oauthlib",    PLUGIN_DEPS,  "جلسة جوجل"),
    ("tinytuya",                "tinytuya",                PLUGIN_DEPS,  "home_assistant"),
    ("paho-mqtt",               "paho.mqtt",               PLUGIN_DEPS,  "printer_control"),
    ("comtypes",                "comtypes",                WIN,     "نوافذ ويندوز"),
    ("pycaw",                   "pycaw",                   WIN,     "صوت ويندوز"),
]

VOICE = [
    ("faster-whisper", "faster_whisper", "تحويل الكلام لنص — محلي ودقيق"),
    ("vosk",           "vosk",           "كلمة التنبيه والكلام — محلي وخفيف"),
    ("edge-tts",       "edge_tts",       "صوت مايكروسوفت — مجاني"),
    ("kokoro",         "kokoro",         "صوت أعمق — اختياري"),
    ("openwakeword",   "openwakeword",   "كلمة «Hey Jarvis» — تُثبَّت من داخل التطبيق"),
]

BAR = "═" * 74

#: اسم وحدة الاستيراد ليس اسم حزمة التثبيت، ومحرّك Mark-LV يبني نصيحته من
#: اسم الوحدة الذي فشل استيراده — فيقول «pip install cv2» وهي حزمة غير
#: موجودة أصلًا على PyPI. هذا الجدول يصحّح النصيحة قبل عرضها.
_IMPORT_TO_PIP = {
    "cv2": "opencv-python", "PIL": "pillow", "bs4": "beautifulsoup4",
    "docx": "python-docx", "pptx": "python-pptx", "yaml": "pyyaml",
    "sklearn": "scikit-learn", "serial": "pyserial", "usb": "pyusb",
    "win32com": "pywin32", "win32": "pywin32", "faster_whisper": "faster-whisper",
    "edge_tts": "edge-tts", "yt_dlp": "yt-dlp", "youtube_transcript_api": "youtube-transcript-api",
    "googleapiclient": "google-api-python-client", "paho": "paho-mqtt",
}


def fix_pip_names(message: str) -> str:
    """يقبل رسالة المحرّك ويعيدها بأسماء حزم صحيحة قابلة للتثبيت فعلًا."""
    def replace(match: re.Match) -> str:
        name = match.group(1)
        return f"pip install {_IMPORT_TO_PIP.get(name, name)}"

    return re.sub(r"pip install ([A-Za-z0-9_.\-]+)", replace, message)


def line(text: str = "") -> None:
    print(text)


def header(title: str) -> None:
    line()
    line(BAR)
    line(f"  {title}")
    line(BAR)


def has(module: str) -> bool:
    """
    هل يمكن استيراد هذه الوحدة؟

    find_spec أسرع بمراحل من الاستيراد الفعلي، ولا تُشغّل كود الوحدة — أي أن
    فحص PyQt6 لا يفتح نافذة، وفحص cv2 لا يحجز الكاميرا.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def find_repo(start: Path) -> Path | None:
    """يبحث عن مجلد جارفس صاعدًا في المسار — فيعمل الملف من أي مكان بداخله."""
    for candidate in [start, *start.parents]:
        if (candidate / "main.py").is_file() and (candidate / "core" / "plugin_loader.py").is_file():
            return candidate
    return None


# ─────────────────────────────────────────────────────────────────────────────
# الفحوص
# ─────────────────────────────────────────────────────────────────────────────

def check_python() -> tuple[bool, list[str]]:
    header("١) بايثون")
    major, minor, micro = sys.version_info[:3]
    version = f"{major}.{minor}.{micro}"
    notes: list[str] = []
    good = True

    line(f"  النسخة         : {version}  ({platform.architecture()[0]})")
    line(f"  النظام        : {platform.system()} {platform.release()}")
    line(f"  مسار بايثون    : {sys.executable}")

    if (major, minor) < (3, 11):
        line("  [X] قديمة جدًا — جارفس يحتاج 3.11 على الأقل.")
        line("      ثبّت Python 3.12 من python.org، ومهم جدًا: علّم على")
        line("      «Add python.exe to PATH» في أول شاشة من شاشات التثبيت.")
        notes.append("ثبّت Python 3.12")
        good = False
    elif (major, minor) > (3, 13):
        line("  [!] أحدث من النسخة المُختبرة عليها (3.13). قد تعمل،")
        line("      وإن فشل تثبيت إحدى الحزم فهذا سببه: ثبّت Python 3.12.")
    else:
        line("  [+] نسخة مدعومة تمامًا.")

    # نسخة متجر مايكروسوفت: خطأ صامت شهير على ويندوز
    if os.name == "nt" and "WindowsApps" in sys.executable:
        line()
        line("  [!] تنبيه: هذه نسخة «متجر مايكروسوفت». مشاكلها كثيرة مع")
        line("      المكتبات العلمية. الأفضل نسخة python.org الرسمية.")
        notes.append("استبدل نسخة المتجر بنسخة python.org")

    return good, notes


def check_packages() -> tuple[list[str], list[str]]:
    header("٢) المكتبات")
    missing_boot: list[str] = []
    missing_else: list[str] = []
    groups: dict[str, list[tuple[str, bool, str]]] = {}

    for pip_name, module, group, note in PACKAGES:
        if group == WIN and os.name != "nt":
            continue
        groups.setdefault(group, []).append((pip_name, has(module), note))

    total_found = total = 0
    for group in (BOOT, FEATURE, PLUGIN_DEPS, WIN):
        rows = groups.get(group)
        if not rows:
            continue
        ok = sum(1 for _, found, _ in rows if found)
        total_found += ok
        total += len(rows)
        line()
        line(f"  ── {group}  ({ok}/{len(rows)})")
        for pip_name, found, note in rows:
            mark = "[+]" if found else ("[X]" if group == BOOT else "[-]")
            line(f"    {mark} {pip_name:<26}{note}")
            if not found:
                (missing_boot if group == BOOT else missing_else).append(pip_name)

    line()
    line(f"  المحصّلة: {total_found} مثبّتة من {total}")
    return missing_boot, missing_else


def check_voice() -> list[str]:
    header("٣) محرّكات الصوت (تُثبَّت عند أول استخدام من داخل التطبيق)")
    missing = []
    for pip_name, module, note in VOICE:
        found = has(module)
        line(f"    {'[+]' if found else '[-]'} {pip_name:<18}{note}")
        if not found:
            missing.append(pip_name)
    line()
    line("  هذه ليست مطلوبة للإقلاع: جارفس يعمل بلا واحد منها، ويطلب")
    line("  تنزيلها عند أول مرة تحتاجها.")
    return missing


def check_plugins(repo: Path | None) -> tuple[int, int, list[str]]:
    header("٤) الإضافات")
    folder = (repo / "plugins") if repo else (Path.cwd() / "plugins")
    if not folder.is_dir():
        line(f"  [X] لا يوجد مجلد plugins/ في {folder.parent}")
        line("      أنشئ مجلدًا باسم plugins وضع فيه ملفات الإضافات.")
        return 0, 0, []

    files = sorted(p for p in folder.glob("*.py")
                   if not p.name.startswith("_") and p.name != "__init__.py")
    helpers = sorted(p for p in folder.glob("_*.py")
                     if p.name not in ("__init__.py", "_template.py"))
    line(f"  المجلد        : {folder}")
    line(f"  إضافات        : {len(files)} ملفًا")
    if helpers:
        line(f"  ملفات مساعدة  : {', '.join(h.name for h in helpers)}  (لا تُحمَّل كأدوات)")
    if not files:
        line()
        line("  المجلد فارغ من الإضافات — انسخ ملفات .py إليه.")
        return 0, 0, []

    # المحرّك الحقيقي هو الحكم. إن وُجد نستعمله؛ وإن لا، نكتفي بعدّ الملفات.
    if repo is not None:
        try:
            sys.path.insert(0, str(repo))
            from core.plugin_loader import discover_plugins          # type: ignore
            import contextlib, io
            # The loader prints a full traceback before it reports the failure in
            # plain language, and for a plugin that is simply missing a package
            # that traceback is noise that makes a first-time reader think
            # something is broken. Captured, not shown — the verdict still is.
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                registry = discover_plugins(folder, core_tool_names=set(),
                                            logger=lambda m: None)
            records = registry.list_for_ui()
            valid = [r for r in records if r["valid"]]
            invalid = [r for r in records if not r["valid"]]
            line()
            line("  تشغيل محرّك Mark-LV الحقيقي:")
            line(f"    [+] {len(valid)} محمّلة")
            line(f"    {'[X]' if invalid else '[+]'} {len(invalid)} مرفوضة")
            for record in invalid:
                line(f"        ✗ {record['name']}: {fix_pip_names(record['error'])[:110]}")
            return len(valid), len(invalid), [r["name"] for r in valid]
        except Exception as exc:
            line(f"  [!] تعذّر تشغيل المحرّك: {exc}")
            line("      المجلد موجود والملفات موجودة — شغّل الفحص من جذر المشروع.")
    return len(files), 0, []


def check_network() -> list[str]:
    header("٥) الإنترنت")
    targets = [
        ("خدمة أسعار العملات", "open.er-api.com"),
        ("زلازل — USGS", "earthquake.usgs.gov"),
        ("طائرات — OpenSky", "opensky-network.org"),
        ("جوجل — مفتاح Gemini", "generativelanguage.googleapis.com"),
    ]
    down = []
    for label, host in targets:
        try:
            import socket
            socket.create_connection((host, 443), timeout=5).close()
            line(f"    [+] {label:<32}{host}")
        except Exception:
            line(f"    [-] {label:<32}{host}  (لا يرد)")
            down.append(host)
    line()
    if down:
        line("  بعض الخدمات لا ترد. هذا لا يمنع جارفس من العمل: الكرة")
        line("  الأرضية ومواقيت الصلاة والإضافات المحلية تعمل بلا إنترنت.")
    else:
        line("  كل الخدمات ترد — الميزات الحيّة (الزلازل، الطائرات، الأسعار)")
        line("  ستعمل كاملة.")
    return down


# ─────────────────────────────────────────────────────────────────────────────
# المحصّلة
# ─────────────────────────────────────────────────────────────────────────────

def summary(python_ok: bool, missing_boot: list[str], missing_else: list[str],
            missing_voice: list[str], loaded: int, rejected: int) -> None:
    header("الخلاصة وما تفعله الآن")
    todo: list[str] = []

    if not python_ok:
        todo.append("ثبّت Python 3.12 من python.org (وعلّم على Add to PATH)")
    if missing_boot:
        line("  [X] جارفس لن يفتح قبل تثبيت هذه:")
        line(f"        pip install {' '.join(missing_boot)}")
        todo.append("ثبّت حزم الإقلاع")
    elif python_ok:
        line("  [+] كل ما يحتاجه جارفس للإقلاع مثبّت.")

    if missing_else:
        line()
        line("  [-] حزم ناقصة لميزات وإضافات (جارفس يعمل بدونها، وتلك")
        line("      الميزات وحدها لن تعمل):")
        line(f"        pip install {' '.join(missing_else)}")
        todo.append("ثبّت الميزات التي تريدها")

    if loaded:
        line()
        line(f"  [+] الإضافات: {loaded} محمّلة" + (f" و{rejected} مرفوضة" if rejected else " بلا أي رفض"))
    else:
        line()
        line("  [!] لا إضافات محمّلة — انسخ ملفات .py إلى مجلد plugins/")

    line()
    line("  " + "─" * 70)
    if not todo and loaded:
        line("  كل شيء جاهز يا سيدي. شغّل:  python main.py")
    else:
        line("  بالترتيب — افتح الطرفية داخل مجلد جارفس (اكتب cmd في شريط العنوان):")
        line()
        line("    1)  python setup.py")
        line("        هذا هو الطريق الرسمي: يثبّت الحزم المناسبة لنظامك وحدها,")
        line("        وينزّل متصفحات Playwright. لا تحتاج غيره عادةً.")
        line()
        if missing_boot or missing_else:
            line("    2)  وإن أردت التثبيت اليدوي بدلًا منه:")
            if missing_boot:
                line(f"          python -m pip install {' '.join(missing_boot)}")
            if missing_else:
                rest = ' '.join(missing_else)
                line(f"          python -m pip install {rest}")
            line()
        line("    3)  python main.py")
    line("  " + "─" * 70)
    line()
    line("  ملاحظتان توفّران عليك وقتًا:")
    line("    • اسم الحزمة يختلف عن اسم الاستيراد: الكاميرا = opencv-python")
    line("      لا cv2، والصوت = pillow لا PIL. استخدم أسماء pip في الأعلى.")
    line("    • إن فشل أي تثبيت:  python -m pip install --upgrade pip  ثم أعد المحاولة.")
    line()


def main() -> int:
    # UTF-8 للطباعة العربية على ويندوز
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # type: ignore
    except Exception:
        pass

    here = Path(__file__).resolve().parent
    repo = find_repo(here)

    line()
    line(BAR)
    line("   فحص جهازك — جارفس / J.A.R.V.I.S")
    line(BAR)
    line(f"  يعمل من: {here}")
    if repo:
        line(f"  وجدت مشروع جارفس في: {repo}")
    else:
        line("  [!] لم أجد main.py بجانب هذا الملف — سأفحص الإضافات من هنا فقط.")
        line("      لنكمل الفحص كاملًا: شغّل هذا الملف من داخل مجلد جارفس.")

    python_ok, _ = check_python()
    missing_boot, missing_else = check_packages()
    missing_voice = check_voice()
    loaded, rejected, _names = check_plugins(repo)
    check_network()
    summary(python_ok, missing_boot, missing_else, missing_voice, loaded, rejected)
    return 0


# ── الإضافة: قابلة للنداء من جارفس نفسه ──────────────────────────────────────
PLUGIN = {
    "name": "system_check",
    "description": (
        "Full readiness check for this machine and this JARVIS install: Python version, all required "
        "and optional packages with the exact pip line for anything missing, voice model status, how "
        "many plugins loaded and which were rejected, and whether the internet services respond. "
        "Use for: 'check my computer', 'is everything installed', 'what is missing', 'افحص جهازي', "
        "'system check', 'why is something not working — check first'. It changes nothing; it only "
        "reports. Do NOT use it for live performance numbers (that is system_health)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "check (default) | packages | plugins | status | help"},
            "fix": {"type": "STRING", "description": "'yes' to include the ready-to-copy pip line for everything missing"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """يشغّل الفحص ويعيد تقريره نصًّا لجارفس. لا يرمي أبدًا ولا يغيّر أي شيء."""
    del player, session_memory
    import io
    from contextlib import redirect_stdout

    try:
        action = str((parameters or {}).get("action") or "check").strip().lower()
        if action == "help":
            return ("سيدي، فحص الجهاز: action=check (فاتورة كاملة) · packages (المكتبات الناقصة فقط) · "
                    "plugins (المحمَّلة والمرفوضة) · status (سطر واحد) · help")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main()
        text = buffer.getvalue().strip()
        if action == "status":
            keep = [row for row in text.splitlines() if any(k in row for k in ("جاهز", "ناقص", "محمَّل", "ملف"))]
            return "سيدي، حالة الجهاز: " + (" · ".join(keep[:3]) if keep else "الفحص تم بنجاح — التفاصيل: action=check")
        if action == "packages":
            lines = [row for row in text.splitlines() if "pip install" in row or "ناقص" in row or "مفقود" in row]
            if not lines:
                return "سيدي، كل المكتبات المطلوبة موجودة ✔ — لا شيء ناقص."
            return "سيدي، الناقص لتثبيته:\n" + "\n".join(lines[:25])
        if action == "plugins":
            lines = [row for row in text.splitlines() if "إضاف" in row or "محمَّل" in row or "مرفوض" in row]
            return "سيدي، حالة الإضافات:\n" + "\n".join(lines[:15]) if lines else "لم أجد تفاصيل الإضافات."
        return "سيدي، فحص جهازك:\n" + text[-2600:]
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، تعذّر إتمام الفحص ({type(exc).__name__}: {str(exc)[:100]}) — لم أغيّر شيئًا."



if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nأُوقف الفحص.")
        raise SystemExit(130)
