from __future__ import annotations

from dataclasses import dataclass
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from autokyo import __version__
from autokyo.service import (
    DEFAULT_CAPTURES_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_PDF_OUTPUT,
    DEFAULT_PHOTOS_LIBRARY_DB,
    build_pdf,
    capture_to_pdf,
    format_payload,
    get_mouse_position_payload,
    get_session_status,
    probe_region,
    run_capture_session,
)
from autokyo.setup_flow import SetupDraft, SetupDraftStore


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


ToolHandler = Callable[[dict[str, Any]], dict[str, object]]
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEBUG_LOG_PATH = Path("/tmp/autokyo-mcp-debug.log")
STDIO_MODE_NDJSON = "ndjson"
STDIO_MODE_CONTENT_LENGTH = "content-length"


def _debug_log(message: str) -> None:
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{timestamp} pid={os.getpid()} {message}\n")
    except OSError:
        pass


def _install_signal_debug_handlers() -> None:
    def _handler(signum: int, _frame: object) -> None:
        _debug_log(f"signal {signum}")
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGPIPE):
        try:
            signal.signal(signum, _handler)
        except (ValueError, OSError):
            continue

SERVER_INSTRUCTIONS = (
    "You are operating a local AutoKyo workflow on macOS. "
    "Choose AutoKyo tools yourself so the user can speak in short natural language. "
    "When the user asks to set up or configure AutoKyo, prefer setup_autokyo and follow its returned next_action. "
    "If setup_autokyo is not suitable, fall back to setup_capture_button, setup_confirm_button, setup_change_region, and save_config. "
    "Never ask the user to edit config.toml manually when setup tools can do the job. "
    "When the user asks to make a book PDF, prefer capture_to_pdf. "
    "When the user asks for status, mouse position, probing, capture, or PDF creation, call the matching tool directly. "
    "Keep follow-up questions short and only ask for missing information."
)


@dataclass
class GuidedSetupState:
    phase: str = "prompt_capture_button"


class GuidedSetupStore:
    def __init__(self) -> None:
        self._states: dict[str, GuidedSetupState] = {}

    def get(self, config_path: str | Path) -> GuidedSetupState:
        key = str(Path(config_path).expanduser().resolve())
        state = self._states.get(key)
        if state is None:
            state = GuidedSetupState()
            self._states[key] = state
        return state

    def reset(self, config_path: str | Path) -> GuidedSetupState:
        key = str(Path(config_path).expanduser().resolve())
        state = GuidedSetupState()
        self._states[key] = state
        return state

    def clear(self, config_path: str | Path) -> None:
        key = str(Path(config_path).expanduser().resolve())
        self._states.pop(key, None)


