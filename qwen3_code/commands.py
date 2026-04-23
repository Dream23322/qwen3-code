"""Slash-command handlers and the main dispatcher."""

import os
import re
from collections import defaultdict
from pathlib import Path

from rich.panel import Panel
from rich.table import Table

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.settings import CFG, handle_settings, SETTINGS_PATH
from qwen3_code.utils import (
    _short_cwd, resolve_path, read_file, run_command_live,
    IGNORED_DIRS, VC_DIR, SESSION_DIR, STREAM_MAX_LINES, SIZE_REDUCTION_THRESHOLD,
)
from qwen3_code.session import save_session, load_session, _session_path
from qwen3_code.vc import (
    all_tracked_files, _load_vc, do_undo, do_redo, do_checkout,
    do_manual_commit, show_log, vc_commit, vc_baseline,
)
from qwen3_code.refresh import handle_refresh
from qwen3_code.renderer import stream_response

# ---------------------------------------------------------------------------
# /help table
# ---------------------------------------------------------------------------

def _help_table() -> Table:
    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column("cmd",  no_wrap=True, min_width=26)
    t.add_column("desc", justify="right")
    D, E = "[dim]", "[/dim]"
    rows = [
        ("[bold]General[/bold]", ""),
        ("/cd [dir]",            "change working directory"),
        ("/read <file>",         f"load file into context  {D}(saves baseline snapshot){E}"),
        ("/read -a",             f"load ALL files recursively  {D}(skips venv/node_modules etc){E}"),
        ("/refresh",             f"reload tracked files, prune stale context  {D}(gone files removed){E}"),
        ("/run <cmd>",           "run a shell command  [dim](output streams live)[/dim]"),
        ("/clear",               "clear conversation history"),
        ("/check <target>",      f"AI code review  {D}ALL | file | file:func{E}"),
        ("/stackview <type>",    f"inspect state  {D}fh / fhf / sessions / env{E}"),
        ("/settings [key val]",  f"view/edit settings  {D}(saved to settings.json){E}"),
        ("/history",             "show message history"),
        ("/help",                "show this help"),
        ("/quit",                "exit"),
        ("", ""),
        ("[bold]Version control[/bold]", f"{D}git-like, tree-based{E}"),
        ("/undo [file]",         "move HEAD to parent commit"),
        ("/redo [file] [id]",    f"move HEAD to child  {D}(menu if branched){E}"),
        ("/checkout <id> [file]","check out any commit by ID"),
        ("/commit <file> [msg]", "manually commit current file state"),
        ("/log [file]",          "show commit tree"),
        ("/files",               "list all tracked files"),
    ]
    for cmd_text, desc_text in rows:
        t.add_row(f"  {cmd_text}", desc_text)
    return t


# ---------------------------------------------------------------------------
# /check
# ---------------------------------------------------------------------------

_CODE_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".java", ".kt", ".swift",
    ".rb", ".php", ".cs", ".sh", ".bash", ".zsh",
    ".lua", ".r", ".scala", ".zig",
}


def _extract_function(source: str, func_name: str) -> str | None:
    # Python
    py = re.compile(rf"^([ \t]*)(async\s+)?def\s+{re.escape(func_name)}\s*\(", re.MULTILINE)
    m  = py.search(source)
    if m:
        indent = m.group(1)
        lines  = source[m.start():].splitlines(keepends=True)
        body   = [lines[0]]
        for line in lines[1:]:
            if line.strip() and not line.startswith("\t") and indent == "":
                if re.match(r"(async\s+)?def |class ", line.lstrip()): break
            elif line.strip() and indent and not line.startswith(indent + " ") and not line.startswith(indent + "\t"):
                if re.match(r"[ \t]*(async\s+)?def |[ \t]*class ", line): break
            body.append(line)
        while body and not body[-1].strip(): body.pop()
        return "".join(body)
    # JS/TS brace-matching
    js = re.compile(
        rf"(?:(?:async\s+)?function\s+{re.escape(func_name)}|(?:const|let|var)\s+{re.escape(func_name)}\s*=)",
        re.MULTILINE,
    )
    m = js.search(source)
    if m:
        bs = source.find("{", m.start())
        if bs != -1:
            depth, end = 0, bs
            for i, ch in enumerate(source[bs:], bs):
                if ch == "{": depth += 1
                elif ch == "}": depth -= 1
                if depth == 0: end = i + 1; break
            return source[m.start():end]
    return None


