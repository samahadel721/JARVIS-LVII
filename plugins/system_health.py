"""
plugins/system_health.py — is anything actually wrong with this computer?

Drop this single file into `plugins/`. Needs `psutil` (`pip install psutil`),
which the app's own requirements already carry, so on a working install there is
nothing to add. The import is deferred to call time, so discovery stays instant
even if the package is missing.

WHY IT EXISTS NEXT TO THE BUILT-IN system_status
    The app already ships a `system_status` tool, and it is good at what it
    covers: CPU percent, RAM, CPU temperature, GPU load, uptime, process count.
    It is also where that list ends — it reports nothing about disks, battery or
    network, and it never says a process name, so "why is my fan loud" comes back
    as a temperature with no suspect attached.

    This plugin is the other half, and its description says so out loud, so the
    model routes raw numbers to the built-in tool and diagnosis here. Nothing is
    changed, no elevation is needed, and the verdict comes first: "disk C: is 94 %
    full" before any healthy-sounding telemetry.

ACCURACY OVER INSTANT
    CPU percentages need an interval to mean anything: an instant read is either
    0 % or 100 % depending on which microsecond it lands on. This waits a fraction
    of a second for a real figure, because a truthful answer half a second late
    beats a confident wrong one.

SETTINGS (Plugin Manager → System Status)
    Warning thresholds and how many processes to list.
"""

from __future__ import annotations

import datetime as _dt
import os
import platform
import time

PLUGIN = {
    "name": "system_health",
    "description": (
        "Diagnoses this computer: free disk space, battery state, network "
        "traffic, which application is using the most CPU or memory, and a "
        "warning-first verdict on whether anything is wrong. Use for: 'is my "
        "disk full', 'how much space is left', 'battery status', 'why is my fan "
        "loud', 'what is eating my CPU', 'do I need to close something', 'is my "
        "computer healthy'. For raw CPU / RAM / temperature / GPU numbers use "
        "the built-in 'system_status' tool instead — this one is the diagnosis "
        "and the things that tool does not report."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'summary' (default) for the whole picture with anything "
                    "wrong named first, or one of: 'cpu', 'memory', 'disk', "
                    "'battery', 'network', 'processes' (what is using the most), "
                    "'uptime'."
                ),
            },
            "count": {
                "type": "INTEGER",
                "description": "How many top processes to list for 'processes' (default 5).",
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "system_health",
    "title": "System Health / صحة الجهاز",
    "fields": [
        {"key": "cpu_warn", "type": "text", "label": "CPU warning % / حد تحذير المعالج",
         "default": "85"},
        {"key": "ram_warn", "type": "text", "label": "RAM warning % / حد تحذير الذاكرة",
         "default": "85"},
        {"key": "disk_warn", "type": "text", "label": "Disk warning % / حد تحذير القرص",
         "default": "90"},
        {"key": "battery_warn", "type": "text", "label": "Battery warning % / حد تحذير البطارية",
         "default": "20"},
    ],
}

_BYTES = ("B", "KB", "MB", "GB", "TB", "PB")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _psutil():
    """
    Import psutil at call time, with a message the user can act on.

    Importing at module level would slow every startup for everyone even after
    the one time it is missing; doing it here means the plugin is always
    *discovered* cleanly and only the call reports the missing package.
    """
    import psutil
    return psutil


def _human_bytes(value: float) -> str:
    step = 0.0
    size = float(value)
    while size >= 1024 and step < len(_BYTES) - 1:
        size /= 1024.0
        step += 1
    return f"{size:.1f} {_BYTES[int(step)]}" if step else f"{int(size)} B"


def _disk_root() -> str:
    """The volume that matters: C:\\ on Windows, / elsewhere."""
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def _config() -> dict:
    values = {"cpu_warn": 85.0, "ram_warn": 85.0, "disk_warn": 90.0, "battery_warn": 20.0}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("system_health") or {}).items():
            if value not in (None, ""):
                try:
                    values[key] = float(value)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return values


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

