from __future__ import annotations

import json
from pathlib import Path

from autokyo.actions import get_mouse_position
from autokyo.config import load_config
from autokyo.orchestrator import CaptureOrchestrator
from autokyo.page_state import PageStateDetector
from autokyo.pdf_builder import build_pdf_from_directory
from autokyo.photos_export import PhotosExportError, delete_photos_assets, export_photos_for_session
from autokyo.session_store import SessionStore


DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_CAPTURES_DIR = Path("./captures")
DEFAULT_PDF_OUTPUT = Path.home() / "Desktop" / "captures.pdf"
DEFAULT_PHOTOS_LIBRARY_DB = (
    Path.home() / "Pictures" / "Photos Library.photoslibrary" / "database" / "Photos.sqlite"
)


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


def capture_to_pdf(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    input_dir: str | Path = DEFAULT_CAPTURES_DIR,
    output_file: str | Path = DEFAULT_PDF_OUTPUT,
    sort_by: str = "name",
    delete_source: bool = False,
    probe_first: bool = True,
    library_db: str | Path = DEFAULT_PHOTOS_LIBRARY_DB,
    time_padding_seconds: float = 5.0,
    take_last: int | None = None,
    match_width: int | None = None,
    match_height: int | None = None,
    clear_output: bool = True,
    allow_fewer: bool = False,
) -> dict[str, object]:
    config = load_config(config_path)

    payload: dict[str, object] = {
        "status": "completed",
        "config_path": str(Path(config_path).expanduser().resolve()),
    }
    if probe_first:
        payload["probe"] = probe_region(config_path)

    payload["capture_session"] = run_capture_session(config_path)

    export_summary = export_photos_for_session(
        config.paths.state_file,
        Path(input_dir),
        library_db=Path(library_db),
        time_padding_seconds=time_padding_seconds,
        take_last=take_last,
        match_width=match_width,
        match_height=match_height,
        clear_output=clear_output,
        allow_fewer=allow_fewer,
        dry_run=False,
    )
    payload["photos_export"] = {
        "session_file": str(export_summary.session_file),
        "library_db": str(export_summary.library_db),
        "output_dir": str(export_summary.output_dir),
        "window_started_at": export_summary.window_started_at,
        "window_ended_at": export_summary.window_ended_at,
        "expected_count": export_summary.expected_count,
        "candidate_count": export_summary.candidate_count,
        "selected_count": export_summary.selected_count,
        "exported_count": export_summary.exported_count,
        "cleared_count": export_summary.cleared_count,
        "missing_count": export_summary.missing_count,
        "first_selected_filename": export_summary.first_selected_filename,
        "last_selected_filename": export_summary.last_selected_filename,
    }
    payload["pdf"] = build_pdf(
        input_dir=input_dir,
        output_file=output_file,
        sort_by=sort_by,
        delete_source=delete_source,
    )
    if delete_source:
        try:
            payload["photos_deleted_count"] = delete_photos_assets(export_summary.selected_assets)
        except PhotosExportError as exc:
            raise PhotosExportError(
                "PDF was created and exported captures were deleted, but matching Photos items "
                f"could not be deleted.\n{exc}"
            ) from exc
    return payload


def get_mouse_position_payload() -> dict[str, int]:
    x, y = get_mouse_position()
    return {"x": x, "y": y}


def format_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
