from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from autokyo.actions import AutomationError, get_mouse_position
from autokyo.config import ConfigError, load_config, resolve_config_path, write_default_config
from autokyo.mcp_http_server import run_streamable_http_server
from autokyo.mcp_launchd import (
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTP_WAIT_SECONDS,
    build_launch_agent_spec,
    install_or_update_launch_agent,
    wait_for_http_health,
)
from autokyo.mcp_server import run_stdio_server
from autokyo.orchestrator import CaptureOrchestrator, OrchestratorError
from autokyo.page_state import PageStateError, PageStateDetector
from autokyo.pdf_builder import PdfBuildError, build_pdf_from_directory
from autokyo.photos_export import (
    PhotosExportError,
    delete_photos_assets,
    export_photos_for_session,
)
from autokyo.service import DEFAULT_CAPTURES_DIR, DEFAULT_PDF_OUTPUT, DEFAULT_PHOTOS_LIBRARY_DB
from autokyo.session_store import SessionStore
from autokyo.setup_flow import SetupDraft


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autokyo",
        description="Automate page-by-page capture in a document viewer on macOS.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to TOML config file. If omitted, AutoKyo searches ./config.toml, "
            "~/Library/Application Support/AutoKyo/config.toml, and ~/.config/autokyo/config.toml"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("mcp", help="Run the Autokyo MCP server over stdio")
    mcp_http_parser = subparsers.add_parser(
        "mcp-http",
        help="Run the Autokyo MCP server over streamable HTTP",
    )
    mcp_http_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind. Defaults to 127.0.0.1",
    )
    mcp_http_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="TCP port to bind. Defaults to 8765",
    )
    subparsers.add_parser(
        "init-config",
        help="Create the default config.toml in the standard AutoKyo config location",
    )
    subparsers.add_parser(
        "setup",
        help="Interactively capture button coordinates and the page-change region, then save config.toml",
    )
    subparsers.add_parser("run", help="Run the capture loop")
    subparsers.add_parser("probe", help="Capture and save one page-check region sample")
    subparsers.add_parser("status", help="Print session state JSON if present")
    pdf_parser = subparsers.add_parser(
        "make-pdf",
        aliases=["pdf"],
        help="Create a PDF, exporting the current session from Photos when needed",
    )
    pdf_parser.add_argument(
        "--input",
        default=None,
        help="Optional input image directory. If omitted, AutoKyo exports the current session from Photos into ./captures first",
    )
    pdf_parser.add_argument(
        "--output",
        default=None,
        help="Output PDF path. Defaults to asking for a title and saving on the Desktop",
    )
    pdf_parser.add_argument(
        "--title",
        default=None,
        help="PDF title to use for the Desktop filename when --output is omitted",
    )
    pdf_parser.add_argument(
        "--sort-by",
        choices=["auto", "created", "modified", "name"],
        default="name",
        help="Image ordering strategy. Defaults to name",
    )
    pdf_parser.add_argument(
        "--delete-source",
        action="store_true",
        help=(
            "Delete source captures after the PDF is created successfully. "
            "When AutoKyo exports from Photos, also delete the matching Photos items"
        ),
    )
    pdf_parser.add_argument(
        "--library-db",
        default=str(DEFAULT_PHOTOS_LIBRARY_DB),
        help="Photos.sqlite path when exporting from the current session",
    )
    pdf_parser.add_argument(
        "--padding-seconds",
        type=float,
        default=5.0,
        help="Time padding added before session start and after session end when exporting from Photos",
    )
    pdf_parser.add_argument(
        "--take-last",
        type=int,
        default=None,
        help="Export only the last N matching Photos items when exporting from the current session",
    )
    pdf_parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="Optional exact width filter for Photos items when exporting from the current session",
    )
    pdf_parser.add_argument(
        "--height",
        type=int,
        default=None,
        help="Optional exact height filter for Photos items when exporting from the current session",
    )
    pdf_parser.add_argument(
        "--strict-count",
        action="store_true",
        help="Fail if Photos contains fewer images than the session capture count when exporting from the current session",
    )
    export_parser = subparsers.add_parser(
        "export-photos-to-captures",
        help="Export Photos items for the current session into a captures folder",
    )
    export_parser.add_argument(
        "--session-file",
        default=None,
        help="Session JSON path. Defaults to the state_file from the active config.toml",
    )
    export_parser.add_argument(
        "--library-db",
        default=str(DEFAULT_PHOTOS_LIBRARY_DB),
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
        default=str(DEFAULT_PDF_OUTPUT),
        help=f"PDF output path when using --make-pdf. Defaults to {DEFAULT_PDF_OUTPUT}",
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
        help=(
            "Delete exported captures after a successful PDF build, then delete the matching "
            "Photos items. Requires --make-pdf"
        ),
    )
    mcp_install_parser = subparsers.add_parser(
        "mcp-install",
        aliases=["mcp-register"],
        help="Register AutoKyo as a local MCP server in a supported client",
    )
    mcp_install_parser.add_argument(
        "client",
        nargs="?",
        choices=["codex", "claude", "claude-code", "antigravity", "openclaw"],
        default="codex",
        help="MCP client to register with. Defaults to codex",
    )
    mcp_install_parser.add_argument(
        "--name",
        default="autokyo",
        help="Registered MCP server name. Defaults to autokyo",
    )
    mcp_install_parser.add_argument(
        "--python",
        dest="python_executable",
        default=None,
        help="Optional Python executable to use for the MCP server command",
    )
    mcp_install_parser.add_argument(
        "--client-config",
        default=None,
        help="Optional client config path for file-based installers like Antigravity",
    )
    mcp_install_parser.add_argument(
        "--scope",
        choices=["local", "project", "user"],
        default="user",
        help="Config scope for clients that support it. Defaults to user",
    )
    mcp_install_parser.add_argument(
        "--transport",
        choices=["auto", "stdio", "http"],
        default="auto",
        help="Transport to register. Defaults to http for codex/claude/antigravity and stdio for other clients",
    )
    mcp_install_parser.add_argument(
        "--http-host",
        default=DEFAULT_HTTP_HOST,
        help=f"HTTP bind host for launchd-backed MCP. Defaults to {DEFAULT_HTTP_HOST}",
    )
    mcp_install_parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"HTTP bind port for launchd-backed MCP. Defaults to {DEFAULT_HTTP_PORT}",
    )
    mcp_install_parser.add_argument(
        "--http-wait-seconds",
        type=float,
        default=DEFAULT_HTTP_WAIT_SECONDS,
        help=f"How long to wait for the HTTP server health check. Defaults to {DEFAULT_HTTP_WAIT_SECONDS}",
    )
    mcp_install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the registration command without running it",
    )
    mousepos_parser = subparsers.add_parser(
        "mousepos",
        aliases=["coords"],
        help="Print current mouse coordinates",
    )
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
            return run_stdio_server(
                default_config_path=resolve_config_path(args.config, must_exist=False)
            )

        if args.command == "mcp-http":
            return run_streamable_http_server(
                default_config_path=resolve_config_path(args.config, must_exist=False),
                host=str(args.host),
                port=int(args.port),
            )

        if args.command == "init-config":
            config_path = resolve_config_path(args.config, must_exist=False)
            existed_before = config_path.exists()
            write_default_config(config_path)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "config_path": str(config_path),
                        "created": not existed_before,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0

        if args.command == "setup":
            return _run_setup(resolve_config_path(args.config, must_exist=False))

        if args.command in {"mousepos", "coords"}:
            return _run_mousepos(watch=bool(args.watch), interval=float(args.interval))

        if args.command in {"make-pdf", "pdf"}:
            return _run_pdf_command(args)

        if args.command in {"mcp-install", "mcp-register"}:
            return _install_mcp_server(
                client=args.client,
                server_name=args.name,
                config_path_arg=args.config,
                python_executable=args.python_executable,
                client_config_path=args.client_config,
                scope=args.scope,
                transport=args.transport,
                http_host=args.http_host,
                http_port=int(args.http_port),
                http_wait_seconds=float(args.http_wait_seconds),
                dry_run=bool(args.dry_run),
            )

        if args.command == "export-photos-to-captures":
            if args.delete_source and not args.make_pdf:
                raise ValueError("--delete-source requires --make-pdf")

            if args.session_file:
                session_file = Path(args.session_file)
            else:
                export_config = load_config(resolve_config_path(args.config))
                session_file = export_config.paths.state_file

            export_summary = export_photos_for_session(
                session_file,
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
                if args.delete_source:
                    try:
                        photos_deleted_count = delete_photos_assets(export_summary.selected_assets)
                    except PhotosExportError as exc:
                        raise PhotosExportError(
                            "PDF was created and exported captures were deleted, but matching "
                            f"Photos items could not be deleted.\n{exc}"
                        ) from exc
                    payload["photos_deleted_count"] = photos_deleted_count

            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0

        config = load_config(resolve_config_path(args.config))
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
        EOFError,
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


def _run_setup(config_path: Path) -> int:
    draft = SetupDraft.load(config_path)
    print(f"설정 파일: {draft.config_path}")

    try:
        input("캡처 버튼 위에 마우스를 올리고 Enter를 누르세요...")
        capture_x, capture_y = get_mouse_position()
        draft.set_capture_button(capture_x, capture_y)
        print(json.dumps({"capture_button": {"x": capture_x, "y": capture_y}}, ensure_ascii=True))

        input("확인 버튼 위에 마우스를 올리고 Enter를 누르세요...")
        confirm_x, confirm_y = get_mouse_position()
        confirm_delay_ms = draft.set_confirm_button(confirm_x, confirm_y)
        print(
            json.dumps(
                {
                    "confirm_button": {
                        "x": confirm_x,
                        "y": confirm_y,
                        "delay_ms": confirm_delay_ms,
                    }
                },
                ensure_ascii=True,
            )
        )

        region = _prompt_change_region(draft)
    except KeyboardInterrupt:
        print()
        return 130

    draft.set_change_region(*region)
    draft.save()

    payload = draft.summary()
    payload["status"] = "completed"
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _prompt_change_region(draft: SetupDraft) -> tuple[int, int, int, int]:
    current = draft.change_region
    print(
        "페이지 변화 영역을 마우스로 선택합니다. "
        f"현재값: {current.x} {current.y} {current.width} {current.height}"
    )
    input("변화 영역의 왼쪽 위에 마우스를 올리고 Enter를 누르세요...")
    start_x, start_y = get_mouse_position()
    print(json.dumps({"change_region_start": {"x": start_x, "y": start_y}}, ensure_ascii=True))

    input("변화 영역의 오른쪽 아래에 마우스를 올리고 Enter를 누르세요...")
    end_x, end_y = get_mouse_position()
    print(json.dumps({"change_region_end": {"x": end_x, "y": end_y}}, ensure_ascii=True))

    x = min(start_x, end_x)
    y = min(start_y, end_y)
    width = abs(end_x - start_x) + 1
    height = abs(end_y - start_y) + 1
    return x, y, width, height


def _run_pdf_command(args: argparse.Namespace) -> int:
    output_file = _resolve_pdf_output_path(args.output, title=args.title)

    if args.input:
        pdf_summary = build_pdf_from_directory(
            Path(args.input),
            output_file,
            sort_by=args.sort_by,
            delete_source=bool(args.delete_source),
        )
        payload = {
            "status": "completed",
            "mode": "build_from_directory",
            "input_dir": str(pdf_summary.input_dir),
            "output_file": str(pdf_summary.output_file),
            "image_count": pdf_summary.image_count,
            "sort_by": pdf_summary.sort_by,
            "deleted_count": pdf_summary.deleted_count,
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    config = load_config(resolve_config_path(args.config))
    export_summary = export_photos_for_session(
        config.paths.state_file,
        DEFAULT_CAPTURES_DIR,
        library_db=Path(args.library_db),
        time_padding_seconds=float(args.padding_seconds),
        take_last=args.take_last,
        match_width=args.width,
        match_height=args.height,
        clear_output=True,
        allow_fewer=not bool(args.strict_count),
        dry_run=False,
    )
    pdf_summary = build_pdf_from_directory(
        DEFAULT_CAPTURES_DIR,
        output_file,
        sort_by=args.sort_by,
        delete_source=bool(args.delete_source),
    )

    photos_deleted_count = 0
    if args.delete_source:
        try:
            photos_deleted_count = delete_photos_assets(export_summary.selected_assets)
        except PhotosExportError as exc:
            raise PhotosExportError(
                "PDF was created and exported captures were deleted, but matching Photos items "
                f"could not be deleted.\n{exc}"
            ) from exc

    payload: dict[str, object] = {
        "status": "completed",
        "mode": "export_session_and_build",
        "session_file": str(export_summary.session_file),
        "input_dir": str(pdf_summary.input_dir),
        "output_file": str(pdf_summary.output_file),
        "image_count": pdf_summary.image_count,
        "sort_by": pdf_summary.sort_by,
        "deleted_count": pdf_summary.deleted_count,
        "expected_count": export_summary.expected_count,
        "candidate_count": export_summary.candidate_count,
        "selected_count": export_summary.selected_count,
        "missing_count": export_summary.missing_count,
    }
    if args.delete_source:
        payload["photos_deleted_count"] = photos_deleted_count

    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _resolve_pdf_output_path(output: str | None, *, title: str | None) -> Path:
    if output:
        return Path(output)

    resolved_title = title.strip() if title else ""
    if not resolved_title:
        raw_title = input("PDF 제목을 입력하세요 [captures]: ").strip()
        resolved_title = raw_title or "captures"

    safe_title = _sanitize_pdf_title(resolved_title)
    return (Path.home() / "Desktop" / f"{safe_title}.pdf").resolve()


def _sanitize_pdf_title(value: str) -> str:
    stripped = value.strip()
    if stripped.lower().endswith(".pdf"):
        stripped = stripped[:-4]
    sanitized = re.sub(r'[/:]+', "_", stripped).strip().rstrip(".")
    return sanitized or "captures"


def _install_mcp_server(
    *,
    client: str,
    server_name: str,
    config_path_arg: str | None,
    python_executable: str | None,
    client_config_path: str | None,
    scope: str,
    transport: str,
    http_host: str,
    http_port: int,
    http_wait_seconds: float,
    dry_run: bool,
) -> int:
    normalized_client = "claude" if client == "claude-code" else client
    resolved_config = resolve_config_path(config_path_arg, must_exist=False)
    config_created = False
    if not resolved_config.exists() and not dry_run:
        write_default_config(resolved_config)
        config_created = True
    resolved_transport = _resolve_mcp_transport(normalized_client, transport)
    invocation = _build_local_mcp_invocation(
        config_path=resolved_config,
        python_executable=python_executable,
    )
    http_invocation = _build_local_http_mcp_invocation(
        config_path=resolved_config,
        python_executable=python_executable,
        host=http_host,
        port=http_port,
    )

    if normalized_client == "codex" and resolved_transport == "http":
        return _install_codex_http_mcp_server(
            server_name=server_name,
            config_path=resolved_config,
            config_created=config_created,
            scope=scope,
            http_invocation=http_invocation,
            host=http_host,
            port=http_port,
            wait_seconds=http_wait_seconds,
            dry_run=dry_run,
        )

    if normalized_client == "claude" and resolved_transport == "http":
        return _install_claude_http_mcp_server(
            server_name=server_name,
            config_path=resolved_config,
            config_created=config_created,
            scope=scope,
            http_invocation=http_invocation,
            host=http_host,
            port=http_port,
            wait_seconds=http_wait_seconds,
            dry_run=dry_run,
        )

    if normalized_client == "antigravity" and resolved_transport == "http":
        return _install_antigravity_http_server(
            server_name=server_name,
            config_path=resolved_config,
            config_created=config_created,
            client_config_path=client_config_path,
            http_invocation=http_invocation,
            host=http_host,
            port=http_port,
            wait_seconds=http_wait_seconds,
            dry_run=dry_run,
        )

    if resolved_transport == "http":
        raise ValueError("HTTP transport registration is currently supported only for codex, claude, and antigravity")

    if normalized_client == "codex":
        client_executable = shutil.which("codex")
        if client_executable is None:
            raise ValueError("Could not find 'codex' on PATH")
        remove_command = [client_executable, "mcp", "remove", server_name]
        install_command = [client_executable, "mcp", "add", server_name, "--", *invocation]
    elif normalized_client == "claude":
        client_executable = shutil.which("claude")
        if client_executable is None:
            raise ValueError("Could not find 'claude' on PATH")
        remove_command = [client_executable, "mcp", "remove", server_name]
        install_command = [
            client_executable,
            "mcp",
            "add",
            server_name,
            "--scope",
            scope,
            "--",
            *invocation,
        ]
    else:
        remove_command = []
        install_command = []

    if normalized_client in {"codex", "claude"}:
        payload = {
            "status": "ready" if dry_run else "completed",
            "client": normalized_client,
            "name": server_name,
            "scope": scope,
            "transport": resolved_transport,
            "config_path": str(resolved_config),
            "config_created": config_created,
            "server_command": invocation,
            "remove_command": remove_command,
            "install_command": install_command,
        }
        if dry_run:
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0
        subprocess.run(remove_command, check=False, capture_output=True, text=True)
        subprocess.run(install_command, check=True)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0
    if normalized_client == "antigravity":
        return _install_antigravity_server(
            server_name=server_name,
            invocation=invocation,
            client_config_path=client_config_path,
            config_path=resolved_config,
            dry_run=dry_run,
        )
    if normalized_client == "openclaw":
        return _install_openclaw_server(
            server_name=server_name,
            invocation=invocation,
            config_path=resolved_config,
            dry_run=dry_run,
        )
    else:
        raise ValueError(f"Unsupported MCP client: {client}")


def _install_codex_http_mcp_server(
    *,
    server_name: str,
    config_path: Path,
    config_created: bool,
    scope: str,
    http_invocation: list[str],
    host: str,
    port: int,
    wait_seconds: float,
    dry_run: bool,
) -> int:
    client_executable = shutil.which("codex")
    if client_executable is None:
        raise ValueError("Could not find 'codex' on PATH")

    launch_agent = build_launch_agent_spec(
        server_name=server_name,
        command=http_invocation,
        host=host,
        port=port,
        working_directory=config_path.parent,
    )
    remove_command = [client_executable, "mcp", "remove", server_name]
    install_command = [
        client_executable,
        "mcp",
        "add",
        server_name,
        "--url",
        launch_agent.endpoint_url,
    ]
    payload = {
        "status": "ready" if dry_run else "completed",
        "client": "codex",
        "name": server_name,
        "scope": scope,
        "transport": "http",
        "config_path": str(config_path),
        "config_created": config_created,
        "server_command": http_invocation,
        "remove_command": remove_command,
        "install_command": install_command,
        "endpoint_url": launch_agent.endpoint_url,
        "healthcheck_url": launch_agent.healthcheck_url,
        "launch_agent": {
            "label": launch_agent.label,
            "plist_path": str(launch_agent.plist_path),
            "stdout_path": str(launch_agent.stdout_path),
            "stderr_path": str(launch_agent.stderr_path),
            "working_directory": str(launch_agent.working_directory),
            "command": list(launch_agent.command),
        },
    }
    if dry_run:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    install_or_update_launch_agent(launch_agent)
    try:
        wait_for_http_health(launch_agent.healthcheck_url, timeout_seconds=wait_seconds)
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc} Check {launch_agent.stderr_path} and {launch_agent.stdout_path}."
        ) from exc
    subprocess.run(remove_command, check=False, capture_output=True, text=True)
    subprocess.run(install_command, check=True)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _install_claude_http_mcp_server(
    *,
    server_name: str,
    config_path: Path,
    config_created: bool,
    scope: str,
    http_invocation: list[str],
    host: str,
    port: int,
    wait_seconds: float,
    dry_run: bool,
) -> int:
    client_executable = shutil.which("claude")
    if client_executable is None:
        raise ValueError("Could not find 'claude' on PATH")

    launch_agent = build_launch_agent_spec(
        server_name=server_name,
        command=http_invocation,
        host=host,
        port=port,
        working_directory=config_path.parent,
    )
    remove_command = [client_executable, "mcp", "remove", server_name]
    install_command = [
        client_executable,
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        scope,
        server_name,
        launch_agent.endpoint_url,
    ]
    payload = {
        "status": "ready" if dry_run else "completed",
        "client": "claude",
        "name": server_name,
        "scope": scope,
        "transport": "http",
        "config_path": str(config_path),
        "config_created": config_created,
        "server_command": http_invocation,
        "remove_command": remove_command,
        "install_command": install_command,
        "endpoint_url": launch_agent.endpoint_url,
        "healthcheck_url": launch_agent.healthcheck_url,
        "launch_agent": {
            "label": launch_agent.label,
            "plist_path": str(launch_agent.plist_path),
            "stdout_path": str(launch_agent.stdout_path),
            "stderr_path": str(launch_agent.stderr_path),
            "working_directory": str(launch_agent.working_directory),
            "command": list(launch_agent.command),
        },
    }
    if dry_run:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    install_or_update_launch_agent(launch_agent)
    try:
        wait_for_http_health(launch_agent.healthcheck_url, timeout_seconds=wait_seconds)
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc} Check {launch_agent.stderr_path} and {launch_agent.stdout_path}."
        ) from exc
    subprocess.run(remove_command, check=False, capture_output=True, text=True)
    subprocess.run(install_command, check=True)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _install_antigravity_http_server(
    *,
    server_name: str,
    config_path: Path,
    config_created: bool,
    client_config_path: str | None,
    http_invocation: list[str],
    host: str,
    port: int,
    wait_seconds: float,
    dry_run: bool,
) -> int:
    antigravity_config = _resolve_antigravity_config_path(client_config_path)
    launch_agent = build_launch_agent_spec(
        server_name=server_name,
        command=http_invocation,
        host=host,
        port=port,
        working_directory=config_path.parent,
    )
    config_data = _load_json_object(antigravity_config)
    mcp_servers = config_data.setdefault("mcpServers", {})
    mcp_servers[server_name] = {
        "type": "http",
        "url": launch_agent.endpoint_url,
    }
    payload = {
        "status": "ready" if dry_run else "completed",
        "client": "antigravity",
        "name": server_name,
        "transport": "http",
        "config_path": str(config_path),
        "config_created": config_created,
        "client_config_path": str(antigravity_config),
        "server_command": http_invocation,
        "entry": {
            "type": "http",
            "url": launch_agent.endpoint_url,
        },
        "endpoint_url": launch_agent.endpoint_url,
        "healthcheck_url": launch_agent.healthcheck_url,
        "launch_agent": {
            "label": launch_agent.label,
            "plist_path": str(launch_agent.plist_path),
            "stdout_path": str(launch_agent.stdout_path),
            "stderr_path": str(launch_agent.stderr_path),
            "working_directory": str(launch_agent.working_directory),
            "command": list(launch_agent.command),
        },
    }
    if dry_run:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    install_or_update_launch_agent(launch_agent)
    try:
        wait_for_http_health(launch_agent.healthcheck_url, timeout_seconds=wait_seconds)
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc} Check {launch_agent.stderr_path} and {launch_agent.stdout_path}."
        ) from exc
    _write_json_object(antigravity_config, config_data)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _install_antigravity_server(
    *,
    server_name: str,
    invocation: list[str],
    client_config_path: str | None,
    config_path: Path,
    dry_run: bool,
) -> int:
    antigravity_config = _resolve_antigravity_config_path(client_config_path)
    config_data = _load_json_object(antigravity_config)
    server_entry = {
        "command": invocation[0],
        "args": invocation[1:],
    }
    mcp_servers = config_data.setdefault("mcpServers", {})
    mcp_servers[server_name] = server_entry

    payload = {
        "status": "ready" if dry_run else "completed",
        "client": "antigravity",
        "name": server_name,
        "config_path": str(config_path),
        "config_created": False,
        "client_config_path": str(antigravity_config),
        "server_command": invocation,
        "entry": server_entry,
    }
    if dry_run:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    _write_json_object(antigravity_config, config_data)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _install_openclaw_server(
    *,
    server_name: str,
    invocation: list[str],
    config_path: Path,
    dry_run: bool,
) -> int:
    server_entry = {
        "command": invocation[0],
        "args": invocation[1:],
        "env": {},
    }
    openclaw_executable = shutil.which("openclaw")
    if openclaw_executable is not None:
        install_command = [
            openclaw_executable,
            "config",
            "set",
            f"mcp.servers.{server_name}",
            json.dumps(server_entry, ensure_ascii=True),
            "--strict-json",
        ]
        validate_command = [openclaw_executable, "config", "validate"]
        payload = {
            "status": "ready" if dry_run else "completed",
            "client": "openclaw",
            "name": server_name,
            "config_path": str(config_path),
            "config_created": False,
            "client_config_path": str(Path.home() / ".openclaw" / "openclaw.json"),
            "server_command": invocation,
            "entry": server_entry,
            "install_command": install_command,
            "validate_command": validate_command,
        }
        if dry_run:
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0
        subprocess.run(install_command, check=True)
        subprocess.run(validate_command, check=True)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    openclaw_config_path = Path.home() / ".openclaw" / "openclaw.json"
    config_data = _load_json_object(openclaw_config_path)
    mcp = config_data.setdefault("mcp", {})
    servers = mcp.setdefault("servers", {})
    servers[server_name] = server_entry
    payload = {
        "status": "ready" if dry_run else "completed",
        "client": "openclaw",
        "name": server_name,
        "config_path": str(config_path),
        "config_created": False,
        "client_config_path": str(openclaw_config_path),
        "server_command": invocation,
        "entry": server_entry,
        "install_method": "file-write-fallback",
    }
    if dry_run:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0
    _write_json_object(openclaw_config_path, config_data)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _resolve_antigravity_config_path(client_config_path: str | None) -> Path:
    if client_config_path:
        return Path(client_config_path).expanduser().resolve()

    candidates = _discover_antigravity_config_candidates()
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError(
            "Found multiple Antigravity mcp_config.json files. Re-run with --client-config <path>."
        )
    raise ValueError(
        "Could not find Antigravity mcp_config.json. Open Antigravity and use "
        "'Manage MCP Servers > View raw config' once, or re-run with --client-config <path>."
    )


