from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from autokyo.actions import AutomationError, get_mouse_position
from autokyo.config import ConfigError, load_config, resolve_config_path
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
        default=None,
        help=(
            "Path to TOML config file. If omitted, AutoKyo searches ./config.toml, "
            "~/Library/Application Support/AutoKyo/config.toml, and ~/.config/autokyo/config.toml"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("mcp", help="Run the Autokyo MCP server over stdio")
    subparsers.add_parser("run", help="Run the capture loop")
    subparsers.add_parser("probe", help="Capture and save one page-check region sample")
    subparsers.add_parser("status", help="Print session state JSON if present")
    pdf_parser = subparsers.add_parser(
        "make-pdf",
        aliases=["pdf"],
        help="Combine images in a folder into one PDF",
    )
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
            return run_stdio_server(default_config_path=resolve_config_path(args.config))

        if args.command in {"mousepos", "coords"}:
            return _run_mousepos(watch=bool(args.watch), interval=float(args.interval))

        if args.command in {"make-pdf", "pdf"}:
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

        if args.command in {"mcp-install", "mcp-register"}:
            return _install_mcp_server(
                client=args.client,
                server_name=args.name,
                config_path_arg=args.config,
                python_executable=args.python_executable,
                client_config_path=args.client_config,
                scope=args.scope,
                dry_run=bool(args.dry_run),
            )

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


def _install_mcp_server(
    *,
    client: str,
    server_name: str,
    config_path_arg: str | None,
    python_executable: str | None,
    client_config_path: str | None,
    scope: str,
    dry_run: bool,
) -> int:
    normalized_client = "claude" if client == "claude-code" else client
    resolved_config = resolve_config_path(config_path_arg)
    invocation = _build_local_mcp_invocation(
        config_path=resolved_config,
        python_executable=python_executable,
    )

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
        payload = {
            "status": "ready" if dry_run else "completed",
            "client": normalized_client,
            "name": server_name,
            "scope": scope,
            "config_path": str(resolved_config),
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
    self_command = _resolve_self_command()
    if self_command is not None:
        return [str(self_command), "--config", str(config_path), "mcp"]

    python_path = _resolve_python_executable(python_executable)
    project_root = Path(__file__).resolve().parent.parent
    main_script = project_root / "main.py"

    if main_script.exists():
        return [str(python_path), str(main_script), "--config", str(config_path), "mcp"]

    return [str(python_path), "-m", "autokyo", "--config", str(config_path), "mcp"]


def _resolve_self_command() -> Path | None:
    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.name != "autokyo":
        return None

    if argv0.exists():
        return argv0.resolve()

    resolved = shutil.which("autokyo")
    if resolved:
        return Path(resolved).expanduser().resolve()
    return None


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
