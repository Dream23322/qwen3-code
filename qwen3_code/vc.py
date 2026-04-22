"""Git-like version control: commits, snapshots, undo/redo, checkout."""

import difflib
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import ollama
from rich.panel import Panel
from rich.syntax import Syntax

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.utils import VC_DIR, SIZE_REDUCTION_THRESHOLD, spinning_dots
from qwen3_code.settings import _model

# ---------------------------------------------------------------------------
# Internal index cache
# ---------------------------------------------------------------------------

_VC_CACHE: dict[str, dict] = {}


def _vc_slot(filepath: str) -> Path:
    safe = re.sub(r"[^\w.\-]", "_", str(Path(filepath).resolve()))[-48:]
    slot = VC_DIR / safe
    slot.mkdir(parents=True, exist_ok=True)
    return slot


def _vc_index_path(filepath: str) -> Path:
    return _vc_slot(filepath) / "index.json"


def _load_vc(filepath: str) -> dict:
    if filepath in _VC_CACHE:
        return _VC_CACHE[filepath]
    p = _vc_index_path(filepath)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            _VC_CACHE[filepath] = data
            return data
        except Exception:
            pass
    idx = {"filepath": filepath, "commits": {}, "head": None, "root": None}
    _VC_CACHE[filepath] = idx
    return idx


def _save_vc(filepath: str) -> None:
    _vc_index_path(filepath).write_text(json.dumps(_load_vc(filepath), indent=2), encoding="utf-8")


def _vc_snapshot(filepath: str, content: str) -> Path:
    slot = _vc_slot(filepath)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak  = slot / f"{ts}.bak"
    bak.write_text(content, encoding="utf-8")
    return bak


def _make_commit_id(filepath: str, content: str, ts: str) -> str:
    raw = f"{filepath}:{ts}:{len(content)}:{content[:128]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:7]


# ---------------------------------------------------------------------------
# Commit-message generation
# ---------------------------------------------------------------------------

def _generate_commit_message(filepath: str, old_content: str, new_content: str) -> str:
    fname = Path(filepath).name
    diff  = list(difflib.unified_diff(old_content.splitlines(), new_content.splitlines(), lineterm="", n=2))
    added   = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++ "))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("--- "))

    if not diff:
        return f"No-op ({fname})"

    snippet = "\n".join(diff[:40])
    prompt  = (
        f"Write a concise git commit message (imperative mood, max 60 chars) for this change "
        f"to `{fname}`. Reply with ONLY the message text.\n\n```diff\n{snippet}\n```"
    )

    result: dict = {"msg": None}

    def _call() -> None:
        try:
            resp = ollama.chat(model=_model(), messages=[{"role": "user", "content": prompt}], stream=False)
            result["msg"] = resp["message"]["content"].strip().split("\n")[0].strip("\"'`")[:72]
        except Exception:
            pass

    with spinning_dots("Generating commit message"):
        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join()

    if result["msg"]:
        console.print(f"  [dim]commit:[/dim] [bold]{result['msg']}[/bold]")
        return result["msg"]
    fallback = f"Update {fname} (+{added}/-{removed} lines)"
    console.print(f"  [dim]{fallback}[/dim]")
    return fallback


# ---------------------------------------------------------------------------
# Core commit
# ---------------------------------------------------------------------------

def vc_commit(filepath: str, new_content: str, message: str | None = None) -> str:
    idx = _load_vc(filepath)
    ts  = datetime.now().isoformat()
    cid = _make_commit_id(filepath, new_content, ts)
    snap = _vc_snapshot(filepath, new_content)

    parent_id = idx.get("head")
    if message is None:
        old = ""
        if parent_id and parent_id in idx["commits"]:
            try:
                old = Path(idx["commits"][parent_id]["snapshot"]).read_text(encoding="utf-8")
            except Exception:
                pass
        message = _generate_commit_message(filepath, old, new_content)

    commit = {"id": cid, "message": message, "timestamp": ts,
              "parent_id": parent_id, "snapshot": str(snap), "children": []}

    if parent_id and parent_id in idx["commits"]:
        if cid not in idx["commits"][parent_id]["children"]:
            idx["commits"][parent_id]["children"].append(cid)

    idx["commits"][cid] = commit
    idx["head"]          = cid
    if not idx.get("root") or not parent_id:
        idx["root"] = cid

    _save_vc(filepath)
    return cid


