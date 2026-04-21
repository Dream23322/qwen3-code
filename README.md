# qwen3-code

A simple [Claude Code](https://www.anthropic.com/claude-code)-style **TUI** (terminal UI) for coding assistance, powered by [Ollama](https://ollama.com) and [`huihui_ai/qwen3-coder-abliterated:30b`](https://ollama.com/huihui_ai/qwen3-coder-abliterated).

## Features

- Stream responses directly in the terminal
- `/read <file>` — inject a local file into the conversation context
- `/run <cmd>` — run a shell command and add its output to context
- `/clear` — wipe conversation history
- `/history` — preview all messages
- `/help` — list commands
- First message automatically includes your CWD and visible files as context

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally
- The model pulled: `ollama pull huihui_ai/qwen3-coder-abliterated:30b`

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

Then just type your coding question. Example session:

```
you  explain this file
/read src/main.rs
you  what does the parse_args function do?
/run cargo check
you  fix the borrow checker error above
```

## Code style

Follows the project's conventions:
- Explicit variable types everywhere
- Variables that belong together are grouped
- Blank line before closing `}` / end of function body
