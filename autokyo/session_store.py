from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
import json


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CaptureRecord:
    page_index: int
    state_digest: str
    captured_at: str


@dataclass
class SessionState:
    started_at: str
    updated_at: str
    status: str
    current_page_index: int
    last_screen_digest: str | None = None
    stop_reason: str | None = None
    captures: list[CaptureRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        payload = asdict(self)
        payload["captures"] = [asdict(item) for item in self.captures]
        return payload

    @classmethod
    def from_json(cls, payload: dict) -> "SessionState":
        captures = [
            CaptureRecord(
                page_index=int(item["page_index"]),
                state_digest=item["state_digest"],
                captured_at=item["captured_at"],
            )
            for item in payload.get("captures", [])
        ]
        return cls(
            started_at=payload["started_at"],
            updated_at=payload["updated_at"],
            status=payload["status"],
            current_page_index=int(payload["current_page_index"]),
            last_screen_digest=payload.get("last_screen_digest"),
            stop_reason=payload.get("stop_reason"),
            captures=captures,
            errors=list(payload.get("errors", [])),
        )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> SessionState | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return SessionState.from_json(payload)

    def create(self, start_index: int) -> SessionState:
        now = utc_now_iso()
        state = SessionState(
            started_at=now,
            updated_at=now,
            status="running",
            current_page_index=start_index,
        )
        self.save(state)
        return state

    def save(self, state: SessionState) -> None:
        state.updated_at = utc_now_iso()
        self.path.write_text(
            json.dumps(state.to_json(), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def append_capture(self, state: SessionState, record: CaptureRecord) -> None:
        state.captures.append(record)
        self.save(state)

    def add_error(self, state: SessionState, message: str) -> None:
        state.errors.append(message)
        self.save(state)

    def mark_completed(self, state: SessionState, reason: str) -> None:
        state.status = "completed"
        state.stop_reason = reason
        self.save(state)

    def mark_failed(self, state: SessionState, reason: str) -> None:
        state.status = "failed"
        state.stop_reason = reason
        self.save(state)
