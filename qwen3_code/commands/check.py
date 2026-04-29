"""/check - AI code review (ALL | <file> | <file>:<func>)."""

import os
import re
from pathlib import Path

from qwen3_code.theme import console
from qwen3_code.utils import resolve_path, read_file, IGNORED_DIRS
from qwen3_code.session import save_session
from qwen3_code.renderer import stream_response
from qwen3_code.commands import Command, register
from qwen3_code.commands._helpers import is_ignored_dir


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


def _build_prompt_all(cwd: str) -> str | None:
    source_files: list[Path] = [
        f for f in Path(cwd).rglob("*")
        if f.is_file() and f.suffix.lower() in _CODE_EXTENSIONS
        and not any(p.startswith(".") for p in f.parts)
        and not any(p in IGNORED_DIRS for p in f.parts)
        and not any(
            is_ignored_dir(Path(*f.parts[:i+1]), False)
            for i in range(1, len(f.parts))
            if Path(*f.parts[:i+1]).is_dir()
        )
    ]
    if not source_files:
        console.print(f"[info]No source files found in {cwd}.[/info]")
        return None

    parts:   list[str] = []
    total:   int       = 0
    skipped: list[str] = []
    for sf in sorted(source_files):
        rel: str = os.path.relpath(str(sf), cwd)
        try:
            content: str = sf.read_text(encoding="utf-8", errors="replace")

        except Exception:
            continue
        if total + len(content) > 400_000:
            skipped.append(rel)
            continue
        parts.append(f"# -- {rel} --\n{content}")
        total += len(content)

    if skipped:
        console.print(
            f"[info]Skipped: {', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''}[/info]"
        )

    return (
        f"Review {len(parts)} file(s) in {cwd} for bugs, logic errors, bad practices, "
        f"and security issues. State file, severity, description, fix for each issue.\n\n"
        + "\n\n".join(parts)
    )


def _build_prompt_function(arg: str, cwd: str) -> str | None:
    lc: int = arg.rfind(":")
    fp: str = resolve_path(arg[:lc], cwd)
    fn: str = arg[lc+1:].strip().rstrip("()")
    if not Path(fp).exists():
        console.print(f"[error]File not found: {fp}[/error]")
        return None

    src:  str        = read_file(fp)
    fsrc: str | None = _extract_function(src, fn)
    if fsrc is None:
        console.print(f"[error]Function '{fn}' not found. Using full file.[/error]")
        fsrc = src

    return f"Review function `{fn}` in `{Path(fp).name}` for bugs and issues.\n\n```\n{fsrc}\n```"


def _build_prompt_file(arg: str, cwd: str) -> str | None:
    fp: str = resolve_path(arg, cwd)
    if not Path(fp).exists():
        console.print(f"[error]File not found: {fp}[/error]")
        return None

    return f"Review `{Path(fp).name}` for bugs and issues.\n\n```\n{read_file(fp)}\n```"


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]
    arg      = arg.strip()
    if not arg:
        console.print("[error]Usage: /check ALL | <file> | <file>:<func>[/error]")
        return

    if arg.upper() == "ALL":
        prompt = _build_prompt_all(cwd)
    elif ":" in arg and not arg.startswith(":"):
        prompt = _build_prompt_function(arg, cwd)
    else:
        prompt = _build_prompt_file(arg, cwd)

    if prompt is None:
        return

    messages.append({"role": "user", "content": prompt})
    reply = stream_response(messages)
    if reply:
        messages.append({"role": "assistant", "content": reply})
        save_session(cwd, messages)


register(Command(
    name="/check",
    handler=_handler,
    usage="/check <target>",
    description="AI code review  [dim]ALL | file | file:func[/dim]",
    category="General",
))
