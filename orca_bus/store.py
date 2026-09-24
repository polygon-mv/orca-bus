"""Durable state of a bus: registry, append-only message log, per-recipient inboxes.

Layout of a bus directory:
    registry.json      stable name -> {handle, kind, session, note, owner, updated}
    locks/<name>.lock  held by whoever is typing into <name>'s tab right now
    log.jsonl          every message and every state change, append-only (the record)
    inbox/<name>.jsonl messages addressed to <name>, append-only (a convenience copy)
    config.json        optional: {"notify": [...], "reply_cmd": "...", "max_len": 600}
    deliver.*          daemon heartbeat / pid / log / STOP files

Every write takes a lock file, so several tabs and the daemon can use one bus at once.
"""
import datetime
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

KINDS = ('claude', 'codex', 'shell')
# typed: the daemon types messages into the tab. inbox: never typed; the tab watches `inbox --follow` itself
MODES = ('typed', 'inbox')
# states: queued -> (delivered | inbox | failed); retry is queued with an attempt count; acked is set by the recipient
OPEN_STATES = ('queued', 'retry')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
DEFAULT_MAX_LEN = 600

# typographic characters are the usual reason a typed message never arrives: map them to ASCII
ASCII_MAP = {
    '—': '-', '–': '-', '−': '-', '‘': "'", '’': "'", '“': '"', '”': '"',
    '…': '...', ' ': ' ', '→': '->', '←': '<-', '×': 'x', '•': '*',
}


class BusError(Exception):
    pass


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def clean_text(text, max_len=DEFAULT_MAX_LEN):
    """Return the text as one safe ASCII line, or raise BusError saying why it cannot be sent."""
    if not text or not text.strip():
        raise BusError('empty message')
    bad = sorted({hex(ord(c)) for c in text if ord(c) < 32 or ord(c) == 127})
    if bad:
        raise BusError(f'control characters {bad} (one line only: put long content in a file and send its path)')
    out = ''.join(ASCII_MAP.get(c, c) for c in text.strip())
    out = ''.join(c if ord(c) < 128 else '?' for c in out)
    if len(out) > max_len:
        raise BusError(f'{len(out)} chars > {max_len}: put the content in a file and send its path')
    return out


def check_name(name):
    if not NAME_RE.match(name or ''):
        raise BusError(f'bad name {name!r}: letters, digits, _ . - (max 64)')
    return name


