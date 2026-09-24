"""Reading an agent TUI's screen: is it busy, what sits in its input box. Pure functions, unit-tested
against captured screens (tests/screens)."""
import os
import re

# Claude Code: a working turn shows a spinner line "<glyph> <Verb>... (12s ..." and the Orca tab title
# starts with a spinner glyph; an idle one shows the title glyph U+2733.
CLAUDE_BUSY = re.compile(r'^\s*\S\s+\S.*…\s*\(\s*\d+[hms]')
# Codex: "Working (12s - esc to interrupt)"
CODEX_BUSY = re.compile(r'\bWorking\b.*\(.*\d+s|esc to interrupt', re.I)
SPINNER_TITLE = set('◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
IDLE_TITLE = '✳'
CODEX_PLACEHOLDER = re.compile(r'Ask Codex to do anything|Implement \{feature\}|Find and fix a bug', re.I)


def is_busy(kind, lines, title=''):
    """True when the agent is mid-turn. Looks only at the bottom of the screen, since an old spinner line
    scrolled up in the history is not a live one."""
    title = (title or '').strip()
    bottom = [l for l in lines if l.strip()][-8:]
    if kind == 'claude':
        if title[:1] in SPINNER_TITLE:
            return True
        return any(CLAUDE_BUSY.search(l) for l in bottom) and not title.startswith(IDLE_TITLE)
    if kind == 'codex':
        return any(CODEX_BUSY.search(l) for l in bottom)
    return False


def codex_input(lines):
    """Text in Codex's composer ('' if empty), from the last line carrying its prompt glyph."""
    for l in reversed(lines):
        if '›' in l:
            txt = l.split('›', 1)[1].strip()
            return '' if CODEX_PLACEHOLDER.search(txt) else txt
    return ''


def claude_input(lines):
    """Text in Claude's composer: the prompt line between the two rules at the bottom of the screen."""
    for l in reversed(lines):
        s = l.strip()
        if s.startswith('❯'):
            return s[1:].strip()
    return ''


# Claude Code shows a ghost PROMPT SUGGESTION in an empty input box, and Orca reports it as `draft` exactly like
# typed text. The footer tells them apart: the "<- for agents" hint is shown only while the input is really empty
# (typing hides it; typing also replaces the suggestion). Override with ORCA_BUS_CLAUDE_EMPTY_HINT (a regex).
CLAUDE_EMPTY_HINT = re.compile(os.environ.get('ORCA_BUS_CLAUDE_EMPTY_HINT', '← for agents'))


def claude_input_empty(lines):
    footer = [l for l in lines if l.strip()][-2:]
    # in a narrow pane the hint is cut off and the footer ends in its leading separator: "... (shift+tab to cycle) ·"
    return any(CLAUDE_EMPTY_HINT.search(l) or l.rstrip().endswith('·') for l in footer)


def composer_text(kind, lines, draft=None):
    """Orca's own `draft` field wins when present (except a Claude suggestion); otherwise read the prompt line."""
    if draft:
        if kind == 'claude' and claude_input_empty(lines):
            return ''
        return draft.strip()
    if kind == 'codex':
        return codex_input(lines)
    if kind == 'claude':
        return claude_input(lines)
    return ''


RULE = re.compile(r'^\s*─{8,}\s*$')


def claude_frame(lines):
    """(ok, why): can this Claude screen be trusted to show the input box?

    Claude draws its input box between two full-width rules with the footer below. If Orca's copy of the screen is
    a different size from the real pane (it starts at 80x24 when a tab is restored before its size is known, and only
    a layout resize fixes it), Claude's output lands at the wrong columns. The prompt row then mixes the ghost
    suggestion with pieces of the footer, and Orca's `draft` reports that mix as typed text. Nothing read from such a
    frame is evidence of anything."""
    rows = [l for l in lines if l.strip()]
    prompt = [i for i, l in enumerate(rows) if l.lstrip().startswith('❯')]
    if not prompt:
        return False, 'no prompt line'
    p = prompt[-1]
    above = [i for i in range(p) if RULE.match(rows[i])]
    below = [i for i in range(p + 1, len(rows)) if RULE.match(rows[i])]
    if not above or not below:
        return False, 'screen out of sync: no rules around the input box'
    width = len(rows[above[-1]].rstrip())
    if len(rows[below[0]].rstrip()) != width:
        return False, 'screen out of sync: rules of different widths'
    return True, ''


def frame_ok(kind, lines):
    if kind == 'claude':
        return claude_frame(lines)
    return True, ''


def title_state(title):
    """'idle' / 'busy' / None from the Orca tab title alone. The title comes from the terminal's own title sequence,
    not from Orca's screen copy, so it stays right when that copy is the wrong size."""
    t = (title or '').strip()[:1]
    if t == IDLE_TITLE:
        return 'idle'
    if t in SPINNER_TITLE:
        return 'busy'
    return None


def prompt_rows(lines):
    """The text after each prompt glyph, top to bottom."""
    return [l.lstrip()[1:].lstrip() for l in lines if l.lstrip().startswith('❯')]


def input_row_starts_with(lines, draft, prefix):
    """Some prompt row (or the draft) begins with `prefix`. Used on a stale screen copy after typing: the prefix
    carries the message id, which exists nowhere before this send, so only the live input row can show it, and it
    starts that row only if the box held nothing before it."""
    # on a stale copy, spaces Claude skips over show the rule row underneath: '[bus─m0924...─from─x]'
    want = _squash(prefix)
    return any(_squash(r.replace('─', ' ')).startswith(want) for r in prompt_rows(lines) + [draft or ''])


def starts_with(have, text, n=60):
    """`have` begins with the first n characters of `text` (modulo whitespace; a stale 80-column copy of a wider
    pane still shows the start of the input row intact)."""
    want = ' '.join(text.split())[:n]
    return ' '.join((have or '').split()).startswith(want)


def _squash(s):
    return ''.join((s or '').split())


def same_text(a, b):
    """Orca soft-wraps a long draft at the pane width (a newline where a space was, or inside a long word):
    compare without whitespace."""
    return _squash(a) == _squash(b)


def is_tail_of(comp, text):
    """Claude's input box scrolls: for a long text in a narrow pane Orca's `draft` holds only the visible last lines.
    Our text ends with its own id (the reply hint), so a visible tail still identifies it."""
    c, t = _squash(comp), _squash(text)
    return len(c) >= min(len(t), 40) and t.endswith(c)


def intact(comp, text):
    return same_text(comp, text) or is_tail_of(comp, text)


def queue_hint(lines):
    """Codex mid-turn says 'tab to queue message': Enter does not queue there, Tab does."""
    return any('tab to queue' in l.lower() for l in lines[-6:])


def is_ready(kind, lines):
    """False while the TUI is still booting: text typed then is lost (Claude) or left unsubmitted (Codex)."""
    text = '\n'.join(lines)
    if kind == 'claude':
        return any(l.strip().startswith('\u276f') for l in lines)
    if kind == 'codex':
        return '\u203a' in text and not re.search(r'model:\s+loading|directory:\s+loading|Booting MCP|Starting', text)
    return True
