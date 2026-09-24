import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orca_bus import tui  # noqa: E402
from orca_bus.deliver import Deliverer  # noqa: E402
from orca_bus.store import Bus, BusError, clean_text  # noqa: E402
from tests.fakes import FakeOrca, FakeTerm  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(tmp, **kw):
    bus = Bus(tmp)
    orca, clock = FakeOrca(), Clock()
    d = Deliverer(bus, orca, sleep=clock.sleep, clock=clock, **kw)
    return bus, orca, clock, d


def run_until_settled(d, clock, bus, mid, ticks=40):
    for _ in range(ticks):
        d.tick()
        if bus.messages()[mid]['state'] not in ('queued', 'retry'):
            break
        clock.t += 20
    return bus.messages()[mid]


class TextTests(unittest.TestCase):
    def test_typography_becomes_ascii(self):
        self.assertEqual(clean_text('a — “b” ’c'), 'a - "b" \'c')

    def test_control_chars_and_length_rejected(self):
        with self.assertRaises(BusError):
            clean_text('two\nlines')
        with self.assertRaises(BusError):
            clean_text('x' * 601)
        with self.assertRaises(BusError):
            clean_text('   ')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bus = Bus(self.tmp)

    def test_send_inbox_ack_reply(self):
        self.bus.register('lead', 'term_a', 'claude')
        self.bus.register('helper', 'term_b', 'codex')
        m = self.bus.send('helper', 'lead', 'hello')
        self.assertEqual([x['id'] for x in self.bus.inbox('lead', unread=True)], [m['id']])
        r = self.bus.send('lead', 'helper', 'hi back', reply_to=m['id'])  # a reply acks the original
        self.assertEqual(self.bus.messages()[m['id']]['state'], 'acked')
        self.bus.ack('helper', r['id'])
        self.assertEqual(self.bus.inbox('helper', unread=True), [])
        with self.assertRaises(BusError):
            self.bus.ack('lead', r['id'])  # not addressed to lead
        self.assertTrue((Path(self.tmp) / 'inbox' / 'lead.jsonl').exists())

    def test_unknown_recipient_fails_loudly_and_is_logged(self):
        with self.assertRaises(BusError):
            self.bus.send('x', 'nobody', 'hi')
        self.assertEqual([m['state'] for m in self.bus.messages().values()], ['failed'])

    def test_register_follows_a_restart(self):
        self.bus.register('coordinator', 'term_old', 'claude')
        old = self.bus.register('coordinator', 'term_new', 'claude')
        self.assertEqual(old['handle'], 'term_old')
        self.assertEqual(self.bus.name_for_handle('term_new'), 'coordinator')
        self.assertIsNone(self.bus.name_for_handle('term_old'))

    def test_concurrent_writers_lose_nothing(self):
        self.bus.register('t', None, 'shell')

        def w(k):
            for i in range(25):
                self.bus.send(f's{k}', 't', f'{k}-{i}')
        ts = [threading.Thread(target=w, args=(k,)) for k in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.bus.messages()), 100)

    def test_torn_log_line_is_skipped(self):
        self.bus.register('t', None, 'shell')
        self.bus.send('a', 't', 'one')
        with open(self.bus.log_path, 'a') as f:
            f.write('{"ev": "msg", "id": "trunc')
        self.assertEqual(len(self.bus.messages()), 1)


