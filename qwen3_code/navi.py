"""Navi mode: ask the model to summarise a task and pick which tools it needs.

When navi is enabled, every user turn goes through a quick routing call
BEFORE the main response. The router LLM produces:

    TASK: <one-sentence restatement>
    TOOLS: <comma-separated subset of: read, write, insert, run, code>

The main turn is then run with a slim system prompt that documents only
those action tags. This dramatically reduces prompt size for small local
models that get distracted by long instruction blocks.

Failure is non-fatal: if the router call errors or its output doesn't
parse, we fall back to the full set of tools so the user still gets a
response.
"""

import re

import ollama

from qwen3_code.settings import _model

_VALID_TOOLS: set[str] = {"code", "write", "insert", "read", "run"}

_NAVI_SYSTEM: str = (
    "You are a router. The user will give you a coding task. "
    "Do exactly TWO things, briefly:\n"
    "1. Restate the task in one short sentence.\n"
    "2. List which action tags will be needed.\n"
    "\n"
    "Available tags:\n"
    "  read   - read a file's contents\n"
    "  write  - rewrite a whole file\n"
    "  insert - insert lines into a file at a given line\n"
    "  run    - execute a shell command\n"
    "  code   - display code without changing files\n"
    "\n"
    "Output EXACTLY this format and nothing else:\n"
    "TASK: <one sentence>\n"
    "TOOLS: <comma-separated list, or 'none'>"
)

_TASK_RE:  re.Pattern = re.compile(r"^\s*task\s*:\s*(.+)$",  re.IGNORECASE | re.MULTILINE)
_TOOLS_RE: re.Pattern = re.compile(r"^\s*tools\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def select_tools_for_task(user_msg: str) -> tuple[str, set[str]]:
    """Run a one-shot routing call.

    Returns (summary, tool_set). On any error, falls back to (msg-prefix,
    every tool) so the caller can keep going with the full prompt.
    """
    try:
        result = ollama.chat(
            model=_model(),
            messages=[
                {"role": "system", "content": _NAVI_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            stream=False,
        )
        text: str = result["message"]["content"]
    except Exception:
        return user_msg[:120], set(_VALID_TOOLS)

    summary: str       = ""
    tools:   set[str]  = set()

    if (m := _TASK_RE.search(text)):
        summary = m.group(1).strip()
    if (m := _TOOLS_RE.search(text)):
        raw: str = m.group(1).strip().lower().rstrip(".")
        if raw and raw != "none":
            for t in re.split(r"[,\s]+", raw):
                t = t.strip().rstrip(".")
                if t in _VALID_TOOLS:
                    tools.add(t)

    if not tools:
        # Router didn't pick anything useful -- fall back to every tool.
        tools = set(_VALID_TOOLS)

    # Display-only code blocks are always allowed.
    tools.add("code")

    return summary or user_msg[:120], tools
