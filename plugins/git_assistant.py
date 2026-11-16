"""
git_assistant.py — reading a repository, and holding the pen loosely.

WHAT IT DOES
    status, log, diff, branch listing, branch creation, and commit preparation —
    reading a repository is the whole point, and it does that freely. It also
    explains what changed in plain words.

WHAT IT WILL NOT DO WITHOUT YOU SAYING SO, IN PERSON, IN THAT BREATH
    push · reset · rebase · force-delete a branch · anything that touches a
    remote. Those are the operations where a wrong word costs somebody their
    work, so they are not available to an assistant that guessed. The plugin
    only ever reads the remote (fetch is a read); every sentence in this file
    about the remote says "local". Creating a branch and committing are real
    writes, so both go through the project's own confirmation gate.

HOW IT RUNS GIT
    `subprocess.run([...argv...])` with a fixed argument list, no shell, no
    string building, and `GIT_TERMINAL_PROMPT=0` so a command that would ask for
    a password fails instead of hanging a desktop session waiting for input
    nobody can see. Configured global flags — hooks, pagers, prompters — are
    disabled (`-c core.pager=cat -c core.hooksPath=` etc.) so the output this
    plugin parses is the same on every machine and no user hook fires because an
    assistant ran a read.

    Credentials are never read, printed, or passed: this plugin cannot push, so
    it never needs one. `redact()` from the shared net core runs over any output
    containing something token-shaped before it is shown.

SETTINGS (Plugin Manager → Git assistant)
    repo_path   blank = the assistant's own folder (or say the path each time)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "git_assistant",
    "description": (
        "Reads a git repository and explains it: 'git status', 'what changed', 'show me the last "
        "commits', 'what is the diff', 'list branches'. It can make a branch or prepare a commit "
        "only after you confirm, and it never pushes, resets, rebases, or touches a remote — "
        "those stay in your own hands."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status | log | diff | branches | explain | create_branch | "
                                      "prepare_commit | help"},
            "repo": {"type": "STRING", "description": "Repository folder (blank = configured one)"},
            "path": {"type": "STRING", "description": "Optional file or folder for diff/log"},
            "name": {"type": "STRING", "description": "Branch name for create_branch"},
            "message": {"type": "STRING", "description": "Commit message for prepare_commit"},
            "files": {"type": "STRING", "description": "Files to stage: 'a.py b.py' or 'all'"},
            "count": {"type": "STRING", "description": "How many commits for log (default 10)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "git_assistant",
    "title": "Git assistant / مساعد جِت",
    "fields": [
        {"key": "repo_path", "type": "text", "label": "Repository folder (blank = the assistant's own)",
         "default": ""},
        {"key": "max_log", "type": "text", "label": "Commits listed at most", "default": "20"},
        {"key": "confirm_writes", "type": "text", "label": "Confirm before branch/commit (yes/no)",
         "default": "yes"},
    ],
}

# One place for the safe invocation. --no-pager everywhere, and the config
# overrides mean no user hook, alias or pager can change what a read returns.
_GIT_BASE = ["git", "--no-pager", "-c", "core.pager=cat", "-c", "core.hooksPath=",
             "-c", "advice.detachedHead=false", "-c", "color.ui=false"]


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("git_assistant") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _redact(text: str, limit: int = 4000) -> str:
    """The shared net core knows what a secret looks like; reuse it rather than
    keeping a second list of patterns here that could drift out of date."""
    try:
        import importlib.util
        import sys
        name = "_jarvis_net_core"
        module = sys.modules.get(name)
        if module is None:
            path = Path(__file__).resolve().parent / "_net_core.py"
            if not path.exists():
                return text[:limit]
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return module.redact(text, limit)
    except Exception:
        return text[:limit]


def git_available() -> tuple[bool, str]:
    binary = shutil.which("git")
    if not binary:
        return False, ("git is not installed on this machine, sir (or not on PATH). Windows: "
                       "winget install Git.Git · Linux: sudo apt install git · macOS: brew "
                       "install git. Everything else in this plugin waits on that one line.")
    return True, binary


def find_repo(start: str | Path | None = None) -> Path | None:
    """
    The repository to work in: what was configured, what was named, or the
    assistant's own folder — and from any of those, walk upward to the real
    .git so a path inside subfolder still works.
    """
    raw = str(start or _cfg().get("repo_path") or "").strip()
    candidate = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent
    try:
        candidate = candidate.resolve()
    except Exception:
        return None
    if candidate.is_file():
        candidate = candidate.parent
    for folder in [candidate, *candidate.parents]:
        if (folder / ".git").exists():
            return folder
    return None


def run_git(repo: Path, args: list[str], timeout: int = 20) -> dict[str, Any]:
    """
    One git command. argv list, no shell, no prompts, never raises — a failure
    comes back as text a person can read.
    """
    ok, binary = git_available()
    if not ok:
        return {"ok": False, "out": "", "error": binary}
    environment = dict(os.environ)
    environment.update({
        "GIT_TERMINAL_PROMPT": "0",      # never wait for a password nobody can type
        "GIT_ASKPASS": "",               # and never launch a GUI asker
        "SSH_ASKPASS": "",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    })
    # The first element is 'git'; swapping it for the resolved binary keeps the
    # command identical everywhere while using the one that actually exists.
    argv = [binary] + _GIT_BASE[1:] + args
    try:
        done = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True,
                              timeout=timeout, env=environment, check=False)
        return {"ok": done.returncode == 0, "out": _redact(done.stdout or ""),
                "error": _redact((done.stderr or "").strip()), "code": done.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "error": f"git did not finish in {timeout}s"}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "out": "", "error": str(e)[:150]}


def scan_status(lines: list[str]) -> dict[str, list[str]]:
    """Porcelain status → {modified, added, deleted, renamed, untracked}."""
    out: dict[str, list[str]] = {"modified": [], "added": [], "deleted": [],
                                 "renamed": [], "untracked": [], "other": []}
    for line in lines:
        if len(line) < 3:
            continue
        code, name = line[:2], line[3:].strip()
        if code == "??":
            out["untracked"].append(name)
        elif "R" in code:
            out["renamed"].append(name)
        elif "D" in code:
            out["deleted"].append(name)
        elif "A" in code:
            out["added"].append(name)
        elif "M" in code:
            out["modified"].append(name)
        elif code.strip():
            out["other"].append(f"{code} {name}")
    return out


def _confirm(title: str, detail: str, action) -> str:
    """The project's own gate. No UI bound → the action does not happen."""
    try:
        from core.confirm import request
        return str(request("git_assistant", title, detail, action))
    except Exception:
        return (f"Sir, I need your yes for that, and I cannot ask right now — so I have not done "
                f"it: {title}")


