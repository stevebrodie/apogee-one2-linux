#!/usr/bin/env python3
"""
Apogee ONEv2 Linux Keepalive Daemon
====================================
Solves two problems with the Apogee ONEv2 on Linux:

1. The device requires a proprietary vendor USB init sequence before
   the UAC2 clock becomes valid and audio can flow. This sequence was
   discovered via Wireshark USB capture on macOS.

2. The device has a hardware watchdog that disconnects it from USB
   every ~9 seconds unless a specific vendor command (0x29) is sent
   to reset it. This daemon sends that command every 4 seconds from
   a background thread, so audio playback is not interrupted.

Usage:
    sudo python3 apogee-one-keepalive.py

Requirements:
    pip install pyusb  (or: sudo apt install python3-usb)

The daemon runs continuously, reinitialising the device if it
disconnects. Run as root (required for USB unbind operations).
"""

import glob
import time
import subprocess
import threading

import usb.core

# USB timeout in milliseconds
TIMEOUT = 200


def log(msg):
    print(msg, flush=True)


def find_and_unbind():
    """Unbind the ONEv2 from the kernel USB driver so libusb can claim it."""
    for vf in glob.glob('/sys/bus/usb/devices/*/idVendor'):
        try:
            with open(vf) as f:
                if f.read().strip() == '0c60':
                    name = vf.replace('/idVendor', '').split('/')[-1]
                    with open('/sys/bus/usb/drivers/usb/unbind', 'w') as u:
                        u.write(name)
                    return True
        except:
            pass
    return False


def wait_for_device():
    """Wait up to 30 seconds for the ONEv2 to appear on USB."""
    for _ in range(60):
        find_and_unbind()
        time.sleep(0.1)
        dev = usb.core.find(idVendor=0x0c60, idProduct=0x0017)
        if dev:
            return dev
        time.sleep(0.4)
    return None


def r(dev, req, ln, idx=0):
    """Send a vendor read control transfer."""
    try:
        return bytes(dev.ctrl_transfer(0xc0, req, 0, idx, ln, TIMEOUT))
    except:
        return None


def w(dev, req, data, idx=0):
    """Send a vendor write control transfer."""
    try:
        dev.ctrl_transfer(0x40, req, 0, idx, bytes(data), TIMEOUT)
        return True
    except:
        return False


def full_init(dev):
    """
    Send the proprietary vendor init sequence.

    This sequence was reverse-engineered via Wireshark USB packet capture
    on macOS while the Apogee Maestro app initialised the device.
    Without it, the device clock is not valid and audio cannot flow.
    """
    for req, ln in [(0x29, 6), (0x31, 4), (0x29, 6), (0x1f, 1),
                    (0x20, 1), (0x28, 3), (0x36, 1)]:
        r(dev, req, ln)
    w(dev, 0x34, [0x17])
    for req, ln in [(0x44, 1), (0x3e, 1), (0x33, 1), (0x35, 1), (0x53, 1)]:
        r(dev, req, ln)
    w(dev, 0x10, [0x00])


def module_loaded():
    """Check whether snd_usb_audio is already loaded in the kernel."""
    result = subprocess.run(['lsmod'], capture_output=True, text=True)
    return 'snd_usb_audio' in result.stdout


def heartbeat_thread(dev, stop_event, lost_event):
    """
    Send the watchdog reset command every 4 seconds.

    Runs in a background thread so audio playback is not blocked.
    The ONEv2 disconnects from USB after ~9 seconds without this command.

    The watchdog command (0x29) was isolated by binary search — testing
    subsets of the init sequence as the heartbeat until the single command
    that prevented the 9-second disconnect was found.
    """
    cycles = 0
    lost = 0
    while not stop_event.is_set():
        try:
            r(dev, 0x29, 6)
            lost = 0
            cycles += 1
            if cycles % 10 == 0:
                log(f"Heartbeat OK ({cycles} cycles, {cycles * 4}s)")
        except Exception as e:
            lost += 1
            if lost == 1:
                log(f"Heartbeat error: {e}")
            if lost > 3:
                log(f"Device lost after {cycles} heartbeat cycles")
                lost_event.set()
                return
        time.sleep(4.0)


# Main loop — runs forever, reinitialising on device loss
while True:
    log("Waiting for device...")
    dev = wait_for_device()
    if not dev:
        log("Device not found, waiting 5s...")
        time.sleep(5)
        continue

    log(f"Device found: bus {dev.bus} addr {dev.address}")
    try:
        dev.reset()
    except:
        pass
    time.sleep(1.0)

    dev = wait_for_device()
    if not dev:
        log("Device lost after reset, waiting 5s...")
        time.sleep(5)
        continue

    log(f"Device re-found after reset: bus {dev.bus} addr {dev.address}")
    try:
        dev.set_configuration()
    except:
        pass

    full_init(dev)
    log("Init complete")

    if not module_loaded():
        subprocess.run(['modprobe', '--ignore-install',
                        'snd_usb_audio', 'ignore_ctl_error=1'])
    else:
        log("snd_usb_audio already loaded, skipping modprobe")

    time.sleep(1.0)

    dev = wait_for_device()
    if not dev:
        log("Device lost after modprobe, waiting 5s...")
        time.sleep(5)
        continue

    log(f"Device ready for keepalive: bus {dev.bus} addr {dev.address}")

    stop_event = threading.Event()
    lost_event = threading.Event()
    hb = threading.Thread(
        target=heartbeat_thread,
        args=(dev, stop_event, lost_event),
        daemon=True
    )
    hb.start()

    # Wait until the heartbeat thread reports device loss
    lost_event.wait()
    stop_event.set()
    hb.join(timeout=5)

    log("Sleeping 5s before retry...")
    time.sleep(5)
