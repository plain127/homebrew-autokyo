from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from autokyo.actions import AutomationError, MacOSAutomation
from autokyo.config import ActionStep, RuntimeConfig
from autokyo.page_state import PageState, PageStateDetector
from autokyo.session_store import CaptureRecord, SessionState, SessionStore, utc_now_iso


class OrchestratorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunSummary:
    captures_completed: int
    state_file: Path
    stop_reason: str


class CaptureOrchestrator:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.store = SessionStore(config.paths.state_file)
        self.automation = MacOSAutomation()
        self.detector = PageStateDetector(
            region=config.page.change_region,
            artifact_dir=config.paths.artifact_dir,
            poll_interval_seconds=config.page.poll_interval_seconds,
            stability_polls=config.page.stability_polls,
        )

    def run(self) -> RunSummary:
        state = self._load_or_create_session()
        try:
            if self.config.loop.startup_delay_seconds > 0:
                time.sleep(self.config.loop.startup_delay_seconds)

            current_state = self.detector.capture_state()
            state.last_screen_digest = current_state.digest
            self.store.save(state)

            current_state = self._resume_if_needed(state, current_state)
            if current_state is None:
                return self._summary_from_state(state)

            while True:
                if self._reached_max_pages(state):
                    self.store.mark_completed(state, "Reached configured max_pages")
                    return self._summary_from_state(state)

                page_index = state.current_page_index

                self.automation.trigger(self.config.capture_trigger, label="capture")
                self._sleep_ms(self.config.capture.post_action_delay_ms)
                self._run_action_steps(self.config.capture.post_steps, label_prefix="capture.post_steps")

                self.store.append_capture(
                    state,
                    CaptureRecord(
                        page_index=page_index,
                        state_digest=current_state.digest,
                        captured_at=utc_now_iso(),
                    ),
                )

                if self._reached_max_pages(state):
                    self.store.mark_completed(state, "Reached configured max_pages")
                    return self._summary_from_state(state)

                next_state = self._advance_page(current_state)
                if next_state is None:
                    self.store.mark_completed(
                        state,
                        "No page change detected for "
                        f"{self.config.page.stall_timeout_seconds:.1f}s after next-page trigger; stopping",
                    )
                    return self._summary_from_state(state)

                state.current_page_index += 1
                state.last_screen_digest = next_state.digest
                self.store.save(state)
                current_state = next_state

                if self.config.loop.cooldown_seconds > 0:
                    time.sleep(self.config.loop.cooldown_seconds)

        except (AutomationError, OrchestratorError, OSError) as exc:
            self.store.add_error(state, str(exc))
            self.store.mark_failed(state, str(exc))
            raise

    def _load_or_create_session(self) -> SessionState:
        if self.config.loop.resume:
            existing = self.store.load()
            if existing is not None:
                if existing.status == "completed":
                    return self.store.create(self.config.page.start_index)
                if existing.status == "failed" and not existing.captures:
                    return self.store.create(self.config.page.start_index)
                existing.status = "running"
                self.store.save(existing)
                return existing
        return self.store.create(self.config.page.start_index)

    def _resume_if_needed(self, state: SessionState, current_state: PageState) -> PageState | None:
        if not self.config.loop.resume or not state.captures:
            return current_state

        last_capture = state.captures[-1]
        if last_capture.state_digest != current_state.digest:
            return current_state

        next_state = self._advance_page(current_state)
        if next_state is None:
            self.store.mark_completed(
                state,
                "Resume found no page change for "
                f"{self.config.page.stall_timeout_seconds:.1f}s after next-page trigger",
            )
            return None

        state.current_page_index = last_capture.page_index + 1
        state.last_screen_digest = next_state.digest
        self.store.save(state)
        return next_state

    def _advance_page(self, current_state: PageState) -> PageState | None:
        deadline = time.monotonic() + self.config.page.stall_timeout_seconds

        while time.monotonic() < deadline:
            self.automation.trigger(self.config.next_page_trigger, label="next_page")
            self._sleep_ms(self.config.page.post_turn_delay_ms)

            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break

            new_state = self.detector.wait_for_change(
                current_state,
                timeout_seconds=min(self.config.page.change_timeout_seconds, remaining_seconds),
            )
            if new_state is not None:
                return new_state

            if time.monotonic() < deadline:
                time.sleep(self.config.page.poll_interval_seconds)
        return None

    def _run_action_steps(self, steps: tuple[ActionStep, ...], *, label_prefix: str) -> None:
        for index, step in enumerate(steps, start=1):
            self.automation.trigger(step.trigger, label=f"{label_prefix}[{index}]")
            self._sleep_ms(step.delay_ms)

    def _reached_max_pages(self, state: SessionState) -> bool:
        if self.config.loop.max_pages is None:
            return False
        return len(state.captures) >= self.config.loop.max_pages

    def _sleep_ms(self, milliseconds: int) -> None:
        if milliseconds > 0:
            time.sleep(milliseconds / 1000)

    def _summary_from_state(self, state: SessionState) -> RunSummary:
        return RunSummary(
            captures_completed=len(state.captures),
            state_file=self.config.paths.state_file,
            stop_reason=state.stop_reason or state.status,
        )
