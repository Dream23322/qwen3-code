"""Multi-model council: members answer in parallel, leader picks the best reply.

Flow
----
1. /council start  -> user picks members and a leader from installed ollama models
2. While active, every plain message is sent to all members in parallel.
3. The leader is asked to choose which response to surface to the user.
4. The user may keep the leader's pick or switch to any other response.
5. Only the selected response is appended to the persistent message history;
   discarded responses are NOT saved as context.
6. /council end closes the session.
"""

import re
import threading
from typing import Any

import ollama
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.renderer import render_response
from qwen3_code.partial import (
    apply_file_writes, apply_file_inserts, apply_command_runs,
)


# ---------------------------------------------------------------------------
# Ollama model discovery
# ---------------------------------------------------------------------------

def _list_installed_models() -> list[str]:
    """Return a sorted, de-duplicated list of locally installed ollama model tags."""
    try:
        data: Any = ollama.list()
    except Exception as exc:
        console.print(f"[error]Could not list ollama models: {exc}[/error]")
        return []

    raw_list: Any = []
    if isinstance(data, dict):
        raw_list = data.get("models", [])
    else:
        raw_list = getattr(data, "models", []) or []

    names: list[str] = []
    for m in raw_list:
        n: str = ""
        if isinstance(m, dict):
            n = str(m.get("name") or m.get("model") or "")
        else:
            n = str(getattr(m, "name", "") or getattr(m, "model", "") or "")
        if n:
            names.append(n)

    return sorted(set(names))


# ---------------------------------------------------------------------------
# Selection UI helpers
# ---------------------------------------------------------------------------

def _parse_indices(raw: str, n: int) -> list[int]:
    out: list[int] = []
    for tok in re.split(r"[\s,]+", raw.strip()):
        if not tok:
            continue
        try:
            i: int = int(tok) - 1
        except ValueError:
            continue
        if 0 <= i < n:
            out.append(i)

    return out


def _members_panel(models: list[str], selected: set[int]) -> Panel:
    rows: list[str] = ["[bold]Members:[/bold]", ""]
    for i, name in enumerate(models, 1):
        mark: str = "x" if (i - 1) in selected else "-"
        rows.append(f"  [{mark}] {i}. {name}")
    rows.append("")
    rows.append(
        "[dim]Toggle by number(s) (e.g. '1 3' or '1,3'). "
        "Press enter on an empty line when done.[/dim]"
    )

    return Panel("\n".join(rows), title="/council  -  Select members", border_style=SAKURA_DEEP)


def _leader_panel(member_names: list[str], chosen: int | None) -> Panel:
    rows: list[str] = ["[bold]Choose a leader:[/bold]", ""]
    for i, name in enumerate(member_names, 1):
        mark: str = "x" if chosen == (i - 1) else "-"
        rows.append(f"  [{mark}] {i}. {name}")
    rows.append("")
    rows.append("[dim]Type a number to choose. Press enter on an empty line to confirm.[/dim]")

    return Panel("\n".join(rows), title="/council  -  Select leader", border_style=SAKURA_DEEP)


def _select_members(models: list[str]) -> list[int]:
    selected: set[int] = set()
    while True:
        console.print(_members_panel(models, selected))
        raw: str = console.input("[bold]toggle> [/bold]").strip()
        if not raw:
            if selected:
                return sorted(selected)
            console.print("[error]Select at least one member.[/error]")
            continue
        for i in _parse_indices(raw, len(models)):
            if i in selected:
                selected.discard(i)
            else:
                selected.add(i)


def _select_leader(member_names: list[str]) -> int:
    chosen: int | None = None
    while True:
        console.print(_leader_panel(member_names, chosen))
        raw: str = console.input("[bold]leader> [/bold]").strip()
        if not raw:
            if chosen is not None:
                return chosen
            console.print("[error]Pick a leader.[/error]")
            continue
        idx: list[int] = _parse_indices(raw, len(member_names))
        if idx:
            chosen = idx[-1]


# ---------------------------------------------------------------------------
# Member queries
# ---------------------------------------------------------------------------

def _ask_member(model: str, messages: list[dict]) -> str:
    """Synchronously query one ollama model and return its full reply."""
    chunks: list[str] = []
    try:
        for chunk in ollama.chat(model=model, messages=messages, stream=True):
            chunks.append(chunk["message"]["content"])
    except Exception as exc:
        return f"[member error: {exc}]"

    return "".join(chunks).strip()


