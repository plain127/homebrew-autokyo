from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Literal


class PdfBuildError(RuntimeError):
    pass


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".webp", ".bmp"}
_NATURAL_SORT_RE = re.compile(r"(\d+)")
SortMode = Literal["auto", "created", "modified", "name"]


@dataclass(frozen=True)
class PdfBuildSummary:
    input_dir: Path
    output_file: Path
    image_count: int
    sort_by: SortMode
    deleted_count: int


def build_pdf_from_directory(
    input_dir: Path,
    output_file: Path,
    *,
    sort_by: SortMode = "auto",
    delete_source: bool = False,
) -> PdfBuildSummary:
    input_dir = input_dir.expanduser().resolve()
    output_file = output_file.expanduser().resolve()

    if not input_dir.exists():
        raise PdfBuildError(f"Input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise PdfBuildError(f"Input path is not a directory: {input_dir}")

    image_paths = _collect_images(input_dir, sort_by=sort_by)
    if not image_paths:
        raise PdfBuildError(f"No supported images found in {input_dir}")

    sips_path = shutil.which("sips")
    pdfunite_path = shutil.which("pdfunite")
    if not sips_path:
        raise PdfBuildError("sips command not found")
    if not pdfunite_path:
        raise PdfBuildError("pdfunite command not found")

    output_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="autokyo_pdf_") as temp_dir:
        temp_path = Path(temp_dir)
        page_pdfs: list[Path] = []

        for index, image_path in enumerate(image_paths, start=1):
            page_pdf = temp_path / f"{index:05d}.pdf"
            _convert_image_to_pdf(sips_path, image_path=image_path, output_pdf=page_pdf)
            page_pdfs.append(page_pdf)

        if len(page_pdfs) == 1:
            shutil.copy2(page_pdfs[0], output_file)
        else:
            _merge_pdfs(pdfunite_path, page_pdfs=page_pdfs, output_file=output_file)

    deleted_count = 0
    if delete_source:
        deleted_count = _delete_source_images(image_paths)

    return PdfBuildSummary(
        input_dir=input_dir,
        output_file=output_file,
        image_count=len(image_paths),
        sort_by=sort_by,
        deleted_count=deleted_count,
    )


def _collect_images(input_dir: Path, *, sort_by: SortMode) -> list[Path]:
    images = [
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS
    ]
    return sorted(images, key=lambda path: _sort_key(path, sort_by=sort_by))


def _natural_sort_key(value: str) -> list[int | str]:
    parts = _NATURAL_SORT_RE.split(value.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def _sort_key(path: Path, *, sort_by: SortMode) -> tuple:
    stat = path.stat()
    name_key = tuple(_natural_sort_key(path.name))

    if sort_by == "name":
        return (name_key,)

    if sort_by == "modified":
        return (stat.st_mtime_ns, name_key)

    created_ns = _read_content_creation_time_ns(path)
    if created_ns is None:
        birth_time = getattr(stat, "st_birthtime", None)
        if birth_time is not None:
            created_ns = int(birth_time * 1_000_000_000)
        else:
            created_ns = int(stat.st_ctime * 1_000_000_000)

    if sort_by == "created":
        return (created_ns, name_key)

    # auto: prefer content creation time when available, then filesystem creation time,
    # then modification time, and finally the filename.
    return (created_ns, stat.st_mtime_ns, name_key)


def _read_content_creation_time_ns(path: Path) -> int | None:
    mdls_path = shutil.which("mdls")
    if not mdls_path:
        return None

    try:
        result = subprocess.run(
            [mdls_path, "-raw", "-name", "kMDItemContentCreationDate", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return None

    raw = result.stdout.strip()
    if not raw or raw == "(null)":
        return None

    try:
        timestamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return int(timestamp.timestamp() * 1_000_000_000)


def _convert_image_to_pdf(sips_path: str, *, image_path: Path, output_pdf: Path) -> None:
    try:
        subprocess.run(
            [sips_path, "-s", "format", "pdf", str(image_path), "--out", str(output_pdf)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise PdfBuildError(f"Failed to convert image to PDF: {image_path}\n{stderr}") from exc


def _merge_pdfs(pdfunite_path: str, *, page_pdfs: list[Path], output_file: Path) -> None:
    try:
        subprocess.run(
            [pdfunite_path, *(str(path) for path in page_pdfs), str(output_file)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise PdfBuildError(f"Failed to merge PDF pages into {output_file}\n{stderr}") from exc


def _delete_source_images(image_paths: list[Path]) -> int:
    deleted_count = 0
    failed_paths: list[str] = []

    for path in image_paths:
        try:
            path.unlink()
            deleted_count += 1
        except OSError:
            failed_paths.append(str(path))

    if failed_paths:
        raise PdfBuildError(
            "PDF was created, but some source images could not be deleted:\n"
            + "\n".join(failed_paths)
        )

    return deleted_count
