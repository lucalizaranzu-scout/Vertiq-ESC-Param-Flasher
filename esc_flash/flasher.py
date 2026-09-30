"""Flashing logic for Vertiq ESCs over DroneCAN, independent of any UI.

For each drone: find the ESCs on the bus, have the operator turn each motor
by hand to learn which ESC sits at which position, write that position's
profile parameters, save, restart, then read everything back to verify.

Only parameters the ESC exposes over DroneCAN can be set (see PARAM_MAP);
the rest of an IQ Control Center profile is reported as not applied.

Run directly for a console version of the GUI:
    python flasher.py [--dry-run] [--positions M2]
"""
import argparse
import glob
import json
import multiprocessing
import os
import re
import sys
import time

import dronecan

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILES = os.path.join(HERE, "..", "CAN_ESC_profiles")

# DroneCAN parameter name -> IQ Control Center profile descriptor.
PARAM_MAP = {
    "uavcan_node_id": "DroneCAN Node ID",
    "bit_rate": "DroneCAN Bitrate",
    "esc_index": "DroneCAN ESC Index",
    "zero_behavior": "DroneCAN Zero Behavior",
    "telem_frequency": "DroneCAN Telemetry Frequency",
    "arm_with_arming_status": "Arming by DroneCAN ArmingStatus",
    "module_id": "Module ID",
    "motor_direction": "Motor Direction",
    "control_mode": "Mode",
    "max_volts": "Max Volts",
    "max_velocity": "Max Velocity",
    "communication_timeout": "Timeout",
    "arm_on_throttle": "Arm On Throttle",
    "arming_throttle_upper_limit": "Arm Throttle Upper Limit",
    "arming_throttle_lower_limit": "Arm Throttle Lower Limit",
    "disarming_throttle_upper_limit": "Disarm Throttle Upper Limit",
    "disarming_throttle_lower_limit": "Disarm Throttle Lower Limit",
    "disarm_behavior": "Disarm Behavior",
    "disarm_song_option": "Disarm Song Playback Option",
    "hold_stow": "Hold Stow",
    "stow_target_angle": "Stow Target Angle",
    "stow_target_acceleration": "Stow Target Acceleration",
}
# These change how we reach the ESC, so they are written after everything else is saved.
APPLY_ON_RESTART = ("uavcan_node_id", "bit_rate")
BITRATE_SEARCH_ORDER = (1_000_000, 500_000, 250_000, 125_000, 800_000)

# Physical place of each profile position, in the order the operator turns them (quad X).
POSITION_NAMES = {"M1": "top right", "M2": "bottom left", "M3": "top left", "M4": "bottom right"}

ESC_NAME_PREFIX = "iq_motion"
TURN_DETECT_DEGREES = 60
TURN_TIMEOUT_S = 45
param = dronecan.uavcan.protocol.param


class Cancelled(Exception):
    pass


class FlashError(Exception):
    pass


# ---------------------------------------------------------------- profiles

def load_profiles(directory):
    """Returns ({"M1": {param: value}}, [profile descriptors DroneCAN cannot set])."""
    targets, unmapped = {}, set()
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        m = re.search(r"\bM(\d+)\b", os.path.basename(path))
        if not m:
            continue
        with open(path) as f:
            entries = {e["descriptor"]: e["value"] for group in json.load(f) for e in group["Entries"]}
        unmapped |= set(entries) - set(PARAM_MAP.values())
        missing = [d for d in PARAM_MAP.values() if d not in entries]
        if missing:
            raise FlashError(f"{os.path.basename(path)} is missing {missing}")
        targets["M" + m.group(1)] = {p: entries[d] for p, d in PARAM_MAP.items()}
    if not targets:
        raise FlashError(f"No 'M<n>' profile JSON files found in {directory}")

    for key in ("uavcan_node_id", "esc_index"):
        vals = [t[key] for t in targets.values()]
        if len(set(vals)) != len(vals):
            raise FlashError(f"Profiles do not have unique {key}: {vals}")
    if len({t["bit_rate"] for t in targets.values()}) != 1:
        raise FlashError("Profiles disagree on bit_rate; the whole bus must use one bitrate")
    order = list(POSITION_NAMES) + sorted(set(targets) - set(POSITION_NAMES), key=lambda p: int(p[1:]))
    return {p: targets[p] for p in order if p in targets}, sorted(unmapped)


