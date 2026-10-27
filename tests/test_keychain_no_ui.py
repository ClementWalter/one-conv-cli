#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
#     "mcp==2.2.0",
#     "websockets==17.2",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
#     "httpx==0.28.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Keychain reads fail closed unless process-wide interaction is suppressed."""

import ctypes
import importlib.machinery
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def cli_module(monkeypatch):
    loader = importlib.machinery.SourceFileLoader(
        "oneconv_keychain_test_cli", str(Path(__file__).parents[1] / "one_conv_cli.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    return module


class FunctionStub:
    """Allow ctypes signatures on controlled Security framework operations."""

    def __init__(self, operation):
        self.operation = operation

    def __call__(self, *args):
        return self.operation(*args)


class SecurityStub:
    """Record interaction state around the only operation allowed to read data."""

    def __init__(self, previous=True, get_status=0, disable_status=0, copy_error=None, restore_status=0):
        self.events = []
        self.previous = previous
        self.get_status = get_status
        self.disable_status = disable_status
        self.restore_status = restore_status
        self.copy_error = copy_error
        self.SecKeychainGetUserInteractionAllowed = FunctionStub(self.get_allowed)
        self.SecKeychainSetUserInteractionAllowed = FunctionStub(self.set_allowed)
        self.SecItemCopyMatching = FunctionStub(self.copy_matching)

    def get_allowed(self, pointer):
        self.events.append("get")
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte))[0] = self.previous
        return self.get_status

    def set_allowed(self, allowed):
        self.events.append(("set", bool(allowed)))
        return self.disable_status if len(self.events) == 2 else self.restore_status

    def copy_matching(self, query, result):
        self.events.append("copy")
        if self.copy_error:
            raise self.copy_error
        return 0


def test_suppresses_interaction_before_access_and_restores(cli_module):
    security = SecurityStub()
    cli_module._keychain_copy_without_ui(security, None, None)
    assert security.events == ["get", ("set", False), "copy", ("set", True)]


def test_failed_state_read_never_attempts_credential_access(cli_module):
    security = SecurityStub(get_status=-1)
    cli_module._keychain_copy_without_ui(security, None, None)
    assert security.events == ["get"]


def test_failed_suppression_never_attempts_credential_access(cli_module):
    security = SecurityStub(disable_status=-1)
    cli_module._keychain_copy_without_ui(security, None, None)
    assert security.events == ["get", ("set", False)]


def test_preserves_existing_suppression(cli_module):
    security = SecurityStub(previous=False)
    cli_module._keychain_copy_without_ui(security, None, None)
    assert security.events[-1] == ("set", False)


def test_restores_interaction_when_copy_raises(cli_module):
    security = SecurityStub(copy_error=ValueError("Copy failed"))
    try:
        cli_module._keychain_copy_without_ui(security, None, None)
    except ValueError:
        pass
    assert security.events[-1] == ("set", True)


def test_failed_restore_does_not_return_success(cli_module):
    assert cli_module._keychain_copy_without_ui(SecurityStub(restore_status=-1), None, None) is None


def test_missing_suppression_api_never_attempts_access(cli_module):
    security = SecurityStub()
    del security.SecKeychainGetUserInteractionAllowed
    try:
        cli_module._keychain_copy_without_ui(security, None, None)
    except AttributeError:
        pass
    assert security.events == []


def test_process_state_operations_are_inside_shared_lock(cli_module, monkeypatch):
    security = SecurityStub()
    class LockStub:
        def __enter__(self):
            security.events.append("lock")
        def __exit__(self, *args):
            security.events.append("unlock")
    monkeypatch.setattr(cli_module, "_KEYCHAIN_INTERACTION_LOCK", LockStub())
    cli_module._keychain_copy_without_ui(security, None, None)
    assert security.events == ["lock", "get", ("set", False), "copy", ("set", True), "unlock"]
