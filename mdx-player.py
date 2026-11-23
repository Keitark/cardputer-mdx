#!/usr/bin/python3
"""X68000 MDX/PDX library player for the CardputerZero LCD."""

import argparse
import fcntl
from functools import lru_cache
import json
from math import ceil
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

from cardputer_display import (
    KEY_A, KEY_B, KEY_C, KEY_D, KEY_DOWN, KEY_E, KEY_ENTER, KEY_ESC, KEY_F, KEY_L, KEY_LEFT,
    KEY_MUTE, KEY_NEXTSONG, KEY_PAGEDOWN, KEY_PAGEUP, KEY_PLAYPAUSE, KEY_PREVIOUSSONG,
    KEY_Q, KEY_RIGHT, KEY_S, KEY_SPACE, KEY_TAB, KEY_UP, KEY_V, KEY_VOLUMEDOWN, KEY_VOLUMEUP,
    KEY_W, KEY_X, KEY_Z, draw_lcd,
    lcd_and_keyboard, read_key_events,
)


ROOT = Path.home() / "Music" / "MDX Library"
DECODER = Path(__file__).resolve().with_name("mdx-render")
PREFERENCES = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "cardputer-mdx"
FAVORITES_FILE = PREFERENCES / "favorites.json"
LAST_TRACK_FILE = PREFERENCES / "last-track.json"
SETTINGS_FILE = PREFERENCES / "settings.json"
SESSION_ENV = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
BG, PANEL, GRID, EMPTY = "#040410", "#0a0a1e", "#373764", "#14142d"
TEXT, DIM, LILAC, AMBER = "#f5f5ff", "#9696b4", "#96aae6", "#ebaf50"
PEAK = "#5252ad"
STRIPES = (("#2d1e6e", "#46468c"), ("#46468c", "#6e6eb4"), ("#6e6eb4", LILAC))
ROWS = 7
FILER_MOVES = {KEY_UP: -1, KEY_F: -1, KEY_DOWN: 1, KEY_X: 1,
               KEY_PAGEUP: -ROWS, KEY_PAGEDOWN: ROWS}
REPEAT_DELAY, REPEAT_INTERVAL = 0.36, 0.09
ESC_HOLD_SECONDS = 1.0
PLAY_MODES = ("NEXT", "LOOP 1", "LOOP DIR", "SINGLE")
AUDIO_MODES = ("AUTO", "MONO", "STEREO")
KEY_M = 50
HEADPHONE_JACK = Path("/dev/input/by-path/platform-sound-event")
EVIOCGSW = 0x8004451B  # EVIOCGSW(4)
SW_HEADPHONE_INSERT = 2
JACK_POLL_SECONDS = 0.25
SINK = "@DEFAULT_AUDIO_SINK@"
MEDIA_KEYS = {KEY_A: KEY_MUTE, KEY_S: KEY_VOLUMEDOWN, KEY_D: KEY_VOLUMEUP,
              KEY_Q: KEY_PLAYPAUSE, KEY_SPACE: KEY_PLAYPAUSE,
              KEY_W: KEY_PREVIOUSSONG, KEY_E: KEY_NEXTSONG}
VOLUME_KEYS = {KEY_VOLUMEDOWN: ("set-volume", "-l", "1.0", SINK, "5%-"),
               KEY_VOLUMEUP: ("set-volume", "-l", "1.0", SINK, "5%+"),
               KEY_MUTE: ("set-mute", SINK, "toggle")}
TITLE_WIDTH, TITLE_HEIGHT, SCROLL_RATE, SCROLL_HOLD_MS = 302, 13, 28, 1400
TEXT_SCALE = 3
PLOT_STEPS = 28
EMPTY_METERS = (0,) * 96
CREDIT = re.compile(r"(?i)(?:^|[ /-])((?:reprogrammed by|programmed by|arranged by|"
                    r"composed by|ar\.by|by[: ])\s*.+)$")


@lru_cache(maxsize=10)
def font(size, mono=False):
    size *= TEXT_SCALE
    if mono:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    return ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", size,
                              index=0)


@lru_cache(maxsize=128)
def fit(text, size, width):
    face = font(size)
    limit = width * TEXT_SCALE
    if face.getlength(text) <= limit:
        return text
    while text and face.getlength(text + "…") > limit:
        text = text[:-1]
    return text + "…"


