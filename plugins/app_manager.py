"""
app_manager.py — finding and starting the programs on this machine.

WHAT IT ACTUALLY DOES
    It builds an index of the applications this computer says it has installed:
    the Start Menu on Windows, the .desktop entries on Linux, the Applications
    folders on macOS. Then it searches that index and starts things from it.

WHY THE INDEX MATTERS, AND NOT ONLY FOR SPEED
    The project already has an application launcher — actions/open_app.py — and
    this plugin hands the launch to it rather than writing a second one. But it
    only ever passes a name that came OUT of the index. A name invented by the
    model, or half-remembered, or containing a shell character, is refused with
    the list of what does exist.

    That is the whole safety story: the set of things this plugin can start is
    the set of things already installed, and nothing else can be typed in.

SCRIPTS AND ARGUMENTS
    .desktop entries on Linux sometimes carry arguments ("Exec=code --new-window").
    This plugin reads the program name and ignores the arguments, and it never
    runs a .lnk or a shell one-liner. If a program needs arguments, start it
    yourself once — this is for opening apps, not for scripting them.

SETTINGS (Plugin Manager → App manager)
    extra_dirs   comma-separated extra folders to scan for apps
    max_apps     600 — ceiling on the index
    refresh_days 7 — re-scan automatically after this long
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "app_manager",
    "description": (
        "Finds and opens the programs installed on this computer: 'what apps do I "
        "have', 'do I have Photoshop installed', 'open Firefox'. It knows what is "
        "actually installed and will not try to start anything else."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | search | open | refresh | status"},
            "name": {"type": "STRING", "description": "Application name to find or open"},
            "limit": {"type": "NUMBER", "description": "How many to list, default 15"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "app_manager",
    "title": "App manager / مدير التطبيقات",
    "fields": [
        {"key": "extra_dirs", "type": "text", "label": "Extra folders to scan",
         "default": "", "placeholder": "D:/Portable, C:/Tools"},
        {"key": "max_apps", "type": "text", "label": "Most apps to index", "default": "600"},
        {"key": "refresh_days", "type": "text", "label": "Re-scan after this many days",
         "default": "7"},
    ],
}

CACHE = Path(__file__).resolve().parent.parent / "config" / ".plugin_cache" / "apps.json"


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("app_manager") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# ── scanning what is installed ────────────────────────────────────────────────

def _scan_dirs() -> list[Path]:
    """The folders each operating system keeps its application shortcuts in."""
    plat = platform_name()
    dirs: list[Path] = []
    if plat == "windows":
        for env in ("ProgramData", "APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base and env == "ProgramData":
                dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
            elif base:
                dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    elif plat == "macos":
        dirs += [Path("/Applications"), Path.home() / "Applications"]
    else:
        dirs += [Path("/usr/share/applications"),
                 Path("/usr/local/share/applications"),
                 Path.home() / ".local" / "share" / "applications"]
    for extra in re.split(r"[,\n;]", str(_cfg().get("extra_dirs") or "")):
        extra = extra.strip().strip('"')
        if extra:
            dirs.append(Path(extra).expanduser())
    return [d for d in dirs if d.exists()]


def _clean_name(raw: str) -> str:
    """A tidy name from a filename: no extension, no 'Shortcut to', no .lnk noise."""
    name = Path(str(raw)).stem
    name = re.sub(r"(?i)^shortcut to\s+", "", name)
    name = re.sub(r"\s*-\s*Shortcut$", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120]


def _from_desktop_file(path: Path) -> str:
    """Read the Name= line of a .desktop entry. Returns "" when it is not for users."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    if "NoDisplay=true" in text:
        return ""
    match = re.search(r"^Name=(.+)$", text, re.M)
    return _clean_name(match.group(1)) if match else ""


