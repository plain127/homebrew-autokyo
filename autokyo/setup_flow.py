from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
from pathlib import Path
from typing import Any
import tomllib

from autokyo.config import (
    DEFAULT_CONFIG_TEXT,
    ActionStep,
    ConfigError,
    Rect,
    TriggerSpec,
    _parse_action_steps,
    _parse_rect,
    _parse_trigger,
    _to_float,
    _to_int,
)


DEFAULT_CONFIRM_BUTTON_DELAY_MS = 2000


def _resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _load_default_data() -> dict[str, Any]:
    return tomllib.loads(DEFAULT_CONFIG_TEXT)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _to_bool(value: object, *, field_name: str, default: bool) -> bool:
    raw = default if value is None else value
    if isinstance(raw, bool):
        return raw
    raise ConfigError(f"Expected boolean for {field_name!r}, got {raw!r}")


def _format_toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise TypeError(f"Unsupported TOML value type: {type(value)!r}")


def _trigger_lines(trigger: TriggerSpec) -> list[str]:
    lines = [f'type = "{trigger.kind}"']
    if trigger.kind == "keycode":
        if trigger.keycode is None:
            raise ConfigError("Missing keycode for keycode trigger")
        lines.append(f"keycode = {trigger.keycode}")
        return lines
    if trigger.kind == "mouse_click":
        if trigger.point is None:
            raise ConfigError("Missing point for mouse_click trigger")
        lines.append(f"x = {trigger.point[0]}")
        lines.append(f"y = {trigger.point[1]}")
        return lines
    raise ConfigError(f"Unsupported trigger type: {trigger.kind}")


def _trigger_payload(trigger: TriggerSpec) -> dict[str, object]:
    payload: dict[str, object] = {"type": trigger.kind}
    if trigger.kind == "keycode":
        payload["keycode"] = trigger.keycode
        return payload
    if trigger.kind == "mouse_click":
        if trigger.point is None:
            raise ConfigError("Missing point for mouse_click trigger")
        payload["x"] = trigger.point[0]
        payload["y"] = trigger.point[1]
        return payload
    raise ConfigError(f"Unsupported trigger type: {trigger.kind}")


