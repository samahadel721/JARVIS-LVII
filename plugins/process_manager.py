"""
process_manager.py — seeing what is running, and stopping it when it must stop.

WHAT IT CAN AND CANNOT DO
    It can list processes, search them by name or PID, show what each one is
    costing you in processor time and memory, and tell you what state it is in.

    It can end a process — yours, selected by you, after you confirm it. It
    cannot gain privileges it does not have: if Windows or Linux refuses because
    the process belongs to another account or to the system, the plugin reports
    exactly that and stops. There is no elevation here, no asking for an
    administrator prompt, no "try harder". That is deliberate and permanent.

THE REFUSALS
    Some processes are the operating system's own machinery. Ending one of them
    does not close a program — it takes the machine down, or leaves it in a
    state where you have to power it off by hand. Those are refused by name and
    the refusal explains the consequence.

    This plugin also refuses to end itself, its parent, or anything in its own
    process tree. A tool that can kill the program running it is a tool that can
    silence its own supervisor.

MEMORY: WHAT THE NUMBERS MEAN
    Percentage of processor time is measured over a short interval, so a
    process that is idle *right now* shows near zero even if it has been busy
    all day. The column is "what it is doing", not "what it has done" — and the
    plugin says so when you sort by it, because the difference matters when you
    are deciding what to close.

SETTINGS (Plugin Manager → Processes)
    confirm_kill   on — always ask before ending a process (leave this on)
    max_list       40 — how many rows the list shows
"""

from __future__ import annotations

import os
import time
from typing import Any

