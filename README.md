# Inovelli VZM31-SN bootloader reflash from a Raspberry Pi

Some Inovelli Blue 2-1 switches (VZM31-SN, 2022 production, firmware 2.18) cannot take the
3.04 OTA: the transfer reaches 100%, the switch reboots, and it still reports 2.18. Inovelli's
fix is a one-time SWD reflash of two images with a J-Link and Simplicity Commander. This repo
does the same thing with OpenOCD driving a Raspberry Pi's GPIO pins as the SWD probe, so no
J-Link is needed. 19 switches were done this way in one evening.

## What is actually wrong

Inovelli's Scribe guide ("How to Update Manually via Harness on Inovelli 2-1 Blue Series
Switch") flashes, in order:

1. `1. VM_SWITCH_buttonmode-signed_V2.00.s37` (application 2.00)
2. `2. VZM31_BootloaderV1_01_20230214.s37` (the new bootloader)

Both are in Inovelli's `VZM31-SN_2.0_Bootloader.zip` (35 MB, mostly a bundled Windows
Simplicity Commander). The images are not included here; download the zip from Inovelli.

The radio module is an `EFR32MG21A020F768IM32`. Before writing, Commander performs a silent
device unlock (the "you may see a warning, click OK" step): these units ship with the Series 2
**debug lock** set. The only way past it is the Secure Engine *device erase*, which wipes main
flash (old bootloader and Zigbee network data included) and clears the lock. That lock is the
real "locked bootloader". With the lock present OpenOCD sees a valid SWD ID (`DPIDR 0x6ba02477`)
but cannot examine the core, and the SE status reads `Debug lock (hw status): Enabled`.
Afterwards the switch runs 2.00 with the new bootloader and takes the 3.04 OTA normally.

## Why OpenOCD works now

Silicon Labs contributed Series 2 (EFR32xG21 and later) support to OpenOCD master, merged
2026-04-26: `tcl/target/silabs/xg21.cfg`, the efm32 flash driver with the `silabs_s2` loader,
and the DCI commands `silabs_s2_dci_read_se_status` and `silabs_s2_dci_device_erase`. It is in
no release, distro package or xpack build yet, so `pi-setup.sh` builds master from source.
OpenOCD reads the .s37 files natively.

## Parts

- Any 40-pin Raspberry Pi. Tested on a Pi 4B with Raspberry Pi OS (Trixie). A Pi 5 is routed
  to OpenOCD's `raspberrypi5-gpiod.cfg` by the script but has not been tested.
- Four female-to-female jumper wires.
- Something to reach the switch's 1.27 mm debug pads. Used here: a Samtec DWM-08-55-L-S-380
  header (Mouser 200-DWM0855LS380; 1.27 mm pitch, long 0.380 in pins, use 5 of the 8 positions)
  plus the passive 2.54 mm to 1.27 mm 10-pin adapter board sold on AliExpress as the CC Debugger
  "downloader cable" (a 2x5 IDC cable on each side of a tiny PCB), which gets from Pi jumper
  wires down to the 1.27 mm connector. Inovelli's own harness works too if you have one.

## Wiring

Switch pads: a single row of 5 plated holes at 1.27 mm pitch, reachable through a slot in the
front plate with the switch still in the wall. The square pad is pin 1.

| Switch pad | Signal | Pi header pin | BCM GPIO |
|-----------:|--------|--------------:|---------:|
| 1 (square) | 3V3    | 1             | 3V3 rail |
| 2          | SWDIO  | 24            | GPIO 8   |
| 3          | SWCLK  | 23            | GPIO 11  |
| 4          | RST    | not connected | (SYSRESETREQ is used) |
| 5          | GND    | 20            | GND      |

These GPIOs are what OpenOCD's `interface/raspberrypi-native.cfg` expects, so nothing needs
remapping. Keep the leads short. The Pi 3V3 pin powers the switch logic; nothing else is powered.

## Safety

The switch power supply is **not isolated from mains**. Breaker OFF before touching the pads,
confirm dead with a tester, then pull the air gap and remove the cover. Never connect the Pi
to a switch that has line voltage. Disconnect the Pi before restoring the breaker.

## Build OpenOCD on the Pi

```bash
git clone https://github.com/dantidote/inovelli-reflash.git
cd inovelli-reflash
./pi-setup.sh        # apt deps, clone OpenOCD master, build, sudo make install to /opt/openocd-s2 (about 10 min on a Pi 4)
mkdir -p ~/inovelli-fw   # unzip the two .s37 files from Inovelli's zip into here
```