@dataclass
class SetupDraft:
    config_path: Path
    state_file: str
    artifact_dir: str
    start_index: int
    change_timeout_seconds: float
    stall_timeout_seconds: float
    post_turn_delay_ms: int
    poll_interval_seconds: float
    stability_polls: int
    change_region: Rect
    post_action_delay_ms: int
    post_steps: list[ActionStep] = field(default_factory=list)
    startup_delay_seconds: float = 3.0
    cooldown_seconds: float = 0.2
    max_pages: int = 0
    resume: bool = True
    capture_trigger: TriggerSpec = field(
        default_factory=lambda: TriggerSpec(kind="mouse_click", point=(0, 0))
    )
    next_page_trigger: TriggerSpec = field(
        default_factory=lambda: TriggerSpec(kind="keycode", keycode=124)
    )
    dirty: bool = False

    @classmethod
    def load(cls, config_path: str | Path) -> SetupDraft:
        resolved_path = _resolve_path(config_path)
        data = _load_default_data()

        if resolved_path.exists():
            try:
                loaded = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"Invalid TOML in {resolved_path}: {exc}") from exc
            except OSError as exc:
                raise ConfigError(f"Unable to read config file {resolved_path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"Config file must decode to a table: {resolved_path}")
            data = _deep_merge(data, loaded)

        paths_section = data.get("paths", {})
        page_section = data.get("page", {})
        capture_section = data.get("capture", {})
        loop_section = data.get("loop", {})
        triggers_section = data.get("triggers", {})

        return cls(
            config_path=resolved_path,
            state_file=str(paths_section.get("state_file", "./runtime/session.json")),
            artifact_dir=str(paths_section.get("artifact_dir", "./artifacts")),
            start_index=_to_int(page_section, "start_index", 1),
            change_timeout_seconds=_to_float(page_section, "change_timeout_seconds", 2.5),
            stall_timeout_seconds=_to_float(page_section, "stall_timeout_seconds", 4.0),
            post_turn_delay_ms=_to_int(page_section, "post_turn_delay_ms", 250),
            poll_interval_seconds=_to_float(page_section, "poll_interval_seconds", 0.25),
            stability_polls=max(1, _to_int(page_section, "stability_polls", 2)),
            change_region=_parse_rect(page_section.get("change_region")),
            post_action_delay_ms=_to_int(capture_section, "post_action_delay_ms", 300),
            post_steps=list(
                _parse_action_steps(
                    capture_section.get("post_steps"),
                    field_name="capture.post_steps",
                )
            ),
            startup_delay_seconds=_to_float(loop_section, "startup_delay_seconds", 3.0),
            cooldown_seconds=_to_float(loop_section, "cooldown_seconds", 0.2),
            max_pages=_to_int(loop_section, "max_pages", 0),
            resume=_to_bool(loop_section.get("resume"), field_name="loop.resume", default=True),
            capture_trigger=_parse_trigger(
                triggers_section.get("capture"),
                field_name="triggers.capture",
            ),
            next_page_trigger=_parse_trigger(
                triggers_section.get("next_page"),
                field_name="triggers.next_page",
            ),
            dirty=False,
        )

    def set_capture_button(self, x: int, y: int) -> None:
        self.capture_trigger = TriggerSpec(kind="mouse_click", point=(int(x), int(y)))
        self.dirty = True

    def set_confirm_button(self, x: int, y: int, *, delay_ms: int | None = None) -> int:
        resolved_delay_ms = self._resolve_confirm_delay(delay_ms)
        step = ActionStep(
            trigger=TriggerSpec(kind="mouse_click", point=(int(x), int(y))),
            delay_ms=resolved_delay_ms,
        )
        if self.post_steps:
            self.post_steps[0] = step
        else:
            self.post_steps.append(step)
        self.dirty = True
        return resolved_delay_ms

    def set_change_region(self, x: int, y: int, width: int, height: int) -> None:
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            raise ConfigError("page.change_region width and height must be positive integers")
        self.change_region = Rect(x=int(x), y=int(y), width=width, height=height)
        self.dirty = True

    def save(self) -> Path:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(self.to_toml(), encoding="utf-8")
        self.dirty = False
        return self.config_path

    def summary(self) -> dict[str, object]:
        confirm_button: dict[str, object] | None = None
        if self.post_steps:
            first_step = self.post_steps[0]
            confirm_button = _trigger_payload(first_step.trigger)
            confirm_button["delay_ms"] = first_step.delay_ms

        return {
            "config_path": str(self.config_path),
            "capture_button": _trigger_payload(self.capture_trigger),
            "confirm_button": confirm_button,
            "change_region": {
                "x": self.change_region.x,
                "y": self.change_region.y,
                "width": self.change_region.width,
                "height": self.change_region.height,
            },
            "post_steps_count": len(self.post_steps),
            "pending_changes": self.dirty,
        }

    def to_toml(self) -> str:
        lines = [
            "# AutoKyo config",
            "# Generated by autokyo setup or the AutoKyo MCP setup tools.",
            "",
            "[paths]",
            f"state_file = {_format_toml_value(self.state_file)}",
            f"artifact_dir = {_format_toml_value(self.artifact_dir)}",
            "",
            "[page]",
            f"start_index = {self.start_index}",
            f"change_timeout_seconds = {_format_toml_value(self.change_timeout_seconds)}",
            f"stall_timeout_seconds = {_format_toml_value(self.stall_timeout_seconds)}",
            f"post_turn_delay_ms = {self.post_turn_delay_ms}",
            f"poll_interval_seconds = {_format_toml_value(self.poll_interval_seconds)}",
            f"stability_polls = {self.stability_polls}",
            (
                "change_region = "
                "{ "
                f"x = {self.change_region.x}, "
                f"y = {self.change_region.y}, "
                f"width = {self.change_region.width}, "
                f"height = {self.change_region.height} "
                "}"
            ),
            "",
            "[capture]",
            f"post_action_delay_ms = {self.post_action_delay_ms}",
            "",
        ]

        for step in self.post_steps:
            lines.append("[[capture.post_steps]]")
            lines.extend(_trigger_lines(step.trigger))
            lines.append(f"delay_ms = {max(0, int(step.delay_ms))}")
            lines.append("")

        lines.extend(
            [
                "[loop]",
                f"startup_delay_seconds = {_format_toml_value(self.startup_delay_seconds)}",
                f"cooldown_seconds = {_format_toml_value(self.cooldown_seconds)}",
                f"max_pages = {self.max_pages}",
                f"resume = {_format_toml_value(self.resume)}",
                "",
                "[triggers.capture]",
                *_trigger_lines(self.capture_trigger),
                "",
                "[triggers.next_page]",
                *_trigger_lines(self.next_page_trigger),
                "",
            ]
        )

        return "\n".join(lines)

    def _resolve_confirm_delay(self, explicit_delay_ms: int | None) -> int:
        if explicit_delay_ms is not None:
            return max(0, int(explicit_delay_ms))
        if self.post_steps:
            return max(0, int(self.post_steps[0].delay_ms))
        return DEFAULT_CONFIRM_BUTTON_DELAY_MS


class SetupDraftStore:
    def __init__(self) -> None:
        self._drafts: dict[str, SetupDraft] = {}

    def get(self, config_path: str | Path) -> SetupDraft:
        resolved_path = _resolve_path(config_path)
        key = str(resolved_path)
        draft = self._drafts.get(key)
        if draft is None:
            draft = SetupDraft.load(resolved_path)
            self._drafts[key] = draft
        return draft

    def reset(self, config_path: str | Path) -> SetupDraft:
        resolved_path = _resolve_path(config_path)
        key = str(resolved_path)
        draft = SetupDraft.load(resolved_path)
        self._drafts[key] = draft
        return draft
