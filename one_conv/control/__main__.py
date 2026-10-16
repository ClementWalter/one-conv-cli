"""Run the cloud operator CLI from an explicitly provisioned Python environment."""

from .cli import build_control_group

if __name__ == "__main__":
    build_control_group()()
