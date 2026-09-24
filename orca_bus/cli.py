"""orca-bus: one message bus for every terminal tab, whatever runs in it (Claude Code, Codex, a shell).

  orca-bus register <name> [--kind claude|codex|shell] [--handle H] [--session S] [--note N] [--owner NAME]
                    [--mode typed|inbox]
  orca-bus send <to> "<text>" [--from NAME] [--re ID] [--now]
  orca-bus inbox [--unread] [--as NAME] [--json] [--follow]
  orca-bus ack <id> [--as NAME]
  orca-bus who [--json]
  orca-bus status [ID]
  orca-bus deliver [--once] [--interval S] [--alert-after S] [--draft-grace S] [--max-attempts N]

Never type into another agent's tab with a raw `orca terminal send`: it bypasses the per-tab typing lock, and two
writers in one input box concatenate into one prompt. Use `send` (the daemon types it) or `send --now`.

The bus directory is --dir, else $ORCA_BUS_DIR, else the nearest `.orca-bus/` above the current directory.
Your own name is --from/--as, else $ORCA_BUS_NAME, else the registry entry for this terminal's
$ORCA_TERMINAL_HANDLE.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from .store import Bus, BusError


def find_root(arg):
    if arg:
        return Path(arg)
    if os.environ.get('ORCA_BUS_DIR'):
        return Path(os.environ['ORCA_BUS_DIR'])
    here = Path.cwd().resolve()
    for d in [here, *here.parents]:
        if (d / '.orca-bus').is_dir():
            return d / '.orca-bus'
    raise BusError('no bus directory: pass --dir, set ORCA_BUS_DIR, or create .orca-bus/ in the project root')


def whoami(bus, explicit):
    if explicit:
        return explicit
    if os.environ.get('ORCA_BUS_NAME'):
        return os.environ['ORCA_BUS_NAME']
    name = bus.name_for_handle(os.environ.get('ORCA_TERMINAL_HANDLE'))
    if name:
        return name
    h = os.environ.get('ORCA_TERMINAL_HANDLE')
    raise BusError('who are you? register this tab first (`register <name>`), or pass --from/--as'
                   + (f' (this terminal is {h})' if h else ''))


def live_terminals():
    try:
        from .orca import Orca
        return Orca().terminals()
    except Exception as ex:
        print(f'(orca not reachable: {ex})', file=sys.stderr)
        return None


def out(obj, as_json, text):
    print(json.dumps(obj, indent=1) if as_json else text)


def main(argv=None, root=None, default_reply_cmd=None):
    p = argparse.ArgumentParser(prog='orca-bus', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dir', default=root)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('register', help='map a stable name to this (or a given) terminal')
    r.add_argument('name'); r.add_argument('--handle'); r.add_argument('--kind', choices=['claude', 'codex', 'shell'])
    r.add_argument('--session'); r.add_argument('--note')
    r.add_argument('--owner', help='who is alerted when this tab cannot take messages (a registered name)')
    r.add_argument('--mode', choices=['typed', 'inbox'],
                   help='typed (default): the daemon types messages in. inbox: never typed; watch `inbox --follow`')
    u = sub.add_parser('unregister'); u.add_argument('name')
    s = sub.add_parser('send', help='send one line (<= max_len chars) to a registered name')
    s.add_argument('to'); s.add_argument('text'); s.add_argument('--from', dest='frm'); s.add_argument('--re', dest='reply_to')
    s.add_argument('--now', action='store_true',
                   help='try one delivery right here (same lock and checks as the daemon); if it cannot, the daemon keeps it')
    i = sub.add_parser('inbox'); i.add_argument('--unread', action='store_true'); i.add_argument('--as', dest='me')
    i.add_argument('--json', action='store_true'); i.add_argument('--all', action='store_true', help='every message on the bus')
    i.add_argument('--follow', action='store_true',
                   help='print each new message to you as one line, forever (run it under a Monitor); with --unread, '
                        'print the unread ones first')
    a = sub.add_parser('ack'); a.add_argument('ids', nargs='+'); a.add_argument('--as', dest='me')
    w = sub.add_parser('who'); w.add_argument('--json', action='store_true')
    st = sub.add_parser('status'); st.add_argument('id', nargs='?'); st.add_argument('--json', action='store_true')
    d = sub.add_parser('deliver', help='run the delivery daemon (in its own plain terminal)')
    d.add_argument('--once', action='store_true'); d.add_argument('--interval', type=float, default=5)
    d.add_argument('--busy-grace', type=float, default=None,
                   help='ignored (kept for old command lines): a busy agent is never typed into')
    d.add_argument('--max-attempts', type=int, default=5); d.add_argument('--settle', type=float, default=3.0)
    d.add_argument('--alert-after', type=float, default=120,
                   help='seconds a tab may stay blocked (text the bus did not write, unreadable screen) before its '
                        'owner, the notify names and the waiting senders are told')
    d.add_argument('--draft-grace', type=float, default=1800, help='seconds before a blocked message fails')
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # titles carry spinner glyphs; a cp1252 console must not crash on them
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass

    try:
        bus = Bus(find_root(args.dir))
        if default_reply_cmd and not (bus.root / 'config.json').exists():
            (bus.root / 'config.json').write_text(json.dumps({'notify': [], 'reply_cmd': default_reply_cmd}, indent=1))

        if args.cmd == 'register':
            handle = args.handle or os.environ.get('ORCA_TERMINAL_HANDLE')
            kind = args.kind
            if not kind:
                terms = live_terminals() or {}
                agent = (terms.get(handle) or {}).get('agent')
                kind = agent if agent in ('claude', 'codex') else 'shell'
            old = bus.register(args.name, handle, kind, args.session, args.note, args.owner, args.mode)
            moved = f' (was {old.get("handle")})' if old and old.get('handle') != handle else ''
            mode = bus.registry()[args.name]['mode']
            print(f'registered {args.name} -> {handle} [{kind}, {mode}]{moved}')
        elif args.cmd == 'unregister':
            print('removed' if bus.unregister(args.name) else 'not registered')
        elif args.cmd == 'send':
            frm = whoami(bus, args.frm)
            m = bus.send(frm, args.to, args.text, args.reply_to)
            print(f'queued {m["id"]} {frm} -> {args.to}')
            if args.now:
                from .deliver import Deliverer
                from .orca import Orca
                orca = Orca()
                Deliverer(bus, orca, holder=f'send --now by {frm}').deliver(
                    bus.messages()[m['id']], bus.registry().get(args.to), orca.terminals())
                m = bus.messages()[m['id']]
                print(f'{m["id"]} {m["state"]}: {m["history"][-1][2]}')
        elif args.cmd == 'inbox' and args.follow:
            me = whoami(bus, args.me)
            cmd = bus.config().get('reply_cmd', 'orca-bus')
            if args.unread:
                for m in bus.inbox(me, unread=True):
                    print(f'[bus {m["id"]} from {m["from"]}] {m["text"]} || reply: {cmd} send {m["from"]} --re {m["id"]} "..."', flush=True)
            for m in bus.follow(me):
                print(f'[bus {m["id"]} from {m["from"]}] {m["text"]} || reply: {cmd} send {m["from"]} --re {m["id"]} "..."', flush=True)
        elif args.cmd == 'inbox':
            if args.all:
                ms = sorted(bus.messages().values(), key=lambda m: m['time'])
            else:
                ms = bus.inbox(whoami(bus, args.me), args.unread)
            out(ms, args.json, '\n'.join(f'{m["id"]} {m["time"][11:19]} {m["from"]}->{m["to"]} [{m["state"]}] {m["text"]}'
                                          for m in ms) or '(empty)')
        elif args.cmd == 'ack':
            me = whoami(bus, args.me)
            for mid in args.ids:
                bus.ack(me, mid)
            print(f'acked {len(args.ids)}')
        elif args.cmd == 'who':
            reg, terms = bus.registry(), live_terminals()
            rows = []
            for n, e in sorted(reg.items()):
                live = None if terms is None else (e.get('handle') in terms if e.get('handle') else None)
                title = (terms or {}).get(e.get('handle'), {}).get('title', '')
                rows.append({'name': n, **e, 'live': live, 'title': title})
            hb = bus.root / 'deliver.heartbeat'
            age = time.time() - hb.stat().st_mtime if hb.exists() else None
            daemon = 'daemon: ' + ('NOT RUNNING' if age is None or age > 60 else f'alive ({int(age)}s ago)')
            out({'daemon_heartbeat_age_s': age, 'names': rows}, args.json, '\n'.join(
                [daemon] + [f'{r["name"]:<16} {r["kind"]:<6} {"LIVE" if r["live"] else ("DEAD" if r["live"] is False else "?"):<4} '
                            f'{(r["handle"] or "-")[:18]:<18} {r["title"][:40]}' for r in rows]))
        elif args.cmd == 'status':
            ms = bus.messages()
            sel = [ms[args.id]] if args.id else sorted(ms.values(), key=lambda m: m['time'])[-20:]
            out(sel, args.json, '\n'.join(f'{m["id"]} {m["from"]}->{m["to"]} {m["state"]} '
                                          f'({m["history"][-1][2]}) {m["text"][:60]}' for m in sel))
        elif args.cmd == 'deliver':
            from .deliver import run
            from .orca import Orca
            run(bus, Orca(), interval=args.interval, once=args.once, busy_grace=args.busy_grace,
                max_attempts=args.max_attempts, settle=args.settle, alert_after=args.alert_after,
                draft_grace=args.draft_grace)
    except BusError as ex:
        print(f'orca-bus: {ex}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
