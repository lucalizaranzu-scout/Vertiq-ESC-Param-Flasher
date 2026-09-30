# ESC Flasher

Flashes Vertiq 4006 ESC profiles over DroneCAN through a Zubax Babel USB-CAN adapter.
For each drone, the operator turns each motor by hand so the tool learns which ESC sits
at which position (M1–M4). The tool then writes that position's profile, saves, restarts
the ESCs and reads everything back to verify it. A spin test afterwards checks each
motor's direction.

Runs on native Linux and on WSL2.

## Requirements

- Python 3.9+ with Tk
  - Debian/Ubuntu: `sudo apt install python3 python3-tk python3-venv`
  - Fedora: `sudo dnf install python3 python3-tkinter`
  - Arch: `sudo pacman -S python tk`
- [uv](https://docs.astral.sh/uv/) (optional). `run.sh` uses it when present, otherwise `python3 -m venv` + pip.
- A Zubax Babel (USB `1d50:60c7`)

## Setup

### Native Linux

Give your user access to the Babel. Either install the udev rule (recommended; it also
stops ModemManager from probing the adapter):

```bash
sudo cp udev/99-zubax-babel.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

or add yourself to the `dialout` group (`sudo usermod -aG dialout $USER`) and log out and back in.

### WSL2

Install [usbipd-win](https://github.com/dorssel/usbipd-win) on Windows (`winget install usbipd`).
The tool attaches the Babel to WSL by itself. The first time, Windows shows one admin
prompt to share the device.

## Usage

```bash
./run.sh                    # GUI; builds .venv on first run
./run.sh --positions M2     # only flash some positions
./run.sh --port /dev/ttyACM1
```

Console version, and a raw CAN traffic dump for checking the adapter and bus:

```bash
.venv/bin/python esc_flash/flasher.py --dry-run
.venv/bin/python probe.py --bitrate 500000
```

## Layout

| Path | Contents |
| --- | --- |
| `esc_flash/gui.py` | Tk operator GUI |
| `esc_flash/flasher.py` | Flashing logic, and a console front end |
| `esc_flash/adapter.py` | Finds the Babel; attaches it through usbipd under WSL |
| `CAN_ESC_profiles/` | IQ Control Center profile exports, one per position (`M<n>` in the file name) |
| `probe.py` | Dumps raw CAN traffic |
| `udev/` | udev rule for native Linux |

Only the profile settings the ESC exposes over DroneCAN are written (`PARAM_MAP` in
`flasher.py`). The GUI shows how many profile settings it cannot apply.
