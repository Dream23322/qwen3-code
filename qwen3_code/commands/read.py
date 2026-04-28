"""/read - load file(s) into AI context (saves baseline snapshot)."""

import os
from pathlib import Path

from qwen3_code.theme import console
from qwen3_code.utils import resolve_path, read_file
from qwen3_code.vc import _load_vc, vc_commit
from qwen3_code.commands import Command, register
from qwen3_code.commands._helpers import is_ignored_dir


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]
    if not arg:
        console.print("[error]Usage: /read <filepath> | /read -a[/error]")
        return

    if arg.strip() == "-a":
        _read_all(cwd, state)
        return

    resolved: str = resolve_path(arg, cwd)
    content:  str = read_file(resolved)
    if Path(resolved).exists():
        ra: str = str(Path(resolved).resolve())
        if not _load_vc(ra).get("head"):
            vc_commit(ra, content, "Baseline (pre-edit)")
            console.print(
                f"[info]Baseline saved for [bold]{Path(resolved).name}[/bold][/info]"
            )

    state.setdefault("pending_context", []).append(
        f"Here is the content of `{resolved}`:\n\n```\n{content}\n```"
    )
    console.print(f"[info]Loaded {resolved} into context.[/info]")


def _read_all(cwd: str, state: dict) -> None:
    ignored_found: set[str]   = set()
    all_files:     list[Path] = []
    try:
        for root_str, dirs, files in os.walk(cwd):
            root_path: Path      = Path(root_str)
            to_remove: list[str] = []
            for d in dirs:
                if is_ignored_dir(root_path / d, include_ignored=False):
                    ignored_found.add(d)
                    to_remove.append(d)
            for d in to_remove:
                dirs.remove(d)
            for fname in files:
                if not fname.startswith("."):
                    all_files.append(root_path / fname)

    except Exception as exc:
        console.print(f"[error]{exc}[/error]")
        return

    snippets:  list[str] = []
    total:     int       = 0
    skipped:   list[str] = []
    baselined: int       = 0
    for sf in sorted(all_files):
        rel: str = os.path.relpath(str(sf), cwd)
        try:
            fc: str = sf.read_text(encoding="utf-8", errors="replace")

        except Exception:
            skipped.append(rel)
            continue
        if total + len(fc) > 400_000:
            skipped.append(rel)
            continue

        rf: str = str(sf.resolve())
        if not _load_vc(rf).get("head"):
            vc_commit(rf, fc, "Baseline (pre-edit)")
            baselined += 1
        snippets.append(f"### {rel}\n```\n{fc}\n```")
        total += len(fc)

    if skipped:
        console.print(
            "[info]Skipped: " + ", ".join(skipped[:5])
            + ("..." if len(skipped) > 5 else "") + "[/info]"
        )
    if not snippets:
        console.print("[info]No readable files found.[/info]")
        return

    ignored_note: str = ""
    if ignored_found:
        dirs_list: str = ", ".join(f"{d}/" for d in sorted(ignored_found))
        ignored_note = (
            f"\n\n[Note: the following directories exist but were not loaded "
            f"(dependencies / generated / VCS): {dirs_list}]"
        )

    state.setdefault("pending_context", []).append(
        f"Here are all {len(snippets)} file(s) from `{cwd}`:\n\n"
        + "\n\n".join(snippets)
        + ignored_note
    )
    msg: str = f"[info]Loaded {len(snippets)} file(s)"
    if baselined:
        msg += f", {baselined} baseline(s) saved"
    if ignored_found:
        msg += f"  [dim](noted {len(ignored_found)} ignored dir(s))[/dim]"
    console.print(msg + ".[/info]")


register(Command(
    name="/read",
    handler=_handler,
    usage="/read <file> | /read -a",
    description="load file(s) into context  [dim](saves baseline snapshot)[/dim]",
    category="General",
))
