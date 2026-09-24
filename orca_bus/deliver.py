"""The delivery daemon: types queued messages into the recipient's agent TUI and marks them delivered only
on proof (the composer is empty again and the message id is in the terminal output).

Per recipient kind:
  shell   inbox only (nothing is typed into a plain terminal)
  claude  wait until idle and the input box is empty, type the text, check it landed intact, send a separate CR
  codex   same; Codex 0.15x can swallow the Enter or leave the text as a draft, so re-send CR

Rules every typed delivery follows (brief: two writers garbled one input box):
  - ONE WRITER PER TAB. The recipient's typing lock (store.Bus.typing_lock) is held from the first screen read to
    the proof of submission. Anyone else who would type into that tab (a `send --now`, a second daemon) waits.
    Two unlocked `terminal send`s into one box concatenate, and one Enter submits both as a single prompt.
  - NEVER TYPE INTO A BUSY AGENT. Busy means wait for its next idle, however long. Text typed mid-turn sits in the
    input box as an unsent draft (and the next writer sees "someone else's text").
  - EMPTY BEFORE, INTACT AFTER. The input box must be empty before typing; after typing, it must hold exactly our
    text (and the agent must still be idle) before Enter is pressed. If it does not, our text is taken back out
    when that can be done without touching anyone else's, and Enter is never pressed on a mix.
  - A STALE SCREEN COPY IS NOT A BLOCKER. Orca's copy of a restored, hidden tab's screen can stay at 80x24 while the
    pane is wider; its rows are then garbage and its `draft` reports the ghost suggestion plus footer pieces as typed
    text. Such a Claude tab is delivered to "blind": idle/busy from the tab title (not the screen), the text typed,
    then kept only if the input row now STARTS with it (typing replaces the suggestion; anything already in the box
    would come first), and proof from Claude's transcript. Proof from the transcript is used for every Claude tab.
  - SAY SO, DON'T WAIT SILENTLY. When a tab's input box holds text the bus did not write, or its screen cannot be
    read, for `alert_after` seconds, the recipient's owner, the `notify` names and the waiting senders are told once,
    with the reason and the remedy. The message itself fails after `draft_grace`.
"""
import datetime
import json
import os
import time

from . import transcript, tui
from .store import OPEN_STATES

DEL = '\x7f'  # what Backspace sends in Claude Code and Codex

REMEDY = {
    'foreign': 'Someone has to look at that tab and submit or clear its input box (its owner, or a person). '
               'The bus never clears text it did not write.',
    'mixed': 'The box holds bus text mixed with other text and Enter was NOT pressed. Clear the input box in that tab '
             '(Esc or Ctrl+U) or have its owner do it; the bus retries on its own afterwards.',
    'desync': "Orca's copy of that tab's screen is out of sync with the pane (it starts at 80x24 when a tab is "
              "restored before its size is known) and its title shows neither idle nor busy, so the bus cannot tell "
              "when to type. Showing the tab and resizing its pane once fixes the copy; no typing is needed.",
}


