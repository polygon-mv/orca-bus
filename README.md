# orca-bus

A small message bus for the terminal tabs of an Orca workspace, whatever runs in
them: Claude Code, Codex, or a plain shell. Every tab sends and replies the same way:

```
orca-bus send coordinator "build is green, report in reports/12.md"
```

and the message is **typed into the recipient's agent TUI and proven to have arrived**, or it fails loudly.

## What it solves

When several AI agents work in parallel tabs, they have to talk to each other and to a coordinating tab. In practice:

- **Names change.** A session name or tab handle changes on every restart, and messages go nowhere.
- **No common channel.** Claude-to-Claude messaging does not reach Codex; Codex has no inbound channel except its
  input box, and replies by appending to some shared file.
- **Typing into a TUI is unreliable.** Text sent while the TUI boots is lost; Enter is sometimes swallowed and the text
  sits as an unsubmitted draft; a busy Codex does not queue on Enter; a screen read can be stale.
- **Nothing is durable.** Nobody can tell whether a message arrived, crossed another, or was answered twice.

orca-bus fixes each one:

| Problem | orca-bus |
|---|---|
| names change | a **registry** maps stable role names (`coordinator`, `builder`, `reviewer-2`) to the current Orca terminal handle; a restarted tab just runs `register <name>` again |
| no common channel | one CLI for everyone; the **delivery daemon** types the message into Claude or Codex, a shell gets an inbox |
| unreliable typing | waits for the TUI to finish booting and go idle, never types over someone else's draft, sends text and Enter as separate writes, re-sends Enter while the text is still a draft, and marks `delivered` only on **proof** |
| not durable | every message and every state change is appended to `log.jsonl`; `status` shows where each one is |

### Why not Orca's own orchestration mailbox?

