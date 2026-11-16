"""
system_diagnostics.py — the full picture of this machine, on one page.

HOW THIS DIFFERS FROM system_health
    They are not the same job and it would be dishonest to pretend otherwise.

    system_health    asks "is anything wrong right now?" — it watches thresholds
                     and raises an alert when the processor, memory, disk or
                     battery crosses a line. It is for the assistant to notice
                     something on its own.

    this plugin      answers "tell me everything about this machine" — the
                     version of Windows, the Python it is running on, what is
                     loaded, which dependencies are present, how the assistant
                     itself is doing, whether the network is up. It is for when
                     a human is looking at the screen.

    Where they overlap it borrows rather than repeats: the metrics come from the
    project's own actions/system_monitor.get_system_status(), the process digest
    from the Process Manager plugin, and the storage summary from the Storage
    Analyzer. There is one implementation of each measurement in this suite.

WHAT IT DELIBERATELY LEAVES OUT
    No environment variables. No API keys, tokens or passwords — not masked, not
    truncated, simply not read. No full personal paths where a shortened one says
    the same thing. No list of every file the assistant knows about. A diagnostic
    report gets pasted into chats and issue trackers, and it should be safe to
    paste.

SETTINGS (Plugin Manager → Diagnostics)
    include_deps   on — list whether the optional libraries are present
    save_report    off | note | document — keep a copy of the report
"""

from __future__ import annotations

import ast
import importlib.util
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "system_diagnostics",
    "description": (
        "Produces a full diagnostic report about this computer: the system, the Python "
        "runtime, memory and processor, disk space, battery, network, the assistant's own "
        "health, and which optional libraries are installed — 'run a full diagnostic', "
        "'what version of Python is this running on'. Safe to share: no keys, no paths."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "report | quick | deps | save | status"},
            "save": {"type": "STRING", "description": "none | note | document"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "system_diagnostics",
    "title": "Diagnostics / التشخيص الكامل",
    "fields": [
        {"key": "include_deps", "type": "choice", "label": "List optional libraries",
         "default": "on", "choices": ["on", "off"]},
        {"key": "save_report", "type": "choice", "label": "Keep a copy of the report",
         "default": "off", "choices": ["off", "note", "document"]},
    ],
}

# The libraries the project's own requirements.txt brings in, plus the optional
# extras. Names only — this plugin never imports them to find out, it asks the
# metadata, because importing a heavy module to run a diagnostic is a waste.
DEPENDENCIES = (
    "PyQt6", "numpy", "requests", "beautifulsoup4", "playwright", "yt-dlp", "pyautogui",
    "pyperclip", "pillow", "opencv-python", "mss", "psutil", "send2trash", "python-pptx",
    "fastapi", "uvicorn", "cryptography", "python-multipart", "qrcode", "google-genai",
    "ddgs", "pdfplumber", "PyPDF2", "python-docx", "pynvml", "pywin32", "pycaw", "comtypes",
    "pygetwindow", "pywinauto", "openpyxl", "pytesseract", "argostranslate", "reportlab",
    "tinytuya", "paho-mqtt",
)


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("system_diagnostics") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("on", "yes", "true", "1")


def _short(path: str) -> str:
    """'C:/Users/ahmed/...' becomes '~/...'. Keeps a report safe to paste."""
    try:
        text = str(path)
        home = os.path.expanduser("~")
        if home and text.startswith(home):
            return "~" + text[len(home):].replace("\\", "/")
        return Path(text).name if len(text) > 60 else text
    except Exception:
        return ""


def _sibling(name: str):
    """Load a sibling plugin by path — the pattern the whole suite uses."""
    cached = sys.modules.get(f"_jarvis_{name}")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"_jarvis_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(f"_jarvis_{name}", None)
        return None