class Deliverer:
    def __init__(self, bus, orca, busy_grace=None, draft_grace=1800, alert_after=120, max_attempts=5, backoff=15,
                 settle=3.0, poll=10, holder='daemon', sleep=time.sleep, clock=time.time, log=None, has_prompt=None):
        self.bus, self.orca = bus, orca
        self.busy_grace = busy_grace  # kept for old command lines; a busy agent is never typed into any more
        self.draft_grace, self.alert_after = draft_grace, alert_after
        self.max_attempts, self.backoff, self.settle, self.poll = max_attempts, backoff, settle, poll
        self.holder = holder
        self.sleep, self.clock = sleep, clock
        self.log = log or (lambda s: None)
        self.stuck = {}  # recipient -> {'cls', 'since', 'alerted'}: one alert per blocked episode
        # has_prompt(token, since): did a Claude session receive this prompt? (its transcript; tests inject a fake)
        self.has_prompt = has_prompt or transcript.claude_has_prompt

    # ------------------------------------------------------------------ formatting
    def format(self, m):
        cmd = self.bus.config().get('reply_cmd', 'orca-bus')
        return f'[bus {m["id"]} from {m["from"]}] {m["text"]} || reply: {cmd} send {m["from"]} --re {m["id"]} "..."'

    # ------------------------------------------------------------------ one pass
    def tick(self):
        msgs = [m for m in self.bus.messages().values() if m['state'] in OPEN_STATES]
        if not msgs:
            return 0
        reg = self.bus.registry()
        terms = self.orca.terminals()
        seen, n = set(), 0
        for m in sorted(msgs, key=lambda m: m['time']):
            to = m['to']
            if to in seen:          # FIFO per recipient: never overtake an older message
                continue
            seen.add(to)
            if m.get('next_try', 0) > self.clock():
                continue
            try:
                self.deliver(m, reg.get(to), terms)
            except Exception as ex:  # an Orca hiccup is a retry, never a crash of the daemon
                self.retry(m, f'error: {ex}')
            n += 1
        return n

    # ------------------------------------------------------------------ outcomes
    def later(self, m, why, since=None):
        since = since or m.get('waiting_since') or self.clock()
        self.bus.set_state(m['id'], 'queued', why, next_try=self.clock() + self.poll, waiting_since=since,
                           attempts=m.get('attempts', 0))
        if why != (m['history'][-1][2] if m.get('history') else ''):
            self.log(f'WAIT {m["id"]} -> {m["to"]}: {why}')

    def retry(self, m, why):
        a = m.get('attempts', 0) + 1
        if a >= self.max_attempts:
            return self.fail(m, f'{why} (after {a} attempts)')
        self.bus.set_state(m['id'], 'retry', why, attempts=a, next_try=self.clock() + self.backoff * 2 ** (a - 1))
        self.log(f'RETRY {m["id"]} -> {m["to"]} #{a}: {why}')

    def fail(self, m, why):
        self.bus.set_state(m['id'], 'failed', why, attempts=m.get('attempts', 0))
        self.log(f'FAILED {m["id"]} -> {m["to"]}: {why}')
        if m['from'] == 'bus':
            return
        self.notify([m['from']], m['to'], f'FAILED {m["id"]} to {m["to"]}: {why}. Text: {m["text"][:200]}')

    def done(self, m, how):
        self.stuck.pop(m['to'], None)
        self.bus.set_state(m['id'], 'delivered', how, attempts=m.get('attempts', 0) + 1)
        self.log(f'DELIVERED {m["id"]} -> {m["to"]}: {how}')

    def notify(self, first, about, text):
        """Tell `first` + the recipient's owner + the configured notify names, never the blocked tab itself."""
        reg = self.bus.registry()
        owner = (reg.get(about) or {}).get('owner')
        tell = list(first) + ([owner] if owner else []) + list(self.bus.config().get('notify', []))
        for name in dict.fromkeys(tell):
            if name in reg and name not in (about, 'bus'):
                try:
                    self.bus.send('bus', name, text[:590])
                except Exception as ex:
                    self.log(f'NOTIFY {name} failed: {ex}')

    def blocked(self, m, cls, why, since, now_alert=False):
        """The tab cannot take a message for a reason a person has to fix: wait, alert once, fail at draft_grace."""
        to, now = m['to'], self.clock()
        st = self.stuck.get(to)
        if not st or st['cls'] != cls:
            st = self.stuck[to] = {'cls': cls, 'since': now, 'alerted': False}
        if not st['alerted'] and (now_alert or now - st['since'] >= self.alert_after):
            st['alerted'] = True
            waiting = sorted((x for x in self.bus.messages().values() if x['to'] == to and x['state'] in OPEN_STATES),
                             key=lambda x: x['time'])
            senders = [x['from'] for x in waiting]
            self.log(f'ALERT {to}: {why}')
            self.notify(senders, to, f'BLOCKED {to}: deliveries held {int(now - st["since"])}s: {why}. {REMEDY[cls]} '
                                     f'Waiting: {len(waiting)} message(s), oldest {m["id"]} from {m["from"]}.')
        if now - since > self.draft_grace:
            return self.fail(m, f'{why} for {int(now - since)}s')
        return self.later(m, why, since)

    # ------------------------------------------------------------------ the typed delivery
    def proven(self, handle, kind, token, since=0, screen_ok=True):
        """(delivered?, composer text). Claude: its transcript is the proof that holds whatever the screen shows.
        The screen counts only when it is a trustworthy frame and the box no longer holds our text."""
        if kind == 'claude' and self.has_prompt(token, since):
            return True, ''
        if not screen_ok:
            return False, ''
        lines, draft = self.orca.screen(handle)
        comp = tui.composer_text(kind, lines, draft)
        if token in comp:
            return False, comp
        seen = any(token in l for l in lines) or any(token in l for l in self.orca.scrollback(handle))
        return seen, comp

    def deliver(self, m, entry, terms):
        if not entry:
            return self.fail(m, f'{m["to"]} is not registered')
        handle, kind = entry.get('handle'), entry.get('kind', 'shell')
        if not handle or kind == 'shell':
            return self.bus.set_state(m['id'], 'inbox', 'shell recipient: inbox only (read with `inbox`)')
        if handle not in terms:
            return self.fail(m, f'tab closed: handle {handle} is not a live Orca terminal (re-register {m["to"]})')
        since = m.get('waiting_since') or self.clock()
        with self.bus.typing_lock(m['to'], self.holder) as got:
            if not got:
                h = self.bus.typing_lock_holder(m['to']) or {}
                return self.later(m, f'another sender is typing into {m["to"]} ({h.get("holder", "?")}, '
                                     f'pid {h.get("pid", "?")}): waiting for it to finish', since)
            return self._deliver_locked(m, handle, kind, terms[handle].get('title', ''), since)

    def _deliver_locked(self, m, handle, kind, title, since):
        token, text = m['id'], self.format(m)
        lines, draft = self.orca.screen(handle)
        good, why = tui.frame_ok(kind, lines)
        ok, comp = self.proven(handle, kind, token, self.sent_at(m), good)
        if ok:  # already there (a previous attempt worked but the daemon died before recording it)
            return self.done(m, 'found in the transcript or terminal output')
        if kind == 'claude' and not good and tui.title_state(title) is not None:
            return self._deliver_blind(m, handle, title, since, why)
        if not tui.is_ready(kind, lines):
            if kind == 'claude' and self.clock() - since > self.alert_after:
                return self.blocked(m, 'desync', 'no input box on the screen', since)
            return self.later(m, 'TUI still booting', since)
        if not good:
            return self.blocked(m, 'desync', why, since)
        comp = tui.composer_text(kind, lines, draft)
        ours = token in comp
        if comp and not ours:
            return self.blocked(m, 'foreign', f'input box holds text the bus did not write: {comp[:60]!r}', since)
        if ours and not tui.intact(comp, text):
            return self.blocked(m, 'mixed', f'input box holds {token} mixed with other text: {comp[:60]!r}', since,
                                now_alert=True)
        if tui.is_busy(kind, lines, title):
            if ours:  # left by an attempt that raced the agent's turn start: take it back out, it is exactly ours
                self.undo(handle, text)
            self.stuck.pop(m['to'], None)
            return self.later(m, 'recipient busy: waiting for its next idle', since)
        if not ours:
            if not self.orca.send(handle, text):
                return self.retry(m, 'orca did not accept the text')
            self.sleep(0.8)
            # verify before Enter: still idle, and the box holds exactly our text
            lines, draft = self.orca.screen(handle)
            comp = tui.composer_text(kind, lines, draft)
            if tui.is_busy(kind, lines, self.orca.terminals().get(handle, {}).get('title', '')):
                if tui.intact(comp, text):
                    self.undo(handle, text)
                    return self.later(m, 'recipient became busy while the text was typed: took it back out', since)
                return self.blocked(m, 'mixed', f'recipient became busy and the box holds {comp[:60]!r}', since,
                                    now_alert=True)
            if not tui.intact(comp, text):
                if tui._squash(comp).endswith(tui._squash(text)) and token in comp:
                    self.undo(handle, text)  # someone else's text came first: remove ours, leave theirs untouched
                    return self.blocked(m, 'foreign', 'someone else typed into the box at the same moment: '
                                                      'took our text back out', since)
                if token not in comp:
                    return self.retry(m, 'the typed text did not appear in the input box')
                return self.blocked(m, 'mixed', f'typed text did not land intact: {comp[:60]!r}', since, now_alert=True)
        t0 = self.clock()
        for i in range(3):  # Codex swallows an Enter now and then: re-send CR while the text is still a draft
            self.orca.send(handle, '\r')
            self.sleep(self.settle)
            ok, comp = self.proven(handle, kind, token, t0)
            if ok:
                return self.done(m, 'submitted' + (f' after {i + 1} keys' if i else ''))
            if token not in comp:
                break
        return self.retry(m, 'no proof of submission' + (' (text still in the input box)' if token in comp else ''))

    def _deliver_blind(self, m, handle, title, since, why):
        """Claude tab whose screen copy cannot be read (see the module docstring)."""
        token, text = m['id'], self.format(m)
        if tui.title_state(title) == 'busy':
            self.stuck.pop(m['to'], None)
            return self.later(m, f'recipient busy: waiting for its next idle ({why}; reading the title)', since)
        t0 = self.clock()
        if not self.orca.send(handle, text):
            return self.retry(m, 'orca did not accept the text')
        self.sleep(0.8)
        lines, draft = self.orca.screen(handle)
        landed = tui.input_row_starts_with(lines, draft, f'[bus {token} from ')
        if tui.title_state(self.orca.terminals().get(handle, {}).get('title', '')) != 'idle':
            self.undo(handle, text)
            return self.later(m, 'recipient became busy while the text was typed: took it back out', since)
        if not landed:  # the box held something else, or the keys went to a dialog: ours is last, take it back
            self.log(f'BLIND {token} -> {m["to"]}: not at the start of an input row; prompt rows '
                     f'{[r[:40] for r in tui.prompt_rows(lines)][-3:]!r}, draft {(draft or "")[:40]!r}')
            self.undo(handle, text)
            return self.blocked(m, 'foreign', f'{why}, and the typed text did not start the input row (it held other '
                                              f'text, or a dialog was open): took it back out', since)
        for i in range(3):
            self.orca.send(handle, '\r')
            self.sleep(self.settle)
            if self.has_prompt(token, t0):
                return self.done(m, f'submitted blind ({why}); proof: transcript' + (f' after {i + 1} keys' if i else ''))
            if tui.title_state(self.orca.terminals().get(handle, {}).get('title', '')) == 'busy':
                break  # the turn started: Enter was taken, the transcript line follows
        self.sleep(self.settle)
        if self.has_prompt(token, t0):
            return self.done(m, f'submitted blind ({why}); proof: transcript')
        return self.retry(m, f'no proof of submission (blind: {why})')

    def sent_at(self, m):
        try:
            return datetime.datetime.fromisoformat(m['time']).timestamp()
        except (KeyError, ValueError):
            return 0

    def undo(self, handle, text):
        """Delete our own text from the end of the input box (only ever called when the box ends with exactly it)."""
        self.orca.send(handle, DEL * len(text))
        self.sleep(0.5)


