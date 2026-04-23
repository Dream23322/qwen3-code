"""Partial-write detection, file-write application, RUN/INSERT-marker handling, and READ-request handling."""

import re
from pathlib import Path

from rich.markup import escape as _esc
from rich.panel import Panel

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.utils import ConsoleSession
from qwen3_code.vc import write_file_with_vc

# ---------------------------------------------------------------------------
# Partial-write detection
# ---------------------------------------------------------------------------

_PARTIAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*\.{3}\s*$",                       re.MULTILINE),
    re.compile(r"^\s*#\s*\.{3}\s*$",                   re.MULTILINE),
    re.compile(r"^\s*//\s*\.{3}\s*$",                  re.MULTILINE),
    re.compile(r"#\s*(rest|remainder|remaining)\s+of",  re.IGNORECASE),
    re.compile(r"#\s*\.\.\.\.*",                        re.IGNORECASE),
    re.compile(r"//\s*\.\.\.\.*",                       re.IGNORECASE),
    re.compile(r"\[\s*previous\s+(code|content)",       re.IGNORECASE),
    re.compile(r"\[\s*rest\s+of\s+(the\s+)?code",       re.IGNORECASE),
    re.compile(r"# same as before",                     re.IGNORECASE),
    re.compile(r"# unchanged",                          re.IGNORECASE),
    re.compile(r"# \(omitted\)",                        re.IGNORECASE),
]

_WRITE_PATTERN: re.Pattern = re.compile(
    r"<!--\s*WRITE:\s*(?P<path>[^\s>]+)\s*-->\s*```(?:\w+)?\n(?P<code>.*?)```",
    re.DOTALL,
)

_INSERT_PATTERN: re.Pattern = re.compile(
    r"<!--\s*INSERT:\s*(?P<path>[^\s>:]+):(?P<line>\d+)\s*-->\s*```(?:\w+)?\n(?P<code>.*?)```",
    re.DOTALL,
)

_RUN_PATTERN: re.Pattern = re.compile(r"<!--\s*RUN:\s*(?P<cmd>[^>]+?)\s*-->", re.DOTALL)

# ---------------------------------------------------------------------------
# READ request markers  (AI → tool)
# ---------------------------------------------------------------------------

_READ_REQUEST_RE: re.Pattern = re.compile(
    r"<!--\s*READ:\s*(?P<path>[^\s>]+)\s*-->"
)


def has_read_requests(reply: str) -> bool:
    return bool(_READ_REQUEST_RE.search(reply))


