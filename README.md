# Apogee ONEv2 on Linux

Reverse-engineered Linux support for the **Apogee ONEv2** USB audio interface (USB VID:PID `0c60:0017`).

This project documents everything needed to make the ONEv2 work on Linux, including two kernel patches, a keepalive daemon that solves the hardware watchdog disconnect, and the full vendor USB init sequence discovered via Wireshark packet capture on macOS.

**Nothing here existed before this project. All of it was built from scratch.**

---

## Current Status

| Feature | Status |
|---------|--------|
| Device enumerates on USB | ✅ Working |
| ALSA sees both Playback and Capture PCMs | ✅ Working |
| Capture (recording) | ✅ Working |
| Hardware watchdog keepalive | ✅ Working (9,000+ cycles verified) |
| Playback PCM opens | ✅ Working (stream starts) |
| Stable playback audio | ⚠️ Stream fails mid-play — investigation ongoing |
| WirePlumber/PipeWire Sink | ⚠️ Not yet confirmed |

---

## Background

The Apogee ONEv2 is a UAC2 USB audio interface with two significant Linux incompatibilities:

### Problem 1: UAC2 Clock Validity
The Linux UAC2 driver queries the device for clock source validity before allowing enumeration. The ONEv2 returns an error to this query (it requires a proprietary vendor init sequence first), causing Linux to reject the clock source and refuse to register the PCM devices.

**Fix:** A one-line patch to `sound/usb/clock.c` that returns `true` (assume clock is valid) when the device fails to respond to clock validity queries, instead of `false` (which causes enumeration to fail).

### Problem 2: Media Controller Pad Link Crash
When the playback PCM is opened, `snd_media_stream_init()` in `sound/usb/media.c` calls `media_create_pad_link()`. On this device this call fails, and the original code uses `goto remove_intf_link` which causes the PCM open to fail entirely.

**Fix:** A one-line patch that changes `goto remove_intf_link` to `continue`, making the failed pad link non-fatal.

### Problem 3: Hardware Watchdog
The ONEv2 has a hardware watchdog that disconnects the device from USB approximately every 9 seconds unless the host sends a specific vendor USB control command (`0x29`) to reset it. Additionally, the device requires a proprietary vendor init sequence (discovered via Wireshark USB capture on macOS) before the clock becomes valid and audio can flow.

**Fix:** A Python keepalive daemon that sends the vendor init sequence on startup and resets the watchdog every 4 seconds from a background thread, so audio playback is not interrupted.

---

## What You Need

- Linux system with kernel 6.x (tested on 6.14 and 6.17)
- Kernel headers for your running kernel
- Kernel source tree matching your running kernel
- `python3-usb` package
- `zstd` package

---

## Kernel Patches

### Patch 1: `sound/usb/clock.c`

```diff
--- a/sound/usb/clock.c
+++ b/sound/usb/clock.c
@@ -276,7 +276,7 @@ static bool uac_clock_source_is_valid(struct snd_usb_audio *chip,
        if (err < 0) {
                dev_warn(&dev->dev,
                         "%s(): cannot get clock validity for id %d\n",
                           __func__, source_id);
-               return false;
+               return true;
        }
```

**Where to apply:** Find the `if (err < 0)` block inside `uac_clock_source_is_valid()` following the `dev_warn` about clock validity. The line number varies by kernel version — use `grep -n "return false" sound/usb/clock.c` to find it.

**Why:** The ONEv2 cannot respond to clock validity queries before receiving the vendor init sequence. Returning `false` causes the kernel to reject the device entirely. Returning `true` allows enumeration to proceed, and the keepalive daemon then sends the init sequence that makes the clock actually valid.

### Patch 2: `sound/usb/media.c`

```diff
--- a/sound/usb/media.c
+++ b/sound/usb/media.c
@@ -95,7 +95,7 @@ int snd_media_stream_init(struct snd_usb_substream *subs, struct snd_pcm *pcm,
                        ret = media_create_pad_link(entity, mixer_pad,
                                                    &mctl->media_entity, 0,
                                                    MEDIA_LNK_FL_ENABLED);
                        if (ret)
-                               goto remove_intf_link;
+                               continue;
```

**Where to apply:** Find the `if (ret)` block after `media_create_pad_link` inside `snd_media_stream_init()`.

**Why:** On this device `media_create_pad_link` fails. The original `goto remove_intf_link` tears down the entire PCM open sequence, preventing any playback. Using `continue` makes the failure non-fatal.

**Note:** This patch produces a harmless compiler warning: `label 'remove_intf_link' defined but not used`. This is expected.

---

## Building the Patched Module

### Step 1: Get kernel source

```bash
# Enable deb-src in /etc/apt/sources.list, then:
sudo apt update
cd ~
apt-get source linux-hwe-$(uname -r | cut -d'.' -f1,2)
```

### Step 2: Copy Module.symvers from installed headers

```bash
cp /usr/src/linux-headers-$(uname -r)/Module.symvers ~/linux-hwe-*/Module.symvers
```

### Step 3: Apply both patches

Find the exact line numbers in your source tree:
```bash
grep -n "return false" ~/linux-hwe-*/sound/usb/clock.c
grep -n "goto remove_intf_link" ~/linux-hwe-*/sound/usb/media.c
```

Apply clock.c patch (replace NN with the line number found above):
```bash
sed -i 'NNs/\t\treturn false;/\t\treturn true;/' ~/linux-hwe-*/sound/usb/clock.c
```

Apply media.c patch:
```bash
sed -i 's/\t\t\t\tgoto remove_intf_link;/\t\t\t\tcontinue;/' ~/linux-hwe-*/sound/usb/media.c
```

