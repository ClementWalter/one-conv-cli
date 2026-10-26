"""Session export for `tune`: secret redaction and authorship, loaded straight from the launcher file."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

loader = importlib.machinery.SourceFileLoader("agent_conv", str(Path(__file__).parent.parent / "bin" / "one-conv"))
spec = importlib.util.spec_from_loader("agent_conv", loader)
ac = importlib.util.module_from_spec(spec)
sys.modules["agent_conv"] = ac
loader.exec_module(ac)


def test_scrub_slack_token():
    assert ac._scrub_secrets("token xoxc-1234567890-abcdefghijklmnopqrstuvwxyz ok") == "token [REDACTED:slack] ok"


def test_scrub_github_token():
    assert "[REDACTED:github]" in ac._scrub_secrets("ghp_" + "A" * 36)


def test_scrub_private_key_block():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    assert ac._scrub_secrets(text) == "[REDACTED:privkey]"


def test_scrub_leaves_plain_text():
    assert ac._scrub_secrets("rien à cacher ici") == "rien à cacher ici"


def test_author_system_for_heartbeat_cwd():
    assert ac._session_author("/Users/x/Documents/claudine/heartbeats/dictaphone", "anything") == "system"


def test_author_system_for_launcher_prompt():
    assert ac._session_author("/Users/x/Documents/claudine", "Lis et applique scrupuleusement...") == "system"


def test_author_human_otherwise():
    assert ac._session_author("/Users/x/Documents/claudine", "peux-tu regarder ce bug") == "human"
