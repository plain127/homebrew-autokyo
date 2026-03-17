from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from autokyo import __version__
from autokyo.service import (
    DEFAULT_CAPTURES_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_PDF_OUTPUT,
    build_pdf,
    format_payload,
    get_mouse_position_payload,
    get_session_status,
    probe_region,
    run_capture_session,
)


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


ToolHandler = Callable[[dict[str, Any]], dict[str, object]]


class AutokyoMCPServer:
    def __init__(self, *, default_config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        self.default_config_path = str(Path(default_config_path))
        self._initialized = False
        self._tool_handlers: dict[str, ToolHandler] = {
            "run_capture_session": self._tool_run_capture_session,
            "get_session_status": self._tool_get_session_status,
            "probe_region": self._tool_probe_region,
            "get_mouse_position": self._tool_get_mouse_position,
            "build_pdf": self._tool_build_pdf,
        }

    def serve(self) -> int:
        input_stream = sys.stdin.buffer
        output_stream = sys.stdout.buffer

        while True:
            message = self._read_message(input_stream)
            if message is None:
                return 0

            if "id" not in message:
                self._handle_notification(message)
                continue

            response = self._handle_request(message)
            self._write_message(output_stream, response)

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method in {"notifications/initialized", "initialized"}:
            self._initialized = True

    def _handle_request(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id")
        try:
            self._validate_request(message)
            method = message["method"]
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise JsonRpcError(-32602, "params must be an object")

            if method == "initialize":
                self._initialized = True
                return self._result(
                    request_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "autokyo",
                            "version": __version__,
                        },
                        "instructions": (
                            "Use the Autokyo tools to inspect status, probe the configured page region, "
                            "run the local capture loop, or build a PDF from captures."
                        ),
                    },
                )

            if method == "ping":
                return self._result(request_id, {})

            if method == "tools/list":
                return self._result(request_id, {"tools": self._tool_definitions()})

            if method == "tools/call":
                if not self._initialized:
                    raise JsonRpcError(-32002, "Server must be initialized before tool calls")
                return self._result(request_id, self._call_tool(params))

            if method == "resources/list":
                return self._result(request_id, {"resources": []})

            if method == "prompts/list":
                return self._result(request_id, {"prompts": []})

            raise JsonRpcError(-32601, f"Method not found: {method}")
        except JsonRpcError as exc:
            return self._error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive fallback
            return self._error(request_id, -32603, str(exc))

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})

        if not isinstance(name, str) or not name:
            raise JsonRpcError(-32602, "tools/call.params.name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise JsonRpcError(-32602, "tools/call.params.arguments must be an object")

        handler = self._tool_handlers.get(name)
        if handler is None:
            raise JsonRpcError(-32602, f"Unknown tool: {name}")

        try:
            payload = handler(arguments)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": format_payload(payload),
                    }
                ],
                "structuredContent": payload,
            }
        except Exception as exc:
            return {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": f"ERROR: {exc}",
                    }
                ],
            }

    def _tool_run_capture_session(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        self._ensure_no_extra_args(arguments, {"config_path"})
        return run_capture_session(config_path)

    def _tool_get_session_status(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        self._ensure_no_extra_args(arguments, {"config_path"})
        return get_session_status(config_path)

    def _tool_probe_region(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        self._ensure_no_extra_args(arguments, {"config_path"})
        return probe_region(config_path)

    def _tool_get_mouse_position(self, arguments: dict[str, Any]) -> dict[str, object]:
        self._ensure_no_extra_args(arguments, set())
        return get_mouse_position_payload()

    def _tool_build_pdf(self, arguments: dict[str, Any]) -> dict[str, object]:
        input_dir = self._string_arg(arguments, "input_dir", str(DEFAULT_CAPTURES_DIR))
        output_file = self._string_arg(arguments, "output_file", str(DEFAULT_PDF_OUTPUT))
        sort_by = self._string_arg(arguments, "sort_by", "auto")
        delete_source = self._bool_arg(arguments, "delete_source", False)
        self._ensure_no_extra_args(arguments, {"input_dir", "output_file", "sort_by", "delete_source"})
        return build_pdf(
            input_dir=input_dir,
            output_file=output_file,
            sort_by=sort_by,
            delete_source=delete_source,
        )

    def _tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "run_capture_session",
                "description": "Run the configured local capture loop using config.toml or a supplied config path.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to ./config.toml",
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_session_status",
                "description": "Read the current session JSON state for the configured capture run.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to ./config.toml",
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "probe_region",
                "description": "Capture the configured page-change region once and return its digest and saved sample path.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to ./config.toml",
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_mouse_position",
                "description": "Return the current macOS mouse pointer coordinates.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            {
                "name": "build_pdf",
                "description": "Combine images from a folder into one PDF and optionally delete the source files.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "input_dir": {
                            "type": "string",
                            "description": "Input image directory. Defaults to ./captures",
                        },
                        "output_file": {
                            "type": "string",
                            "description": "Output PDF path. Defaults to ./exports/captures.pdf",
                        },
                        "sort_by": {
                            "type": "string",
                            "enum": ["auto", "created", "modified", "name"],
                            "description": "Image ordering strategy. Defaults to auto",
                        },
                        "delete_source": {
                            "type": "boolean",
                            "description": "Delete source images after the PDF is created successfully",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        ]

    def _string_arg(self, arguments: dict[str, Any], key: str, default: str) -> str:
        value = arguments.get(key, default)
        if not isinstance(value, str):
            raise JsonRpcError(-32602, f"{key} must be a string")
        return value

    def _bool_arg(self, arguments: dict[str, Any], key: str, default: bool) -> bool:
        value = arguments.get(key, default)
        if not isinstance(value, bool):
            raise JsonRpcError(-32602, f"{key} must be a boolean")
        return value

    def _ensure_no_extra_args(self, arguments: dict[str, Any], allowed: set[str]) -> None:
        extras = sorted(set(arguments) - allowed)
        if extras:
            raise JsonRpcError(-32602, f"Unexpected arguments: {', '.join(extras)}")

    def _validate_request(self, message: dict[str, Any]) -> None:
        if message.get("jsonrpc") != "2.0":
            raise JsonRpcError(-32600, "jsonrpc must be '2.0'")
        if "method" not in message or not isinstance(message["method"], str):
            raise JsonRpcError(-32600, "method must be a string")

    def _result(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str, data: object | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def _read_message(self, stream: Any) -> dict[str, Any] | None:
        content_length: int | None = None

        while True:
            line = stream.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            header = line.decode("utf-8").strip()
            if not header:
                break
            name, _, value = header.partition(":")
            if name.lower() == "content-length":
                content_length = int(value.strip())

        if content_length is None:
            raise JsonRpcError(-32700, "Missing Content-Length header")

        payload = stream.read(content_length)
        if len(payload) != content_length:
            raise JsonRpcError(-32700, "Unexpected EOF while reading message body")

        try:
            message = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise JsonRpcError(-32700, f"Invalid JSON: {exc}") from exc
        if not isinstance(message, dict):
            raise JsonRpcError(-32600, "Request body must be a JSON object")
        return message

    def _write_message(self, stream: Any, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        stream.write(header)
        stream.write(payload)
        stream.flush()


def run_stdio_server(*, default_config_path: str | Path = DEFAULT_CONFIG_PATH) -> int:
    return AutokyoMCPServer(default_config_path=default_config_path).serve()