def _project_metrics() -> tuple[dict[str, Any], str]:
    """
    The project's own system readings. Reused, not re-implemented: system_monitor
    already knows how to read temperatures and GPU on each platform.
    """
    target = Path(__file__).resolve().parent.parent / "actions" / "system_monitor.py"
    if not target.exists():
        return {}, "the project's system_monitor.py is not next to me"
    try:
        spec = importlib.util.spec_from_file_location("_jarvis_sysmon", target)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_jarvis_sysmon"] = module
        spec.loader.exec_module(module)
        return dict(module.get_system_status()), ""
    except Exception as e:
        return {}, f"the project's monitor could not be read ({str(e)[:60]})"


def dependency_report() -> list[tuple[str, str]]:
    """(name, version-or-status) for each library. Never imports them to find out."""
    out: list[tuple[str, str]] = []
    try:
        from importlib.metadata import version
    except Exception:
        return [("importlib.metadata", "unavailable")]
    for name in DEPENDENCIES:
        try:
            out.append((name, version(name)))
        except Exception:
            out.append((name, "not installed"))
    return out


def assistant_health() -> dict[str, Any]:
    """
    How the assistant itself is doing. Its own process, its own memory, how long
    this Python has been running, and whether its plugin folder is where it
    should be. Nothing here leaves the machine.
    """
    info: dict[str, Any] = {"pid": os.getpid(),
                            "running_since": time.strftime("%H:%M:%S", time.localtime(
                                time.time() - _uptime_seconds()))}
    try:
        import psutil
        me = psutil.Process()
        with me.oneshot():
            info["memory_mb"] = round(me.memory_info().rss / 1048576, 1)
            info["threads"] = me.num_threads()
            info["children"] = len(me.children())
    except Exception:
        pass
    plugins = Path(__file__).resolve().parent
    try:
        count = len([p for p in plugins.glob("*.py") if not p.name.startswith("_")])
        info["plugins_folder"] = str(plugins)
        info["plugins_found"] = count
    except Exception:
        pass
    return info


_START = time.time()


def _uptime_seconds() -> float:
    return max(0.0, time.time() - _START)


def network_status() -> dict[str, Any]:
    """
    Whether the machine has a network at all. It does NOT test internet access by
    calling out — a diagnostic should not need the thing it is measuring.
    """
    out: dict[str, Any] = {}
    try:
        import psutil
        stats = psutil.net_if_stats()
        up = [name for name, st in stats.items() if st.isup]
        out["interfaces_up"] = up[:6]
        out["interface_count_up"] = len(up)
        try:
            counters = psutil.net_io_counters()
            out["received_mb"] = round(counters.bytes_recv / 1048576, 1)
            out["sent_mb"] = round(counters.bytes_sent / 1048576, 1)
        except Exception:
            pass
    except Exception as e:
        out["error"] = f"network could not be read ({type(e).__name__})"
    return out


def battery_status() -> dict[str, Any]:
    try:
        import psutil
        battery = psutil.sensors_battery()
        if battery is None:
            return {"present": False, "note": "this machine has no battery (a desktop, or a "
                                              "virtual machine)"}
        seconds = battery.secsleft
        return {"present": True, "percent": round(battery.percent, 1),
                "plugged_in": bool(battery.power_plugged),
                "time_left": "not reported" if seconds in (-1, -2) else f"{seconds // 3600}h "
                                                                         f"{(seconds % 3600) // 60}m"}
    except Exception as e:
        return {"present": False, "note": f"not readable ({type(e).__name__})"}