def handle_check(arg: str, messages: list[dict], state: dict) -> None:
    cwd = state["cwd"]
    arg = arg.strip()
    if arg.upper() == "ALL":
        source_files = [
            f for f in Path(cwd).rglob("*")
            if f.is_file() and f.suffix.lower() in _CODE_EXTENSIONS
            and not any(p.startswith(".") for p in f.parts)
            and not any(p in IGNORED_DIRS for p in f.parts)
        ]
        if not source_files:
            console.print(f"[info]No source files found in {cwd}.[/info]")
            return
        parts, total, skipped = [], 0, []
        for sf in sorted(source_files):
            rel = os.path.relpath(str(sf), cwd)
            try: content = sf.read_text(encoding="utf-8", errors="replace")
            except Exception: continue
            if total + len(content) > 400_000: skipped.append(rel); continue
            parts.append(f"# -- {rel} --\n{content}")
            total += len(content)
        if skipped:
            console.print(f"[info]Skipped: {', '.join(skipped[:5])}{'...' if len(skipped)>5 else ''}[/info]")
        prompt = (f"Review {len(parts)} file(s) in {cwd} for bugs, logic errors, bad practices, "
                  f"and security issues. State file, severity, description, fix for each issue.\n\n"
                  + "\n\n".join(parts))
    elif ":" in arg and not arg.startswith(":"):
        lc   = arg.rfind(":")
        fp   = resolve_path(arg[:lc], cwd)
        fn   = arg[lc+1:].strip().rstrip("()")
        if not Path(fp).exists():
            console.print(f"[error]File not found: {fp}[/error]")
            return
        src  = read_file(fp)
        fsrc = _extract_function(src, fn)
        if fsrc is None:
            console.print(f"[error]Function '{fn}' not found. Using full file.[/error]")
            fsrc = src
        prompt = f"Review function `{fn}` in `{Path(fp).name}` for bugs and issues.\n\n```\n{fsrc}\n```"
    else:
        fp = resolve_path(arg, cwd)
        if not Path(fp).exists():
            console.print(f"[error]File not found: {fp}[/error]")
            return
        prompt = f"Review `{Path(fp).name}` for bugs and issues.\n\n```\n{read_file(fp)}\n```"

    messages.append({"role": "user", "content": prompt})
    reply = stream_response(messages)
    if reply:
        messages.append({"role": "assistant", "content": reply})
        save_session(cwd, messages)


# ---------------------------------------------------------------------------
# /stackview
# ---------------------------------------------------------------------------

_SV_TYPES = {
    "fh":       "File history (current project)",
    "fhf":      "File history full (all projects)",
    "sessions": "Saved sessions",
    "env":      "Runtime environment info",
}


def _sv_fh(cwd: str) -> None:
    local = [fp for fp in all_tracked_files() if fp.startswith(cwd)]
    if not local:
        console.print(f"[info]No tracked files under {cwd}.[/info]")
        return
    rows = []
    for fp in local:
        idx = _load_vc(fp); n = len(idx.get("commits", {})); hid = idx.get("head", "-")
        hm  = idx["commits"][hid]["message"][:40] if hid and hid in idx["commits"] else "(none)"
        rows.append(f"  {os.path.relpath(fp, cwd):<36}  {'exists' if Path(fp).exists() else 'missing':<7}  {n} commits  HEAD=[cyan]{hid}[/cyan] {hm}")
    console.print(Panel("\n".join(rows), title=f"File history [{_short_cwd(cwd)}]", border_style=SAKURA))