PLUGIN = {
    "name": "process_manager",
    "description": (
        "Shows what is running on this computer and what it is using: 'what is using "
        "the processor', 'is Chrome running', 'show me process 4821', 'end that "
        "process'. Ending a process always asks first and never touches system "
        "processes."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | search | info | stop | status"},
            "name": {"type": "STRING", "description": "Process name to look for"},
            "pid": {"type": "NUMBER", "description": "Process id"},
            "sort": {"type": "STRING", "description": "cpu | memory | name"},
            "limit": {"type": "NUMBER", "description": "How many rows, default 15"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "process_manager",
    "title": "Processes / العمليات",
    "fields": [
        {"key": "confirm_kill", "type": "choice", "label": "Ask before ending a process",
         "default": "on", "choices": ["on", "off"]},
        {"key": "max_list", "type": "text", "label": "Rows in the list", "default": "40"},
    ],
}

# Ending one of these is not closing an app. The reason is kept so the refusal
# can tell the user what it would actually have cost them.
_PROTECTED = {
    "system": "it is the Windows kernel itself",
    "system idle process": "it is how Windows accounts for idle time",
    "registry": "it holds the Windows registry in memory",
    "memory compression": "it is part of Windows memory management",
    "smss.exe": "it is the Windows session manager — the machine stops",
    "csrss.exe": "it is a core Windows process — the machine crashes",
    "wininit.exe": "it starts Windows services — the machine stops",
    "winlogon.exe": "it handles login — ending it ends your session",
    "services.exe": "it manages every Windows service",
    "lsass.exe": "it handles your login security",
    "dwm.exe": "it draws the desktop — the screen goes with it",
    "explorer.exe": "it is your taskbar and desktop, and would not come back by itself",
    "svchost.exe": "it hosts Windows services — many would stop at once",
    "audiodg.exe": "it is the Windows audio engine",
    "init": "it is the first process on Linux — the machine stops",
    "systemd": "it starts and supervises everything on Linux",
    "kthreadd": "it is the Linux kernel thread manager",
    "kernel_task": "it is the macOS kernel",
    "launchd": "it starts and supervises everything on macOS",
    "windowserver": "it draws every window on macOS",
    "loginwindow": "it is the macOS login process",
    "finders": "",
}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("process_manager") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("on", "yes", "true", "1")


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def psutil_module():
    """psutil is in the project's requirements; if it is missing, say so plainly."""
    try:
        import psutil
        return psutil
    except Exception:
        return None


def own_tree() -> set[int]:
    """This process and everything above and below it — never to be ended."""
    psutil = psutil_module()
    if psutil is None:
        return {os.getpid()}
    mine: set[int] = set()
    try:
        me = psutil.Process()
        mine.add(me.pid)
        parent = me.parent()
        while parent is not None:
            mine.add(parent.pid)
            try:
                parent = parent.parent()
            except Exception:
                break
        for child in me.children(recursive=True):
            mine.add(child.pid)
    except Exception:
        mine.add(os.getpid())
    return mine


def protected(pid: int, name: str = "") -> tuple[bool, str]:
    """(protected, why). Checked before any attempt to end anything."""
    key = (name or "").strip().lower()
    if key in _PROTECTED:
        return True, _PROTECTED[key] or "it is a system process"
    if pid <= 4:
        return True, "it is a kernel process"
    if pid in own_tree():
        return True, "it is part of the assistant itself — ending it would end me"
    return False, ""


# ── reading the process table ─────────────────────────────────────────────────

def snapshot(limit: int = 40, sort: str = "cpu", name_filter: str = "") -> list[dict[str, Any]]:
    """
    The process table, one pass, ranked.

    A process that disappears between listing and reading is skipped silently:
    the table changes under you all the time and that is normal, not an error.
    """
    psutil = psutil_module()
    if psutil is None:
        return []
    needle = (name_filter or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "username", "status", "memory_info"]):
        try:
            info = proc.info
            pname = str(info.get("name") or "")
            if needle and needle not in pname.lower() and needle != str(info.get("pid")):
                continue
            mem_mb = round((info.get("memory_info").rss if info.get("memory_info") else 0)
                           / (1024 * 1024), 1)
            rows.append({
                "pid": info.get("pid"), "name": pname,
                "status": str(info.get("status") or ""),
                "user": str(info.get("username") or "").split("\\")[-1].split("/")[-1],
                "memory_mb": mem_mb, "cpu": 0.0,
            })
        except Exception:
            continue
    if sort == "cpu":
        # One short measurement, only for the processes that will be shown: reading
        # cpu_percent for every process on the machine is expensive and pointless.
        rows.sort(key=lambda r: -r["memory_mb"])
        for row in rows[:min(len(rows), limit)]:
            try:
                row["cpu"] = round(psutil.Process(row["pid"]).cpu_percent(interval=0.1), 1)
            except Exception:
                row["cpu"] = 0.0
    if sort == "memory":
        rows.sort(key=lambda r: -r["memory_mb"])
    elif sort == "name":
        rows.sort(key=lambda r: r["name"].lower())
    elif sort == "cpu":
        rows.sort(key=lambda r: -r["cpu"])
    return rows[:max(1, limit)]


def find(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search by name (partial) or by exact PID."""
    query = str(query or "").strip()
    if not query:
        return []
    psutil = psutil_module()
    if psutil is None:
        return []
    if query.isdigit():
        return [r for r in snapshot(limit=500, sort="name") if str(r["pid"]) == query][:limit]
    return snapshot(limit=limit, sort="memory", name_filter=query.lower())


def usage(pid: int) -> dict[str, Any]:
    """Everything worth knowing about one process, or a reason it cannot be read."""
    psutil = psutil_module()
    if psutil is None:
        return {"ok": False, "reason": "psutil is not installed"}
    try:
        proc = psutil.Process(int(pid))
        with proc.oneshot():
            mem = proc.memory_info()
            info = {
                "ok": True, "pid": proc.pid, "name": proc.name(),
                "status": proc.status(), "user": proc.username(),
                "cpu": round(proc.cpu_percent(interval=0.15), 1),
                "memory_mb": round(mem.rss / (1024 * 1024), 1),
                "threads": proc.num_threads(),
                "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(proc.create_time())),
                "exe": _short_path(proc.exe() if hasattr(proc, "exe") else ""),
                "cmdline": " ".join(proc.cmdline()[:6])[:160],
            }
        return info
    except Exception as e:
        return {"ok": False, "reason": f"that process could not be read ({type(e).__name__})"}


def _short_path(path: str) -> str:
    """Never print a full personal path when a short one says the same thing."""
    try:
        home = os.path.expanduser("~")
        return str(path).replace(home, "~") if path else ""
    except Exception:
        return ""


def summary() -> dict[str, Any]:
    """
    A small digest for the diagnostics plugin — counts and the biggest consumers.
    Kept here so there is one place that knows how to read the process table.
    """
    psutil = psutil_module()
    if psutil is None:
        return {"available": False}
    try:
        top = snapshot(limit=5, sort="memory")
        return {"available": True, "count": len(psutil.pids()),
                "top": [(r["name"], r["memory_mb"]) for r in top],
                "own_pid": os.getpid()}
    except Exception as e:
        return {"available": False, "reason": str(e)[:60]}


# ── ending one ────────────────────────────────────────────────────────────────

def stop(pid: int, name: str = "", force: bool = False, player=None) -> str:
    """
    End one process, after the checks and (normally) the confirmation.

    terminate() first, which is the polite request every system offers; kill()
    only when the caller explicitly asked for force. Returns the sentence to say.
    """
    psutil = psutil_module()
    if psutil is None:
        return "Sir, psutil is not installed, so I cannot manage processes (it is in your requirements)."
    try:
        pid = int(pid)
    except Exception:
        return "Sir, that is not a process id."

    is_protected, why = protected(pid, name)
    if is_protected:
        return (f"Sir, I will not end process {pid}"
                + (f" ({name})" if name else "") + f" — {why}. That is a system process, and "
                f"no instruction should end it.")

    try:
        proc = psutil.Process(pid)
        real_name = proc.name()
        status = proc.status()
    except Exception:
        return f"Sir, there is no process {pid} running now (it may have already exited)."

    if status in ("zombie", "dead"):
        return f"Sir, process {pid} ({real_name}) has already finished."

    def _do() -> str:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                if force:
                    proc.kill()
                    proc.wait(timeout=5)
                else:
                    # Still alive after being asked politely: say so rather than
                    # escalating on our own. Escalating is a decision, not a default.
                    return (f"'{real_name}' (pid {pid}) was asked to stop but is still there. "
                            f"Some programs refuse. I have not forced it — say 'force' if you "
                            f"want that, and it may lose unsaved work.")
        except PermissionError:
            return (f"'{real_name}' (pid {pid}) belongs to another account or to the system, and "
                    f"this process is not allowed to end it. I cannot ask for higher privileges "
                    f"— that is by design.")
        except Exception as e:
            return f"'{real_name}' (pid {pid}) could not be ended: {str(e)[:80]}"
        if player:
            try:
                player.write_log(f"[Process] ended {real_name} ({pid})")
            except Exception:
                pass
        return f"Ended, sir — {real_name} (pid {pid}) is no longer running."

    if _truthy(_cfg().get("confirm_kill")) and not force:
        try:
            from core import confirm
            detail = f"{real_name} (pid {pid}) is using {round(proc.memory_info().rss / 1048576, 1)} MB"
            answer = confirm.request(key=f"kill_{pid}", title=f"End {real_name} (pid {pid})?",
                                     detail=detail + ". Unsaved work in it will be lost.",
                                     run=_do)
            if "[CONFIRMATION_PENDING]" in answer:
                return answer
            return f"Sir, {answer} It is still running."
        except Exception:
            return (f"Sir, I could not put a confirmation on screen, so I have NOT ended "
                    f"'{real_name}'. It is still running.")
    return _do()


# ── tool entry point ──────────────────────────────────────────────────────────

def _render(rows: list[dict[str, Any]], headline: str) -> str:
    if not rows:
        return "No processes matched, sir."
    lines = [headline]
    for r in rows:
        lines.append(f"  {str(r['pid']):>7}  {r['name'][:28]:<28} cpu {r['cpu']:>5}%  "
                     f"mem {r['memory_mb']:>7} MB  {r['status']}")
    return "\n".join(lines)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one process action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "list").strip().lower()
        psutil = psutil_module()
        if psutil is None:
            return ("Sir, psutil is not installed, so I cannot read the process table — it is "
                    "in the project's requirements, so a pip install psutil fixes it.")

        if action in ("list", "all", "top"):
            sort = str(p.get("sort") or "cpu").strip().lower()
            limit = int(float(p.get("limit") or 15))
            rows = snapshot(limit=limit, sort=sort if sort in ("cpu", "memory", "name") else "cpu")
            headline = (f"Highest {sort} first, sir — processor column is what each one is doing "
                        f"right now, not a total:")
            return _render(rows, headline)

        if action in ("search", "find", "is_running"):
            query = str(p.get("name") or p.get("query") or p.get("pid") or "").strip()
            if not query:
                return "Which process, sir?"
            rows = find(query, limit=int(float(p.get("limit") or 10)))
            if not rows:
                return f"Nothing called '{query}' is running right now, sir."
            return _render(rows, f"{len(rows)} match(es) for '{query}':")

        if action in ("info", "show", "details"):
            raw = str(p.get("pid") or p.get("name") or "").strip()
            if not raw:
                return "Which process, sir?"
            if raw.isdigit():
                pid = int(raw)
            else:
                hits = find(raw, limit=1)
                if not hits:
                    return f"Nothing called '{raw}' is running, sir."
                pid = int(hits[0]["pid"])
            info = usage(pid)
            if not info.get("ok"):
                return f"Sir, {info.get('reason')}."
            prot, why = protected(pid, info["name"])
            return (f"{info['name']}  (pid {pid})\n"
                    f"  status   : {info['status']}\n"
                    f"  user     : {info['user']}\n"
                    f"  processor: {info['cpu']}% right now\n"
                    f"  memory   : {info['memory_mb']} MB\n"
                    f"  threads  : {info['threads']}\n"
                    f"  started  : {info['started']}\n"
                    f"  path     : {info['exe'] or '(not readable)'}\n"
                    + (f"  PROTECTED: {why} — I will not end this one" if prot else
                       "  I can end this one, with your confirmation"))

        if action in ("stop", "kill", "end", "terminate"):
            raw = str(p.get("pid") or p.get("name") or "").strip()
            if not raw:
                return "Which process should I end, sir? Give me its name or number."
            force = str(p.get("force") or "").strip().lower() in ("yes", "true", "on", "1")
            if raw.isdigit():
                name = ""
                try:
                    name = psutil.Process(int(raw)).name()
                except Exception:
                    pass
                return stop(int(raw), name, force, player)
            hits = find(raw, limit=3)
            if not hits:
                return f"Nothing called '{raw}' is running, sir — nothing to end."
            exact = [h for h in hits if h["name"].lower() == raw.lower()]
            if len(exact) == 1:
                return stop(int(exact[0]["pid"]), exact[0]["name"], force, player)
            if len(hits) > 1:
                listing = ", ".join(f"{h['name']} ({h['pid']})" for h in hits[:4])
                return (f"'{raw}' matches {len(hits)} processes: {listing} — give me the number "
                        f"and I will end exactly that one.")
            return stop(int(hits[0]["pid"]), hits[0]["name"], force, player)

        if action in ("status", "info_all", "summary"):
            s = summary()
            if not s.get("available"):
                return f"Sir, I cannot read the process table: {s.get('reason', 'unknown')}."
            top = ", ".join(f"{n} ({m} MB)" for n, m in s["top"][:3])
            return (f"Processes running : {s['count']}\n"
                    f"Biggest by memory : {top}\n"
                    f"Protected names   : {len(_PROTECTED)} system processes I will never end\n"
                    f"Privileges        : none — I never ask for elevation; if a process is not "
                    f"mine to end, I say so and stop")

        return (f"Sir, '{action}' is not a process action I have. I can list, search, show one "
                f"in detail, or end one.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the process manager hit a problem: {str(e)[:150]}"


# ── self-test: python process_manager.py ────────────────────────────────────

def _selftest() -> int:
    print("process_manager — self-test")
    problems = 0
    psutil = psutil_module()
    print(f"  psutil              : {'available' if psutil else 'NOT available'}")
    if psutil is None:
        print("  RESULT: cannot test without psutil")
        return 1

    rows = snapshot(limit=5, sort="memory")
    print(f"  table read          : {len(rows)} row(s)")
    if rows:
        print(f"  biggest by memory   : {rows[0]['name']} ({rows[0]['memory_mb']} MB)")
    problems += 0 if rows else 1

    mine = own_tree()
    print(f"  own tree (protected): {sorted(mine)[:5]}")
    for pid, name, want in ((1, "init", True), (4, "System", True),
                            (999999, "ghost", False), (os.getpid(), "", True),
                            (4242, "chrome.exe", False), (9001, "csrss.exe", True)):
        got, why = protected(pid, name)
        mark = "✓" if got == want else "✗"
        problems += 0 if got == want else 1
        print(f"  guard {name or '(self)':<12} pid {pid:<9} → {'refused' if got else 'allowed':<8} {mark}")

    # the assistant must not be able to end itself
    answer = stop(os.getpid(), "python", force=False)
    print(f"  ends itself?        : {answer[:78]}")
    problems += 0 if "will not end" in answer else 1

    answer = stop(999999, "ghost")
    print(f"  missing process     : {answer[:70]}")
    problems += 0 if "no process" in answer else 1

    pid_of_self = os.getpid()
    info = usage(pid_of_self)
    print(f"  own details         : ok={info.get('ok')} mem={info.get('memory_mb')} MB "
          f"threads={info.get('threads')}")
    problems += 0 if info.get("ok") else 1

    print(f"  list action         : {run({'action': 'list', 'limit': 3}).splitlines()[0][:74]}")
    print(f"  search action       : {run({'action': 'search', 'name': 'python'}).splitlines()[0][:70]}")
    print(f"  unknown search      : {run({'action': 'search', 'name': 'zzzznotrunning'})[:70]}")
    problems += 0 if "Nothing called" in run({"action": "search", "name": "zzzznotrunning"}) else 1
    print(f"  stop w/o pid        : {run({'action': 'stop'})}")
    problems += 0 if "Which process" in run({"action": "stop"}) else 1
    print(f"  bad action          : {run({'action': 'sing'})[:70]}")
    problems += 0 if "not a process action" in run({"action": "sing"}) else 1
    print(f"  summary for diag    : {summary().get('count')} processes")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
