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

BIG_MODEL = "qwen2.5-coder:7b"      # sharper; needs a healthy amount of RAM
SMALL_MODEL = "qwen2.5-coder:1.5b"  # fast/light; fine on modest VMs


def _total_ram_gb():
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)   # kB -> GiB
    except Exception:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        return 0.0


def _preferred_model():
    """Pick a sensible default by hardware: the 7B (Q4) needs ~6 GB resident, so
    only prefer it on machines with real headroom; otherwise the fast 1.5B. The
    header selector still lets the user switch, and pick_model() falls back to
    whatever is actually installed."""
    return BIG_MODEL if _total_ram_gb() >= 12 else SMALL_MODEL


DEFAULT_MODEL = os.environ.get("SYSIBLE_AI_MODEL") or _preferred_model()

# --- optional cloud providers (opt-in; local Ollama stays the default) ------ #
# Atlas is local-first and private by default. These providers send the command
# and the terminal slice to a third party, so they are never the default: the
# user must explicitly pick "Claude" or "GPT" in the model selector, and a key
# must be configured. Model ids are overridable for whatever the account has.
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MODEL = os.environ.get("SYSIBLE_ANTHROPIC_MODEL") or "claude-sonnet-5"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("SYSIBLE_OPENAI_MODEL") or "gpt-4o"
# provider -> (label, env var, config.ini key, human name)
CLOUD = {
    "anthropic": ("Claude", "SYSIBLE_ANTHROPIC_API_KEY", "anthropic_api_key", "Anthropic"),
    "openai":    ("GPT",    "SYSIBLE_OPENAI_API_KEY",    "openai_api_key",    "OpenAI"),
}


def atlas_conf(key, fallback=None):
    """Read a value from the [atlas] section of ~/.config/systerm/config.ini."""
    try:
        import configparser
        from .config import CONFIG_PATH
        cp = configparser.ConfigParser()
        cp.read(CONFIG_PATH)
        v = cp.get("atlas", key, fallback=None)
        return v if v is not None else fallback
    except Exception:
        return fallback


def atlas_enabled():
    """Is the Atlas companion turned on? Default yes. Set `enabled = no` in the
    [atlas] config section to run SysTerm with NO Atlas at all — no model client,
    no watcher, no pane, zero footprint (a plain terminal). Env override:
    SYSIBLE_ATLAS=0/off/no."""
    env = (os.environ.get("SYSIBLE_ATLAS") or "").strip().lower()
    if env in ("0", "off", "no", "false"):
        return False
    if env in ("1", "on", "yes", "true"):
        return True
    v = (atlas_conf("enabled", "yes") or "yes").strip().lower()
    return v not in ("no", "off", "0", "false")


def cloud_key(provider):
    """Resolve a provider's API key: environment first, then the [atlas] section
    of ~/.config/systerm/config.ini. Returns None if neither is set."""
    _, env, ini_key, _ = CLOUD[provider]
    v = (os.environ.get(env) or "").strip()
    if v:
        return v
    try:
        import configparser
        from .config import CONFIG_PATH
        cp = configparser.ConfigParser()
        cp.read(CONFIG_PATH)
        return (cp.get("atlas", ini_key, fallback="") or "").strip() or None
    except Exception:
        return None


