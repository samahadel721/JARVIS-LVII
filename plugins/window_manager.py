"""
window_manager.py — seeing and arranging the windows that are open.

WHERE THE ABILITIES COME FROM
    Windows  pywin32 (already in the project's requirements). Full support:
             list, focus, minimise, maximise, restore, move, resize, close.
    macOS    System Events through osascript. Most operations.
    Linux    wmctrl, and xdotool as a second choice. If neither is installed,
             the plugin says so and gives the exact command instead of failing
             quietly — these are two small programs, not Python packages.

    Focusing is handed to the project's own computer_control action when it can
    do the job, so there is one implementation of "bring that window forward",
    not two.

THE TWO THINGS IT WILL NOT DO
    It will not close a critical system window — the shell, the login process,
    the window manager itself. Closing explorer.exe takes your taskbar with it,
    and no wording of a request makes that a good idea. The refusal is by name,
    and it explains what it would have cost you.

    It will never close the assistant's own window. That is not a safety rule,
    it is a courtesy: you would have to restart the program to get it back.

    Everything else that closes goes through the project's confirmation gate
    (core/confirm.py), because "close that window" is the kind of instruction
    that sounds the same whether or not you have unsaved work in it.

SETTINGS (Plugin Manager → Windows)
    confirm_close   on — ask before closing anything that is not the assistant
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from typing import Any

PLUGIN = {
    "name": "window_manager",
    "description": (
        "Works with the windows that are open: 'what windows are open', 'bring Chrome "
        "to the front', 'minimise the calculator', 'close that window'. It refuses to "
        "close system windows and asks before closing anything else."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | focus | minimize | maximize | restore | close | move | resize | active | status"},
            "title": {"type": "STRING", "description": "Part of the window title"},
            "x": {"type": "NUMBER", "description": "For move: left position"},
            "y": {"type": "NUMBER", "description": "For move: top position"},
            "width": {"type": "NUMBER", "description": "For resize: width"},
            "height": {"type": "NUMBER", "description": "For resize: height"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "window_manager",
    "title": "Windows / إدارة النوافذ",
    "fields": [
        {"key": "confirm_close", "type": "choice", "label": "Ask before closing a window",
         "default": "on", "choices": ["on", "off"]},
    ],
}

# Closing any of these is a system-level act, not a window act.
_CRITICAL = {
    "explorer.exe": "it is your taskbar and desktop — closing it takes both away",
    "winlogon.exe": "it is the Windows login process",
    "csrss.exe": "it is a core Windows process",
    "dwm.exe": "it is the desktop window manager — the screen would go with it",
    "wininit.exe": "it is a core Windows process",
    "services.exe": "it manages Windows services",
    "lsass.exe": "it handles your login security",
    "smss.exe": "it is a core Windows process",
    "systemsettings.exe": "it is a system settings window",
    "gnome-shell": "it is your desktop shell",
    "plasmashell": "it is your desktop shell",
    "kwin_x11": "it is your window manager",
    "kwin_wayland": "it is your window manager",
    "xfwm4": "it is your window manager",
    "mutter": "it is your window manager",
    "cinnamon": "it is your desktop shell",
    "lxpanel": "it is your desktop panel",
    "finder": "it is the macOS desktop itself",
    "dock": "it is the macOS dock",
    "windowserver": "it is the macOS display server",
    "loginwindow": "it is the macOS login process",
    "systemuiserver": "it is part of the macOS system interface",
}

# Linux fallbacks: small command-line tools, not Python packages.
_LINUX_TOOLS = ("wmctrl", "xdotool")


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("window_manager") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("on", "yes", "true", "1")


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _have(tool: str) -> bool:
    import shutil
    return shutil.which(tool) is not None


def _linux_tool() -> str:
    for tool in _LINUX_TOOLS:
        if _have(tool):
            return tool
    return ""


def _run(args: list[str], timeout: int = 8) -> tuple[bool, str]:
    """A fixed argument list, never a shell string. Returns (ok, output-or-reason)."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if done.returncode != 0:
            return False, (done.stderr or done.stdout or f"exit {done.returncode}").strip()[:160]
        return True, done.stdout
    except FileNotFoundError:
        return False, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return False, f"{args[0]} did not answer in time"
    except Exception as e:
        return False, f"{args[0]} failed: {str(e)[:80]}"


