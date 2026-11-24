#!/usr/bin/env python3
"""Check Bluetooth playback keys without Bluetooth hardware."""

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch


SOURCE = Path(__file__).resolve().parents[1]


@contextmanager
def keyboard_pipe():
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb", buffering=0) as keyboard:
        try:
            yield keyboard, writer
        finally:
            os.close(writer)


@contextmanager
def input_directories(display):
    with TemporaryDirectory() as temporary:
        input_class = Path(temporary) / "class"
        input_devices = Path(temporary) / "devices"
        input_class.mkdir()
        input_devices.mkdir()
        with patch.multiple(display, INPUT_CLASS=input_class, INPUT_DEVICES=input_devices):
            yield input_class, input_devices


def make_input(input_class, input_devices, event, name):
    name_file = input_class / event / "device" / "name"
    name_file.parent.mkdir(parents=True, exist_ok=True)
    name_file.write_text(name + "\n", encoding="utf-8")
    device = input_devices / event
    os.mkfifo(device)
    return name_file, device


def key(display, code, value=1):
    return display.INPUT_EVENT.pack(0, 0, 1, code, value)


def check_keyboard_without_bluetooth(display):
    with keyboard_pipe() as (keyboard, writer):
        dropped = display.INPUT_EVENT.pack(0, 0, 0, 3, 0)
        os.write(writer, key(display, display.KEY_ESC) + key(display, display.KEY_ESC, 0) + dropped)
        assert display.read_key_events(keyboard, 0) == [(display.KEY_ESC, 1), (display.KEY_ESC, 0), (None, 0)]


def check_only_headset_playback_presses_are_multiplexed(display):
    with input_directories(display) as directories, keyboard_pipe() as (keyboard, writer):
        _, headset = make_input(*directories, "event5", "Test earbuds (AVRCP)")
        _, unrelated = make_input(*directories, "event6", "External keyboard")
        with os.fdopen(os.open(headset, os.O_RDWR | os.O_NONBLOCK), "wb", buffering=0) as remote, \
                os.fdopen(os.open(unrelated, os.O_RDWR | os.O_NONBLOCK), "wb", buffering=0) as other, \
                display.BluetoothMediaInput() as bluetooth:
            playback_keys = (display.KEY_PLAYCD, display.KEY_PLAY, display.KEY_PAUSECD,
                             display.KEY_PAUSE, display.KEY_PLAYPAUSE, display.KEY_NEXTSONG,
                             display.KEY_PREVIOUSSONG)
            os.write(writer, key(display, display.KEY_Q))
            remote.write(b"".join(key(display, code) for code in playback_keys))
            remote.write(key(display, display.KEY_NEXTSONG, 0) + key(display, display.KEY_NEXTSONG, 2)
                         + key(display, display.KEY_VOLUMEUP) + key(display, display.KEY_VOLUMEDOWN)
                         + key(display, display.KEY_MUTE) + key(display, 208) + key(display, display.KEY_ESC))
            other.write(key(display, display.KEY_NEXTSONG))
            events = display.read_key_events(keyboard, 0, bluetooth=bluetooth)
            assert events == [(display.KEY_Q, 1)] + [(code, 1) for code in playback_keys]


def check_reconnect_with_reused_event_number(display):
    with input_directories(display) as directories, keyboard_pipe() as (keyboard, writer), \
            patch.object(display.time, "monotonic", return_value=100.0) as clock, \
            display.BluetoothMediaInput() as bluetooth:
        assert display.read_key_events(keyboard, 0, bluetooth=bluetooth) == []
        name_file, headset = make_input(*directories, "event5", "Test earbuds (AVRCP)")
        with os.fdopen(os.open(headset, os.O_RDWR | os.O_NONBLOCK), "wb", buffering=0) as remote:
            remote.write(key(display, display.KEY_NEXTSONG))
            clock.return_value += 2
            assert display.read_key_events(keyboard, 0, bluetooth=bluetooth) == [(display.KEY_NEXTSONG, 1)]

        assert display.read_key_events(keyboard, 0, bluetooth=bluetooth) == []
        name_file.unlink()
        headset.unlink()
        os.write(writer, key(display, display.KEY_W))
        clock.return_value += 2
        assert display.read_key_events(keyboard, 0, bluetooth=bluetooth) == [(display.KEY_W, 1)]

        _, reconnected = make_input(*directories, "event5", "Test earbuds (AVRCP)")
        with os.fdopen(os.open(reconnected, os.O_RDWR | os.O_NONBLOCK), "wb", buffering=0) as remote:
            remote.write(key(display, display.KEY_PREVIOUSSONG))
            clock.return_value += 2
            assert display.read_key_events(keyboard, 0, bluetooth=bluetooth) == [(display.KEY_PREVIOUSSONG, 1)]


def check_keyboard_works_when_headset_input_is_missing(display):
    with input_directories(display) as directories, keyboard_pipe() as (keyboard, writer), \
            display.BluetoothMediaInput() as bluetooth:
        _, headset = make_input(*directories, "event5", "Test earbuds (AVRCP)")
        headset.unlink()
        os.write(writer, key(display, display.KEY_E))
        assert display.read_key_events(keyboard, 0, bluetooth=bluetooth) == [(display.KEY_E, 1)]


def check_explicit_play_pause_is_idempotent(player):
    with patch.object(player, "AudioRoute"):
        playback = player.Playback()
    playback.audio = Mock(poll=Mock(return_value=None))
    playback.decoder = Mock(poll=Mock(return_value=None))
    playback.status = "PLAY"

    playback.play_pause(pause=False)
    assert playback.status == "PLAY"
    playback.play_pause(pause=True)
    playback.play_pause(pause=True)
    assert playback.status == "PAUSED"
    playback.play_pause(pause=False)
    playback.play_pause(pause=False)
    assert playback.status == "PLAY"
    playback.play_pause()
    assert playback.status == "PAUSED"
    playback.play_pause()
    assert playback.status == "PLAY"
    signals = [call(signal.SIGSTOP), call(signal.SIGCONT), call(signal.SIGSTOP), call(signal.SIGCONT)]
    assert playback.audio.send_signal.call_args_list == signals
    assert playback.decoder.send_signal.call_args_list == signals


def check_explicit_pause_never_starts_a_stopped_track(player):
    with patch.object(player, "AudioRoute"):
        playback = player.Playback()
    playback.track = Path("last.mdx")
    with patch.object(playback, "start") as start:
        playback.play_pause(pause=True)
        start.assert_not_called()
        playback.play_pause(pause=False)
        start.assert_called_once_with(playback.track)


if __name__ == "__main__":
    sys.path.insert(0, str(SOURCE))
    import cardputer_display as display

    spec = importlib.util.spec_from_file_location("mdx_player", SOURCE / "mdx-player.py")
    player = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(player)
    check_keyboard_without_bluetooth(display)
    check_only_headset_playback_presses_are_multiplexed(display)
    check_reconnect_with_reused_event_number(display)
    check_keyboard_works_when_headset_input_is_missing(display)
    check_explicit_play_pause_is_idempotent(player)
    check_explicit_pause_never_starts_a_stopped_track(player)
    print("Bluetooth media-key checks passed.")
