"""Safe-startup contract for the local vLLM server.

The documented start path is ``bash scripts/run_vllm_a100.sh``. These tests
run that script and ``scripts/bootstrap_dell_vllm.sh``. They fail while the
server still binds every interface, starts with no API key, or always passes
``--trust-remote-code``.

Operator controls the tests expect:

- ``VLLM_API_KEY``: required. The process must exit before launching vLLM when
  this variable is unset, empty, or equal to the client placeholder stored in
  ``config/tiz_cutting_tools.example.yaml``. When it is set to any other
  value, that same value is passed as ``--api-key``.
- ``HOST``: defaults to ``127.0.0.1``. An explicit value is still forwarded.
- ``VLLM_TRUST_REMOTE_CODE=1``: the only value that may pass
  ``--trust-remote-code``. Any other value, including an unset variable, must
  not.

``scripts/check_dell_vllm.sh`` only probes an already running server. Example
client ``api_key`` values are not read by the server launcher. Neither is
asserted here.
"""

from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN_SCRIPT = REPO / "scripts" / "run_vllm_a100.sh"
BOOTSTRAP_SCRIPT = REPO / "scripts" / "bootstrap_dell_vllm.sh"
EXAMPLE_CONFIG = REPO / "config" / "tiz_cutting_tools.example.yaml"
PLACEHOLDER_KEY = "placeholder-not-a-secret"