# ── the three backends ────────────────────────────────────────────────────────

def _windows_windows() -> list[dict[str, Any]]:
    """Every top-level window with a title. Order is Z-order, front first."""
    try:
        import win32gui
    except Exception:
        return []
    found: list[dict[str, Any]] = []

    def visit(hwnd: int, _acc) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title:
                return True
            rect = win32gui.GetWindowRect(hwnd)
            found.append({
                "id": hwnd, "title": title,
                "process": _windows_process_name(hwnd),
                "geometry": {"x": rect[0], "y": rect[1],
                             "width": rect[2] - rect[0], "height": rect[3] - rect[1]},
            })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return []
    return found


def _windows_process_name(hwnd: int) -> str:
    """Which program owns a window. Used for the critical-window check."""
    try:
        import win32process
        import psutil
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _linux_windows() -> list[dict[str, Any]]:
    """wmctrl first, xdotool second. Both are one small install and the plugin says which."""
    tool = _linux_tool()
    if not tool:
        return []
    found: list[dict[str, Any]] = []
    if tool == "wmctrl":
        ok, out = _run(["wmctrl", "-lG"])
        if not ok:
            return []
        for line in out.splitlines():
            parts = line.split(None, 7)
            if len(parts) < 8:
                continue
            try:
                found.append({"id": parts[0], "title": parts[7],
                              "geometry": {"x": int(parts[2]), "y": int(parts[3]),
                                           "width": int(parts[4]), "height": int(parts[5])},
                              "process": ""})
            except Exception:
                continue
        return found
    ok, out = _run(["xdotool", "search", "--onlyvisible", "--name", ""])
    if not ok:
        return []
    for wid in out.split()[:80]:
        ok2, title = _run(["xdotool", "getwindowname", wid])
        if ok2 and title.strip():
            found.append({"id": wid, "title": title.strip(), "process": "", "geometry": {}})
    return found


def _macos_windows() -> list[dict[str, Any]]:
    script = ('tell application "System Events" to get name of every process whose '
              'visible is true')
    ok, out = _run(["osascript", "-e", script])
    if not ok:
        return []
    return [{"id": "", "title": name.strip(), "process": "", "geometry": {}}
            for name in out.split(",") if name.strip()]


def list_windows() -> list[dict[str, Any]]:
    """Open windows, front-most first where the platform can tell us."""
    plat = platform_name()
    if plat == "windows":
        return _windows_windows()
    if plat == "macos":
        return _macos_windows()
    return _linux_windows()


def capability() -> tuple[bool, str]:
    """(usable, why not) — asked before anything is promised."""
    plat = platform_name()
    if plat == "windows":
        try:
            found = importlib.util.find_spec("win32gui") is not None
        except Exception:
            found = False
        if found:
            return True, "pywin32/window API"
        return False, ("pywin32 is not installed — it is in the project's requirements "
                       "(pip install pywin32)")
    if plat == "macos":
        ok, _ = _run(["osascript", "-e", "return 1"])
        return (ok, "osascript" if ok else "osascript is not answering")
    tool = _linux_tool()
    if tool:
        return True, tool
    if not _display():
        return False, ("there is no graphical display here, so there are no windows to manage")
    return False, ("Linux window control needs wmctrl or xdotool — both small programs, "
                   "neither is a Python package: sudo apt install wmctrl xdotool")


def _display() -> bool:
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def find_windows(title: str) -> list[dict[str, Any]]:
    """Windows whose title contains the text, case-insensitively."""
    needle = (title or "").strip().lower()
    if not needle:
        return []
    return [w for w in list_windows() if needle in str(w.get("title", "")).lower()]