def scan(force: bool = False) -> list[dict[str, Any]]:
    """
    Build (or reuse) the index of installed applications.

    Each entry: {name, path, kind, platform}. Every path was produced by the
    operating system's own listing, never by the caller.
    """
    cached = _load_cache()
    if cached and not force:
        age_days = (time.time() - cached.get("scanned", 0)) / 86400
        if age_days < _int_cfg("refresh_days", 7, 1, 365):
            return cached.get("apps", [])

    cap = _int_cfg("max_apps", 600, 10, 5000)
    plat = platform_name()
    apps: list[dict[str, Any]] = []
    seen: set[str] = set()

    for folder in _scan_dirs():
        try:
            entries = sorted(folder.rglob("*"))
        except Exception:
            continue
        for entry in entries:
            if len(apps) >= cap:
                break
            try:
                suffix = entry.suffix.lower()
                if plat == "windows" and suffix == ".lnk":
                    name = _clean_name(entry.name)
                elif plat == "linux" and suffix == ".desktop":
                    name = _from_desktop_file(entry)
                elif plat == "macos" and suffix == ".app":
                    name = _clean_name(entry.name)
                else:
                    continue
            except Exception:
                continue
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            apps.append({"name": name, "path": str(entry), "kind": suffix.lstrip("."),
                         "platform": plat})

    # Whatever PATH knows about as well — a shortcut does not always exist.
    for tool in ("firefox", "chromium", "google-chrome", "code", "vlc", "gimp", "libreoffice",
                 "thunderbird", "spotify", "steam", "obs", "notepad", "calc", "explorer"):
        found = shutil.which(tool)
        if found and tool.lower() not in seen and len(apps) < cap:
            seen.add(tool.lower())
            apps.append({"name": tool, "path": found, "kind": "path", "platform": plat})

    apps.sort(key=lambda a: a["name"].lower())
    _save_cache({"apps": apps, "scanned": time.time(), "platform": plat})
    return apps


