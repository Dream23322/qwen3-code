"""Detection and application of AI-emitted action blocks.

The assistant communicates structured side-effects through XML-style tags:

    <qwrite path="..." lang="...">  ... full file ...  </qwrite>
    <qinsert path="..." line="N" lang="...">  ... lines ...  </qinsert>
    <qcode lang="...">  ... display-only code ...  </qcode>
    <qread path="..." />
    <qrun>shell command</qrun>

Legacy markers (HTML comments paired with markdown fences) are still parsed
for backwards compatibility with old sessions:

    <!-- WRITE: path -->```lang\n...```
    <!-- INSERT: path:LINE -->```lang\n...```
    <!-- READ: path -->
    <!-- RUN: cmd -->
"""

import re
from pathlib import Path
from typing import Iterator

from rich.markup import escape as _esc
from rich.panel import Panel

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.utils import ConsoleSession
from qwen3_code.vc import write_file_with_vc

# ---------------------------------------------------------------------------
# Partial-write detection (truncation markers inside a write payload)
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

# ---------------------------------------------------------------------------
# Block patterns -- new XML-style tags (preferred)
# ---------------------------------------------------------------------------

QWRITE_RE: re.Pattern = re.compile(
    r"<qwrite\s+(?P<attrs>[^>]*?)>\s*\n?(?P<code>.*?)</qwrite>",
    re.DOTALL | re.IGNORECASE,
)

QINSERT_RE: re.Pattern = re.compile(
    r"<qinsert\s+(?P<attrs>[^>]*?)>\s*\n?(?P<code>.*?)</qinsert>",
    re.DOTALL | re.IGNORECASE,
)

QCODE_RE: re.Pattern = re.compile(
    r"<qcode(?:\s+(?P<attrs>[^>]*?))?\s*>\s*\n?(?P<code>.*?)</qcode>",
    re.DOTALL | re.IGNORECASE,
)

QREAD_RE: re.Pattern = re.compile(
    r"<qread\s+(?P<attrs>[^/>]*?)\s*/\s*>",
    re.IGNORECASE,
)

QRUN_RE: re.Pattern = re.compile(
    r"<qrun\s*>(?P<cmd>.*?)</qrun>",
    re.DOTALL | re.IGNORECASE,
)

# Attribute parser: handles double-quoted, single-quoted, or bare values.
_ATTR_RE: re.Pattern = re.compile(
    r"(?P<key>\w+)\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s>]+))",
)


def parse_attrs(attrs: str | None) -> dict[str, str]:
    """Parse `key="value" key='value' key=value` style attribute strings."""
    out: dict[str, str] = {}
    if not attrs:
        return out
    for m in _ATTR_RE.finditer(attrs):
        out[m.group("key").lower()] = (
            m.group("dq") or m.group("sq") or m.group("bare") or ""
        )

    return out


# ---------------------------------------------------------------------------
# Block patterns -- legacy HTML-comment + markdown-fence markers
# ---------------------------------------------------------------------------

_LEGACY_WRITE_RE: re.Pattern = re.compile(
    r"<!--\s*WRITE:\s*(?P<path>[^\s>]+)\s*-->\s*```(?:\w+)?\n(?P<code>.*?)```",
    re.DOTALL,
)

_LEGACY_INSERT_RE: re.Pattern = re.compile(
    r"<!--\s*INSERT:\s*(?P<path>[^\s>:]+):(?P<line>\d+)\s*-->\s*```(?:\w+)?\n(?P<code>.*?)```",
    re.DOTALL,
)

_LEGACY_RUN_RE: re.Pattern = re.compile(
    r"<!--\s*RUN:\s*(?P<cmd>[^>]+?)\s*-->",
    re.DOTALL,
)

_LEGACY_READ_RE: re.Pattern = re.compile(
    r"<!--\s*READ:\s*(?P<path>[^\s>]+)\s*-->",
)

# Public aliases preserved for backwards compatibility with any external
# callers that imported these names directly.
_WRITE_PATTERN  = _LEGACY_WRITE_RE
_INSERT_PATTERN = _LEGACY_INSERT_RE
_RUN_PATTERN    = _LEGACY_RUN_RE
_READ_REQUEST_RE = _LEGACY_READ_RE