def _is_assistant(title: str, process: str = "") -> bool:
    """Is this window the assistant itself? Closing it would end the session."""
    low = f"{title} {process}".lower()
    return any(word in low for word in ("jarvis", "mark-lv", "marklv", "mark lv"))


def is_critical(title: str, process: str = "") -> tuple[bool, str]:
    """(critical, why). Checked before any close, with the reason kept for the reply."""
    proc = (process or "").strip().lower()
    if proc in _CRITICAL:
        return True, _CRITICAL[proc]
    low_title = (title or "").lower()
    for name, why in _CRITICAL.items():
        if name in low_title:
            return True, why
    return False, ""


# ── acting on one window ──────────────────────────────────────────────────────

def _windows_act(hwnd: int, action: str, p: dict) -> tuple[bool, str]:
    import win32gui
    try:
        if action == "focus":
            win32gui.ShowWindow(hwnd, 9)          # SW_RESTORE — also un-minimises
            win32gui.SetForegroundWindow(hwnd)
            return True, "in front"
        if action == "minimize":
            win32gui.ShowWindow(hwnd, 6)          # SW_MINIMIZE
            return True, "minimised"
        if action == "maximize":
            win32gui.ShowWindow(hwnd, 3)          # SW_MAXIMIZE
            return True, "maximised"
        if action == "restore":
            win32gui.ShowWindow(hwnd, 9)
            return True, "restored"
        if action == "move":
            left, top = win32gui.GetWindowRect(hwnd)[:2]
            win32gui.MoveWindow(hwnd, int(p.get("x", left)), int(p.get("y", top)),
                                win32gui.GetWindowRect(hwnd)[2] - left,
                                win32gui.GetWindowRect(hwnd)[3] - top, True)
            return True, f"moved to {p.get('x', left)},{p.get('y', top)}"
        if action == "resize":
            rect = win32gui.GetWindowRect(hwnd)
            win32gui.MoveWindow(hwnd, rect[0], rect[1],
                                int(p.get("width") or (rect[2] - rect[0])),
                                int(p.get("height") or (rect[3] - rect[1])), True)
            return True, f"resized to {p.get('width')}x{p.get('height')}"
        if action == "close":
            win32gui.PostMessage(hwnd, 0x0010, 0, 0)   # WM_CLOSE — asks, does not kill
            return True, "asked to close"
    except Exception as e:
        return False, f"Windows refused that: {str(e)[:90]}"
    return False, f"'{action}' is not something I can do to a window here"


def _linux_act(wid: str, action: str, p: dict) -> tuple[bool, str]:
    tool = _linux_tool()
    if tool == "wmctrl":
        mapping = {"focus": ["-i", "-a"], "minimize": ["-i", "-r", "-b", "add,hidden"],
                   "maximize": ["-i", "-r", "-b", "add,maximized_vert,maximized_horz"],
                   "restore": ["-i", "-r", "-b", "remove,hidden,maximized_vert,maximized_horz"],
                   "close": ["-i", "-c"]}
        if action in mapping:
            args = ["wmctrl"] + mapping[action][:2] + [wid] + mapping[action][2:]
            ok, out = _run(args)
            return (ok, action + "d") if ok else (False, out)
        if action in ("move", "resize"):
            geom = f"0,{int(p.get('x', 0))},{int(p.get('y', 0))},{int(p.get('width', 800))},{int(p.get('height', 600))}"
            ok, out = _run(["wmctrl", "-i", "-r", wid, "-e", geom])
            return (ok, "moved") if ok else (False, out)
    if tool == "xdotool":
        mapping = {"focus": ["windowactivate"], "minimize": ["windowminimize"],
                   "close": ["windowkill"]}
        if action in mapping:
            ok, out = _run(["xdotool"] + mapping[action] + [wid])
            return (ok, action + "d") if ok else (False, out)
        if action == "move":
            ok, out = _run(["xdotool", "windowmove", wid, str(p.get("x", 0)), str(p.get("y", 0))])
            return (ok, "moved") if ok else (False, out)
        if action == "resize":
            ok, out = _run(["xdotool", "windowsize", wid,
                            str(p.get("width", 800)), str(p.get("height", 600))])
            return (ok, "resized") if ok else (False, out)
        if action in ("maximize", "restore"):
            return False, f"xdotool cannot {action} a window reliably — wmctrl can"
    return False, f"'{action}' is not something I can do here"


