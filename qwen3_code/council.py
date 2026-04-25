"""Multi-model council: members answer one-by-one (or in parallel), leader picks.

Flow
----
1. /council start  -> user picks members and a leader from installed ollama models
2. While active, every plain message is sent to all members.
   - Default mode is SEQUENTIAL: one member runs at a time and is unloaded
     between turns (keep_alive=0). This is correct on RAM-constrained hosts;
     ollama would otherwise queue parallel requests waiting for RAM and hang.
   - /council parallel on  switches to true concurrency for users with enough VRAM.
3. The leader is asked to choose which response to surface to the user.
4. The user may keep the leader's pick or switch to any other response.
5. Only the selected response is appended to the persistent message history;
   discarded responses are NOT saved as context.
6. /council end closes the session.
"""

import re
import threading
import time
from typing import Any

import ollama
from ollama import Client as OllamaClient
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.renderer import render_response
from qwen3_code.partial import (
    apply_file_writes, apply_file_inserts, apply_command_runs,
)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
DEFAULT_MEMBER_TIMEOUT_S: float = 180.0
DEFAULT_PARALLEL:         bool  = False  # sequential is RAM-safe; opt in to parallel


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
# Member queries  -  with REAL wall-clock timeout (watchdog)
# ---------------------------------------------------------------------------

def _ask_member(
    model: str,
    messages: list[dict],
    timeout_s: float = DEFAULT_MEMBER_TIMEOUT_S,
    keep_alive: float | str | None = 0,
) -> str:
    """Query one ollama model with a hard wall-clock deadline.

    The actual HTTP call runs on a worker thread; the calling thread waits on
    an Event with a timeout. When the deadline passes we forcibly close the
    underlying httpx client so the worker errors out instead of hanging
    forever (which is what happens when ollama is queueing the request while
    waiting for free RAM).

    ``keep_alive=0`` tells ollama to unload the model right after the call,
    which frees RAM for the next member in sequential mode.
    """
    # Cushion the underlying socket timeout slightly above our wall-clock
    # budget so the watchdog is the authority, not httpx.
    client: OllamaClient = OllamaClient(timeout=timeout_s + 30.0)

    result_holder:   list[str]        = [""]
    finished_event:  threading.Event  = threading.Event()

    def _run() -> None:
        chunks: list[str] = []
        try:
            stream = client.chat(
                model=model,
                messages=messages,
                stream=True,
                keep_alive=keep_alive,
            )
            for chunk in stream:
                chunks.append(chunk["message"]["content"])
            result_holder[0] = "".join(chunks).strip()
        except Exception as exc:
            result_holder[0] = f"[member error: {exc}]"
        finally:
            finished_event.set()

    worker: threading.Thread = threading.Thread(target=_run, daemon=True)
    worker.start()

    if not finished_event.wait(timeout=timeout_s):
        # Wall-clock deadline hit. Best-effort tear down the http client to
        # break the worker out of any read.
        try:
            inner: Any = getattr(client, "_client", None)
            if inner is not None:
                inner.close()
        except Exception:
            pass
        return f"[member error: timeout after {timeout_s:.0f}s]"

    return result_holder[0]


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

def _classify(reply: str) -> str:
    if not reply:
        return "empty"
    if reply.startswith("[member error"):
        low: str = reply.lower()
        if "timeout" in low or "timed out" in low or "readtimeout" in low:
            return "timeout"
        return "error"

    return f"done ({len(reply)} chars)"


def _is_valid_reply(text: str) -> bool:
    s: str = (text or "").strip()
    if not s:
        return False
    if s.startswith("[member error"):
        return False

    return True


# ---------------------------------------------------------------------------
# Gather responses (sequential or parallel)
# ---------------------------------------------------------------------------

