"""Install a stable native credential reader only during explicit authorization."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


SERVICES = frozenset({"Chrome Safe Storage", "Arc Safe Storage", "Brave Safe Storage",
                      "Microsoft Edge Safe Storage", "Chromium Safe Storage"})
SOURCE = Path(__file__).parent / "native" / "CredentialHelper.swift"
IDENTIFIER = "com.oneconv.credentials"
# Universal build shipped in the Claude Desktop extension, for Macs without developer tools.
PREBUILT = Path(__file__).parent / "native" / "dist" / "credential-helper"
ARCHITECTURES = ("arm64", "x86_64")
MIN_MACOS = "13.0"


class NativeAuthError(RuntimeError):
    """A local helper installation problem without any credential contents."""


def helper_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "OneConv" / "credential-helper-v1"


def _installed(path: Path) -> bool:
    """Only run a private executable owned by the current user."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077 or not metadata.st_mode & stat.S_IXUSR):
        raise NativeAuthError("Credential helper must be a private executable owned by this user")
    return True


def build_helper(output: Path, *, universal: bool = False) -> None:
    """Compile and ad hoc sign the helper; the identifier keeps Keychain trust across builds."""
    with tempfile.TemporaryDirectory(prefix=".credential-build-", dir=output.parent) as temporary:
        if universal:
            slices = [str(Path(temporary) / arch) for arch in ARCHITECTURES]
            for arch, slice_path in zip(ARCHITECTURES, slices):
                subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-target", f"{arch}-apple-macos{MIN_MACOS}",
                                str(SOURCE), "-o", slice_path], check=True, capture_output=True, timeout=300)
            subprocess.run(["/usr/bin/xcrun", "lipo", "-create", *slices, "-output", str(output)],
                           check=True, capture_output=True, timeout=60)
        else:
            subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", str(SOURCE), "-o", str(output)],
                           check=True, capture_output=True, timeout=120)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", IDENTIFIER,
                    str(output)], check=True, capture_output=True, timeout=30)
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(output)],
                   check=True, capture_output=True, timeout=30)


def ensure_helper() -> Path:
    """Compile once so ordinary source updates cannot change Keychain trust identity."""
    if sys.platform != "darwin":
        raise NativeAuthError("The native credential helper requires macOS")
    import fcntl
    destination = helper_path()
    if _installed(destination):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = destination.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o022:
        raise NativeAuthError("Credential helper directory must be private to this user")
    lock_descriptor = os.open(destination.parent / ".credential-helper-install.lock",
                              os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_descriptor, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if _installed(destination):
            return destination
        if not PREBUILT.is_file() and not SOURCE.is_file():
            raise NativeAuthError("This OneConv installation is missing its native helper source")
        with tempfile.TemporaryDirectory(prefix=".credential-build-", dir=destination.parent) as temporary:
            binary = Path(temporary) / "credential-helper"
            try:
                if PREBUILT.is_file():
                    # copyfile, not copy2: no extended attributes, so no download quarantine.
                    shutil.copyfile(PREBUILT, binary)
                    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(binary)],
                                   check=True, capture_output=True, timeout=30)
                else:
                    build_helper(binary)
                binary.chmod(0o700)
                os.replace(binary, destination)
            except (OSError, subprocess.SubprocessError):
                if PREBUILT.is_file():
                    raise NativeAuthError("The bundled credential helper failed its signature check; reinstall the extension") from None
                raise NativeAuthError("Could not build the native credential helper; macOS developer tools are required") from None
        return destination


def read_secret(service: str, *, authorize: bool = False) -> bytes | None:
    """Capture native stdout in memory; passive reads never install or authorize."""
    if service not in SERVICES:
        raise ValueError("Unsupported browser credential service")
    if sys.platform != "darwin":
        return None
    try:
        path = ensure_helper() if authorize else helper_path()
        if not _installed(path):
            return None
        result = subprocess.run([str(path), "authorize" if authorize else "read", service],
                                capture_output=True, timeout=600 if authorize else 10)
    except NativeAuthError:
        if authorize:
            raise
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None
