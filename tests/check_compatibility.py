#!/usr/bin/env python3
"""Check RetroFM compatibility with locally generated MDX and PDX fixtures."""

import argparse
from pathlib import Path
import struct
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1]
END_TRACK = b"\xf1\x00"


def mdx_file(pdx_name, tracks, voices=b""):
    offset = 2 * (len(tracks) + 1)
    offsets = []
    for track in tracks:
        offsets.append(offset)
        offset += len(track)
    pointers = struct.pack(f">{len(offsets) + 1}H", offset, *offsets)
    return (b"Compatibility test\r\n\x1a" + pdx_name + b"\0" + pointers +
            b"".join(tracks) + voices)


def render(renderer, track):
    result = subprocess.run([renderer, track], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace").splitlines()[:3]
    assert result.stdout and len(result.stdout) % 4096 == 0
    duration = subprocess.check_output([renderer, "--duration", track], stderr=subprocess.PIPE,
                                       timeout=5)
    assert int(duration) == len(result.stdout) * 1000 // (4 * 44100)
    return result.stdout


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("renderer", type=lambda path: Path(path).resolve())
    renderer = cli.parse_args().renderer
    demo = (SOURCE / "demo" / "Prism Steps.mdx").read_bytes()
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        track = folder / "test.mdx"
        track.write_bytes(demo)
        original = render(renderer, track)
        assert any(original), "The FM demo should be audible"
        for delimiter in (b"\xff", b"\x1a"):
            track.write_bytes(demo + delimiter)
            assert render(renderer, track) == original, "A voice delimiter changed the audio"
        track.write_bytes(demo + b"Composer")
        assert render(renderer, track) == original, "A trailing credit changed the audio"

        data_start = demo.index(b"\r\n\x1a\0") + 4
        voice_start = data_start + struct.unpack_from(">H", demo, data_start)[0]
        voice = demo[voice_start:voice_start + 27]
        first_note = b"\xfd\x00\xfc\x03\xfb\x0d\xff\xd7\xbc\x30"
        second_note = b"\xbe\x30" + END_TRACK
        empty_tracks = [END_TRACK] * 8
        track.write_bytes(mdx_file(b"", [first_note + second_note, *empty_tracks], voice))
        expected = render(renderer, track)
        assert any(expected), "The test voice should be audible"
        missing_voice = first_note + b"\xfd\xfe" + second_note
        track.write_bytes(mdx_file(b"", [missing_voice, *empty_tracks], voice))
        assert render(renderer, track) == expected, "An undefined voice should keep the current patch"
        alternate_voice = voice[:1] + bytes((voice[1] ^ 7,)) + voice[2:]
        track.write_bytes(mdx_file(b"", [first_note + second_note, *empty_tracks],
                                   voice + alternate_voice))
        assert render(renderer, track) == expected, "Duplicate voice IDs should use the first patch"
        track.write_bytes(mdx_file(b"", [first_note + second_note + b"Unreachable", *empty_tracks],
                                   voice))
        assert render(renderer, track) == expected, "Bytes after the end command changed the audio"
        extended_lfo = b"\xec\x06\x00\x0c\x00\x03"
        track.write_bytes(mdx_file(b"", [first_note + extended_lfo + second_note, *empty_tracks],
                                   voice))
        modulated = render(renderer, track)
        assert len(modulated) == len(expected) and modulated != expected
        buffered_sync = b"\xef\x00\xee"
        track.write_bytes(mdx_file(b"", [first_note + buffered_sync + second_note, *empty_tracks],
                                   voice))
        assert render(renderer, track) == expected, "Sync sent before waiting should be saved"

        sample = b"\x77\xff" * 256
        bank_header = struct.pack(">II", 768, len(sample)) + bytes(760)
        (folder / "bAnK.pDx").write_bytes(bank_header + sample)
        pcm_tracks = [*empty_tracks, b"\xfb\x0f\xfc\x03\x80\xbf" + END_TRACK]
        track.write_bytes(mdx_file(b"BANK.PDX", pcm_tracks))
        assert any(render(renderer, track)), "A PCM-only file should play with a matching bank"

        pcm8_tracks = [b"\xe8" + END_TRACK, *pcm_tracks[1:], *([END_TRACK] * 7)]
        track.write_bytes(mdx_file(b"BANK.PDX", pcm8_tracks))
        expected = render(renderer, track)
        assert any(expected), "PCM8 should play from the first bank"
        multi_bank_header = bytes(768) + struct.pack(">II", 1536, len(sample)) + bytes(760)
        (folder / "bAnK.pDx").write_bytes(multi_bank_header + sample)
        pcm8_tracks[8] = b"\xfd\x01" + pcm8_tracks[8]
        track.write_bytes(mdx_file(b"BANK.PDX", pcm8_tracks))
        assert render(renderer, track) == expected, "PCM8 should play notes from the selected bank"

        track.write_bytes(mdx_file(b"", pcm_tracks))
        assert not any(render(renderer, track)), "PCM with no bank should be silent"

        track.write_bytes(demo + b"\x12")
        invalid = subprocess.run([renderer, track], capture_output=True, timeout=5)
        assert invalid.returncode != 0 and not invalid.stdout, "Malformed voice data was accepted"
    print("MDX voices, PCM-only and multi-bank playback, and malformed voice data passed")


if __name__ == "__main__":
    main()
