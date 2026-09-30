"""Open the Zubax Babel over SLCAN, print adapter info, and dump CAN traffic."""
import argparse
import os
import sys
import time

import can

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "esc_flash"))
from adapter import ensure_adapter  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--port", help="SLCAN device (default: find the Babel, attaching it under WSL)")
parser.add_argument("--bitrate", type=int, default=1_000_000)
parser.add_argument("--seconds", type=float, default=5.0)
args = parser.parse_args()
args.port = args.port or ensure_adapter()

with can.Bus(interface="slcan", channel=args.port, bitrate=args.bitrate) as bus:
    hw, sw = bus.get_version(timeout=1.0)
    print(f"Adapter on {args.port}: hw={hw} sw={sw} serial={bus.get_serial_number(timeout=1.0)}")
    print(f"Listening at {args.bitrate} bit/s for {args.seconds}s...")

    count = 0
    deadline = time.monotonic() + args.seconds
    while (remaining := deadline - time.monotonic()) > 0:
        msg = bus.recv(timeout=remaining)
        if msg is not None:
            count += 1
            print(msg)
    print(f"{count} frames received")
