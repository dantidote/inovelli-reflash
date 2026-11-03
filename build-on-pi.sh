#!/usr/bin/env bash
# Root-free OpenOCD build for a Pi where sudo needs a password.
# Input: ~/openocd-bootstrapped.tgz (OpenOCD master with ./bootstrap already run elsewhere).
# Output: ~/openocd-s2/bin/openocd + ~/openocd-s2/share/openocd/scripts
# Needs only gcc, make, pkg-config (present on Raspberry Pi OS). USB adapters and
# linuxgpiod are left out (no dev headers); bcm2835gpio covers Pi 1-4 via /dev/gpiomem.
set -euo pipefail

PREFIX="$HOME/openocd-s2"
TGZ="$HOME/openocd-bootstrapped.tgz"
SRC="$HOME/openocd"

[ -f "$TGZ" ] || { echo "missing $TGZ"; exit 1; }
rm -rf "$SRC"
tar xzf "$TGZ" -C "$HOME"
cd "$SRC"
test -f tcl/target/silabs/xg21.cfg || { echo "no xg21.cfg in tree"; exit 1; }

./configure --prefix="$PREFIX" --enable-internal-jimtcl --enable-bcm2835gpio --disable-werror \
  --disable-jlink --disable-ftdi --disable-cmsis-dap --disable-stlink \
  > configure.log 2>&1 || { tail -30 configure.log; exit 1; }
grep -E "bcm2835gpio|Linux GPIOD" config.log | head -2 || true
make -j"$(nproc)" MAKEINFO=true > make.log 2>&1 || { tail -40 make.log; exit 1; }
make install-exec MAKEINFO=true > install.log 2>&1
mkdir -p "$PREFIX/share/openocd"
rm -rf "$PREFIX/share/openocd/scripts"
cp -r tcl "$PREFIX/share/openocd/scripts"

echo
"$PREFIX/bin/openocd" --version 2>&1 | head -1
ls "$PREFIX/share/openocd/scripts/target/silabs/"
echo "OK: $PREFIX"
