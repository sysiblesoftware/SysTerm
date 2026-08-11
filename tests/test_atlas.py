"""Regression tests for Sysible Atlas.

The headline test guards the bug that twice disabled the whole companion: Pango
markup containing a literal `alpha='NN%'` combined with a `%`-format operator
raises "unsupported format character '''" at construction time, which the window
swallows — so the pane and its right-click entry silently vanish. The AST scan
below catches that pattern with no GTK needed; the construction test confirms the
real pane builds where gi is available.
"""
import ast
import os

import pytest

HERE = os.path.dirname(__file__)
ATLAS = os.path.join(HERE, os.pardir, "systerm", "atlas.py")


def test_no_percent_format_over_literal_percent_markup():
    """No `"...%'..." % x` anywhere: a string literal that contains `%'` (a
    percent glued to a quote, i.e. `alpha='60%'`) must never be the left side of a
    `%` operator. That is exactly the footer/badge crash."""
    with open(ATLAS, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=ATLAS)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str):
                if "%'" in left.value:
                    offenders.append((node.lineno, left.value[:60]))
    assert not offenders, (
        "%%-format over markup containing a literal percent (alpha='NN%%') — "
        "this crashes pane construction. Concatenate instead. Offenders: %r"
        % offenders)


def test_pick_model_prefers_available():
    # pick_model is pure logic, but importing the module pulls in gi — skip where
    # the GTK bindings aren't installed (the AST scan above needs no gi).
    pytest.importorskip("gi")
    from systerm.atlas import AtlasClient
    c = AtlasClient(model="qwen2.5-coder:7b")
    # exact match kept
    assert c.pick_model(["qwen2.5-coder:7b", "llama3.2:3b"]) == "qwen2.5-coder:7b"
    # base-name match when tag differs
    c = AtlasClient(model="qwen2.5-coder:7b")
    assert c.pick_model(["qwen2.5-coder:latest"]) == "qwen2.5-coder:latest"
    # falls back to a coder/qwen model, then to first
    c = AtlasClient(model="not-installed:1b")
    assert c.pick_model(["llama3.2:3b", "qwen2.5-coder:1.5b"]) == "qwen2.5-coder:1.5b"
    c = AtlasClient(model="not-installed:1b")
    assert c.pick_model(["mistral:7b"]) == "mistral:7b"
    # empty list keeps the configured default
    c = AtlasClient(model="qwen2.5-coder:7b")
    assert c.pick_model([]) == "qwen2.5-coder:7b"


def test_cloud_sse_parsing_and_keys(monkeypatch):
    """The opt-in cloud providers: SSE delta extraction, model-error surfacing,
    and key resolution (env over config.ini). Pure logic behind the gi import."""
    pytest.importorskip("gi")
    from systerm.atlas import AtlasClient, cloud_key, _AtlasModelError
    st = AtlasClient._sse_text
    # text deltas
    assert st("anthropic", {"type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "he"}}) == "he"
    assert st("anthropic", {"type": "message_start"}) == ""
    assert st("openai", {"choices": [{"delta": {"content": "llo"}}]}) == "llo"
    assert st("openai", {"choices": [{"delta": {}}]}) == ""
    # model errors become _AtlasModelError (so retry logic doesn't treat them as
    # transient connection drops)
    with pytest.raises(_AtlasModelError):
        st("anthropic", {"type": "error", "error": {"message": "boom"}})
    with pytest.raises(_AtlasModelError):
        st("openai", {"error": {"message": "bad key"}})
    # key resolution: env wins; absent -> None
    monkeypatch.delenv("SYSIBLE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SYSIBLE_ANTHROPIC_API_KEY", "sk-ant-xyz")
    assert cloud_key("anthropic") == "sk-ant-xyz"


def test_cloud_dispatch_without_key_fails_clearly(monkeypatch):
    """Selecting a cloud provider with no key must fail with setup guidance, not
    hang or send an unauthenticated request."""
    pytest.importorskip("gi")
    from systerm.atlas import AtlasClient
    monkeypatch.delenv("SYSIBLE_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("systerm.atlas.cloud_key", lambda p: None)
    c = AtlasClient()
    c.provider = "anthropic"
    errs = []
    c._run_cloud([{"role": "user", "content": "hi"}],
                 lambda ch: None, lambda: errs.append("done"), errs.append)
    assert errs and "API key" in errs[0] and "Anthropic" in errs[0]


def test_panel_constructs():
    """The real pane must build without raising (catches markup/GI regressions).
    Skipped where GTK isn't importable (e.g. a headless CI without gir bindings)."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    try:
        from gi.repository import Gtk  # noqa: F401
    except Exception:
        pytest.skip("GTK typelib not available")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no display")
    from systerm.atlas import AtlasPanel, AtlasClient
    AtlasPanel(AtlasClient())  # must not raise