# ---------------------------------------------------------------- param values

def unpack_value(v):
    kind = dronecan.get_active_union_field(v)
    return kind, (None if kind == "empty" else getattr(v, kind))


def pack_value(kind, x):
    if kind == "integer_value":
        return param.Value(integer_value=int(round(x)))
    if kind == "real_value":
        return param.Value(real_value=float(x))
    if kind == "boolean_value":
        return param.Value(boolean_value=bool(x))
    raise ValueError(f"cannot write parameter of type {kind}")


def same_value(a, b):
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= 1e-5 * max(1.0, abs(float(b)))   # float32 round trip


def fmt(x):
    return f"{x:g}" if isinstance(x, float) else str(x)


# ---------------------------------------------------------------- bus session

class Session:
    def __init__(self, port, bitrate, local_id):
        # dronecan's SLCAN IO process exits when its parent PID changes; Python 3.14's
        # default "forkserver" start method makes that check fail, so use "spawn".
        if multiprocessing.get_start_method(allow_none=True) != "spawn":
            multiprocessing.set_start_method("spawn", force=True)
        self.port, self.local_id = port, local_id
        self.bitrate = None
        self.node = None
        self.set_bitrate(bitrate)

    def close(self):
        if self.node is not None:
            self.node.close()
            self.node = None

    def set_bitrate(self, bitrate):
        if bitrate != self.bitrate:
            self.close()
            self.node = dronecan.make_node(self.port, node_id=self.local_id, bitrate=bitrate)
            self.bitrate = bitrate

    def call(self, payload, dest, timeout=0.5, retries=2):
        for _ in range(retries + 1):
            box = {}
            self.node.request(payload, dest, lambda e: box.setdefault("r", e), timeout=timeout)
            while "r" not in box:
                self.node.spin(0.01)
            if box["r"] is not None:
                return box["r"].response
        return None

    def listen(self, dtype, seconds):
        """Returns [(source_node_id, message)] for one message type."""
        got = []
        h = self.node.add_handler(dtype, lambda e: got.append((e.transfer.source_node_id, e.message)))
        try:
            self.node.spin(seconds)
        finally:
            h.remove()
        return got

    def node_info(self, nid):
        return self.call(dronecan.uavcan.protocol.GetNodeInfo.Request(), nid)

    def read_params(self, nid):
        """Returns {name: (kind, value)} by walking parameter indices."""
        out = {}
        for i in range(256):
            r = self.call(param.GetSet.Request(index=i), nid)
            if r is None:
                raise FlashError(f"Node {nid} stopped answering at parameter index {i}")
            name = bytes(r.name).decode()
            if not name:
                break
            out[name] = unpack_value(r.value)
        return out

    def write_param(self, nid, name, kind, value):
        """Returns the value the ESC reports back; raises TimeoutError on no response."""
        r = self.call(param.GetSet.Request(name=name, value=pack_value(kind, value)), nid)
        if r is None:
            raise TimeoutError(f"no response writing {name}")
        return unpack_value(r.value)[1]

    def save(self, nid):
        req = param.ExecuteOpcode.Request()
        req.opcode = req.OPCODE_SAVE
        r = self.call(req, nid, timeout=1.5)
        return r is not None and r.ok

    def restart(self, nid):
        req = dronecan.uavcan.protocol.RestartNode.Request()
        req.magic_number = req.MAGIC_NUMBER
        r = self.call(req, nid, retries=0)
        return r is not None and r.ok


# ---------------------------------------------------------------- flashing

