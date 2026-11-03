#!/usr/bin/env bash
# Build OpenOCD from git master on a Raspberry Pi and install it to /opt/openocd-s2.
# Silicon Labs Series-2 (EFR32xG21) flash support landed on master 2026-04-26 and is
# not in any release or distro package yet, so a source build is required.
set -euo pipefail

PREFIX=/opt/openocd-s2
SRC="$HOME/openocd-src"

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  git build-essential autoconf automake libtool pkg-config texinfo \
  libusb-1.0-0-dev libgpiod-dev libhidapi-dev libftdi1-dev

if [ ! -d "$SRC/.git" ]; then
  git clone --depth 50 https://github.com/openocd-org/openocd.git "$SRC"
else
  git -C "$SRC" fetch --depth 50 origin master && git -C "$SRC" checkout -q FETCH_HEAD
fi

cd "$SRC"
git log -1 --format='Building OpenOCD %h (%cd)' --date=short
test -f tcl/target/silabs/xg21.cfg || { echo "xg21.cfg missing: this checkout predates Series-2 support"; exit 1; }

./bootstrap                      # also fetches the jimtcl submodule used below
./configure --prefix="$PREFIX" --enable-internal-jimtcl --enable-bcm2835gpio --enable-linuxgpiod --disable-werror
nice -n 19 make -j"$(nproc)"
sudo make install

echo
"$PREFIX/bin/openocd" --version
echo "OK: installed to $PREFIX"
