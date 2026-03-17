from __future__ import annotations

import json
from pathlib import Path

from autokyo.actions import get_mouse_position
from autokyo.config import load_config
from autokyo.orchestrator import CaptureOrchestrator
from autokyo.page_state import PageStateDetector
from autokyo.pdf_builder import build_pdf_from_directory
from autokyo.session_store import SessionStore


DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_CAPTURES_DIR = Path("./captures")
DEFAULT_PDF_OUTPUT = Path("./exports/captures.pdf")


def run_capture_session(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    config = load_config(config_path)
    summary = CaptureOrchestrator(config).run()
    return {
        "status": "completed",
        "captures_completed": summary.captures_completed,
        "state_file": str(summary.state_file),
        "stop_reason": summary.stop_reason,
    }


def probe_region(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    config = load_config(config_path)
    detector = PageStateDetector(
        region=config.page.change_region,
        artifact_dir=config.paths.artifact_dir,
        poll_interval_seconds=config.page.poll_interval_seconds,
        stability_polls=config.page.stability_polls,
    )
    sample = detector.capture_state(persist=True, prefix="probe")
    return {
        "digest": sample.digest,
        "byte_size": sample.byte_size,
        "captured_at": sample.captured_at,
        "sample_path": sample.sample_path,
    }


def get_session_status(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    config = load_config(config_path)
    state_file = config.paths.state_file
    if not state_file.exists():
        return {
            "status": "missing",
            "state_file": str(state_file),
        }

    store = SessionStore(state_file)
    state = store.load()
    if state is None:
        return {
            "status": "missing",
            "state_file": str(state_file),
        }
    return state.to_json()


def build_pdf(
    *,
    input_dir: str | Path = DEFAULT_CAPTURES_DIR,
    output_file: str | Path = DEFAULT_PDF_OUTPUT,
    sort_by: str = "auto",
    delete_source: bool = False,
) -> dict[str, object]:
    summary = build_pdf_from_directory(
        Path(input_dir),
        Path(output_file),
        sort_by=sort_by,
        delete_source=delete_source,
    )
    return {
        "status": "completed",
        "input_dir": str(summary.input_dir),
        "output_file": str(summary.output_file),
        "image_count": summary.image_count,
        "sort_by": summary.sort_by,
        "deleted_count": summary.deleted_count,
    }


def get_mouse_position_payload() -> dict[str, int]:
    x, y = get_mouse_position()
    return {"x": x, "y": y}


def format_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