class Flasher:
    """Runs one drone through identify -> write -> verify, and spin tests.

    `ui` is called from the flashing thread and must provide:
        log(text), status(text), escs_found({nid: info}), prompt_turn(pos),
        identified(pos, nid, info), show_plan({pos: [(name, kind, old, new)]}),
        result(pos, ok, [problems]), cancelled() -> bool
    """

    def __init__(self, port, targets, ui, local_id=127, first_bitrate=None):
        self.port, self.targets, self.ui = port, targets, ui
        # Configured drones are already on the profiles' bitrate, so look there first.
        profile_bitrate = next((int(t["bit_rate"]) for t in targets.values()), BITRATE_SEARCH_ORDER[0])
        self.local_id, self.bitrate = local_id, first_bitrate or profile_bitrate

    def _check_cancel(self):
        if self.ui.cancelled():
            raise Cancelled()

    def run(self, dry_run=False):
        sess = Session(self.port, self.bitrate, self.local_id)
        try:
            return self._run(sess, dry_run)
        finally:
            self.bitrate = sess.bitrate      # start the next drone where this one ended
            sess.close()

    def _run(self, sess, dry_run):
        ui = self.ui
        ui.status("Searching for the drone's CAN bus...")
        seen = self._find_bus(sess)
        ui.log(f"Bus is at {sess.bitrate} bit/s; nodes {sorted(seen)}")
        escs = self._find_escs(sess, seen)
        ui.escs_found(escs)
        if len(escs) != len(self.targets):
            raise FlashError(f"Found {len(escs)} ESC(s) but there are {len(self.targets)} profiles "
                             f"({', '.join(self.targets)})")

        ui.status("Reading current parameters...")
        current = {}
        for nid in escs:
            self._check_cancel()
            current[nid] = sess.read_params(nid)

        mapping = self._identify(sess, escs)

        plans = {}
        for pos, nid in mapping.items():
            plans[pos] = self._plan(current[nid], self.targets[pos])
        ui.show_plan(plans)
        if dry_run:
            ui.status("Dry run complete: nothing was written.")
            return True
        if not any(plans.values()):
            for pos in mapping:
                ui.result(pos, True, [])
            ui.status("All ESCs already match their profiles.")
            return True
        self._check_cancel()

        ui.status("Writing parameters...")
        reach = {}
        write_errors = {}
        for pos, nid in mapping.items():
            write_errors[pos], reach[pos] = self._apply(sess, nid, plans[pos])
            for e in write_errors[pos]:
                ui.log(f"{pos}: {e}")

        ui.status("Restarting ESCs...")
        for pos in mapping:
            sess.restart(reach[pos])
        sess.set_bitrate(int(next(iter(self.targets.values()))["bit_rate"]))
        time.sleep(2)

        ui.status(f"Verifying at {sess.bitrate} bit/s...")
        ok = self._verify(sess, mapping, escs)
        ui.status("Drone complete. Swap in the next one." if ok else "Verification FAILED; see log.")
        return ok

    def spin_test(self, pos, throttle, seconds):
        """Arms over DroneCAN and spins one motor at a low throttle so its direction can be checked.
        Returns the peak |rpm| the ESC reported. Always sends zero throttle and disarms on exit."""
        esc, safety = dronecan.uavcan.equipment.esc, dronecan.uavcan.equipment.safety
        index = int(self.targets[pos]["esc_index"])
        cmd = [0] * (max(int(t["esc_index"]) for t in self.targets.values()) + 1)
        cmd[index] = int(round(max(0.0, min(throttle, 0.3)) * 8191))    # hard cap at 30%
        peak = [0]

        def on_status(e):
            if e.message.esc_index == index:
                peak[0] = max(peak[0], abs(e.message.rpm))

        sess = Session(self.port, self.bitrate, self.local_id)
        h = sess.node.add_handler(esc.Status, on_status)
        try:
            end, next_arm = time.monotonic() + seconds, 0.0
            while time.monotonic() < end and not self.ui.cancelled():
                if time.monotonic() >= next_arm:
                    sess.node.broadcast(safety.ArmingStatus(status=safety.ArmingStatus().STATUS_FULLY_ARMED))
                    next_arm = time.monotonic() + 0.2
                sess.node.broadcast(esc.RawCommand(cmd=cmd))
                sess.node.spin(0.02)      # 50 Hz, well inside the ESC's throttle timeout
        finally:
            zero = [0] * len(cmd)
            for _ in range(10):
                sess.node.broadcast(esc.RawCommand(cmd=zero))
                sess.node.broadcast(safety.ArmingStatus(status=safety.ArmingStatus().STATUS_DISARMED))
                sess.node.spin(0.02)
            h.remove()
            sess.close()
        return peak[0]

    def _scan(self, sess, seconds):
        seen = {}
        for nid, msg in sess.listen(dronecan.uavcan.protocol.NodeStatus, seconds):
            seen.setdefault(nid, []).append(msg.uptime_sec)
        return seen

    def _find_bus(self, sess):
        for b in [sess.bitrate] + [b for b in BITRATE_SEARCH_ORDER if b != sess.bitrate]:
            self._check_cancel()
            sess.set_bitrate(b)
            seen = self._scan(sess, 1.5)
            if seen:
                return seen
        raise FlashError("No DroneCAN traffic at any bitrate. Is the drone powered and the CAN cable connected?")

    def _find_escs(self, sess, seen):
        escs = {}
        for nid, uptimes in sorted(seen.items()):
            if nid == self.local_id:
                raise FlashError(f"Another node on the bus uses the flasher's ID {nid}; change --local-id")
            if any(b < a for a, b in zip(uptimes, uptimes[1:])):
                self.ui.log(f"Warning: node {nid} uptime goes backwards; several ESCs may share this node ID")
            info = sess.node_info(nid)
            if info is None:
                self.ui.log(f"Node {nid}: no GetNodeInfo response, ignoring")
                continue
            name = bytes(info.name).decode()
            if not name.startswith(ESC_NAME_PREFIX):
                self.ui.log(f"Node {nid}: {name} (not an ESC, ignoring)")
                continue
            escs[nid] = {"uid": bytes(info.hardware_version.unique_id).hex(), "name": name,
                         "sw": f"{info.software_version.major}.{info.software_version.minor}"}
            self.ui.log(f"Node {nid}: {name} sw {escs[nid]['sw']} uid {escs[nid]['uid']}")
        return escs

    def _identify(self, sess, escs):
        """Operator turns each motor by hand; the ESC whose motor_angle moves is that position."""
        status_ext = dronecan.uavcan.equipment.esc.StatusExtended
        heard = {nid for nid, _ in sess.listen(status_ext, 2.5)}
        silent = set(escs) - heard
        if silent:
            raise FlashError(f"No ESC telemetry from node(s) {sorted(silent)} (telem_frequency 0?)")

        remaining, mapping = set(escs), {}
        for pos in self.targets:
            place = POSITION_NAMES.get(pos, pos)
            self.ui.status(f"Slowly turn the {place.upper()} motor ({pos}) by hand, about a quarter turn per second")
            self.ui.prompt_turn(pos)
            last, moved = {}, {nid: 0.0 for nid in remaining}

            def on_status(e):
                nid, a = e.transfer.source_node_id, e.message.motor_angle
                if nid in last and nid in moved:
                    moved[nid] += abs(((a - last[nid] + 180) % 360) - 180)
                last[nid] = a

            h = sess.node.add_handler(status_ext, on_status)
            winner, deadline = None, time.monotonic() + TURN_TIMEOUT_S
            try:
                while winner is None:
                    self._check_cancel()
                    if time.monotonic() > deadline:
                        raise FlashError(f"Did not detect {pos} turning "
                                         f"(movement seen: { {n: round(v) for n, v in moved.items()} })")
                    sess.node.spin(0.2)
                    ranked = sorted(moved.values(), reverse=True) + [0.0]   # [0.0]: last motor has no rival
                    if ranked[0] >= TURN_DETECT_DEGREES and ranked[1] < ranked[0] / 3:
                        winner = max(moved, key=moved.get)
            finally:
                h.remove()
            mapping[pos] = winner
            remaining.discard(winner)
            self.ui.identified(pos, winner, escs[winner])
        return mapping

    @staticmethod
    def _plan(current, target):
        changes = []
        for name, new in target.items():
            if name not in current:
                raise FlashError(f"ESC does not expose parameter {name}")
            kind, old = current[name]
            if not same_value(old, new):
                changes.append((name, kind, old, new))
        return changes

    def _apply(self, sess, nid, changes):
        """Writes and saves. Returns ([errors], node ID the ESC is now reachable on)."""
        errors = []
        normal = [c for c in changes if c[0] not in APPLY_ON_RESTART]
        late = [c for c in changes if c[0] in APPLY_ON_RESTART]
        for name, kind, _, new in normal:
            try:
                got = sess.write_param(nid, name, kind, new)
                if not same_value(got, new):
                    errors.append(f"{name}: wrote {fmt(new)}, ESC reports {fmt(got)}")
            except TimeoutError as ex:
                errors.append(str(ex))
        if normal and not sess.save(nid):
            errors.append("save after parameter writes failed")

        reach = nid
        new_id = next((int(c[3]) for c in late if c[0] == "uavcan_node_id"), None)
        for name, kind, _, new in late:
            try:
                sess.write_param(reach, name, kind, new)
            except TimeoutError:
                if name == "uavcan_node_id":     # the ESC may switch IDs immediately
                    reach = new_id
        if late and not sess.save(reach):
            if new_id is not None and reach != new_id and sess.save(new_id):
                reach = new_id
            else:
                errors.append("save after node ID / bitrate write failed")
        return errors, reach

    def _verify(self, sess, mapping, escs):
        """Finds each ESC again by unique ID and compares every target parameter."""
        want_uid = {escs[nid]["uid"]: pos for pos, nid in mapping.items()}
        found = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(found) < len(want_uid):
            for nid in self._scan(sess, 1.5):
                if nid in found.values():
                    continue
                info = sess.node_info(nid)
                pos = info and want_uid.get(bytes(info.hardware_version.unique_id).hex())
                if pos:
                    found[pos] = nid
        all_ok = True
        for pos in mapping:
            if pos not in found:
                self.ui.result(pos, False, [f"not found on the bus at {sess.bitrate} bit/s"])
                all_ok = False
                continue
            nid, want = found[pos], self.targets[pos]
            problems = []
            if nid != want["uavcan_node_id"]:
                problems.append(f"node ID is {nid}, expected {want['uavcan_node_id']}")
            current = sess.read_params(nid)
            for name, value in want.items():
                have = current.get(name, (None, None))[1]
                if not same_value(have, value):
                    problems.append(f"{name} = {fmt(have)}, expected {fmt(value)}")
            self.ui.result(pos, not problems, problems)
            all_ok &= not problems
        return all_ok


