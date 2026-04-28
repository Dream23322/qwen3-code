"""Coding-rules manager: presets, global custom file, per-session custom file."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rich.markup import escape as _esc
from rich.panel import Panel

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP
from qwen3_code.settings import CFG, save_settings


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

RULES_DIR:         Path = Path.home() / ".local" / "share" / "qwen3-code" / "rules"
GLOBAL_CUSTOM:     Path = RULES_DIR / "custom.md"
SESSION_RULES_DIR: Path = RULES_DIR / "session"


def _session_rules_path(cwd: str) -> Path:
    h: str = hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:16]
    return SESSION_RULES_DIR / f"{h}.md"


# ---------------------------------------------------------------------------
# HTML-comment template scrubber (template hints don't reach the model)
# ---------------------------------------------------------------------------

_HTML_COMMENT_RE: re.Pattern[str] = re.compile(r"<!--.*?-->", re.DOTALL)


def _read_custom(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        text: str = path.read_text(encoding="utf-8")
    except Exception:
        return ""

    return _HTML_COMMENT_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# Built-in presets (condensed but information-complete)
# ---------------------------------------------------------------------------

_PEP8_BODY: str = """\
PEP 8 \u2014 Python style guide.

LAYOUT
- Indent with 4 spaces per level. No tabs. For continuation lines, either align with the opening delimiter, or use a hanging indent (no arguments on the first line, closing delimiter on its own line).
- Maximum line length: 79 characters for code, 72 for comments and docstrings.
- Break BEFORE binary operators (Knuth style):
    income = (gross
              + bonus
              - taxes)
- Blank lines: 2 between top-level functions and classes; 1 between methods inside a class; sparingly inside functions to separate logical sections.
- Source files use UTF-8; no encoding declaration needed.

IMPORTS
- One module per import line: `import os` then `import sys`, never `import os, sys`. (`from x import a, b` is fine.)
- Place imports at the top of the file, after the module docstring and module-level dunders, and before module globals.
- Group imports in this order, separated by a blank line: standard library, related third party, local application.
- Prefer absolute imports. Use explicit relative imports (`from . import x`) only when absolute imports would be awkward.
- Avoid wildcard imports (`from x import *`) except when re-exporting.
- Module-level dunders (`__all__`, `__version__`, `__author__`) go after the docstring and before any imports except `from __future__`.

STRINGS AND QUOTES
- Pick single or double quotes for string literals and stick to it within a project. Use the other style to avoid backslash escaping.
- Triple-double-quoted strings for docstrings (PEP 257).

WHITESPACE
- No whitespace immediately inside `()`, `[]`, `{}`; before commas, semicolons, colons; or before the opening paren of a call/index.
- Single space around binary operators: `x = 1`, `a == b`. When mixing operators of different precedence, group by precedence: `hypot2 = x*x + y*y`.
- No spaces around `=` for keyword arguments or default parameter values, UNLESS an annotation is present: `def f(x: int = 0)` is correct.
- No trailing whitespace.
- Slices: treat the colon as a binary operator with equal spaces on both sides (`ham[1:9]`, `ham[1:9:3]`).

COMMENTS
- Keep comments up to date with the code. Write complete sentences.
- Block comments: each line starts with `# ` (hash + single space), at the indent level of the code they describe.
- Inline comments: at least two spaces from the statement, then `# `.
- Use English unless your audience is otherwise.
- Docstrings: follow PEP 257. Multi-line docstrings have a summary line, a blank line, then more.

NAMING
- Modules: `short_lowercase`, underscores allowed.
- Packages: `shortlowercase`, no underscores.
- Classes: `CapWords` (PascalCase).
- Exceptions: `CapWords` ending in `Error` if they really are errors.
- Type variables: short `CapWords` (`T`, `KT`, `VT`, `AnyStr`).
- Functions / methods / variables: `lowercase_with_underscores`.
- Constants: `UPPER_SNAKE_CASE`, defined at module level.
- First arg of an instance method is `self`; of a classmethod, `cls`.
- Trailing underscore avoids reserved-word clashes (`class_`, `type_`).
- Avoid `l`, `O`, `I` as single-character names (visually ambiguous).
- Single leading underscore (`_private`) marks weak \"internal use\" intent. Double leading underscore (`__name`) triggers name-mangling on classes \u2014 use sparingly.

