# MDX Player for CardputerZero

Play Sharp X68000 MDX music on the CardputerZero. The player includes a note
spectrum, FM and PCM part meters, a library browser, favorites, and automatic
audio routing for the built-in speaker and wired headphones.
It uses the playback core from the FM-1 MDX player, with RetroFM sequencing,
PDX sample mixing, and software YM2151 synthesis at 44.1 kHz stereo.

## Install and play

1. Download the Arm64 Debian package from the release and copy it to your
   CardputerZero.
2. Run `sudo apt install ./cardputer-mdx_0.2.2-1_arm64.deb` on the device.
3. Put your MDX files in `~/Music/MDX Library`. You can organize them in
   subfolders. Keep each PDX sample bank in the same folder as the tracks
   that use it.
4. Open **MDX Player** from APPLaunch. Press **Tab** to browse, use **F/X** or
   the arrow keys to move, and press **Enter** to load a track.

Tested on the CardputerZero Debian 13 Arm64 image. Playback uses its LCD,
keyboard, headphone-jack switch and PipeWire audio session. The installation
needs administrator access. Playback runs as the signed-in device user.

| Key | Action |
| --- | --- |
| Q or Space | Play or pause |
| W / E | Previous or next track |
| A / S / D | Mute, volume down, volume up |
| M | Cycle Auto → Mono → Stereo |
| L | Cycle next track, loop track, loop folder, and single |
| Tab | Open or close the library |
| B / V | Open favorites or toggle a favorite |
| Hold Esc for 1 second | Exit |

Compatible Bluetooth headsets can play, pause, and skip tracks while MDX Player
is open. Headset volume uses the system audio controls.

Auto mixes to mono on the built-in speaker and follows the wired headphone
jack for stereo. It also follows the active Bluetooth or other external audio
output. Changing the output mode keeps the current playback position.
Playback settings and favorites are stored under
`~/.local/share/cardputer-mdx/` by default.

## Build the package

On Debian 13 Arm64, including the CardputerZero itself, install the build tools
and run the build from the source tree.

```sh
sudo apt install build-essential binutils dpkg
sh ./build-deb.sh
```

The package is written to `dist/cardputer-mdx_0.2.2-1_arm64.deb`. Pass a
directory as the first argument to change the output location. Set
`SOURCE_DATE_EPOCH` to the release commit timestamp to fix package timestamps.
The build uses only the source in this tree and the installed system tools.

APPLaunch uses the included desktop entry and icon. The player, display module,
and decoder are installed together under `/usr/lib/cardputer-mdx/`.
With the package installed, run the compatibility tests using
`python3 tests/check_compatibility.py /usr/lib/cardputer-mdx/mdx-render`.

## Source and license

The player and [FM-1 playback core](https://github.com/Keitark/fm1-mdx) use
GPL-3.0-or-later. The FM-1 core uses RetroFM at
`3aad4b3d893dcb11cf6318bd8fee18c9022eccd5` and an adapted FM driver and
YM2151 emulator from [mdxtools](https://github.com/vampirefrog/mdxtools) at
`606e3a7009aa1a9dfa6bee8bc875dbd5483714e9`. The CardputerZero build includes
compatibility changes to RetroFM for MDX files in existing libraries. The
[license](LICENSE) and [packaging notices](packaging/copyright) accompany the
corresponding source.