class AutokyoMCPServer:
    def __init__(self, *, default_config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        self.default_config_path = str(Path(default_config_path))
        self._initialized = False
        self._setup_drafts = SetupDraftStore()
        self._guided_setup = GuidedSetupStore()
        self._tool_handlers: dict[str, ToolHandler] = {
            "setup_autokyo": self._tool_setup_autokyo,
            "setup_capture_button": self._tool_setup_capture_button,
            "setup_confirm_button": self._tool_setup_confirm_button,
            "setup_change_region": self._tool_setup_change_region,
            "save_config": self._tool_save_config,
            "capture_to_pdf": self._tool_capture_to_pdf,
            "run_capture_session": self._tool_run_capture_session,
            "get_session_status": self._tool_get_session_status,
            "probe_region": self._tool_probe_region,
            "get_mouse_position": self._tool_get_mouse_position,
            "build_pdf": self._tool_build_pdf,
        }

    def serve(self) -> int:
        _install_signal_debug_handlers()
        input_stream = sys.stdin.buffer
        output_stream = sys.stdout.buffer
        stdio_mode: str | None = None
        _debug_log("serve.start")

        while True:
            message, detected_mode = self._read_message(input_stream)
            if message is None:
                _debug_log("serve.eof")
                return 0
            if stdio_mode is None:
                stdio_mode = detected_mode
                _debug_log(f"serve.mode {stdio_mode}")

            if "id" not in message:
                _debug_log(f"notification method={message.get('method')}")
                self._handle_notification(message)
                continue

            _debug_log(f"request method={message.get('method')} id={message.get('id')}")
            response = self._handle_request(message)
            self._write_message(output_stream, response, mode=stdio_mode or STDIO_MODE_NDJSON)
            _debug_log(f"response.write id={response.get('id')}")

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
                requested_protocol_version = params.get("protocolVersion")
                protocol_version = self._resolve_protocol_version(requested_protocol_version)
                self._initialized = True
                return self._result(
                    request_id,
                    {
                        "protocolVersion": protocol_version,
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                            "prompts": {},
                        },
                        "serverInfo": {
                            "name": "autokyo",
                            "version": __version__,
                        },
                        "instructions": SERVER_INSTRUCTIONS,
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

            if method == "resources/templates/list":
                return self._result(request_id, {"resourceTemplates": []})

            if method == "prompts/list":
                return self._result(request_id, {"prompts": []})

            raise JsonRpcError(-32601, f"Method not found: {method}")
        except JsonRpcError as exc:
            _debug_log(f"request.error code={exc.code} method={message.get('method')} message={exc.message}")
            return self._error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive fallback
            _debug_log(f"request.exception method={message.get('method')} error={exc!r}")
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

    def _tool_setup_autokyo(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        reset = self._bool_arg(arguments, "reset", False)
        probe_after_save = self._bool_arg(arguments, "probe_after_save", True)
        confirm_delay_ms = self._optional_int_arg(arguments, "confirm_delay_ms")
        region = self._optional_region_args(arguments)
        self._ensure_no_extra_args(
            arguments,
            {
                "config_path",
                "reset",
                "probe_after_save",
                "confirm_delay_ms",
                "x",
                "y",
                "width",
                "height",
            },
        )

        if reset:
            draft = self._setup_drafts.reset(config_path)
            state = self._guided_setup.reset(config_path)
        else:
            draft = self._setup_drafts.get(config_path)
            state = self._guided_setup.get(config_path)

        payload = draft.summary()

        if state.phase == "prompt_capture_button":
            state.phase = "read_capture_button"
            payload["status"] = "needs_user_action"
            payload["next_action"] = "hover_capture_button"
            payload["message"] = "Ask the user to hover over the capture button, then call setup_autokyo again."
            return payload

        if state.phase == "read_capture_button":
            position = get_mouse_position_payload()
            draft.set_capture_button(position["x"], position["y"])
            state.phase = "read_confirm_button"
            payload = draft.summary()
            payload["status"] = "needs_user_action"
            payload["updated"] = "capture_button"
            payload["next_action"] = "hover_confirm_button"
            payload["message"] = "Ask the user to hover over the confirm button, then call setup_autokyo again."
            return payload

        if state.phase == "read_confirm_button":
            position = get_mouse_position_payload()
            resolved_delay_ms = draft.set_confirm_button(
                position["x"],
                position["y"],
                delay_ms=confirm_delay_ms,
            )
            payload = draft.summary()
            payload["updated"] = "confirm_button"
            payload["confirm_delay_ms"] = resolved_delay_ms
            if region is None:
                state.phase = "read_change_region"
                payload["status"] = "needs_user_input"
                payload["next_action"] = "provide_change_region"
                payload["message"] = "Ask the user for page-change region x y width height, then call setup_autokyo with those values."
                return payload
            return self._complete_guided_setup(
                config_path=config_path,
                draft=draft,
                state=state,
                region=region,
                probe_after_save=probe_after_save,
                base_payload=payload,
            )

        if state.phase == "read_change_region":
            if region is None:
                payload["status"] = "needs_user_input"
                payload["next_action"] = "provide_change_region"
                payload["message"] = "Ask the user for page-change region x y width height, then call setup_autokyo with those values."
                return payload
            return self._complete_guided_setup(
                config_path=config_path,
                draft=draft,
                state=state,
                region=region,
                probe_after_save=probe_after_save,
                base_payload=payload,
            )

        self._guided_setup.clear(config_path)
        payload["status"] = "completed"
        payload["message"] = "Setup is already complete. Call setup_autokyo with reset=true to run it again."
        return payload

    def _tool_setup_capture_button(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        self._ensure_no_extra_args(arguments, {"config_path"})
        draft = self._setup_drafts.get(config_path)
        position = get_mouse_position_payload()
        draft.set_capture_button(position["x"], position["y"])
        payload = draft.summary()
        payload["status"] = "staged"
        payload["updated"] = "capture_button"
        return payload

    def _tool_setup_confirm_button(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        delay_ms = self._optional_int_arg(arguments, "delay_ms")
        self._ensure_no_extra_args(arguments, {"config_path", "delay_ms"})
        draft = self._setup_drafts.get(config_path)
        position = get_mouse_position_payload()
        resolved_delay_ms = draft.set_confirm_button(
            position["x"],
            position["y"],
            delay_ms=delay_ms,
        )
        payload = draft.summary()
        payload["status"] = "staged"
        payload["updated"] = "confirm_button"
        payload["confirm_delay_ms"] = resolved_delay_ms
        return payload

    def _tool_setup_change_region(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        x = self._required_int_arg(arguments, "x")
        y = self._required_int_arg(arguments, "y")
        width = self._required_int_arg(arguments, "width")
        height = self._required_int_arg(arguments, "height")
        self._ensure_no_extra_args(arguments, {"config_path", "x", "y", "width", "height"})
        draft = self._setup_drafts.get(config_path)
        draft.set_change_region(x, y, width, height)
        payload = draft.summary()
        payload["status"] = "staged"
        payload["updated"] = "change_region"
        return payload

    def _tool_save_config(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        self._ensure_no_extra_args(arguments, {"config_path"})
        draft = self._setup_drafts.get(config_path)
        saved_path = draft.save()
        payload = draft.summary()
        payload["status"] = "completed"
        payload["saved_path"] = str(saved_path)
        return payload

    def _tool_capture_to_pdf(self, arguments: dict[str, Any]) -> dict[str, object]:
        config_path = self._string_arg(arguments, "config_path", self.default_config_path)
        input_dir = self._string_arg(arguments, "input_dir", str(DEFAULT_CAPTURES_DIR))
        output_file = self._string_arg(arguments, "output_file", str(DEFAULT_PDF_OUTPUT))
        sort_by = self._string_arg(arguments, "sort_by", "name")
        delete_source = self._bool_arg(arguments, "delete_source", False)
        probe_first = self._bool_arg(arguments, "probe_first", True)
        library_db = self._string_arg(arguments, "library_db", str(DEFAULT_PHOTOS_LIBRARY_DB))
        time_padding_seconds = self._float_arg(arguments, "time_padding_seconds", 5.0)
        take_last = self._optional_int_arg(arguments, "take_last")
        match_width = self._optional_int_arg(arguments, "match_width")
        match_height = self._optional_int_arg(arguments, "match_height")
        clear_output = self._bool_arg(arguments, "clear_output", True)
        allow_fewer = self._bool_arg(arguments, "allow_fewer", False)
        self._ensure_no_extra_args(
            arguments,
            {
                "config_path",
                "input_dir",
                "output_file",
                "sort_by",
                "delete_source",
                "probe_first",
                "library_db",
                "time_padding_seconds",
                "take_last",
                "match_width",
                "match_height",
                "clear_output",
                "allow_fewer",
            },
        )
        return capture_to_pdf(
            config_path=config_path,
            input_dir=input_dir,
            output_file=output_file,
            sort_by=sort_by,
            delete_source=delete_source,
            probe_first=probe_first,
            library_db=library_db,
            time_padding_seconds=time_padding_seconds,
            take_last=take_last,
            match_width=match_width,
            match_height=match_height,
            clear_output=clear_output,
            allow_fewer=allow_fewer,
        )

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
                "name": "setup_autokyo",
                "description": (
                    "High-level guided setup flow. Call it repeatedly: it tells you what to ask next, "
                    "reads the hovered button positions when appropriate, saves config.toml, and can probe after save."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to the server config path.",
                        },
                        "reset": {
                            "type": "boolean",
                            "description": "Start a fresh guided setup session from the first step.",
                        },
                        "probe_after_save": {
                            "type": "boolean",
                            "description": "Run probe_region automatically after save. Defaults to true.",
                        },
                        "confirm_delay_ms": {
                            "type": "integer",
                            "description": "Optional delay after the confirm-button click.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "setup_capture_button",
                "description": (
                    "After the user hovers the mouse over the capture button, read the current pointer "
                    "coordinates and stage them as triggers.capture in config.toml."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to the server config path.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "setup_confirm_button",
                "description": (
                    "After the user hovers the mouse over the confirm button, read the current pointer "
                    "coordinates and stage them as the first capture.post_steps mouse_click."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to the server config path.",
                        },
                        "delay_ms": {
                            "type": "integer",
                            "description": "Optional delay after the confirm-button click. Defaults to the existing value or 2000ms.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "setup_change_region",
                "description": "Stage page.change_region using user-provided x, y, width, and height values.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to the server config path.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["x", "y", "width", "height"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "save_config",
                "description": "Persist the staged setup values into config.toml. Use after the setup_* tools.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to the server config path.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "capture_to_pdf",
                "description": (
                    "High-level end-to-end flow for making a PDF: optionally probe the page region, "
                    "run the capture session, export matching Photos images into captures, and build one PDF."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "description": "Optional path to a TOML config file. Defaults to the server config path.",
                        },
                        "input_dir": {
                            "type": "string",
                            "description": "Destination captures directory for the Photos export. Defaults to ./captures",
                        },
                        "output_file": {
                            "type": "string",
                            "description": f"Output PDF path. Defaults to {DEFAULT_PDF_OUTPUT}",
                        },
                        "sort_by": {
                            "type": "string",
                            "enum": ["auto", "created", "modified", "name"],
                            "description": "Image ordering strategy for the PDF. Defaults to name",
                        },
                        "delete_source": {
                            "type": "boolean",
                            "description": "Delete exported captures after the PDF is created successfully, then delete the matching Photos items.",
                        },
                        "probe_first": {
                            "type": "boolean",
                            "description": "Run probe_region before capture. Defaults to true.",
                        },
                        "library_db": {
                            "type": "string",
                            "description": "Photos.sqlite path. Defaults to the standard Photos library database.",
                        },
                        "time_padding_seconds": {
                            "type": "number",
                            "description": "Time padding around the session window for the Photos export. Defaults to 5.0",
                        },
                        "take_last": {
                            "type": "integer",
                            "description": "Export only the last N matching Photos items.",
                        },
                        "match_width": {
                            "type": "integer",
                            "description": "Optional exact width filter for Photos items.",
                        },
                        "match_height": {
                            "type": "integer",
                            "description": "Optional exact height filter for Photos items.",
                        },
                        "clear_output": {
                            "type": "boolean",
                            "description": "Delete existing images in the captures directory before export. Defaults to true.",
                        },
                        "allow_fewer": {
                            "type": "boolean",
                            "description": "Allow fewer Photos matches than the session capture count.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
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
                            "description": f"Output PDF path. Defaults to {DEFAULT_PDF_OUTPUT}",
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

    def _resolve_protocol_version(self, requested_version: object) -> str:
        if isinstance(requested_version, str) and requested_version in SUPPORTED_PROTOCOL_VERSIONS:
            return requested_version
        return DEFAULT_PROTOCOL_VERSION

    def _float_arg(self, arguments: dict[str, Any], key: str, default: float) -> float:
        value = arguments.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JsonRpcError(-32602, f"{key} must be a number")
        return float(value)

    def _optional_int_arg(self, arguments: dict[str, Any], key: str) -> int | None:
        value = arguments.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise JsonRpcError(-32602, f"{key} must be an integer")
        return value

    def _required_int_arg(self, arguments: dict[str, Any], key: str) -> int:
        value = arguments.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise JsonRpcError(-32602, f"{key} must be an integer")
        return value

    def _optional_region_args(
        self,
        arguments: dict[str, Any],
    ) -> tuple[int, int, int, int] | None:
        keys = ("x", "y", "width", "height")
        present = [key for key in keys if key in arguments]
        if not present:
            return None
        if len(present) != len(keys):
            raise JsonRpcError(-32602, "x, y, width, and height must be provided together")
        return tuple(self._required_int_arg(arguments, key) for key in keys)

    def _complete_guided_setup(
        self,
        *,
        config_path: str,
        draft: SetupDraft,
        state: GuidedSetupState,
        region: tuple[int, int, int, int],
        probe_after_save: bool,
        base_payload: dict[str, object],
    ) -> dict[str, object]:
        draft.set_change_region(*region)
        saved_path = draft.save()
        state.phase = "completed"
        payload = draft.summary()
        if "confirm_delay_ms" in base_payload:
            payload["confirm_delay_ms"] = base_payload["confirm_delay_ms"]
        payload["status"] = "completed"
        payload["updated"] = "change_region"
        payload["saved_path"] = str(saved_path)
        if probe_after_save:
            payload["probe"] = probe_region(config_path)
        self._guided_setup.clear(config_path)
        return payload

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

    def _read_message(self, stream: Any) -> tuple[dict[str, Any] | None, str]:
        line = stream.readline()
        if not line:
            return None, STDIO_MODE_NDJSON

        if line.startswith(b"Content-Length:"):
            content_length = self._parse_content_length_header(line)
            while True:
                header_line = stream.readline()
                if not header_line:
                    raise JsonRpcError(-32700, "Unexpected EOF while reading message headers")
                if header_line in (b"\r\n", b"\n"):
                    break
                header = header_line.decode("utf-8").strip()
                if not header:
                    break
                name, _, value = header.partition(":")
                if name.lower() == "content-length":
                    content_length = int(value.strip())

            payload = stream.read(content_length)
            if len(payload) != content_length:
                raise JsonRpcError(-32700, "Unexpected EOF while reading message body")
            return self._decode_message(payload), STDIO_MODE_CONTENT_LENGTH

        payload = line.strip()
        if not payload:
            return self._read_message(stream)
        return self._decode_message(payload), STDIO_MODE_NDJSON

    def _write_message(self, stream: Any, message: dict[str, Any], *, mode: str) -> None:
        payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if mode == STDIO_MODE_CONTENT_LENGTH:
            header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
            _debug_log(f"write.begin bytes={len(header) + len(payload)} id={message.get('id')} mode={mode}")
            stream.write(header)
            stream.write(payload)
        else:
            framed_payload = payload + b"\n"
            _debug_log(f"write.begin bytes={len(framed_payload)} id={message.get('id')} mode={mode}")
            stream.write(framed_payload)
        stream.flush()
        _debug_log(f"write.end id={message.get('id')}")

    def _decode_message(self, payload: bytes) -> dict[str, Any]:
        try:
            message = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise JsonRpcError(-32700, f"Invalid JSON: {exc}") from exc
        if not isinstance(message, dict):
            raise JsonRpcError(-32600, "Request body must be a JSON object")
        return message

    def _parse_content_length_header(self, line: bytes) -> int:
        try:
            header = line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise JsonRpcError(-32700, f"Invalid header encoding: {exc}") from exc
        _, _, value = header.partition(":")
        try:
            return int(value.strip())
        except ValueError as exc:
            raise JsonRpcError(-32700, "Invalid Content-Length header") from exc


def run_stdio_server(*, default_config_path: str | Path = DEFAULT_CONFIG_PATH) -> int:
    return AutokyoMCPServer(default_config_path=default_config_path).serve()