def _cpu(ps) -> tuple[float, int, float]:
    """(percent, core count, 1-minute load average or 0 when unavailable)."""
    percent = ps.cpu_percent(interval=0.4)
    cores = ps.cpu_count(logical=True) or 1
    try:
        load = os.getloadavg()[0]          # absent on Windows
    except (AttributeError, OSError):
        load = 0.0
    return percent, cores, load


def _memory(ps) -> tuple[float, float, float]:
    """(used percent, used bytes, total bytes)."""
    mem = ps.virtual_memory()
    return mem.percent, mem.total - mem.available, mem.total


def _disks(ps) -> list[tuple[str, float, float, float]]:
    """(mount, percent, used, total) for the volumes worth mentioning."""
    out: list[tuple[str, float, float, float]] = []
    root = _disk_root()
    try:
        usage = ps.disk_usage(root)
        out.append((root, usage.percent, usage.used, usage.total))
    except Exception:
        pass
    if os.name != "nt":
        for part in ps.disk_partitions(all=False):
            if part.mountpoint in ("/", "") or part.fstype in ("squashfs", "tmpfs", "devtmpfs"):
                continue
            try:
                usage = ps.disk_usage(part.mountpoint)
            except Exception:
                continue
            if usage.total > 4 * 1024 ** 3:
                out.append((part.mountpoint, usage.percent, usage.used, usage.total))
    return out[:4]


