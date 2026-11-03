**Title:** Fixing "locked bootloader" Blue 2-1 switches (OTA hits 100% but stays on 2.18) with a Raspberry Pi instead of a J-Link

**Why this post exists:** if you have more than a handful of the 2022 "locked bootloader" switches, the official fix means waiting on one of the few loaner J-Link programmers support has to pass around, or buying a J-Link yourself, which costs several hundred dollars for a tool you will use once. A Raspberry Pi you probably already own, four jumper wires and a couple of dollars in pins do exactly the same job. I did my 19 switches in one evening this way and did not touch a J-Link.

**TL;DR:** The 2022-production VZM31-SN that cannot take the 3.04 OTA have the EFR32's factory *debug lock* set. Inovelli's harness procedure clears it with a J-Link and Simplicity Commander. The same thing works with the Pi's GPIO pins as the SWD probe and current OpenOCD, which gained Silicon Labs Series 2 support this spring. Scripts and a longer README: https://github.com/dantidote/inovelli-reflash

## The symptom and the actual cause

Z2M (or ZHA) pushes 3.04, the transfer reaches 100%, the switch reboots, and it still reports 2.18. Support calls it a locked bootloader and offers the harness/J-Link procedure (the Scribe guide "How to Update Manually via Harness on Inovelli 2-1 Blue Series Switch").

What that procedure really does: Commander first performs a silent device unlock (the "you may see a warning, click OK" step). On the EFR32MG21 that is a Secure Engine *device erase*: it wipes main flash, including the old bootloader and the Zigbee network data, and clears the debug lock. Then it flashes two images:

1. `1. VM_SWITCH_buttonmode-signed_V2.00.s37` (application 2.00)
2. `2. VZM31_BootloaderV1_01_20230214.s37` (the new bootloader)

Both come from Inovelli's zip (`VZM31-SN_2.0_Bootloader.zip`, 35 MB, most of it a bundled Windows Commander). After that the switch runs 2.00 with the new bootloader and the normal 3.04 OTA applies.

You can see the lock yourself: with the factory state OpenOCD gets a valid SWD ID (`DPIDR 0x6ba02477`) but cannot examine the core, and the SE status reads `Debug lock (hw status): Enabled`. Any switch that shows that will never apply 3.04 over the air, no matter how many times you retry.

## What you need