def build_report(full: bool = True) -> str:
    """
    The report itself. Sections are omitted rather than guessed when a source is
    missing, and the reader is told which source was missing.
    """
    lines: list[str] = []
    stamp = time.strftime("%Y-%m-%d %H:%M")
    lines.append(f"Mark-LV diagnostic report — {stamp}")
    lines.append("=" * 58)

    # ── system ──
    lines.append("\nSYSTEM")
    lines.append(f"  platform      : {platform.system()} {platform.release()} "
                 f"({platform.version()[:60]})")
    lines.append(f"  machine       : {platform.machine()}")
    lines.append(f"  processor     : {platform.processor() or 'not reported'}")
    lines.append(f"  hostname      : {platform.node() or '—'}")
    try:
        import psutil
        lines.append(f"  cores         : {psutil.cpu_count(logical=True)} logical, "
                     f"{psutil.cpu_count(logical=False)} physical")
        boot = psutil.boot_time()
        up = time.time() - boot
        lines.append(f"  machine up for: {int(up // 86400)}d {int((up % 86400) // 3600)}h "
                     f"{int((up % 3600) // 60)}m")
    except Exception:
        pass

    # ── runtime ──
    lines.append("\nRUNTIME")
    lines.append(f"  python        : {sys.version.split()[0]} ({platform.python_implementation()})")
    lines.append(f"  executable    : {_short(sys.executable)}")
    lines.append(f"  working dir   : {_short(os.getcwd())}")
    lines.append(f"  64-bit        : {sys.maxsize > 2 ** 32}")

    # ── live metrics, from the project's own monitor ──
    metrics, why = _project_metrics()
    lines.append("\nLOAD RIGHT NOW" + (f"   (source: {why})" if why else "   (source: the "
                                       "project's own system_monitor)"))
    if metrics:
        lines.append(f"  processor     : {metrics.get('cpu_percent')}%")
        lines.append(f"  memory        : {metrics.get('ram_percent')}% "
                     f"({metrics.get('ram_used_gb')} of {metrics.get('ram_total_gb')} GB)")
        if metrics.get("cpu_temp_c") is not None:
            lines.append(f"  cpu temp      : {metrics.get('cpu_temp_c')} °C")
        if metrics.get("gpu_percent") is not None:
            lines.append(f"  gpu           : {metrics.get('gpu_percent')}%")
        lines.append(f"  processes     : {metrics.get('process_count')}")
        lines.append(f"  uptime        : {metrics.get('uptime')}")
    else:
        lines.append("  (the project's monitor was not reachable, so this section is empty "
                     "rather than invented)")

    # ── storage, from the storage plugin ──
    lines.append("\nSTORAGE")
    storage = _sibling("storage_analyzer")
    if storage is not None and hasattr(storage, "disks"):
        try:
            for part in storage.disks()[:4]:
                lines.append(f"  {part['mount']:<18} {part['percent']:>5.1f}% used · "
                             f"{storage.human(part['free'])} free of "
                             f"{storage.human(part['total'])}")
        except Exception as e:
            lines.append(f"  (could not be read: {str(e)[:60]})")
    else:
        lines.append("  (the storage plugin is not next to me, so this section is empty — "
                     "I will not guess at your disk figures)")

    # ── battery ──
    battery = battery_status()
    lines.append("\nBATTERY")
    if battery.get("present"):
        lines.append(f"  charge        : {battery['percent']}%"
                     + (", plugged in" if battery["plugged_in"] else ", on battery")
                     + f" · time left {battery['time_left']}")
    else:
        lines.append(f"  {battery.get('note', 'not available')}")

    # ── network ──
    net = network_status()
    lines.append("\nNETWORK")
    if "error" in net:
        lines.append(f"  {net['error']}")
    else:
        lines.append(f"  interfaces up : {net.get('interface_count_up', 0)} "
                     f"({', '.join(net.get('interfaces_up', [])) or 'none'})")
        if "received_mb" in net:
            lines.append(f"  since boot    : {net['received_mb']} MB received, "
                         f"{net['sent_mb']} MB sent")
        lines.append("  (no test call was made — a diagnostic should not need the network it "
                     "is measuring)")

    # ── the assistant itself ──
    health = assistant_health()
    lines.append("\nTHE ASSISTANT ITSELF")
    lines.append(f"  process       : pid {health.get('pid')}, "
                 f"{health.get('memory_mb', '?')} MB resident, "
                 f"{health.get('threads', '?')} thread(s)")
    lines.append(f"  running since : {health.get('running_since')}")
    lines.append(f"  children      : {health.get('children', 0)}")
    lines.append(f"  plugin folder : {_short(health.get('plugins_folder', ''))} "
                 f"({health.get('plugins_found', '?')} plugins)")

    processes = _sibling("process_manager")
    if processes is not None and hasattr(processes, "summary"):
        try:
            digest = processes.summary()
            if digest.get("available"):
                top = ", ".join(f"{n} ({m} MB)" for n, m in digest["top"][:3])
                lines.append(f"  biggest procs : {top}   (from the Process Manager plugin)")
        except Exception:
            pass

    # ── dependencies ──
    if _truthy(_cfg().get("include_deps")):
        deps = dependency_report()
        present = [f"{n} {v}" for n, v in deps if v != "not installed"]
        missing = [n for n, v in deps if v == "not installed"]
        lines.append(f"\nLIBRARIES ({len(present)} of {len(deps)} present)")
        lines.append("  " + ", ".join(present[:18]) + ("…" if len(present) > 18 else ""))
        if missing:
            lines.append(f"  not installed ({len(missing)}): " + ", ".join(missing[:18]))
            lines.append("  (each of those belongs to one optional feature — nothing here is "
                         "required for the assistant to run)")

    if full:
        lines.append("\nPRIVACY")
        lines.append("  This report contains no keys, tokens, passwords or environment "
                     "variables, and personal paths are shortened to ~. It is safe to share.")

    return "\n".join(lines)


