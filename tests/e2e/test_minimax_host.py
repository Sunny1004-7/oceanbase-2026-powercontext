# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Opt-in qualification against a real MiniMax CLI and authenticated SQLite Server."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from typer.testing import CliRunner

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.cli.app import create_cli
from powercontext.server.factory import create_server_app
from powercontext.server.settings import BearerAuthConfig, ServerSettings

COMMAND = os.environ.get("POWERCONTEXT_MINIMAX_ACCEPTANCE_COMMAND", "")
pytestmark = pytest.mark.skipif(
    not COMMAND, reason="Set POWERCONTEXT_MINIMAX_ACCEPTANCE_COMMAND to a real mcode launcher"
)
ROOT = Path(__file__).parents[2]


@pytest.fixture
def host(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("POWERCONTEXT_HOME", str(tmp_path / "pc"))
    monkeypatch.setenv("POWERCONTEXT_CLIENT_CONFIG_FILE", str(tmp_path / "clients.json"))
    authenticated = getattr(request, "param", True)
    if authenticated:
        monkeypatch.setenv("POWERCONTEXT_MINIMAX_AUTHORIZATION", "acceptance-only-token")
    else:
        monkeypatch.delenv("POWERCONTEXT_MINIMAX_AUTHORIZATION", raising=False)
    for name in ("MINIMAX_DATA_DIR", "MAVIS_DATA_DIR", "MAVIS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    app = create_server_app(
        settings=ServerSettings(
            database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'server.db'}"),
            auth=BearerAuthConfig(
                enabled=authenticated, token=SecretStr("acceptance-only-token") if authenticated else None
            ),
        ),
        scheduler_path=tmp_path / "scheduler",
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield tmp_path, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive()


def invoke(*arguments):
    return CliRunner().invoke(create_cli(), list(arguments))


@pytest.mark.parametrize("selection", ["explicit", "minimax_env", "mavis_env"])
def test_native_discovery_repeat_install_and_readonly_doctor(host, monkeypatch, selection):
    root, endpoint = host
    data = root / "MiniMax 原生 profile"
    options = ["--minimax-data-dir", str(data)] if selection == "explicit" else []
    if selection != "explicit":
        monkeypatch.setenv("MINIMAX_DATA_DIR" if selection == "minimax_env" else "MAVIS_DATA_DIR", str(data))
    arguments = [
        "setup",
        "minimax",
        "--source",
        str(ROOT),
        "--minimax-command",
        COMMAND,
        "--server-url",
        endpoint,
        "--json",
        *options,
    ]
    for _ in range(2):
        result = invoke(*arguments)
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)["hosts"]["minimax"]
        assert row["checks"]["native"]["status"] == "ok"
        assert row["checks"]["server_access"]["status"] == "ok"
    private = data / "mcp.json"
    assert "acceptance-only-token" in private.read_text()
    before = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    result = invoke("doctor", "minimax", "--json")
    assert result.exit_code == 0, result.output
    assert {path: path.read_bytes() for path in data.rglob("*") if path.is_file()} == before
    assert "acceptance-only-token" not in result.output
    environment = dict(os.environ, MINIMAX_DATA_DIR=str(data))
    disabled = subprocess.run(
        [COMMAND, "plugin", "disable", "powercontext@local", "--json"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert disabled.returncode == 0, disabled.stderr
    before = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    result = invoke("doctor", "minimax", "--json")
    assert result.exit_code == 1
    assert "disabled" in json.loads(result.output)["hosts"]["minimax"]["checks"]["native"]["detail"]
    assert {path: path.read_bytes() for path in data.rglob("*") if path.is_file()} == before
    # Repeated setup must respect native disablement, rather than silently enabling it.
    result = invoke(*arguments)
    assert result.exit_code == 1
    assert "disabled" in json.loads(result.output)["hosts"]["minimax"]["checks"]["native"]["detail"]


def test_native_loader_omission_and_private_authorization_failure(host):
    root, endpoint = host
    data = root / "omitted"
    result = invoke(
        "setup",
        "minimax",
        "--source",
        str(ROOT),
        "--minimax-command",
        COMMAND,
        "--minimax-data-dir",
        str(data),
        "--server-url",
        endpoint,
        "--json",
    )
    assert result.exit_code == 0, result.output
    private = data / "mcp.json"
    config = json.loads(private.read_text())
    config["mcpServers"]["powercontext"]["headers"]["Authorization"] = "Bearer wrong-acceptance-token"
    private.write_text(json.dumps(config))
    manifest_path = data / "plugins/powercontext/.minimax-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_bytes())
    # Local resource checks pass, but the native V1 loader rejects an unknown field.
    manifest["unsupportedField"] = True
    manifest_path.write_text(json.dumps(manifest))
    result = invoke("doctor", "minimax", "--json")
    assert result.exit_code == 1
    checks = json.loads(result.output)["hosts"]["minimax"]["checks"]
    assert checks["native"]["status"] == "failed"
    assert "omitted" in checks["native"]["detail"]
    assert checks["server_access"]["status"] == "failed"
    assert "401" in checks["server_access"]["detail"]
    assert "wrong-acceptance-token" not in result.output


@pytest.mark.parametrize("host", [False], indirect=True, ids=["anonymous-server"])
@pytest.mark.parametrize("authorization", [False, True], ids=["without-authorization", "with-authorization"])
def test_native_setup_preserves_disabled_private_override_and_probes_bundle(host, authorization):
    root, endpoint = host
    source = root / "bundle"
    shutil.copytree(ROOT / "integrations/minimax/plugins/powercontext", source)
    bundled_path = source / "powercontext.mcp.json"
    bundled = json.loads(bundled_path.read_bytes())
    bundled["mcpServers"]["powercontext"]["url"] = endpoint + "/mcp"
    bundled_path.write_text(json.dumps(bundled))
    data = root / "disabled-private"
    data.mkdir()
    private = data / "mcp.json"
    entry = {"type": "streamable-http", "url": "https://inactive.example/mcp", "enabled": False}
    if authorization:
        entry["headers"] = {"Authorization": "Bearer inactive-private-token"}
    original = (
        json.dumps({"note": "保留格式和禁用连接", "mcpServers": {"powercontext": entry}}, ensure_ascii=False, indent=4)
        + "\n"
    ).encode()
    private.write_bytes(original)
    for _ in range(2):
        result = invoke(
            "setup",
            "minimax",
            "--source",
            str(source),
            "--minimax-command",
            COMMAND,
            "--minimax-data-dir",
            str(data),
            "--json",
        )
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)["hosts"]["minimax"]
        assert row["checks"]["native"]["status"] == "ok"
        assert row["checks"]["server_access"]["status"] == "ok"
        assert endpoint in row["checks"]["server_liveness"]["detail"]
        assert row["result"]["private_mcp_state"] == "disabled"
        assert private.read_bytes() == original
        assert "inactive-private-token" not in result.output
    result = invoke("doctor", "minimax", "--json")
    assert result.exit_code == 0, result.output
    assert private.read_bytes() == original
    assert endpoint in json.loads(result.output)["hosts"]["minimax"]["checks"]["server_liveness"]["detail"]


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return
        yield from _objects(decoded)


class ModelGateway(ThreadingHTTPServer):
    def __init__(self, *, plugin_connection=False):
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.requests: list[dict[str, Any]] = []
        self.plugin_connection = plugin_connection
        self.invoked_tools: list[str] = []


class ModelHandler(BaseHTTPRequestHandler):
    server: ModelGateway

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - match the HTTP handler interface.
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        messages = body["messages"]
        tool_results = [message for message in messages if message["role"] == "tool"]
        names = [tool["function"]["name"] for tool in body.get("tools", [])]
        selected = None
        arguments = {}
        if not tool_results and "tool_search" in names:
            selected = "tool_search"
            arguments = {"query": "list_scopes", "top_k": 20}
        elif tool_results and len(tool_results) == 1 and "mcp_invoke" in names:
            match = next(
                (
                    item["name"]
                    for item in _objects(tool_results[0]["content"])
                    if isinstance(item.get("name"), str)
                    and "list_scopes" in item["name"]
                    and (not self.server.plugin_connection or "powercontext_2" in item["name"])
                ),
                None,
            )
            if match:
                selected = "mcp_invoke"
                arguments = {"tool_name": match, "arguments": {"limit": 10}}
                self.server.invoked_tools.append(match)
        elif not tool_results:
            selected = next(
                (
                    name
                    for name in names
                    if "list_scopes" in name and (not self.server.plugin_connection or "powercontext_2" in name)
                ),
                None,
            )
            arguments = {"limit": 10}
            if selected:
                self.server.invoked_tools.append(selected)
        delta: dict[str, Any] = {"content": "MINIMAX_MCP_ACCEPTANCE_OK"}
        finish = "stop"
        if selected:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call-{len(tool_results)}",
                        "type": "function",
                        "function": {"name": selected, "arguments": json.dumps(arguments)},
                    }
                ]
            }
            finish = "tool_calls"
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for content in [
                {
                    "id": "acceptance",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                },
                {
                    "id": "acceptance",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": "acceptance",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                },
            ]:
                self.wfile.write(f"data: {json.dumps(content)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({
                    "id": "acceptance",
                    "object": "chat.completion",
                    "model": "fixture",
                    "choices": [{"index": 0, "message": {"role": "assistant", **delta}, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                }).encode()
            )


@pytest.mark.parametrize("plugin_connection", [False, True], ids=["private-mcp", "plugin-mcp"])
def test_real_host_invokes_powercontext_mcp_with_controlled_inference(host, plugin_connection):
    root, endpoint = host
    data = root / "mcp-tool"
    result = invoke(
        "setup",
        "minimax",
        "--source",
        str(ROOT),
        "--minimax-command",
        COMMAND,
        "--minimax-data-dir",
        str(data),
        "--server-url",
        endpoint,
        "--json",
    )
    assert result.exit_code == 0, result.output
    marker = "MiniMax native MCP scope evidence"
    with httpx.Client(
        base_url=endpoint, headers={"Authorization": "Bearer acceptance-only-token"}, timeout=10
    ) as client:
        response = client.post(
            "/v1/scopes",
            json={"title": marker, "summary": "Isolated native MCP acceptance", "idempotency_key": "minimax-scope"},
        )
        assert response.status_code == 201, response.text
    gateway = ModelGateway(plugin_connection=plugin_connection)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    environment = dict(os.environ, MINIMAX_DATA_DIR=str(data), MCODE_PROVIDER_API_KEY="synthetic-model-token")
    try:
        added = subprocess.run(
            [
                COMMAND,
                "provider",
                "add",
                "--name",
                "acceptance",
                "--base-url",
                f"http://127.0.0.1:{gateway.server_port}/v1",
                "--api-format",
                "openai-completions",
                "--model",
                "fixture",
                "--use",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert added.returncode == 0, added.stderr
        executed = subprocess.run(
            [
                COMMAND,
                "exec",
                "List PowerContext scopes using MCP; return the scope titles.",
                "--permission",
                "full",
                "--max-steps",
                "5",
                "--timeout",
                "40s",
                "--output-format",
                "json",
                "--cwd",
                str(root),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert executed.returncode == 0, executed.stdout + executed.stderr
        observed = [
            message
            for request in gateway.requests
            for message in request.get("messages", [])
            if message["role"] == "tool"
        ]
        assert any(marker in json.dumps(message, ensure_ascii=False) for message in observed), executed.stdout
        assert any("powercontext-project-context" in json.dumps(request) for request in gateway.requests)
        assert gateway.invoked_tools
        if plugin_connection:
            assert any("powercontext_2" in name for name in gateway.invoked_tools)
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=5)


def test_real_doctor_does_not_initialize_an_uninstalled_native_target(host):
    root, _ = host
    data = root / "uninstalled native profile"
    result = invoke("doctor", "minimax", "--minimax-data-dir", str(data), "--minimax-command", COMMAND, "--json")
    assert result.exit_code == 1
    assert not data.exists()