Orca ships `orca orchestration send/check/ask/reply`, a durable mailbox built for **dispatched workers**. A worker
started with `orchestration worker-start` gets an injected preamble telling it to poll `check`. For an ordinary tab
(an agent you started yourself), `orchestration send --to <handle>` stores the message
(`delivered_at: null`, warning `legacy_terminal_recipient`) but nothing appears in the recipient's TUI, so an idle agent
never sees it. orca-bus is for those ordinary tabs. It uses Orca only for what Orca does reliably: listing terminals
(`terminal list`, including each tab's `agentIdentity`), reading the rendered screen and composer draft
(`terminal read --screen`), and writing bytes (`terminal send`).

## Install

Python 3.9+, no dependencies. The `orca` CLI must be reachable: `ORCA_BUS_ORCA`, then `orca` on `PATH`, then the path
Orca exports to its own terminals, then the default per-user install on Windows.

```
pip install git+https://github.com/polygon-mv/orca-bus
# or, without installing:  python -m orca_bus ...   (from a checkout)
```

Choose a bus directory shared by all tabs: `--dir`, or `ORCA_BUS_DIR`, or a `.orca-bus/` directory in the project root
(found by walking up from the current directory). Optional `config.json` in it:

```json
{ "notify": ["coordinator"], "reply_cmd": "orca-bus", "max_len": 600 }
```

`notify` names are told about every failed delivery (the sender always is). `reply_cmd` is what the delivered text
tells the recipient to run to reply. A project wrapper script can put its own command there.

## CLI

```
orca-bus register <name> [--kind claude|codex|shell] [--handle H] [--session S] [--note N]
orca-bus send <to> "<one line>" [--from NAME] [--re ID]
orca-bus inbox [--unread] [--as NAME] [--all] [--json]
orca-bus ack <id>... [--as NAME]
orca-bus who [--json]                  # names, kind, live/dead, tab title, daemon heartbeat
orca-bus status [ID] [--json]          # the last 20 messages, or one, with state and reason
orca-bus deliver [--once] [--interval 5] [--busy-grace S] [--max-attempts 5]
orca-bus unregister <name>
```

- **Who you are** comes from `--from/--as`, else `ORCA_BUS_NAME`, else the registry entry for this terminal's
  `ORCA_TERMINAL_HANDLE` (Orca sets it in every terminal it starts, and agents inherit it). So after
  `register` a tab never has to name itself again.
- `register` without `--handle` registers the current terminal; without `--kind` it asks Orca what agent runs there.
- **Text** is one line of at most `max_len` characters. Control characters are refused. Typographic characters (dashes,
  curly quotes, ellipses) become ASCII, because non-ASCII text is a common reason a typed message silently never
  arrives. Long content goes in a file, and the message carries its path.
- `send --re <id>` replies to a message and acks it. A message to an unknown name fails at once (exit code 2) and
  is still logged.

What the recipient sees in its input box, submitted as a prompt:

```
[bus m0924134747e040 from builder] build is green, report in reports/12.md || reply: orca-bus send builder --re m0924134747e040 "..."
```

## The delivery daemon

Run `orca-bus deliver` in its own plain terminal (no agent, no tokens). It stops cleanly when `<bus>/deliver.STOP`
exists, keeps a heartbeat that `who` reports, and refuses to start while another daemon's heartbeat is fresh.
Messages to one recipient go strictly in order. For each open message:

| recipient | what happens |
|---|---|
| unregistered name | `failed` |
| `shell` kind (or no handle) | `inbox`: nothing is typed, read it with `inbox` |
| handle not in `orca terminal list` | `failed`: "tab closed", sender and `notify` names told |
| TUI still booting | wait |
| input box holds someone else's text | wait; never typed over; `failed` after 30 min |
| agent busy | wait for its next idle (see `--busy-grace`) |
| idle and empty | type the text, pause, send CR as a separate write, check; up to 3 CRs while the text is still in the box |
| no proof | `retry` with backoff 15 s, 30 s, 60 s...; `failed` after `--max-attempts`, with a notice |

**Proof of delivery** means the composer no longer holds the text and the message id appears in the terminal output.
Before every attempt the daemon checks whether the id is already on screen, so a daemon that died mid-delivery never
sends a duplicate.

`--busy-grace S`: after S seconds of waiting on a busy agent, hand the message to the agent's **own queue**: Enter
while Claude Code is working (it shows the message to the agent at its next step, between tool calls), Tab while
Codex is working (Codex's "tab to queue message"). Neither interrupts a running tool. Without it the daemon waits
for a real idle, which can be hours for a long-running agent.

### TUI quirks it handles (Claude Code 2.1, Codex 0.156)

- **Claude busy vs idle:** Orca's tab title starts with a spinner glyph while Claude works and U+2733 when idle; the
  screen shows a `<Verb>... (12s` spinner line.
- **Claude's ghost prompt suggestion** is reported by Orca as `draft`, exactly like typed text. The footer tells them
  apart: `<- for agents` is shown only while the input is really empty. Typing replaces the suggestion.
  (`ORCA_BUS_CLAUDE_EMPTY_HINT` overrides the regex if a future version changes the footer.)
- **Codex busy:** `Working (8s - esc to interrupt)`. Mid-turn, Enter does **not** queue; only Tab does.
- **Codex swallows Enter** now and then, leaving the text as a draft: a bare `--enter` does not submit it, a raw CR
  does. The daemon sends text and CR separately and re-sends CR while the text is still in the box.
- **Booting:** a Codex that still shows `model: loading` or a Claude with no prompt line yet is not ready. Text sent
  then is lost or left unsubmitted.
- **Stale screens:** proof is read from `terminal read --screen` plus the scrollback, never inferred from a send
  receipt, since `input_accepted` does not mean the prompt was submitted.

## Files

```
<bus>/registry.json        name -> {handle, kind, session, note, updated}
<bus>/log.jsonl            the durable record: every message and state change
<bus>/inbox/<name>.jsonl   copy of every message addressed to <name>
<bus>/deliver.log          what the daemon did, one line per action
```

Writes take a lock file (safe on Windows and POSIX), and a torn last log line from a killed writer is skipped.

## Tests

```
python -m unittest discover -s tests -t .
```

The delivery logic runs against a fake Orca whose terminals reproduce the quirks above: swallowed Enter, Codex's
Tab-only queue, booting, busy, someone else's draft, a closed tab, a Claude ghost suggestion, a daemon that died
after delivering. Store tests cover concurrent writers, replies, acks and restarts.

## Licence

MIT
