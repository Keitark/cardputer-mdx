"""LCD and keyboard access for the CardputerZero launcher apps."""

from contextlib import contextmanager
import fcntl
import os
import select
import struct

import numpy as np


KEY_ESC, KEY_TAB, KEY_ENTER, KEY_SPACE = 1, 15, 28, 57
KEY_Q, KEY_W, KEY_E = 16, 17, 18
KEY_A, KEY_D = 30, 32
KEY_Z, KEY_C, KEY_F, KEY_X, KEY_S, KEY_L = 44, 46, 33, 45, 31, 38
KEY_V, KEY_B = 47, 48
KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN = 103, 105, 106, 108
KEY_PAGEUP, KEY_PAGEDOWN = 104, 109
KEY_MUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP = 113, 114, 115
KEY_NEXTSONG, KEY_PLAYPAUSE, KEY_PREVIOUSSONG = 163, 164, 165
INPUT_EVENT = struct.Struct("@llHHi")
EVIOCGRAB = 0x40044590


@contextmanager
def lcd_and_keyboard():
    with open("/dev/input/cardputer-zero-internal", "rb", buffering=0) as keyboard:
        with open("/dev/fb_lcd", "wb", buffering=0) as lcd:
            fcntl.ioctl(keyboard, EVIOCGRAB, 1)
            yield lcd, keyboard


def read_key_events(keyboard, delay=0.15):
    if not select.select([keyboard], [], [], delay)[0]:
        return []
    data = os.read(keyboard.fileno(), INPUT_EVENT.size * 32)
    return [(code, value) if event_type == 1 else (None, 0)
            for _, _, event_type, code, value in INPUT_EVENT.iter_unpack(data)
            if (event_type == 1 and value in (0, 1)) or (event_type == 0 and code == 3)]


def read_keys(keyboard, delay=0.15):
    return [code for code, pressed in read_key_events(keyboard, delay) if pressed]


def draw_lcd(lcd, image):
    pixels = np.asarray(image, dtype=np.uint16)
    rgb565 = ((pixels[:, :, 0] >> 3) << 11) | ((pixels[:, :, 1] >> 2) << 5)
    rgb565 |= pixels[:, :, 2] >> 3
    lcd.seek(0)
    lcd.write(rgb565.astype("<u2", copy=False).tobytes())
