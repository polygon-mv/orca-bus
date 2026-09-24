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
| unreliable typing | one writer per tab (a **typing lock**), waits for the TUI to finish booting and go idle, never types into a busy agent or over someone else's draft, checks the whole text landed intact before pressing Enter, re-sends Enter while the text is still a draft, and marks `delivered` only on **proof** |
| a tab silently stops taking messages | its owner, the `notify` names and every waiting sender are **told** after 2 minutes, with the reason and the fix |
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

`notify` names are told about every failed delivery (the sender always is) and about every blocked tab. `reply_cmd` is what the delivered text
tells the recipient to run to reply. A project wrapper script can put its own command there.

## CLI

```
orca-bus register <name> [--kind claude|codex|shell] [--handle H] [--session S] [--note N] [--owner NAME]
                  [--mode typed|inbox]
orca-bus send <to> "<one line>" [--from NAME] [--re ID] [--now]
orca-bus inbox [--unread] [--as NAME] [--all] [--json] [--follow]
orca-bus ack <id>... [--as NAME]
orca-bus who [--json]                  # names, kind, live/dead, tab title, daemon heartbeat
orca-bus status [ID] [--json]          # the last 20 messages, or one, with state and reason
orca-bus deliver [--once] [--interval 5] [--alert-after 120] [--draft-grace 1800] [--max-attempts 5]
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
- `send --now` also tries one delivery right away, from the sending process, under the same lock and checks as the
  daemon (useful when the daemon is down). If it cannot deliver yet, the message stays queued for the daemon.
- `register --owner <name>`: who is alerted when this tab stops taking messages (the tab that started it, say). The
  owner is kept when the tab re-registers after a restart.

### Long-running agents: watch your inbox instead of being typed to

An agent that is almost always mid-turn (a scheduler, a coordinator, a build watcher) only receives typed messages
at its rare idle moments. It can take them from its inbox file instead:

```
orca-bus register scheduler --mode inbox        # the daemon never types into this tab
orca-bus inbox --follow --unread                # one line per message, forever: run it under a Monitor
```

`inbox --follow` prints each new message as the same one line a typed delivery would show, so a Claude Code
Monitor (or any watcher) hands it to the agent between tool calls. The inbox file is append-only and the daemon is
not involved, so a stale screen, a busy agent or a text in the input box cannot hold anything up. `--mode` is kept
when the tab re-registers; `register <name> --mode typed` switches back.

**Never type into another agent's tab with a raw `orca terminal send`.** It skips the typing lock and every check
below. Two writers in one input box do not interleave characters (each `terminal send` is one write), but the
second text is **appended** to the first, and the first writer's Enter submits both as one prompt. A raw send into
a busy tab leaves the text sitting in the box as a draft, and every later delivery then waits on "someone else's
text". Use `send` (or `send --now`).

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
| `shell` kind (or no handle), or registered with `--mode inbox` | `inbox`: nothing is typed, read it with `inbox` / `inbox --follow` |
| handle not in `orca terminal list` | `failed`: "tab closed", sender and `notify` names told |
| another writer holds the tab's typing lock | wait (never two writers in one input box) |
| TUI still booting | wait |
| Claude screen copy out of sync (see below), title says idle | delivered **blind**: type, keep it only if an input row now starts with `[bus <id> from`, Enter, proof from the transcript |
| Claude screen copy out of sync, title says busy | wait for its next idle |
| screen copy out of sync and the title says neither | wait; **alert** after `--alert-after`; `failed` after `--draft-grace` |
| input box holds text the bus did not write | wait, never typed over; **alert** after `--alert-after`; `failed` after `--draft-grace` |
| agent busy | wait for its next idle, however long; **nothing is typed into a busy agent** |
| idle and empty | type the text; check the agent is still idle and the box holds exactly that text; only then send CR as a separate write; up to 3 CRs while the text is still in the box |
| the check after typing fails | the agent started a turn: our text is taken back out (Backspace), wait. Someone else typed first: ours is taken back out, theirs left alone. Anything else: Enter is **not** pressed and the owner and `notify` names are alerted at once |
| no proof | `retry` with backoff 15 s, 30 s, 60 s...; `failed` after `--max-attempts`, with a notice |

**One writer per tab.** Every path that types into a tab (the daemon, `send --now`, a second daemon started by
mistake) first takes `<bus>/locks/<name>.lock` and holds it from the first screen read to the proof of submission. A
lock left by a killed writer is broken after 2 minutes.

**Proof of delivery.** For Claude Code: the prompt `[bus <id> from ...` appears as a `user` entry in a session
transcript (`~/.claude/projects/*/*.jsonl`; set `CLAUDE_CONFIG_DIR` or `ORCA_BUS_CLAUDE_PROJECTS` if yours is
elsewhere). Nothing else writes that prefix: a sender's own transcript shows the id only inside tool output, which
is not counted. Otherwise, and for Codex: the composer no longer holds the text and the id appears in the terminal
output. Before every attempt the daemon checks both, so a daemon that died mid-delivery never sends a duplicate.

**Alerts.** When a tab cannot take messages for a reason only a person can fix (text in its box that the bus did
not write, or a screen that cannot be read), the daemon waits `--alert-after` seconds (a person may be mid-sentence)
and then sends one `BLOCKED` notice, with the reason and the fix, to the tab's owner, the `notify` names and the
sender of every message waiting for that tab. It does not go on waiting silently. There is one notice per episode;
the next one comes only after the tab has taken a message or the reason changes.

`--busy-grace` is accepted for old command lines and ignored. Earlier versions typed into a busy agent's own queue
after that many seconds. That leaves text in the box whenever the queue does not take it, so it was removed.

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
- **A screen copy of the wrong size.** Orca keeps its own copy of each terminal's screen for `terminal read
  --screen`. If a tab is restored before its size is known (after a restart, say), that copy starts at 80x24 and
  is only fixed by a real resize of a pane that is on screen. Claude draws for the real width, so the copy is
  garbage: words at column 0 and stray letters at column 79, and a prompt row where the ghost suggestion is mixed
  with pieces of the footer (`...tonighthell, 1 mon  or st ll  unning`). Orca's `draft` is derived from that same
  copy and reports the mix as typed text. The daemon only trusts a Claude screen that has the input box's shape
  (two rules of the same width around the prompt). Any hidden tab can be in this state, so the daemon does not
  depend on the copy there. It delivers **blind**. Idle and busy come from the tab title, which Claude sets itself
  (U+2733 idle, a spinner glyph busy), not from the copy. It types the text and keeps it only if an input row now
  *starts* with `[bus <id> from`. That id exists nowhere before this send, and anything already in the box would come
  before it. Otherwise it takes its own text back out with Backspace. Proof comes from Claude's transcript. On the
  stale copy the spaces Claude skips over show the rule row underneath (`[bus─m0924...─from─x]`), and the check
  allows for that. Showing the tab and resizing its pane once fixes the copy itself.
- **Long drafts come back soft-wrapped**: Orca's `draft` has a newline where the pane wrapped (at a space, or inside
  a long word). The "landed intact" check compares with all whitespace removed.
- **Claude's input box scrolls.** In a narrow pane a long text fills more lines than the box shows, and Orca's `draft`
  then holds only the visible last lines. Our text ends with its own id (in the reply hint), so a draft that is
  the tail of our text is accepted as ours.
- **Text typed into a busy Claude** is kept as a draft through the turn and is still there afterwards, unsent.

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

The delivery logic runs against a fake Orca whose terminals reproduce the quirks above: swallowed Enter, booting,
busy, someone else's draft, a closed tab, a Claude ghost suggestion, a daemon that died after delivering, a screen
copy of the wrong size, a turn that starts while the text is typed, a person typing at the same moment, and two
writers delivering into one tab at once (from two threads). Store tests cover concurrent writers, the typing lock
(including a stale one), replies, acks and restarts. Each new test was checked by removing the code it guards.

## Licence

MIT