def _gather_responses(
    members: list[str],
    messages: list[dict],
    timeout_s: float = DEFAULT_MEMBER_TIMEOUT_S,
    parallel: bool   = DEFAULT_PARALLEL,
) -> dict[str, str]:
    """Run all members and return ``{model_name: reply}``.

    Sequential (default): runs one model at a time with ``keep_alive=0`` so
    each is unloaded before the next is loaded. This is the only safe choice
    when total model size > available RAM.

    Parallel: dispatches every member at once. Faster on hosts that can
    actually hold them all simultaneously.

    User can press Ctrl+C to abandon any pending members and continue.
    """
    statuses:      dict[str, str]   = {m: "queued" for m in members}
    starts:        dict[str, float] = {}                # set when each starts
    elapsed_final: dict[str, float] = {}                # frozen on completion
    results:       dict[str, str]   = {}
    lock:          threading.Lock   = threading.Lock()
    cancelled:     dict[str, bool]  = {"flag": False}

    mode_label: str = "parallel" if parallel else "sequential"

    def _row(m: str) -> str:
        tag: str = statuses[m]
        if m in results:
            secs: float = elapsed_final.get(m, 0.0)
            return f"  [bold]{m}[/bold]  [dim]{tag}  ({secs:.0f}s)[/dim]"
        if m in starts:
            secs = time.monotonic() - starts[m]
            return (
                f"  [bold]{m}[/bold]  [dim]{tag}  "
                f"({secs:.0f}s / {timeout_s:.0f}s)[/dim]"
            )
        return f"  [bold]{m}[/bold]  [dim]{tag}[/dim]"

    def _panel() -> Panel:
        rows: list[str] = [_row(m) for m in members]
        rows.append("")
        rows.append(
            f"[dim]Mode: {mode_label}.  "
            "Ctrl+C to abandon pending members and continue with what is done.[/dim]"
        )
        return Panel(
            "\n".join(rows),
            title="Council members responding",
            border_style=SAKURA,
        )

    def _record(model: str, reply: str, started_at: float) -> None:
        with lock:
            results[model]       = reply
            elapsed_final[model] = time.monotonic() - started_at
            statuses[model]      = _classify(reply)

    def _runner_sequential() -> None:
        for m in members:
            if cancelled["flag"]:
                with lock:
                    if m not in results:
                        results[m]       = ""
                        elapsed_final[m] = 0.0
                        statuses[m]      = "skipped"
                continue
            with lock:
                starts[m]   = time.monotonic()
                statuses[m] = "thinking\u2026"
            started_at: float = starts[m]
            reply: str = _ask_member(m, messages, timeout_s, keep_alive=0)
            _record(m, reply, started_at)

    def _runner_parallel_one(model: str) -> None:
        with lock:
            starts[model]   = time.monotonic()
            statuses[model] = "thinking\u2026"
        started_at: float = starts[model]
        reply: str = _ask_member(model, messages, timeout_s, keep_alive=None)
        _record(model, reply, started_at)

    workers: list[threading.Thread] = []
    if parallel:
        for m in members:
            t: threading.Thread = threading.Thread(
                target=_runner_parallel_one, args=(m,), daemon=True,
            )
            t.start()
            workers.append(t)
    else:
        seq_thread: threading.Thread = threading.Thread(
            target=_runner_sequential, daemon=True,
        )
        seq_thread.start()
        workers.append(seq_thread)

    try:
        with Live(_panel(), console=console, refresh_per_second=4) as live:
            while any(t.is_alive() for t in workers):
                for t in workers:
                    t.join(timeout=0.2)
                live.update(_panel())
            live.update(_panel())
    except KeyboardInterrupt:
        with lock:
            cancelled["flag"] = True
            for m in members:
                if m not in results:
                    results[m]       = ""
                    elapsed_final[m] = (
                        time.monotonic() - starts[m] if m in starts else 0.0
                    )
                    statuses[m]      = "skipped"
        console.print(
            "[warn]Skipped pending members - continuing with what is done.[/warn]"
        )

    return results


# ---------------------------------------------------------------------------
# Leader judging
# ---------------------------------------------------------------------------