@lru_cache(maxsize=384)
def text_mask(text, size, mono, anchor):
    face = font(size, mono)
    left, top, right, bottom = face.getbbox(text, anchor=anchor)
    x = left // TEXT_SCALE
    y = top // TEXT_SCALE
    width = ceil(right / TEXT_SCALE) - x
    height = ceil(bottom / TEXT_SCALE) - y
    if not width or not height:
        return x, y, Image.new("L", (0, 0))
    # Give the resize filter room around the glyphs, then crop back to the text bounds.
    pad = 2
    padded_width, padded_height = width + 2 * pad, height + 2 * pad
    mask = Image.new("L", (padded_width * TEXT_SCALE, padded_height * TEXT_SCALE))
    ImageDraw.Draw(mask).text(((pad - x) * TEXT_SCALE, (pad - y) * TEXT_SCALE), text,
                              font=face, fill=255, anchor=anchor)
    resized = mask.resize((padded_width, padded_height), Image.Resampling.LANCZOS)
    return x, y, resized.crop((pad, pad, pad + width, pad + height))


def paint_text(image, position, text, size, color, mono=False, anchor="la"):
    if not text:
        return
    offset_x, offset_y, mask = text_mask(text, size, mono, anchor)
    if not mask.width:
        return
    x, y = position[0] + offset_x, position[1] + offset_y
    image.paste(color, (x, y, x + mask.width, y + mask.height), mask)


