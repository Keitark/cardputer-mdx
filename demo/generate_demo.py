# SPDX-License-Identifier: CC0-1.0
"""Generate the original eight-bar stereo FM demo, Prism Steps."""

from pathlib import Path
from struct import pack


TITLE = b"Prism Steps / By Cardputer MDX"
END = b"\xf1\x00"
SIXTEENTH = 12

# MIDI pitches ordered as bass, lower harmony, upper harmony and color tone.
CHORDS = (
    (38, 53, 60, 64),  # Dm9
    (44, 55, 60, 62),  # Abmaj7(#11)
    (41, 57, 60, 62),  # F6
    (40, 55, 59, 64),  # Em
    (41, 57, 64, 67),  # Fmaj9
    (46, 57, 62, 64),  # Bbmaj7(#11)
    (43, 55, 58, 65),  # Gm7
    (45, 55, 61, 70),  # A7(b9)
)

# Each bar contains 16 sixteenth notes. None denotes a rest.
MELODY = (
    ((74, 3), (77, 2), (None, 1), (76, 2), (81, 3), (79, 1), (77, 4)),
    ((72, 2), (74, 2), (79, 3), (None, 1), (75, 2), (74, 2), (72, 4)),
    ((69, 2), (74, 3), (72, 1), (69, 2), (79, 2), (76, 2), (74, 4)),
    ((71, 3), (76, 1), (77, 2), (76, 4), (None, 2), (67, 4)),
    ((69, 2), (76, 2), (79, 3), (81, 1), (84, 3), (83, 1), (79, 4)),
    ((77, 3), (74, 1), (76, 2), (81, 2), (79, 2), (76, 1), (74, 5)),
    ((70, 3), (None, 1), (77, 2), (79, 2), (82, 3), (81, 1), (77, 4)),
    ((76, 2), (73, 2), (70, 3), (67, 1), (73, 2), (76, 2), (74, 4)),
)


def voice(number, algorithm, mult, level, attack, decay, sustain, release):
    """Build a YM2151 voice with the four slot parameter rows used by MDX."""
    return bytes((number, algorithm, 0x0F, *mult, *level, *attack,
                  *decay, *sustain, *release))


def track(bars, program, pan, volume, gate, tempo=None):
    stream = bytearray((0xFD, program, 0xFC, pan, 0xFB, volume, 0xF8, gate))
    if tempo is not None:
        stream.extend((0xFF, tempo))
    for bar in bars:
        assert sum(duration for _, duration in bar) == 16
        for pitch, duration in bar:
            clocks = duration * SIXTEENTH
            if pitch is None:
                while clocks:
                    rest = min(clocks, 128)
                    stream.append(rest - 1)
                    clocks -= rest
            else:
                assert 12 <= pitch <= 107 and 1 <= clocks <= 256
                stream.extend((0x80 + pitch - 12, clocks - 1))
    return bytes(stream) + END


def make_demo():
    counter = [((None, 1), (low + 12, 2), (mid + 12, 2), (low + 12, 1),
                (color + 12, 2), (None, 1), (mid + 12, 2), (low + 12, 1), (mid + 12, 4))
               for _, low, mid, color in CHORDS]
    bass = [((root, 5), (root + 12, 1), (None, 2), (root + 7, 3), (root, 5))
            for root, _, _, _ in CHORDS]
    pad_low = [((low, 8), (low, 8)) for _, low, _, _ in CHORDS]
    pad_high = [((high, 8), (color, 8)) for _, _, high, color in CHORDS]
    pulse = [((36, 1), (None, 5), (36, 1), (None, 1),
              (36, 1), (None, 5), (36, 1), (None, 1))] * len(CHORDS)
    tracks = (
        track(MELODY, 0, 2, 13, 7, tempo=215),
        track(counter, 1, 1, 12, 5),
        track(bass, 2, 3, 13, 7),
        track(pad_low, 3, 1, 10, 8),
        track(pad_high, 3, 2, 10, 8),
        track(pulse, 4, 3, 10, 4),
        END, END, END,
    )
    voices = b"".join((
        voice(0, 0x24, (1, 2, 1, 3), (35, 52, 5, 23),
              (30, 26, 31, 27), (16, 12, 7, 9), (7, 5, 3, 5), (0x55, 0x66, 0x46, 0x56)),
        voice(1, 0x1C, (3, 2, 1, 1), (25, 42, 9, 23),
              (31, 31, 31, 30), (23, 20, 13, 14), (9, 11, 6, 7), (0x77, 0x87, 0x69, 0x79)),
        voice(2, 0x14, (1, 2, 1, 1), (31, 49, 8, 25),
              (30, 29, 31, 31), (13, 15, 7, 7), (5, 6, 3, 3), (0x46, 0x57, 0x45, 0x55)),
        voice(3, 0x0F, (1, 2, 3, 1), (29, 41, 47, 21),
              (17, 15, 16, 20), (8, 7, 8, 7), (3, 5, 4, 3), (0x35, 0x46, 0x46, 0x35)),
        voice(4, 0x2C, (1, 3, 1, 2), (25, 30, 11, 16),
              (31, 31, 31, 31), (29, 31, 22, 25), (14, 13, 15, 16), (0xB9, 0xA9, 0xB8, 0x98)),
    ))
    offsets = []
    position = 2 * (len(tracks) + 1)
    for part in tracks:
        offsets.append(position)
        position += len(part)
    pointers = pack(f">{len(offsets) + 1}H", position, *offsets)
    return TITLE + b"\r\n\x1a\0" + pointers + b"".join(tracks) + voices


if __name__ == "__main__":
    Path(__file__).with_name("Prism Steps.mdx").write_bytes(make_demo())