### Step 4: Build

**Critical:** Use the exact compiler that built your running kernel. Check with `cat /proc/version`. On Ubuntu/Zorin this is typically `x86_64-linux-gnu-gcc-13`.

```bash
touch ~/linux-hwe-*/sound/usb/*.c

make -C /usr/src/linux-headers-$(uname -r) \
     M=~/linux-hwe-*/sound/usb \
     modules -j$(nproc) \
     CC=x86_64-linux-gnu-gcc-13
```

You should see a full wall of `CC [M]` lines. If make exits immediately with no output, the `touch` step was skipped.

### Step 5: Install

**Do NOT use `make modules_install`** — it installs to `/updates/` which takes load priority and can cause symbol version mismatches.

```bash
KVER=$(uname -r)
SOURCE=~/linux-hwe-*/sound/usb
DEST=/lib/modules/$KVER/kernel/sound/usb

sudo zstd -f $SOURCE/snd-usb-audio.ko -o $DEST/snd-usb-audio.ko.zst
sudo zstd -f $SOURCE/snd-usbmidi-lib.ko -o $DEST/snd-usbmidi-lib.ko.zst
sudo depmod -a
```

### Step 6: Set ignore_ctl_error permanently

```bash
echo "options snd_usb_audio ignore_ctl_error=1" | sudo tee /etc/modprobe.d/apogee-one.conf
```

---

## Keepalive Daemon

The keepalive daemon (`apogee-one-keepalive.py`) does three things:

1. Unbinds the device from the kernel USB driver
2. Sends the proprietary vendor init sequence via libusb
3. Runs a background thread that sends a `0x29` vendor read every 4 seconds to reset the hardware watchdog

Without the keepalive, the device disconnects from USB every ~9 seconds.

### Installation

```bash
sudo apt install python3-usb

sudo cp apogee-one-keepalive.py /usr/local/bin/
sudo chmod +x /usr/local/bin/apogee-one-keepalive.py
sudo cp apogee-one.service /etc/systemd/system/
sudo systemctl daemon-reload
```

**Test manually first before enabling as a boot service:**
```bash
sudo python3 /usr/local/bin/apogee-one-keepalive.py
```

Wait for "Device ready for keepalive" and confirm the heartbeat runs. Then:
```bash
sudo systemctl enable --now apogee-one
```

### Service file (`apogee-one.service`)

```ini
[Unit]
Description=Apogee ONEv2 Init and Keepalive
After=sound.target

[Service]
Type=simple
ExecStart=/usr/local/bin/apogee-one-keepalive.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

---

## WirePlumber Configuration

Without this config, WirePlumber repeatedly attempts to use the ALSA Control Protocol (ACP/mixer) on the ONEv2, which it doesn't support, causing repeated errors and preventing stable operation.

Create `~/.config/wireplumber/main.lua.d/50-apogee-one.lua`:

```lua
rule = {
  matches = {
    {
      { "api.alsa.card.name", "matches", "*ONEv2*" },
    },
  },
  apply_properties = {
    ["api.alsa.use-acp"] = false,
    ["api.alsa.open.ucm"] = false,
    ["device.profile"] = "on",
    ["session.suspend-timeout-seconds"] = 0,
  },
}
table.insert(alsa_monitor.rules, rule)
```

---

## How the Vendor Init Sequence Was Discovered

The init sequence was reverse-engineered by capturing USB traffic with Wireshark on macOS while the Apogee Maestro app initialised the device. The capture was filtered for `URB_CONTROL` vendor-type transfers to/from the device address, and the sequence of `bRequest` values and payloads was extracted.

The watchdog command (`0x29`) was isolated by binary search — testing subsets of the init sequence as the heartbeat until the single command that prevented the 9-second disconnect was identified.

---

## Known Issues and Open Questions

- **Playback stream fails mid-play** — the PCM opens successfully and audio begins, but the stream drops with "File descriptor in bad state" or "No such device". Root cause not yet confirmed. May be related to USB address reassignment when `wait_for_device()` re-unbinds the device after modprobe.
- **The `.zst.bak` backup is not the original stock module** — if you need to restore the stock module, use `sudo apt install --reinstall linux-modules-extra-$(uname -r)`
- **Both modules must come from the same build** — `snd_usb_audio` and `snd_usbmidi_lib` must be loaded from the same compiled tree. Mixing stock and custom versions causes symbol CRC mismatches.

---

## Contributions and Next Steps

If you can get stable playback working, please open an issue or PR. The two kernel patches are candidates for upstream submission to the ALSA maintainer (Takashi Iwai). If accepted, they would fix the ONEv2 for all Linux users without requiring any manual kernel patching.

The vendor init sequence and watchdog command could potentially be added as a quirk in the upstream `snd_usb_audio` driver, eliminating the need for the keepalive daemon entirely.

---

## Related Projects

- [take_control](https://github.com/stefanocoding/take_control) — Python control script for the Apogee Duet USB (VID `0c60:0016`), which shares the same vendor command architecture as the ONEv2
- The ALSA USB audio driver source: `sound/usb/` in the Linux kernel tree

---

## Hardware

Tested on:
- **Device:** Apogee ONEv2 (USB VID:PID `0c60:0017`, bcdDevice 1.05)
- **System:** ThinkPad T410, Intel Core i5 M520
- **OS:** Zorin OS 18 (Ubuntu 24.04 base), kernel 6.14.0-36-generic
- **Also tested:** Linux Mint (kernel 6.17.0-22-generic) — module build issues encountered due to compiler mismatch; Zorin recommended