def _gather_responses(members: list[str], messages: list[dict]) -> dict[str, str]:
    """Run all members concurrently, return {model_name: reply}."""
    statuses: dict[str, str] = {m: "thinking\u2026" for m in members}
    results: dict[str, str]  = {}
    lock: threading.Lock     = threading.Lock()

    def _panel() -> Panel:
        rows: list[str] = []
        for m in members:
            rows.append(f"  [bold]{m}[/bold]  [dim]{statuses[m]}[/dim]")
        return Panel("\n".join(rows), title="Council members responding", border_style=SAKURA)

    def _worker(model: str) -> None:
        reply: str = _ask_member(model, messages)
        with lock:
            results[model]  = reply
            statuses[model] = (
                f"done ({len(reply)} chars)" if reply else "empty"
            )

    threads: list[threading.Thread] = []
    for m in members:
        t: threading.Thread = threading.Thread(target=_worker, args=(m,), daemon=True)
        t.start()
        threads.append(t)

    with Live(_panel(), console=console, refresh_per_second=8) as live:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.1)
            live.update(_panel())
        live.update(_panel())

    for t in threads:
        t.join()

    return results


# ---------------------------------------------------------------------------
# Leader judging
# ---------------------------------------------------------------------------

def _ask_leader_choice(
    leader: str,
    user_prompt: str,
    member_names: list[str],
    responses: dict[str, str],
) -> int:
    blocks: list[str] = []
    for i, m in enumerate(member_names, 1):
        blocks.append(f"--- Response {i} (from {m}) ---\n{responses.get(m, '').strip()}")

    judge_prompt: str = (
        "You are the leader of a coding-assistant council. Several council members "
        "have produced candidate replies to the user's request below. Pick the SINGLE "
        "best response. Reply with ONLY the response number (e.g. '2'). Do not explain.\n\n"
        f"User's request:\n{user_prompt}\n\n" + "\n\n".join(blocks)
    )

    raw: str = _ask_member(leader, [{"role": "user", "content": judge_prompt}])
    m: re.Match[str] | None = re.search(r"\d+", raw or "")
    if not m:
        return 0
    idx: int = int(m.group(0)) - 1

    return max(0, min(idx, len(member_names) - 1))


# ---------------------------------------------------------------------------
# Result rendering / user review loop
# ---------------------------------------------------------------------------

def _summary_table(
    member_names: list[str],
    responses: dict[str, str],
    chosen_idx: int,
) -> Table:
    t: Table = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("idx",     no_wrap=True)
    t.add_column("model",   no_wrap=True)
    t.add_column("preview")
    for i, m in enumerate(member_names):
        marker: str = "[bold green]\u2192[/bold green]" if i == chosen_idx else " "
        text: str   = (responses.get(m, "") or "").strip()
        first: str  = text.splitlines()[0][:70] if text else "(empty)"
        t.add_row(f"{marker} {i + 1}.", m, first)

    return t


def _review_loop(
    member_names: list[str],
    responses: dict[str, str],
    leader_pick: int,
) -> int:
    chosen_idx: int = leader_pick
    while True:
        console.print(_summary_table(member_names, responses, chosen_idx))
        console.print(
            "[dim]Number = preview that response.  "
            "'u <n>' = use response n instead.  "
            "empty / 'k' = keep current pick.[/dim]"
        )
        raw: str = console.input("[bold]council> [/bold]").strip().lower()
        if raw in ("", "k", "keep"):
            return chosen_idx

        if raw.startswith("u"):
            tail: str = raw[1:].strip()
            try:
                n: int = int(tail) - 1
            except ValueError:
                console.print("[error]Usage: u <number>[/error]")
                continue
            if 0 <= n < len(member_names):
                chosen_idx = n
                console.print(
                    f"[info]Now using response {n + 1} from "
                    f"[bold]{member_names[n]}[/bold].[/info]"
                )
                render_response(responses[member_names[n]])
            else:
                console.print("[error]Out of range.[/error]")
            continue

        try:
            n = int(raw) - 1
        except ValueError:
            console.print("[error]Unknown input.[/error]")
            continue
        if 0 <= n < len(member_names):
            console.print(Panel(
                f"Response {n + 1} from [bold]{member_names[n]}[/bold]",
                border_style=SAKURA_MUTED,
            ))
            render_response(responses[member_names[n]])
        else:
            console.print("[error]Out of range.[/error]")


# ---------------------------------------------------------------------------
# One council round
# ---------------------------------------------------------------------------