class Bus:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'inbox').mkdir(exist_ok=True)
        self.reg_path = self.root / 'registry.json'
        self.log_path = self.root / 'log.jsonl'
        self.lock_path = self.root / '.lock'

    # ---- config
    def config(self):
        p = self.root / 'config.json'
        cfg = {'notify': [], 'reply_cmd': 'orca-bus', 'max_len': DEFAULT_MAX_LEN}
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding='utf-8')))
        return cfg

    # ---- locking (portable: an O_EXCL lock file, broken if older than `stale` seconds)
    @staticmethod
    def _try_lock(path, stale, holder=''):
        """Take the lock file at `path` once; True on success. A lock older than `stale` seconds is broken."""
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, (holder or str(os.getpid())).encode())
            os.close(fd)
            return True
        except (FileExistsError, PermissionError):
            # Windows raises PermissionError while another writer's delete of the lock is still pending
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink()
            except (FileNotFoundError, PermissionError):
                pass
            return False

    @staticmethod
    def _unlock(path):
        for _ in range(100):
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:  # a reader has it open for a moment (Windows)
                time.sleep(0.01)

    @contextmanager
    def lock(self, timeout=30, stale=60):
        t0 = time.time()
        while not self._try_lock(self.lock_path, stale):
            if time.time() - t0 > timeout:
                raise BusError(f'bus lock busy for {timeout}s: {self.lock_path}')
            time.sleep(0.05)
        try:
            yield
        finally:
            self._unlock(self.lock_path)

    # ---- per-recipient typing lock
    # Only one writer may type into a tab at a time. Two `terminal send`s into one input box concatenate: the first
    # writer's Enter then submits both texts as one prompt. Every code path that types into a tab (the daemon, a
    # `send --now`, a second daemon started by mistake) takes this lock first and holds it from the screen check to
    # the proof of submission.
    def typing_lock_path(self, name):
        return self.root / 'locks' / f'{check_name(name)}.lock'

    @contextmanager
    def typing_lock(self, name, holder, stale=120):
        """Yield True while holding `name`'s typing lock, False (at once, no waiting) if someone else holds it."""
        path = self.typing_lock_path(name)
        path.parent.mkdir(exist_ok=True)
        got = self._try_lock(path, stale, json.dumps({'holder': holder, 'pid': os.getpid(), 'time': time.time()}))
        try:
            yield got
        finally:
            if got:
                self._unlock(path)

    def typing_lock_holder(self, name):
        try:
            return json.loads(self.typing_lock_path(name).read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, PermissionError):
            return None

    # ---- registry
    def registry(self):
        if not self.reg_path.exists():
            return {}
        return json.loads(self.reg_path.read_text(encoding='utf-8'))

    def _save_registry(self, reg):
        tmp = self.reg_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(reg, indent=1, sort_keys=True), encoding='utf-8')
        os.replace(tmp, self.reg_path)

    def register(self, name, handle=None, kind='shell', session=None, note=None, owner=None, mode=None):
        check_name(name)
        if kind not in KINDS:
            raise BusError(f'kind must be one of {KINDS}')
        if mode not in (None,) + MODES:
            raise BusError(f'mode must be one of {MODES}')
        with self.lock():
            reg = self.registry()
            old = reg.get(name)
            if owner:
                check_name(owner)
            elif old:
                owner = old.get('owner')  # a restarted tab re-registering its name keeps the name's owner
            mode = mode or (old or {}).get('mode') or 'typed'
            reg[name] = {'handle': handle, 'kind': kind, 'session': session, 'note': note, 'owner': owner,
                         'mode': mode, 'updated': now_iso()}
            self._save_registry(reg)
            self._append({'ev': 'register', 'name': name, 'handle': handle, 'kind': kind, 'session': session,
                          'previous_handle': old and old.get('handle'), 'time': now_iso()})
        return old

    def unregister(self, name):
        with self.lock():
            reg = self.registry()
            old = reg.pop(name, None)
            self._save_registry(reg)
            self._append({'ev': 'unregister', 'name': name, 'time': now_iso()})
        return old

    def name_for_handle(self, handle):
        if not handle:
            return None
        names = [n for n, e in self.registry().items() if e.get('handle') == handle]
        return sorted(names)[0] if names else None

    # ---- log
    def _append(self, ev, path=None):
        with open(path or self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(ev, ensure_ascii=True) + '\n')

    def events(self):
        if not self.log_path.exists():
            return []
        out = []
        for line in self.log_path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # a torn last line from a killed writer: skip it, never crash the reader
        return out

    def messages(self):
        """Fold the log into {id: message} with its current state and history."""
        msgs = {}
        for ev in self.events():
            if ev.get('ev') == 'msg':
                m = dict(ev)
                m.pop('ev')
                m.setdefault('attempts', 0)
                m['history'] = [[ev['time'], ev['state'], '']]
                msgs[m['id']] = m
            elif ev.get('ev') == 'state' and ev.get('id') in msgs:
                m = msgs[ev['id']]
                m['state'] = ev['state']
                m['history'].append([ev['time'], ev['state'], ev.get('detail', '')])
                for k in ('attempts', 'next_try', 'waiting_since', 'acked_by'):
                    if k in ev:
                        m[k] = ev[k]
        return msgs

    def send(self, frm, to, text, reply_to=None):
        check_name(to)
        text = clean_text(text, self.config()['max_len'])
        # 8 random hex digits: with 4, two sends in the same second collided often enough to merge two messages
        # into one in the fold (7% of the 100-sends-in-a-second test runs)
        mid = f"m{datetime.datetime.now():%m%d%H%M%S}{secrets.token_hex(4)}"
        msg = {'ev': 'msg', 'id': mid, 'from': frm, 'to': to, 'time': now_iso(), 'text': text,
               'state': 'queued', 'reply_to': reply_to}
        with self.lock():
            known = to in self.registry()
            if not known:
                msg['state'] = 'failed'
            self._append(msg)
            self._append(msg, self.root / 'inbox' / f'{to}.jsonl')
            if not known:
                self._append({'ev': 'state', 'id': mid, 'state': 'failed', 'time': now_iso(),
                              'detail': f'no such recipient {to!r} in the registry'})
            if reply_to:
                self._append({'ev': 'state', 'id': reply_to, 'state': 'acked', 'time': now_iso(),
                              'detail': f'replied by {frm} with {mid}', 'acked_by': frm})
        if not known:
            raise BusError(f'no such recipient {to!r} (logged as failed {mid}); see `who`')
        return {k: v for k, v in msg.items() if k != 'ev'}

    def set_state(self, mid, state, detail='', **extra):
        with self.lock():
            self._append({'ev': 'state', 'id': mid, 'state': state, 'time': now_iso(), 'detail': detail, **extra})

    def inbox(self, name, unread=False):
        out = [m for m in self.messages().values() if m['to'] == name]
        if unread:
            out = [m for m in out if m['state'] not in ('acked',)]
        return sorted(out, key=lambda m: m['time'])

    def follow(self, name, from_start=False, poll=2.0, sleep=time.sleep, stop=None):
        """Yield each message addressed to `name` as it arrives (tails inbox/<name>.jsonl; cheap to poll)."""
        path = self.root / 'inbox' / f'{check_name(name)}.jsonl'
        pos = 0 if from_start or not path.exists() else path.stat().st_size
        buf = b''
        while not (stop and stop()):
            try:
                with open(path, 'rb') as f:
                    f.seek(pos)
                    chunk = f.read()
            except FileNotFoundError:
                chunk = b''
            pos += len(chunk)
            buf += chunk
            *lines, buf = buf.split(b'\n')
            for line in lines:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get('ev') == 'msg':
                    yield ev
            sleep(poll)

    def ack(self, name, mid):
        m = self.messages().get(mid)
        if not m:
            raise BusError(f'no message {mid}')
        if m['to'] != name:
            raise BusError(f'{mid} is addressed to {m["to"]}, not {name}')
        self.set_state(mid, 'acked', f'acked by {name}', acked_by=name)
        return m
