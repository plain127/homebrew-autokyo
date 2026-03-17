from __future__ import annotations

import ctypes
from ctypes.util import find_library
import subprocess

from autokyo.config import TriggerSpec


class AutomationError(RuntimeError):
    pass


_QUARTZ = None
_KCGHID_EVENT_TAP = 0
_KCGEVENT_LEFT_MOUSE_DOWN = 1
_KCGEVENT_LEFT_MOUSE_UP = 2
_KCGEVENT_MOUSE_MOVED = 5
_KCGMOUSE_BUTTON_LEFT = 0


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load_quartz() -> ctypes.CDLL:
    global _QUARTZ
    if _QUARTZ is None:
        library = find_library("ApplicationServices")
        if not library:
            raise AutomationError("Could not load ApplicationServices for mouse automation")
        _QUARTZ = ctypes.CDLL(library)
        _QUARTZ.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            CGPoint,
            ctypes.c_uint32,
        ]
        _QUARTZ.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        _QUARTZ.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        _QUARTZ.CGEventPost.restype = None
        _QUARTZ.CGEventCreate.argtypes = [ctypes.c_void_p]
        _QUARTZ.CGEventCreate.restype = ctypes.c_void_p
        _QUARTZ.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        _QUARTZ.CGEventGetLocation.restype = CGPoint
        _QUARTZ.CFRelease.argtypes = [ctypes.c_void_p]
        _QUARTZ.CFRelease.restype = None
    return _QUARTZ


def get_mouse_position() -> tuple[int, int]:
    quartz = _load_quartz()
    event = quartz.CGEventCreate(None)
    if not event:
        raise AutomationError("Failed to create Quartz event for reading mouse position")
    try:
        point = quartz.CGEventGetLocation(event)
        return int(round(point.x)), int(round(point.y))
    finally:
        quartz.CFRelease(event)


class MacOSAutomation:
    def trigger(self, spec: TriggerSpec, *, label: str) -> None:
        if spec.kind == "keycode":
            if spec.keycode is None:
                raise AutomationError(f"Missing keycode for {label}")
            self._trigger_keycode(spec.keycode)
            return
        if spec.kind == "mouse_click":
            if spec.point is None:
                raise AutomationError(f"Missing click point for {label}")
            self._click_at(*spec.point)
            return
        raise AutomationError(f"Unsupported trigger kind: {spec.kind}")

    def _click_at(self, x: int, y: int) -> None:
        quartz = _load_quartz()
        point = CGPoint(float(x), float(y))

        for event_type in (
            _KCGEVENT_MOUSE_MOVED,
            _KCGEVENT_LEFT_MOUSE_DOWN,
            _KCGEVENT_LEFT_MOUSE_UP,
        ):
            event = quartz.CGEventCreateMouseEvent(
                None,
                event_type,
                point,
                _KCGMOUSE_BUTTON_LEFT,
            )
            if not event:
                raise AutomationError(f"Failed to create mouse event at ({x}, {y})")
            quartz.CGEventPost(_KCGHID_EVENT_TAP, event)
            quartz.CFRelease(event)

    def _trigger_keycode(self, keycode: int) -> None:
        script = (
            'tell application "System Events"\n'
            f"  key code {int(keycode)}\n"
            "end tell"
        )
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise AutomationError(stderr) from exc