def _ask_leader_choice(
    leader: str,
    user_prompt: str,
    member_names: list[str],
    responses: dict[str, str],
    timeout_s: float = DEFAULT_MEMBER_TIMEOUT_S,
) -> int:
    valid: list[tuple[int, str]] = [
        (i, m) for i, m in enumerate(member_names)
        if _is_valid_reply(responses.get(m, ""))
    ]
    if not valid:
        return 0
    if len(valid) == 1:
        return valid[0][0]

    blocks: list[str] = []
    for j, (_orig_i, m) in enumerate(valid, 1):
        blocks.append(
            f"--- Response {j} (from {m}) ---\n{responses[m].strip()}"
        )

    judge_prompt: str = (
        "You are the leader of a coding-assistant council. Several council members "
        "have produced candidate replies to the user's request below. Pick the SINGLE "
        "best response. Reply with ONLY the response number (e.g. '2'). Do not explain.\n\n"
        f"User's request:\n{user_prompt}\n\n" + "\n\n".join(blocks)
    )

    raw: str = _ask_member(
        leader,
        [{"role": "user", "content": judge_prompt}],
        timeout_s,
        keep_alive=0,
    )
    match: re.Match[str] | None = re.search(r"\d+", raw or "")
    if not match:
        return valid[0][0]
    j_idx: int = int(match.group(0)) - 1
    j_idx     = max(0, min(j_idx, len(valid) - 1))

    return valid[j_idx][0]


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
        if not text:
            first: str = "(no response)"
        elif text.startswith("[member error"):
            first = text[:80]
        else:
            first = text.splitlines()[0][:70]
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
                if not _is_valid_reply(responses.get(member_names[n], "")):
                    console.print("[error]That member has no usable reply.[/error]")
                    continue
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
            text: str = responses.get(member_names[n], "") or ""
            if text:
                render_response(text)
            else:
                console.print("[dim](no response)[/dim]")
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
    members:    list[str] = council["members"]
    leader:     str       = council["leader"]
    timeout_s:  float     = float(council.get("timeout", DEFAULT_MEMBER_TIMEOUT_S))
    parallel:   bool      = bool(council.get("parallel", DEFAULT_PARALLEL))

    request_messages: list[dict] = messages + [{"role": "user", "content": user_prompt}]

    console.print(Panel(
        f"[bold]Members:[/bold] {', '.join(members)}\n"
        f"[bold]Leader:[/bold]  {leader}\n"
        f"[bold]Mode:[/bold]    {'parallel' if parallel else 'sequential (RAM-safe)'}\n"
        f"[bold]Timeout:[/bold] {timeout_s:.0f}s per member",
        title="Council session", border_style=SAKURA_MUTED,
    ))

    responses: dict[str, str] = _gather_responses(
        members, request_messages, timeout_s, parallel,
    )

    valid_members: list[str] = [
        m for m in members if _is_valid_reply(responses.get(m, ""))
    ]
    if not valid_members:
        console.print(
            "[error]No member produced a usable response. "
            "Try /council timeout <seconds>, drop the slow member, "
            "or /council parallel off.[/error]"
        )
        return ""

    leader_pick: int = _ask_leader_choice(
        leader, user_prompt, members, responses, timeout_s,
    )
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
    members:    list[str] = [models[i] for i in member_idx]
    leader_idx: int       = _select_leader(members)
    leader:     str       = members[leader_idx]

    state["council"] = {
        "members":  members,
        "leader":   leader,
        "timeout":  DEFAULT_MEMBER_TIMEOUT_S,
        "parallel": DEFAULT_PARALLEL,
    }

    console.print(Panel(
        f"[bold]Members:[/bold] {', '.join(members)}\n"
        f"[bold]Leader:[/bold]  {leader}\n"
        f"[bold]Mode:[/bold]    {'parallel' if DEFAULT_PARALLEL else 'sequential (RAM-safe)'}\n"
        f"[bold]Timeout:[/bold] {DEFAULT_MEMBER_TIMEOUT_S:.0f}s per member\n\n"
        "[dim]/council parallel on|off  -  toggle concurrency.\n"
        "/council timeout <seconds>  -  change per-member timeout.\n"
        "Plain messages are routed through the council; "
        "end with [bold]/council end[/bold].[/dim]",
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
    parallel: bool = bool(c.get("parallel", DEFAULT_PARALLEL))
    console.print(Panel(
        f"[bold]Members:[/bold] {', '.join(c['members'])}\n"
        f"[bold]Leader:[/bold]  {c['leader']}\n"
        f"[bold]Mode:[/bold]    {'parallel' if parallel else 'sequential (RAM-safe)'}\n"
        f"[bold]Timeout:[/bold] {float(c.get('timeout', DEFAULT_MEMBER_TIMEOUT_S)):.0f}s per member",
        title="Council status", border_style=SAKURA,
    ))


def _set_timeout(arg: str, state: dict) -> None:
    c: dict | None = state.get("council")
    if not c:
        console.print(
            "[info]No active council. Start one with [bold]/council start[/bold].[/info]"
        )
        return
    try:
        secs: float = float(arg.strip())
    except ValueError:
        console.print("[error]Usage: /council timeout <seconds>[/error]")
        return
    if secs <= 0:
        console.print("[error]Timeout must be positive.[/error]")
        return
    c["timeout"] = secs
    console.print(f"[info]Council per-member timeout set to {secs:.0f}s.[/info]")


def _set_parallel(arg: str, state: dict) -> None:
    c: dict | None = state.get("council")
    if not c:
        console.print(
            "[info]No active council. Start one with [bold]/council start[/bold].[/info]"
        )
        return
    val: str = arg.strip().lower()
    enabled: bool
    if val in ("on", "true", "yes", "1"):
        enabled = True
    elif val in ("off", "false", "no", "0"):
        enabled = False
    else:
        console.print("[error]Usage: /council parallel on|off[/error]")
        return
    c["parallel"] = enabled
    console.print(
        f"[info]Council mode: {'parallel' if enabled else 'sequential (RAM-safe)'}.[/info]"
    )


def handle_council(arg: str, state: dict) -> None:
    raw: str = arg.strip()
    parts: list[str] = raw.split(None, 1)
    sub:  str = parts[0].lower() if parts else ""
    rest: str = parts[1] if len(parts) > 1 else ""

    if sub in ("", "status"):
        _status_council(state); return
    if sub == "start":
        _start_council(state);  return
    if sub == "end":
        _end_council(state);    return
    if sub == "timeout":
        _set_timeout(rest, state);  return
    if sub == "parallel":
        _set_parallel(rest, state); return

    console.print(
        "[error]Usage: /council start  |  /council end  |  /council status  |  "
        "/council timeout <seconds>  |  /council parallel on|off[/error]"
    )
