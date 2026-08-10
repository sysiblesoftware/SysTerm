"""Sysible Atlas — the AI companion pane.

A second surface inside SysTerm that *reads your session*: it catches a command
that just failed (no copy-paste) and answers questions you ask, streaming from a
LOCAL model server (Ollama by default, or any OpenAI-compatible /v1 server). The
model, the errors, and your code never leave the machine.

Pieces:
  * AtlasClient  — threaded streaming client for the local model, marshalled onto
                   the GTK main loop.
  * AtlasControl — a tiny FIFO the shell writes to, so a failed command or an
                   `ai …` question in the terminal reaches this pane. Bash can
                   write a FIFO natively (no nc/socat), and small writes are
                   atomic, so many panes share one channel safely.
  * AtlasPanel   — the companion widget: watching header, streamed answer cards
                   with Run-in-terminal buttons, an ask box, and a local footer.
"""
import base64
import http.client
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango  # noqa: E402

DEFAULT_URL = (os.environ.get("SYSIBLE_AI_URL") or "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("SYSIBLE_AI_MODEL") or "qwen2.5-coder:7b"

SYSTEM_PROMPT = (
    "You are Sysible Atlas, a terse Linux troubleshooting companion inside the "
    "SysTerm terminal on Sysible Linux (Debian-based; package manager is apt). You "
    "are given a shell command, its exit code, and the terminal output, or a direct "
    "question. Answer in this shape and keep it tight:\n"
    "1. **Cause** — one or two sentences on what actually went wrong.\n"
    "2. **Fix** — the concrete command(s), in a ```fenced``` block, minimal and "
    "safe, using apt/systemctl/Debian conventions.\n"
    "3. **Note** — one line only if a command is risky; otherwise omit.\n"
    "No pleasantries. If there's no real error, say so briefly."
)


# --------------------------------------------------------------------------- #
# local model client
# --------------------------------------------------------------------------- #
class AtlasClient:
    def __init__(self, url=DEFAULT_URL, model=DEFAULT_MODEL):
        self.url = url
        self.model = model

    def stream(self, messages, on_chunk, on_done, on_error):
        """Stream a chat completion. Callbacks fire on the GTK main thread."""
        emit = lambda c: GLib.idle_add(on_chunk, c)
        done = lambda: GLib.idle_add(on_done)
        fail = lambda m: GLib.idle_add(on_error, m)
        threading.Thread(target=self._run, args=(messages, emit, done, fail),
                         daemon=True).start()

    def _run(self, messages, emit, done, fail):
        payload = {"model": self.model, "messages": messages, "stream": True}
        try:
            req = urllib.request.Request(
                self.url + "/api/chat", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=300) as r:
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        return fail(str(msg["error"]))
                    chunk = msg.get("message", {}).get("content", "")
                    if chunk:
                        emit(chunk)
            done()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if "not found" in body.lower():
                fail("model '%s' isn't downloaded — run:  ollama pull %s"
                     % (self.model, self.model))
            else:
                fail("model server error %s: %s" % (e.code, body[:200]))
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            fail("no local model server at %s — start it:  sudo systemctl start ollama"
                 % self.url)


# --------------------------------------------------------------------------- #
# shell control channel (FIFO)
# --------------------------------------------------------------------------- #
class AtlasControl:
    """A FIFO the shell integration writes lines to:
         error<TAB>paneid<TAB>exitcode<TAB>base64(command)
         ask<TAB>paneid<TAB>base64(question)
    `on_event(kind, pane_id, exit_code, text)` fires on the GTK main loop.
    """
    def __init__(self, on_event):
        self._on_event = on_event
        self.path = None
        self._dir = None
        self._fd = -1
        self._chan = None
        self._src = 0
        self._buf = b""

    def start(self):
        # Never let a failure here stop SysTerm from opening — Atlas is optional.
        # Use GLib.io_add_watch (present on every GLib) rather than
        # unix_fd_add_full, whose availability/signature varies. On any failure we
        # tear down and return None; the terminal runs fine without the channel.
        try:
            self._dir = tempfile.mkdtemp(prefix="systerm-atlas-")
            self.path = os.path.join(self._dir, "ctl")
            os.mkfifo(self.path, 0o600)
            # O_RDWR so the reader never sees EOF as writers (shells) come and go.
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
            self._chan = GLib.IOChannel.unix_new(self._fd)
            try:
                self._chan.set_encoding(None)
                self._chan.set_buffered(False)
            except Exception:
                pass
            self._src = GLib.io_add_watch(self._chan, GLib.IOCondition.IN, self._on_io)
            return self.path
        except Exception:
            self.stop()
            return None

    def _on_io(self, _chan, _cond):
        try:
            data = os.read(self._fd, 65536)
        except (BlockingIOError, OSError):
            return True
        if data:
            self._buf += data
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                self._dispatch(line.decode("utf-8", "replace"))
        return True

    def _dispatch(self, line):
        parts = line.split("\t")
        try:
            if parts[0] == "error" and len(parts) >= 4:
                cmd = base64.b64decode(parts[3]).decode("utf-8", "replace")
                self._on_event("error", parts[1], int(parts[2] or 0), cmd)
            elif parts[0] == "ask" and len(parts) >= 3:
                q = base64.b64decode(parts[2]).decode("utf-8", "replace")
                self._on_event("ask", parts[1], 0, q)
        except (ValueError, IndexError, base64.binascii.Error):
            pass

    def stop(self):
        if self._src:
            try:
                GLib.source_remove(self._src)
            except Exception:
                pass
            self._src = 0
        self._chan = None
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        for p in (self.path, self._dir):
            try:
                if p and os.path.exists(p):
                    os.remove(p) if p is self.path else os.rmdir(p)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# a single streamed answer card
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```[a-zA-Z0-9]*\n?(.*?)```", re.S)


class AtlasCard(Gtk.Box):
    def __init__(self, kind, title, subtitle, on_run):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_run = on_run
        self._raw = ""
        self.get_style_context().add_class("atlas-card")
        self.get_style_context().add_class(
            "atlas-card-err" if kind == "error" else "atlas-card-ans")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lab = Gtk.Label(xalign=0.0, label=title)
        lab.get_style_context().add_class("atlas-card-title")
        head.pack_start(lab, False, False, 0)
        if subtitle:
            sub = Gtk.Label(xalign=1.0, label=subtitle,
                            ellipsize=Pango.EllipsizeMode.MIDDLE)
            sub.get_style_context().add_class("atlas-card-sub")
            head.pack_end(sub, True, True, 0)
        self.pack_start(head, False, False, 0)

        # Live streaming text (monospace); replaced by a parsed layout on finish.
        self._live = Gtk.Label(xalign=0.0, label="", wrap=True, selectable=True)
        self._live.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._live.get_style_context().add_class("atlas-stream")
        self.pack_start(self._live, False, False, 0)
        self.show_all()

    def append_text(self, chunk):
        self._raw += chunk
        self._live.set_text(self._raw + " ▏")

    def error_text(self, message):
        self._raw = message
        self._live.set_text(message)
        self._live.get_style_context().add_class("atlas-fail")

    def add_recovery(self, on_run, model):
        """When the model server is down or the model isn't downloaded, offer
        one-click fixes that run in the terminal (so you see live progress) —
        this is the 'give me a model to download' picker."""
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        wrap.get_style_context().add_class("atlas-recovery")
        opts = [
            ("▸  Start the Ollama server", "sudo systemctl start ollama"),
            ("▾  Download %s  (code, ~4.7 GB)" % model, "ollama pull %s" % model),
            ("▾  Download llama3.2:3b  (small, ~2 GB)", "ollama pull llama3.2:3b"),
            ("▾  Download qwen2.5-coder:1.5b  (tiny, ~1 GB)",
             "ollama pull qwen2.5-coder:1.5b"),
        ]
        for label, cmd in opts:
            b = Gtk.Button(label=label)
            b.get_style_context().add_class("atlas-run")
            b.set_halign(Gtk.Align.START)
            b.connect("clicked", lambda _w, c=cmd: on_run(c))
            wrap.pack_start(b, False, False, 0)
        self.pack_start(wrap, False, False, 0)
        self.show_all()

    def finish(self):
        """Render the final answer: prose as wrapped text, fenced blocks as a
        monospace box with a Run-in-terminal button per command line."""
        text = self._raw.strip()
        self.remove(self._live)
        pos = 0
        for m in _FENCE.finditer(text):
            self._add_prose(text[pos:m.start()])
            self._add_code(m.group(1))
            pos = m.end()
        self._add_prose(text[pos:])
        self.show_all()

    def _add_prose(self, s):
        s = s.strip()
        if not s:
            return
        lab = Gtk.Label(xalign=0.0, wrap=True, selectable=True)
        lab.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lab.get_style_context().add_class("atlas-prose")
        lab.set_markup(_md_inline(s))
        self.pack_start(lab, False, False, 0)

    def _add_code(self, code):
        code = code.strip("\n")
        if not code.strip():
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.get_style_context().add_class("atlas-code")
        view = Gtk.Label(xalign=0.0, label=code, selectable=True)
        view.get_style_context().add_class("atlas-code-text")
        box.pack_start(view, False, False, 0)
        self.pack_start(box, False, False, 0)

        cmds = [ln.strip() for ln in code.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        if cmds:
            joined = " && ".join(cmds) if len(cmds) > 1 else cmds[0]
            act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            run = Gtk.Button(label="↵  Run in terminal")
            run.get_style_context().add_class("atlas-run")
            run.connect("clicked", lambda *_: self._on_run(joined))
            act.pack_start(run, False, False, 0)
            self.pack_start(act, False, False, 0)


def _md_inline(s):
    """Minimal, safe inline markdown → Pango markup (**bold** and `code`)."""
    s = GLib.markup_escape_text(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+?)`", r"<tt>\1</tt>", s)
    return s


# --------------------------------------------------------------------------- #
# the companion panel
# --------------------------------------------------------------------------- #
class AtlasPanel(Gtk.Box):
    """on_ask(question) and on_run(command) are set by the window; on_analyze()
    is invoked by the header 'Analyze visible output' button."""
    def __init__(self, client):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._client = client
        self.on_ask = None
        self.on_run = None
        self.on_analyze = None
        self.get_style_context().add_class("atlas-panel")
        self.set_size_request(360, -1)

        self.pack_start(self._build_header(), False, False, 0)

        self._cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._cards.set_border_width(12)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self._cards)
        self._scroller = sw
        self.pack_start(sw, True, True, 0)

        self._empty = Gtk.Label(
            label="Atlas is watching this session.\n\nA failed command lands here "
                  "automatically — or ask below.\nType  ai <question>  in the "
                  "terminal, or use the box.",
            justify=Gtk.Justification.CENTER, wrap=True)
        self._empty.get_style_context().add_class("atlas-empty")
        self._cards.pack_start(self._empty, False, False, 8)

        self.pack_start(self._build_ask(), False, False, 0)
        self.pack_start(self._build_footer(), False, False, 0)
        self.show_all()

    # ----- chrome ----------------------------------------------------------
    def _build_header(self):
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        head.get_style_context().add_class("atlas-header")
        dot = Gtk.Label(label="●")
        dot.get_style_context().add_class("atlas-dot")
        head.pack_start(dot, False, False, 0)
        title = Gtk.Label(xalign=0.0)
        title.set_markup("<b>Sysible Atlas</b>  <span alpha='55%'>· watching</span>")
        head.pack_start(title, False, False, 0)
        analyze = Gtk.Button(label="Analyze output")
        analyze.get_style_context().add_class("atlas-ghost")
        analyze.set_tooltip_text("Explain the last command's output in the focused terminal")
        analyze.connect("clicked", lambda *_: self.on_analyze and self.on_analyze())
        head.pack_end(analyze, False, False, 0)
        return head

    def _build_ask(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("atlas-ask")
        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text("Ask Atlas about this terminal…")
        self._entry.connect("activate", self._on_ask_activate)
        send = Gtk.Button(label="Ask")
        send.get_style_context().add_class("atlas-run")
        send.connect("clicked", self._on_ask_activate)
        row.pack_start(self._entry, True, True, 0)
        row.pack_start(send, False, False, 0)
        return row

    def _build_footer(self):
        foot = Gtk.Label(xalign=0.0)
        foot.get_style_context().add_class("atlas-footer")
        foot.set_markup(
            "<span alpha='75%'>● local · Ollama · %s</span>"
            "   <span alpha='45%'>· nothing leaves this machine</span>"
            % GLib.markup_escape_text(self._client.model))
        return foot

    def focus_ask(self):
        self._entry.grab_focus()

    def _on_ask_activate(self, *_):
        q = self._entry.get_text().strip()
        if q and self.on_ask:
            self._entry.set_text("")
            self.on_ask(q)

    # ----- streaming a card ------------------------------------------------
    def start_card(self, kind, title, subtitle, messages):
        if self._empty is not None:
            self._cards.remove(self._empty)
            self._empty = None
        card = AtlasCard(kind, title, subtitle,
                         on_run=lambda cmd: self.on_run and self.on_run(cmd))
        self._cards.pack_start(card, False, False, 0)
        self._scroll_end()
        def on_err(m):
            card.error_text(m)
            card.add_recovery(lambda cmd: self.on_run and self.on_run(cmd),
                              self._client.model)
            self._scroll_end()
            return False
        self._client.stream(
            messages,
            on_chunk=lambda c: (card.append_text(c), self._scroll_end()) and False,
            on_done=lambda: (card.finish(), self._scroll_end()) and False,
            on_error=on_err,
        )
        return card

    def _scroll_end(self):
        def go():
            adj = self._scroller.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.idle_add(go)
