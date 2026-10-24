"""Operator GUI for flashing Vertiq ESC profiles by motor position."""
import argparse
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from adapter import AdapterError, adapter_name, ensure_adapter
from flasher import (DEFAULT_PROFILES, POSITION_NAMES, Cancelled, FlashError, Flasher, fmt, load_profiles,
                     same_value, select_positions)

DIRECTIONS = {3: "CCW", 4: "CW"}
COLORS = {"idle": "#9aa0a6", "turn": "#f9ab00", "found": "#1a73e8", "pass": "#1e8e3e", "fail": "#d93025"}
# Meanings of enumerated parameters, from Vertiq's DroneCAN protocol docs.
VALUE_NAMES = {
    "control_mode": {0: "PWM", 1: "Voltage", 2: "Velocity"},
    "motor_direction": {0: "Unconfigured", 1: "3D CCW", 2: "3D CW", 3: "2D CCW", 4: "2D CW"},
    "zero_behavior": {0: "Coast", 1: "Brake", 2: "Normal controller"},
    "disarm_behavior": {0: "Coast", 1: "0V to coast", 2: "0V to brake", 3: "Stow"},
    "disarm_song_option": {0: "Never", 1: "Once", 2: "Continuously"},
    "hold_stow": {0: "Coast", 1: "Hold stow", 2: "Brake", 3: "Low power hold stow"},
    "arm_with_arming_status": {0: "off", 1: "on"},
    "arm_on_throttle": {0: "off", 1: "on"},
}


def describe(name, value):
    if value is None:
        return "—"
    if name == "max_velocity":
        return f"{fmt(value)} rad/s ({float(value) * 60 / 6.283185307:.0f} rpm)"
    meaning = VALUE_NAMES.get(name, {}).get(int(value)) if float(value).is_integer() else None
    return f"{fmt(value)} {meaning}" if meaning else fmt(value)


# Grid cell for each place name, as seen from above with the nose at the top.
GRID = {"top left": (0, 0), "top right": (0, 1), "bottom left": (1, 0), "bottom right": (1, 1)}


class MotorTile(ttk.Frame):
    def __init__(self, parent, pos, target, on_spin):
        super().__init__(parent, padding=10, relief="groove")
        place = POSITION_NAMES.get(pos, pos)
        ttk.Label(self, text=place.upper(), foreground="#5f6368").pack()
        self.canvas = tk.Canvas(self, width=70, height=70, highlightthickness=0)
        self.dot = self.canvas.create_oval(5, 5, 65, 65, fill=COLORS["idle"], outline="")
        self.canvas.create_text(35, 35, text=pos, fill="white", font=("TkDefaultFont", 16, "bold"))
        self.canvas.pack()
        direction = DIRECTIONS.get(target["motor_direction"], f"dir {target['motor_direction']}")
        ttk.Label(self, text=f"node {target['uavcan_node_id']} · ESC {target['esc_index']} · {direction}").pack()
        self.detail = ttk.Label(self, text="", foreground="#5f6368")
        self.detail.pack()
        self.state = ttk.Label(self, text="waiting", font=("TkDefaultFont", 11, "bold"))
        self.state.pack()
        self.spin_btn = ttk.Button(self, text=f"Spin test ({direction})", command=lambda: on_spin(pos))
        self.spin_btn.pack(pady=(6, 0))
        self.spin_btn.state(["disabled"])
        self._blink = None

    def set(self, state, text, detail=None):
        self.stop_blink()
        self.canvas.itemconfigure(self.dot, fill=COLORS[state])
        self.state.configure(text=text)
        if detail is not None:
            self.detail.configure(text=detail)
        if state == "turn":
            self._blink_step(True)

    def _blink_step(self, on):
        self.canvas.itemconfigure(self.dot, fill=COLORS["turn"] if on else COLORS["idle"])
        self._blink = self.after(400, self._blink_step, not on)

    def stop_blink(self):
        if self._blink:
            self.after_cancel(self._blink)
            self._blink = None