def run_council_round(
    council: dict,
    messages: list[dict],
    user_prompt: str,
    cwd: str = "",
) -> str:
    """Run one council round on ``user_prompt``.

    The selected response is appended to ``messages`` (along with the user turn).
    Discarded responses are NEVER added to ``messages``.
    Returns the chosen reply text (empty string on total failure).
    """
    members: list[str] = council["members"]
    leader: str        = council["leader"]

    request_messages: list[dict] = messages + [{"role": "user", "content": user_prompt}]

    console.print(Panel(
        f"[bold]Members:[/bold] {', '.join(members)}\n"
        f"[bold]Leader:[/bold]  {leader}",
        title="Council session", border_style=SAKURA_MUTED,
    ))

    responses: dict[str, str] = _gather_responses(members, request_messages)
    if not any((v or "").strip() for v in responses.values()):
        console.print("[error]No member produced a response.[/error]")
        return ""

    leader_pick: int = _ask_leader_choice(leader, user_prompt, members, responses)
    chosen_member: str = members[leader_pick]
    console.print(Panel(
        f"[bold]Leader[/bold] [{SAKURA}]{leader}[/{SAKURA}] picked response "
        f"[bold]{leader_pick + 1}[/bold] from "
        f"[{SAKURA_DEEP}]{chosen_member}[/{SAKURA_DEEP}]",
        title="Leader's pick", border_style=SAKURA,
    ))
    render_response(responses[chosen_member])

    chosen_idx: int = _review_loop(members, responses, leader_pick)
    chosen_member   = members[chosen_idx]
    chosen_text: str = responses[chosen_member]

    if chosen_idx != leader_pick:
        console.print(Panel(
            f"Final pick: response [bold]{chosen_idx + 1}[/bold] from "
            f"[{SAKURA_DEEP}]{chosen_member}[/{SAKURA_DEEP}]",
            title="User override", border_style=SAKURA_DARK,
        ))

    # Persist ONLY the chosen response into the conversation history.
    messages.append({"role": "user",      "content": user_prompt})
    messages.append({"role": "assistant", "content": chosen_text})

    # Apply any tool markers that may be present in the chosen reply, so the
    # council behaves like the normal chat loop for file edits and commands.
    apply_file_writes(chosen_text)
    apply_file_inserts(chosen_text, cwd)
    apply_command_runs(chosen_text, cwd, messages)

    return chosen_text


# ---------------------------------------------------------------------------
# /council command surface
# ---------------------------------------------------------------------------

def _start_council(state: dict) -> None:
    if state.get("council"):
        console.print(
            "[info]A council session is already active. "
            "Use [bold]/council end[/bold] to close it first.[/info]"
        )
        return

    models: list[str] = _list_installed_models()
    if not models:
        console.print(
            "[error]No ollama models installed. "
            "Run [bold]ollama pull <model>[/bold] first.[/error]"
        )
        return

    member_idx: list[int] = _select_members(models)
    members: list[str]    = [models[i] for i in member_idx]
    leader_idx: int       = _select_leader(members)
    leader: str           = members[leader_idx]

    state["council"] = {"members": members, "leader": leader}

    console.print(Panel(
        f"[bold]Members:[/bold] {', '.join(members)}\n"
        f"[bold]Leader:[/bold]  {leader}\n\n"
        "[dim]Council is now active. Plain messages are routed through the council.\n"
        "End the session with [bold]/council end[/bold].[/dim]",
        title="Council started", border_style=SAKURA_DEEP,
    ))


def _end_council(state: dict) -> None:
    if not state.get("council"):
        console.print("[info]No council session is active.[/info]")
        return
    state.pop("council", None)
    console.print(Panel(
        "Council session ended.",
        title="/council end", border_style=SAKURA_MUTED,
    ))


def _status_council(state: dict) -> None:
    c: dict | None = state.get("council")
    if not c:
        console.print(
            "[info]No active council. Start one with [bold]/council start[/bold].[/info]"
        )
        return
    console.print(Panel(
        f"[bold]Members:[/bold] {', '.join(c['members'])}\n"
        f"[bold]Leader:[/bold]  {c['leader']}",
        title="Council status", border_style=SAKURA,
    ))


def handle_council(arg: str, state: dict) -> None:
    sub: str = arg.strip().lower()
    if sub in ("", "status"):
        _status_council(state); return
    if sub == "start":
        _start_council(state);  return
    if sub == "end":
        _end_council(state);    return

    console.print(
        "[error]Usage: /council start  |  /council end  |  /council status[/error]"
    )