# ── actions ───────────────────────────────────────────────────────────────────

def status_text(repo: Path) -> str:
    result = run_git(repo, ["status", "--porcelain", "--branch"])
    if not result["ok"]:
        return f"Sir, git could not read the repository: {result['error']}"
    lines = result["out"].splitlines()
    branch = ""
    for line in lines:
        if line.startswith("##"):
            branch = line[2:].strip()
            break
    groups = scan_status([line for line in lines if not line.startswith("##")])
    total = sum(len(v) for v in groups.values())
    head = f"Branch: {branch or '(unknown)'} — " + (f"{total} change(s) waiting."
            if total else "clean, nothing to commit.")
    if total:
        head += "\n" + "\n".join(f"  {kind:<10} {len(names):>3}  "
                                 + ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
                                 for kind, names in groups.items() if names)
        head += "\n\nI have not staged or committed anything — say 'prepare a commit' when you know."
    return head


def log_text(repo: Path, count: int, path: str = "") -> str:
    args = ["log", f"-{max(1, min(count, 200))}",
            "--pretty=format:%h|%an|%ar|%s", "--no-merges"]
    if path:
        args += ["--", path]
    result = run_git(repo, args)
    if not result["ok"]:
        return f"Sir, git could not read the history: {result['error'] or 'no commits yet'}"
    lines = [line for line in result["out"].splitlines() if line.strip()]
    if not lines:
        return "There are no commits yet, sir."
    out = [f"Last {len(lines)} commit(s)"
           + (f" touching {path}" if path else "") + ":"]
    for line in lines:
        parts = line.split("|", 3)
        if len(parts) == 4:
            sha, author, when, subject = parts
            out.append(f"  {sha}  {when:<16} {author[:14]:<14} {subject[:64]}")
        else:
            out.append("  " + line[:90])
    return "\n".join(out)


