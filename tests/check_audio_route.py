#!/usr/bin/env python3
"""Check the Mono/Stereo decision independently of attached audio hardware."""

from concurrent.futures import Future
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch


BUILTIN = "alsa_output.platform-sound.stereo-fallback"
BLUETOOTH = "bluez_output.test.1"
STEREO = "front-left,front-right"


def check_pulse_output(player):
    devices = {
        "sinks": [
            {"index": 10, "name": BUILTIN, "channel_map": STEREO},
            {"index": 20, "name": BLUETOOTH, "channel_map": STEREO},
        ],
        "sink_inputs": [
            {"sink": 10, "properties": {"application.process.id": "123"}},
            {"sink": 20, "properties": {"application.process.id": "456"}},
        ],
    }

    def pactl(command, **kwargs):
        output = json.dumps(devices) if "list" in command else BUILTIN + "\n"
        return SimpleNamespace(stdout=output)

    with patch.object(player.subprocess, "run", side_effect=pactl):
        assert player.pulse_output(456) == (BLUETOOTH, STEREO)
        assert player.pulse_output(123) == (BUILTIN, STEREO)
        assert player.pulse_output(None) == (BUILTIN, "")
        assert player.pulse_output(789) == (BUILTIN, "")
    with patch.object(player.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("pactl", 0.35)):
        assert player.pulse_output(123) is None


def check_route_refresh(player):
    routes = player.AudioRoute()
    try:
        routes.output = (BUILTIN, STEREO)
        routes.query = Future()
        routes.query.set_result((BLUETOOTH, STEREO))
        routes.query_pid = 456
        with patch.object(routes.worker, "submit", return_value=Future()):
            assert routes.update(1, 123) == (BUILTIN, STEREO)
            assert routes.query_pid == 123
        routes.query.set_result(None)
        assert routes.update(1.1, 123) == (BUILTIN, STEREO)
    finally:
        routes.close()


def check_mix_modes(player):
    route = Mock()
    route.update.return_value = (BUILTIN, STEREO)
    with patch.object(player, "AudioRoute", return_value=route):
        playback = player.Playback()
    playback.audio = SimpleNamespace(pid=456)
    with patch.object(player, "headphones_connected", return_value=False) as jack:
        playback.update_audio_mode(1)
        assert playback.audio_label == "AUTO MONO"
        jack.return_value = True
        playback.update_audio_mode(2)
        assert playback.audio_label == "AUTO STEREO"
        jack.return_value = False
        route.update.return_value = (BLUETOOTH, STEREO)
        playback.update_audio_mode(3)
        assert playback.audio_label == "AUTO STEREO"
        route.update.assert_called_with(3, 456)
        playback.cycle_audio_mode(4)
        assert playback.audio_label == "MONO"
        playback.cycle_audio_mode(5)
        assert playback.audio_label == "STEREO"
        route.update.return_value = (BLUETOOTH, "mono")
        playback.cycle_audio_mode(6)
        assert playback.audio_label == "AUTO MONO"
        route.update.return_value = ("alsa_output.usb-audio.analog-stereo", STEREO)
        playback.update_audio_mode(7)
        assert playback.audio_label == "AUTO STEREO"
        route.update.return_value = (BUILTIN, STEREO)
        playback.update_audio_mode(8)
        assert playback.audio_label == "AUTO MONO"


if __name__ == "__main__":
    path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (
        Path(__file__).resolve().parents[1] / "mdx-player.py")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("mdx_player", path)
    player = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(player)
    check_pulse_output(player)
    check_route_refresh(player)
    check_mix_modes(player)
    print("Audio routing checks passed.")
