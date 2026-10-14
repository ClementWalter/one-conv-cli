"""Relay messages between running Claude Code, Codex, Oh My Pi and Cursor sessions.

Each harness gets an adapter that lists its live sessions and delivers text through
that harness's own live-input channel; nothing here writes into a session transcript.
"""