def decode_text(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace")


def audio_command(*args):
    try:
        return subprocess.run(["/usr/bin/wpctl", *args], env=SESSION_ENV, text=True,
                              capture_output=True, check=True, timeout=0.75).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read_system_volume():
    reading = re.search(r"Volume:\s*(\d+(?:\.\d+)?)(?:\s+(\[MUTED\]))?",
                        audio_command("get-volume", SINK))
    if not reading:
        return None, False
    return round(float(reading[1]) * 100), bool(reading[2])


def headphones_connected():
    switches = bytearray(4)
    try:
        with HEADPHONE_JACK.open("rb", buffering=0) as jack:
            fcntl.ioctl(jack, EVIOCGSW, switches, True)
    except OSError:
        return False
    return bool(switches[0] & (1 << SW_HEADPHONE_INSERT))


class Playback:
    def __init__(self):
        self.decoder = None
        self.audio = None
        self.mix_control = None
        self.mix_pending = False
        self.audio_mode = "AUTO"
        self.mono = True
        self.jack_poll_at = 0.0
        self.duration_probe = None
        self.duration_deadline = 0.0
        self.title = "Open the filer with Tab"
        self.engine = ""
        self.meters = EMPTY_METERS
        self.elapsed_ms = 0
        self.duration_ms = None
        self.status = "READY"
        self.pending = b""
        self.track = None

    @property
    def audio_label(self):
        if self.audio_mode == "AUTO":
            return "AUTO MONO" if self.mono else "AUTO STEREO"
        return self.audio_mode

    def cycle_audio_mode(self, now):
        index = AUDIO_MODES.index(self.audio_mode)
        self.audio_mode = AUDIO_MODES[(index + 1) % len(AUDIO_MODES)]
        self.jack_poll_at = 0.0
        self.update_audio_mode(now)

    def update_audio_mode(self, now):
        if self.audio_mode == "AUTO":
            if now >= self.jack_poll_at:
                self.jack_poll_at = now + JACK_POLL_SECONDS
                mono = not headphones_connected()
            else:
                mono = self.mono
        else:
            mono = self.audio_mode == "MONO"
        if mono != self.mono:
            self.mono = mono
            self.mix_pending = True
        if self.mix_pending and self.mix_control is not None:
            try:
                os.write(self.mix_control, b"M" if self.mono else b"S")
            except BlockingIOError:
                return
            except BrokenPipeError:
                # The track ended before the mode changed.
                pass
            self.mix_pending = False

    def start(self, path):
        cached_duration = self.duration_ms if path == self.track else None
        self.stop()
        self.track = path
        self.duration_ms = cached_duration
        self.title = path.stem
        self.engine = "MDX"
        self.status = "PLAY"
        mix_reader = None
        try:
            mix_reader, self.mix_control = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
            self.mix_pending = True
            self.update_audio_mode(time.monotonic())
            self.decoder = subprocess.Popen(
                [DECODER, "--mix-fd", str(mix_reader), path.name], cwd=path.parent,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(mix_reader,),
            )
            os.set_blocking(self.decoder.stderr.fileno(), False)
            self.audio = subprocess.Popen(
                ["/usr/bin/pacat", "--playback", "--raw", "--format=s16le", "--rate=44100",
                 "--channels=2", "--latency-msec=80", "--client-name=MDX Player"],
                stdin=self.decoder.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=SESSION_ENV,
            )
            self.decoder.stdout.close()
        except OSError:
            self.stop("ERROR")
            return
        finally:
            if mix_reader is not None:
                os.close(mix_reader)
        try:
            self.duration_probe = subprocess.Popen(
                [DECODER, "--duration", path.name], cwd=path.parent, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self.duration_deadline = time.monotonic() + 5
        except OSError:
            pass

    def stop(self, status="STOP"):
        for process in (self.decoder, self.audio, self.duration_probe):
            if process is None:
                continue
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        self.decoder = self.audio = None
        if self.mix_control is not None:
            os.close(self.mix_control)
            self.mix_control = None
        self.duration_probe = None
        self.status = status
        self.meters = EMPTY_METERS
        if status != "DONE":
            self.elapsed_ms = 0
            self.duration_ms = None
        self.pending = b""

    def play_pause(self):
        if not self.audio or self.audio.poll() is not None:
            if self.track is not None:
                self.start(self.track)
            return
        paused = self.status == "PAUSED"
        for process in (self.decoder, self.audio):
            if process and process.poll() is None:
                process.send_signal(signal.SIGCONT if paused else signal.SIGSTOP)
        self.status = "PLAY" if paused else "PAUSED"

    def update_duration(self):
        probe = self.duration_probe
        if probe is None:
            return
        result = probe.poll()
        if result is None:
            if time.monotonic() < self.duration_deadline:
                return
            probe.kill()
            probe.wait()
        elif result == 0:
            duration = probe.stdout.read().strip()
            if duration.isdigit():
                milliseconds = int(duration)
                if milliseconds > 0:
                    self.duration_ms = milliseconds
        probe.stdout.close()
        self.duration_probe = None

    def update(self):
        self.update_duration()
        if not self.decoder:
            return
        try:
            self.pending += os.read(self.decoder.stderr.fileno(), 8192)
        except BlockingIOError:
            pass
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        for line in lines:
            tag, _, value = line.partition(b" ")
            if tag == b"TITLE" and value.strip():
                self.title = decode_text(value).strip()
            elif tag == b"ENGINE":
                self.engine = decode_text(value)
            elif tag == b"METER" and self.status != "PAUSED":
                fields = value.split()
                if len(fields) == len(EMPTY_METERS) + 1 and all(field.isdigit() for field in fields):
                    self.elapsed_ms = int(fields[0])
                    self.meters = tuple(min(int(field), PLOT_STEPS) for field in fields[1:])
            elif tag == b"ERROR":
                self.title = decode_text(value)
        result = self.decoder.poll()
        audio_result = self.audio.poll() if self.audio else None
        if result not in (None, 0) or audio_result not in (None, 0):
            self.stop("ERROR")
        elif result == 0 and audio_result == 0:
            self.stop("DONE")


def entries_at(folder):
    try:
        entries = [path for path in folder.iterdir()
                   if path.is_dir() or path.suffix.lower() == ".mdx"]
    except OSError:
        return []
    return sorted(entries, key=lambda path: (not path.is_dir(), path.name.casefold()))


def adjacent_track(track, step, loop=False):
    if track is None:
        return None
    tracks = [path for path in entries_at(track.parent) if path.is_file()]
    if track not in tracks:
        return None
    index = tracks.index(track) + step
    if loop:
        return tracks[index % len(tracks)]
    return tracks[index] if 0 <= index < len(tracks) else None


def track_after(track, mode):
    if mode == "LOOP 1":
        return track
    if mode == "SINGLE":
        return None
    return adjacent_track(track, 1, loop=mode == "LOOP DIR")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, data):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.stem}-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False)
        temporary.replace(path)
        return True
    except (OSError, ValueError):
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return False


