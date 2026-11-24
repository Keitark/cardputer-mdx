#!/usr/bin/env python3
"""Check audio routing and volume independently of attached hardware."""

from concurrent.futures import Future
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


BUILTIN = "alsa_output.platform-sound.stereo-fallback"
BLUETOOTH = "bluez_output.test.1"
STEREO = "front-left,front-right"
BLUETOOTH_NODE = "87"


def check_pulse_output(player):
    devices = {
        "sinks": [
            {"index": 10, "name": BUILTIN, "channel_map": STEREO,
             "properties": {"object.id": "invalid"}},
            {"index": 20, "name": BLUETOOTH, "channel_map": STEREO,
             "properties": {"object.id": BLUETOOTH_NODE}},
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
        assert player.pulse_output(456) == player.AudioOutput(BLUETOOTH, STEREO, BLUETOOTH_NODE)
        assert player.pulse_output(123) == player.AudioOutput(BUILTIN, STEREO)
        assert player.pulse_output(None) == player.AudioOutput(BUILTIN)
        assert player.pulse_output(789) == player.AudioOutput(BUILTIN)
    with patch.object(player.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("pactl", 0.35)):
        assert player.pulse_output(123) is None


def check_route_refresh(player):
    routes = player.AudioRoute()
    try:
        routes.output = player.AudioOutput(BUILTIN, STEREO)
        routes.volume = (60, False)
        routes.query = Future()
        routes.query.set_result((player.AudioOutput(BLUETOOTH, STEREO), (25, True)))
        routes.query_pid = 456
        with patch.object(routes.worker, "submit", return_value=Future()):
            assert routes.update(1, 123) == player.AudioOutput(BUILTIN, STEREO)
            assert routes.query_pid == 123
            assert routes.volume == (60, False)
        routes.query.set_result((None, None))
        assert routes.update(1.1, 123) == player.AudioOutput(BUILTIN, STEREO)
        assert routes.volume == (60, False)
    finally:
        routes.close()


def check_mix_modes(player):
    route = Mock()
    route.update.return_value = player.AudioOutput(BUILTIN, STEREO)
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
        route.update.return_value = player.AudioOutput(BLUETOOTH, STEREO)
        playback.update_audio_mode(3)
        assert playback.audio_label == "AUTO STEREO"
        route.update.assert_called_with(3, 456)
        playback.cycle_audio_mode(4)
        assert playback.audio_label == "MONO"
        playback.cycle_audio_mode(5)
        assert playback.audio_label == "STEREO"
        route.update.return_value = player.AudioOutput(BLUETOOTH, "mono")
        playback.cycle_audio_mode(6)
        assert playback.audio_label == "AUTO MONO"
        route.update.return_value = player.AudioOutput("alsa_output.usb-audio.analog-stereo", STEREO)
        playback.update_audio_mode(7)
        assert playback.audio_label == "AUTO STEREO"
        route.update.return_value = player.AudioOutput(BUILTIN, STEREO)
        playback.update_audio_mode(8)
        assert playback.audio_label == "AUTO MONO"


def check_volume_snapshot_reads_the_playback_output(player):
    output = player.AudioOutput(BLUETOOTH, STEREO, BLUETOOTH_NODE)
    with patch.object(player, "pulse_output", return_value=output) as resolve, \
            patch.object(player, "audio_command", return_value="Volume: 0.82 [MUTED]\n") as command:
        assert player.audio_output_state(456) == (output, (82, True))
        resolve.assert_called_once_with(456)
        command.assert_called_once_with("get-volume", BLUETOOTH_NODE)


def check_external_volume_refreshes_in_manual_mix_modes(player):
    playback = player.Playback()
    playback.audio = SimpleNamespace(pid=456)
    routes = playback.audio_route
    output = player.AudioOutput(BLUETOOTH, STEREO, BLUETOOTH_NODE)
    try:
        for index, (mode, volume) in enumerate((("MONO", (82, False)), ("STEREO", (37, True)))):
            playback.audio_mode = mode
            routes.query = Future()
            routes.query.set_result((output, volume))
            routes.query_pid = 456
            routes.poll_at = 60
            playback.update_audio_mode(1 + index)
            assert playback.audio_label == mode
            assert playback.mono == (mode == "MONO")
            assert routes.volume == volume
    finally:
        routes.close()


def check_volume_keys_target_the_playback_output(player):
    routes = player.AudioRoute()
    routes.output = player.AudioOutput(BLUETOOTH, STEREO, BLUETOOTH_NODE)
    routes.output_pid = 456
    try:
        for key, expected in (
            (player.KEY_A, ("set-mute", BLUETOOTH_NODE, "toggle")),
            (player.KEY_S, ("set-volume", "-l", "1.0", BLUETOOTH_NODE, "5%-")),
            (player.KEY_D, ("set-volume", "-l", "1.0", BLUETOOTH_NODE, "5%+")),
        ):
            with patch.object(player, "audio_command", side_effect=("", "Volume: 0.63")) as command:
                assert routes.change_volume(player.MEDIA_KEYS[key], 456) == (63, False)
                assert command.call_args_list == [call(*expected), call("get-volume", BLUETOOTH_NODE)]
                assert routes.volume == (63, False)

        with patch.object(player, "audio_command", side_effect=("", "Volume: 0.28")) as command:
            assert routes.change_volume(player.KEY_VOLUMEUP, 789) == (28, False)
            assert command.call_args_list == [call("set-volume", "-l", "1.0", player.SINK, "5%+"),
                                              call("get-volume", player.SINK)]
    finally:
        routes.close()


def check_key_change_discards_an_in_flight_volume_read(player):
    routes = player.AudioRoute()
    output = player.AudioOutput(BLUETOOTH, STEREO, BLUETOOTH_NODE)
    routes.output, routes.output_pid = output, 456
    routes.volume = (40, False)
    stale = Future()
    assert stale.set_running_or_notify_cancel()
    routes.query, routes.query_pid, routes.poll_at = stale, 456, 60
    try:
        with patch.object(player, "audio_command", side_effect=("", "Volume: 0.45")):
            assert routes.change_volume(player.KEY_VOLUMEUP, 456) == (45, False)
        stale.set_result((output, (40, False)))
        fresh = Future()
        with patch.object(routes.worker, "submit", return_value=fresh) as submit:
            routes.update(1, 456)
            submit.assert_called_once_with(player.audio_output_state, 456)
        assert routes.volume == (45, False)
        fresh.set_result((output, (48, True)))
        routes.update(1.1, 456)
        assert routes.volume == (48, True)
    finally:
        routes.close()


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
    check_volume_snapshot_reads_the_playback_output(player)
    check_external_volume_refreshes_in_manual_mix_modes(player)
    check_volume_keys_target_the_playback_output(player)
    check_key_change_discards_an_in_flight_volume_read(player)
    print("Audio routing checks passed.")
