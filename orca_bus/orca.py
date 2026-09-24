"""Thin wrapper over the `orca` CLI (terminal list / read / send). Everything the daemon knows about a
tab comes through here, so tests replace this class with a fake."""
import json
import os
import shutil
import subprocess
from pathlib import Path


def find_orca():
    """ORCA_BUS_ORCA, then `orca` on PATH, then the exe Orca advertises to its own terminals, then the
    default per-user install location on Windows."""
    cands = [os.environ.get('ORCA_BUS_ORCA'), shutil.which('orca'), os.environ.get('ORCA_CODEX_LAUNCH_PREFLIGHT')]
    la = os.environ.get('LOCALAPPDATA')
    if la:
        cands.append(str(Path(la) / 'Programs' / 'orca' / 'resources' / 'bin' / 'orca.exe'))
    for c in cands:
        if c and Path(c).exists() and Path(c).name.lower().startswith('orca'):
            return c
    raise RuntimeError('orca CLI not found: set ORCA_BUS_ORCA to its path')


class OrcaError(RuntimeError):
    pass


class Orca:
    def __init__(self, exe=None, timeout=60):
        self.exe = exe or find_orca()
        self.timeout = timeout

    def _run(self, *args):
        env = dict(os.environ, MSYS_NO_PATHCONV='1')
        r = subprocess.run([self.exe, *args, '--json'], capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=env, timeout=self.timeout)
        try:
            d = json.loads(r.stdout)
        except ValueError:
            raise OrcaError(f'orca {args[:2]} rc={r.returncode}: {(r.stdout + r.stderr)[-300:]}')
        if not d.get('ok', False):
            raise OrcaError(f'orca {args[:2]}: {json.dumps(d.get("error", d))[:300]}')
        return d['result']

    def terminals(self):
        """{handle: {'title', 'agent', 'cwd'}} for every live terminal."""
        res = self._run('terminal', 'list')
        out = {}
        for t in res.get('terminals', res if isinstance(res, list) else []):
            out[t['handle']] = {'title': t.get('title') or '', 'agent': t.get('agentIdentity'),
                                'cwd': t.get('worktreePath')}
        return out

    def screen(self, handle):
        """(lines, draft): the rendered screen and the composer text Orca reports (None when empty)."""
        t = self._run('terminal', 'read', '--terminal', handle, '--screen')['terminal']
        return t.get('tail') or [], (t.get('draft') or None)

    def scrollback(self, handle, limit=400):
        t = self._run('terminal', 'read', '--terminal', handle, '--limit', str(limit))['terminal']
        return t.get('tail') or []

    def send(self, handle, text):
        """Type text (or raw keys such as '\\r', '\\t') into a terminal. Never presses Enter itself."""
        res = self._run('terminal', 'send', '--terminal', handle, '--text', text)
        return bool(res.get('send', {}).get('accepted'))
