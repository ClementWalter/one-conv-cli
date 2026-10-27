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
"""Cloud installation keeps credentials private and makes readiness a checked result."""

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from one_conv.control import installer


@pytest.fixture
def payload():
    return {"revision": "a" * 40, "public_origin": "https://workspace.example.test", "boat_token": "fixture-boat-key",
            "service_account": "fixture-vault-key", "worker_config": {"profiles": {"default": {}}}}


@pytest.fixture
def payload_file(tmp_path, payload):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


class Commands:
    """External programs acknowledge operations while retaining actual filesystem effects."""

    def __init__(self, home, payload):
        self.home, self.payload, self.calls, self.fail = home, payload, [], None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        stage = kwargs["stage"]
        if self.fail == stage:
            raise installer.InstallationError(stage)
        stdout = {"repository-origin": installer.REPOSITORY, "repository-head": self.payload["revision"],
                  "service-linger-check": "yes",
                  "python-path": str(self.home / ".local/share/one-conv-control/venv/lib/python3.11/site-packages")}.get(stage, "")
        return subprocess.CompletedProcess(argv, 0, stdout, "")


@pytest.fixture
def installed(tmp_path, payload, payload_file, monkeypatch):
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    commands = Commands(tmp_path, payload)
    result = installer.install(payload_file, home=tmp_path, runner=commands,
                               downloader=lambda path: installer.private_file(path, b"fixture-binary", 0o700),
                               health=lambda origin: True)
    return tmp_path, commands, result


def test_valid_payload_is_removed_after_read(payload_file):
    installer.read_payload(payload_file)
    assert not payload_file.exists()


@pytest.mark.parametrize("changes", [
    {"revision": "main"}, {"revision": "A" * 40}, {"public_origin": "http://workspace.example.test"},
    {"public_origin": "https://name:password@workspace.example.test"}, {"public_origin": "https://workspace.example.test/path"},
    {"public_origin": "https://workspace.example.test:bad"}, {"public_origin": "https://workspace.example.test:"},
    {"public_origin": "https://WORKSPACE.example.test"}, {"public_origin": "https://workspace.example.test/#key"},
    {"public_origin": "https://workspace.example.test?key=fixture"}, {"boat_token": "key\nvalue"},
    {"boat_token": "\x7f"}, {"service_account": None}, {"worker_config": []}, {"extra": "field"},
])
def test_malformed_payload_is_rejected(payload, changes):
    with pytest.raises(installer.InstallationError, match="payload"):
        installer.validate_payload({**payload, **changes})


def test_default_https_port_is_canonical(payload):
    assert installer.validate_payload({**payload, "public_origin": "https://workspace.example.test:443/"})["public_origin"] == "https://workspace.example.test"


def test_invalid_payload_is_removed(payload_file):
    payload_file.write_text("not JSON")
    with pytest.raises(installer.InstallationError, match="payload"):
        installer.read_payload(payload_file)
    assert not payload_file.exists()


def test_world_readable_payload_is_rejected(payload_file):
    payload_file.chmod(0o644)
    with pytest.raises(installer.InstallationError, match="payload"):
        installer.read_payload(payload_file)