def _example_client_api_key() -> str:
    for line in EXAMPLE_CONFIG.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("api_key:"):
            return stripped.split(":", 1)[1].strip().strip("\"'")
    raise AssertionError(f"api_key entry missing from {EXAMPLE_CONFIG}")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _recorder(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
if [ -z "${CAPTURE_ARGV:-}" ]; then
  echo "CAPTURE_ARGV is not set" >&2
  exit 2
fi
printf '%s\\0' "$@" > "$CAPTURE_ARGV"
exit 0
""",
    )


def _nvidia_smi(path: Path, gpu_lines: int = 4) -> None:
    quoted = " ".join(f"'GPU {index}: stub'" for index in range(gpu_lines))
    _write_executable(
        path,
        f"""#!/bin/sh
if [ "${{1:-}}" = "-L" ]; then
  printf '%s\\n' {quoted}
fi
exit 0
""",
    )


def _non_loopback_ipv4() -> str:
    text = Path("/proc/net/fib_trie").read_text(encoding="utf-8").splitlines()
    found: list[str] = []
    for index, line in enumerate(text):
        if "32 host LOCAL" not in line or index == 0:
            continue
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", text[index - 1])
        if match and not match.group(1).startswith("127."):
            found.append(match.group(1))
    if not found:
        pytest.fail("no non-loopback local IPv4 address is available to probe the bind")
    return sorted(set(found))[0]


def _parse_argv(capture: Path) -> list[str] | None:
    if not capture.exists() or capture.stat().st_size == 0:
        return None
    parts = capture.read_bytes().split(b"\0")
    return [part.decode() for part in parts if part]


def _flag_values(argv: list[str], flag: str) -> list[str]:
    values: list[str] = []
    prefix = flag + "="
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == flag:
            if index + 1 >= len(argv):
                pytest.fail(f"{flag} is missing its value in {argv}")
            values.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith(prefix):
            values.append(arg[len(prefix) :])
        index += 1
    return values


def _has_switch(argv: list[str], flag: str) -> bool:
    return flag in argv or any(arg.startswith(flag + "=") for arg in argv)


def _base_env(tmp_path: Path, capture: Path) -> dict[str, str]:
    stub = tmp_path / "bin"
    stub.mkdir(parents=True, exist_ok=True)
    _recorder(stub / "python")
    _recorder(stub / "python3")
    _nvidia_smi(stub / "nvidia-smi")
    env = os.environ.copy()
    for name in (
        "HOST",
        "VLLM_API_KEY",
        "MARKET_AGENTS_LLM_API_KEY",
        "PYTHON",
        "VLLM_TRUST_REMOTE_CODE",
        "MODEL",
        "PORT",
        "TP",
    ):
        env.pop(name, None)
    env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    env["PYTHON"] = str(stub / "python")
    env["CAPTURE_ARGV"] = str(capture)
    return env


def _run_server(tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess[str], list[str] | None]:
    capture = tmp_path / "argv.bin"
    env = _base_env(tmp_path, capture)
    env.update(extra)
    proc = subprocess.run(
        ["bash", str(RUN_SCRIPT)],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc, _parse_argv(capture)


def _assert_not_launched(proc: subprocess.CompletedProcess[str], argv: list[str] | None) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert argv is None, argv


def _bind_like_vllm(host: str) -> socket.socket:
    """Same socket setup as vLLM ``create_server_socket`` (AF_INET, SO_REUSEADDR, bind)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(1)
    return sock


def _connect(host: str, port: int) -> bool:
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(1)
    try:
        client.connect((host, port))
    except OSError:
        return False
    else:
        return True
    finally:
        client.close()


def test_default_listen_rejects_non_loopback_clients(tmp_path: Path) -> None:
    proc, argv = _run_server(tmp_path, VLLM_API_KEY=PLACEHOLDER_KEY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert argv is not None
    hosts = _flag_values(argv, "--host")
    assert hosts == ["127.0.0.1"], argv

    remote = _non_loopback_ipv4()
    server = _bind_like_vllm(hosts[0])
    port = server.getsockname()[1]

    def _accept_one() -> None:
        try:
            server.settimeout(2)
            conn, _ = server.accept()
            conn.close()
        except OSError:
            return

    thread = threading.Thread(target=_accept_one, daemon=True)
    thread.start()
    try:
        assert _connect("127.0.0.1", port)
        assert not _connect(remote, port)
    finally:
        server.close()
        thread.join(timeout=2)


def test_explicit_host_is_forwarded(tmp_path: Path) -> None:
    remote = _non_loopback_ipv4()
    proc, argv = _run_server(tmp_path, VLLM_API_KEY=PLACEHOLDER_KEY, HOST=remote)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert argv is not None
    assert _flag_values(argv, "--host") == [remote]


def test_server_does_not_start_without_api_key(tmp_path: Path) -> None:
    proc, argv = _run_server(tmp_path)
    _assert_not_launched(proc, argv)


def test_server_does_not_start_with_empty_api_key(tmp_path: Path) -> None:
    proc, argv = _run_server(tmp_path, VLLM_API_KEY="")
    _assert_not_launched(proc, argv)


def test_server_does_not_start_with_example_placeholder_api_key(tmp_path: Path) -> None:
    placeholder = _example_client_api_key()
    assert placeholder
    proc, argv = _run_server(tmp_path, VLLM_API_KEY=placeholder)
    _assert_not_launched(proc, argv)


def test_configured_api_key_is_the_key_passed_to_vllm(tmp_path: Path) -> None:
    proc, argv = _run_server(tmp_path, VLLM_API_KEY=PLACEHOLDER_KEY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert argv is not None
    assert _flag_values(argv, "--api-key") == [PLACEHOLDER_KEY]
    assert not _has_switch(argv, "--trust-remote-code")


def test_trust_remote_code_is_off_unless_explicitly_enabled(tmp_path: Path) -> None:
    proc, argv = _run_server(
        tmp_path,
        VLLM_API_KEY=PLACEHOLDER_KEY,
        MODEL="local-custom-model",
        VLLM_TRUST_REMOTE_CODE="0",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert argv is not None
    assert not _has_switch(argv, "--trust-remote-code")

    proc, argv = _run_server(tmp_path, VLLM_API_KEY=PLACEHOLDER_KEY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert argv is not None
    assert not _has_switch(argv, "--trust-remote-code")


def test_trust_remote_code_opt_in_still_requires_the_api_key(tmp_path: Path) -> None:
    refused, refused_argv = _run_server(tmp_path, VLLM_TRUST_REMOTE_CODE="1")
    _assert_not_launched(refused, refused_argv)

    proc, argv = _run_server(
        tmp_path,
        VLLM_API_KEY=PLACEHOLDER_KEY,
        VLLM_TRUST_REMOTE_CODE="1",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert argv is not None
    assert _has_switch(argv, "--trust-remote-code")
    assert _flag_values(argv, "--api-key") == [PLACEHOLDER_KEY]
    assert _flag_values(argv, "--host") == ["127.0.0.1"]


def _prepare_bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "scripts").mkdir()
    (repo / "requirements.txt").write_text("placeholder\n", encoding="utf-8")
    (repo / "config" / "dell_vpn.example.yaml").write_text("llm: {}\n", encoding="utf-8")
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_executable(venv_bin / "python3", "#!/bin/sh\nexit 0\n")
    _write_executable(venv_bin / "activate", "#!/bin/sh\n# no-op for the test harness\n")
    return repo


def _bootstrap_env(tmp_path: Path, repo: Path) -> dict[str, str]:
    stub = tmp_path / "bootstrap-bin"
    stub.mkdir()
    for name in ("python", "python3", "pip", "nvidia-smi", "git", "sudo", "apt-get"):
        _write_executable(
            stub / name,
            f"""#!/bin/sh
printf '%s\\n' "{name} $*" >> "{tmp_path / "calls.txt"}"
exit 0
""",
        )
    env = os.environ.copy()
    env.pop("HOST", None)
    env.pop("VLLM_API_KEY", None)
    env["HOME"] = str(tmp_path / "home")
    env["REPO_DIR"] = str(repo)
    env["INSTALL_VLLM"] = "0"
    env["PATH"] = str(stub) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
    (tmp_path / "home").mkdir()
    return env


def test_bootstrap_env_does_not_force_every_interface(tmp_path: Path) -> None:
    repo = _prepare_bootstrap_repo(tmp_path)
    env = _bootstrap_env(tmp_path, repo)
    proc = subprocess.run(
        ["bash", str(BOOTSTRAP_SCRIPT)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8")
    assert "sudo " not in calls
    env_file = tmp_path / "home" / ".tiz_dell_env"
    assert env_file.is_file()

    # Source the generated file, then launch the real server script. A later
    # HOST=0.0.0.0 export would override a safe default inside the launcher.
    capture = tmp_path / "argv.bin"
    launch_env = _base_env(tmp_path / "launch", capture)
    launch_env["HOME"] = env["HOME"]
    launch_env["VLLM_API_KEY"] = PLACEHOLDER_KEY
    sourced = subprocess.run(
        [
            "bash",
            "-c",
            'set -a; source "$ENV_FILE"; set +a; exec bash "$RUN_SCRIPT"',
        ],
        cwd=REPO,
        env={
            **launch_env,
            "ENV_FILE": str(env_file),
            "RUN_SCRIPT": str(RUN_SCRIPT),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    argv = _parse_argv(capture)
    assert sourced.returncode == 0, sourced.stdout + sourced.stderr
    assert argv is not None
    assert _flag_values(argv, "--host") == ["127.0.0.1"], argv