def save_atlas_key(ini_key, value):
    """Upsert `ini_key = value` into the [atlas] section of config.ini, preserving
    the rest of the file (comments and other sections included), and tighten the
    file to 0600 since it now holds a secret. Creates the file/section as needed.
    Returns True on success. Used by the Atlas setup UI so users can paste a
    Claude/GPT key instead of hand-editing the file."""
    import re
    from .config import CONFIG_DIR, CONFIG_PATH, ensure_default_config
    value = (value or "").strip()
    line = "%s = %s" % (ini_key, value)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        ensure_default_config()            # writes the commented template if absent
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
        # Find an ACTIVE (uncommented) [atlas] header — the template ships it as
        # "# [atlas]", which must NOT match, so we append a real section instead.
        m = re.search(r"(?m)^\[atlas\][ \t]*$", text)
        if m:
            start = m.end()
            nxt = re.search(r"(?m)^\[", text[start:])
            end = start + (nxt.start() if nxt else len(text) - start)
            body = text[start:end]
            key_re = re.compile(r"(?mi)^[ \t]*%s[ \t]*=.*$" % re.escape(ini_key))
            if key_re.search(body):
                body = key_re.sub(line, body, count=1)
            else:
                body = body.rstrip("\n") + "\n" + line + "\n"
            text = text[:start] + body + text[end:]
        else:
            text = text.rstrip("\n") + "\n\n[atlas]\n" + line + "\n"
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


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
    "If the output already suggests how to fix it — e.g. 'can be installed with: "
    "<cmd>', 'try: <cmd>', 'did you mean <cmd>?' — reproduce that command VERBATIM "
    "(keep sudo and the exact package manager; do not shorten 'sudo snap install X' "
    "to 'install X'). Never invent a command, package, path, or exit code that is "
    "not present in the input, and never reference a command the user did not run.\n"
    "Add a one-line **Note** ONLY if a command is destructive; otherwise omit the "
    "Note entirely — never write 'No error' as a Note. No preamble, no restating "
    "the task, no explaining what a message 'means'. If there is genuinely no "
    "error to fix, reply with ONLY: 'No error — <one short line>.' and no Cause/Fix.\n"
    "\n"
    "Example.\n"
    "Input — command that failed: kubectl (exit 127); output: \"Command 'kubectl' "
    "not found, but can be installed with: sudo snap install kubectl\"\n"
    "Correct reply:\n"
    "**Cause** kubectl is not installed.\n"
    "**Fix**\n"
    "```\nsudo snap install kubectl\n```"
)