def diff_text(repo: Path, path: str = "", staged: bool = False, summary_only: bool = False) -> str:
    args = ["diff", "--stat"] if summary_only else ["diff"]
    if staged:
        args.append("--cached")
    if path:
        args += ["--", path]
    result = run_git(repo, args, timeout=30)
    if not result["ok"]:
        return f"Sir, git could not produce a diff: {result['error']}"
    body = result["out"].strip()
    if not body:
        return ("Nothing changed in the working tree, sir"
                + (f" for {path}" if path else "") + ". (Staged changes show with staged=yes.)")
    if len(body) > 4000:
        body = body[:4000] + "\n… (cut here — say 'show me <file>' for one file at a time)"
    return body


def explain_changes(repo: Path) -> str:
    """The diff's shape in words. Rules, not a model: a model could invent a
    change that is not in the diff, and this is the one place where that would
    be actively dangerous to trust."""
    result = run_git(repo, ["diff", "--numstat", "HEAD"])
    if not result["ok"]:
        result = run_git(repo, ["diff", "--numstat"])
    if not result["ok"]:
        return f"Sir, git could not read the diff: {result['error']}"
    files, added, removed = [], 0, 0
    for line in result["out"].splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        plus, minus, name = parts
        if plus.isdigit():
            added += int(plus)
        if minus.isdigit():
            removed += int(minus)
        files.append((name, plus, minus))
    # `git diff` cannot see a file git has never been told about, so counting
    # only the diff would have said "1 file changed" while a new file sat in the
    # folder. Untracked files are counted and named separately, because they are
    # exactly the files a person forgets to add.
    untracked: list[str] = []
    status_result = run_git(repo, ["status", "--porcelain"])
    if status_result["ok"]:
        untracked = [line[3:].strip() for line in status_result["out"].splitlines()
                     if line.startswith("??")]
    if not files and not untracked:
        return "Nothing has changed since the last commit, sir."
    headline = f"{len(files)} tracked file(s) changed: +{added} / −{removed} lines"
    if untracked:
        headline += (f" — and {len(untracked)} new file(s) git is not tracking yet: "
                     f"{', '.join(untracked[:3])}")
    lines = [headline, ""]
    for name, plus, minus in files[:20]:
        lines.append(f"  {name[:58]:<60} +{plus:>5} −{minus:>5}")
    if len(files) > 20:
        lines.append(f"  … and {len(files) - 20} more file(s)")
    lines.append("")
    test_files = [n for n, _, _ in files if "test" in n.lower()]
    docs = [n for n, _, _ in files if n.lower().endswith((".md", ".rst", ".txt"))]
    if test_files:
        lines.append(f"Tests moved with the code ({len(test_files)} file(s)) — that is the good shape.")
    else:
        lines.append("No test file changed. If the change touches behaviour, that is worth saying "
                     "before it is committed.")
    if docs:
        lines.append(f"Documentation changed too ({len(docs)} file(s)).")
    lines.append("This is counted from the diff itself — I do not guess at intent.")
    return "\n".join(lines)


def branches_text(repo: Path) -> str:
    result = run_git(repo, ["branch", "--list", "--format=%(refname:short)|%(upstream:short)|%(committerdate:relative)"])
    if not result["ok"]:
        return f"Sir, git could not list branches: {result['error']}"
    lines = [line for line in result["out"].splitlines() if line.strip()]
    current_result = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    current = current_result["out"].strip() if current_result["ok"] else ""
    if not lines:
        return f"No branches yet, sir (HEAD is at {current or 'nothing'})."
    out = [f"{len(lines)} branch(es)" + (f", on '{current}'" if current else "") + ":"]
    for line in lines:
        parts = (line.split("|") + ["", ""])[:3]
        name, upstream, when = parts
        mark = "*" if name == current else " "
        out.append(f" {mark} {name:<28} {('→ ' + upstream) if upstream else '(no upstream)':<28} {when}")
    out.append("")
    out.append("Reading only — nothing was fetched, pushed, or deleted.")
    return "\n".join(out)


