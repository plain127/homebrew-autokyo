from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from autokyo.session_store import SessionState, SessionStore


class PhotosExportError(RuntimeError):
    pass


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".webp", ".bmp"}
_PHOTOS_REFERENCE_DATE = datetime(2001, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PhotosAssetCandidate:
    asset_uuid: str
    filename: str
    width: int
    height: int
    added_at: datetime
    created_at: datetime | None


@dataclass(frozen=True)
class PhotosExportSummary:
    session_file: Path
    library_db: Path
    output_dir: Path
    window_started_at: str
    window_ended_at: str
    expected_count: int
    candidate_count: int
    selected_count: int
    exported_count: int
    cleared_count: int
    missing_count: int
    first_selected_filename: str | None
    last_selected_filename: str | None
    selected_assets: tuple[PhotosAssetCandidate, ...] = ()


def export_photos_for_session(
    session_file: Path,
    output_dir: Path,
    *,
    library_db: Path,
    time_padding_seconds: float = 5.0,
    take_last: int | None = None,
    match_width: int | None = None,
    match_height: int | None = None,
    clear_output: bool = False,
    allow_fewer: bool = False,
    dry_run: bool = False,
) -> PhotosExportSummary:
    session_file = session_file.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    library_db = library_db.expanduser().resolve()

    if not session_file.exists():
        raise PhotosExportError(f"Session file does not exist: {session_file}")
    if not library_db.exists():
        raise PhotosExportError(f"Photos database does not exist: {library_db}")

    state = SessionStore(session_file).load()
    if state is None:
        raise PhotosExportError(f"Session file is empty or unreadable: {session_file}")

    expected_count = _resolve_expected_count(state, take_last=take_last)
    window_start, window_end = _build_session_window(
        state,
        time_padding_seconds=max(0.0, time_padding_seconds),
    )

    candidates = _query_candidates(
        library_db,
        window_start=window_start,
        window_end=window_end,
        match_width=match_width,
        match_height=match_height,
    )
    if not candidates:
        raise PhotosExportError(
            "No matching Photos images were found for the session window "
            f"{window_start.isoformat()} - {window_end.isoformat()}"
        )
    if len(candidates) < expected_count:
        if allow_fewer:
            selected = candidates
        else:
            raise PhotosExportError(
                f"Expected at least {expected_count} Photos images in the session window, "
                f"but found {len(candidates)}. "
                "Re-run with --allow-fewer if partial export is acceptable."
            )
    else:
        selected = candidates[-expected_count:]

    missing_count = max(0, expected_count - len(selected))
    if not allow_fewer and missing_count > 0:
        raise PhotosExportError(
            f"Expected at least {expected_count} Photos images in the session window, "
            f"but found {len(candidates)}. "
            "Re-run with --allow-fewer if partial export is acceptable."
        )

    cleared_count = 0
    exported_count = 0
    if not dry_run:
        cleared_count = _prepare_output_directory(output_dir, clear_output=clear_output)
        exported_count = _export_candidates(selected, output_dir=output_dir)

    return PhotosExportSummary(
        session_file=session_file,
        library_db=library_db,
        output_dir=output_dir,
        window_started_at=window_start.isoformat(),
        window_ended_at=window_end.isoformat(),
        expected_count=expected_count,
        candidate_count=len(candidates),
        selected_count=len(selected),
        exported_count=exported_count,
        cleared_count=cleared_count,
        missing_count=missing_count,
        first_selected_filename=selected[0].filename if selected else None,
        last_selected_filename=selected[-1].filename if selected else None,
        selected_assets=tuple(selected),
    )


def delete_photos_assets(candidates: list[PhotosAssetCandidate] | tuple[PhotosAssetCandidate, ...]) -> int:
    if not candidates:
        return 0

    _run_photos_script(
        _build_delete_script(list(candidates)),
        failure_prefix=(
            "Photos delete failed. Make sure Photos is installed and that the "
            "terminal has permission to automate Photos."
        ),
    )
    return len(candidates)


def _resolve_expected_count(state: SessionState, *, take_last: int | None) -> int:
    if take_last is not None:
        if take_last <= 0:
            raise PhotosExportError("--take-last must be a positive integer")
        return take_last

    capture_count = len(state.captures)
    if capture_count <= 0:
        raise PhotosExportError(
            "The session does not contain any captures. "
            "Use --take-last to explicitly choose how many Photos items to export."
        )
    return capture_count


def _build_session_window(
    state: SessionState,
    *,
    time_padding_seconds: float,
) -> tuple[datetime, datetime]:
    window_start = _parse_iso_datetime(state.started_at) - timedelta(seconds=time_padding_seconds)
    if state.captures:
        window_end_anchor = _parse_iso_datetime(state.captures[-1].captured_at)
    else:
        window_end_anchor = _parse_iso_datetime(state.updated_at)
    window_end = window_end_anchor + timedelta(seconds=time_padding_seconds)
    return window_start, window_end


def _query_candidates(
    library_db: Path,
    *,
    window_start: datetime,
    window_end: datetime,
    match_width: int | None,
    match_height: int | None,
) -> list[PhotosAssetCandidate]:
    query_lines = [
        "SELECT ZUUID, ZFILENAME, ZWIDTH, ZHEIGHT, ZADDEDDATE, ZDATECREATED",
        "FROM ZASSET",
        "WHERE ZTRASHEDSTATE = 0",
        "  AND ZCLOUDDELETESTATE = 0",
        "  AND ZFILENAME IS NOT NULL",
        "  AND ZADDEDDATE IS NOT NULL",
        "  AND ZADDEDDATE >= ?",
        "  AND ZADDEDDATE <= ?",
    ]
    params: list[float | int] = [
        _to_photos_absolute_seconds(window_start),
        _to_photos_absolute_seconds(window_end),
    ]

    if match_width is not None:
        query_lines.append("  AND ZWIDTH = ?")
        params.append(match_width)
    if match_height is not None:
        query_lines.append("  AND ZHEIGHT = ?")
        params.append(match_height)

    query_lines.append("ORDER BY ZADDEDDATE ASC, Z_PK ASC")

    connection = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("\n".join(query_lines), params).fetchall()
    finally:
        connection.close()

    candidates: list[PhotosAssetCandidate] = []
    for row in rows:
        filename = str(row["ZFILENAME"] or "").strip()
        if Path(filename).suffix.lower() not in _IMAGE_EXTENSIONS:
            continue
        candidates.append(
            PhotosAssetCandidate(
                asset_uuid=str(row["ZUUID"]),
                filename=filename,
                width=int(row["ZWIDTH"] or 0),
                height=int(row["ZHEIGHT"] or 0),
                added_at=_from_photos_absolute_seconds(float(row["ZADDEDDATE"])),
                created_at=(
                    _from_photos_absolute_seconds(float(row["ZDATECREATED"]))
                    if row["ZDATECREATED"] is not None
                    else None
                ),
            )
        )
    return candidates


def _prepare_output_directory(output_dir: Path, *, clear_output: bool) -> int:
    if output_dir.exists() and not output_dir.is_dir():
        raise PhotosExportError(f"Output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    existing_images = [
        path
        for path in output_dir.iterdir()
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS
    ]
    if not existing_images:
        return 0

    if not clear_output:
        raise PhotosExportError(
            f"Output directory already contains {len(existing_images)} image files: {output_dir}. "
            "Use --clear-output to remove them first."
        )

    cleared_count = 0
    for path in existing_images:
        path.unlink()
        cleared_count += 1
    return cleared_count


def _export_candidates(candidates: list[PhotosAssetCandidate], *, output_dir: Path) -> int:
    pad_width = max(4, len(str(len(candidates))))
    with tempfile.TemporaryDirectory(prefix="autokyo_photos_export_") as temp_dir:
        staging_root = Path(temp_dir)
        export_specs = []
        for index, candidate in enumerate(candidates, start=1):
            staging_dir = staging_root / f"item_{index:0{pad_width}d}"
            staging_dir.mkdir(parents=True, exist_ok=True)
            export_specs.append((candidate, staging_dir))

        script = _build_export_script(export_specs)
        _run_photos_script(
            script,
            failure_prefix=(
                "Photos export failed. Make sure Photos is installed and that the "
                "terminal has permission to automate Photos."
            ),
        )

        exported_count = 0
        for index, (_, staging_dir) in enumerate(export_specs, start=1):
            exported_files = [
                path
                for path in staging_dir.iterdir()
                if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS
            ]
            if len(exported_files) != 1:
                raise PhotosExportError(
                    f"Expected exactly one exported image in {staging_dir}, found {len(exported_files)}"
                )
            exported_path = exported_files[0]
            target_path = output_dir / f"{index:0{pad_width}d}{exported_path.suffix.lower()}"
            if target_path.exists():
                raise PhotosExportError(f"Target capture file already exists: {target_path}")
            shutil.move(str(exported_path), str(target_path))
            exported_count += 1

    return exported_count


def _build_export_script(export_specs: list[tuple[PhotosAssetCandidate, Path]]) -> str:
    spec_literals: list[str] = []
    for candidate, staging_dir in export_specs:
        spec_literals.append(
            "{outputDir:"
            + _applescript_string(str(staging_dir))
            + ", assetFilename:"
            + _applescript_string(candidate.filename)
            + ", assetWidth:"
            + str(candidate.width)
            + ", assetHeight:"
            + str(candidate.height)
            + "}"
        )
    lines = ["set exportSpecs to {" + ", ".join(spec_literals) + "}"]
    lines.extend(
        [
            "",
            'tell application id "com.apple.Photos"',
            "  repeat with exportSpec in exportSpecs",
            "    set targetFilename to assetFilename of exportSpec",
            "    set targetWidth to assetWidth of exportSpec",
            "    set targetHeight to assetHeight of exportSpec",
            "    set matchedItems to every media item whose filename is targetFilename and width is targetWidth and height is targetHeight",
            "    if (count of matchedItems) is 0 then",
            '      error "No Photos media item matched filename: " & targetFilename',
            "    end if",
            "    if (count of matchedItems) is greater than 1 then",
            '      error "Multiple Photos media items matched filename: " & targetFilename',
            "    end if",
            "    export matchedItems to POSIX file (outputDir of exportSpec) using originals true",
            "  end repeat",
            "end tell",
        ]
    )
    return "\n".join(lines)


def _build_delete_script(candidates: list[PhotosAssetCandidate]) -> str:
    spec_literals = [_candidate_spec_literal(candidate) for candidate in candidates]
    lines = ["set deleteSpecs to {" + ", ".join(spec_literals) + "}"]
    lines.extend(
        [
            "",
            'tell application id "com.apple.Photos"',
            "  repeat with deleteSpec in deleteSpecs",
            "    set targetFilename to assetFilename of deleteSpec",
            "    set targetWidth to assetWidth of deleteSpec",
            "    set targetHeight to assetHeight of deleteSpec",
            "    set matchedItems to every media item whose filename is targetFilename and width is targetWidth and height is targetHeight",
            "    if (count of matchedItems) is 0 then",
            '      error "No Photos media item matched filename: " & targetFilename',
            "    end if",
            "    if (count of matchedItems) is greater than 1 then",
            '      error "Multiple Photos media items matched filename: " & targetFilename',
            "    end if",
            "    delete (item 1 of matchedItems)",
            "  end repeat",
            "end tell",
        ]
    )
    return "\n".join(lines)


def _candidate_spec_literal(candidate: PhotosAssetCandidate) -> str:
    return (
        "{assetFilename:"
        + _applescript_string(candidate.filename)
        + ", assetWidth:"
        + str(candidate.width)
        + ", assetHeight:"
        + str(candidate.height)
        + "}"
    )


def _run_photos_script(script: str, *, failure_prefix: str) -> None:
    osascript_path = shutil.which("osascript")
    if not osascript_path:
        raise PhotosExportError("osascript command not found")

    try:
        subprocess.run(
            [osascript_path, "-l", "AppleScript"],
            input=script,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise PhotosExportError(f"{failure_prefix}\n{stderr}") from exc


def _parse_iso_datetime(raw_value: str) -> datetime:
    timestamp = datetime.fromisoformat(raw_value)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _to_photos_absolute_seconds(timestamp: datetime) -> float:
    return (timestamp.astimezone(timezone.utc) - _PHOTOS_REFERENCE_DATE).total_seconds()


def _from_photos_absolute_seconds(value: float) -> datetime:
    return _PHOTOS_REFERENCE_DATE + timedelta(seconds=value)


def _applescript_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