- Any 40-pin Raspberry Pi (I used a Pi 4B; a Pi 5 needs OpenOCD's `raspberrypi5-gpiod.cfg` instead, untested here)
- 4 female jumper wires
- Pins for the switch's 1.27 mm debug pads. I used a Samtec DWM-08-55-L-S-380 header (Mouser 200-DWM0855LS380): 1.27 mm pitch with long 0.380 in pins, which reach the board through the slot in the front plate; use 5 of the 8 positions.
- To get from the Pi's 2.54 mm jumper wires down to 1.27 mm: the passive adapter sold on AliExpress as the CC Debugger "downloader cable" (a tiny PCB with a 2x5 2.54 mm IDC cable on one side and a 2x5 1.27 mm IDC cable on the other). Inovelli's own harness works too if you have one.
- OpenOCD built from git master. The Series 2 support (`target/silabs/xg21.cfg`, the efm32 flash driver update and the DCI unlock commands, contributed by Silicon Labs) was merged in April 2026 and is **not** in any release, distro package or xpack build yet.

Apart from the Pi, that is under $10 of parts, and nothing in the chain is single-purpose.

Build on the Pi (about 10 minutes on a Pi 4). `pi-setup.sh` in the repo does exactly this:

```bash
sudo apt install git build-essential autoconf automake libtool pkg-config libusb-1.0-0-dev
git clone https://github.com/openocd-org/openocd.git && cd openocd
./bootstrap
./configure --enable-bcm2835gpio --enable-linuxgpiod --enable-internal-jimtcl --disable-werror
make -j4 && sudo make install
```

## Wiring

The debug pads are a single row of five, 1.27 mm pitch, square pad = pin 1.

| Switch pad | Function | Pi header pin |
|---|---|---|
| 1 (square) | 3V3 | 1 (3V3) |
| 2 | SWDIO | 24 (GPIO 8) |
| 3 | SWCLK | 23 (GPIO 11) |
| 4 | RESET | not used |
| 5 | GND | 20 (GND) |

Those GPIOs are what OpenOCD's `interface/raspberrypi-native.cfg` expects, so no pin remapping is needed.

**Safety:** breaker OFF and the air-gap pulled before touching the pads. The switch's power supply is not isolated from the mains. With the breaker off, the Pi's 3V3 pin powers the radio module through pad 1, which is exactly how the J-Link harness does it too.

## Procedure per switch

With the repo it is `./flash-inovelli.sh probe` to check contact, then `./flash-inovelli.sh full`. What `full` runs, if you would rather do it by hand:

```bash
APP="1. VM_SWITCH_buttonmode-signed_V2.00.s37"
BL="2. VZM31_BootloaderV1_01_20230214.aligned.s37"   # see the alignment note below
common=(-f interface/raspberrypi-native.cfg -c "transport select swd" -c "set WORKAREASIZE 0x8000"
        -f target/silabs/xg21.cfg -c "adapter speed 500")

# 1. contact check and lock state
sudo openocd "${common[@]}" -c init -c silabs_s2_dci_read_se_status -c shutdown

# 2. unlock: SE device erase (wipes flash, clears the debug lock, the SE resets the chip itself)
sudo openocd "${common[@]}" -c init -c silabs_s2_dci_device_erase -c "sleep 1500" \
        -c silabs_s2_dci_read_se_status -c shutdown      # now prints "Debug lock (hw status): Disabled"

# 3. write and verify both images, then let it boot
sudo openocd "${common[@]}" -c init -c "reset halt" \
        -c "flash write_image erase {$APP} 0 s19" -c "verify_image {$APP} 0 s19" \
        -c "flash write_image erase {$BL} 0 s19"  -c "verify_image {$BL} 0 s19" \
        -c "reset run" -c shutdown
```

Step 3 takes about 12 seconds (303 KB app at 500 kHz bit-bang, then the bootloader). Unplug, close the switch up, breaker on. With the wiring done ahead of time it is about a minute per switch at the Pi; 18 switches took me 47 minutes.

**Alignment note:** the stock bootloader .s37 has a second section starting at 0xBE286. OpenOCD's efm32 driver requires 4-byte aligned writes and refuses it (`addr 0xbe286 breaks required 4-byte alignment`). The fix is to re-emit the S-records with that section padded down to 0xBE284 with 0xFF; on freshly erased flash the result is identical. `align_srec.py` in the repo does that (and verifies every original byte survived); `flash-inovelli.sh` calls it automatically. The application image needs no change.

## After the reflash

- The switch boots 2.00 factory-fresh and goes looking for a network, so open permit-join. It keeps its IEEE address, so Z2M and Home Assistant keep the same device, name and entities.
- Z2M will still display 2.18 until you re-interview the device (Z2M caches genBasic).
- Run the 3.04 OTA as usual (about 35 minutes each on my CC2652 coordinator).
- When it is on 3.04, run **Reconfigure** in Z2M. The erase also wiped the reporting bindings, and `configure` fails on 2.00 (it reads attributes that only exist in newer firmware), so do it after the OTA.
- A fresh switch only searches for a network for a few minutes. If you missed the window, triple-tap the config button (LED bar pulses blue) or power-cycle it with permit-join open.
- Doing many: `z2m_batch_ota.py` in the repo handles the rejoin, re-interview, OTA queue and reconfigure for all of them over the Z2M websocket. The OTAs blink the LED bars and end in a reboot, so do that part in the daytime.

## Troubleshooting

- `Error connecting DP: cannot read IDR`: no electrical contact. Check ground first; with the Pi idle, `pinctrl get 8,11` should show GPIO 8 high and GPIO 11 low. Both high usually means no common ground.
- `Examination failed` plus `Debug lock (hw status): Enabled` before the unlock is normal; that is the locked switch.
- `Failed to write memory at 0x200032d8` (or similar) on the first write right after the unlock: the core is sitting in lockup on an empty vector table. Just run step 3 again; it happened on 2 of my 20 runs and the retry always worked (the script now does it on its own).
- An interrupted write does not brick anything. SWD keeps working and you can run step 3 again.

Credits: Inovelli for publishing the images and the Scribe procedure, and the Silicon Labs engineers who upstreamed Series 2 support into OpenOCD, which is what makes this possible without their proprietary tools.