# ---------------------------------------------------------------------------
# Unified iteration over both new tags and legacy markers
# ---------------------------------------------------------------------------

def iter_writes(reply: str) -> Iterator[tuple[str, str]]:
    """Yield (path, code) for every WRITE block in *reply*."""
    for m in QWRITE_RE.finditer(reply):
        attrs = parse_attrs(m.group("attrs"))
        path  = attrs.get("path", "").strip()
        if path:
            yield path, m.group("code")
    for m in _LEGACY_WRITE_RE.finditer(reply):
        yield m.group("path").strip(), m.group("code")


def iter_inserts(reply: str) -> Iterator[tuple[str, int, str]]:
    """Yield (path, line_no, code) for every INSERT block in *reply*."""
    for m in QINSERT_RE.finditer(reply):
        attrs = parse_attrs(m.group("attrs"))
        path  = attrs.get("path", "").strip()
        line  = attrs.get("line", "").strip()
        if path and line.isdigit():
            yield path, int(line), m.group("code")
    for m in _LEGACY_INSERT_RE.finditer(reply):
        yield m.group("path").strip(), int(m.group("line")), m.group("code")


def iter_reads(reply: str) -> Iterator[str]:
    """Yield path for every READ request in *reply*."""
    for m in QREAD_RE.finditer(reply):
        attrs = parse_attrs(m.group("attrs"))
        path  = attrs.get("path", "").strip()
        if path:
            yield path
    for m in _LEGACY_READ_RE.finditer(reply):
        yield m.group("path").strip()


def iter_runs(reply: str) -> Iterator[str]:
    """Yield shell command for every RUN block in *reply*."""
    for m in QRUN_RE.finditer(reply):
        cmd = m.group("cmd").strip()
        if cmd:
            yield cmd
    for m in _LEGACY_RUN_RE.finditer(reply):
        cmd = m.group("cmd").strip()
        if cmd:
            yield cmd


# ---------------------------------------------------------------------------
# READ requests  (AI -> tool)
# ---------------------------------------------------------------------------

def has_read_requests(reply: str) -> bool:
    return bool(QREAD_RE.search(reply) or _LEGACY_READ_RE.search(reply))


def collect_read_requests(reply: str) -> list[str]:
    seen:  set[str]  = set()
    paths: list[str] = []
    for p in iter_reads(reply):
        if p not in seen:
            seen.add(p)
            paths.append(p)

    return paths


def has_inserts(reply: str) -> bool:
    return bool(QINSERT_RE.search(reply) or _LEGACY_INSERT_RE.search(reply))


# ---------------------------------------------------------------------------
# Partial-write helpers
# ---------------------------------------------------------------------------

def reply_has_partial_write(reply: str) -> bool:
    for _path, code in iter_writes(reply):
        for pat in _PARTIAL_PATTERNS:
            if pat.search(code):
                return True
    return False


def apply_file_writes(reply: str) -> None:
    for path, code in iter_writes(reply):
        write_file_with_vc(path, code)


# ---------------------------------------------------------------------------
# INSERT helpers
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
    """Process every INSERT block in *reply*.

    When the 'insert_verify' setting is True (default):
      - Checks syntax of the resulting file.
      - Shows a preview diff around the insertion point.
      - Asks the user to confirm before writing.

    When 'insert_verify' is False the insertion is applied silently.
    """
    from qwen3_code.settings import CFG
    from qwen3_code.utils import resolve_path

    verify: bool = bool(CFG.get("insert_verify", True))

    for raw_path, line_no, new_code in iter_inserts(reply):
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


# ---------------------------------------------------------------------------
# RUN command application
# ---------------------------------------------------------------------------

def apply_command_runs(reply: str, cwd: str, messages: list[dict]) -> None:
    commands: list[str] = list(iter_runs(reply))
    if not commands:
        return

    session = ConsoleSession()

    for cmd in commands:
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
        messages.append({
            "role":    "user",
            "content": f"[Command `{cmd}` output:]\n<qcode>\n{output}\n</qcode>",
        })

    session.print_summary()
