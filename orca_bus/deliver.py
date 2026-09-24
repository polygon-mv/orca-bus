"""The delivery daemon: types queued messages into the recipient's agent TUI and marks them delivered only
on proof (the composer is empty again and the message id is in the terminal output).

Per recipient kind:
  shell   inbox only (nothing is typed into a plain terminal)
  claude  wait until idle and the input box is empty, type the text, send a separate CR
  codex   same; Codex 0.15x can swallow the Enter or leave the text as a draft, so re-send CR;
          mid-turn, Enter does not queue in Codex but Tab does
A busy agent is never interrupted: the message waits for the next idle. With --busy-grace N, after N seconds
of waiting it is handed to the agent's own queue (Claude: Enter while busy; Codex: Tab), which also never
interrupts a running tool.
"""
import datetime
import json
import os
import time

from . import tui
from .store import OPEN_STATES


class Deliverer:
    def __init__(self, bus, orca, busy_grace=None, draft_grace=1800, max_attempts=5, backoff=15, settle=3.0,
                 poll=10, sleep=time.sleep, clock=time.time, log=None):
        self.bus, self.orca = bus, orca
        self.busy_grace, self.draft_grace = busy_grace, draft_grace
        self.max_attempts, self.backoff, self.settle, self.poll = max_attempts, backoff, settle, poll
        self.sleep, self.clock = sleep, clock
        self.log = log or (lambda s: None)

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
        reg = self.bus.registry()
        tell = [m['from']] + list(self.bus.config().get('notify', []))
        for name in dict.fromkeys(tell):
            if name in reg and name != m['to']:
                try:
                    self.bus.send('bus', name, f'FAILED {m["id"]} to {m["to"]}: {why}. Text: {m["text"][:200]}')
                except Exception as ex:
                    self.log(f'NOTIFY {name} failed: {ex}')

    def done(self, m, how):
        self.bus.set_state(m['id'], 'delivered', how, attempts=m.get('attempts', 0) + 1)
        self.log(f'DELIVERED {m["id"]} -> {m["to"]}: {how}')

    # ------------------------------------------------------------------ the typed delivery
    def proven(self, handle, kind, token):
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
        token = m['id']
        ok, comp = self.proven(handle, kind, token)
        if ok:  # already there (a previous attempt worked but the daemon died before recording it)
            return self.done(m, 'found in terminal output')
        lines, draft = self.orca.screen(handle)
        title = terms[handle].get('title', '')
        comp = tui.composer_text(kind, lines, draft)
        ours = token in comp
        now = self.clock()
        since = m.get('waiting_since') or now
        if not tui.is_ready(kind, lines):
            return self.later(m, 'TUI still booting', since)
        if comp and not ours:
            if now - since > self.draft_grace:
                return self.fail(m, f'input box held other text for {int(now - since)}s: {comp[:60]!r}')
            return self.later(m, 'input box not empty (someone is typing, or a stale draft)', since)
        busy = tui.is_busy(kind, lines, title)
        queue_key = None
        if busy:
            if self.busy_grace is None or now - since < self.busy_grace:
                return self.later(m, 'recipient busy: waiting for its next idle', since)
            queue_key = '\t' if kind == 'codex' and tui.queue_hint(lines) else '\r'
        if not ours:
            if not self.orca.send(handle, self.format(m)):
                return self.retry(m, 'orca did not accept the text')
            self.sleep(0.8)
        submit = queue_key or '\r'
        for i in range(3):  # Codex swallows an Enter now and then: re-send CR while the text is still a draft
            self.orca.send(handle, submit)
            self.sleep(self.settle)
            ok, comp = self.proven(handle, kind, token)
            if ok:
                return self.done(m, ('queued in the agent (busy)' if queue_key else 'submitted') + (f' after {i + 1} keys' if i else ''))
            if token not in comp:
                break
        return self.retry(m, 'no proof of submission' + (' (text still in the input box)' if token in comp else ''))


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
    log(f'START bus={root} busy_grace={d.busy_grace} max_attempts={d.max_attempts}')
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