# A separate prompt for direct questions typed in the ask box — these are NOT
# error diagnoses, so no Cause/Fix shape and never a "No error" prefix.
QUESTION_PROMPT = (
    "You are Sysible Atlas, a concise Linux/DevOps assistant inside the SysTerm "
    "terminal on Sysible Linux (Debian/Ubuntu; apt). Answer the user's question "
    "directly and practically. Put ALL commands, code, config, playbooks, scripts, "
    "or file contents inside a ```fenced``` code block (one block per file; a short "
    "prose line before it is fine) — NEVER paste multi-line code or a file as plain "
    "prose. Make it ready to run and correct for Debian/Ubuntu. Use the terminal "
    "context "
    "only if relevant to the question. If the request is ambiguous, state your "
    "assumption in one short line, then answer. Emit only commands that are VALID "
    "for the tool you name — correct subcommands/flags/modules — and never mix a "
    "shell package-manager's flags into another tool. If unsure of exact syntax, "
    "give the tool's standard documented form, not a guess. Never invent package "
    "names, flags, hosts, or output. Do NOT use a Cause/Fix layout and never begin "
    "with 'No error' — those are only for diagnosing a failed command.\n"
    "Reference — Ansible ad-hoc is: ansible <pattern> -m <module> -a "
    "\"key=value ...\" [--become]. To install a package everywhere: "
    "ansible all -m apt -a \"name=<pkg> state=present\" --become  (module is 'apt', "
    "NOT 'apt-get'; state=present, not '-y install')."
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
        # Which backend serves requests: "ollama" (local, default) or a cloud
        # provider key from CLOUD ("anthropic"/"openai"). Set by the selector.
        self.provider = "ollama"
        # True once the user explicitly picks a model in the header selector, so
        # the auto-resolver stops overriding their choice on the next request.
        self.pinned = False
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
        caller can decide what to do when nothing is installed. A model the user
        pinned via the selector is kept as long as it's still installed."""
        models = self.list_models()
        if models and not (self.pinned and self.model in models):
            self.pick_model(models)
        return models

    def unload(self):
        """Ask Ollama to unload the current local model immediately, freeing the
        RAM/CPU it holds. Best-effort, in a background thread; cloud providers have
        nothing resident to unload."""
        if self.provider in CLOUD:
            return
        model = self.model
        def work():
            try:
                data = json.dumps({"model": model, "keep_alive": 0}).encode()
                req = urllib.request.Request(
                    self.url + "/api/generate", data=data,
                    headers={"Content-Type": "application/json"}, method="POST")
                self._opener.open(req, timeout=10).read()
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _run(self, messages, emit, done, fail):
        # Cloud providers are opt-in; dispatch to them when selected. Local Ollama
        # is the default path below.
        if self.provider in CLOUD:
            return self._run_cloud(messages, emit, done, fail)
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
            # How long Ollama keeps the model resident after a reply. Short by
            # default so an idle Atlas doesn't hold RAM/CPU — a quick follow-up is
            # still warm, but walking away frees the model. Override with
            # [atlas] keep_alive = 10m (or "0" to unload immediately).
            "keep_alive": os.environ.get("SYSIBLE_AI_KEEP_ALIVE") or atlas_conf("keep_alive", "30s"),
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

    # -- cloud providers (opt-in) -------------------------------------------- #
    def _run_cloud(self, messages, emit, done, fail):
        provider = self.provider
        label, env, ini_key, human = CLOUD[provider]
        key = cloud_key(provider)
        if not key:
            return fail(
                "%s needs an API key. Open the Setup card (the 'Setup' button in the "
                "footer) and paste your key under 'Cloud models' — or set the %s "
                "environment variable. Note: using %s sends the command and terminal "
                "output to %s (Atlas is local-only with Ollama)."
                % (label, env, label, human))
        try:
            max_tokens = int(os.environ.get("SYSIBLE_AI_MAX_TOKENS") or 350)
        except ValueError:
            max_tokens = 350
        if provider == "anthropic":
            # Anthropic takes the system prompt as a top-level field, not a message.
            system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
            chat = [{"role": m["role"], "content": m["content"]}
                    for m in messages if m.get("role") != "system"]
            payload = {"model": ANTHROPIC_MODEL, "max_tokens": max_tokens,
                       "temperature": 0.2, "stream": True, "messages": chat}
            if system:
                payload["system"] = system
            req = urllib.request.Request(
                ANTHROPIC_URL, data=json.dumps(payload).encode(),
                headers={"content-type": "application/json", "x-api-key": key,
                         "anthropic-version": ANTHROPIC_VERSION}, method="POST")
        else:  # openai — supports a system-role message directly
            payload = {"model": OPENAI_MODEL, "messages": messages, "stream": True,
                       "temperature": 0.2, "max_tokens": max_tokens}
            req = urllib.request.Request(
                OPENAI_URL, data=json.dumps(payload).encode(),
                headers={"content-type": "application/json",
                         "authorization": "Bearer " + key}, method="POST")
        self._run_sse(req, provider, human, emit, done, fail)

    def _run_sse(self, req, provider, human, emit, done, fail):
        """Read a Server-Sent-Events chat stream (Anthropic / OpenAI shape). Unlike
        the local Ollama path this MUST honor the system proxy, so it uses a fresh
        default opener rather than self._opener (which strips proxies)."""
        got = 0
        try:
            opener = urllib.request.build_opener()
            with opener.open(req, timeout=300) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        continue
                    chunk = self._sse_text(provider, msg)
                    if chunk:
                        got += len(chunk)
                        emit(chunk)
            if got == 0:
                return fail("no output from %s. Check the API key, the model id, and "
                            "your network." % human)
            done()
        except _AtlasModelError as e:
            fail(str(e))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            hint = ""
            if e.code in (401, 403):
                hint = " — the API key looks invalid or lacks access."
            elif e.code == 404:
                hint = " — that model id isn't available to your account."
            fail("%s API error %s%s: %s" % (human, e.code, hint, body[:200]))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            fail("can't reach %s (%s). Check your network/proxy." % (human, e))
        except Exception as e:
            fail("%s request failed: %s" % (human, e))

    @staticmethod
    def _sse_text(provider, msg):
        """Extract the text delta from one SSE JSON object; raise on a model error."""
        if provider == "anthropic":
            t = msg.get("type")
            if t == "content_block_delta":
                return (msg.get("delta") or {}).get("text") or ""
            if t == "error":
                err = msg.get("error") or {}
                raise _AtlasModelError(err.get("message") or str(err))
            return ""
        # openai
        if msg.get("error"):
            err = msg["error"]
            raise _AtlasModelError(err.get("message") if isinstance(err, dict) else str(err))
        choices = msg.get("choices") or [{}]
        return (choices[0].get("delta") or {}).get("content") or ""


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
    def __init__(self, kind, title, subtitle, on_run, run_target="terminal",
                 prompt=None, badge=None):
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
        # Small status pill (e.g. "exit 127") next to the title — cleaner than
        # baking the code into an ALL-CAPS title string.
        if badge:
            pill = Gtk.Label(label=badge)
            pill.get_style_context().add_class(
                "atlas-exit" if kind == "error" else "atlas-tag")
            head.pack_start(pill, False, False, 0)
        # A small spinner shows the model is working (from card creation until the
        # answer finishes) — the "progress" indicator while it generates.
        self._spinner = Gtk.Spinner()
        head.pack_start(self._spinner, False, False, 0)
        self._spinner.start()
        if subtitle:
            sub = Gtk.Label(xalign=1.0, label=subtitle,
                            ellipsize=Pango.EllipsizeMode.MIDDLE)
            sub.get_style_context().add_class("atlas-card-sub")
            head.pack_end(sub, True, True, 0)
        self.pack_start(head, False, False, 0)

        # Echo the question you asked, so the card reads like a conversation.
        if prompt:
            q = Gtk.Label(xalign=0.0, wrap=True, selectable=True)
            q.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            q.get_style_context().add_class("atlas-question")
            q.set_markup("<b>You asked</b>  " + GLib.markup_escape_text(prompt))
            self.pack_start(q, False, False, 0)

        # Live streaming text (monospace); replaced by a parsed layout on finish.
        # Starts as a dim "Generating…" placeholder until the first token lands.
        self._live = Gtk.Label(xalign=0.0, label="Generating…", wrap=True,
                               selectable=True)
        self._live.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._live.get_style_context().add_class("atlas-stream")
        self._live.get_style_context().add_class("atlas-wait")
        self.pack_start(self._live, False, False, 0)
        self.show_all()

    def _stop_spinner(self):
        self._spinner.stop()
        self._spinner.hide()

    def append_text(self, chunk):
        if not self._raw:
            self._live.get_style_context().remove_class("atlas-wait")
        self._raw += chunk
        self._live.set_text(self._raw + " ▏")

    def error_text(self, message):
        self._stop_spinner()
        self._raw = message
        self._live.get_style_context().remove_class("atlas-wait")
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
        self._stop_spinner()
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
        self._stopped = False    # Stop button: model unloaded, Atlas idle
        self.get_style_context().add_class("atlas-panel")
        # A comfortable minimum width; the divider can be dragged narrower than the
        # panel's natural size because its Paned slot is packed shrink=True.
        self.set_size_request(300, -1)

        self.pack_start(self._build_header(), False, False, 0)

        self._cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._cards.set_border_width(12)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # Pin the panel's NATURAL width. Without this the scroller reports the
        # widest card (the Setup card's model buttons, ~600px) as its natural
        # width, so on first-run GtkPaned opens Atlas at ~2/3 of the window
        # instead of as a sidebar. Capping the natural width makes the sidebar
        # narrow BY CONSTRUCTION — no divider-timing hack can undo it.
        sw.set_propagate_natural_width(False)
        sw.set_min_content_width(320)
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

    # Atlas is a COMPANION, never the main view — on first launch the terminal
    # must keep the bulk of the window. GtkPaned seeds its first divider position
    # from the child's NATURAL width, and a Gtk.Box's natural width is the widest
    # of ALL its children — here the footer row ("● local · Ollama · … Analyze
    # Ask Clear Setup") and the header, not just the cards. Capping the inner
    # card-scroller alone (below) therefore never bounded the panel: the footer
    # still reported ~700px and Atlas opened at ~2/3 of the window.
    #
    # Clamp the WHOLE panel's natural width here — the single place no wide child
    # can escape. We keep the real MINIMUM (so nothing is ever clipped / no GTK
    # under-allocation warnings) and only pull the NATURAL down to a sidebar
    # width. Result: a narrow sidebar by construction, on every GTK version, with
    # no divider-timing hacks. The user can still drag it wider (the slot is
    # shrink=True/resize handling in window.py); this governs the initial split.
    SIDEBAR_NATURAL = 400

    def do_get_preferred_width(self):
        min_w, nat_w = Gtk.Box.do_get_preferred_width(self)
        return (min_w, max(min_w, min(nat_w, self.SIDEBAR_NATURAL)))

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
        # Expand+ellipsize: the title takes the slack when the sidebar is wide, but
        # collapses gracefully when narrow instead of forcing a wide panel MINIMUM
        # (its natural is ~230px). Without this the header pins the panel min near
        # 530px and the sidebar clamp can't take effect.
        title.set_ellipsize(Pango.EllipsizeMode.END)
        head.pack_start(title, True, True, 0)
        # Close (hide) the pane. Hiding keeps the widget alive, so every card and
        # the whole conversation history is preserved — reopening (Alt+A, the
        # right-click entry, or a caught error) shows exactly where you left off.
        close = Gtk.Button(label="Hide")
        close.get_style_context().add_class("atlas-ghost")
        close.get_style_context().add_class("atlas-close")
        close.set_tooltip_text("Hide Atlas and return to the terminal (Alt+A) — "
                               "history is kept. This does NOT close the window.")
        close.connect("clicked", lambda *_: self.on_close and self.on_close())
        head.pack_end(close, False, False, 0)   # rightmost in the Atlas header
        # Stop / Start: turn Atlas's local model OFF (frees the RAM/CPU it holds)
        # and stop it responding to the terminal — without closing the pane. Click
        # again to start it back up. This is the visible "off switch" for Atlas.
        self._power = Gtk.Button(label="◼ Stop")
        self._power.get_style_context().add_class("atlas-ghost")
        self._power.set_tooltip_text("Stop Atlas: unload the local model and stop "
                                     "watching the terminal (frees RAM/CPU). Click "
                                     "again to start it back up.")
        self._power.connect("clicked", self._toggle_power)
        head.pack_end(self._power, False, False, 0)   # just left of Hide
        self._dot = dot        # refs so Stop/Start can reflect state
        self._title = title
        # Model selector — populated from the server's installed models, so you
        # SEE what's available and pick it, rather than Atlas guessing a default
        # that may not be pulled. Shows "detecting…" until the first probe returns.
        picker = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        picker.get_style_context().add_class("atlas-badge")
        loc = Gtk.Label()
        loc.set_markup("<span alpha='60%'>model ·</span>")
        picker.pack_start(loc, False, False, 0)
        self._model_combo = Gtk.ComboBoxText()
        self._model_combo.get_style_context().add_class("atlas-model")
        self._model_combo.set_tooltip_text(
            "Local Ollama models (default), or a ☁ cloud model (Claude / GPT) — "
            "cloud sends this session's output off the machine")
        self._model_combo_handler = self._model_combo.connect(
            "changed", self._on_model_changed)
        picker.pack_start(self._model_combo, False, False, 0)
        self._refresh_model_combo([])   # initial "detecting…"
        head.pack_end(picker, False, False, 0)
        return head

    def _toggle_power(self, *_):
        """Stop <-> Start. Stop unloads the local model and marks Atlas idle so it
        stops answering and stops reacting to failed commands; Start re-enables it."""
        self._stopped = not self._stopped
        if self._stopped:
            try:
                self._client.unload()
            except Exception:
                pass
        else:
            self.refresh_setup()
        self._reflect_power()

    def is_stopped(self):
        return self._stopped

    def _reflect_power(self):
        stopped = self._stopped
        self._power.set_label("▶ Start" if stopped else "◼ Stop")
        try:
            self._entry.set_sensitive(not stopped)
            self._entry.set_placeholder_text(
                "Atlas is stopped — press Start" if stopped
                else "Ask Atlas about this terminal…")
        except Exception:
            pass
        try:
            state = "stopped" if stopped else "watching this session"
            self._title.set_markup(
                "<b>Sysible Atlas</b>  <span alpha='55%'>· " + state + "</span>")
        except Exception:
            pass
        try:
            ctx = self._dot.get_style_context()
            (ctx.add_class if stopped else ctx.remove_class)("atlas-dot-off")
        except Exception:
            pass

    def _refresh_model_combo(self, models):
        combo = self._model_combo
        combo.handler_block(self._model_combo_handler)
        combo.remove_all()
        # Parallel list of (provider, model) for each row, so a selection maps back
        # to a backend without parsing the label.
        self._combo_items = []
        for m in (models or []):
            combo.append_text(m)
            self._combo_items.append(("ollama", m))
        if not models:
            combo.append_text("detecting…")
            self._combo_items.append((None, None))
        # Cloud options, always offered (opt-in). A ☁ marks that it leaves the box;
        # a • marks a key is configured and it's ready to use right now.
        for prov in ("anthropic", "openai"):
            ready = cloud_key(prov) is not None
            combo.append_text("☁ " + CLOUD[prov][0] + ("  •" if ready else ""))
            self._combo_items.append((prov, None))
        # Restore the active row to whatever the client is currently pointed at.
        active = 0
        for i, (prov, mdl) in enumerate(self._combo_items):
            if prov == self._client.provider and (prov != "ollama" or mdl == self._client.model):
                active = i
                break
        combo.set_active(active)
        combo.set_sensitive(True)
        combo.handler_unblock(self._model_combo_handler)

    def _on_model_changed(self, combo):
        idx = combo.get_active()
        items = getattr(self, "_combo_items", [])
        if idx < 0 or idx >= len(items):
            return
        prov, mdl = items[idx]
        if prov is None:            # the "detecting…" placeholder
            return
        self._client.provider = prov
        if prov == "ollama":
            self._client.model = mdl
        self._client.pinned = True   # honor this until they pick again
        self._refresh_footer()
        # Picked a ☁ cloud model but no key is configured yet → open the Setup
        # card and focus that provider's key field, so entering credentials is
        # the obvious next step (rather than a silent failure when you Ask).
        if prov in CLOUD and cloud_key(prov) is None:
            self.show_setup()
            try:
                entry, _stat = self._key_rows[prov]
                entry.grab_focus()
            except Exception:
                pass

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
        # Ellipsize so this status line never forces a wide panel MINIMUM (it can
        # read "● local · Ollama · qwen2.5-coder:7b · nothing leaves this machine",
        # ~600px). With a small min, the sidebar clamp above is free to stay narrow.
        self._footer_left.set_ellipsize(Pango.EllipsizeMode.END)
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
                           self._analyze_clicked),
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
        prov = self._client.provider
        if prov in CLOUD:
            label, _env, _ini, human = CLOUD[prov]
            model = ANTHROPIC_MODEL if prov == "anthropic" else OPENAI_MODEL
            # A cloud model — be explicit that output leaves the box (the opposite
            # of the local promise), so the trade-off is never hidden.
            self._footer_left.set_markup(
                "<span alpha='75%'>☁ " + GLib.markup_escape_text(human) + " · "
                + GLib.markup_escape_text(model) + "</span>"
                "   <span alpha='45%'>· sent to " + GLib.markup_escape_text(human)
                + "</span>")
        else:
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
    # The official one-liner both installs fresh and reinstalls/repairs in place.
    OLLAMA_INSTALL = "curl -fsSL https://ollama.com/install.sh | sh"
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
        # Optional cloud providers — built ONCE here (not in refresh_setup, which
        # rebuilds on every probe) so typed keys aren't wiped mid-entry.
        box.pack_start(self._build_cloud_keys(), False, False, 0)
        return box

    # ----- optional cloud keys (Claude / GPT) ------------------------------
    def _build_cloud_keys(self):
        """Paste a Claude / GPT API key so the ☁ cloud models in the selector work,
        without hand-editing config.ini. Keys are written to the [atlas] section of
        ~/.config/systerm/config.ini (tightened to 0600)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.get_style_context().add_class("atlas-card")
        t = Gtk.Label(xalign=0.0)
        t.set_markup("<b>Cloud models</b>  <span alpha='60%'>· optional</span>")
        t.get_style_context().add_class("atlas-card-title")
        box.pack_start(t, False, False, 0)
        intro = Gtk.Label(
            xalign=0.0, wrap=True,
            label="Atlas is local by default. Paste a key to enable a ☁ cloud model "
                  "in the selector above — that sends this session's output to the "
                  "provider. Saved to your config.ini.")
        intro.get_style_context().add_class("atlas-prose")
        box.pack_start(intro, False, False, 0)
        self._key_rows = {}
        for prov in ("anthropic", "openai"):
            box.pack_start(self._key_row(prov), False, False, 0)
        return box

    def _key_row(self, prov):
        label, env, ini_key, human = CLOUD[prov]
        via_env = bool((os.environ.get(env) or "").strip())
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        head = Gtk.Label(xalign=0.0)
        head.set_markup("<b>" + label + "</b>  <span alpha='55%'>· "
                        + GLib.markup_escape_text(human) + "</span>")
        head.get_style_context().add_class("atlas-prose")
        row.pack_start(head, False, False, 0)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry()
        entry.set_visibility(False)                    # it's a secret
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        entry.set_hexpand(True)
        entry.set_placeholder_text(
            (human + " key comes from the environment") if via_env
            else ("Paste " + human + " API key"))
        entry.set_sensitive(not via_env)
        entry.connect("activate", self._on_save_key, prov)
        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("atlas-run")
        save.set_sensitive(not via_env)
        save.connect("clicked", self._on_save_key, prov)
        line.pack_start(entry, True, True, 0)
        line.pack_start(save, False, False, 0)
        row.pack_start(line, False, False, 0)
        stat = Gtk.Label(xalign=0.0, wrap=True)
        stat.get_style_context().add_class("atlas-prose")
        row.pack_start(stat, False, False, 0)
        self._key_rows[prov] = (entry, stat)
        self._reflect_key_state(prov)
        return row

    def _reflect_key_state(self, prov):
        label, env, ini_key, human = CLOUD[prov]
        _, stat = self._key_rows[prov]
        if (os.environ.get(env) or "").strip():
            stat.set_markup("<span alpha='70%'>Provided by the environment ("
                            + env + ").</span>")
        elif cloud_key(prov):
            stat.set_markup("<span alpha='70%'>• key saved — pick ☁ " + label
                            + " above. Paste a new one to replace it.</span>")
        else:
            stat.set_text("")

    def _on_save_key(self, _w, prov):
        entry, stat = self._key_rows[prov]
        val = entry.get_text().strip()
        if not val:
            stat.set_markup("<span alpha='70%'>Enter a key first.</span>")
            return
        if save_atlas_key(CLOUD[prov][2], val):
            entry.set_text("")
            self._reflect_key_state(prov)
            # Refresh the model selector so its ☁ • "ready" marker updates.
            try:
                self._refresh_model_labels()
            except Exception:
                pass
        else:
            stat.set_markup("<span alpha='70%'>Couldn't write config.ini — check "
                            "permissions on ~/.config/systerm.</span>")

    def _setup_row(self, markup):
        lab = Gtk.Label(xalign=0.0, wrap=True)
        lab.get_style_context().add_class("atlas-prose")
        lab.set_markup(markup)
        return lab

    def _setup_button(self, label, cmd, ghost=False):
        b = Gtk.Button(label=label)
        # Primary actions (Install / Start) are green "atlas-run"; secondary
        # repair actions (Reinstall) use the quieter "atlas-ghost" so they're
        # discoverable without competing with the happy-path button.
        b.get_style_context().add_class("atlas-ghost" if ghost else "atlas-run")
        b.set_halign(Gtk.Align.START)
        b.connect("clicked", lambda _w, c=cmd: self.on_run and self.on_run(c))
        return b

    def _model_button(self, name, desc, cmd):
        """A model-download button whose label is TWO lines (name over a wrapped
        description) so it fits the narrow companion sidebar fully — a single-line
        'name (code · ~4.7 GB · best default)' is ~430px and gets clipped."""
        b = Gtk.Button()
        b.get_style_context().add_class("atlas-run")
        b.set_halign(Gtk.Align.FILL)          # span the panel width
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        top = Gtk.Label(xalign=0.0, label="▾  " + name)
        top.set_ellipsize(Pango.EllipsizeMode.END)
        sub = Gtk.Label(xalign=0.0, wrap=True)
        # Concatenate (never %-format) — the literal contains alpha='65%', and a
        # % operator over it raises "unsupported format character" (the bug that
        # twice disabled Atlas; guarded by tests/test_atlas.py).
        sub.set_markup("<span alpha='65%' size='small'>"
                       + GLib.markup_escape_text(desc) + "</span>")
        inner.pack_start(top, False, False, 0)
        inner.pack_start(sub, False, False, 0)
        b.add(inner)
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
        if state["models"] and not (self._client.pinned
                                     and self._client.model in state["models"]):
            self._client.pick_model(state["models"])
        self._refresh_model_labels(state["models"])
        for c in self._setup_status.get_children():
            self._setup_status.remove(c)
        s = self._setup_status

        if state["binary"]:
            s.pack_start(self._setup_row("✓  <b>Ollama</b> installed"), False, False, 0)
        else:
            s.pack_start(self._setup_row("✗  <b>Ollama</b> not installed"), False, False, 0)
            s.pack_start(self._setup_button("Install Ollama", self.OLLAMA_INSTALL),
                         False, False, 0)

        if state["server"]:
            s.pack_start(self._setup_row("✓  model server running"), False, False, 0)
        elif state["binary"]:
            s.pack_start(self._setup_row("✗  model server not running"), False, False, 0)
            s.pack_start(self._setup_button("Start Ollama",
                         "sudo systemctl start ollama || ollama serve &"), False, False, 0)

        # A detected-but-broken ("borked") install shows "✓ installed" above and
        # otherwise has no repair path — re-running the official installer
        # reinstalls/repairs in place. Offer it whenever the binary is present;
        # make it the obvious next step when the server won't come up.
        if state["binary"]:
            hint = ("<span alpha='60%'>Ollama installed but not working? "
                    "Reinstall to repair it:</span>") if not state["server"] else \
                   "<span alpha='60%'>Reinstall Ollama (repair a broken install):</span>"
            s.pack_start(self._setup_row(hint), False, False, 0)
            s.pack_start(self._setup_button("↻  Reinstall Ollama",
                         self.OLLAMA_INSTALL, ghost=True), False, False, 0)

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
                s.pack_start(self._model_button(name, desc, "ollama pull %s" % name),
                             False, False, 0)
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
        if self._stopped:
            return
        q = self._entry.get_text().strip()
        if q and self.on_ask:
            self._entry.set_text("")
            self.on_ask(q)

    def _analyze_clicked(self):
        if self._stopped:
            return
        if self.on_analyze:
            self.on_analyze()

    # ----- streaming a card ------------------------------------------------
    def start_card(self, kind, title, subtitle, messages, run_target="terminal",
                   run_pane_id=None, prompt=None, badge=None):
        if self._setup in self._cards.get_children():
            self._cards.remove(self._setup)   # kept alive; re-openable via header
        # Bind this card's Run button to the SPECIFIC pane it came from (run_pane_id),
        # so with several terminals each card types into its own pane, not a global
        # "active" one.
        card = AtlasCard(kind, title, subtitle,
                         on_run=lambda cmd: self.on_run and self.on_run(cmd, run_pane_id),
                         run_target=run_target, prompt=prompt, badge=badge)
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

    def note_card(self, text):
        """A plain, non-streamed note (e.g. 'nothing to analyze') — never calls the
        model, so it can't hallucinate."""
        if self._setup in self._cards.get_children():
            self._cards.remove(self._setup)
        card = AtlasCard("answer", "ATLAS", None, on_run=lambda *_: None)
        card._raw = text
        card.finish()
        self._cards.pack_start(card, False, False, 0)
        self._scroll_end()
        return card

    def _scroll_end(self):
        def go():
            adj = self._scroller.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.idle_add(go)