def restore_player_settings():
    settings = read_json(SETTINGS_FILE)
    if not isinstance(settings, dict):
        settings = {}
    mode = settings.get("play_mode")
    mode_index = PLAY_MODES.index(mode) if mode in PLAY_MODES else 0
    preferred = settings.get("volume")
    level, muted = read_system_volume()
    if type(preferred) is int and 0 <= preferred <= 100 and preferred != level:
        audio_command("set-volume", "-l", "1.0", SINK, f"{preferred}%")
        level, muted = read_system_volume()
    return mode_index, level, muted


def save_player_settings(level, mode):
    settings = {"play_mode": mode}
    if level is None:
        previous = read_json(SETTINGS_FILE)
        level = previous.get("volume") if isinstance(previous, dict) else None
    if type(level) is int and 0 <= level <= 100:
        settings["volume"] = level
    write_json(SETTINGS_FILE, settings)


def library_track(value):
    if not isinstance(value, str):
        return None
    path = Path(value)
    if not path.is_absolute() and ".." not in path.parts and path.suffix.lower() == ".mdx":
        return ROOT / path
    return None


def load_favorites():
    saved = read_json(FAVORITES_FILE)
    if not isinstance(saved, list):
        return set()
    return {path for item in saved if (path := library_track(item)) is not None}


def restore_last_track(filer, playback):
    saved = read_json(LAST_TRACK_FILE)
    if not isinstance(saved, dict) or (track := library_track(saved.get("track"))) is None:
        return
    if track.parent.is_dir():
        filer.open_folder(track.parent)
    if track.is_file() and track in filer.entries:
        filer.selection = filer.entries.index(track)
        playback.track = track
        title = saved.get("title")
        playback.title = title if isinstance(title, str) and title else track.stem
        duration = saved.get("duration_ms")
        if type(duration) is int and duration > 0:
            playback.duration_ms = duration


class Filer:
    def __init__(self):
        self.folder = ROOT
        self.library_entries = entries_at(ROOT)
        self.favorites = load_favorites()
        self.favorite_entries = self.list_favorites()
        self.selection = self.other_selection = 0
        self.active = self.show_favorites = False
        self.repeat_step = 0
        self.repeat_at = 0.0

    @property
    def entries(self):
        return self.favorite_entries if self.show_favorites else self.library_entries

    @property
    def selected(self):
        return self.entries[self.selection] if self.entries else None

    def list_favorites(self):
        return sorted((path for path in self.favorites if path.is_file()),
                      key=lambda path: (path.name.casefold(), path.as_posix()))

    def toggle(self, show_favorites):
        if self.active and self.show_favorites == show_favorites:
            self.close()
            return
        if self.show_favorites != show_favorites:
            self.selection, self.other_selection = self.other_selection, self.selection
            self.show_favorites = show_favorites
        self.active = True
        self.repeat_step = 0
        self.move(0)

    def close(self):
        self.active = False
        self.repeat_step = 0

    def toggle_favorite(self, path):
        if path is None or not path.is_file() or not path.is_relative_to(ROOT):
            return
        updated = self.favorites ^ {path}
        if write_json(FAVORITES_FILE, sorted(item.relative_to(ROOT).as_posix() for item in updated)):
            self.favorites = updated
            self.favorite_entries = self.list_favorites()
            self.move(0)

    def move(self, step):
        self.selection = max(0, min(len(self.entries) - 1, self.selection + step))

    def press_move(self, step, now):
        self.move(step)
        self.repeat_step = step if abs(step) == 1 else 0
        self.repeat_at = now + REPEAT_DELAY

    def release_move(self, key):
        if key is None or FILER_MOVES.get(key) == self.repeat_step:
            self.repeat_step = 0

    def repeat(self, now):
        if self.repeat_step and now >= self.repeat_at:
            self.move(self.repeat_step)
            self.repeat_at = now + REPEAT_INTERVAL

    def open_folder(self, folder):
        self.folder = folder
        self.library_entries = entries_at(folder)
        self.selection = 0
        self.repeat_step = 0

    def back(self):
        if self.show_favorites:
            self.toggle(False)
        elif self.folder != ROOT:
            previous = self.folder
            self.open_folder(previous.parent)
            if previous in self.entries:
                self.selection = self.entries.index(previous)


