"""Locate the Zubax Babel's serial device. Under WSL2 the Babel is first
attached from Windows with usbipd-win (bound once with a UAC prompt, then
attached silently); on native Linux it is simply found by USB VID:PID."""
import glob
import json
import os
import shutil
import subprocess
import time

BABEL_VID, BABEL_PID = "1d50", "60c7"
USBIPD_PATHS = ["usbipd.exe", "/mnt/c/Program Files/usbipd-win/usbipd.exe"]


class AdapterError(Exception):
    pass


def running_in_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def find_tty():
    """Returns /dev/ttyACMx of the first attached Babel, or None."""
    for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        usb_dev = os.path.realpath(os.path.join(tty, "device", ".."))
        try:
            with open(os.path.join(usb_dev, "idVendor")) as v, open(os.path.join(usb_dev, "idProduct")) as p:
                if (v.read().strip(), p.read().strip()) == (BABEL_VID, BABEL_PID):
                    return "/dev/" + os.path.basename(tty)
        except OSError:
            continue
    return None


def _usbipd():
    for p in USBIPD_PATHS:
        if shutil.which(p) or os.path.exists(p):
            return p
    raise AdapterError("usbipd-win is not installed on Windows (winget install usbipd)")


def _babel_state(usbipd):
    out = subprocess.run([usbipd, "state"], capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        raise AdapterError(f"usbipd state failed: {out.stderr.strip() or out.stdout.strip()}")
    tag = f"VID_{BABEL_VID}&PID_{BABEL_PID}".upper()
    for dev in json.loads(out.stdout)["Devices"]:
        if tag in (dev.get("InstanceId") or "").upper() and dev.get("BusId"):
            return dev
    return None


def _wsl_attach(log):
    usbipd = _usbipd()
    dev = _babel_state(usbipd)
    if dev is None:
        raise AdapterError("No Zubax Babel is plugged into this PC")
    busid = dev["BusId"]
    if not dev.get("PersistedGuid"):
        log(f"Sharing Babel (bus {busid}) with WSL; approve the Windows admin prompt...")
        win_exe = subprocess.check_output(["wslpath", "-w", shutil.which(usbipd) or usbipd], text=True).strip()
        # bind is the only step needing admin, and Windows remembers it afterwards.
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"Start-Process -FilePath '{win_exe}' -ArgumentList 'bind','--busid','{busid}' "
                        f"-Verb RunAs -Wait -WindowStyle Hidden"], timeout=120)
        dev = _babel_state(usbipd)
        if not dev or not dev.get("PersistedGuid"):
            raise AdapterError("Babel was not shared; the admin prompt may have been declined")
    if not dev.get("ClientIPAddress"):
        log(f"Attaching Babel (bus {busid}) to WSL...")
        out = subprocess.run([usbipd, "attach", "--wsl", "--busid", busid], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AdapterError(f"usbipd attach failed: {(out.stderr or out.stdout).strip()}")


def ensure_adapter(log=print, timeout=15):
    """Returns the Babel's tty path, attaching it through usbipd first under WSL."""
    tty = find_tty()
    if tty is None and running_in_wsl():
        _wsl_attach(log)
        deadline = time.monotonic() + timeout
        while tty is None and time.monotonic() < deadline:
            time.sleep(0.25)
            tty = find_tty()
    if tty is None:
        raise AdapterError("Zubax Babel not found. Is it plugged in?")
    # udev may apply permissions a moment after the node appears.
    for _ in range(20):
        if os.access(tty, os.R_OK | os.W_OK):
            return tty
        time.sleep(0.1)
    if running_in_wsl():
        # Windows can run commands as root in the distro without a password, so fix
        # the device node directly rather than making the operator restart WSL.
        distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
        subprocess.run(["wsl.exe", "-d", distro, "-u", "root", "chmod", "a+rw", tty],
                       capture_output=True, timeout=30)
        if os.access(tty, os.R_OK | os.W_OK):
            return tty
    raise AdapterError(f"No permission to open {tty}. Install udev/99-zubax-babel.rules (see README), "
                       f"or run: sudo usermod -aG dialout $USER, then log out and back in "
                       f"(under WSL: wsl --shutdown)")