class TuiTests(unittest.TestCase):
    def test_claude_screens(self):
        idle = ['❯ [bus m1 from p] hi', '● PONG', '✻ Cogitated for 4s · done', '─' * 9, '❯', '─' * 9]
        self.assertFalse(tui.is_busy('claude', idle, '✳ Claude Code'))
        self.assertEqual(tui.claude_input(idle), '')
        busy = idle[:-3] + ['✽ Considering… (50m 29s · ↓ 213.4k tokens)', '─' * 9, '❯', '─' * 9]
        self.assertTrue(tui.is_busy('claude', busy, 'Task 7'))
        self.assertTrue(tui.is_busy('claude', idle, '◐ Task 7'))
        self.assertTrue(tui.is_ready('claude', idle))
        self.assertFalse(tui.is_ready('claude', ['Welcome']))

    def test_claude_ghost_suggestion_is_not_a_draft(self):
        # captured 2026-09: Orca reports Claude's ghost suggestion as `draft`; only the footer hint differs
        rule = '─' * 9
        empty = ['✻ Cogitated for 1m 47s', rule, '❯', rule,
                 '  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents']
        typed = empty[:-1] + ['  ⏵⏵ bypass permissions on (shift+tab to cycle)']
        self.assertEqual(tui.composer_text('claude', empty, 'Show test results'), '')
        self.assertEqual(tui.composer_text('claude', typed, 'abc'), 'abc')
        self.assertEqual(tui.composer_text('codex', empty, 'abc'), 'abc')

    def test_codex_screens(self):
        idle = ['model: gpt   /model to change', '  › Ask Codex to do anything   model default']
        self.assertEqual(tui.codex_input(idle), '')
        self.assertTrue(tui.is_ready('codex', idle))
        self.assertFalse(tui.is_ready('codex', ['model:       loading   /model to change'] + idle[1:]))
        self.assertEqual(tui.codex_input(['› [bus m9 from a] hi']), '[bus m9 from a] hi')
        self.assertTrue(tui.is_busy('codex', ['• Working (8s • esc to interrupt)', '› x']))
        self.assertTrue(tui.queue_hint(['  tab to queue message']))


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_claude_idle_delivered_with_proof(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_c', FakeTerm('claude'), '✳ idle')
        bus.register('c', 'term_c', 'claude')
        m = bus.send('me', 'c', 'hello there')
        self.assertEqual(run_until_settled(d, clock, bus, m['id'])['state'], 'delivered')
        self.assertIn(m['id'], t.history[0])
        self.assertEqual(t.keys[1], '\r')  # text and Enter are separate writes

    def test_codex_swallowed_enter_is_resent(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_x', FakeTerm('codex', swallow=1, report_draft=False))
        bus.register('x', 'term_x', 'codex')
        m = bus.send('me', 'x', 'look at this')
        d.tick()  # within ONE attempt: the second CR goes out while the text still sits in the box
        self.assertEqual(bus.messages()[m['id']]['state'], 'delivered')
        self.assertEqual(len(t.history), 1)
        self.assertEqual(t.keys.count('\r'), 2)

    def test_busy_waits_then_delivers_at_next_idle(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_c', FakeTerm('claude', busy=True))
        bus.register('c', 'term_c', 'claude')
        m = bus.send('me', 'c', 'when you are free')
        for _ in range(5):
            d.tick(); clock.t += 20
        self.assertEqual(bus.messages()[m['id']]['state'], 'queued')
        self.assertEqual(t.keys, [])  # never typed into a busy agent
        t.busy = False
        self.assertEqual(run_until_settled(d, clock, bus, m['id'])['state'], 'delivered')

    def test_busy_grace_uses_native_queue(self):
        bus, orca, clock, d = make(self.tmp, busy_grace=60)
        t = orca.add('term_x', FakeTerm('codex', busy=True))
        bus.register('x', 'term_x', 'codex')
        m = bus.send('me', 'x', 'queue me')
        r = run_until_settled(d, clock, bus, m['id'])
        self.assertEqual(r['state'], 'delivered')
        self.assertIn('queued', r['history'][-1][2])
        self.assertIn('\t', t.keys)  # Codex queues with Tab, not Enter

    def test_booting_tab_waits(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_x', FakeTerm('codex', booting=True))
        bus.register('x', 'term_x', 'codex')
        m = bus.send('me', 'x', 'first task')
        d.tick(); clock.t += 20; d.tick()
        self.assertEqual(t.keys, [])
        t.booting = False
        self.assertEqual(run_until_settled(d, clock, bus, m['id'])['state'], 'delivered')

    def test_someone_typing_is_not_clobbered(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_c', FakeTerm('claude', composer='half a thought'))
        bus.register('c', 'term_c', 'claude')
        m = bus.send('me', 'c', 'hi')
        d.tick()
        self.assertEqual(t.composer, 'half a thought')
        self.assertEqual(bus.messages()[m['id']]['state'], 'queued')

    def test_closed_tab_fails_loudly_and_notifies(self):
        bus, orca, clock, d = make(self.tmp)
        orca.add('term_me', FakeTerm('shell'))
        bus.register('me', 'term_me', 'shell')
        bus.register('gone', 'term_dead', 'claude')
        (Path(self.tmp) / 'config.json').write_text(json.dumps({'notify': ['boss']}))
        bus.register('boss', None, 'shell')
        m = bus.send('me', 'gone', 'anyone there?')
        d.tick()
        self.assertEqual(bus.messages()[m['id']]['state'], 'failed')
        notes = [x for x in bus.messages().values() if x['from'] == 'bus']
        self.assertEqual(sorted(x['to'] for x in notes), ['boss', 'me'])

    def test_unprovable_send_retries_then_fails(self):
        bus, orca, clock, d = make(self.tmp, max_attempts=3)
        orca.add('term_x', FakeTerm('codex', swallow=99))
        bus.register('x', 'term_x', 'codex')
        m = bus.send('me', 'x', 'never lands')
        r = run_until_settled(d, clock, bus, m['id'], ticks=200)
        self.assertEqual(r['state'], 'failed')
        self.assertEqual(r['attempts'], 2)

    def test_fifo_per_recipient_and_no_duplicates(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_c', FakeTerm('claude'))
        bus.register('c', 'term_c', 'claude')
        ids = [bus.send('me', 'c', f'n{i}')['id'] for i in range(3)]
        for _ in range(10):
            d.tick(); clock.t += 20
        self.assertEqual([h.split()[1] for h in t.history], ids)

    def test_already_delivered_is_recognised_after_a_crash(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_c', FakeTerm('claude'))
        bus.register('c', 'term_c', 'claude')
        m = bus.send('me', 'c', 'x')
        t.history.append(d.format(m))  # it arrived, but the daemon died before recording it
        d.tick()
        self.assertEqual(bus.messages()[m['id']]['state'], 'delivered')
        self.assertEqual(t.keys, [])

    def test_shell_is_inbox_only(self):
        bus, orca, clock, d = make(self.tmp)
        t = orca.add('term_s', FakeTerm('shell'))
        bus.register('s', 'term_s', 'shell')
        m = bus.send('me', 's', 'fyi')
        d.tick()
        self.assertEqual(bus.messages()[m['id']]['state'], 'inbox')
        self.assertEqual(t.keys, [])


class CliTests(unittest.TestCase):
    def test_cli_round_trip(self):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, ORCA_BUS_DIR=tmp, ORCA_TERMINAL_HANDLE='term_cli')
        env.pop('ORCA_BUS_NAME', None)
        root = str(Path(__file__).resolve().parents[1])

        def run(*a):
            return subprocess.run([sys.executable, '-m', 'orca_bus', *a], cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(run('register', 'alice', '--kind', 'shell').returncode, 0)
        self.assertEqual(run('register', 'bob', '--kind', 'shell', '--handle', 'term_bob').returncode, 0)
        r = run('send', 'bob', 'hi bob')  # sender resolved from ORCA_TERMINAL_HANDLE
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('alice -> bob', r.stdout)
        self.assertEqual(run('send', 'carol', 'hi').returncode, 2)
        self.assertIn('hi bob', run('inbox', '--as', 'bob').stdout)


if __name__ == '__main__':
    unittest.main()
