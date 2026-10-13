"""Keep every verified provider session so switching browser accounts never loses one.

A browser profile holds one login per provider at a time. Each session that
one-conv verifies is saved here, keyed by its account identity, so an
account signed out of the browser stays readable until its session expires.
Entries are private files (0600 in a 0700 directory), like the managed
session files `cloud_cli.session_provider` accepts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PROVIDERS = frozenset({"claude", "chatgpt"})


@dataclass(frozen=True)
class VaultEntry:
    provider: str
    identity: str
    label: str
    cookies: dict[str, str]
    saved_at: float


def _private(metadata: os.stat_result) -> bool:
    return metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077


class SessionVault:
    def __init__(self, root: Path):
        self.root = root

    def _directory(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or not _private(metadata):
            raise PermissionError("Session vault directory must be private to this user")
        return self.root

    def _path(self, provider: str, identity: str) -> Path:
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        return self.root / f"{provider}-{digest}.json"

    def save(self, provider: str, identity: str, label: str, cookies: dict[str, str]) -> None:
        if provider not in PROVIDERS:
            raise ValueError("Unsupported vault provider")
        directory = self._directory()
        target = self._path(provider, identity)
        temporary = directory / f".{target.name}.{os.getpid()}.tmp"
        payload = json.dumps({"provider": provider, "identity": identity, "label": label,
                              "cookies": cookies, "saved_at": time.time()})
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(payload)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def entries(self, provider: str) -> list[VaultEntry]:
        """Saved sessions for one provider, newest first; unsafe or unreadable files are skipped."""
        if not self.root.is_dir():
            return []
        found = []
        for path in self.root.glob(f"{provider}-*.json"):
            try:
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or not _private(metadata):
                    log.warning("Ignoring a saved session file that is not private: %s", path.name)
                    continue
                doc = json.loads(path.read_text())
                cookies = doc["cookies"]
                if not isinstance(cookies, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                            for k, v in cookies.items()):
                    continue
                found.append(VaultEntry(provider, str(doc["identity"]), str(doc["label"]), cookies,
                                        float(doc["saved_at"])))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(found, key=lambda entry: entry.saved_at, reverse=True)