def vc_baseline(filepath: str) -> None:
    """Create an initial commit for *filepath* if none exists yet."""
    resolved = str(Path(filepath).resolve())
    if _load_vc(resolved).get("head"):
        return
    try:
        content = Path(resolved).read_text(encoding="utf-8")
    except Exception:
        return
    vc_commit(resolved, content, "Baseline (pre-edit)")
    console.print(f"[info]Baseline snapshot saved for [bold]{Path(resolved).name}[/bold][/info]")


# ---------------------------------------------------------------------------
# Size-reduction guard
# ---------------------------------------------------------------------------

def confirm_size_reduction(filepath: str, old: str, new: str, reduction: float) -> bool:
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     fromfile="current", tofile="proposed", lineterm="", n=3))
    console.print(Panel(
        f"[bold yellow]\u26a0  Large size reduction[/bold yellow]\n\n"
        f"  File    : [bold]{filepath}[/bold]\n"
        f"  Before  : {len(old):,} chars  ({len(old.splitlines())} lines)\n"
        f"  After   : {len(new):,} chars  ({len(new.splitlines())} lines)\n"
        f"  Reduced : [bold red]{reduction*100:.1f}%[/bold red]",
        title="Write guard", border_style=SAKURA_DARK,
    ))
    if diff:
        snippet = "\n".join(diff[:80]) + (f"\n\u2026 ({len(diff)-80} more)" if len(diff) > 80 else "")
        console.print(Panel(Syntax(snippet, "diff", theme="dracula"),
                            title="Diff (current \u2192 proposed)", border_style=SAKURA_MUTED))
    try:
        return input("Write anyway? [y/N] ").strip().lower() in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        console.print("[info]Write cancelled.[/info]")
        return False


# ---------------------------------------------------------------------------
# File writer
# ---------------------------------------------------------------------------

def write_file_with_vc(filepath: str, new_content: str, commit_message: str | None = None) -> None:
    resolved = str(Path(filepath).resolve())

    if Path(resolved).exists() and not _load_vc(resolved).get("head"):
        vc_baseline(resolved)

    if Path(resolved).exists():
        try:
            existing = Path(resolved).read_text(encoding="utf-8")
            if len(existing) > 0:
                reduction = (len(existing) - len(new_content)) / len(existing)
                if reduction > SIZE_REDUCTION_THRESHOLD:
                    if not confirm_size_reduction(resolved, existing, new_content, reduction):
                        console.print("[info]Write cancelled.[/info]")
                        return
        except Exception:
            pass

    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    Path(filepath).write_text(new_content, encoding="utf-8")

    cid = vc_commit(resolved, new_content, commit_message)
    idx = _load_vc(resolved)
    console.print(Panel(
        f"[info]Wrote [bold]{len(new_content.splitlines())}[/bold] lines to [bold]{filepath}[/bold]\n"
        f"Commit [bold cyan]{cid}[/bold cyan]  {idx['commits'][cid]['message']}[/info]",
        title="File written", border_style=SAKURA_DEEP,
    ))


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

def _resolve_commit(filepath: str, id_prefix: str) -> str | None:
    idx = _load_vc(filepath)
    commits = idx.get("commits", {})
    if id_prefix in commits:
        return id_prefix
    matches = [c for c in commits if c.startswith(id_prefix)]
    if len(matches) == 1:
        return matches[0]
    msg = f"Ambiguous prefix '{id_prefix}': {matches}" if matches else f"Commit '{id_prefix}' not found"
    console.print(f"[error]{msg}[/error]")
    return None