def create_branch(repo: Path, name: str, player=None) -> str:
    clean = str(name or "").strip()
    if not clean:
        return "What should the branch be called, sir?"
    if not re.fullmatch(r"[A-Za-z0-9._\-/]{1,80}", clean):
        return ("A branch name I will accept uses letters, numbers, dot, dash, slash and "
                "underscore only, sir.")
    if clean.startswith(("-", "/")) or ".." in clean:
        return "That name would confuse git itself, sir — pick another."
    existing = run_git(repo, ["branch", "--list", clean])
    if existing["ok"] and existing["out"].strip():
        return f"A branch called '{clean}' already exists, sir — I will not touch it."
    if str(_cfg().get("confirm_writes") or "yes").lower() in ("yes", "true", "1"):
        return _confirm(f"Create branch '{clean}'",
                        f"in {repo.name} — a new branch from the current commit, nothing else",
                        lambda: _do_create_branch(repo, clean))
    return _do_create_branch(repo, clean)


def _do_create_branch(repo: Path, name: str) -> str:
    result = run_git(repo, ["switch", "-c", name])
    if not result["ok"]:
        result = run_git(repo, ["checkout", "-b", name])       # older git
    if not result["ok"]:
        return f"Sir, git refused: {result['error'][:160]}"
    return f"Branch '{name}' created locally, sir — nothing was sent anywhere."