# ---------------------------------------------------------------- console front end

class ConsoleUI:
    def log(self, text): print("   ", text)
    def status(self, text): print(f"\n== {text}")
    def escs_found(self, escs): pass
    def prompt_turn(self, pos): pass
    def identified(self, pos, nid, info): print(f"    {pos} = node {nid}  (uid {info['uid']})")
    def cancelled(self): return False

    def show_plan(self, plans):
        for pos, changes in plans.items():
            print(f"    {pos}: {len(changes)} change(s)")
            for name, _, old, new in changes:
                print(f"        {name:32} {fmt(old):>10} -> {fmt(new)}")

    def result(self, pos, ok, problems):
        print(f"    {pos}: {'PASS' if ok else 'FAIL'}" + "".join(f"\n        {p}" for p in problems))


def select_positions(targets, spec):
    if not spec:
        return targets
    wanted = [p.strip().upper() for p in spec.split(",")]
    unknown = [p for p in wanted if p not in targets]
    if unknown:
        raise FlashError(f"No profile for {unknown}; have {list(targets)}")
    return {p: targets[p] for p in wanted}


def main():
    from adapter import ensure_adapter
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="SLCAN device (default: find the Babel, attaching it under WSL)")
    ap.add_argument("--profiles", default=DEFAULT_PROFILES)
    ap.add_argument("--positions", help="only these positions, e.g. M2 or M1,M3")
    ap.add_argument("--local-id", type=int, default=127)
    ap.add_argument("--dry-run", action="store_true", help="identify and show planned changes; write nothing")
    args = ap.parse_args()

    targets, _ = load_profiles(args.profiles)
    targets = select_positions(targets, args.positions)
    port = args.port or ensure_adapter()
    flasher = Flasher(port, targets, ConsoleUI(), args.local_id)
    while input(f"\nConnect a drone, power it, then press Enter (q to quit): ").strip().lower() != "q":
        try:
            ok = flasher.run(args.dry_run)
            print(f"\n==> {'DONE' if ok else 'NOT COMPLETE'}")
        except (FlashError, Cancelled) as ex:
            print(f"\n==> NOT COMPLETE: {ex or 'cancelled'}")


if __name__ == "__main__":
    try:
        main()
    except FlashError as ex:
        sys.exit(str(ex))
    except KeyboardInterrupt:
        pass