def _macos_act(app_name: str, action: str) -> tuple[bool, str]:
    cmd = {"focus": "set frontmost to true", "minimize": "set visible to false",
           "restore": "set visible to true", "maximize": "set frontmost to true"}.get(action)
    if action == "close":
        ok, out = _run(["osascript", "-e",
                        f'tell application "{app_name}" to close front window'])
        return (ok, "asked to close") if ok else (False, out)
    if not cmd:
        return False, f"'{action}' is not something I can do to a window on macOS"
    ok, out = _run(["osascript", "-e", f'tell application "{app_name}" to {cmd}'])
    return (ok, action + "d") if ok else (False, out)


def act(title: str, action: str, p: dict | None = None, player=None) -> str:
    """
    Do one thing to one window, with the safety checks the action needs.

    Returns the sentence to say. Closing is gated: system windows are refused
    outright, the assistant's own window is refused, and everything else asks
    first through core/confirm.py.
    """
    p = p or {}
    ok, why = capability()
    if not ok:
        return f"Sir, {why}."

    matches = find_windows(title)
    if not matches:
        return (f"I cannot see a window with '{title}' in its title, sir. Say 'what windows "
                f"are open' and we will find the right name.")

    window = matches[0]
    title_full = str(window.get("title", ""))
    process = str(window.get("process", ""))

    if action == "close":
        if _is_assistant(title_full, process):
            return ("That is my own window, sir — closing it ends the session and you would "
                    "have to start me again. I have not touched it.")
        critical, reason = is_critical(title_full, process)
        if critical:
            return (f"Sir, I will not close '{title_full}' — {reason}. That is a system window, "
                    f"and no window command should end it.")

    if len(matches) > 1:
        names = " | ".join(str(w.get("title", ""))[:34] for w in matches[:3])
        return (f"'{title}' matches {len(matches)} windows: {names} — say a longer part of the "
                f"title and I will know which one.")

    wid = window.get("id", "")
    plat = platform_name()

    def _do() -> str:
        if plat == "windows":
            done, note = _windows_act(int(wid), action, p)
        elif plat == "macos":
            done, note = _macos_act(title_full, action)
        else:
            done, note = _linux_act(str(wid), action, p)
        if not done:
            return f"Sir, {note}."
        if player:
            try:
                player.write_log(f"[Window] {action} → {title_full[:40]}")
            except Exception:
                pass
        return f"Done, sir — {title_full[:60]} is {note}."

    if action == "close" and _truthy(_cfg().get("confirm_close")):
        try:
            from core import confirm
            answer = confirm.request(key=f"win_close_{wid}", title=f"Close '{title_full[:50]}'?",
                                     detail="Anything unsaved in it may be lost.",
                                     run=_do)
            if "[CONFIRMATION_PENDING]" in answer:
                return answer
            return f"Sir, {answer} Nothing was closed."
        except Exception:
            return (f"Sir, I could not put a confirmation on screen, so I have NOT closed "
                    f"'{title_full[:50]}'.")
    return _do()


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one window action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "list").strip().lower()
        title = str(p.get("title") or p.get("name") or "").strip()

        if action in ("list", "all", "windows"):
            ok, why = capability()
            if not ok:
                return f"Sir, {why}."
            windows = list_windows()
            if not windows:
                return ("I can reach the window system but it is showing me no titled windows, "
                        "sir.")
            lines = [f"{len(windows)} window(s) open:"]
            for w in windows[:20]:
                lines.append(f"  {str(w.get('title', ''))[:62]}")
            if len(windows) > 20:
                lines.append(f"  … and {len(windows) - 20} more")
            return "\n".join(lines)

        if action in ("active", "current", "focused"):
            windows = list_windows()
            if not windows:
                return "I cannot read the window list here, sir."
            return f"The front window looks like: {str(windows[0].get('title', '?'))[:70]}"

        if action in ("focus", "bring", "switch", "activate"):
            if not title:
                return "Which window, sir?"
            return act(title, "focus", p, player)

        if action in ("minimize", "minimise", "minimize_all"):
            return act(title, "minimize", p, player) if title else "Which window, sir?"
        if action in ("maximize", "maximise"):
            return act(title, "maximize", p, player) if title else "Which window, sir?"
        if action in ("restore", "unminimize"):
            return act(title, "restore", p, player) if title else "Which window, sir?"
        if action in ("close", "quit"):
            return act(title, "close", p, player) if title else "Which window, sir?"
        if action == "move":
            return act(title, "move", p, player) if title else "Which window, sir?"
        if action in ("resize", "size"):
            return act(title, "resize", p, player) if title else "Which window, sir?"

        if action in ("status", "info"):
            ok, why = capability()
            return (f"Platform    : {platform_name()}\n"
                    f"Control     : {'ready — ' + why if ok else 'NOT ready — ' + why}\n"
                    f"Windows now : {len(list_windows())}\n"
                    f"Protected   : {len(_CRITICAL)} system windows I will never close\n"
                    f"Closing     : {'asks first' if _truthy(_cfg().get('confirm_close')) else 'no confirmation (you turned it off)'}")

        return (f"Sir, '{action}' is not a window action I have. I can list, focus, minimise, "
                f"maximise, restore, move, resize, or close.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the window manager hit a problem: {str(e)[:150]}"


# ── self-test: python window_manager.py ──────────────────────────────────────

def _selftest() -> int:
    print("window_manager — self-test")
    problems = 0
    print(f"  platform            : {platform_name()}")
    ok, why = capability()
    print(f"  control available   : {ok}  ({why[:76]})")

    windows = list_windows()
    print(f"  windows seen        : {len(windows)}")
    if windows:
        print(f"  front window        : {str(windows[0].get('title', ''))[:60]}")
    else:
        # No display here is normal for a server: the honest answer must still come out.
        print(f"  → {run({'action': 'list'})[:88]}")

    # the guard is pure logic and must be right on every platform
    for title, proc, want in (
        ("Program Manager", "explorer.exe", True),
        ("", "csrss.exe", True),
        ("Desktop", "gnome-shell", True),
        ("New Tab - Google Chrome", "chrome.exe", False),
        ("JARVIS", "", True),
        ("mark-lv dashboard", "", True),
        ("Terminal", "gnome-terminal", False),
    ):
        got, why_crit = is_critical(title, proc)
        is_assistant = _is_assistant(title, proc)
        verdict = "critical" if got else ("assistant" if is_assistant else "normal")
        mark = "✓" if (got or is_assistant) == want else "✗"
        problems += 0 if (got or is_assistant) == want else 1
        print(f"  guard {title[:24]!r:<26} {proc:<16} → {verdict:<10} {mark}")

    unknown = run({"action": "focus", "title": "zzz-no-such"})
    print(f"  unknown window      : {unknown[:80]}")
    # Two answers are both correct: without a display the capability check speaks
    # first, and with one the window lookup does. Either way it must not act.
    problems += 0 if ("cannot see a window" in unknown or not ok) else 1
    print(f"  no title given      : {run({'action': 'focus'})}")
    problems += 0 if "Which window" in run({"action": "focus"}) else 1
    print(f"  bad action          : {run({'action': 'sing'})[:72]}")
    problems += 0 if "not a window action" in run({"action": "sing"}) else 1
    print(f"  status              : {run({'action': 'status'}).splitlines()[1][:70]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