def _sv_fhf() -> None:
    tracked = all_tracked_files()
    if not tracked:
        console.print("[info]No tracked files.[/info]")
        return
    by_dir: dict = defaultdict(list)
    for fp in tracked:
        by_dir[str(Path(fp).parent)].append(fp)
    rows = []
    for d in sorted(by_dir):
        rows.append(f"  [{d}]")
        for fp in sorted(by_dir[d]):
            n = len(_load_vc(fp).get("commits", {}))
            rows.append(f"    {Path(fp).name:<36}  {'exists' if Path(fp).exists() else 'missing':<7}  {n} commits")
    console.print(Panel("\n".join(rows), title="File history (all projects)", border_style=SAKURA))


def _sv_sessions() -> None:
    if not SESSION_DIR.exists() or not list(SESSION_DIR.glob("*.json")):
        console.print("[info]No sessions saved yet.[/info]")
        return
    import json
    rows = []
    for sf in sorted(SESSION_DIR.glob("*.json")):
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            rows.append(f"  {data.get('cwd','?'):<45}  {data.get('saved_at','?')[:19].replace('T','  ')}  {len(data.get('messages',[]))} msg  {sf.stat().st_size} B")
        except Exception:
            rows.append(f"  {sf.name}  (unreadable)")
    console.print(Panel("\n".join(rows), title=f"Saved sessions ({SESSION_DIR})", border_style=SAKURA))


def _sv_env(cwd: str, messages: list[dict]) -> None:
    import sys as _sys
    sf = _session_path(cwd)
    from qwen3_code.settings import SETTINGS_PATH, _model, _app_name, _assistant_name
    rows = [
        f"  Model           : {_model()}",
        f"  App name        : {_app_name()}",
        f"  Assistant name  : {_assistant_name()}",
        f"  Resume session  : {CFG.get('open_from_last_session')}",
        f"  Settings file   : {SETTINGS_PATH}",
        f"  Size-reduction  : >{SIZE_REDUCTION_THRESHOLD*100:.0f}% triggers confirmation",
        f"  CWD             : {cwd}",
        f"  VC dir          : {VC_DIR}",
        f"  Session dir     : {SESSION_DIR}",
        f"  Session file    : {sf}  ({'exists' if sf.exists() else 'none'})",
        f"  Messages        : {len([m for m in messages if m['role'] != 'system'])}",
        f"  Python          : {_sys.version.split()[0]}  ({_sys.executable})",
    ]
    console.print(Panel("\n".join(rows), title="Environment", border_style=SAKURA_DEEP))


