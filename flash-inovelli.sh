#!/usr/bin/env bash
# Reflash an Inovelli VZM31-SN (EFR32MG21A020F768IM32) over SWD from Raspberry Pi GPIO.
# Replicates Inovelli's Simplicity Commander harness procedure with OpenOCD:
#   device unlock (SE device erase, clears the factory debug lock),
#   flash "1. VM_SWITCH_buttonmode-signed_V2.00.s37", then "2. VZM31_BootloaderV1_01_20230214.s37".
#
# Usage:  ./flash-inovelli.sh probe    # connect only, print SE/debug-lock status + flash info
#         ./flash-inovelli.sh full     # unlock + flash + verify + reset, one shot per switch
#         ./flash-inovelli.sh unlock   # SE device erase only (wipes flash, clears debug lock)
#         ./flash-inovelli.sh flash    # write app + bootloader, verify, reset (chip must be unlocked)
#         ./flash-inovelli.sh bl       # bootloader only, re-verify app, reset
#
# MAINS BREAKER OFF. The switch PSU is not isolated. Power the board from the Pi 3V3 pin only.
set -euo pipefail

OPENOCD=${OPENOCD:-$HOME/openocd-s2/bin/openocd}
[ -x "$OPENOCD" ] || OPENOCD=/opt/openocd-s2/bin/openocd
# bcm2835gpio uses /dev/gpiomem and gpiod uses /dev/gpiochipN; both are group gpio, so sudo only if needed
SUDO=""
{ [ -w /dev/gpiomem ] || [ -w /dev/gpiochip0 ]; } || SUDO=sudo
FW_DIR=${FW_DIR:-"$HOME/inovelli-fw"}
APP="$FW_DIR/1. VM_SWITCH_buttonmode-signed_V2.00.s37"
BL="$FW_DIR/2. VZM31_BootloaderV1_01_20230214.s37"
# The stock bootloader image has a section starting at 0xBE286; OpenOCD's efm32 driver needs
# 4-byte aligned writes, so use an 0xFF-padded copy (same bytes on erased flash), made on first use.
ALIGNED="${BL%.s37}.aligned.s37"
if [ ! -s "$ALIGNED" ] && [ -s "$BL" ] && [ -f "$(dirname "$0")/align_srec.py" ]; then
  python3 "$(dirname "$0")/align_srec.py" "$BL" "$ALIGNED" || exit 1
fi
[ -s "$ALIGNED" ] && BL="$ALIGNED"
LOG="$FW_DIR/flash-$(date +%Y%m%d-%H%M%S).log"

mode=${1:-probe}

model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
if [[ "$model" == *"Raspberry Pi 5"* ]]; then
  IFACE=interface/raspberrypi5-gpiod.cfg
else
  IFACE=interface/raspberrypi-native.cfg
fi

SCRIPTS="$(cd "$(dirname "$OPENOCD")/../share/openocd/scripts" 2>/dev/null && pwd || true)"
common=(
  ${SCRIPTS:+-s "$SCRIPTS"}
  -f "$IFACE"
  -c "transport select swd"
  -c "set WORKAREASIZE 0x8000"
  -f target/silabs/xg21.cfg
  -c "adapter speed 500"
)

ocd() { $SUDO "$OPENOCD" "${common[@]}" "$@" 2>&1 | tee -a "$LOG"; return "${PIPESTATUS[0]}"; }

need_images() {
  for f in "$APP" "$BL"; do
    [ -s "$f" ] || { echo "missing: $f"; exit 1; }
    head -c 2 "$f" | grep -q '^S' || { echo "not an S-record: $f"; exit 1; }
  done
}

do_probe() {
  ocd -c "init" -c "silabs_s2_dci_read_se_status" -c "reset halt" -c "flash probe 0" \
      -c "flash info 0" -c "mdw 0x0FE08000 4" -c "shutdown"
}

do_unlock() {
  # Factory units have the Series-2 debug lock set. The only way past it is the SE device
  # erase (what Commander's "unlock" prompt does): wipes main flash, clears the lock.
  # The SE resets the chip itself afterwards; the re-read status should show the lock cleared.
  ocd -c "init" -c "silabs_s2_dci_device_erase" -c "sleep 1500" \
      -c "catch {efm32.cpu arp_examine}" -c "silabs_s2_dci_read_se_status" -c "shutdown" || true
  if ! grep -q "Debug lock (hw status): Disabled" "$LOG"; then
    echo
    echo "Debug lock still reports Enabled. Power-cycle the switch (unplug the 3V3 jumper for 3 s),"
    echo "then run: $0 probe   and expect the Cortex-M33 to examine OK, then: $0 flash"
    return 1
  fi
}

do_flash() {
  need_images
  ocd -c "init" -c "reset halt" -c "flash probe 0" \
      -c "flash write_image erase {$APP} 0 s19" -c "verify_image {$APP} 0 s19" \
      -c "flash write_image erase {$BL} 0 s19"  -c "verify_image {$BL} 0 s19" \
      -c "reset run" -c "shutdown"
}

do_bl() {
  need_images
  ocd -c "init" -c "reset halt" -c "flash probe 0" \
      -c "flash write_image erase {$BL} 0 s19" -c "verify_image {$BL} 0 s19" \
      -c "verify_image {$APP} 0 s19" -c "reset run" -c "shutdown"
}

finish() {
  echo
  echo "Done. Log: $LOG"
  echo "Unplug the Pi, close the switch up, restore the breaker: it boots fw 2.00 in pairing mode."
  echo "Permit join in Zigbee2MQTT (same IEEE, entities are kept), then OTA to 3.04."
}

case "$mode" in
  probe)  do_probe ;;
  unlock) do_unlock ;;
  flash)  do_flash; finish ;;
  bl)     do_bl; finish ;;
  full)
    need_images
    do_unlock
    sleep 2
    # The core sits in lockup right after the erase (empty vector table); the write occasionally
    # fails once before "reset halt" clears it, and a second pass always went through.
    do_flash || { echo "flash failed once, retrying"; sleep 2; do_flash; }
    finish
    ;;
  *) echo "usage: $0 probe|full|unlock|flash|bl"; exit 2 ;;
esac