def do_undo(filepath: str) -> None:
    filepath = str(Path(filepath).resolve())
    idx = _load_vc(filepath)
    head_id = idx.get("head")
    if not head_id or head_id not in idx["commits"]:
        console.print(f"[error]No commits for {filepath}[/error]")
        return
    parent_id = idx["commits"][head_id].get("parent_id")
    if not parent_id:
        console.print("[error]Already at root commit.[/error]")
        return
    parent = idx["commits"][parent_id]
    try:
        Path(filepath).write_text(Path(parent["snapshot"]).read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as exc:
        console.print(f"[error]Snapshot missing: {exc}[/error]")
        return
    idx["head"] = parent_id
    _save_vc(filepath)
    console.print(Panel(
        f"[info]HEAD \u2192 [bold cyan]{parent_id}[/bold cyan]\n"
        f"{parent['message']}  [dim]{parent['timestamp'][:19]}[/dim][/info]",
        title="Undo", border_style=SAKURA,
    ))


def do_redo(filepath: str, target_id: str | None = None) -> None:
    filepath = str(Path(filepath).resolve())
    idx = _load_vc(filepath)
    head_id = idx.get("head")
    if not head_id or head_id not in idx["commits"]:
        console.print(f"[error]No commits for {filepath}[/error]")
        return
    children = [c for c in idx["commits"][head_id].get("children", []) if c in idx["commits"]]
    if not children:
        console.print("[error]Already at tip of this branch.[/error]")
        return
    if target_id:
        full = _resolve_commit(filepath, target_id)
        if not full or full not in children:
            console.print(f"[error]{target_id} is not a direct child of HEAD.[/error]")
            return
        child_id = full
    elif len(children) == 1:
        child_id = children[0]
    else:
        rows = [f"  [bold cyan]{c}[/bold cyan]  {idx['commits'][c]['message']}" for c in children]
        console.print(Panel("[info]Multiple branches:\n\n" + "\n".join(rows) + "\n\nUse /redo <file> <id>[/info]",
                            title="Branch", border_style=SAKURA_DEEP))
        return
    child = idx["commits"][child_id]
    try:
        Path(filepath).write_text(Path(child["snapshot"]).read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as exc:
        console.print(f"[error]Snapshot missing: {exc}[/error]")
        return
    idx["head"] = child_id
    _save_vc(filepath)
    console.print(Panel(
        f"[info]HEAD \u2192 [bold cyan]{child_id}[/bold cyan]\n"
        f"{child['message']}  [dim]{child['timestamp'][:19]}[/dim][/info]",
        title="Redo", border_style=SAKURA,
    ))


def do_checkout(filepath: str, id_prefix: str) -> None:
    filepath = str(Path(filepath).resolve())
    full = _resolve_commit(filepath, id_prefix)
    if not full:
        return
    idx    = _load_vc(filepath)
    commit = idx["commits"][full]
    try:
        Path(filepath).write_text(Path(commit["snapshot"]).read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as exc:
        console.print(f"[error]Snapshot missing: {exc}[/error]")
        return
    idx["head"] = full
    _save_vc(filepath)
    console.print(Panel(
        f"[info]Checked out [bold cyan]{full}[/bold cyan]\n"
        f"{commit['message']}  [dim]{commit['timestamp'][:19]}[/dim][/info]",
        title="Checkout", border_style=SAKURA,
    ))


def do_manual_commit(filepath: str, message: str) -> None:
    filepath = str(Path(filepath).resolve())
    if not Path(filepath).exists():
        console.print(f"[error]File not found: {filepath}[/error]")
        return
    content = Path(filepath).read_text(encoding="utf-8")
    cid = vc_commit(filepath, content, message or None)
    idx = _load_vc(filepath)
    console.print(Panel(
        f"[info][bold]{filepath}[/bold]\n[bold cyan]{cid}[/bold cyan]  {idx['commits'][cid]['message']}[/info]",
        title="Commit", border_style=SAKURA_DEEP,
    ))


def show_log(filepath: str) -> None:
    filepath = str(Path(filepath).resolve())
    idx      = _load_vc(filepath)
    commits  = idx.get("commits", {})
    head_id  = idx.get("head")
    root_id  = idx.get("root")
    if not commits:
        console.print(f"[info]No commits for {filepath}.[/info]")
        return
    lines: list[str] = []

    def _walk(cid: str, prefix: str, is_last: bool) -> None:
        c = commits.get(cid, {})
        if not c:
            return
        conn     = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
        head_tag = "  [bold yellow](HEAD)[/bold yellow]" if cid == head_id else ""
        lines.append(f"{prefix}{conn}[bold cyan]{cid}[/bold cyan]{head_tag}  {c.get('message','?')}  [dim]{c.get('timestamp','')[:19]}[/dim]")
        child_prefix = prefix + ("    " if is_last else "\u2502   ")
        children = [ch for ch in c.get("children", []) if ch in commits]
        for i, ch in enumerate(children):
            _walk(ch, child_prefix, i == len(children) - 1)

    if root_id and root_id in commits:
        _walk(root_id, "", True)
    console.print(Panel("\n".join(lines), title=f"Commit tree: {Path(filepath).name}", border_style=SAKURA))


def all_tracked_files() -> list[str]:
    result: list[str] = []
    if not VC_DIR.exists():
        return result
    for slot in sorted(VC_DIR.iterdir()):
        idx_path = slot / "index.json"
        if idx_path.exists():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
                fp   = data.get("filepath", "")
                if fp:
                    result.append(fp)
            except Exception:
                pass
    return result