@lru_cache(maxsize=700)
def meter_column(width, height, level, marker, part_meter=False):
    column = Image.new("RGB", (width, height), EMPTY)
    draw = ImageDraw.Draw(column)
    filled = (level * height + PLOT_STEPS - 1) // PLOT_STEPS
    if part_meter and level < PLOT_STEPS:
        filled = min(filled, height - 1)
    for offset in range(filled):
        step = offset * PLOT_STEPS // height
        band = 0 if step < 9 else 1 if step < 19 else 2
        y = height - 1 - offset
        draw.line((0, y, width - 1, y), fill=STRIPES[band][offset % 2])
    if marker:
        y = height - (marker * height + PLOT_STEPS - 1) // PLOT_STEPS
        start = max(0, width - 5) if part_meter else 0
        draw.line((start, y, width - 1, y), fill=PEAK)
    return column


def plot(image, readings, y, height, part_meter=False):
    count = len(readings) // 2
    for index in range(count):
        left = 8 + index * 304 // count
        right = 8 + (index + 1) * 304 // count
        level, marker = readings[2 * index:2 * index + 2]
        image.paste(meter_column(right - left - 1, height, level, marker, part_meter), (left, y))


@lru_cache(maxsize=32)
def title_and_credit(title):
    credit = CREDIT.search(title)
    return (title[:credit.start()].rstrip(" /-"), credit[1]) if credit else (title, "")


@lru_cache(maxsize=32)
def title_strip(title):
    face = font(13)
    left, _, right, _ = face.getbbox(title, anchor="lt")
    x = -min(0, left // TEXT_SCALE)
    width = max(TITLE_WIDTH, x + ceil(max(right, face.getlength(title)) / TEXT_SCALE))
    strip = Image.new("RGB", (width, TITLE_HEIGHT), PANEL)
    paint_text(strip, (x, 0), title, 13, TEXT, anchor="lt")
    return strip


def title_scroll_offset(overflow, elapsed_ms):
    if overflow <= 0:
        return 0
    travel_ms = ceil(overflow * 1000 / SCROLL_RATE)
    phase = elapsed_ms % (2 * (travel_ms + SCROLL_HOLD_MS))
    if phase < SCROLL_HOLD_MS:
        return 0
    phase -= SCROLL_HOLD_MS
    if phase < travel_ms:
        return phase * SCROLL_RATE // 1000
    phase -= travel_ms
    if phase < SCROLL_HOLD_MS:
        return overflow
    return overflow - (phase - SCROLL_HOLD_MS) * SCROLL_RATE // 1000


def format_time(milliseconds):
    if milliseconds is None:
        return "--:--"
    minutes, seconds = divmod(milliseconds // 1000, 60)
    return f"{minutes:02d}:{seconds:02d}"


def paint_player(image, playback, mode, volume, now_favorite, offset):
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 319, 19), fill=PANEL)
    draw.line((0, 19, 319, 19), fill=GRID)
    paint_text(image, (8, 2), "MDX PLAYER", 13, TEXT, mono=True)
    paint_text(image, (96, 5), mode, 9, LILAC, mono=True)
    paint_text(image, (158, 5), volume, 9, LILAC, mono=True)
    clock = f"{format_time(playback.elapsed_ms)}/{format_time(playback.duration_ms)}"
    paint_text(image, (204, 5), clock, 9, LILAC, mono=True)
    paint_text(image, (311, 3), playback.status, 11, AMBER, mono=True, anchor="rt")

    draw.rectangle((5, 23, 314, 46), fill=PANEL)
    title, credit = title_and_credit(playback.title)
    image.paste(title_strip(title).crop((offset, 0, offset + TITLE_WIDTH, TITLE_HEIGHT)), (8, 24))
    paint_text(image, (8, 36), fit(credit, 9, 220), 9, LILAC)
    if now_favorite:
        paint_text(image, (252, 37), "FAV", 8, AMBER, mono=True, anchor="rt")
    paint_text(image, (310, 37), playback.engine, 8, DIM, mono=True, anchor="rt")

    paint_text(image, (8, 48), "NOTE SPECTRUM", 9, LILAC, mono=True)
    paint_text(image, (160, 48), playback.audio_label, 9, LILAC, mono=True, anchor="mt")
    paint_text(image, (311, 48), "MAX", 9, LILAC, mono=True, anchor="rt")
    plot(image, playback.meters[:64], 60, 39)
    for x, label in ((8, "28"), (146, "220"), (276, "3.5k Hz")):
        paint_text(image, (x, 100), label, 8, DIM, mono=True)

    paint_text(image, (8, 110), "FM8 + PCM8", 9, LILAC, mono=True)
    paint_text(image, (311, 110), "VELOCITY", 9, LILAC, mono=True, anchor="rt")
    plot(image, playback.meters[64:], 122, 25, part_meter=True)
    for index in range(16):
        label = f"F{index + 1}" if index < 8 else f"P{index - 7}"
        paint_text(image, (17 + index * 19, 147), label, 8, DIM, mono=True, anchor="mt")


