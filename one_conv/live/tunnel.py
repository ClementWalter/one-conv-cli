"""An ssh local forward to a hub that listens only on its own machine's loopback.

`ssh://[user@]HOST` reaches the hub as `http://127.0.0.1:<port>` through
`ssh -N -L 127.0.0.1:<port>:127.0.0.1:<hub port>`. The argv is fixed: the host is validated
so it can never become an option, host keys must already be known, and nothing prompts.
The tunnel counts as open only once lsof shows that very ssh process listening on the
port, so an unrelated program on that port is never mistaken for the hub.
"""

import re
import shutil
import socket
import subprocess
import time

# [user@]host: letters, digits, dots, dashes, underscores; no option, no shell syntax.
SSH_DESTINATION = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*")


class TunnelError(Exception):
    """The ssh forward could not be opened, or it closed."""


def destination(hub: str) -> str:
    """The ssh destination of an `ssh://[user@]HOST` hub; refuses anything else."""
    host = hub[len("ssh://"):] if hub.startswith("ssh://") else ""
    if not SSH_DESTINATION.fullmatch(host):
        raise TunnelError(f"an ssh hub is ssh://[user@]host, not {hub!r}")
    return host


def command(host: str, local_port: int, hub_port: int) -> list[str]:
    if not SSH_DESTINATION.fullmatch(host):
        raise TunnelError(f"not an ssh destination: {host!r}")
    return ["ssh", "-N", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{hub_port}", "--", host]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listening(pid: int, port: int) -> bool:
    """Whether process `pid` itself holds a TCP listener on `port`."""
    result = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                            capture_output=True, text=True, check=False)
    return str(pid) in result.stdout.split()


class Tunnel:
    def __init__(self, host: str, hub_port: int):
        self.host, self.hub_port = host, hub_port
        self.process: subprocess.Popen | None = None
        self.port = 0

    def open(self, seconds: float = 20.0) -> str:
        """Start ssh and return the local hub URL once ssh itself listens on the forwarded port."""
        for tool in ("ssh", "lsof"):
            if shutil.which(tool) is None:
                raise TunnelError(f"an ssh hub needs {tool} on PATH")
        self.port = _free_port()
        self.process = subprocess.Popen(command(self.host, self.port, self.hub_port), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                error = (self.process.stderr.read() if self.process.stderr else "").strip()
                raise TunnelError(f"ssh to {self.host} exited ({self.process.returncode}): {error[-300:]}")
            if listening(self.process.pid, self.port):
                return f"http://127.0.0.1:{self.port}"
            time.sleep(0.25)
        self.close()
        raise TunnelError(f"ssh to {self.host} did not open the forward within {seconds:.0f}s")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