def collect_read_requests(reply: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for m in _READ_REQUEST_RE.finditer(reply):
        p = m.group("path").strip()
        if p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


def has_inserts(reply: str) -> bool:
    return bool(_INSERT_PATTERN.search(reply))


# ---------------------------------------------------------------------------
# Partial-write helpers
# ---------------------------------------------------------------------------

def reply_has_partial_write(reply: str) -> bool:
    for m in _WRITE_PATTERN.finditer(reply):
        for pat in _PARTIAL_PATTERNS:
            if pat.search(m.group("code")):
                return True
    return False


def apply_file_writes(reply: str) -> None:
    for m in _WRITE_PATTERN.finditer(reply):
        write_file_with_vc(m.group("path").strip(), m.group("code"))


# ---------------------------------------------------------------------------
# INSERT marker helpers
# ---------------------------------------------------------------------------

def _verify_syntax(content: str, path: str) -> tuple[bool, str]:
    """Check syntax of *content* as if it were *path*. Returns (ok, message)."""
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        import py_compile, tempfile, os
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp = f.name
        try:
            py_compile.compile(tmp, doraise=True)
            return True, "Python syntax OK"
        except py_compile.PyCompileError as exc:
            return False, str(exc)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    elif suffix in (".js", ".ts", ".jsx", ".tsx"):
        depth = 0
        for ch in content:
            if ch == "{": depth += 1
            elif ch == "}": depth -= 1
            if depth < 0:
                return False, "Unbalanced braces (extra '}')"
        if depth != 0:
            return False, f"Unbalanced braces ({depth} unclosed '{{')"
        return True, "Brace balance OK"
    return True, "(no syntax check for this file type)"


def _insertion_preview(
    original_lines: list[str],
    insert_lines:   list[str],
    line_no:        int,
) -> str:
    """Return a coloured diff-style preview string around the insertion point."""
    CTX   = 3
    start = max(0, line_no - 1 - CTX)
    out:  list[str] = []

    # Lines before insertion
    for i in range(start, min(line_no - 1, len(original_lines))):
        out.append(f"  {i + 1:>5}  {_esc(original_lines[i].rstrip())}")

    # Inserted lines (highlighted)
    for j, l in enumerate(insert_lines):
        out.append(f"[green]+{line_no + j:>5}  {_esc(l.rstrip())}[/green]")

    # Lines after insertion
    after_start = line_no - 1
    for i in range(after_start, min(after_start + CTX, len(original_lines))):
        out.append(f"  {i + 1 + len(insert_lines):>5}  {_esc(original_lines[i].rstrip())}")

    return "\n".join(out)


def apply_file_inserts(reply: str, cwd: str = "") -> None:
    """Process <!-- INSERT: path:LINE --> markers in *reply*.

    When the 'insert_verify' setting is True (default):
      - Checks syntax of the resulting file.
      - Shows a preview diff around the insertion point.
      - Asks the user to confirm before writing.

    When 'insert_verify' is False the insertion is applied silently.
    """
    from qwen3_code.settings import CFG
    from qwen3_code.utils import resolve_path, read_file

    verify: bool = bool(CFG.get("insert_verify", True))

    for m in _INSERT_PATTERN.finditer(reply):
        raw_path  = m.group("path").strip()
        line_no   = int(m.group("line"))
        new_code  = m.group("code")

        abs_path  = resolve_path(raw_path, cwd) if cwd else raw_path
        path_obj  = Path(abs_path)

        # --- Read existing file (or start empty) ---
        if path_obj.exists():
            original = path_obj.read_text(encoding="utf-8", errors="replace")
        else:
            console.print(f"[info]INSERT: file does not exist yet, creating {raw_path}[/info]")
            original = ""

        original_lines = original.splitlines(keepends=True)
        insert_lines   = new_code.splitlines(keepends=True)
        # Ensure trailing newline on each inserted line
        insert_lines   = [
            (l if l.endswith("\n") else l + "\n") for l in insert_lines
        ]

        # Clamp line_no to [1, len+1]
        line_no = max(1, min(line_no, len(original_lines) + 1))

        # Build the resulting content
        result_lines = (
            original_lines[: line_no - 1]
            + insert_lines
            + original_lines[line_no - 1 :]
        )
        result = "".join(result_lines)

        if verify:
            # --- Syntax check ---
            ok, msg = _verify_syntax(result, abs_path)

            # --- Preview ---
            plain_insert = [l.rstrip("\n") for l in insert_lines]
            plain_orig   = [l.rstrip("\n") for l in original_lines]
            preview_str  = _insertion_preview(plain_orig, plain_insert, line_no)

            status_colour = "green" if ok else "red"
            status_label  = msg
            panel_body = (
                f"[bold]File:[/bold] {raw_path}   "
                f"[bold]Insert before line:[/bold] {line_no}   "
                f"[{status_colour}]{status_label}[/{status_colour}]\n\n"
                + preview_str
            )
            console.print(Panel(
                panel_body,
                title="INSERT preview",
                border_style=SAKURA_DARK if not ok else SAKURA_DEEP,
            ))

            if not ok:
                console.print(
                    "[error]Syntax verification failed. "
                    "Apply anyway? This may break the file.[/error]"
                )

            try:
                answer = input("Apply insert? [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("[info]Insert skipped.[/info]")
                continue
            if answer not in ("y", "yes"):
                console.print("[info]Insert skipped.[/info]")
                continue

        write_file_with_vc(abs_path, result)
        console.print(
            f"[info]Inserted {len(insert_lines)} line(s) into "
            f"[bold]{raw_path}[/bold] before line {line_no}.[/info]"
        )


def apply_command_runs(reply: str, cwd: str, messages: list[dict]) -> None:
    matches = list(_RUN_PATTERN.finditer(reply))
    if not matches:
        return

    session = ConsoleSession()

    for m in matches:
        cmd = m.group("cmd").strip()
        if not cmd:
            continue
        console.print(Panel(
            f"[bold]The assistant wants to run:[/bold]\n  [bold]{cmd}[/bold]\n\n[info]CWD: {cwd}[/info]",
            title="Permission required", border_style=SAKURA_DARK,
        ))
        try:
            answer = input("Allow? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("[info]Skipped.[/info]")
            continue
        if answer not in ("y", "yes"):
            console.print("[info]Command skipped.[/info]")
            messages.append({"role": "user", "content": f"[Command `{cmd}` was denied.]"})
            continue
        output = session.run(cmd, cwd)
        messages.append({"role": "user", "content": f"[Command `{cmd}` output:]\n```\n{output}\n```"})

    session.print_summary()
