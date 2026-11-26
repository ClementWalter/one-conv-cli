#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["click==8.3.3"]
# ///
"""Rebuild a cloud box's base over ssh: the tools every teleport needs, before any session-specific ones.

Runs on an Ubuntu VM reachable as root: installs the system packages, a user named like the local
one (passwordless sudo, lingering so its bridge runs without a login), uv, Claude Code, Node.js,
prek, the Scaleway CLI, this machine's git identity and a one-conv checkout on PATH. Idempotent:
re-running only fills what is missing. What a teleported session installs later stays on the
box's disk; logins (claude, gh, ...) are the person's and never travel.

Then join it: `one-conv live invite NODE` here, `one-conv live join '<invite>' --accept-teleport --yes`
there, and `one-conv live box add NODE --scaleway <id> --yes` here.
"""

import getpass
import logging
import shlex
import subprocess

import click

logger = logging.getLogger("box-bootstrap")

REPO = "https://github.com/ClementWalter/one-conv-cli"
PACKAGES = "git tmux curl jq ripgrep sqlite3 build-essential unzip python3 ca-certificates gh"
NODE_MAJOR = 22


def root_script(user: str) -> str:
    """As root: packages, Node.js, the Scaleway CLI and the box user."""
    return f"""set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq {PACKAGES} >/dev/null
if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt {NODE_MAJOR} ]; then
  curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
command -v scw >/dev/null || curl -fsSL https://raw.githubusercontent.com/scaleway/scaleway-cli/master/scripts/get.sh | sh >/dev/null
id {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}
echo '{user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-{user}
install -d -o {user} -g {user} -m 700 /home/{user}/.ssh
cp /root/.ssh/authorized_keys /home/{user}/.ssh/authorized_keys
chown {user}:{user} /home/{user}/.ssh/authorized_keys
loginctl enable-linger {user}
"""


def user_script(name: str, email: str) -> str:
    """As the box user: uv, Claude Code, prek, git identity and the one-conv checkout."""
    return f"""set -e
export PATH=$HOME/.local/bin:$PATH
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1
command -v prek >/dev/null || uv tool install -q prek
git config --global user.name {shlex.quote(name)}
git config --global user.email {shlex.quote(email)}
mkdir -p ~/Documents ~/.local/bin
[ -d ~/Documents/one-conv-cli ] || git clone -q {REPO} ~/Documents/one-conv-cli
ln -sf ~/Documents/one-conv-cli/bin/one-conv ~/.local/bin/one-conv
grep -q '.local/bin' ~/.bashrc || echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc
echo "uv $(uv --version | cut -d' ' -f2), claude $(claude --version | cut -d' ' -f1), node $(node --version), prek $(prek --version | cut -d' ' -f2)"
"""


def ssh(target: str, script: str) -> str:
    result = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=accept-new", target, "bash -s"], input=script,
                            capture_output=True, text=True, timeout=1200)
    if result.returncode:
        raise click.ClickException(f"{target}: {result.stderr.strip()[-2000:]}")
    return result.stdout.strip()


def git_identity() -> tuple[str, str]:
    read = lambda key: subprocess.run(["git", "config", "--global", key], capture_output=True, text=True).stdout.strip()
    return read("user.name"), read("user.email")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("host")
@click.option("--user", default=getpass.getuser(), show_default=True, help="The box user to create.")
@click.option("--yes", is_flag=True, help="Run it. Without it, only prints the two scripts.")
def main(host: str, user: str, yes: bool) -> None:
    """Install a cloud box's base on HOST (an ssh host reachable as root)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    name, email = git_identity()
    if not yes:
        click.echo(f"# as root@{host}\n{root_script(user)}\n# as {user}@{host}\n{user_script(name, email)}")
        click.echo("DRY-RUN: re-run with --yes to install.")
        return
    ssh(f"root@{host}", root_script(user))
    logger.info("%s: system packages, Node.js, scw and user %s ready", host, user)
    logger.info("%s: %s", host, ssh(f"{user}@{host}", user_script(name, email)))


if __name__ == "__main__":
    main()