def test_payload_symlink_does_not_read_its_target(payload_file, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(payload_file)
    with pytest.raises(installer.InstallationError, match="payload"):
        installer.read_payload(link)
    assert payload_file.exists()


def test_payload_fifo_is_rejected_without_waiting(tmp_path):
    fifo = tmp_path / "payload.pipe"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(installer.InstallationError, match="payload"):
        installer.read_payload(fifo)


@pytest.mark.parametrize("raw", ['{"revision":1,"revision":2}', '{"number":NaN}', "[" * 2000 + "]" * 2000])
def test_ambiguous_json_is_rejected(payload_file, raw):
    payload_file.write_text(raw)
    with pytest.raises(installer.InstallationError, match="payload"):
        installer.read_payload(payload_file)


def test_oversized_payload_is_rejected(payload_file):
    payload_file.write_bytes(b" " * (installer.PAYLOAD_LIMIT + 1))
    with pytest.raises(installer.InstallationError, match="payload"):
        installer.read_payload(payload_file)


def test_private_file_replaces_a_link_without_writing_its_target(tmp_path):
    target, link = tmp_path / "target", tmp_path / "link"
    target.write_bytes(b"original")
    link.symlink_to(target)
    installer.private_file(link, b"private")
    assert target.read_bytes() == b"original"


def test_secret_file_permissions_are_private(installed):
    account = installed[0] / ".config/one-conv/control/service-account.json"
    assert stat.S_IMODE(account.stat().st_mode) == 0o600


def test_secret_never_enters_command_arguments(installed, payload):
    assert not any(payload["boat_token"] in str(argv) or payload["service_account"] in str(argv) for argv, _kwargs in installed[1].calls)


def test_boat_key_uses_stdin(installed, payload):
    call = next(kwargs for _argv, kwargs in installed[1].calls if kwargs["stage"] == "boat-login")
    assert call["input"] == payload["boat_token"] + "\n"


def test_boat_login_uses_the_documented_stdin_flag(installed):
    call = next(argv for argv, kwargs in installed[1].calls if kwargs["stage"] == "boat-login")
    assert call[1:] == ["login", "--key-stdin", "--json", "--no-update"]


def test_dependencies_are_explicitly_pinned(installed):
    call = next(argv for argv, kwargs in installed[1].calls if kwargs["stage"] == "python-dependencies")
    assert tuple(call[-len(installer.DEPENDENCIES):]) == installer.DEPENDENCIES


@pytest.mark.parametrize("dependency", ["sqlalchemy==2.1.4", "pydantic==2.12.5"])
def test_archive_storage_dependencies_are_installed(installed, dependency):
    call = next(argv for argv, kwargs in installed[1].calls if kwargs["stage"] == "python-dependencies")
    assert dependency in call


def test_archive_imports_are_checked_before_the_service_starts(installed):
    imports = [argv[-1] for argv, kwargs in installed[1].calls if kwargs["stage"] == "runtime-imports"]
    assert "import archive.server, archive.store" in imports


def test_runtime_import_check_uses_optimized_python(installed):
    call = next(argv for argv, kwargs in installed[1].calls if kwargs["stage"] == "runtime-imports")
    assert call[1] == "-O"


def test_checkout_uses_the_exact_revision(installed, payload):
    call = next(argv for argv, kwargs in installed[1].calls if kwargs["stage"] == "repository-checkout")
    assert call[-2:] == ["--detach", payload["revision"]]


def test_worker_configuration_uses_private_credentials(installed):
    home = installed[0]
    config = json.loads((home / ".config/one-conv/control/worker-config.json").read_text())
    assert config["service_account_file"] == str(home / ".config/one-conv/control/service-account.json")


def test_unit_keeps_runtime_optimized(installed):
    content = (installed[0] / ".config/systemd/user" / installer.UNIT).read_text()
    assert '"-O" "-m" "one_conv.control"' in content


def test_unit_contains_no_credential(installed, payload):
    content = (installed[0] / ".config/systemd/user" / installer.UNIT).read_text()
    assert payload["boat_token"] not in content and payload["service_account"] not in content


def test_unit_escapes_specifiers():
    assert installer.unit_argument('/home/a%u/"repo"') == '"/home/a%%u/\\"repo\\""'


def test_working_directory_is_an_absolute_scalar_path(installed):
    content = (installed[0] / ".config/systemd/user" / installer.UNIT).read_text()
    assert f"WorkingDirectory={installed[0]}/Documents/one-conv-cli\n" in content


def test_scalar_path_preserves_spaces_and_escapes_specifiers():
    assert installer.unit_path("/home/a%u/repo with spaces") == "/home/a%%u/repo with spaces"


@pytest.mark.parametrize("value", ["relative/repo", "/home/a\nrepo", "/home/a/repo "])
def test_scalar_path_rejects_ambiguous_values(value):
    with pytest.raises(installer.InstallationError, match="service-unit"):
        installer.unit_path(value)


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd parser is unavailable")
def test_generated_unit_passes_the_real_systemd_parser(tmp_path):
    work = tmp_path / "repository with spaces"
    work.mkdir()
    unit = tmp_path / "oneconv-installer-review.service"
    unit.write_text(installer.service_unit(sys.executable, work, tmp_path, tmp_path / "config", "https://example.test", tmp_path))
    result = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True, check=False)
    assert result.returncode == 0


def test_unit_rejects_control_characters():
    with pytest.raises(installer.InstallationError, match="service-unit"):
        installer.unit_argument("/home/a\nrepo")


def test_whitelisted_receipt_contains_no_payload(installed):
    assert installed[2] == {"revision_verified": True, "runtime_imports": True, "boat_login": True,
                            "service_enabled": True, "healthy": True, "unit": installer.UNIT}