PROGRAMMING RECOMMENDATIONS
- Compare with `None` using `is` / `is not`, never `==`.
- `is not` is preferred over `not ... is`.
- For booleans, write `if x:` / `if not x:`, never `if x == True`.
- Define functions with `def`, not by assigning a `lambda`.
- Derive custom exceptions from `Exception`, not `BaseException`.
- Catch the narrowest exception you can; bare `except:` only when re-raising or logging unexpected errors at a top-level boundary.
- Keep `try` blocks minimal \u2014 only the code that can raise the handled exception.
- Use `with` for resources that need cleanup (files, locks, connections).
- Use string methods, not the `string` module.
- Use `str.startswith()` / `str.endswith()` instead of slicing for prefix/suffix checks.
- `isinstance(obj, cls)` for type checks; `type(obj) is cls` only when you specifically want exact type.
- For sequences and strings, `if seq:` / `if not seq:` \u2014 don't compare length to 0.
- Use `functools.wraps` when writing decorators.
- Return statements should be consistent: every return in a function either returns a value, or none of them do.
- Function annotations: PEP 484. Variable annotations: PEP 526.

LINE CONTINUATION
- Prefer implicit continuation inside `()`, `[]`, `{}` over backslash continuation. Use a backslash only when there is no other option.
"""


_PEP257_BODY: str = """\
PEP 257 \u2014 Docstring conventions.