class App:
    def __init__(self, root, targets, unmapped, args):
        self.root, self.targets, self.args = root, targets, args
        self.port = args.port
        self.flasher = None
        self.flashed = set()          # positions verified this drone, which may be spin tested
        self.props_confirmed = False
        self._events = queue.Queue()
        self._cancel = threading.Event()

        root.title("ESC Flasher")
        root.minsize(880, 760)
        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        self.adapter_label = ttk.Label(top, text="Adapter: looking for Zubax Babel...")
        self.adapter_label.pack(side="left")
        ttk.Label(top, text=f"{len(targets)} motors · {len(unmapped)} profile settings not settable over "
                            f"DroneCAN", foreground="#5f6368").pack(side="right")

        self.status_label = ttk.Label(root, text="", font=("TkDefaultFont", 14, "bold"), padding=(10, 4),
                                      wraplength=860)
        self.status_label.pack(fill="x")

        frame = ttk.Frame(root, padding=10)
        frame.pack()
        ttk.Label(frame, text="▲ FRONT", foreground="#5f6368").grid(row=0, column=0, columnspan=2)
        self.tiles = {}
        for i, (pos, target) in enumerate(targets.items()):
            row, col = GRID.get(POSITION_NAMES.get(pos), (2 + i // 2, i % 2))
            self.tiles[pos] = MotorTile(frame, pos, target, self.spin)
            self.tiles[pos].grid(row=row + 1, column=col, padx=6, pady=6)

        buttons = ttk.Frame(root, padding=(10, 0))
        buttons.pack(fill="x")
        self.start_btn = ttk.Button(buttons, text=f"Flash all {len(targets)} motors", command=self.start)
        self.read_btn = ttk.Button(buttons, text="Read settings", command=self.read_settings)
        self.cancel_btn = ttk.Button(buttons, text="Cancel / Stop", command=self._cancel.set)
        for w in (self.start_btn, self.read_btn, self.cancel_btn):
            w.pack(side="left", padx=(0, 6))

        panes = ttk.PanedWindow(root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=10, pady=10)
        self.tabs = ttk.Notebook(panes)
        self.plan = ttk.Treeview(self.tabs, columns=("pos", "param", "old", "new"), show="headings", height=6)
        for col, title, width in (("pos", "Motor", 60), ("param", "Parameter", 260),
                                  ("old", "Old", 120), ("new", "New", 120)):
            self.plan.heading(col, text=title)
            self.plan.column(col, width=width, anchor="w")
        self.settings = ttk.Treeview(self.tabs, show="headings", height=6)
        self.settings.tag_configure("diff", background="#fce8e6")
        self.tabs.add(self.plan, text="Planned changes")
        self.tabs.add(self.settings, text="ESC settings")
        self.log_box = scrolledtext.ScrolledText(panes, height=6, state="disabled", font=("TkFixedFont", 9))
        panes.add(self.tabs, weight=1)
        panes.add(self.log_box, weight=1)

        self._busy(True)
        self.root.after(50, self._pump)
        threading.Thread(target=self._connect_adapter, daemon=True).start()

    # ---- thread plumbing: worker threads only ever queue calls onto the Tk thread

    def _post(self, fn, *a):
        self._events.put((fn, a))

    def _pump(self):
        try:
            while True:
                fn, a = self._events.get_nowait()
                fn(*a)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _busy(self, busy):
        """While a job runs only Cancel is available; otherwise Start and verified spin tests are."""
        for btn in (self.start_btn, self.read_btn):
            btn.state(["disabled"] if busy or self.flasher is None else ["!disabled"])
        self.cancel_btn.state(["!disabled"] if busy else ["disabled"])
        for pos, tile in self.tiles.items():
            tile.spin_btn.state(["!disabled"] if not busy and pos in self.flashed else ["disabled"])

    def _append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _job(self, target, *args):
        self._cancel.clear()
        self._busy(True)

        def run():
            try:
                target(*args)
            except Cancelled:
                self.status_("Cancelled.")
            except FlashError as ex:
                self.status_(f"Stopped: {ex}")
                self.log(f"==> NOT COMPLETE: {ex}")
            except Exception as ex:          # keep the app alive on unexpected bus/driver errors
                self.status_(f"Error: {ex!r}")
                self.log(f"==> ERROR: {ex!r}")
            finally:
                for tile in self.tiles.values():
                    self._post(tile.stop_blink)
                self._post(self._busy, False)
        threading.Thread(target=run, daemon=True).start()

    # ---- adapter

    def _connect_adapter(self):
        try:
            port = self.port or ensure_adapter(log=self.log)
        except AdapterError as ex:
            self._post(self.adapter_label.configure, {"text": f"Adapter: {ex}", "foreground": COLORS["fail"]})
            self.status_("Fix the adapter problem, then restart the app.")
            return
        self.port = port
        self.flasher = Flasher(port, self.targets, self, self.args.local_id)
        self._post(self.adapter_label.configure, {"text": f"Adapter: {adapter_name(port)} on {port}",
                                                  "foreground": COLORS["pass"]})
        self.status_(f"Connect a drone, power it, then press Flash all {len(self.targets)} motors.")
        self._post(self._busy, False)

    # ---- flashing

    def start(self):
        self.flashed.clear()
        self.props_confirmed = False
        self.plan.delete(*self.plan.get_children())
        for tile in self.tiles.values():
            tile.set("idle", "waiting", "")
        self._job(self._flash)

    def _flash(self):
        ok = self.flasher.run()
        if ok:
            self.status_("Drone complete. Use Spin test to check each motor's direction, "
                         "then swap in the next drone.")
            self.log("==> DONE")
        else:
            self.log("==> NOT COMPLETE")

    # ---- settings view

    def read_settings(self):
        self._job(self._read_settings)

    def _read_settings(self):
        escs, params = self.flasher.read_settings()
        self._post(self._fill_settings, escs, params)
        self.status_(f"Read settings from {len(escs)} ESC(s); rows that differ from a profile are highlighted.")

    def _fill_settings(self, escs, params):
        """One column per ESC, named by the position whose profile node ID it is on; one row per parameter."""
        by_node = {t["uavcan_node_id"]: pos for pos, t in self.targets.items()}
        cols = [f"n{nid}" for nid in sorted(escs)]
        tree = self.settings
        tree.delete(*tree.get_children())
        tree.configure(columns=["param"] + cols)
        tree.heading("param", text="Parameter")
        tree.column("param", width=200, anchor="w")
        for nid in sorted(escs):
            pos = by_node.get(nid)
            tree.heading(f"n{nid}", text=f"{pos} · node {nid}" if pos else f"node {nid} (no profile)")
            tree.column(f"n{nid}", width=150, anchor="w")
        names = list(next(iter(self.targets.values())))
        names += sorted({n for p in params.values() for n in p} - set(names))
        for name in names:
            cells, diff = [], False
            for nid in sorted(escs):
                have = params[nid].get(name, (None, None))[1]
                want = self.targets.get(by_node.get(nid), {}).get(name)
                text = describe(name, have)
                if want is not None and not same_value(have, want):
                    text += f"  ≠ {describe(name, want)}"
                    diff = True
                cells.append(text)
            tree.insert("", "end", values=[name] + cells, tags=("diff",) if diff else ())
        self.tabs.select(self.settings)

    # ---- spin test

    def spin(self, pos):
        if not self.props_confirmed:
            if not messagebox.askokcancel(
                    "Spin test", "The motor will spin at low throttle for a few seconds.\n\n"
                                 "Are the PROPELLERS REMOVED and the drone secured?", icon="warning"):
                return
            self.props_confirmed = True
        self._job(self._spin, pos)

    def _spin(self, pos):
        place = POSITION_NAMES.get(pos, pos)
        direction = DIRECTIONS.get(self.targets[pos]["motor_direction"], "?")
        self.status_(f"Spinning the {place} motor ({pos}); it should turn {direction} seen from above.")
        self._post(self.tiles[pos].set, "turn", "SPINNING")
        try:
            rpm = self.flasher.spin_test(pos, self.args.spin_throttle, self.args.spin_seconds)
        finally:
            self._post(self.tiles[pos].set, "pass", "FLASHED")
        self.log(f"{pos} spin test: ESC reported up to {rpm} rpm")
        if rpm == 0:
            self.status_(f"{pos} reported no rotation; check it is armed-by-DroneCAN and the throttle "
                         f"is above its deadband.")
        else:
            self.status_(f"Did the {place} motor turn {direction}? If not, fix its profile's Motor Direction.")

    # ---- Flasher UI interface (called from worker threads)

    def log(self, text):
        self._post(self._append_log, text)

    def status_(self, text):
        self._post(self.status_label.configure, {"text": text})

    def status(self, text):
        self.status_(text)
        self.log(text)

    def escs_found(self, escs):
        self.log(f"Found {len(escs)} ESC(s)")

    def prompt_turn(self, pos):
        self._post(self.tiles[pos].set, "turn", "TURN THIS MOTOR")

    def identified(self, pos, nid, info):
        self._post(self.tiles[pos].set, "found", f"found: node {nid}", f"uid …{info['uid'][-8:]}")

    def show_plan(self, plans):
        def fill():
            for pos, changes in plans.items():
                if not changes:
                    self.plan.insert("", "end", values=(pos, "(already correct)", "", ""))
                for name, _, old, new in changes:
                    self.plan.insert("", "end", values=(pos, name, fmt(old), fmt(new)))
        self._post(fill)

    def result(self, pos, ok, problems):
        if ok:
            self.flashed.add(pos)
        self._post(self.tiles[pos].set, "pass" if ok else "fail", "FLASHED" if ok else "FAIL")
        for p in problems:
            self.log(f"{pos}: {p}")

    def cancelled(self):
        return self._cancel.is_set()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="SLCAN device or SocketCAN interface, e.g. /dev/ttyACM0 or can0 (default: find the Babel or a PEAK adapter)")
    ap.add_argument("--profiles", default=DEFAULT_PROFILES)
    ap.add_argument("--positions", help="only these positions, e.g. M2 or M1,M3")
    ap.add_argument("--local-id", type=int, default=127)
    ap.add_argument("--spin-throttle", type=float, default=0.08, help="spin test throttle, 0-0.3 (default 0.08)")
    ap.add_argument("--spin-seconds", type=float, default=3.0, help="spin test duration (default 3)")
    args = ap.parse_args()

    root = tk.Tk()
    try:
        targets, unmapped = load_profiles(args.profiles)
        targets = select_positions(targets, args.positions)
    except FlashError as ex:
        root.withdraw()
        messagebox.showerror("ESC Flasher", str(ex))
        return
    App(root, targets, unmapped, args)
    root.mainloop()


if __name__ == "__main__":
    main()