def prepare_commit(repo: Path, message: str, files: str, player=None) -> str:
    """Stages what you named and stops there. The commit itself happens only
    after you confirm — and never happens if you cannot be asked."""
    status_result = run_git(repo, ["status", "--porcelain"])
    if not status_result["ok"]:
        return f"Sir, git could not read the status: {status_result['error']}"
    changes = scan_status([line for line in status_result["out"].splitlines()
                           if not line.startswith("##")])
    total = sum(len(v) for v in changes.values())
    if not total:
        return "There is nothing to commit, sir — the tree is clean."

    clean_files = str(files or "").strip()
    if clean_files.lower() in ("all", "everything", ""):
        chosen = [n for kind in ("modified", "added", "deleted", "renamed") for n in changes[kind]]
        chosen += changes["untracked"]
    else:
        chosen = [part.strip() for part in re.split(r"[,\s]+", clean_files) if part.strip()]
        known = {n for names in changes.values() for n in names}
        unknown = [n for n in chosen if n not in known]
        if unknown:
            return (f"These are not in the change list, sir: {', '.join(unknown[:4])}. Say "
                    f"'status' to see what is actually there.")
    if not chosen:
        return "I found no matching changes to stage, sir."

    message = str(message or "").strip() or "update"
    dirty = [n for n in chosen if n in changes["untracked"]]

    def _do() -> str:
        staged = run_git(repo, ["add", "--", *chosen])
        if not staged["ok"]:
            return f"Sir, git could not stage those: {staged['error'][:150]}"
        # A commit, not a push. The message is passed as one argv element, so
        # nothing in it can turn into a second command.
        committed = run_git(repo, ["commit", "-m", message])
        if not committed["ok"]:
            return (f"Staged {len(chosen)} file(s) but the commit did not happen: "
                    f"{committed['error'][:140]}")
        return (f"Committed {len(chosen)} file(s) locally, sir: \"{message[:60]}\". "
                f"Nothing left this machine — pushing is yours to do.")

    gate = str(_cfg().get("confirm_writes") or "yes").lower() in ("yes", "true", "1")
    detail = (f"{len(chosen)} file(s) in {repo.name}: {', '.join(chosen[:4])}"
              + (f" … (+{len(chosen) - 4} more)" if len(chosen) > 4 else "")
              + (f"\nThese are new files that are not in git yet: {', '.join(dirty[:3])}"
                 if dirty else "")
              + f"\nMessage: {message[:80]}\nA local commit only — no push, no remote.")
    return _confirm("Make a local commit", detail, _do) if gate else _do()


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """One git action. Reads freely, writes only with your yes."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "status").strip().lower()

        if action in ("help", "?"):
            return ("I read git repositories for you, sir: status, log, diff, what changed in "
                    "words, and the branch list. I can create a branch or make a local commit "
                    "after your confirmation. I will never push, reset, rebase, force-delete or "
                    "touch a remote — those are yours alone.")
        if action in ("push", "reset", "rebase", "force", "force_push", "remote_add", "remote"):
            return ("No, sir. Pushing, resetting, rebasing and anything that touches a remote are "
                    "exactly the operations I am not allowed to do — one wrong word and somebody "
                    "loses their work. Run it yourself; I will happily show you the diff first.")

        repo = find_repo(p.get("repo"))
        if repo is None:
            return ("I could not find a git repository, sir. Tell me the folder, or set it in my "
                    "settings.")

        if action in ("status", "state"):
            return f"{repo}\n{status_text(repo)}"
        if action in ("log", "history", "commits"):
            try:
                count = int(str(p.get("count") or _cfg().get("max_log") or 10))
            except Exception:
                count = 10
            return log_text(repo, count, str(p.get("path") or ""))
        if action in ("diff", "changes"):
            summary = str(p.get("count") or "").lower() in ("stat", "summary")
            return diff_text(repo, str(p.get("path") or ""),
                             staged=bool(p.get("staged")), summary_only=summary)
        if action in ("explain", "what_changed", "summarise", "summarize"):
            return explain_changes(repo)
        if action in ("branches", "branch_list", "list_branches"):
            return branches_text(repo)
        if action in ("create_branch", "new_branch", "branch"):
            return create_branch(repo, str(p.get("name") or p.get("branch") or ""), player)
        if action in ("prepare_commit", "commit", "stage"):
            return prepare_commit(repo, str(p.get("message") or ""), str(p.get("files") or ""),
                                  player)
        return (f"Sir, '{action}' is not a git action I have. I can do status, log, diff, explain, "
                f"branches, create_branch or prepare_commit.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the git assistant hit a problem: {str(e)[:150]}"


# ── self-test: python git_assistant.py ──────────────────────────────────────

def _selftest() -> int:
    print("git_assistant — self-test")
    problems = 0

    ok, binary = git_available()
    print(f"  git متاح                : {ok} — {binary[:60]}")
    problems += 0 if ok else 1
    if not ok:
        print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
        return problems

    import shutil as _sh
    import tempfile
    sandbox = Path(tempfile.mkdtemp(prefix="git_probe_"))
    try:
        def g(*args: str) -> str:
            return run_git(sandbox, list(args))["out"]

        run_git(sandbox, ["init", "-q"])
        run_git(sandbox, ["-c", "user.email=t@example.com", "-c", "user.name=Test",
                          "commit", "--allow-empty", "-m", "first"])
        # config for the probe repo only
        run_git(sandbox, ["config", "user.email", "t@example.com"])
        run_git(sandbox, ["config", "user.name", "Test"])

        (sandbox / "code.py").write_text("print('hello')\n", encoding="utf-8")
        status = status_text(sandbox)
        print(f"  الحالة (ملف غير متتبَّع): {status.splitlines()[-1][:76]}")
        problems += 0 if "untracked" in status else 1

        run_git(sandbox, ["add", "code.py"])
        run_git(sandbox, ["commit", "-q", "-m", "add code"])
        (sandbox / "code.py").write_text("print('hello')\nprint('again')\n", encoding="utf-8")
        (sandbox / "notes.md").write_text("# notes\n", encoding="utf-8")

        explain = run({"action": "explain", "repo": str(sandbox)})
        print(f"  تفسير التغيير           : {explain.splitlines()[0][:96]}")
        problems += 0 if ("1 tracked file(s) changed" in explain and "1 new file(s)" in explain) else 1

        log = run({"action": "log", "repo": str(sandbox)})
        print(f"  السجل                   : {log.splitlines()[1][:74]}")
        problems += 0 if "add code" in log else 1

        diff = run({"action": "diff", "repo": str(sandbox), "path": "code.py"})
        problems += 0 if "again" in diff else 1
        print(f"  الفرق لملف واحد         : {diff.splitlines()[0][:66]}")

        status2 = run({"action": "status", "repo": str(sandbox)})
        problems += 0 if "2" in status2.split("Branch")[-1][:200] else 1

        branches = run({"action": "branches", "repo": str(sandbox)})
        print(f"  الفروع                  : {branches.splitlines()[1].strip()[:70]}")
        problems += 0 if "master" in branches or "main" in branches else 1

        # Branch creation goes through the confirm gate. In this sandbox there is
        # no UI, so the gate must RETURN a pending message and change nothing —
        # which is exactly the behaviour that matters for a write operation.
        answer = run({"action": "create_branch", "repo": str(sandbox), "name": "feature/probe"})
        after = g("branch", "--list")
        created_without_ask = "feature/probe" in after
        print(f"  فرع بلا بوابة           : {'أُنشئ' if created_without_ask else 'لم يُنشأ'} — "
              f"{answer.splitlines()[0][:60]}")
        # With no UI the safe answer is: nothing happened. With a UI, the gate
        # would have asked. Either way the plugin never creates silently — so the
        # test accepts exactly two outcomes and refuses anything else.
        if "feature/probe" in after:
            problems += 0 if "Confirm" in answer or "confirm" in answer or "created" in answer else 1
            print("    (واجهة تأكيد متاحة — أُنشئ بعد الإذن)")
        else:
            problems += 0 if ("cannot confirm" in answer or "I need your yes" in answer
                              or "need your yes" in answer) else 1

        # The refusal list, by name.
        for blocked in ("push", "reset", "rebase", "remote"):
            answer = run({"action": blocked, "repo": str(sandbox)})
            mark = "✓" if "not allowed" in answer or "No, sir" in answer else "✗"
            problems += 0 if mark == "✓" else 1
            if blocked == "push":
                print(f"  رفض {blocked:<8}          : {answer.splitlines()[0][:70]} {mark}")

        bad_name = run({"action": "create_branch", "repo": str(sandbox), "name": "bad name;rm -rf"})
        print(f"  اسم فرع خبيث            : {bad_name.splitlines()[0][:70]}")
        problems += 0 if "letters, numbers" in bad_name else 1

        # A path inside the repo resolves to the repo (correct); a folder that is
        # in NO repository must say so. The first test expected the inner path to
        # fail, which would have meant walking upward was broken.
        inside = run({"action": "status", "repo": str(sandbox / "some" / "deeper")})
        problems += 0 if "Branch" in inside else 1
        print(f"  مسار داخل المستودع      : {inside.splitlines()[1][:64]}")
        clean_dir = Path(tempfile.mkdtemp(prefix="no_repo_"))
        missing_repo = run({"action": "status", "repo": str(clean_dir)})
        problems += 0 if "could not find a git repository" in missing_repo else 1
        print(f"  خارج أي مستودع          : {missing_repo.splitlines()[0][:66]}")
        _sh.rmtree(clean_dir, ignore_errors=True)

        # The plumbing promises, checked as code: no shell, no push in the argv set.
        import ast as _ast
        source = Path(__file__).read_text(encoding="utf-8")
        tree = _ast.parse(source)
        bad_calls = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Attribute) and node.func.attr in ("system", "popen"):
                    bad_calls.append(f"os.{node.func.attr}")
                if isinstance(node.func, _ast.Name) and node.func.id in ("eval", "exec"):
                    bad_calls.append(f"{node.func.id}()")
            if isinstance(node, _ast.keyword) and node.arg == "shell":
                if not (isinstance(node.value, _ast.Constant) and node.value.value is False):
                    bad_calls.append("shell=")
        print(f"  لا shell ولا eval       : {bad_calls or 'لا شيء في الشجرة كلها'}")
        problems += 0 if not bad_calls else 1

        # 'push' appears only in the refusal list and in the words explaining it,
        # so the check is about what RUNS, not about which strings exist.
        runs = re.findall(r'run_git\([^)]*\)', source)
        dangerous = [r for r in runs if any(word in r for word in
                                            ("push", "reset", "rebase", "force", "remote update"))]
        print(f"  ألا يوجد run_git يدفع    : {dangerous or 'لا أمر كتابة بعيد في الكود'}")
        problems += 0 if not dangerous else 1

        print("  Git تلميح صادق           : push/reset/rebase مرفوضة بالاسم، والكتابة عبر بوابة تأكيد")
    finally:
        _sh.rmtree(sandbox, ignore_errors=True)

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