- Every public module, function, class, and method has a docstring.
- Always use triple double quotes for docstrings.
- One-line docstrings: a phrase ending in a period, on a single line. Use the imperative mood (\"Do X\", not \"Does X\" / \"Returns X\").
- Multi-line docstrings: a summary line just like a one-liner, then a blank line, then the more elaborate description. The closing triple quotes go on a line by themselves.
- Class docstring: summary first, then document public methods, attributes, and any subclass-relevant behaviour.
- Function/method docstring: summarise behaviour, document arguments, return value(s), side effects, exceptions raised, and any restrictions on when it can be called.
- Script (executable) docstring: should be usable as the script's \"usage\" message printed in response to invalid or missing arguments.
- Don't repeat the function/method's signature in the docstring.
- No blank line before or after the docstring inside its function or class.
"""


_PEP20_BODY: str = """\
PEP 20 \u2014 The Zen of Python (guidelines, not strict rules).

- Beautiful is better than ugly.
- Explicit is better than implicit.
- Simple is better than complex.
- Complex is better than complicated.
- Flat is better than nested.
- Sparse is better than dense.
- Readability counts.
- Special cases aren't special enough to break the rules \u2014 although practicality beats purity.
- Errors should never pass silently \u2014 unless explicitly silenced.
- In the face of ambiguity, refuse the temptation to guess.
- There should be one \u2014 and preferably only one \u2014 obvious way to do it.
- Now is better than never; although never is often better than *right* now.
- If the implementation is hard to explain, it's a bad idea. If the implementation is easy to explain, it may be a good idea.
- Namespaces are one honking great idea \u2014 let's do more of those!
"""


_GOOGLE_PY_BODY: str = """\
Google Python Style Guide \u2014 key rules.

- Indent: 4 spaces. No tabs. Max line length: 80 characters (Google's choice within PEP 8 latitude).
- Strings: prefer single quotes for plain strings; triple double quotes for docstrings. f-strings, str.format, and `%` are all OK \u2014 pick one per file.
- Imports: only `import x` or `from x import y` where `y` is a package, module, class, function, type, or constant. Don't import functions and use them unqualified.
- Use absolute imports. Avoid relative imports.
- Naming: `module_name`, `package_name`, `ClassName`, `ExceptionName`, `function_name`, `GLOBAL_CONSTANT_NAME`, `global_var_name`, `instance_var_name`, `function_parameter_name`, `local_var_name`. `_protected` (single leading underscore), `__private` (double leading underscore for name-mangling).
- Public APIs have docstrings using Args / Returns / Raises sections (Google style):
    \"\"\"Summary line.

    Args:
        path: Description of path.
        count: Description of count.

    Returns:
        Description of the return value.

    Raises:
        IOError: When the file cannot be read.
    \"\"\"
- Type-annotate public APIs (PEP 484).
- Avoid mutable default arguments (`def f(x=[])` is a bug magnet). Use `None` and create the value inside.
- Use `is` / `is not` for `None` and other singletons.
- Use list/dict/set comprehensions for simple cases (single for-clause and optional filter). For anything more complex, use a regular loop.
- Prefer generator expressions over building intermediate lists when iterating once.
- Use `with` for resource management.
- Catch the narrowest exception possible. No bare `except:`.
- TODO comments include a name and a short description: `# TODO(username): Reason or link to bug`.
- Keep functions short, focused, and named for what they do, not how.
"""


_AIRBNB_JS_BODY: str = """\
Airbnb JavaScript Style \u2014 key rules.

- Variables: use `const` for everything; use `let` only when reassignment is required. Never use `var`.
- Use object/array literal syntax: `const obj = {}`, `const arr = []`.
- Use property and method shorthand in object literals.
- Use template literals (`` `Hello ${name}` ``) instead of string concatenation.
- Use the spread operator for shallow copies and merging: `{ ...a, ...b }`, `[ ...arr, x ]`.
- Use destructuring for objects and arrays whenever practical.
- Always use `===` and `!==` (never `==` / `!=`).
- Arrow functions for anonymous callbacks. Always wrap parameters in parens: `(x) => x * 2`.
- Modules: prefer named exports; one component or one logical unit per file when possible. Use `import` / `export`, never `require` in new code.
- Indentation: 2 spaces. Single quotes `'` for strings, backticks for templates. Always end statements with a semicolon.
- Always use braces for `if`, `else`, `while`, `for` \u2014 even single-statement bodies.
- Avoid leading or trailing underscores in identifiers.
- Naming: `PascalCase` for classes and React components, `camelCase` for variables/functions, `UPPER_SNAKE_CASE` for true constants. Filenames match the default export.
- Prefer functional iteration (`map`, `filter`, `reduce`, `find`, `some`, `every`) over imperative loops where readability is equal.
- Treat function arguments as immutable; don't mutate them.
- Never use the comma operator. Never use `with`. Avoid `eval`.
- No unused variables, no unreachable code, no implicit globals.
- Always handle Promise rejections; either `await` inside `try`/`catch`, or attach `.catch()`.
- Prefer `async`/`await` over raw `.then()` chains.
"""


_CLEAN_CODE_BODY: str = """\
Clean Code (Robert C. Martin) \u2014 language-agnostic essentials.

NAMES
- Use intention-revealing, pronounceable, searchable names.
- Avoid disinformation, encodings (e.g. Hungarian notation), and mental mapping.
- Class names are nouns. Function names are verbs.
- Be consistent: one word per concept, one concept per word.

FUNCTIONS
- Small. Then smaller. Aim for a few lines.
- Do one thing. One level of abstraction per function.
- Use descriptive names; long names that explain are better than short cryptic ones.
- Few arguments: 0 > 1 > 2 > 3+. Avoid flag arguments \u2014 if a function does two things, split it.
- Avoid output arguments; prefer return values.
- No side effects beyond what the name implies.

ERROR HANDLING
- Prefer exceptions to error codes.
- Don't return null \u2014 throw, or return an empty collection / Optional.
- Don't pass null \u2014 treat passing null as a programming error at the boundary.

COMMENTS
- Don't compensate for bad code with comments \u2014 fix the code.
- Comments explain *why*, never *what* (the code already says what).
- Delete commented-out code; rely on version control.
- TODOs are fine for known follow-ups, but tracked.

FORMATTING
- Vertical openness between concepts; closely-related lines stay close.
- Horizontal: short lines; consistent indentation; team agrees on a single style.

OBJECTS AND DATA
- Objects expose behaviour and hide data. Data structures expose data and have no significant behaviour. Don't mix the two in one type.
- Tell, don't ask: prefer methods that act over getters that leak state.

CONTROL FLOW
- Avoid deep nesting. Prefer guard clauses for early returns.
- Replace nested conditionals with polymorphism when types vary.
- Don't repeat yourself \u2014 but don't prematurely abstract either; wait until duplication actually hurts.

TESTS
- Keep test code as clean as production code.
- One assert per concept where possible. Readable test names.
- F.I.R.S.T.: Fast, Independent, Repeatable, Self-validating, Timely.

THE BOY SCOUT RULE
- Always leave the code cleaner than you found it.
"""


PRESETS: dict[str, tuple[str, str]] = {
    "pep8":          ("PEP 8 \u2014 Python style",         _PEP8_BODY),
    "pep257":        ("PEP 257 \u2014 Python docstrings",  _PEP257_BODY),
    "pep20":         ("PEP 20 \u2014 Zen of Python",       _PEP20_BODY),
    "google-python": ("Google Python style",               _GOOGLE_PY_BODY),
    "airbnb-js":     ("Airbnb JavaScript style",           _AIRBNB_JS_BODY),
    "clean-code":    ("Clean Code (Robert C. Martin)",     _CLEAN_CODE_BODY),
}


# ---------------------------------------------------------------------------
# Active rules text (consumed by the renderer)
# ---------------------------------------------------------------------------

def get_active_rules_text(cwd: str = "") -> str:
    """Return concatenated rules text from preset + global + session custom.

    Empty / missing sources contribute nothing.
    """
    parts: list[str] = []

    preset_name: str | None = CFG.get("active_preset") or None
    if preset_name and preset_name in PRESETS:
        display, body = PRESETS[preset_name]
        parts.append(f"=== Preset: {display} ===\n\n{body.strip()}")

    global_text: str = _read_custom(GLOBAL_CUSTOM)
    if global_text:
        parts.append(f"=== Custom rules (global) ===\n\n{global_text}")

    if cwd:
        session_text: str = _read_custom(_session_rules_path(cwd))
        if session_text:
            parts.append(f"=== Custom rules (session: {cwd}) ===\n\n{session_text}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Editor launching
# ---------------------------------------------------------------------------

_GLOBAL_TEMPLATE: str = (
    "<!--\n"
    "Custom coding rules (global).\n"
    "\n"
    "Anything you write OUTSIDE these <!-- ... --> blocks is sent to the model\n"
    "as a system message on every request, on top of any active preset and\n"
    "on top of session-specific custom rules.\n"
    "\n"
    "Template comments inside <!-- ... --> are stripped before sending, so\n"
    "feel free to leave them. Save and quit when done. An empty file (or one\n"
    "containing only template comments) disables the global rules.\n"
    "-->\n"
    "\n"
)


def _session_template(cwd: str) -> str:
    return (
        "<!--\n"
        f"Custom coding rules for: {cwd}\n"
        "\n"
        "These rules apply ONLY when qwen3-code is run from this directory.\n"
        "They stack on top of any active preset and on top of global custom rules.\n"
        "\n"
        "Template comments inside <!-- ... --> are stripped before sending.\n"
        "An empty file disables session-specific rules.\n"
        "-->\n"
        "\n"
    )


def _resolve_editor() -> str | None:
    visual: str | None = os.environ.get("VISUAL")
    editor: str | None = os.environ.get("EDITOR")
    if visual: return visual
    if editor: return editor

    for candidate in ("vim", "nvim", "vi", "nano"):
        if shutil.which(candidate):
            return candidate

    if sys.platform == "win32":
        return "notepad"

    return None


def _open_in_editor(path: Path, template: str = "") -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(template, encoding="utf-8")

    editor: str | None = _resolve_editor()
    if not editor:
        console.print(
            "[error]No editor found. Set $EDITOR (e.g. `export EDITOR=vim`) "
            "or install vim / nano.[/error]"
        )
        return False

    try:
        subprocess.call([editor, str(path)])
    except Exception as exc:
        console.print(f"[error]Editor failed: {exc}[/error]")
        return False

    return True


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _show_status(cwd: str) -> None:
    preset:    str | None = CFG.get("active_preset")
    global_t:  str        = _read_custom(GLOBAL_CUSTOM)
    session_p: Path       = _session_rules_path(cwd) if cwd else GLOBAL_CUSTOM
    session_t: str        = _read_custom(session_p) if cwd else ""

    lines: list[str] = []

    if preset and preset in PRESETS:
        display: str = PRESETS[preset][0]
        lines.append(f"  [bold]Preset[/bold]   : [{SAKURA}]{preset}[/{SAKURA}]  [dim]({display})[/dim]")
    elif preset:
        lines.append(f"  [bold]Preset[/bold]   : [{SAKURA}]{preset}[/{SAKURA}]  [dim](unknown \u2014 run /rules off)[/dim]")
    else:
        lines.append(f"  [bold]Preset[/bold]   : [dim](none)[/dim]")

    g_state: str = "active" if global_t else "[dim]empty[/dim]"
    lines.append(f"  [bold]Global[/bold]   : {g_state}  [dim]{GLOBAL_CUSTOM}[/dim]")

    if cwd:
        s_state: str = "active" if session_t else "[dim]empty[/dim]"
        lines.append(f"  [bold]Session[/bold]  : {s_state}  [dim]{session_p}[/dim]")

    lines.append("")
    lines.append("[dim]Subcommands:[/dim]")
    lines.append("  [bold]/rules list[/bold]                 list available presets")
    lines.append("  [bold]/rules show[/bold]                 print combined active rules")
    lines.append("  [bold]/rules <preset>[/bold]             activate a preset (e.g. /rules pep8)")
    lines.append("  [bold]/rules off[/bold]                  deactivate the current preset")
    lines.append("  [bold]/rules custom[/bold]               edit GLOBAL custom rules in $EDITOR")
    lines.append("  [bold]/rules custom -s[/bold]            edit SESSION (per-cwd) custom rules in $EDITOR")
    lines.append("  [bold]/rules custom clear[/bold]         delete the global custom file")
    lines.append("  [bold]/rules custom -s clear[/bold]      delete the session custom file")

    console.print(Panel("\n".join(lines), title="/rules", border_style=SAKURA_DEEP))


def _list_presets() -> None:
    rows:   list[str]  = []
    active: str | None = CFG.get("active_preset")
    for key, (display, _body) in PRESETS.items():
        marker: str = "[green]\u2022[/green]" if key == active else " "
        rows.append(f"  {marker} [bold cyan]{key:<14}[/bold cyan] [dim]{display}[/dim]")

    console.print(Panel("\n".join(rows), title="Built-in rule presets", border_style=SAKURA))


def _show_active(cwd: str) -> None:
    text: str = get_active_rules_text(cwd)
    if not text:
        console.print(
            "[info]No rules active. "
            "Use [bold]/rules <preset>[/bold] or [bold]/rules custom[/bold].[/info]"
        )
        return

    console.print(Panel(_esc(text), title="Active rules (sent to model)", border_style=SAKURA_DEEP))


def _post_edit_report(path: Path, label: str) -> None:
    text: str = _read_custom(path)
    if not text:
        console.print(
            f"[info]{label.capitalize()} custom rules file is empty \u2014 rules disabled.[/info]"
        )
        return

    line_count: int = len([l for l in text.splitlines() if l.strip()])
    console.print(
        f"[info]{label.capitalize()} custom rules saved "
        f"({line_count} non-empty line(s)).[/info]"
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def handle_rules(arg: str, state: dict) -> None:
    cwd:   str       = state.get("cwd", "")
    parts: list[str] = arg.strip().split()

    if not parts:
        _show_status(cwd)
        return

    head: str       = parts[0].lower()
    rest: list[str] = [p.lower() for p in parts[1:]]

    if head in ("list", "ls"):
        _list_presets()
        return

    if head == "show":
        _show_active(cwd)
        return

    if head == "off":
        old: str | None = CFG.get("active_preset")
        if old:
            CFG["active_preset"] = None
            save_settings(CFG)
            console.print(
                f"[info]Preset [bold]{old}[/bold] deactivated. "
                "Custom rules unchanged.[/info]"
            )
        else:
            console.print("[info]No preset was active.[/info]")
        return

    if head == "custom":
        session_scope: bool = "-s" in rest
        clear:         bool = "clear" in rest

        if clear:
            target: Path = _session_rules_path(cwd) if session_scope else GLOBAL_CUSTOM
            label:  str  = "session" if session_scope else "global"
            if not target.exists():
                console.print(f"[info]No {label} custom rules to clear.[/info]")
                return
            try:
                target.unlink()
                console.print(
                    f"[info]Deleted {label} custom rules at [bold]{target}[/bold].[/info]"
                )
            except Exception as exc:
                console.print(f"[error]Failed to delete {target}: {exc}[/error]")
            return

        if session_scope:
            if not cwd:
                console.print(
                    "[error]No working directory available for session rules.[/error]"
                )
                return
            sp: Path = _session_rules_path(cwd)
            if _open_in_editor(sp, _session_template(cwd)):
                _post_edit_report(sp, "session")
            return

        if _open_in_editor(GLOBAL_CUSTOM, _GLOBAL_TEMPLATE):
            _post_edit_report(GLOBAL_CUSTOM, "global")
        return

    if head in PRESETS:
        CFG["active_preset"] = head
        save_settings(CFG)
        display: str = PRESETS[head][0]
        console.print(
            f"[info]Activated preset [bold]{head}[/bold]  [dim]({display})[/dim]. "
            "Custom rules still apply on top.[/info]"
        )
        return

    console.print(
        f"[error]Unknown preset or subcommand: {head!r}. "
        "Run [bold]/rules list[/bold] for presets or [bold]/rules[/bold] for usage.[/error]"
    )
