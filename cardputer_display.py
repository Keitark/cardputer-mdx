"""LCD and keyboard access for the CardputerZero launcher apps."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import select
import struct
import time

import numpy as np


KEY_ESC, KEY_TAB, KEY_ENTER, KEY_SPACE = 1, 15, 28, 57
KEY_Q, KEY_W, KEY_E = 16, 17, 18
KEY_A, KEY_D = 30, 32
KEY_Z, KEY_C, KEY_F, KEY_X, KEY_S, KEY_L = 44, 46, 33, 45, 31, 38
KEY_V, KEY_B = 47, 48
KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN = 103, 105, 106, 108
KEY_PAGEUP, KEY_PAGEDOWN = 104, 109
KEY_MUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP = 113, 114, 115
KEY_PAUSE = 119
KEY_NEXTSONG, KEY_PLAYPAUSE, KEY_PREVIOUSSONG = 163, 164, 165
KEY_PLAYCD, KEY_PAUSECD, KEY_PLAY = 200, 201, 207
INPUT_EVENT = struct.Struct("@llHHi")
EVIOCGRAB = 0x40044590
INPUT_CLASS = Path("/sys/class/input")
INPUT_DEVICES = Path("/dev/input")
BLUETOOTH_SCAN_INTERVAL = 1.0
BLUETOOTH_PLAYBACK_KEYS = {
    KEY_PLAYCD, KEY_PLAY, KEY_PAUSECD, KEY_PAUSE, KEY_PLAYPAUSE,
    KEY_NEXTSONG, KEY_PREVIOUSSONG,
}


class BluetoothMediaInput:
    """Read media buttons from BlueZ's AVRCP input devices."""

    def __init__(self):
        self._devices = {}
        self._next_scan = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        for descriptor in self._devices.values():
            os.close(descriptor)
        self._devices.clear()
        self._next_scan = 0.0

    def descriptors(self):
        now = time.monotonic()
        if now >= self._next_scan:
            self._next_scan = now + BLUETOOTH_SCAN_INTERVAL
            self._scan()
        return self._devices.values()

    def _scan(self):
        try:
            entries = list(INPUT_CLASS.glob("event*/device/name"))
        except OSError:
            return
        found = set()
        for entry in entries:
            try:
                name = entry.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if name.endswith(" (AVRCP)"):
                found.add(INPUT_DEVICES / entry.parents[1].name)
        for path in self._devices.keys() - found:
            os.close(self._devices.pop(path))
        for path in found - self._devices.keys():
            try:
                self._devices[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue

    def read_ready(self, ready):
        events = []
        for path, descriptor in list(self._devices.items()):
            if descriptor not in ready:
                continue
            try:
                data = os.read(descriptor, INPUT_EVENT.size * 32)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:
                os.close(self._devices.pop(path))
                continue
            events.extend((code, value)
                          for _, _, event_type, code, value in INPUT_EVENT.iter_unpack(data)
                          if event_type == 1 and value == 1 and code in BLUETOOTH_PLAYBACK_KEYS)
        return events


@contextmanager
def lcd_and_keyboard():
    with open("/dev/input/cardputer-zero-internal", "rb", buffering=0) as keyboard:
        with open("/dev/fb_lcd", "wb", buffering=0) as lcd:
            fcntl.ioctl(keyboard, EVIOCGRAB, 1)
            yield lcd, keyboard


def read_key_events(keyboard, delay=0.15, *, bluetooth=None):
    inputs = [keyboard]
    if bluetooth is not None:
        inputs.extend(bluetooth.descriptors())
    ready, _, _ = select.select(inputs, [], [], delay)
    events = []
    if keyboard in ready:
        data = os.read(keyboard.fileno(), INPUT_EVENT.size * 32)
        events.extend((code, value) if event_type == 1 else (None, 0)
                      for _, _, event_type, code, value in INPUT_EVENT.iter_unpack(data)
                      if (event_type == 1 and value in (0, 1)) or (event_type == 0 and code == 3))
    if bluetooth is not None:
        events.extend(bluetooth.read_ready(ready))
    return events


def read_keys(keyboard, delay=0.15):
    return [code for code, pressed in read_key_events(keyboard, delay) if pressed]


def draw_lcd(lcd, image):
    pixels = np.asarray(image, dtype=np.uint16)
    rgb565 = ((pixels[:, :, 0] >> 3) << 11) | ((pixels[:, :, 1] >> 2) << 5)
    rgb565 |= pixels[:, :, 2] >> 3
    lcd.seek(0)
    lcd.write(rgb565.astype("<u2", copy=False).tobytes())
