from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import subprocess
import tempfile
import time

from autokyo.config import Rect


class PageStateError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PageState:
    digest: str
    byte_size: int
    captured_at: str
    sample_path: str | None = None


class PageStateDetector:
    def __init__(
        self,
        *,
        region: Rect,
        artifact_dir: Path,
        poll_interval_seconds: float,
        stability_polls: int,
    ) -> None:
        self.region = region
        self.artifact_dir = artifact_dir
        self.poll_interval_seconds = poll_interval_seconds
        self.stability_polls = max(1, stability_polls)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def capture_state(self, *, persist: bool = False, prefix: str = "state") -> PageState:
        timestamp = int(time.time() * 1000)
        with tempfile.NamedTemporaryFile(
            suffix=".png",
            prefix=f"{prefix}_{timestamp}_",
            dir=self.artifact_dir,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)

        try:
            subprocess.run(
                ["screencapture", "-x", self.region.as_screencapture_arg(), str(temp_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = temp_path.read_bytes()
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            temp_path.unlink(missing_ok=True)
            raise PageStateError(f"screencapture failed: {stderr}") from exc
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise PageStateError(f"Unable to read captured state image: {exc}") from exc

        digest = hashlib.sha256(payload).hexdigest()
        saved_path = str(temp_path) if persist else None
        if not persist:
            temp_path.unlink(missing_ok=True)

        return PageState(
            digest=digest,
            byte_size=len(payload),
            captured_at=utc_now_iso(),
            sample_path=saved_path,
        )

    def wait_for_change(self, previous: PageState, *, timeout_seconds: float) -> PageState | None:
        deadline = time.monotonic() + timeout_seconds
        last_changed_digest: str | None = None
        stable_hits = 0

        while time.monotonic() < deadline:
            current = self.capture_state()
            if current.digest != previous.digest:
                if current.digest == last_changed_digest:
                    stable_hits += 1
                else:
                    last_changed_digest = current.digest
                    stable_hits = 1

                if stable_hits >= self.stability_polls:
                    return current
            else:
                last_changed_digest = None
                stable_hits = 0

            time.sleep(self.poll_interval_seconds)

        return None
