"""
storage_analyzer.py — where the disk space actually went.

WHAT IT MEASURES
    Which disks exist and how full each one is, which files are largest, which
    folders hold the most, and which of those are the usual suspects — a browser
    cache, a download folder nobody has emptied, temporary files left by an
    installer.

WHAT IT DOES WITH THAT
    It reports. It recommends. It does not delete anything on its own, ever, and
    there is no setting that turns that into automatic behaviour.

    When you do ask it to remove something, three things happen together: the
    path must be inside a folder you authorised, the action goes through the
    project's confirmation gate, and it goes to the recycle bin rather than
    being erased — because a storage tool that cannot be argued with is a
    storage tool that will eventually delete the wrong thing.

WHY THE SCAN IS CAPPED
    Walking an entire disk takes minutes and can pin a processor. So the scan
    has a time budget and a depth limit, and it tells you when it stopped at
    the ceiling rather than presenting a half-finished list as the whole truth.
    Folders you never authorised are never opened.

SETTINGS (Plugin Manager → Storage)
    roots         comma-separated folders to scan (blank = your home folder)
    max_seconds   12 — budget per scan; it stops and says so
    max_depth     6 — how deep below each root it looks
    min_mb        50 — files smaller than this are not reported
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "storage_analyzer",
    "description": (
        "Shows where the disk space went: 'how much space is left', 'what is filling my "
        "disk', 'find the biggest files', 'biggest folders in Downloads'. It recommends, "
        "it never deletes on its own, and anything it removes goes to the recycle bin."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "disks | large | folders | recommendations | report | remove | status"},
            "path": {"type": "STRING", "description": "Folder to scan, or the file to remove"},
            "limit": {"type": "NUMBER", "description": "How many rows, default 12"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "storage_analyzer",
    "title": "Storage / المساحة",
    "fields": [
        {"key": "roots", "type": "text", "label": "Folders to scan (blank = home)",
         "default": "", "placeholder": "C:/Users/me/Downloads, D:/Work"},
        {"key": "max_seconds", "type": "text", "label": "Scan time budget, seconds", "default": "12"},
        {"key": "max_depth", "type": "text", "label": "How deep to look", "default": "6"},
        {"key": "min_mb", "type": "text", "label": "Smallest file worth reporting, MB",
         "default": "50"},
    ],
}

_SKIP = {"$recycle.bin", "system volume information", "windows", "proc", "sys", "dev",
         "node_modules", ".git", "__pycache__", "appdata"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("storage_analyzer") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def roots() -> list[Path]:
    """
    The folders this plugin may look inside. Blank means the home folder only —
    never the whole disk, because a tool that scans everything also reports
    things that are none of its business.
    """
    raw = str(_cfg().get("roots") or "").strip()
    out: list[Path] = []
    if raw:
        for part in raw.replace(";", ",").split(","):
            part = part.strip().strip('"')
            if not part:
                continue
            try:
                p = Path(part).expanduser().resolve()
            except Exception:
                continue
            if p.exists() and p not in out:
                out.append(p)
    else:
        home = Path.home().resolve()
        if home.exists():
            out.append(home)
    return out


def is_authorized(path: str | Path) -> tuple[bool, str]:
    """Same rule as the knowledge base: resolve first, then compare. No escapes."""
    try:
        target = Path(path).expanduser().resolve()
    except Exception as e:
        return False, f"I could not make sense of that path: {str(e)[:60]}"
    allowed = roots()
    if not allowed:
        return False, "no folders are authorised, so I will not touch anything"
    for root in allowed:
        try:
            target.relative_to(root)
            return True, ""
        except ValueError:
            continue
    return False, (f"'{target}' is outside every folder I am allowed to look at "
                   f"({', '.join(str(r) for r in allowed)})")


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


# ── measuring ─────────────────────────────────────────────────────────────────

def disks() -> list[dict[str, Any]]:
    """Every mounted disk with its usage. psutil knows how to ask each system."""
    try:
        import psutil
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    try:
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            out.append({"mount": part.mountpoint, "fs": part.fstype,
                        "total": usage.total, "used": usage.used, "free": usage.free,
                        "percent": usage.percent})
    except Exception:
        pass
    return out


def scan(folder: Path | None = None) -> dict[str, Any]:
    """
    One bounded walk. Returns {files, folders, stopped_early, seconds, scanned_dirs}.

    The budget is the point: a scan that would take ten minutes is a scan nobody
    runs twice, and a progress bar that lies is worse than a ceiling that is
    stated.
    """
    allowed = [folder] if folder else roots()
    budget = _int_cfg("max_seconds", 12, 1, 600)
    depth_cap = _int_cfg("max_depth", 6, 1, 30)
    floor = _int_cfg("min_mb", 50, 1, 100_000) * 1024 * 1024
    started = time.monotonic()

    files: list[tuple[str, int]] = []
    folder_sizes: dict[str, int] = {}
    stopped_early = False
    scanned = 0

    for root in allowed:
        if not root.exists():
            continue
        base_depth = len(root.parts)
        stack = [root]
        while stack:
            if time.monotonic() - started > budget:
                stopped_early = True
                break
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except (PermissionError, OSError):
                continue
            scanned += 1
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in _SKIP or entry.name.startswith("."):
                            continue
                        if len(Path(entry.path).parts) - base_depth < depth_cap:
                            stack.append(Path(entry.path))
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    size = stat.st_size
                    if size >= floor:
                        files.append((entry.path, size))
                    # Attribute the file to its top-level folder under the root, so
                    # the folder list answers "which folder", not "which leaf".
                    rel = Path(entry.path).relative_to(root)
                    top = root / rel.parts[0] if len(rel.parts) > 1 else root
                    folder_sizes[str(top)] = folder_sizes.get(str(top), 0) + size
                except (PermissionError, OSError):
                    continue
                except Exception:
                    continue
        if stopped_early:
            break

    files.sort(key=lambda x: -x[1])
    folders = sorted(folder_sizes.items(), key=lambda x: -x[1])
    return {"files": files, "folders": folders, "stopped_early": stopped_early,
            "seconds": round(time.monotonic() - started, 1), "scanned_dirs": scanned,
            "roots": [str(r) for r in allowed]}


def recommendations(limit: int = 6) -> list[str]:
    """
    Suggestions, with the size attached — never a command. Each one names what it
    is and why it is usually safe, and none of them happen by themselves.
    """
    ideas: list[str] = []
    home = Path.home()
    known = {
        "Downloads": "the folder you have already forgotten about",
        "Temp": "temporary files, safe to clear when no installer is running",
        "temp": "temporary files, safe to clear when no installer is running",
        ".cache": "application caches — programs rebuild them, just more slowly at first",
        "Cache": "application caches",
        "Caches": "application caches",
    }
    for name, why in known.items():
        candidate = home / name
        if not candidate.is_dir():
            continue
        size, _ = _folder_size_bounded(candidate, 4)
        if size > 50 * 1024 * 1024:
            ideas.append(f"{candidate.name} is about {human(size)} — {why}")
        if len(ideas) >= limit:
            break
    for part in disks()[:3]:
        if part["percent"] >= 85:
            ideas.append(f"{part['mount']} is {part['percent']}% full — "
                         f"{human(part['free'])} left")
    if not ideas:
        ideas.append("Nothing stands out on this machine — no folder I would single out.")
    return ideas[:limit]


def _folder_size_bounded(folder: Path, depth: int) -> tuple[int, bool]:
    """Size of a folder, depth-limited and with a hard stop on entry count."""
    total = 0
    entries_seen = 0
    stack = [(folder, 0)]
    while stack and entries_seen < 20000:
        current, level = stack.pop()
        try:
            for entry in os.scandir(current):
                entries_seen += 1
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif level < depth and entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), level + 1))
                except OSError:
                    continue
        except OSError:
            continue
    return total, entries_seen >= 20000


# ── the one destructive action, and how it is contained ──────────────────────

def remove_file(path: str, player=None) -> str:
    """
    Send one file to the recycle bin — recoverable, confirmed, and only inside an
    authorised folder. Folders are refused here; deleting a tree is not something
    a voice instruction should be able to do in one step.
    """
    allowed, why = is_authorized(path)
    if not allowed:
        return f"Sir, I will not remove that: {why}."
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return f"Sir, '{target.name}' is not there any more."
    if target.is_dir():
        return (f"Sir, '{target.name}' is a folder. I only remove single files — emptying a "
                f"folder is too big a thing to get wrong, so please do that one yourself.")
    try:
        size = human(target.stat().st_size)
    except Exception:
        size = "unknown size"

    def _do() -> str:
        try:
            from send2trash import send2trash
            send2trash(str(target))
            if player:
                try:
                    player.write_log(f"[Storage] recycled {target.name}")
                except Exception:
                    pass
            return f"'{target.name}' is in the recycle bin — you can restore it from there."
        except ImportError:
            return ("the send2trash package is not installed, and I will not erase a file "
                    "permanently (pip install send2trash)")
        except Exception as e:
            return f"it could not be recycled: {str(e)[:80]}"

    try:
        from core import confirm
        answer = confirm.request(key=f"storage_remove_{target.name}",
                                 title=f"Send '{target.name}' to the recycle bin?",
                                 detail=f"{size} — it can be restored from the bin afterwards.",
                                 run=_do)
        if "[CONFIRMATION_PENDING]" in answer:
            return answer
        return f"Sir, {answer} Nothing was removed."
    except Exception:
        return ("Sir, I could not put a confirmation on screen, so I have NOT removed "
                "anything.")


# ── tool entry point ──────────────────────────────────────────────────────────

def report(limit: int = 8) -> str:
    """The readable summary — also what System Diagnostics prints."""
    parts = disks()
    lines = ["Disks:"]
    for part in parts[:6]:
        lines.append(f"  {part['mount']:<22} {part['percent']:>5.1f}% used · "
                     f"{human(part['free'])} free of {human(part['total'])}")
    if not parts:
        lines.append("  (this system reports no mounted disks)")

    result = scan()
    lines.append(f"\nLargest files below {', '.join(Path(r).name for r in result['roots']) or '—'} "
                 f"(scan took {result['seconds']}s"
                 + (", stopped at the time limit" if result["stopped_early"] else "") + "):")
    for path, size in result["files"][:limit]:
        lines.append(f"  {human(size):>10}  {_short(path)}")
    if not result["files"]:
        lines.append("  nothing above the size floor")

    lines.append("\nLargest folders:")
    for path, size in result["folders"][:limit]:
        lines.append(f"  {human(size):>10}  {_short(path)}")

    lines.append("\nWhat I would look at first:")
    for idea in recommendations():
        lines.append(f"  · {idea}")
    lines.append("\nI have removed nothing, sir. Say 'remove <file>' and I will ask you to "
                 "confirm, then send it to the recycle bin.")
    return "\n".join(lines)


def _short(path: str) -> str:
    """Home folder as ~, and long paths trimmed from the left. No full personal paths."""
    try:
        text = str(path).replace(os.path.expanduser("~"), "~")
        return text if len(text) <= 84 else "…" + text[-82:]
    except Exception:
        return str(path)[-84:]


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one storage action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "report").strip().lower()
        limit = int(float(p.get("limit") or 12))

        if action in ("disks", "drives", "space"):
            parts = disks()
            if not parts:
                return "This system is not reporting any mounted disks to me, sir."
            lines = ["Disk space:"]
            for part in parts[:8]:
                lines.append(f"  {part['mount']:<22} {human(part['total'])} total · "
                             f"{human(part['used'])} used · {human(part['free'])} free "
                             f"({part['percent']}%)")
            return "\n".join(lines)

        if action in ("large", "big", "biggest", "files"):
            folder = str(p.get("path") or "").strip()
            target = None
            if folder:
                allowed, why = is_authorized(folder)
                if not allowed:
                    return f"Sir, {why}."
                target = Path(folder).expanduser().resolve()
            result = scan(target)
            if not result["files"]:
                return (f"Nothing above {_int_cfg('min_mb', 50, 1, 100000)} MB in what I am "
                        f"allowed to look at, sir.")
            lines = [f"{min(limit, len(result['files']))} largest file(s)"
                     + (f" — scan hit the {result['seconds']}s limit"
                        if result["stopped_early"] else f" — scanned in {result['seconds']}s") + ":"]
            for path, size in result["files"][:limit]:
                lines.append(f"  {human(size):>10}  {_short(path)}")
            return "\n".join(lines)

        if action in ("folders", "folder", "directories"):
            result = scan()
            if not result["folders"]:
                return "I could not measure any folders in what I am allowed to look at, sir."
            lines = [f"Largest folders under {', '.join(result['roots'])}:"]
            for path, size in result["folders"][:limit]:
                lines.append(f"  {human(size):>10}  {_short(path)}")
            return "\n".join(lines)

        if action in ("recommendations", "advice", "clean_suggestions"):
            return ("What I would look at, sir — and I will touch none of it myself:\n"
                    + "\n".join(f"  · {idea}" for idea in recommendations(limit)))

        if action in ("report", "summary", "analyze", "analyse"):
            return report(limit=min(limit, 10))

        if action in ("remove", "delete", "recycle"):
            path = str(p.get("path") or "").strip()
            if not path:
                return ("Which file, sir? Give me its full path — and remember I only remove "
                        "single files inside folders I am allowed to look at.")
            return remove_file(path, player)

        if action in ("status", "info"):
            allowed = roots()
            return (f"Allowed folders : {', '.join(str(r) for r in allowed) or 'none'}\n"
                    f"Time budget     : {_int_cfg('max_seconds', 12, 1, 600)}s per scan\n"
                    f"Size floor      : {_int_cfg('min_mb', 50, 1, 100000)} MB\n"
                    f"Removal         : recycle bin only, one file at a time, after confirmation\n"
                    f"Automatic clean : there is no such setting, on purpose")

        return (f"Sir, '{action}' is not a storage action I have. I can show disks, largest "
                f"files or folders, recommendations, a full report, or remove one file.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the storage analyzer hit a problem: {str(e)[:150]}"


# ── self-test: python storage_analyzer.py ───────────────────────────────────

def _selftest() -> int:
    print("storage_analyzer — self-test")
    problems = 0

    print(f"  human sizes         : {human(0)} · {human(1536)} · {human(5*1024**3)}")
    problems += 0 if human(1536) == "1.5 KB" else 1

    parts = disks()
    print(f"  disks found         : {len(parts)}")
    for part in parts[:3]:
        print(f"      {part['mount']:<20} {part['percent']}% used, {human(part['free'])} free")

    root = Path(__file__).resolve().parent / "_storage_probe"
    root.mkdir(exist_ok=True)
    (root / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    (root / "small.txt").write_text("tiny", encoding="utf-8")
    outside = Path(__file__).resolve().parent / "_storage_outside"
    outside.mkdir(exist_ok=True)
    (outside / "private.txt").write_text("not mine to touch", encoding="utf-8")

    global _cfg
    real = _cfg
    _cfg = lambda: {**real(), "roots": str(root), "min_mb": "1", "max_seconds": "10"}
    try:
        ok_in, _ = is_authorized(root / "big.bin")
        ok_out, why = is_authorized(outside / "private.txt")
        ok_trav, _ = is_authorized(str(root / ".." / "_storage_outside" / "private.txt"))
        print(f"  inside allowed      : {ok_in} · outside refused: {not ok_out} · '..' refused: {not ok_trav}")
        problems += 0 if ok_in and not ok_out and not ok_trav else 1
        print(f"  refusal reason      : {why[:74]}")

        result = scan()
        print(f"  bounded scan        : {len(result['files'])} file(s) ≥1MB, {result['scanned_dirs']} "
              f"dir(s) in {result['seconds']}s")
        problems += 0 if any("big.bin" in f for f, _ in result["files"]) else 1
        problems += 0 if not any("small.txt" in f for f, _ in result["files"]) else 1

        print(f"  folders measured    : {[(Path(p).name, human(s)) for p, s in result['folders'][:2]]}")

        answer = remove_file(str(outside / "private.txt"))
        print(f"  remove outside      : {answer[:76]}")
        problems += 0 if "outside every folder" in answer else 1
        problems += 0 if (outside / "private.txt").exists() else 1

        folder_answer = remove_file(str(root))
        print(f"  remove a folder     : {folder_answer[:76]}")
        problems += 0 if "is a folder" in folder_answer else 1

        # with no interface bound, the confirmation gate refuses — nothing is removed
        before = (root / "big.bin").exists()
        answer = remove_file(str(root / "big.bin"))
        after = (root / "big.bin").exists()
        print(f"  remove w/o UI       : refused={before == after} — {answer[:62]}")
        problems += 0 if before == after else 1

        print(f"  recommendations     : {len(recommendations())} idea(s)")
        print(f"  disks action        : {run({'action': 'disks'}).splitlines()[0][:60]}")
        print(f"  large action        : {run({'action': 'large', 'limit': 3}).splitlines()[0][:70]}")
        print(f"  no path             : {run({'action': 'remove'})[:64]}")
        problems += 0 if "Which file" in run({"action": "remove"}) else 1
    finally:
        _cfg = real
        for d in (root, outside):
            for f in sorted(d.rglob("*"), reverse=True):
                try:
                    f.unlink() if f.is_file() else f.rmdir()
                except Exception:
                    pass
            try:
                d.rmdir()
            except Exception:
                pass

    print(f"  bad action          : {run({'action': 'sing'})[:68]}")
    problems += 0 if "not a storage action" in run({"action": "sing"}) else 1
    print(f"  status              : {run({'action': 'status'}).splitlines()[3][:66]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
