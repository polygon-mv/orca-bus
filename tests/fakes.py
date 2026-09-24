"""A fake Orca whose terminals behave like the real TUIs, quirks included."""

RULE = '─' * 40


class FakeTerm:
    def __init__(self, kind, busy=False, swallow=0, booting=False, composer='', report_draft=True, desync=False):
        self.kind, self.busy, self.swallow, self.booting = kind, busy, swallow, booting
        self.composer, self.history, self.queued = composer, [], []
        self.report_draft = report_draft
        self.desync = desync        # Orca's screen copy is 80x24 while the pane is wider: the frame is garbage
        self.on_type = None         # hook(term, text) run after a text write: another writer, a turn starting
        self.keys = []

    def type(self, text):
        self.keys.append(text)
        if self.booting:
            return  # typed while booting: lost
        if text and set(text) == {'\x7f'}:  # Backspace
            self.composer = self.composer[:-len(text)] if len(text) <= len(self.composer) else ''
            return
        if text in ('\r', '\t'):
            if not self.composer:
                return
            if self.swallow:
                self.swallow -= 1
                return
            if self.busy:
                if self.kind == 'codex' and text == '\r':
                    return  # Codex mid-turn: Enter does not queue
                self.queued.append(self.composer)
            else:
                self.history.append(self.composer)
            self.composer = ''
            return
        self.composer += text  # Claude keeps text typed mid-turn as a draft
        if self.on_type:
            hook, self.on_type = self.on_type, None
            hook(self, text)

    def lines(self):
        if self.kind == 'claude':
            if self.booting:
                return ['Claude Code starting']
            if self.desync:  # captured shape: words at column 0, a stray char at column 79, no rules
                return ['ow' + ' ' * 77 + 'm', 'emory,' + ' ' * 73 + 'C', 'ead.' + ' ' * 75 + 'O',
                        '❯\xa0run the tests tonighthell, 1 mon  or st ll  unning']
            out = [f'❯ {h}' for h in self.history]
            out += [f'  queued: {q}' for q in self.queued]
            if self.busy:
                out.append('✽ Considering… (12s · thinking)')
            return out + [RULE, f'❯ {self.composer}'.rstrip(), RULE, '  bypass permissions on']
        if self.kind == 'codex':
            if self.booting:
                return ['model:       loading   /model to change', '› Ask Codex to do anything']
            out = [f'› {h}' for h in self.history] + [f'  ↳ {q}' for q in self.queued]
            if self.busy:
                out += ['• Working (8s • esc to interrupt)', '  tab to queue message']
            return out + [f'› {self.composer or "Ask Codex to do anything"}']
        return ['$ ']


class FakeOrca:
    def __init__(self):
        self.terms = {}

    def add(self, handle, term, title=''):
        self.terms[handle] = (term, title)
        return term

    def terminals(self):
        return {h: {'title': ('◐ work' if t.busy and t.kind == 'claude' else title), 'agent': t.kind, 'cwd': '.'}
                for h, (t, title) in self.terms.items()}

    def screen(self, handle):
        t = self.terms[handle][0]
        if t.desync:  # Orca derives `draft` from the same broken frame
            return t.lines(), 'run the tests tonighthell, 1 mon  or st ll  unning'
        return t.lines(), (t.composer or None) if t.report_draft else None

    def scrollback(self, handle, limit=400):
        return self.terms[handle][0].lines()

    def send(self, handle, text):
        self.terms[handle][0].type(text)
        return True