def paint_filer(image, filer):
    entries, selection = filer.entries, filer.selection
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 20, 315, 155), fill=PANEL, outline=GRID)
    draw.rectangle((5, 21, 314, 37), fill=GRID)
    paint_text(image, (10, 22), "FAVORITES" if filer.show_favorites else "FILER",
               11, TEXT, mono=True)
    position = f"{selection + 1} / {len(entries)}" if entries else "0 / 0"
    paint_text(image, (308, 24), position, 9, LILAC, mono=True, anchor="rt")
    if filer.show_favorites:
        location = "Saved tracks"
    else:
        location = filer.folder.relative_to(ROOT).as_posix() if filer.folder != ROOT else "MDX Library"
    paint_text(image, (10, 37), fit(location, 10, 294), 10, LILAC)
    start = min(max(0, selection - ROWS // 2), max(0, len(entries) - ROWS))
    if not entries:
        empty = "Press V on a track to save it here" if filer.show_favorites else "No MDX tracks"
        paint_text(image, (10, 56), empty, 12, DIM)
    for row, path in enumerate(entries[start:start + ROWS]):
        y = 50 + row * 14
        current = start + row == selection
        if current:
            draw.rectangle((7, y, 312, y + 13), fill=GRID)
        directory = path.is_dir()
        marker = "+" if directory else "*" if path in filer.favorites else ""
        paint_text(image, (12, y + 2), marker, 9, AMBER, mono=True)
        name = path.name if directory else path.stem
        if filer.show_favorites:
            name = f"{name} / {path.parent.name}"
        paint_text(image, (26, y - 1), fit(name, 12, 283), 12, TEXT if current else DIM)


def paint_exit_dialog(image, progress):
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((44, 54, 275, 115), radius=5, fill=PANEL, outline=LILAC)
    paint_text(image, (160, 64), "Hold Esc to exit", 13, TEXT, mono=True, anchor="mt")
    paint_text(image, (160, 83), "Release to cancel", 9, DIM, mono=True, anchor="mt")
    draw.rectangle((60, 100, 259, 105), fill=GRID)
    if progress:
        draw.rectangle((60, 100, 59 + 2 * progress, 105), fill=LILAC)


def render(filer, playback, mode, volume, offset, exit_progress=None):
    image = Image.new("RGB", (320, 170), BG)
    paint_player(image, playback, mode, volume, playback.track in filer.favorites, offset)
    if filer.active:
        paint_filer(image, filer)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 157, 319, 169), fill=PANEL)
    hint = ("F/X Move  V Fav  B List  Z Back  ESC Close" if filer.active else
            "Q Play W/E Skip A/S/D Vol Tab L Mode M Audio Hold Esc")
    paint_text(image, (8, 158), hint, 9, LILAC, mono=True)
    if exit_progress is not None:
        paint_exit_dialog(image, exit_progress)
    return image


