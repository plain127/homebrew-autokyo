from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    pass


def _to_path(base_dir: Path, raw_value: str) -> Path:
    path = Path(raw_value).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _to_float(section: dict, key: str, default: float) -> float:
    raw = section.get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Expected float for {key!r}, got {raw!r}") from exc


def _to_int(section: dict, key: str, default: int) -> int:
    raw = section.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Expected int for {key!r}, got {raw!r}") from exc


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def as_screencapture_arg(self) -> str:
        return f"-R{self.x},{self.y},{self.width},{self.height}"


@dataclass(frozen=True)
class TriggerSpec:
    kind: str
    keycode: int | None = None
    point: tuple[int, int] | None = None


@dataclass(frozen=True)
class PathConfig:
    state_file: Path
    artifact_dir: Path


@dataclass(frozen=True)
class PageConfig:
    start_index: int
    change_region: Rect
    change_timeout_seconds: float
    stall_timeout_seconds: float
    post_turn_delay_ms: int
    poll_interval_seconds: float
    stability_polls: int


@dataclass(frozen=True)
class CaptureConfig:
    post_action_delay_ms: int
    post_steps: tuple["ActionStep", ...] = ()


@dataclass(frozen=True)
class ActionStep:
    trigger: TriggerSpec
    delay_ms: int = 0


@dataclass(frozen=True)
class LoopConfig:
    startup_delay_seconds: float
    cooldown_seconds: float
    max_pages: int | None
    resume: bool


@dataclass(frozen=True)
class RuntimeConfig:
    paths: PathConfig
    page: PageConfig
    capture: CaptureConfig
    loop: LoopConfig
    capture_trigger: TriggerSpec
    next_page_trigger: TriggerSpec


def _parse_rect(raw: dict | None) -> Rect:
    if not isinstance(raw, dict):
        raise ConfigError("page.change_region must be a table with x/y/width/height")
    try:
        return Rect(
            x=int(raw["x"]),
            y=int(raw["y"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
        )
    except KeyError as exc:
        raise ConfigError(f"Missing rect field: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError("Rect values must be integers") from exc


def _parse_trigger(raw: dict | None, *, field_name: str) -> TriggerSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{field_name} must be a table")
    kind = str(raw.get("type", "")).strip().lower()
    if kind not in {"keycode", "mouse_click"}:
        raise ConfigError(
            f"{field_name}.type must be one of keycode, mouse_click; got {kind!r}"
        )
    if kind == "keycode":
        keycode = raw.get("keycode")
        if keycode is None:
            raise ConfigError(f"{field_name}.keycode is required for keycode")
        try:
            return TriggerSpec(kind=kind, keycode=int(keycode))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field_name}.keycode must be an integer") from exc
    if kind == "mouse_click":
        try:
            x = int(raw["x"])
            y = int(raw["y"])
        except KeyError as exc:
            raise ConfigError(f"Missing {field_name}.{exc.args[0]} for mouse_click") from exc
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field_name}.x and .y must be integers") from exc
        return TriggerSpec(kind=kind, point=(x, y))
    raise ConfigError(f"Unsupported trigger type: {kind}")


def _parse_action_steps(raw: object, *, field_name: str) -> tuple[ActionStep, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{field_name} must be an array of tables")

    steps: list[ActionStep] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{field_name}[{index}] must be a table")
        trigger = _parse_trigger(item, field_name=f"{field_name}[{index}]")
        delay_ms = _to_int(item, "delay_ms", 0)
        steps.append(ActionStep(trigger=trigger, delay_ms=max(0, delay_ms)))
    return tuple(steps)


def load_config(path: str | Path) -> RuntimeConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    base_dir = config_path.parent

    paths_section = data.get("paths", {})
    page_section = data.get("page", {})
    capture_section = data.get("capture", {})
    loop_section = data.get("loop", {})
    triggers_section = data.get("triggers", {})

    paths = PathConfig(
        state_file=_to_path(
            base_dir,
            paths_section.get("state_file", "./runtime/session.json"),
        ),
        artifact_dir=_to_path(
            base_dir,
            paths_section.get("artifact_dir", "./artifacts"),
        ),
    )

    max_pages_raw = loop_section.get("max_pages")
    max_pages = None if max_pages_raw in (None, 0, "0") else int(max_pages_raw)

    page = PageConfig(
        start_index=_to_int(page_section, "start_index", 1),
        change_region=_parse_rect(page_section.get("change_region")),
        change_timeout_seconds=_to_float(page_section, "change_timeout_seconds", 2.5),
        stall_timeout_seconds=_to_float(page_section, "stall_timeout_seconds", 4.0),
        post_turn_delay_ms=_to_int(page_section, "post_turn_delay_ms", 250),
        poll_interval_seconds=_to_float(page_section, "poll_interval_seconds", 0.25),
        stability_polls=max(1, _to_int(page_section, "stability_polls", 2)),
    )
    capture = CaptureConfig(
        post_action_delay_ms=_to_int(capture_section, "post_action_delay_ms", 300),
        post_steps=_parse_action_steps(capture_section.get("post_steps"), field_name="capture.post_steps"),
    )
    loop = LoopConfig(
        startup_delay_seconds=_to_float(loop_section, "startup_delay_seconds", 3.0),
        cooldown_seconds=_to_float(loop_section, "cooldown_seconds", 0.2),
        max_pages=max_pages,
        resume=bool(loop_section.get("resume", True)),
    )
    capture_trigger = _parse_trigger(triggers_section.get("capture"), field_name="triggers.capture")
    next_page_trigger = _parse_trigger(
        triggers_section.get("next_page"), field_name="triggers.next_page"
    )

    return RuntimeConfig(
        paths=paths,
        page=page,
        capture=capture,
        loop=loop,
        capture_trigger=capture_trigger,
        next_page_trigger=next_page_trigger,
    )
