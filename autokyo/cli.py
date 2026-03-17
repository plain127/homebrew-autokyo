from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from autokyo.actions import AutomationError, get_mouse_position
from autokyo.config import ConfigError, load_config
from autokyo.mcp_server import run_stdio_server
from autokyo.orchestrator import CaptureOrchestrator, OrchestratorError
from autokyo.page_state import PageStateError, PageStateDetector
from autokyo.pdf_builder import PdfBuildError, build_pdf_from_directory
from autokyo.photos_export import PhotosExportError, export_photos_for_session
from autokyo.session_store import SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autokyo",
        description="Automate page-by-page capture in a document viewer on macOS.",
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to TOML config file. Defaults to ./config.toml",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("mcp", help="Run the Autokyo MCP server over stdio")
    subparsers.add_parser("run", help="Run the capture loop")
    subparsers.add_parser("probe", help="Capture and save one page-check region sample")
    subparsers.add_parser("status", help="Print session state JSON if present")
    pdf_parser = subparsers.add_parser("make-pdf", help="Combine images in a folder into one PDF")
    pdf_parser.add_argument(
        "--input",
        default="./captures",
        help="Input image directory. Defaults to ./captures",
    )
    pdf_parser.add_argument(
        "--output",
        default="./exports/captures.pdf",
        help="Output PDF path. Defaults to ./exports/captures.pdf",
    )
    pdf_parser.add_argument(
        "--sort-by",
        choices=["auto", "created", "modified", "name"],
        default="auto",
        help="Image ordering strategy. Defaults to auto",
    )
    pdf_parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Delete source images after the PDF is created successfully",
    )
    export_parser = subparsers.add_parser(
        "export-photos-to-captures",
        help="Export Photos items for the current session into a captures folder",
    )
    export_parser.add_argument(
        "--session-file",
        default="./runtime/session.json",
        help="Session JSON path. Defaults to ./runtime/session.json",
    )
    export_parser.add_argument(
        "--library-db",
        default="~/Pictures/Photos Library.photoslibrary/database/Photos.sqlite",
        help="Photos.sqlite path. Defaults to the standard Photos library database",
    )
    export_parser.add_argument(
        "--output-dir",
        default="./captures",
        help="Destination directory for exported images. Defaults to ./captures",
    )
    export_parser.add_argument(
        "--padding-seconds",
        type=float,
        default=5.0,
        help="Time padding added before session start and after session end. Defaults to 5.0",
    )
    export_parser.add_argument(
        "--take-last",
        type=int,
        default=None,
        help="Export only the last N matching Photos items. Defaults to the session capture count",
    )
    export_parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="Optional exact width filter for Photos items",
    )
    export_parser.add_argument(
        "--height",
        type=int,
        default=None,
        help="Optional exact height filter for Photos items",
    )
    export_parser.add_argument(
        "--clear-output",
        action="store_true",
        help="Delete existing image files in the output directory before export",
    )
    export_parser.add_argument(
        "--allow-fewer",
        action="store_true",
        help="Export fewer files when Photos has fewer matches than the session capture count",
    )
    export_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the selected Photos match set without exporting files",
    )
    export_parser.add_argument(
        "--make-pdf",
        action="store_true",
        help="Build a PDF from the exported captures after export completes",
    )
    export_parser.add_argument(
        "--pdf-output",
        default="./exports/captures.pdf",
        help="PDF output path when using --make-pdf. Defaults to ./exports/captures.pdf",
    )
    export_parser.add_argument(
        "--pdf-sort-by",
        choices=["auto", "created", "modified", "name"],
        default="name",
        help="Image ordering for the PDF step. Defaults to name",
    )
    export_parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Delete exported captures after a successful PDF build. Requires --make-pdf",
    )
    mousepos_parser = subparsers.add_parser("mousepos", help="Print current mouse coordinates")
    mousepos_parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously print mouse coordinates until interrupted",
    )
    mousepos_parser.add_argument(
        "--interval",
        type=float,
        default=0.25,
        help="Polling interval in seconds when using --watch",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "mcp":
            return run_stdio_server(default_config_path=args.config)

        if args.command == "mousepos":
            return _run_mousepos(watch=bool(args.watch), interval=float(args.interval))

        if args.command == "make-pdf":
            summary = build_pdf_from_directory(
                Path(args.input),
                Path(args.output),
                sort_by=args.sort_by,
                delete_source=bool(args.delete_source),
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "input_dir": str(summary.input_dir),
                        "output_file": str(summary.output_file),
                        "image_count": summary.image_count,
                        "sort_by": summary.sort_by,
                        "deleted_count": summary.deleted_count,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0

        if args.command == "export-photos-to-captures":
            if args.delete_source and not args.make_pdf:
                raise ValueError("--delete-source requires --make-pdf")

            export_summary = export_photos_for_session(
                Path(args.session_file),
                Path(args.output_dir),
                library_db=Path(args.library_db),
                time_padding_seconds=float(args.padding_seconds),
                take_last=args.take_last,
                match_width=args.width,
                match_height=args.height,
                clear_output=bool(args.clear_output),
                allow_fewer=bool(args.allow_fewer),
                dry_run=bool(args.dry_run),
            )

            payload: dict[str, object] = {
                "status": "completed",
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
                "allow_fewer": bool(args.allow_fewer),
                "dry_run": bool(args.dry_run),
            }

            if args.make_pdf and not args.dry_run:
                pdf_summary = build_pdf_from_directory(
                    Path(args.output_dir),
                    Path(args.pdf_output),
                    sort_by=args.pdf_sort_by,
                    delete_source=bool(args.delete_source),
                )
                payload["pdf"] = {
                    "output_file": str(pdf_summary.output_file),
                    "image_count": pdf_summary.image_count,
                    "sort_by": pdf_summary.sort_by,
                    "deleted_count": pdf_summary.deleted_count,
                }

            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0

        config = load_config(args.config)
        if args.command == "run":
            summary = CaptureOrchestrator(config).run()
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "captures_completed": summary.captures_completed,
                        "state_file": str(summary.state_file),
                        "stop_reason": summary.stop_reason,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0

        if args.command == "probe":
            detector = PageStateDetector(
                region=config.page.change_region,
                artifact_dir=config.paths.artifact_dir,
                poll_interval_seconds=config.page.poll_interval_seconds,
                stability_polls=config.page.stability_polls,
            )
            sample = detector.capture_state(persist=True, prefix="probe")
            print(
                json.dumps(
                    {
                        "digest": sample.digest,
                        "byte_size": sample.byte_size,
                        "captured_at": sample.captured_at,
                        "sample_path": sample.sample_path,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0

        if args.command == "status":
            if not config.paths.state_file.exists():
                print(
                    json.dumps(
                        {
                            "status": "missing",
                            "state_file": str(config.paths.state_file),
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
                return 0
            store = SessionStore(config.paths.state_file)
            state = store.load()
            if state is None:
                print(
                    json.dumps(
                        {
                            "status": "missing",
                            "state_file": str(config.paths.state_file),
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
                return 0
            print(json.dumps(state.to_json(), ensure_ascii=True, indent=2))
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2

    except (
        AutomationError,
        ConfigError,
        OrchestratorError,
        PageStateError,
        PdfBuildError,
        PhotosExportError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}")
        return 1


def _run_mousepos(*, watch: bool, interval: float) -> int:
    interval = max(0.05, interval)
    if not watch:
        x, y = get_mouse_position()
        print(json.dumps({"x": x, "y": y}, ensure_ascii=True, indent=2))
        return 0

    try:
        while True:
            x, y = get_mouse_position()
            print(json.dumps({"x": x, "y": y}, ensure_ascii=True))
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
