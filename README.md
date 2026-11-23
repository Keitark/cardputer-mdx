# MDX Player for CardputerZero

Play Sharp X68000 MDX music on the CardputerZero. The player includes a note
spectrum, FM and PCM part meters, a library browser, favorites, and automatic
audio routing for the built-in speaker and wired headphones.

## Install and play

1. Download the Arm64 Debian package from the release and copy it to your
   CardputerZero.
2. Run `sudo apt install ./cardputer-mdx_0.1.0-1_arm64.deb` on the device.
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

Auto starts in mono on the built-in speaker and follows the wired headphone
jack for stereo. Changing the output mode keeps the current playback position.
Playback settings and favorites are stored under
`~/.local/share/cardputer-mdx/` by default.

## Build the package

On Debian 13 Arm64, including the CardputerZero itself, install the build tools
and run the build from the source tree.

```sh
sudo apt install build-essential binutils dpkg
sh ./build-deb.sh
```

The package is written to `dist/cardputer-mdx_0.1.0-1_arm64.deb`. Pass a
directory as the first argument to change the output location. Set
`SOURCE_DATE_EPOCH` to the release commit timestamp to fix package timestamps.
The build uses only the source in this tree and the installed system tools.

APPLaunch uses the included desktop entry and icon. The player, display module,
and decoder are installed together under `/usr/lib/cardputer-mdx/`.

## Source and license

The application and the FM-1 meter code use GPL-3.0-or-later. The decoder
statically links [mdxmini](https://github.com/mistydemeo/mdxmini) at
`003531a471c1955f4ed4357d0e2a6cba809c34a0`, distributed by its authors under
GPL-2.0-or-later. The [license](LICENSE), [packaging notices](packaging/copyright),
[mdxmini authors](vendor/mdxmini/AUTHORS), and [mdxmini license](vendor/mdxmini/COPYING)
are included with the corresponding source.