## Flash a switch

```bash
./flash-inovelli.sh probe   # connect only: prints the SE/debug-lock status and flash info
./flash-inovelli.sh full    # unlock (SE device erase), write app + bootloader, verify, reset
```

`full` takes about 15 seconds; with the wiring prepared it is about a minute per switch.
Other modes: `unlock`, `flash` (write + verify only; chip must already be unlocked), `bl`
(bootloader only). Every run is logged next to the firmware files. The script uses `sudo`
only if your user cannot open `/dev/gpiomem`.

On first use the script creates `2. VZM31_BootloaderV1_01_20230214.aligned.s37` with
`align_srec.py`: the stock bootloader image has a section starting at 0xBE286, and OpenOCD's
efm32 driver refuses writes that are not 4-byte aligned. The copy pads that section down to
0xBE284 with 0xFF (sections become 0x0-0x2810 and 0xBE284-0xBE38C, 184 bytes of padding, every
original byte preserved), which programs identically on erased flash. The application image
needs no change.

### Troubleshooting

- `Error connecting DP: cannot read IDR`: no electrical contact. Check ground first. With the Pi
  idle, `pinctrl get 8,11` should show GPIO 8 (SWDIO) high and GPIO 11 (SWCLK) low; both high
  usually means no common ground.
- `Examination failed` together with `Debug lock (hw status): Enabled` on `probe` is the normal
  locked state; `full` handles it.
- `Failed to write memory at 0x200032d8` (or nearby) on the first write right after the unlock:
  the core is in lockup on an empty vector table. `full` retries the flash step once by itself;
  it happened on 2 of 20 runs and the retry always worked.
- An interrupted write does not brick anything. SWD keeps working; run `flash` again.

## After the reflash

The switch boots 2.00 factory-fresh and looks for a network, so open permit-join. It keeps its
IEEE address, so Zigbee2MQTT and Home Assistant keep the same device, name and entities. Then:

1. Re-interview it in Z2M (Z2M caches genBasic and keeps showing 2.18 until you do).
2. Run the 3.04 OTA as usual (about 35 minutes each on a CC2652 coordinator).
3. Once it is on 3.04, run **Reconfigure** in Z2M: the erase also wiped the reporting bindings.
   `configure` fails on 2.00 (it reads attributes that only exist in newer firmware) and
   succeeds on 3.04.

A fresh switch only searches for a network for a few minutes. If it missed the window,
triple-tap the config button (LED bar pulses blue) or power-cycle it with permit-join open.

`z2m_batch_ota.py` does all of that unattended over the Z2M frontend websocket (set `Z2M_URL`,
default `ws://localhost:8080/api`; needs `pip install websockets`) for every VZM31-SN not yet
on 3.04, or only the names given: keeps permit-join open for up to 45 minutes while any target
is missing, probes every 2 minutes, re-interviews on first contact, queues the 2.00 ones for
OTA (4 at a time by default), retries a failed OTA twice, and runs `configure` after each
finish. It logs a STATUS line every 5 minutes and a `STILL ON 2.18` line for any switch whose
reflash did not take. `z2m_rejoin_ota.py "<name>"` and `z2m_watch_ota.py "<name>"` are the
single-switch versions. Zigbee OTAs make the LED bars blink and end in a reboot, so run the
batch in the daytime.

## Alternative probes

With a Pi Pico flashed as a CMSIS-DAP `debugprobe`, pyOCD works from any PC:
`pip install pyocd bincopy`, `pyocd pack install EFR32MG21A020F768IM32`, convert each .s37 to
.hex with `bincopy convert -i srec -o ihex`, then `pyocd load -t efr32mg21a020f768im32`. Same
wiring. The unlock step still needs a tool that speaks the Series 2 DCI, which is why OpenOCD
master is used here.

## Files

- `pi-setup.sh`: build OpenOCD master with the Series 2 support on the Pi (needs sudo).
- `build-on-pi.sh`: root-free variant; expects a tarball of OpenOCD master with `./bootstrap`
  already run elsewhere.
- `flash-inovelli.sh`: probe / unlock / flash / bl / full.
- `align_srec.py`: 4-byte-aligns an S-record image with 0xFF padding and verifies the result.
- `z2m_batch_ota.py`, `z2m_rejoin_ota.py`, `z2m_watch_ota.py`: the Zigbee2MQTT side.
- `forum-post.md`: the write-up posted on the Inovelli community forum.

MIT licensed. Not affiliated with Inovelli; the firmware images are theirs.