def run(bus, orca, interval=5, once=False, **kw):
    root = bus.root
    logf = open(root / 'deliver.log', 'a', encoding='utf-8')

    def log(s):
        line = f'{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {s}'
        print(line, flush=True)
        logf.write(line + '\n')
        logf.flush()

    pidf = root / 'deliver.pid'
    if pidf.exists() and not once:
        try:
            pid, t = json.loads(pidf.read_text())['pid'], (root / 'deliver.heartbeat').stat().st_mtime
            if time.time() - t < 60 and pid != os.getpid():
                raise SystemExit(f'another daemon (pid {pid}) is alive: heartbeat {int(time.time() - t)}s ago')
        except (FileNotFoundError, ValueError, KeyError):
            pass
    pidf.write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}))
    d = Deliverer(bus, orca, log=log, **kw)
    stop = root / 'deliver.STOP'
    log(f'START bus={root} alert_after={d.alert_after} draft_grace={d.draft_grace} max_attempts={d.max_attempts}'
        + (' (--busy-grace is ignored: a busy agent is never typed into)' if d.busy_grace is not None else ''))
    while not stop.exists():
        (root / 'deliver.heartbeat').write_text(str(time.time()))
        try:
            d.tick()
        except Exception as ex:
            log(f'TICK ERROR {ex}')
        if once:
            break
        time.sleep(interval)
    pidf.unlink(missing_ok=True)  # a clean stop frees the slot at once for the next daemon
    log('STOP')