def handle_stackview(sv_type: str, cwd: str, messages: list[dict]) -> None:
    t = sv_type.strip().lower()
    if t == "fh":                     _sv_fh(cwd)
    elif t == "fhf":                  _sv_fhf()
    elif t in ("sessions", "sess"):   _sv_sessions()
    elif t in ("env", "environment"): _sv_env(cwd, messages)
    elif t in ("", "help"):
        rows = [f"  {k:<12}  {v}" for k, v in _SV_TYPES.items()]
        console.print(Panel("\n".join(rows), title="/stackview types", border_style=SAKURA_DEEP))
    else:
        console.print(f"[error]Unknown type '{t}'. Run /stackview help.[/error]")


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def handle_slash_command(cmd: str, messages: list[dict], state: dict) -> bool:
    """Dispatch a slash command.  Returns False to signal exit."""
    parts = cmd.strip().split(maxsplit=1)
    name  = parts[0].lower()
    arg   = parts[1] if len(parts) > 1 else ""
    cwd   = state["cwd"]

    if name in ("/quit", "/exit", "/q"):
        console.print("[info]Goodbye.[/info]")
        return False

    elif name == "/cd":
        if not arg:
            console.print(f"[info]Current directory: {cwd}[/info]")
        else:
            target = Path(arg) if Path(arg).is_absolute() else Path(cwd) / arg
            try:
                target = target.resolve(strict=True)
                if not target.is_dir():
                    console.print(f"[error]{target} is not a directory.[/error]")
                else:
                    save_session(cwd, messages)
                    state["cwd"] = str(target)
                    os.chdir(target)
                    new_msgs = load_session(str(target))
                    messages.clear(); messages.extend(new_msgs)
                    state["first_message"] = not any(m["role"] != "system" for m in messages)
                    try: entries = [e.name for e in target.iterdir() if not e.name.startswith(".")][:30]
                    except Exception: entries = []
                    console.print(Panel(f"[info]Changed to: [bold]{target}[/bold]\nContents: {', '.join(entries) or '(empty)'}[/info]",
                                        title="cd", border_style=SAKURA))
            except FileNotFoundError:
                console.print(f"[error]Directory not found: {arg}[/error]")

    elif name == "/clear":
        messages.clear()
        console.clear()
        console.print("[info]Conversation cleared.[/info]")

    elif name == "/read":
        if not arg:
            console.print("[error]Usage: /read <filepath> | /read -a[/error]")
        elif arg.strip() == "-a":
            ignored_found: set[str] = set()
            all_files: list[Path]   = []
            try:
                for root_str, dirs, files in os.walk(cwd):
                    root_path = Path(root_str)
                    to_remove = []
                    for d in dirs:
                        if d.startswith(".") or d in IGNORED_DIRS:
                            ignored_found.add(d)
                            to_remove.append(d)
                    for d in to_remove:
                        dirs.remove(d)
                    for fname in files:
                        if not fname.startswith("."):
                            all_files.append(root_path / fname)
            except Exception as exc:
                console.print(f"[error]{exc}[/error]")
            else:
                snippets, total, skipped, baselined = [], 0, [], 0
                for sf in sorted(all_files):
                    rel = os.path.relpath(str(sf), cwd)
                    try: fc = sf.read_text(encoding="utf-8", errors="replace")
                    except Exception: skipped.append(rel); continue
                    if total + len(fc) > 400_000: skipped.append(rel); continue
                    rf = str(sf.resolve())
                    if not _load_vc(rf).get("head"):
                        vc_commit(rf, fc, "Baseline (pre-edit)")
                        baselined += 1
                    snippets.append(f"### {rel}\n```\n{fc}\n```")
                    total += len(fc)
                if skipped:
                    console.print("[info]Skipped: " + ", ".join(skipped[:5]) + ("..." if len(skipped) > 5 else "") + "[/info]")
                if snippets:
                    ignored_note = ""
                    if ignored_found:
                        dirs_list = ", ".join(f"{d}/" for d in sorted(ignored_found))
                        ignored_note = (
                            f"\n\n[Note: the following directories exist but were not loaded "
                            f"(dependencies / generated / VCS): {dirs_list}]"
                        )
                    state.setdefault("pending_context", []).append(
                        f"Here are all {len(snippets)} file(s) from `{cwd}`:\n\n"
                        + "\n\n".join(snippets)
                        + ignored_note
                    )
                    msg = f"[info]Loaded {len(snippets)} file(s)"
                    if baselined: msg += f", {baselined} baseline(s) saved"
                    if ignored_found: msg += f"  [dim](noted {len(ignored_found)} ignored dir(s))[/dim]"
                    console.print(msg + ".[/info]")
                else:
                    console.print("[info]No readable files found.[/info]")
        else:
            resolved = resolve_path(arg, cwd)
            content  = read_file(resolved)
            if Path(resolved).exists():
                ra = str(Path(resolved).resolve())
                if not _load_vc(ra).get("head"):
                    vc_commit(ra, content, "Baseline (pre-edit)")
                    console.print(f"[info]Baseline saved for [bold]{Path(resolved).name}[/bold][/info]")
            state.setdefault("pending_context", []).append(
                f"Here is the content of `{resolved}`:\n\n```\n{content}\n```"
            )
            console.print(f"[info]Loaded {resolved} into context.[/info]")

    elif name == "/refresh":
        handle_refresh(messages, state)

    elif name == "/run":
        if not arg:
            console.print("[error]Usage: /run <shell command>[/error]")
        else:
            output = run_command_live(arg, cwd)
            messages.append({"role": "user", "content": f"Output of `{arg}`:\n\n```\n{output}\n```"})

    elif name == "/undo":
        tracked = all_tracked_files()
        if not arg:
            candidates = [fp for fp in tracked
                          if _load_vc(fp).get("head") and
                          _load_vc(fp)["commits"].get(_load_vc(fp)["head"], {}).get("parent_id")]
            if not candidates:         console.print("[info]Nothing to undo.[/info]")
            elif len(candidates) == 1: do_undo(candidates[0])
            else:
                console.print("[info]Multiple files:[/info]")
                for fp in candidates: console.print(f"  /undo {fp}")
        else:
            do_undo(resolve_path(arg.split()[0], cwd))

    elif name == "/redo":
        tokens = arg.split(maxsplit=1) if arg else []
        if not tokens:
            tracked = all_tracked_files()
            candidates = [fp for fp in tracked
                          if _load_vc(fp).get("head") and
                          _load_vc(fp)["commits"].get(_load_vc(fp)["head"], {}).get("children")]
            if not candidates:         console.print("[info]Nothing to redo.[/info]")
            elif len(candidates) == 1: do_redo(candidates[0])
            else:
                console.print("[info]Multiple files:[/info]")
                for fp in candidates: console.print(f"  /redo {fp}")
        elif len(tokens) == 1: do_redo(resolve_path(tokens[0], cwd))
        else:                  do_redo(resolve_path(tokens[0], cwd), target_id=tokens[1])

    elif name == "/checkout":
        if not arg:
            console.print("[error]Usage: /checkout <commit_id> [filepath][/error]")
        else:
            tokens = arg.split(maxsplit=1)
            cid_arg = tokens[0]
            fp_arg  = tokens[1] if len(tokens) > 1 else None
            if fp_arg:
                do_checkout(resolve_path(fp_arg, cwd), cid_arg)
            else:
                tracked = all_tracked_files()
                found = [fp for fp in tracked
                         if any(c.startswith(cid_arg) for c in _load_vc(fp).get("commits", {}))]
                if len(found) == 1:   do_checkout(found[0], cid_arg)
                elif len(found) > 1:
                    console.print("[info]Matches multiple files. Specify filepath:[/info]")
                    for fp in found: console.print(f"  /checkout {cid_arg} {fp}")
                else:
                    console.print(f"[error]Commit '{cid_arg}' not found.[/error]")

    elif name == "/files":
        tracked = all_tracked_files()
        if not tracked:
            console.print("[info]No tracked files.[/info]")
        else:
            rows = [f"  {fp}  {'exists' if Path(fp).exists() else 'missing'}  {len(_load_vc(fp).get('commits',{}))} commits  HEAD={_load_vc(fp).get('head','-')}"
                    for fp in tracked]
            console.print(Panel("\n".join(rows), title="Tracked files", border_style=SAKURA))

    elif name == "/check":
        if not arg: console.print("[error]Usage: /check ALL | <file> | <file>:<func>[/error]")
        else:       handle_check(arg, messages, state)

    elif name == "/stackview":  handle_stackview(arg, cwd, messages)
    elif name == "/settings":   handle_settings(arg)

    elif name == "/commit":
        if not arg: console.print("[error]Usage: /commit <filepath> [message][/error]")
        else:
            tokens = arg.split(maxsplit=1)
            do_manual_commit(resolve_path(tokens[0], cwd), tokens[1] if len(tokens) > 1 else "")

    elif name == "/log":
        tracked = all_tracked_files()
        if not arg:
            if not tracked:         console.print("[info]No tracked files.[/info]")
            elif len(tracked) == 1: show_log(tracked[0])
            else:
                console.print("[info]Multiple files:[/info]")
                for fp in tracked: console.print(f"  /log {fp}")
        else:
            show_log(resolve_path(arg.split()[0], cwd))

    elif name == "/history":
        for i, m in enumerate(messages):
            console.print(f"[info][{i}] {m['role']}: {m['content'][:120].replace(chr(10),' ')}[/info]")

    elif name == "/help":
        console.print(Panel(_help_table(), title="Help", border_style=SAKURA_DEEP))

    else:
        console.print(f"[error]Unknown command: {name}[/error]")

    return True
