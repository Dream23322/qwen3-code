"""Slash-command handlers and the main dispatcher."""

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from rich.markup import escape as _esc
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree as RichTree

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.settings import CFG, handle_settings, SETTINGS_PATH, save_settings, DEFAULT_SETTINGS
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
from qwen3_code.context_tools import handle_context
from qwen3_code.council import handle_council

# ---------------------------------------------------------------------------
# Description cache
# ---------------------------------------------------------------------------

_DESC_DIR: Path = Path.home() / ".local" / "share" / "qwen3-code" / "descriptions"


def _desc_cache_path(cwd: str) -> Path:
    h = hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:16]
    return _DESC_DIR / f"{h}.json"


def _load_desc_cache(cwd: str) -> dict[str, str] | None:
    p = _desc_cache_path(cwd)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("_cwd") != cwd:
            return None
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        return None


def _save_desc_cache(cwd: str, descriptions: dict[str, str]) -> None:
    _DESC_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {"_cwd": cwd, "_generated_at": datetime.now().isoformat()}
    data.update(descriptions)
    _desc_cache_path(cwd).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _desc_context_block(cwd: str, descriptions: dict[str, str]) -> str:
    lines = [f"Project file descriptions for `{cwd}`:", ""]
    for rel, desc in sorted(descriptions.items()):
        lines.append(f"  {rel}  \u2014  {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_ignored_dir(entry: Path, include_ignored: bool) -> bool:
    if include_ignored:
        return False
    if entry.name.startswith("."):
        return True
    if entry.name in IGNORED_DIRS:
        return True
    if (entry / "pyvenv.cfg").exists():
        return True
    return False


# ---------------------------------------------------------------------------
# /learn
# ---------------------------------------------------------------------------

def handle_learn(arg: str) -> None:
    """Toggle or explicitly set learn mode."""
    a = arg.strip().lower()
    current: bool = bool(CFG.get("learn_mode", False))

    if a in ("on", "true", "1", "yes"):
        new_val = True
    elif a in ("off", "false", "0", "no"):
        new_val = False
    elif a == "":
        new_val = not current
    else:
        console.print("[error]Usage: /learn  |  /learn on  |  /learn off[/error]")
        return

    CFG["learn_mode"] = new_val
    save_settings(CFG)

    if new_val:
        console.print(Panel(
            "[bold green]\u2713 Learn mode ON[/bold green]\n\n"
            "The AI will now:\n"
            "  \u2022 Explain the [bold]why[/bold] behind every step\n"
            "  \u2022 Break solutions into small numbered steps\n"
            "  \u2022 Define jargon and use analogies\n"
            "  \u2022 Guide you instead of doing everything silently\n"
            "  \u2022 Encourage you to try parts yourself\n\n"
            "[dim]Toggle off with [bold]/learn[/bold] or [bold]/learn off[/bold][/dim]",
            title="/learn",
            border_style=SAKURA,
        ))
    else:
        console.print(Panel(
            "[bold]Learn mode OFF[/bold]\n"
            "[dim]Back to standard concise mode. Toggle on with [bold]/learn[/bold][/dim]",
            title="/learn",
            border_style=SAKURA_MUTED,
        ))


# ---------------------------------------------------------------------------
# /help table
# ---------------------------------------------------------------------------

def _help_table() -> Table:
    learn_status = " [green](ON)[/green]" if CFG.get("learn_mode") else ""
    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column("cmd",  no_wrap=True, min_width=26)
    t.add_column("desc", justify="right")
    D, E = "[dim]", "[/dim]"
    rows = [
        ("[bold]General[/bold]", ""),
        ("/cd [dir]",             "change working directory"),
        ("/read <file>",          f"load file into context  {D}(saves baseline snapshot){E}"),
        ("/read -a",              f"load ALL files recursively  {D}(skips venv/node_modules etc){E}"),
        ("/tree [-i]",            f"show project file tree  {D}(-i includes ignored dirs){E}"),
        ("/v [-i]",               f"generate + cache AI descriptions, show tree  {D}(streamed){E}"),
        ("/loadtree [-i] [-d]",   f"inject project tree into AI context  {D}(-i incl. ignored, -d adds AI descriptions){E}"),
        ("/context [sub]",        f"context tools  {D}display / clear / clean{E}"),
        ("/refresh",              f"reload tracked files, prune stale context  {D}(gone files removed){E}"),
        ("/run <cmd>",            "run a shell command  [dim](output streams live)[/dim]"),
        ("/plan <task>",          f"AI plans then auto-executes a task"),
        ("/council [start|end]",  f"multi-model deliberation  {D}members answer, leader picks{E}"),
        ("/learn [on|off]",       f"beginner tutorial mode{learn_status}"),
        ("/clear",                "clear conversation history"),
        ("/check <target>",       f"AI code review  {D}ALL | file | file:func{E}"),
        ("/stackview <type>",     f"inspect state  {D}fh / fhf / sessions / env / tree{E}"),
        ("/settings [key val]",   f"view/edit settings  {D}(saved to settings.json){E}"),
        ("/history",              "show message history"),
        ("/help",                 "show this help"),
        ("/quit",                 "exit"),
        ("", ""),
        ("[bold]Version control[/bold]", f"{D}git-like, tree-based{E}"),
        ("/undo [file]",          "move HEAD to parent commit"),
        ("/redo [file] [id]",     f"move HEAD to child  {D}(menu if branched){E}"),
        ("/checkout <id> [file]", "check out any commit by ID"),
        ("/commit <file> [msg]",  "manually commit current file state"),
        ("/log [file]",           "show commit tree"),
        ("/files",                "list all tracked files"),
    ]
    for cmd_text, desc_text in rows:
        t.add_row(f"  {cmd_text}", desc_text)
    return t


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def _collect_files_for_tree(
    root: str,
    include_ignored: bool = False,
) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for rdir, dirs, fnames in os.walk(root):
        rpath = Path(rdir)
        dirs[:] = [
            d for d in sorted(dirs)
            if not _is_ignored_dir(rpath / d, include_ignored)
        ]
        for fname in sorted(fnames):
            if not include_ignored and fname.startswith("."):
                continue
            fp = rpath / fname
            results.append((os.path.relpath(str(fp), root), str(fp)))
    return results


def _generate_file_descriptions_streamed(
    files: list[tuple[str, str]],
    cwd: str = "",
) -> dict[str, str]:
    import ollama
    from rich.live import Live
    from qwen3_code.settings import _model

    result: dict[str, str] = {}
    subset = files[:40]
    total  = len(subset)

    for idx, (rel, absp) in enumerate(subset, 1):
        try:
            lines   = Path(absp).read_text(encoding="utf-8", errors="replace").splitlines()[:12]
            snippet = "\n".join(lines)
        except Exception:
            snippet = "(unreadable)"

        prompt = (
            "Give a single short description of this file (8 words max). "
            "Output ONLY the description \u2014 no filename, no prefix, no punctuation at the end.\n\n"
            f"File: {rel}\n{snippet}"
        )

        tokens: list[str] = []
        progress = f"[dim]({idx}/{total})[/dim]"

        def _panel(desc_so_far: str, _rel: str = rel, _prog: str = progress) -> Panel:
            body = (
                f"{_prog}  [bold]{_esc(_rel)}[/bold]\n"
                f"[dim]{_esc(desc_so_far) if desc_so_far else 'thinking\u2026'}[/dim]"
            )
            return Panel(body, title="Describing files", border_style=SAKURA_MUTED)

        try:
            with Live(
                _panel(""),
                console=console,
                refresh_per_second=20,
                transient=True,
            ) as live:
                for chunk in ollama.chat(
                    model=_model(),
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                ):
                    token = chunk["message"]["content"]
                    tokens.append(token)
                    live.update(_panel("".join(tokens).strip()))
        except Exception:
            continue

        desc = "".join(tokens).strip().splitlines()[0].strip()
        if desc:
            result[rel] = desc

    if cwd and result:
        _save_desc_cache(cwd, result)
        console.print(f"[dim]Descriptions cached ({len(result)} files).[/dim]")

    return result


def _build_rich_tree(
    root: str,
    include_ignored: bool = False,
    descriptions: dict[str, str] | None = None,
) -> RichTree:
    tracked = set(all_tracked_files())

    def _add_children(node: RichTree, path: Path) -> None:
        try:
            entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return
        ignored_names: list[str] = []
        for entry in entries:
            if entry.is_dir() and _is_ignored_dir(entry, include_ignored):
                ignored_names.append(entry.name)
                continue
            if entry.is_dir():
                branch = node.add(f"[bold]{entry.name}/[/bold]")
                _add_children(branch, entry)
            else:
                if not include_ignored and entry.name.startswith("."):
                    continue
                rel = os.path.relpath(str(entry), root)
                is_tracked = str(entry.resolve()) in tracked
                desc_part  = ""
                if descriptions and rel in descriptions:
                    desc_part = f"  [dim]\u2014 {_esc(descriptions[rel])}[/dim]"
                if is_tracked:
                    node.add(f"[{SAKURA}]{entry.name}[/{SAKURA}] [dim](tracked)[/dim]{desc_part}")
                else:
                    node.add(f"[dim]{entry.name}[/dim]{desc_part}")
        if ignored_names:
            node.add(f"[dim italic]\u2026 {len(ignored_names)} ignored dir(s): {", ".join(ignored_names)}[/dim italic]")

    root_label = f"[bold]{Path(root).name}/[/bold]"
    tree = RichTree(root_label)
    _add_children(tree, Path(root))
    return tree


def _build_text_tree(
    root: str,
    include_ignored: bool = False,
    descriptions: dict[str, str] | None = None,
) -> str:
    lines: list[str] = [Path(root).name + "/"]

    def _walk(path: Path, prefix: str) -> None:
        try:
            entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return
        filtered: list[Path] = []
        ignored_names: list[str] = []
        for e in entries:
            if e.is_dir() and _is_ignored_dir(e, include_ignored):
                ignored_names.append(e.name)
                continue
            if not include_ignored and e.is_file() and e.name.startswith("."):
                continue
            filtered.append(e)
        for i, entry in enumerate(filtered):
            is_last = (i == len(filtered) - 1) and not ignored_names
            conn = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
            if entry.is_dir():
                lines.append(prefix + conn + entry.name + "/")
                _walk(entry, prefix + ("    " if is_last else "\u2502   "))
            else:
                rel = os.path.relpath(str(entry), root)
                desc_suffix = f"  \u2014 {descriptions[rel]}" if descriptions and rel in descriptions else ""
                lines.append(prefix + conn + entry.name + desc_suffix)
        if ignored_names:
            lines.append(prefix + "\u2514\u2500\u2500 " + f"[{len(ignored_names)} ignored: " + ", ".join(ignored_names) + "]")

    _walk(Path(root), "")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /plan
# ---------------------------------------------------------------------------

def handle_plan(task: str, messages: list[dict], state: dict) -> None:
    cwd = state["cwd"]
    if not task.strip():
        console.print("[error]Usage: /plan <task description>[/error]")
        return

    plan_prompt = (
        f"Create a concise numbered step-by-step plan for the following task. "
        f"Be specific about which files to create or edit and which commands to run. "
        f"Output the plan only \u2014 do NOT start implementing yet.\n\nTask: {task}"
    )
    messages.append({"role": "user", "content": plan_prompt})
    console.print(Panel(f"[bold]Planning:[/bold] {task}", title="/plan", border_style=SAKURA_MUTED))
    plan_reply = stream_response(messages)
    if not plan_reply:
        messages.pop()
        return
    messages.append({"role": "assistant", "content": plan_reply})

    exec_prompt = (
        "Good plan. Now execute it step by step. "
        "Use <!-- WRITE: path --> markers for any file edits and "
        "<!-- RUN: cmd --> markers for any shell commands that need to run."
    )
    messages.append({"role": "user", "content": exec_prompt})
    console.print(Panel("[bold]Executing plan\u2026[/bold]", title="/plan", border_style=SAKURA_MUTED))
    exec_reply = stream_response(messages, cwd)
    if exec_reply:
        messages.append({"role": "assistant", "content": exec_reply})
    else:
        messages.pop()
    save_session(cwd, messages)


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
            and not any(
                _is_ignored_dir(Path(*f.parts[:i+1]), False)
                for i in range(1, len(f.parts))
                if Path(*f.parts[:i+1]).is_dir()
            )
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
    "tree":     "Project tree (uses cached AI descriptions; run /v to generate)",
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
    cached = _load_desc_cache(cwd)
    rows = [
        f"  Model           : {_model()}",
        f"  App name        : {_app_name()}",
        f"  Assistant name  : {_assistant_name()}",
        f"  Resume session  : {CFG.get('open_from_last_session')}",
        f"  Learn mode      : {'ON' if CFG.get('learn_mode') else 'off'}",
        f"  Settings file   : {SETTINGS_PATH}",
        f"  Size-reduction  : >{SIZE_REDUCTION_THRESHOLD*100:.0f}% triggers confirmation",
        f"  CWD             : {cwd}",
        f"  VC dir          : {VC_DIR}",
        f"  Session dir     : {SESSION_DIR}",
        f"  Session file    : {sf}  ({'exists' if sf.exists() else 'none'})",
        f"  Desc cache      : {_desc_cache_path(cwd)}  ({len(cached)} entries)" if cached else f"  Desc cache      : (none \u2014 run /v to generate)",
        f"  Messages        : {len([m for m in messages if m['role'] != 'system'])}",
        f"  Python          : {_sys.version.split()[0]}  ({_sys.executable})",
    ]
    console.print(Panel("\n".join(rows), title="Environment", border_style=SAKURA_DEEP))


def _sv_tree(cwd: str, state: dict) -> None:
    cached = _load_desc_cache(cwd)
    if cached is None:
        console.print(
            "[info]No cached descriptions for this directory.\n"
            "Run [bold]/v[/bold] or [bold]/loadtree -d[/bold] to generate and cache them.[/info]"
        )
        return

    tree = _build_rich_tree(cwd, include_ignored=False, descriptions=cached)
    ts   = ""
    cp   = _desc_cache_path(cwd)
    if cp.exists():
        try:
            meta = json.loads(cp.read_text(encoding="utf-8"))
            ts   = meta.get("_generated_at", "")[:19].replace("T", "  ")
        except Exception:
            pass
    title = f"Tree + AI descriptions [{_short_cwd(cwd)}]"
    if ts:
        title += f"  (cached {ts})"
    console.print(Panel(tree, title=title, border_style=SAKURA_DEEP))

    state.setdefault("pending_context", []).append(
        _desc_context_block(cwd, cached)
    )
    console.print(f"[dim]Descriptions ({len(cached)} files) added to AI context.[/dim]")


def handle_stackview(sv_type: str, cwd: str, messages: list[dict], state: dict) -> None:
    t = sv_type.strip().lower()
    if t == "fh":                     _sv_fh(cwd)
    elif t == "fhf":                  _sv_fhf()
    elif t in ("sessions", "sess"):   _sv_sessions()
    elif t in ("env", "environment"): _sv_env(cwd, messages)
    elif t == "tree":                 _sv_tree(cwd, state)
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
                    messages.clear()
                    messages.extend(new_msgs)
                    # ---- Reset per-directory state ----
                    state["pending_context"] = []   # discard staged context from old dir
                    state["first_message"]   = not any(m["role"] != "system" for m in messages)
                    try: entries = [e.name for e in target.iterdir() if not e.name.startswith(".")][:30]
                    except Exception: entries = []
                    msg_count = len([m for m in messages if m["role"] != "system"])
                    session_note = (
                        f"Resumed session  ({msg_count} message(s))"
                        if msg_count else "New session"
                    )
                    console.print(Panel(
                        f"[info]Changed to: [bold]{target}[/bold]\n"
                        f"Contents: {', '.join(entries) or '(empty)'}\n"
                        f"[dim]{session_note}[/dim][/info]",
                        title="cd", border_style=SAKURA,
                    ))
            except FileNotFoundError:
                console.print(f"[error]Directory not found: {arg}[/error]")

    elif name == "/clear":
        messages.clear()
        console.clear()
        console.print("[info]Conversation cleared.[/info]")

    elif name == "/learn":
        handle_learn(arg)

    elif name == "/tree":
        include_ignored = "-i" in arg
        tree = _build_rich_tree(cwd, include_ignored=include_ignored)
        console.print(Panel(tree, title=f"Tree [{_short_cwd(cwd)}]{'  (all dirs)' if include_ignored else ''}",
                            border_style=SAKURA_DEEP))

    elif name == "/v":
        include_ignored = "-i" in arg
        file_list = _collect_files_for_tree(cwd, include_ignored=include_ignored)
        if not file_list:
            console.print("[info]No files found.[/info]")
        else:
            console.print(f"[dim]Generating descriptions for {len(file_list[:40])} file(s)\u2026[/dim]")
            descriptions = _generate_file_descriptions_streamed(file_list, cwd=cwd)
            tree = _build_rich_tree(cwd, include_ignored=include_ignored, descriptions=descriptions)
            title = f"Tree + descriptions [{_short_cwd(cwd)}]"
            if include_ignored: title += "  (all dirs)"
            console.print(Panel(tree, title=title, border_style=SAKURA_DEEP))
            if descriptions:
                state.setdefault("pending_context", []).append(
                    _desc_context_block(cwd, descriptions)
                )
                console.print(f"[dim]Descriptions added to AI context.[/dim]")

    elif name == "/loadtree":
        flags = arg.lower().split()
        include_ignored   = "-i" in flags
        with_descriptions = "-d" in flags

        descriptions: dict[str, str] | None = None
        if with_descriptions:
            descriptions = _load_desc_cache(cwd)
            if descriptions:
                console.print(f"[dim]Using cached descriptions ({len(descriptions)} files).[/dim]")
            else:
                file_list = _collect_files_for_tree(cwd, include_ignored=include_ignored)
                if file_list:
                    console.print(f"[dim]Generating descriptions for {len(file_list[:40])} file(s)\u2026[/dim]")
                    descriptions = _generate_file_descriptions_streamed(file_list, cwd=cwd)
                    console.print(f"[dim]Got descriptions for {len(descriptions)} file(s).[/dim]")

        tree_text = _build_text_tree(cwd, include_ignored=include_ignored, descriptions=descriptions)
        note_parts = []
        if include_ignored:   note_parts.append("all directories included")
        if with_descriptions: note_parts.append("AI descriptions included")
        note = "(" + ", ".join(note_parts) + ")" if note_parts else "(ignored dirs noted but not expanded)"
        context_block = (
            f"Project directory tree for `{cwd}` {note}:\n\n"
            f"```\n{tree_text}\n```"
        )
        state.setdefault("pending_context", []).append(context_block)
        line_count = tree_text.count("\n") + 1
        console.print(f"[info]Project tree loaded into context ({line_count} lines). {note}[/info]")

    elif name == "/context":
        handle_context(arg, messages, state)

    elif name == "/council":
        handle_council(arg, state)

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
                        if _is_ignored_dir(root_path / d, include_ignored=False):
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

    elif name == "/plan":
        handle_plan(arg, messages, state)

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

    elif name == "/stackview":  handle_stackview(arg, cwd, messages, state)
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
