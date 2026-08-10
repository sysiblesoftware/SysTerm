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
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango  # noqa: E402

DEFAULT_URL = (os.environ.get("SYSIBLE_AI_URL") or "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("SYSIBLE_AI_MODEL") or "qwen2.5-coder:1.5b"

SYSTEM_PROMPT = (
    "You are Sysible Atlas, a terse Linux troubleshooting companion inside the "
    "SysTerm terminal on Sysible Linux (Debian/Ubuntu; package manager is apt). You "
    "get the terminal's recent output (sometimes a specific failed command + exit "
    "code) or a direct question. Diagnose the MOST RECENT command actually shown. "
    "Be extremely concise — a few lines total. Reply in exactly this shape:\n"
    "**Cause** — one sentence naming the real problem (quote the exact program/"
    "package/file from the output; do NOT invent generic phrases like 'session "
    "terminated', and do NOT invent an exit code that isn't shown).\n"
    "**Fix** — the exact command(s) between triple backticks on their own lines, "
    "and NOTHING else in that section: no label like 'block:' or 'bash', no prose. "
    "If a program is just not installed, use the install command shown in the "
    "output (apt/snap). Keep it minimal.\n"
    "Add a one-line **Note** ONLY if a command is destructive; otherwise omit the "
    "Note entirely — never write 'No error' as a Note. No preamble, no restating "
    "the task, no explaining what a message 'means'. If there is genuinely no "
    "error to fix, reply with ONLY: 'No error — <one short line>.' and no Cause/Fix."
)


class _AtlasModelError(Exception):
    """A model-reported error (e.g. an {"error": ...} line). Distinct from a
    connection error so the retry logic doesn't retry it as if it were transient."""


# --------------------------------------------------------------------------- #
# local model client
# --------------------------------------------------------------------------- #
class AtlasClient:
    def __init__(self, url=DEFAULT_URL, model=DEFAULT_MODEL):
        self.url = url
        self.model = model
        # Ollama is ALWAYS local, so never route these requests through a proxy.
        # urllib otherwise honors http_proxy/all_proxy from the environment and
        # tries to reach 127.0.0.1:11434 via the proxy, which accepts and closes
        # the connection ("Remote end closed connection without response") even
        # though the server is running fine. An empty ProxyHandler disables that.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def pick_model(self, models):
        """Point the client at a model that's actually downloaded. Prefer the
        configured one (exact, or ignoring the :tag), then any qwen/coder model,
        then whatever's first. Returns the chosen name (or the configured default
        if the list is empty). This is why analysis 'does nothing' otherwise — the
        default qwen2.5-coder:7b may not be the model the user pulled."""
        if not models:
            return self.model
        if self.model in models:
            return self.model
        base = self.model.split(":", 1)[0]
        for m in models:
            if m.split(":", 1)[0] == base:
                self.model = m
                return m
        for m in models:
            if "coder" in m or m.startswith("qwen"):
                self.model = m
                return m
        self.model = models[0]
        return self.model

    def probe(self, callback):
        """Report setup state off the main thread: is the ollama binary present,
        is the server answering, and which models are downloaded. `callback(dict)`
        fires on the GTK main loop with keys: binary(bool), server(bool),
        models(list)."""
        def work():
            # `which` uses PATH, which a GUI app launched from the shell/dock may
            # trim to a minimal set that omits /usr/local/bin — where ollama.com's
            # installer puts the binary. Check the common absolute paths too.
            binary = shutil.which("ollama") is not None or any(
                os.path.exists(p) for p in
                ("/usr/local/bin/ollama", "/usr/bin/ollama", "/bin/ollama",
                 "/opt/ollama/ollama", os.path.expanduser("~/.local/bin/ollama")))
            # Retrying list — tolerant of a just-restarted / hydrating server.
            models = self.list_models()
            server = bool(models)
            if not server:
                # No models listed — is the server up-but-empty, or unreachable?
                try:
                    with self._opener.open(self.url + "/api/tags", timeout=3) as r:
                        r.read(1)
                    server = True
                except Exception:
                    server = False
            # If the server answers, ollama is unquestionably installed and running,
            # regardless of what `which`/paths said.
            if server or models:
                binary = True
            GLib.idle_add(callback, {"binary": binary, "server": server, "models": models})
        threading.Thread(target=work, daemon=True).start()

    def stream(self, messages, on_chunk, on_done, on_error):
        """Stream a chat completion. Callbacks fire on the GTK main thread."""
        emit = lambda c: GLib.idle_add(on_chunk, c)
        done = lambda: GLib.idle_add(on_done)
        fail = lambda m: GLib.idle_add(on_error, m)
        threading.Thread(target=self._run, args=(messages, emit, done, fail),
                         daemon=True).start()

    def list_models(self):
        """Return the downloaded model names, or [] if the server can't be listed.
        Retries a few times: right after an Ollama restart the tag list can error
        or come back empty while its cache hydrates. Logs the last error instead of
        swallowing it (a silent failure once left the model stuck on a default the
        user hadn't pulled)."""
        last = None
        for attempt in range(4):
            try:
                with self._opener.open(self.url + "/api/tags", timeout=5) as r:
                    data = json.load(r)
                names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                if names:
                    return names
                # Server answered but no models yet (hydrating) — wait and retry.
            except Exception as e:
                last = e
            time.sleep(0.4)
        if last is not None:
            sys.stderr.write("Atlas: /api/tags failed at %s: %r\n" % (self.url, last))
        return []

    def _resolve_model(self):
        """Point at a currently-downloaded model right before a request, so a
        request never targets a model that isn't pulled (which, with Ollama cloud
        enabled, hangs or closes the connection). Returns the model list so the
        caller can decide what to do when nothing is installed."""
        models = self.list_models()
        if models:
            self.pick_model(models)
        return models

    def _run(self, messages, emit, done, fail):
        # Always target a model that's actually installed. If none is (or the
        # server can't be listed), fail fast with recovery buttons — never send a
        # request for a model the user hasn't pulled, which with Ollama cloud
        # enabled hangs or drops the connection.
        models = self._resolve_model()
        if not models:
            return fail(
                "no local models available at %s. Is Ollama running, and have you "
                "downloaded a model? Use the buttons below." % self.url)

        # Bound the reply: fewer tokens = tighter answer AND faster generation
        # (crucial on CPU-only boxes). Low temperature keeps it focused, not
        # rambling. Both tunable via env for power users.
        try:
            max_tokens = int(os.environ.get("SYSIBLE_AI_MAX_TOKENS") or 350)
        except ValueError:
            max_tokens = 350
        payload = {
            "model": self.model, "messages": messages, "stream": True,
            # Keep the model resident so the SECOND ask onward is fast (no reload).
            "keep_alive": os.environ.get("SYSIBLE_AI_KEEP_ALIVE") or "10m",
            "options": {"temperature": 0.2, "top_p": 0.9, "num_predict": max_tokens},
        }

        def attempt():
            got = 0
            req = urllib.request.Request(
                self.url + "/api/chat", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with self._opener.open(req, timeout=300) as r:
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        # A non-JSON line must not kill the stream thread — that
                        # left an empty, buttonless card with no error. Skip it.
                        continue
                    if msg.get("error"):
                        raise _AtlasModelError(str(msg["error"]))
                    chunk = msg.get("message", {}).get("content", "")
                    if chunk:
                        got += len(chunk)
                        emit(chunk)
            return got

        try:
            try:
                got = attempt()
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                # One transient drop (e.g. Ollama loading the model on first use,
                # or a just-restarted server) shouldn't surface as a hard failure.
                time.sleep(0.6)
                got = attempt()
            if got == 0:
                return fail(
                    "no output from '%s' at %s. Try again, or re-pull it:  "
                    "ollama pull %s" % (self.model, self.url, self.model))
            done()
        except _AtlasModelError as e:
            fail(str(e))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if "not found" in body.lower():
                fail("model '%s' isn't downloaded — run:  ollama pull %s"
                     % (self.model, self.model))
            else:
                fail("model server error %s: %s" % (e.code, body[:200]))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            fail("can't reach a model server at %s (%s) — start it:  "
                 "sudo systemctl start ollama   (or: ollama serve)" % (self.url, e))
        except Exception as e:
            # Absolute backstop: any other error becomes a visible message, never
            # a silently dead thread + blank card.
            fail("model request failed: %s" % e)


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
    def __init__(self, kind, title, subtitle, on_run, run_target="terminal"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_run = on_run
        self._run_target = run_target
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

    # Fence-label / boilerplate lines a small model leaks around code (e.g. it
    # writes "block:" or "bash" on its own line before a command). Drop them so
    # the card stays clean.
    _PROSE_NOISE = re.compile(
        r"^\s*(block|bash|sh|shell|code|command|console|text|plaintext|"
        r"here('?s| is)[^\n:]*)\s*:?\s*$", re.I)

    def _add_prose(self, s):
        # Strip leaked fence labels / filler lines.
        s = "\n".join(ln for ln in s.splitlines()
                      if not self._PROSE_NOISE.match(ln)).strip()
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
            run = Gtk.Button(label="↵  Run in %s" % self._run_target)
            run.get_style_context().add_class("atlas-run")
            run.connect("clicked", lambda *_: self._on_run(joined))
            act.pack_start(run, False, False, 0)
            copy = Gtk.Button(label="Copy")
            copy.get_style_context().add_class("atlas-ghost")
            copy.connect("clicked", lambda *_: self._copy(joined))
            act.pack_start(copy, False, False, 0)
            self.pack_start(act, False, False, 0)

    def _copy(self, text):
        try:
            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clip.set_text(text, -1)
        except Exception:
            pass


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
        self.on_close = None
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

        # First-run / no-model state shows a Setup card (install Ollama, download
        # a model) instead of a bare hint. Built once; shown when there are no
        # answer cards and re-openable from the header "Setup" button.
        self._setup = self._build_setup()
        self._cards.pack_start(self._setup, False, False, 4)
        self.refresh_setup()

        self.pack_start(self._build_ask(), False, False, 0)
        self.pack_start(self._build_footer(), False, False, 0)
        self.show_all()

        # Re-probe whenever the pane becomes visible (opened via Alt+A / right-
        # click). Ollama may not have been ready at startup; this keeps the setup
        # state fresh and, crucially, re-picks a downloaded model so the badge and
        # the next ask target a model that's actually installed.
        self.connect("map", lambda *_: self.refresh_setup())

    # ----- chrome ----------------------------------------------------------
    def _build_header(self):
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        head.get_style_context().add_class("atlas-header")
        dot = Gtk.Label(label="●")
        dot.get_style_context().add_class("atlas-dot")
        head.pack_start(dot, False, False, 0)
        title = Gtk.Label(xalign=0.0)
        title.set_markup(
            "<b>Sysible Atlas</b>  <span alpha='55%'>· watching this session</span>")
        head.pack_start(title, False, False, 0)
        # Close (hide) the pane. Hiding keeps the widget alive, so every card and
        # the whole conversation history is preserved — reopening (Alt+A, the
        # right-click entry, or a caught error) shows exactly where you left off.
        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("atlas-ghost")
        close.get_style_context().add_class("atlas-close")
        close.set_tooltip_text("Close Atlas (Alt+A) — history is kept")
        close.connect("clicked", lambda *_: self.on_close and self.on_close())
        # Model selector — populated from the server's installed models, so you
        # SEE what's available and pick it, rather than Atlas guessing a default
        # that may not be pulled. Shows "detecting…" until the first probe returns.
        picker = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        picker.get_style_context().add_class("atlas-badge")
        loc = Gtk.Label()
        loc.set_markup("<span alpha='60%'>local ·</span>")
        picker.pack_start(loc, False, False, 0)
        self._model_combo = Gtk.ComboBoxText()
        self._model_combo.get_style_context().add_class("atlas-model")
        self._model_combo.set_tooltip_text("Installed models on this machine — pick one")
        self._model_combo_handler = self._model_combo.connect(
            "changed", self._on_model_changed)
        picker.pack_start(self._model_combo, False, False, 0)
        self._refresh_model_combo([])   # initial "detecting…"
        head.pack_end(picker, False, False, 0)
        return head

    def _refresh_model_combo(self, models):
        combo = self._model_combo
        combo.handler_block(self._model_combo_handler)
        combo.remove_all()
        if models:
            for m in models:
                combo.append_text(m)
            active = self._client.model if self._client.model in models else models[0]
            combo.set_active(models.index(active))
            combo.set_sensitive(True)
        else:
            combo.append_text("detecting…")
            combo.set_active(0)
            combo.set_sensitive(False)
        combo.handler_unblock(self._model_combo_handler)

    def _on_model_changed(self, combo):
        m = combo.get_active_text()
        if m and "…" not in m:
            self._client.model = m
            self._refresh_footer()

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
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        foot.get_style_context().add_class("atlas-footer")
        # Left: where the model runs + the privacy promise. Kept as a ref so the
        # model name refreshes when we auto-select a downloaded model.
        self._footer_left = Gtk.Label(xalign=0.0)
        self._refresh_footer()
        foot.pack_start(self._footer_left, False, False, 0)
        # Right: the action hints (clickable), mockup-style. These carry the
        # Analyze / Setup actions that used to sit in the header.
        def hint(label, tip, cb):
            b = Gtk.Button(label=label)
            b.get_style_context().add_class("atlas-hint")
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda *_: cb())
            return b
        foot.pack_end(hint("Setup", "Install Ollama / download a model",
                           self.show_setup), False, False, 0)
        foot.pack_end(hint("Clear", "Remove all cards (history)",
                           self.clear_cards), False, False, 0)
        foot.pack_end(hint("⌥K Ask", "Ask about this terminal",
                           self.focus_ask), False, False, 0)
        foot.pack_end(hint("⌥A Analyze", "Explain the focused terminal's last output",
                           lambda: self.on_analyze and self.on_analyze()),
                      False, False, 0)
        return foot

    def clear_cards(self):
        """Remove every answer/error card (keeps the Setup card). Old cards are
        history and can look like current failures; this wipes them."""
        for c in list(self._cards.get_children()):
            if c is not self._setup:
                self._cards.remove(c)
        if self._setup not in self._cards.get_children():
            self._cards.pack_start(self._setup, False, False, 4)
        self._setup.show_all()
        self.refresh_setup()

    # These build markup by CONCATENATION on purpose — the strings hold literal
    # `alpha='NN%'`, and a %-format operator would choke on the `%'` (the bug that
    # twice disabled all of Atlas). Don't reintroduce %-formatting here.
    def _refresh_footer(self):
        model = self._client.model if self._client.model else "no model"
        self._footer_left.set_markup(
            "<span alpha='75%'>● local · Ollama · "
            + GLib.markup_escape_text(model) + "</span>"
            "   <span alpha='45%'>· nothing leaves this machine</span>")

    def _refresh_model_labels(self, models=None):
        # Populate the header selector from what's actually installed, then sync
        # the footer to the active model.
        if models is not None:
            self._refresh_model_combo(models)
        self._refresh_footer()

    # ----- first-run setup (install Ollama + download a model) -------------
    PULLS = [
        ("qwen2.5-coder:7b", "code · ~4.7 GB · best default"),
        ("llama3.2:3b", "general · ~2 GB · light"),
        ("qwen2.5-coder:1.5b", "code · ~1 GB · tiny/low-RAM"),
    ]

    def _build_setup(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.get_style_context().add_class("atlas-card")
        title = Gtk.Label(xalign=0.0)
        title.set_markup("<b>Set up Sysible Atlas</b>")
        title.get_style_context().add_class("atlas-card-title")
        box.pack_start(title, False, False, 0)
        intro = Gtk.Label(
            xalign=0.0, wrap=True,
            label="Atlas runs on a LOCAL model — set one up once. Buttons run in "
                  "your terminal so you see progress; then hit Re-check.")
        intro.get_style_context().add_class("atlas-prose")
        box.pack_start(intro, False, False, 0)
        self._setup_status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        box.pack_start(self._setup_status, False, False, 0)
        recheck = Gtk.Button(label="↻  Re-check")
        recheck.get_style_context().add_class("atlas-ghost")
        recheck.set_halign(Gtk.Align.START)
        recheck.connect("clicked", lambda *_: self.refresh_setup())
        box.pack_start(recheck, False, False, 0)
        return box

    def _setup_row(self, markup):
        lab = Gtk.Label(xalign=0.0, wrap=True)
        lab.get_style_context().add_class("atlas-prose")
        lab.set_markup(markup)
        return lab

    def _setup_button(self, label, cmd):
        b = Gtk.Button(label=label)
        b.get_style_context().add_class("atlas-run")
        b.set_halign(Gtk.Align.START)
        b.connect("clicked", lambda _w, c=cmd: self.on_run and self.on_run(c))
        return b

    def refresh_setup(self):
        for c in self._setup_status.get_children():
            self._setup_status.remove(c)
        self._setup_status.pack_start(self._setup_row("<span alpha='60%'>Checking…</span>"),
                                      False, False, 0)
        self._setup_status.show_all()
        self._client.probe(self._render_setup)

    def _render_setup(self, state):
        # Drive the header selector from what's actually installed (so you pick
        # from real models, not a guessed default), and point the client at one
        # that exists so Ask/Analyze work.
        if state["models"]:
            self._client.pick_model(state["models"])
        self._refresh_model_labels(state["models"])
        for c in self._setup_status.get_children():
            self._setup_status.remove(c)
        s = self._setup_status

        if state["binary"]:
            s.pack_start(self._setup_row("✓  <b>Ollama</b> installed"), False, False, 0)
        else:
            s.pack_start(self._setup_row("✗  <b>Ollama</b> not installed"), False, False, 0)
            s.pack_start(self._setup_button("Install Ollama",
                         "curl -fsSL https://ollama.com/install.sh | sh"), False, False, 0)

        if state["server"]:
            s.pack_start(self._setup_row("✓  model server running"), False, False, 0)
        elif state["binary"]:
            s.pack_start(self._setup_row("✗  model server not running"), False, False, 0)
            s.pack_start(self._setup_button("Start Ollama",
                         "sudo systemctl start ollama || ollama serve &"), False, False, 0)

        if state["models"]:
            names = ", ".join(GLib.markup_escape_text(m) for m in state["models"])
            s.pack_start(self._setup_row("✓  models: <tt>%s</tt>" % names), False, False, 0)
            s.pack_start(self._setup_row(
                "<span alpha='70%'>Ready. Press <b>Alt+A</b> anytime, or ask below.</span>"),
                False, False, 0)
        else:
            s.pack_start(self._setup_row(
                "<span alpha='70%'>Download a model:</span>"), False, False, 0)
            for name, desc in self.PULLS:
                s.pack_start(self._setup_button("▾  %s   (%s)" % (name, desc),
                             "ollama pull %s" % name), False, False, 0)
        s.show_all()
        return False

    def show_setup(self):
        if self._setup not in self._cards.get_children():
            self._cards.pack_start(self._setup, False, False, 4)
            self._cards.reorder_child(self._setup, 0)
        self._setup.show_all()
        self.refresh_setup()

    def focus_ask(self):
        self._entry.grab_focus()

    def _on_ask_activate(self, *_):
        q = self._entry.get_text().strip()
        if q and self.on_ask:
            self._entry.set_text("")
            self.on_ask(q)

    # ----- streaming a card ------------------------------------------------
    def start_card(self, kind, title, subtitle, messages, run_target="terminal"):
        if self._setup in self._cards.get_children():
            self._cards.remove(self._setup)   # kept alive; re-openable via header
        card = AtlasCard(kind, title, subtitle,
                         on_run=lambda cmd: self.on_run and self.on_run(cmd),
                         run_target=run_target)
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
