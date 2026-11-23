#!/bin/sh
set -eu
umask 022

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination=${1:-"$root/dist"}
package=cardputer-mdx
version=0.1.0-1
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

build="$temporary/mdxmini"
mkdir -p "$build/obj"
cp -R "$root/vendor/mdxmini/." "$build/"
make -C "$build" -f Makefile.lib

stage="$temporary/package"
app="$stage/usr/lib/$package"
launcher="$stage/usr/share/APPLaunch"
doc="$stage/usr/share/doc/$package"
mkdir -p "$app" "$launcher/applications" "$launcher/share/images" "$doc" "$stage/DEBIAN"

meters="$root/vendor/fm1-meters"
gcc -O2 -c "$meters/fm1_meters.c" -o "$temporary/fm1_meters.o"
gcc -O2 -Wall -Wextra -Werror -I"$build/src" -I"$meters" \
    "$root/mdx-render.c" "$temporary/fm1_meters.o" "$build/obj/libmdxmini.a" \
    -Wl,--wrap=YM2151WriteReg,--wrap=pcm8_note_on,--wrap=pcm8_note_off,--wrap=fopen \
    -lm -o "$app/mdx-render"
strip --strip-unneeded "$app/mdx-render"

install -m 755 "$root/mdx-player.py" "$app/"
install -m 644 "$root/cardputer_display.py" "$app/"
install -m 644 "$root/packaging/$package.desktop" "$launcher/applications/"
install -m 644 "$root/packaging/$package.png" "$launcher/share/images/"
install -m 644 "$root/README.md" "$root/LICENSE" "$root/packaging/copyright" "$doc/"
install -m 644 "$build/AUTHORS" "$doc/mdxmini-AUTHORS"
install -m 644 "$build/COPYING" "$doc/mdxmini-COPYING"

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
 favorites, and automatic mono speaker and stereo headphone playback.
EOF

artifact="$destination/${package}_${version}_arm64.deb"
dpkg-deb --root-owner-group --build "$stage" "$artifact"