def run(duration, play):
    filer = Filer()
    playback = Playback()
    restore_last_track(filer, playback)
    remembered = (playback.track, playback.duration_ms)
    mode_index, level, muted = restore_player_settings()
    started = time.monotonic()
    last_frame = None
    esc_started = None
    try:
        with lcd_and_keyboard() as (lcd, keyboard):
            if play:
                playback.start(play.resolve())
            while not duration or time.monotonic() - started < duration:
                for key, pressed in read_key_events(keyboard, 0.045):
                    key = MEDIA_KEYS.get(key, key)
                    if key == KEY_ESC:
                        now = time.monotonic()
                        if pressed:
                            if esc_started is None:
                                esc_started = now
                        elif esc_started is not None:
                            if now - esc_started >= ESC_HOLD_SECONDS:
                                return
                            filer.close()
                            esc_started = None
                    elif not pressed:
                        if key is None:
                            esc_started = None
                        filer.release_move(key)
                    elif key in (KEY_TAB, KEY_B):
                        filer.toggle(show_favorites=key == KEY_B)
                    elif key in VOLUME_KEYS:
                        audio_command(*VOLUME_KEYS[key])
                        level, muted = read_system_volume()
                        if key != KEY_MUTE:
                            save_player_settings(level, PLAY_MODES[mode_index])
                    elif key == KEY_PLAYPAUSE:
                        if playback.track is not None:
                            playback.play_pause()
                        elif filer.active and filer.selected and filer.selected.is_file():
                            playback.start(filer.selected)
                            filer.close()
                        elif not filer.active:
                            filer.toggle(False)
                    elif key == KEY_L:
                        mode_index = (mode_index + 1) % len(PLAY_MODES)
                        save_player_settings(level, PLAY_MODES[mode_index])
                    elif key == KEY_M:
                        playback.cycle_audio_mode(time.monotonic())
                    elif key == KEY_V:
                        filer.toggle_favorite(filer.selected if filer.active else playback.track)
                    elif key in (KEY_PREVIOUSSONG, KEY_NEXTSONG):
                        step = 1 if key == KEY_NEXTSONG else -1
                        track = adjacent_track(playback.track, step, loop=PLAY_MODES[mode_index] == "LOOP DIR")
                        if track:
                            playback.start(track)
                    elif not filer.active:
                        continue
                    elif key in (KEY_LEFT, KEY_Z):
                        filer.back()
                    elif key in FILER_MOVES:
                        filer.press_move(FILER_MOVES[key], time.monotonic())
                    elif filer.selected and key in (KEY_ENTER, KEY_RIGHT, KEY_C):
                        selected = filer.selected
                        if selected.is_dir():
                            filer.open_folder(selected)
                        elif key == KEY_ENTER:
                            playback.start(selected)
                            filer.close()
                now = time.monotonic()
                esc_elapsed = None if esc_started is None else now - esc_started
                if esc_elapsed is not None and esc_elapsed >= ESC_HOLD_SECONDS:
                    return
                exit_progress = None if esc_elapsed is None else int(esc_elapsed * 100 / ESC_HOLD_SECONDS)
                filer.repeat(now)
                playback.update_audio_mode(now)
                previous_status = playback.status
                playback.update()
                mode = PLAY_MODES[mode_index]
                if previous_status != "DONE" and playback.status == "DONE":
                    track = track_after(playback.track, mode)
                    if track:
                        playback.start(track)
                track = playback.track
                record = (track, playback.duration_ms)
                if (track is not None and playback.status == "PLAY" and playback.engine in ("FM", "FM + PCM")
                        and record != remembered and track.is_relative_to(ROOT)):
                    write_json(LAST_TRACK_FILE, {"track": track.relative_to(ROOT).as_posix(),
                                                "title": playback.title,
                                                "duration_ms": playback.duration_ms})
                    remembered = record
                volume = "MUTED" if muted else "VOL --" if level is None else f"VOL {level}%"
                title, _ = title_and_credit(playback.title)
                offset = title_scroll_offset(title_strip(title).width - TITLE_WIDTH, playback.elapsed_ms)
                frame = (filer.folder, filer.selection, filer.active, filer.show_favorites,
                         filer.favorites, playback.status, playback.track, playback.title,
                         playback.engine, playback.audio_label, volume, mode,
                         playback.elapsed_ms // 1000, playback.duration_ms,
                         0 if filer.active else offset, () if filer.active else playback.meters,
                         exit_progress)
                if frame != last_frame:
                    draw_lcd(lcd, render(filer, playback, mode, volume, offset, exit_progress))
                    last_frame = frame
    finally:
        playback.stop()


def make_icon(path):
    icon = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((1, 1, 62, 62), radius=12, fill=PANEL, outline=GRID, width=2)
    draw.rectangle((8, 13, 55, 50), outline=GRID, width=1)
    for index, level in enumerate((7, 14, 21, 26, 22, 14, 8)):
        icon.paste(meter_column(5, 28, level, min(28, level + 3)), (12 + index * 6, 18))
    draw.line((12, 54, 51, 54), fill=LILAC)
    icon.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--play", type=Path)
    options = parser.parse_args()
    if options.icon:
        make_icon(options.icon)
    else:
        run(options.duration, options.play)


if __name__ == "__main__":
    main()