def _battery(ps) -> dict:
    try:
        battery = ps.sensors_battery()
    except Exception:
        battery = None
    if battery is None:
        return {"present": False}
    seconds = battery.secsleft
    return {
        "present": True,
        "percent": float(battery.percent),
        "plugged": bool(battery.power_plugged),
        "minutes": None if seconds in (None, -1, -2) or seconds > 10 * 3600 * 24
        else int(seconds // 60),
    }


def _network(ps) -> dict:
    counters = ps.net_io_counters()
    return {"sent": counters.bytes_sent, "received": counters.bytes_recv,
            "errors": counters.errin + counters.errout}


def _uptime(ps) -> tuple[str, float]:
    boot = float(ps.boot_time())
    seconds = max(0.0, time.time() - boot)
    return _dt.datetime.fromtimestamp(boot).strftime("%Y-%m-%d %H:%M"), seconds


def _human_duration(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _top_processes(ps, count: int) -> list[tuple[str, float, float]]:
    """
    (name, cpu %, memory %) for the hungriest processes.

    CPU has to be sampled twice — the first reading after `cpu_percent()` is
    always 0.0 — so every process is primed, then read again after a short wait.
    Ranking purely on that value would list whatever happened to wake up last,
    so the list is sorted by the larger of the two signals.
    """
    procs: list = []
    for proc in ps.process_iter(["name", "memory_percent"]):
        try:
            proc.cpu_percent(None)             # prime the counter
            procs.append(proc)
        except Exception:
            continue
    time.sleep(0.35)
    rows: list[tuple[str, float, float]] = []
    for proc in procs:
        try:
            info = proc.as_dict(attrs=["name", "memory_percent"])
            rows.append((info.get("name") or "?", float(proc.cpu_percent(None) or 0.0),
                         float(info.get("memory_percent") or 0.0)))
        except Exception:
            continue
    rows.sort(key=lambda row: max(row[1], row[2] * 2), reverse=True)
    return rows[:max(1, count)]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_summary(ps, config: dict) -> str:
    cpu, cores, load = _cpu(ps)
    ram, ram_used, ram_total = _memory(ps)
    disks = _disks(ps)
    battery = _battery(ps)
    uptime_at, uptime_seconds = _uptime(ps)

    problems: list[str] = []
    if cpu >= config["cpu_warn"]:
        problems.append(f"CPU is at {cpu:.0f}%")
    if ram >= config["ram_warn"]:
        problems.append(f"memory is at {ram:.0f}% ({_human_bytes(ram_used)} of {_human_bytes(ram_total)})")
    for mount, percent, _, _ in disks:
        if percent >= config["disk_warn"]:
            problems.append(f"disk {mount} is {percent:.0f}% full")
    if battery["present"] and not battery["plugged"] and battery["percent"] <= config["battery_warn"]:
        problems.append(f"battery is down to {battery['percent']:.0f}% and unplugged")

    healthy = (f"CPU {cpu:.0f}% across {cores} cores, memory {ram:.0f}% "
               f"({_human_bytes(ram_used)} of {_human_bytes(ram_total)})")
    if disks:
        mount, percent, used, total = disks[0]
        healthy += f", disk {percent:.0f}% full ({_human_bytes(total - used)} free)"
    if battery["present"]:
        healthy += (f", battery {battery['percent']:.0f}%"
                    + (" on power" if battery["plugged"] else " on battery"))
    healthy += f", up {_human_duration(uptime_seconds)}."

    if problems:
        return "Everything is under load, sir: " + "; ".join(problems) + ". " + healthy
    return "All healthy, sir. " + healthy


def _action_processes(ps, count: int) -> str:
    rows = _top_processes(ps, count)
    if not rows:
        return "I could not read the process list on this system."
    listed = ", ".join(f"{name} (CPU {cpu:.0f}%, RAM {mem:.1f}%)"
                       for name, cpu, mem in rows)
    return f"The heaviest processes right now: {listed}."


def _action_disk(ps, config: dict) -> str:
    disks = _disks(ps)
    if not disks:
        return "I could not read any disk on this system."
    parts = [f"{mount} {percent:.0f}% full, {_human_bytes(total - used)} free of {_human_bytes(total)}"
             for mount, percent, used, total in disks]
    return "Disk usage: " + "; ".join(parts) + "."


def _action_battery(ps, config: dict) -> str:
    battery = _battery(ps)
    if not battery["present"]:
        return "This machine reports no battery — it is a desktop, or the sensors are hidden."
    state = "plugged in" if battery["plugged"] else "on battery"
    tail = ""
    if not battery["plugged"] and battery["minutes"]:
        tail = f", about {_human_duration(battery['minutes'] * 60)} left"
    warn = " That is low — worth plugging in." \
        if (not battery["plugged"] and battery["percent"] <= config["battery_warn"]) else ""
    return f"Battery is at {battery['percent']:.0f}% and {state}{tail}.{warn}"


def _action_network(ps) -> str:
    net = _network(ps)
    return (f"Since boot: {_human_bytes(net['received'])} received and "
            f"{_human_bytes(net['sent'])} sent"
            + (f", with {net['errors']} interface errors" if net["errors"] else "") + ".")


def _action_uptime(ps) -> str:
    boot_at, seconds = _uptime(ps)
    return f"Up for {_human_duration(seconds)} — running since {boot_at}."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        action = str(parameters.get("action", "summary") or "summary").strip().lower()
        try:
            count = int(parameters.get("count") or 5)
        except (TypeError, ValueError):
            count = 5
        count = max(1, min(20, count))

        try:
            ps = _psutil()
        except ImportError:
            return ("I need the psutil package for that, sir. Run: "
                    "pip install psutil — then ask again.")

        if action in ("cpu", "processor"):
            cpu, cores, load = _cpu(ps)
            tail = f" Load average {load:.2f}." if load else ""
            result = f"CPU is at {cpu:.0f}% across {cores} logical cores.{tail}"
        elif action in ("memory", "ram"):
            ram, used, total = _memory(ps)
            result = (f"Memory is {ram:.0f}% used — {_human_bytes(used)} of "
                      f"{_human_bytes(total)}, {_human_bytes(total - used)} free.")
        elif action in ("disk", "storage"):
            result = _action_disk(ps, _config())
        elif action == "battery":
            result = _action_battery(ps, _config())
        elif action in ("network", "net"):
            result = _action_network(ps)
        elif action in ("processes", "top", "heavy"):
            result = _action_processes(ps, count)
        elif action == "uptime":
            result = _action_uptime(ps)
        else:
            result = _action_summary(ps, _config())

    except Exception as exc:
        return f"Sir, system_health failed: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result


if __name__ == "__main__":
    print(platform.system())
    for a in ("summary", "cpu", "memory", "disk", "battery", "processes", "uptime", "network"):
        print(f"[{a}] {run({'action': a})}")
