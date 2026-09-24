"""Proof of delivery that does not depend on Orca's screen copy: Claude Code writes every submitted prompt to its
session transcript (<claude home>/projects/<cwd slug>/<session>.jsonl) as a `user` entry. The bus's text starts with
"[bus <id> from ", which no other entry carries (a sender's tool output shows the id, never that prefix)."""
import os
import time
from pathlib import Path


def claude_projects():
    env = os.environ.get('ORCA_BUS_CLAUDE_PROJECTS')
    if env:
        return Path(env)
    home = os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home() / '.claude')
    return Path(home) / 'projects'


def claude_has_prompt(token, since, root=None, tail_bytes=1 << 20):
    """True if a Claude transcript written to since `since` (epoch s) holds a submitted prompt starting with our text."""
    root = Path(root) if root else claude_projects()
    needle = f'[bus {token} from '
    try:
        files = [f for f in root.glob('*/*.jsonl') if f.stat().st_mtime >= since - 5]
    except OSError:
        return False
    for f in files:
        try:
            with open(f, 'rb') as fh:
                size = fh.seek(0, 2)
                fh.seek(max(0, size - tail_bytes))
                data = fh.read().decode('utf-8', 'replace')
        except OSError:
            continue
        if needle not in data:
            continue
        for line in data.splitlines():
            if needle in line and '"type":"user"' in line and 'tool_result' not in line:
                return True
    return False