def _discover_antigravity_config_candidates() -> list[Path]:
    direct_candidates = [
        Path.home() / "Library" / "Application Support" / "Antigravity" / "mcp_config.json",
        Path.home() / "Library" / "Application Support" / "Google" / "Antigravity" / "mcp_config.json",
        Path.home() / ".config" / "antigravity" / "mcp_config.json",
        Path.home() / ".config" / "Antigravity" / "mcp_config.json",
    ]
    matches = [path.resolve() for path in direct_candidates if path.exists()]
    if matches:
        return matches

    search_roots = [
        Path.home() / "Library" / "Application Support",
        Path.home() / ".config",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("mcp_config.json"):
            if path.is_file():
                matches.append(path.resolve())
    unique_matches: list[Path] = []
    seen: set[Path] = set()
    for path in matches:
        if path in seen:
            continue
        seen.add(path)
        unique_matches.append(path)
    return unique_matches


def _load_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse JSON config file: {path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return loaded


def _write_json_object(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _build_local_mcp_invocation(
    *,
    config_path: Path,
    python_executable: str | None,
) -> list[str]:
    return _build_local_entrypoint_invocation(
        config_path=config_path,
        python_executable=python_executable,
        subcommand="mcp",
    )


def _build_local_http_mcp_invocation(
    *,
    config_path: Path,
    python_executable: str | None,
    host: str,
    port: int,
) -> list[str]:
    return _build_local_entrypoint_invocation(
        config_path=config_path,
        python_executable=python_executable,
        subcommand="mcp-http",
        subcommand_args=["--host", host, "--port", str(port)],
    )


def _build_local_entrypoint_invocation(
    *,
    config_path: Path,
    python_executable: str | None,
    subcommand: str,
    subcommand_args: list[str] | None = None,
) -> list[str]:
    extra_args = subcommand_args or []
    self_command = _resolve_self_command()
    if self_command is not None:
        return [str(self_command), "--config", str(config_path), subcommand, *extra_args]

    python_path = _resolve_python_executable(python_executable)
    project_root = Path(__file__).resolve().parent.parent
    main_script = project_root / "main.py"

    if main_script.exists():
        return [str(python_path), str(main_script), "--config", str(config_path), subcommand, *extra_args]

    return [str(python_path), "-m", "autokyo", "--config", str(config_path), subcommand, *extra_args]


def _resolve_self_command() -> Path | None:
    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.name != "autokyo":
        return None

    resolved = shutil.which("autokyo")
    if resolved:
        return Path(resolved).expanduser()
    if argv0.exists():
        return argv0
    return None


def _resolve_mcp_transport(client: str, transport: str) -> str:
    if transport != "auto":
        return transport
    if client in {"codex", "claude", "antigravity"}:
        return "http"
    return "stdio"


def _resolve_python_executable(python_executable: str | None) -> Path:
    if python_executable:
        return Path(python_executable).expanduser().resolve()

    candidates: list[Path] = []
    for command_name in ("python3", "python"):
        command_path = shutil.which(command_name)
        if command_path:
            path = Path(command_path).expanduser().resolve()
            if ".venv" not in path.parts:
                candidates.append(path)

    current_python = Path(sys.executable).expanduser().resolve()
    if ".venv" not in current_python.parts:
        candidates.append(current_python)

    candidates.append(current_python)
    return candidates[0]
