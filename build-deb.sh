#!/bin/sh
set -eu
umask 022

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination=${1:-"$root/dist"}
package=cardputer-mdx
version=0.2.3-1
maintainer='Keitark <33802890+Keitark@users.noreply.github.com>'

if [ "$(dpkg --print-architecture)" != arm64 ]; then
    printf '%s\n' 'Build on Debian 13 arm64 or on a CardputerZero.' >&2
    exit 1
fi

mkdir -p -- "$destination"
temp_root=${TMPDIR:-/tmp}
if [ ! -d "$temp_root" ]; then
    temp_root=/tmp
fi
TMPDIR=$temp_root
export TMPDIR
temporary=$(mktemp -d "$temp_root/cardputer-mdx.XXXXXXXX")
trap 'rm -rf -- "$temporary"' EXIT
trap 'exit 1' HUP INT TERM

stage="$temporary/package"
app="$stage/usr/lib/$package"
launcher="$stage/usr/share/APPLaunch"
doc="$stage/usr/share/doc/$package"
mkdir -p "$app" "$launcher/applications" "$launcher/share/images" "$doc" "$stage/DEBIAN"

meters="$root/vendor/fm1-meters"
player="$root/vendor/fm1-mdx"
retrofm="$root/vendor/retrofm"
mdxtools="$root/vendor/mdxtools"
gcc -std=c11 -O2 -c "$meters/fm1_meters.c" -o "$temporary/fm1_meters.o"
gcc -std=c11 -O2 -Wall -Wextra -Werror -I"$player" -I"$meters" -I"$retrofm" -I"$mdxtools" \
    -c "$root/mdx-render.c" -o "$temporary/mdx-render.o"
gcc -std=c11 -O2 -I"$player" -I"$meters" -I"$retrofm" -I"$mdxtools" \
    "$temporary/mdx-render.o" "$player/fm1_mdx.c" "$player/fm1_mdx_mix.c" "$temporary/fm1_meters.o" \
    "$retrofm"/*.c "$mdxtools"/*.c \
    -lm -o "$app/mdx-render"
strip --strip-unneeded "$app/mdx-render"

install -m 755 "$root/mdx-player.py" "$app/"
install -m 644 "$root/cardputer_display.py" "$app/"
install -m 644 "$root/packaging/$package.desktop" "$launcher/applications/"
install -m 644 "$root/packaging/$package.png" "$launcher/share/images/"
install -m 644 "$root/README.md" "$root/LICENSE" "$root/packaging/copyright" "$doc/"
install -m 644 "$retrofm/COPYING" "$doc/retrofm-COPYING"
install -m 644 "$mdxtools/LICENSE" "$doc/mdxtools-LICENSE"

libc_version=$(objdump -T "$app/mdx-render" |
    sed -nE 's/.*GLIBC_([0-9.]+).*/\1/p' | sort -Vu | tail -n 1)
installed_size=$(du -sk "$stage" | cut -f1)
cat >"$stage/DEBIAN/control" <<EOF
Package: $package
Version: $version
Section: sound
Priority: optional
Architecture: arm64
Maintainer: $maintainer
Installed-Size: $installed_size
Depends: libc6 (>= $libc_version), python3 (>= 3.10), python3-numpy, python3-pil, fonts-dejavu-mono, fonts-noto-cjk, pulseaudio-utils, wireplumber
Homepage: https://github.com/Keitark/cardputer-mdx
Description: X68000 MDX/PDX music player for CardputerZero
 Browse a local MDX/PDX library with FM and PCM part meters, a note spectrum,
 favorites, and automatic mono or stereo playback for the active audio output.
EOF

artifact="$destination/${package}_${version}_arm64.deb"
dpkg-deb --root-owner-group --build "$stage" "$artifact"