def quick() -> str:
    """The short version, for when the full report is more than was asked for."""
    metrics, why = _project_metrics()
    health = assistant_health()
    lines = []
    if metrics:
        lines.append(f"Processor {metrics.get('cpu_percent')}% · "
                     f"memory {metrics.get('ram_percent')}% · "
                     f"{metrics.get('process_count')} processes")
    else:
        lines.append(f"(the project's monitor was not reachable: {why})")
    storage = _sibling("storage_analyzer")
    if storage is not None:
        try:
            for part in storage.disks()[:2]:
                lines.append(f"{part['mount']}: {part['percent']}% used, "
                             f"{storage.human(part['free'])} free")
        except Exception:
            pass
    battery = battery_status()
    lines.append(f"Battery {battery['percent']}%"
                 + (", plugged in" if battery.get("plugged_in") else "")
                 if battery.get("present") else battery.get("note", "battery: not available"))
    lines.append(f"{platform.system()} {platform.release()} · Python "
                 f"{sys.version.split()[0]} · assistant {health.get('memory_mb', '?')} MB")
    return "\n".join(lines)


def _save(body: str, how: str) -> str:
    if how == "note":
        notes = _sibling("note_manager")
        if notes is None:
            return "\n(I could not save it: the note manager is not next to me.)"
        result = notes.add_note("Diagnostic report " + time.strftime("%Y-%m-%d %H:%M"),
                                body, ["diagnostics", "system"], "system",
                                source="system_diagnostics")
        return (f"\n(Saved as note {result['note']['id']}.)" if result.get("ok")
                else f"\n(I could not save it: {result.get('reason')})")
    if how == "document":
        docgen = _sibling("document_generator")
        if docgen is None:
            return "\n(I could not save it: the document generator is not next to me.)"
        result = docgen.generate("Diagnostic report " + time.strftime("%Y-%m-%d"),
                                 body, fmt="docx", filename="diagnostic_report")
        return (f"\n(Saved to {result['path']})" if result.get("ok")
                else f"\n(I could not save it: {result.get('reason')})")
    return ""


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one diagnostics action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "report").strip().lower()

        if action in ("report", "full", "diagnose", "diagnostic"):
            body = build_report(full=True)
            if player:
                try:
                    player.write_log("[Diagnostics] full report built")
                except Exception:
                    pass
                try:
                    player.show_content("Diagnostic report", body)
                except Exception:
                    pass
            how = str(p.get("save") or _cfg().get("save_report") or "off").strip().lower()
            if how == "none":
                how = "off"
            return body + _save(body, how)

        if action in ("quick", "brief", "summary"):
            return quick()

        if action in ("deps", "dependencies", "libraries"):
            deps = dependency_report()
            present = [f"{n} {v}" for n, v in deps if v != "not installed"]
            missing = [n for n, v in deps if v == "not installed"]
            out = [f"{len(present)} of {len(deps)} optional libraries are installed.",
                   "", "Installed:"]
            out += [f"  {x}" for x in present]
            if missing:
                out += ["", "Not installed (each belongs to one optional feature):"]
                out += [f"  {x}" for x in missing]
            return "\n".join(out)

        if action == "save":
            body = build_report(full=True)
            how = str(p.get("save") or "document").strip().lower()
            return body + _save(body, how if how in ("note", "document") else "document")

        if action in ("status", "info"):
            return ("Diagnostics builds a full report from: the project's own system_monitor, "
                    "the Storage Analyzer plugin, the Process Manager plugin, psutil and the "
                    "standard library.\n"
                    "It reads no environment variables and no stored secrets — not masked, "
                    "simply never read — and shortens personal paths to ~.\n"
                    "Unlike system_health (which watches for problems and alerts you), this "
                    "one is for when you actually want to look at the machine.")

        return (f"Sir, '{action}' is not a diagnostics action I have. I can build the full "
                f"report, give a quick summary, list libraries, or save a copy.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the diagnostics plugin hit a problem: {str(e)[:150]}"


# ── self-test: python system_diagnostics.py ──────────────────────────────────

def _selftest() -> int:
    print("system_diagnostics — self-test")
    problems = 0

    body = build_report(full=True)
    print(f"  report built        : {len(body.splitlines())} lines, {len(body)} chars")
    for section in ("SYSTEM", "RUNTIME", "LOAD RIGHT NOW", "STORAGE", "BATTERY",
                    "NETWORK", "THE ASSISTANT ITSELF", "PRIVACY"):
        present = section in body
        problems += 0 if present else 1
        print(f"  section {section:<22} {'✓' if present else '✗'}")

    answers = [l for l in body.splitlines() if ":" in l and "answer" in l.lower()]
    print(f"  'answer' placeholders: {len(answers)} (must be 0 — no invented values)")
    problems += 0 if not answers else 1

    # the privacy promise, checked rather than asserted
    import re as _re
    leak_patterns = {
        "an OpenAI-style key": _re.compile(r"sk-[A-Za-z0-9]{16,}"),
        "a Google key": _re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),
        "a Telegram token": _re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}"),
        "a password field": _re.compile(r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S"),
        "a bearer token": _re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    }
    for label, pattern in leak_patterns.items():
        hit = pattern.search(body)
        problems += 0 if not hit else 1
        print(f"  no {label:<20} {'✓' if not hit else '✗ FOUND: ' + str(hit.group(0))[:30]}")

    home = os.path.expanduser("~")
    raw_home = [l for l in body.splitlines() if home and home in l and "plugin folder" not in l]
    print(f"  no raw home paths   : {'✓' if not raw_home else '✗ ' + str(len(raw_home)) + ' line(s)'}")
    problems += 0 if not raw_home else 1

    # The promise is about the CODE, not the wording: the report itself says the
    # word "environment" in its privacy note. What must be absent is any actual
    # read of the environment — so this looks at the syntax tree, where a real
    # read is an attribute or a call, and a mention in a string is neither.
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    reads: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            reads.append("os.environ")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "getenv":
            reads.append("getenv()")
    print(f"  environment read    : {'NONE — the report never touches it' if not reads else str(reads)}"
          f"  {'✓' if not reads else '✗'}")
    problems += 0 if not reads else 1

    metrics, why = _project_metrics()
    print(f"  project monitor     : {'reused ✓' if metrics else 'not reachable — ' + why[:50]}")
    problems += 0 if metrics else 0     # absence is reported honestly, not a failure

    deps = dependency_report()
    print(f"  dependencies        : {sum(1 for _, v in deps if v != 'not installed')}/{len(deps)} installed")
    missing = [n for n, v in deps if v == "not installed"]
    print(f"  example missing     : {missing[:5]}")

    q = quick()
    print(f"  quick summary       : {q.splitlines()[0][:66]}")
    problems += 0 if q and len(q) > 20 else 1
    print(f"  deps action         : {run({'action': 'deps'}).splitlines()[0][:60]}")
    print(f"  bad action          : {run({'action': 'sing'})[:68]}")
    problems += 0 if "not a diagnostics action" in run({"action": "sing"}) else 1
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
