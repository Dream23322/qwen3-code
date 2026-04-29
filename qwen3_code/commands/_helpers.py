"""Shared helpers used by multiple slash-command handlers.

Description cache, file-walk filters, AI description generation, and the
rich/text tree builders live here so individual command modules stay small.
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from rich.markup import escape as _esc
from rich.panel import Panel
from rich.tree import Tree as RichTree

from qwen3_code.theme import console, SAKURA, SAKURA_MUTED
from qwen3_code.utils import IGNORED_DIRS
from qwen3_code.vc import all_tracked_files


# ---------------------------------------------------------------------------
# Description cache
# ---------------------------------------------------------------------------

_DESC_DIR: Path = Path.home() / ".local" / "share" / "qwen3-code" / "descriptions"


def desc_cache_path(cwd: str) -> Path:
    h: str = hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:16]

    return _DESC_DIR / f"{h}.json"


def load_desc_cache(cwd: str) -> dict[str, str] | None:
    p: Path = desc_cache_path(cwd)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("_cwd") != cwd:
            return None

        return {k: v for k, v in data.items() if not k.startswith("_")}

    except Exception:
        return None


def save_desc_cache(cwd: str, descriptions: dict[str, str]) -> None:
    _DESC_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {"_cwd": cwd, "_generated_at": datetime.now().isoformat()}
    data.update(descriptions)
    desc_cache_path(cwd).write_text(json.dumps(data, indent=2), encoding="utf-8")


def desc_context_block(cwd: str, descriptions: dict[str, str]) -> str:
    lines: list[str] = [f"Project file descriptions for `{cwd}`:", ""]
    for rel, desc in sorted(descriptions.items()):
        lines.append(f"  {rel}  \u2014  {desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File-description filters
# ---------------------------------------------------------------------------

# Binary / generated / lockfile-ish extensions where a one-line AI description
# would be useless and just waste a model call.
_DESC_SKIP_EXTS: set[str] = {
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tiff",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".mp3", ".mp4", ".mov", ".avi", ".webm", ".wav", ".flac",
    ".so", ".dll", ".dylib", ".o", ".a", ".class", ".jar",
    ".lock", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
}

_DESC_SKIP_NAMES: set[str] = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "Cargo.lock", "Pipfile.lock", "uv.lock",
    "composer.lock", "Gemfile.lock",
}

# Files larger than this are skipped (too big to summarise meaningfully
# from the first dozen lines, and slow to read repeatedly).
_DESC_MAX_BYTES: int = 200_000


def should_describe(rel: str, absp: str) -> bool:
    p: Path = Path(absp)
    if p.name in _DESC_SKIP_NAMES:
        return False
    if p.suffix.lower() in _DESC_SKIP_EXTS:
        return False
    if rel.endswith(".min.js") or rel.endswith(".min.css"):
        return False
    try:
        if p.stat().st_size > _DESC_MAX_BYTES:
            return False

    except Exception:
        return False

    return True


# ---------------------------------------------------------------------------
# Directory filters
# ---------------------------------------------------------------------------

def is_ignored_dir(entry: Path, include_ignored: bool) -> bool:
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
# Tree helpers
# ---------------------------------------------------------------------------

def collect_files_for_tree(
    root: str,
    include_ignored: bool = False,
) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for rdir, dirs, fnames in os.walk(root):
        rpath: Path = Path(rdir)
        dirs[:] = [
            d for d in sorted(dirs)
            if not is_ignored_dir(rpath / d, include_ignored)
        ]
        for fname in sorted(fnames):
            if not include_ignored and fname.startswith("."):
                continue
            fp: Path = rpath / fname
            results.append((os.path.relpath(str(fp), root), str(fp)))

    return results


def generate_file_descriptions_streamed(
    files: list[tuple[str, str]],
    cwd: str = "",
) -> dict[str, str]:
    """Describe every textual file under ``files`` (no hard cap).

    Behaviour:
      - Filters out binary / lockfile / oversized files up-front.
      - Accumulates into the existing description cache (so re-runs only
        re-describe what's missing or changed; nothing previously cached is
        thrown away).
      - Persists after every successful description, so Ctrl+C keeps every
        description finished so far.
    """
    import ollama
    from rich.live import Live
    from qwen3_code.settings import _model

    # Start from whatever's already cached so partial runs accumulate.
    result: dict[str, str] = dict(load_desc_cache(cwd) or {}) if cwd else {}

    targets:    list[tuple[str, str]] = [
        (rel, absp) for rel, absp in files if should_describe(rel, absp)
    ]
    skipped_n:   int  = len(files) - len(targets)
    total:       int  = len(targets)
    interrupted: bool = False

    if skipped_n:
        console.print(
            f"[dim]Skipping {skipped_n} non-textual / oversized file(s).[/dim]"
        )
    if not targets:
        return result

    try:
        for idx, (rel, absp) in enumerate(targets, 1):
            try:
                lines:   list[str] = Path(absp).read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines()[:12]
                snippet: str       = "\n".join(lines)

            except Exception:
                continue

            prompt: str = (
                "Give a single short description of this file (8 words max). "
                "Output ONLY the description \u2014 no filename, no prefix, no punctuation at the end.\n\n"
                f"File: {rel}\n{snippet}"
            )

            tokens:   list[str] = []
            progress: str       = f"[dim]({idx}/{total})[/dim]"

            def _panel(desc_so_far: str, _rel: str = rel, _prog: str = progress) -> Panel:
                body: str = (
                    f"{_prog}  [bold]{_esc(_rel)}[/bold]\n"
                    f"[dim]{_esc(desc_so_far) if desc_so_far else 'thinking\u2026'}[/dim]\n\n"
                    f"[dim]Ctrl+C to stop and keep partial descriptions.[/dim]"
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
                        token: str = chunk["message"]["content"]
                        tokens.append(token)
                        live.update(_panel("".join(tokens).strip()))

            except KeyboardInterrupt:
                interrupted = True
                break
            except Exception:
                continue

            desc: str = (
                "".join(tokens).strip().splitlines()[0].strip() if tokens else ""
            )
            if desc:
                result[rel] = desc
                # Persist after every successful description so a crash or
                # Ctrl+C never throws progress away.
                if cwd:
                    save_desc_cache(cwd, result)

    except KeyboardInterrupt:
        interrupted = True

    if cwd and result:
        save_desc_cache(cwd, result)
        if interrupted:
            console.print(
                f"[dim]Interrupted. Cached {len(result)} description(s) so far. "
                f"Re-run [bold]/v[/bold] to fill in the rest.[/dim]"
            )
        else:
            console.print(f"[dim]Descriptions cached ({len(result)} files).[/dim]")

    return result


def build_rich_tree(
    root: str,
    include_ignored: bool = False,
    descriptions: dict[str, str] | None = None,
) -> RichTree:
    tracked: set[str] = set(all_tracked_files())

    def _add_children(node: RichTree, path: Path) -> None:
        try:
            entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return

        ignored_names: list[str] = []
        for entry in entries:
            if entry.is_dir() and is_ignored_dir(entry, include_ignored):
                ignored_names.append(entry.name)
                continue
            if entry.is_dir():
                branch = node.add(f"[bold]{entry.name}/[/bold]")
                _add_children(branch, entry)
            else:
                if not include_ignored and entry.name.startswith("."):
                    continue
                rel:         str  = os.path.relpath(str(entry), root)
                is_tracked:  bool = str(entry.resolve()) in tracked
                desc_part:   str  = ""
                if descriptions and rel in descriptions:
                    desc_part = f"  [dim]\u2014 {_esc(descriptions[rel])}[/dim]"
                if is_tracked:
                    node.add(f"[{SAKURA}]{entry.name}[/{SAKURA}] [dim](tracked)[/dim]{desc_part}")
                else:
                    node.add(f"[dim]{entry.name}[/dim]{desc_part}")

        if ignored_names:
            node.add(
                f"[dim italic]\u2026 {len(ignored_names)} ignored dir(s): "
                f"{', '.join(ignored_names)}[/dim italic]"
            )

    root_label: str = f"[bold]{Path(root).name}/[/bold]"
    tree = RichTree(root_label)
    _add_children(tree, Path(root))

    return tree


def build_text_tree(
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

        filtered:      list[Path] = []
        ignored_names: list[str]  = []
        for e in entries:
            if e.is_dir() and is_ignored_dir(e, include_ignored):
                ignored_names.append(e.name)
                continue
            if not include_ignored and e.is_file() and e.name.startswith("."):
                continue
            filtered.append(e)

        for i, entry in enumerate(filtered):
            is_last: bool = (i == len(filtered) - 1) and not ignored_names
            conn:    str  = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
            if entry.is_dir():
                lines.append(prefix + conn + entry.name + "/")
                _walk(entry, prefix + ("    " if is_last else "\u2502   "))
            else:
                rel:         str = os.path.relpath(str(entry), root)
                desc_suffix: str = (
                    f"  \u2014 {descriptions[rel]}" if descriptions and rel in descriptions else ""
                )
                lines.append(prefix + conn + entry.name + desc_suffix)

        if ignored_names:
            lines.append(
                prefix + "\u2514\u2500\u2500 ["
                + f"{len(ignored_names)} ignored: " + ", ".join(ignored_names) + "]"
            )

    _walk(Path(root), "")

    return "\n".join(lines)