def _load_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("apps"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def _save_cache(data: dict[str, Any]) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def _score(query: str, name: str) -> int:
    """Exact beats prefix beats substring — the same order a person would use."""
    q, n = query.lower().strip(), name.lower()
    if q == n:
        return 100
    if n.startswith(q):
        return 80
    if any(word == q for word in re.split(r"[\s\-_.]+", n)):
        return 70
    if q in n:
        return 50
    if all(part in n for part in q.split()):
        return 30
    return 0


def find(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    Search the installed applications. Returns matches best-first, and an empty
    list when nothing matches — the caller says so rather than guessing.
    """
    query = (query or "").strip()
    if not query:
        return []
    scored = []
    for app in scan():
        score = _score(query, app["name"])
        if score:
            scored.append((score, app))
    scored.sort(key=lambda x: (-x[0], x[1]["name"].lower()))
    return [app for _, app in scored[:max(1, limit)]]


def resolve(name: str) -> tuple[dict[str, Any] | None, str]:
    """
    Turn a spoken name into exactly one installed application. (app, why-not).

    Ambiguity is reported, never resolved by picking the first one: starting the
    wrong program on someone's screen is not a harmless guess.
    """
    query = (name or "").strip()
    if not query:
        return None, "you did not give me a name"
    apps = scan()
    if not apps:
        return None, ("I could not find any application list on this machine — the "
                      "Start Menu (or the equivalent) does not seem readable")
    matches = find(query, limit=5)
    if not matches:
        return None, f"nothing called '{query}' is installed as far as I can see"
    exact = [a for a in matches if a["name"].lower() == query.lower()]
    if exact:
        return exact[0], ""
    if len(matches) == 1:
        return matches[0], ""
    names = ", ".join(a["name"] for a in matches[:4])
    return None, f"'{query}' could match several: {names} — say which one"


def launch(name: str, player=None) -> tuple[bool, str]:
    """
    Start an installed application by handing it to the project's own launcher.

    Returns (started, message). The name is resolved against the index first, so
    only something the operating system already lists can ever be started.
    """
    app, why = resolve(name)
    if app is None:
        return False, why

    opener = _project_action("open_app")
    if opener is None:
        return False, ("I cannot reach the project's own launcher (actions/open_app.py) — "
                       "I will not start programs by another route")
    try:
        result = opener.open_app({"action": "open", "app_name": app["name"]}, player=player)
        text = str(result or "")
    except Exception as e:
        return False, f"the launcher failed: {str(e)[:90]}"
    lowered = text.lower()
    failed = ("could not" in lowered or "not found" in lowered or "failed" in lowered
              or "unavailable" in lowered)
    if failed:
        return False, f"the launcher could not start {app['name']}: {text[:110]}"
    return True, f"Opening {app['name']}, sir."


def _project_action(module_name: str):
    """
    Load one of the project's own actions by path.

    Reuse, not duplication: open_app already knows how each operating system
    starts a program, and this plugin does not pretend to know better.
    """
    cached = sys.modules.get(f"_jarvis_act_{module_name}")
    if cached is not None:
        return cached
    target = Path(__file__).resolve().parent.parent / "actions" / f"{module_name}.py"
    if not target.exists():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"_jarvis_act_{module_name}", target)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(f"_jarvis_act_{module_name}", None)
        return None


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one app action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "list").strip().lower()
        limit = int(float(p.get("limit") or 15))

        if action in ("list", "all", "apps"):
            apps = scan()
            if not apps:
                return (f"I cannot find an application list on this {platform_name()} machine, "
                        f"sir. On Windows that is the Start Menu; if it is in a unusual place, "
                        f"add the folder to 'extra_dirs' in my settings.")
            head = f"{len(apps)} application(s) installed, sir. The first {min(limit, len(apps))}:"
            return head + "\n" + "\n".join(f"  {a['name']}" for a in apps[:limit])

        if action in ("search", "find", "do_i_have", "have"):
            name = str(p.get("name") or p.get("query") or "").strip()
            if not name:
                return "Which application, sir?"
            matches = find(name, limit=max(1, limit))
            if not matches:
                return (f"Nothing called '{name}' is installed here as far as I can see, sir. I "
                        f"looked at what the Start Menu (or equivalent) lists.")
            lines = [f"{len(matches)} match(es) for '{name}':"]
            for a in matches:
                lines.append(f"  {a['name']}  ({a['kind']})")
            return "\n".join(lines)

        if action in ("open", "launch", "start", "run"):
            name = str(p.get("name") or p.get("query") or "").strip()
            if not name:
                return "Which application should I open, sir?"
            if player:
                try:
                    player.write_log(f"[Apps] opening {name[:40]}")
                except Exception:
                    pass
            started, message = launch(name, player)
            return message

        if action in ("refresh", "rescan", "rebuild"):
            apps = scan(force=True)
            return f"Re-scanned, sir: {len(apps)} application(s) found on this {platform_name()} machine."

        if action in ("status", "info"):
            cached = _load_cache()
            age = ""
            if cached.get("scanned"):
                hours = (time.time() - cached["scanned"]) / 3600
                age = f"{hours:.0f} hour(s) ago" if hours < 48 else f"{hours / 24:.0f} day(s) ago"
            return (f"Platform   : {platform_name()}\n"
                    f"Indexed    : {len(cached.get('apps', []))} application(s)"
                    + (f" — scanned {age}" if age else " — not scanned yet") + "\n"
                    f"Index file : {CACHE}\n"
                    f"Safety     : only names from that index can be started; nothing typed "
                    f"in by the model ever reaches a shell")

        return (f"Sir, '{action}' is not an app action I have. I can list, search, open, "
                f"refresh the index, or report status.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the app manager hit a problem: {str(e)[:150]}"


# ── self-test: python app_manager.py ─────────────────────────────────────────

def _selftest() -> int:
    print("app_manager — self-test")
    problems = 0
    print(f"  platform            : {platform_name()}")
    print(f"  scan folders        : {[str(d) for d in _scan_dirs()][:3]}")

    apps = scan(force=True)
    print(f"  indexed             : {len(apps)} application(s)")
    if apps:
        print(f"  sample              : {[a['name'] for a in apps[:5]]}")
        problems += 0 if all(a.get("path") for a in apps) else 1

    for value, expect in (("zzzznotanapp", "nothing called"),):
        app, why = resolve(value)
        print(f"  unknown name        : {why[:74]}")
        problems += 0 if app is None and expect in why else 1

    app, why = resolve("")
    print(f"  empty name          : {why}")
    problems += 0 if app is None else 1

    # scoring is what decides search quality; test it directly, no apps needed
    cases = [("firefox", "Firefox", 100), ("fire", "Firefox", 80), ("fox", "Firefox", 50),
             ("visual studio", "Visual Studio Code", 80), ("calc", "Calculator Plus", 80),
             ("nonsense", "Firefox", 0)]
    for query, name, want in cases:
        got = _score(query, name)
        ok = "✓" if got == want else "✗"
        problems += 0 if got == want else 1
        print(f"  score {query!r:<16} vs {name!r:<22} = {got:<4} {ok}")

    print(f"  clean name          : {_clean_name('Shortcut to Photoshop 2024.lnk')!r}")
    problems += 0 if _clean_name("Shortcut to Photoshop 2024.lnk") == "Photoshop 2024" else 1

    # the launcher must be reachable but never invoked during a test
    opener = _project_action("open_app")
    print(f"  project launcher    : {'reachable' if opener else 'NOT reachable (actions/open_app.py)'}")
    print(f"  launch unknown      : {launch('zzzznotanapp')[1][:66]}")
    problems += 0 if not launch("zzzznotanapp")[0] else 1

    print(f"  list action         : {run({'action': 'list', 'limit': 3}).splitlines()[0][:70]}")
    print(f"  search action       : {run({'action': 'search', 'name': 'zzzz'})[:76]}")
    problems += 0 if "Nothing called" in run({"action": "search", "name": "zzzz"}) else 1
    print(f"  bad action          : {run({'action': 'sing'})[:74]}")
    problems += 0 if "not an app action" in run({"action": "sing"}) else 1
    print(f"  status              : {run({'action': 'status'}).splitlines()[1][:66]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
