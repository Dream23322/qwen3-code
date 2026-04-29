"""/stackview - inspect runtime state (file history, sessions, env, tree)."""

import json
import os
import sys as _sys
from collections import defaultdict
from pathlib import Path

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP
from qwen3_code.settings import (
    CFG, SETTINGS_PATH, _model, _app_name, _assistant_name,
)
from qwen3_code.utils import (
    _short_cwd, VC_DIR, SESSION_DIR, SIZE_REDUCTION_THRESHOLD,
)
from qwen3_code.session import _session_path
from qwen3_code.vc import all_tracked_files, _load_vc
from qwen3_code.commands import Command, register
from qwen3_code.commands._helpers import (
    load_desc_cache,
    desc_cache_path,
    desc_context_block,
    build_rich_tree,
)


_SV_TYPES: dict[str, str] = {
    "fh":       "File history (current project)",
    "fhf":      "File history full (all projects)",
    "sessions": "Saved sessions",
    "env":      "Runtime environment info",
    "tree":     "Project tree (uses cached AI descriptions; run /v to generate)",
}


def _sv_fh(cwd: str) -> None:
    local: list[str] = [fp for fp in all_tracked_files() if fp.startswith(cwd)]
    if not local:
        console.print(f"[info]No tracked files under {cwd}.[/info]")
        return

    rows: list[str] = []
    for fp in local:
        idx        = _load_vc(fp)
        n:   int   = len(idx.get("commits", {}))
        hid: str   = idx.get("head", "-")
        hm:  str   = idx["commits"][hid]["message"][:40] if hid and hid in idx["commits"] else "(none)"
        rows.append(
            f"  {os.path.relpath(fp, cwd):<36}  "
            f"{'exists' if Path(fp).exists() else 'missing':<7}  "
            f"{n} commits  HEAD=[cyan]{hid}[/cyan] {hm}"
        )
    console.print(Panel("\n".join(rows), title=f"File history [{_short_cwd(cwd)}]", border_style=SAKURA))


def _sv_fhf() -> None:
    tracked: list[str] = all_tracked_files()
    if not tracked:
        console.print("[info]No tracked files.[/info]")
        return

    by_dir: dict[str, list[str]] = defaultdict(list)
    for fp in tracked:
        by_dir[str(Path(fp).parent)].append(fp)

    rows: list[str] = []
    for d in sorted(by_dir):
        rows.append(f"  [{d}]")
        for fp in sorted(by_dir[d]):
            n: int = len(_load_vc(fp).get("commits", {}))
            rows.append(
                f"    {Path(fp).name:<36}  "
                f"{'exists' if Path(fp).exists() else 'missing':<7}  {n} commits"
            )
    console.print(Panel("\n".join(rows), title="File history (all projects)", border_style=SAKURA))


def _sv_sessions() -> None:
    if not SESSION_DIR.exists() or not list(SESSION_DIR.glob("*.json")):
        console.print("[info]No sessions saved yet.[/info]")
        return

    rows: list[str] = []
    for sf in sorted(SESSION_DIR.glob("*.json")):
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            rows.append(
                f"  {data.get('cwd','?'):<45}  "
                f"{data.get('saved_at','?')[:19].replace('T','  ')}  "
                f"{len(data.get('messages',[]))} msg  {sf.stat().st_size} B"
            )
        except Exception:
            rows.append(f"  {sf.name}  (unreadable)")
    console.print(Panel("\n".join(rows), title=f"Saved sessions ({SESSION_DIR})", border_style=SAKURA))


def _sv_env(cwd: str, messages: list[dict]) -> None:
    sf     = _session_path(cwd)
    cached = load_desc_cache(cwd)
    rows: list[str] = [
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
        f"  Desc cache      : {desc_cache_path(cwd)}  ({len(cached)} entries)" if cached else f"  Desc cache      : (none \u2014 run /v to generate)",
        f"  Messages        : {len([m for m in messages if m['role'] != 'system'])}",
        f"  Python          : {_sys.version.split()[0]}  ({_sys.executable})",
    ]
    console.print(Panel("\n".join(rows), title="Environment", border_style=SAKURA_DEEP))


def _sv_tree(cwd: str, state: dict) -> None:
    cached = load_desc_cache(cwd)
    if cached is None:
        console.print(
            "[info]No cached descriptions for this directory.\n"
            "Run [bold]/v[/bold] or [bold]/loadtree -d[/bold] to generate and cache them.[/info]"
        )
        return

    tree       = build_rich_tree(cwd, include_ignored=False, descriptions=cached)
    ts:  str   = ""
    cp         = desc_cache_path(cwd)
    if cp.exists():
        try:
            meta = json.loads(cp.read_text(encoding="utf-8"))
            ts   = meta.get("_generated_at", "")[:19].replace("T", "  ")
        except Exception:
            pass

    title: str = f"Tree + AI descriptions [{_short_cwd(cwd)}]"
    if ts:
        title += f"  (cached {ts})"
    console.print(Panel(tree, title=title, border_style=SAKURA_DEEP))

    state.setdefault("pending_context", []).append(
        desc_context_block(cwd, cached)
    )
    console.print(f"[dim]Descriptions ({len(cached)} files) added to AI context.[/dim]")


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]
    t:   str = arg.strip().lower()

    if t == "fh":
        _sv_fh(cwd)
    elif t == "fhf":
        _sv_fhf()
    elif t in ("sessions", "sess"):
        _sv_sessions()
    elif t in ("env", "environment"):
        _sv_env(cwd, messages)
    elif t == "tree":
        _sv_tree(cwd, state)
    elif t in ("", "help"):
        rows: list[str] = [f"  {k:<12}  {v}" for k, v in _SV_TYPES.items()]
        console.print(Panel("\n".join(rows), title="/stackview types", border_style=SAKURA_DEEP))
    else:
        console.print(f"[error]Unknown type '{t}'. Run /stackview help.[/error]")


register(Command(
    name="/stackview",
    handler=_handler,
    usage="/stackview <type>",
    description="inspect state  [dim]fh / fhf / sessions / env / tree[/dim]",
    category="General",
))
