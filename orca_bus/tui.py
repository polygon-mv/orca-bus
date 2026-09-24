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