def test_system_service_uses_noninteractive_sudo(tmp_path, monkeypatch):
    calls = []

    def commands(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if kwargs["stage"] == "service-manager" else 0, "", "")

    monkeypatch.setattr(installer.os, "getuid", lambda: 1000)
    monkeypatch.setattr(installer.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="worker"))
    installer.start_service(tmp_path, lambda user: "User=" + user, commands)
    assert calls[1][:5] == ["sudo", "-n", "install", "-m", "0644"]


def test_system_unit_runs_as_the_current_user(tmp_path):
    unit = installer.service_unit("python", tmp_path, tmp_path, tmp_path / "config", "https://example.test", tmp_path, user="worker")
    assert "User=worker\n" in unit


def test_user_service_is_kept_alive_after_logout(installed):
    assert any(kwargs["stage"] == "service-linger" for _argv, kwargs in installed[1].calls)


def test_unconfirmed_linger_uses_a_system_service(tmp_path):
    calls = []

    def commands(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "no" if kwargs["stage"] == "service-linger-check" else "", "")

    installer.start_service(tmp_path, lambda user: str(user), commands)
    assert any("/etc/systemd/system/" + installer.UNIT in argv for argv in calls)


def test_failed_runtime_validation_never_starts_a_service(tmp_path, payload, payload_file, monkeypatch):
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    commands = Commands(tmp_path, payload)
    commands.fail = "runtime-imports"
    with pytest.raises(installer.InstallationError, match="runtime-imports"):
        installer.install(payload_file, home=tmp_path, runner=commands, downloader=lambda _path: None)
    assert not any(kwargs["stage"] == "service-start" for _argv, kwargs in commands.calls)


def test_download_rejects_an_unexpected_executable(tmp_path, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return installer.BOAT_URL

        def read(self, _limit):
            return b"not an ELF executable"

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(installer.InstallationError, match="boat-download"):
        installer.download_boat(tmp_path / "box")


@pytest.fixture
def health_server():
    class Handler(BaseHTTPRequestHandler):
        received_host = None

        def do_GET(self):
            type(self).received_host = self.headers.get("Host")
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, Handler
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_real_http_health_probe_accepts_only_checked_health(health_server):
    assert installer.check_health("https://workspace.example.test", port=health_server[0]) is True


def test_real_http_health_probe_sends_the_canonical_host(health_server):
    installer.check_health("https://workspace.example.test", port=health_server[0])
    assert health_server[1].received_host == "workspace.example.test"


def test_provider_diagnostics_are_not_exposed(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "fixture-secret", "fixture-secret"))
    with pytest.raises(installer.InstallationError, match="^boat-login$"):
        installer.run(["box", "login"], stage="boat-login")


def test_entrypoint_does_not_log_payload_on_failure(payload_file, monkeypatch, caplog):
    monkeypatch.setattr(installer, "install", lambda _path: (_ for _ in ()).throw(installer.InstallationError("fixture-boat-key")))
    installer.main([str(payload_file)])
    assert "fixture-boat-key" not in caplog.text


def test_entrypoint_logs_a_fixed_safe_failure_stage(payload_file, monkeypatch, caplog):
    monkeypatch.setattr(installer, "install", lambda _path: (_ for _ in ()).throw(installer.InstallationError("repository-fetch")))
    installer.main([str(payload_file)])
    assert "stage=repository-fetch" in caplog.text


def test_entrypoint_help_runs_as_a_standalone_script():
    result = subprocess.run([sys.executable, "-O", installer.__file__, "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0


def test_real_entrypoint_consumes_an_invalid_secret_envelope(payload_file):
    payload_file.write_text('{"boat_token":"fixture-private-key"}')
    result = subprocess.run([sys.executable, "-O", installer.__file__, str(payload_file)], capture_output=True, text=True, check=False)
    assert (result.returncode, payload_file.exists(), "fixture-private-key" in result.stderr) == (1, False, False)


def test_standalone_success_receipt_uses_only_stdout():
    source = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('installer',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "module.install=lambda path:{'healthy':True,'unit':'one-conv-control.service'}; "
        "raise SystemExit(module.main(['fixture-payload']))"
    )
    result = subprocess.run([sys.executable, "-O", "-c", source, installer.__file__],
                            capture_output=True, text=True, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (
        0, '{"healthy": true, "unit": "one-conv-control.service"}\n', "")
