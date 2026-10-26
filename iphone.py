#!/usr/bin/env python3
"""Eyes and hands for the mirrored iPhone: frames dumped by start_uxplay.sh, pointer via hid_server.py.

All coordinates are iPhone screen points (IPHONE_POINTS in config.env, e.g. 430 x 932 on an
iPhone 15 Pro Max), origin top-left. perform() takes 0-1000 coordinates for the web console
and the agent.
The iPhone's pointer must be set to a yellow border with auto-hide off
(Settings > Accessibility > Pointer Control), so it can be found as a yellow ring in each frame.

  python3 iphone.py shot OUT.jpg            save the latest frame
  python3 iphone.py locate                  find the pointer
  python3 iphone.py calibrate [X Y]         measure HID units -> points around an empty spot (X, Y)
  python3 iphone.py goto X Y                move the pointer to (X, Y)
  python3 iphone.py tap X Y                 move to (X, Y) and click
"""
import glob
import io
import json
import os
import re
import socket
import sys
import time

import numpy as np
from PIL import Image

from config import POINTS_W, POINTS_H

FRAMES = "/dev/shm/iphone"
HID_SOCK = "/run/pi-hid.sock"
CALIBRATION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
RING_SIZE = (28, 44)  # outer diameter of the pointer ring (~35 points); excludes Calendar's ~21 pt dots
DEBUG = bool(os.environ.get("IPHONE_DEBUG"))


def frame_index(path):
    return int(re.search(r"(\d+)\.jpg$", path).group(1))


def frame_files():
    return sorted(glob.glob(f"{FRAMES}/frame-*.jpg"), key=frame_index, reverse=True)


def yellow_mask(img):
    hsv = np.asarray(img.convert("HSV")).astype(np.int16)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (h >= 25) & (h <= 45) & (s > 150) & (v > 150)


def components(mask, cell=2):
    """Connected regions of a boolean mask as (x0, y0, x1, y1) pixel boxes."""
    h, w = mask.shape[0] // cell * cell, mask.shape[1] // cell * cell
    on = mask[:h, :w].reshape(h // cell, cell, w // cell, cell).any(axis=(1, 3))
    seen = np.zeros_like(on)
    boxes = []
    for y0, x0 in zip(*np.nonzero(on)):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        stack, ys, xs = [(y0, x0)], [], []
        while stack:
            cy, cx = stack.pop()
            ys.append(cy)
            xs.append(cx)
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < on.shape[0] and 0 <= nx < on.shape[1] and on[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        boxes.append((min(xs) * cell, min(ys) * cell, (max(xs) + 1) * cell, (max(ys) + 1) * cell))
    return boxes


def find_ring(img):
    """Centre of the yellow pointer ring in points, or None."""
    mask = yellow_mask(img)
    sx, sy = POINTS_W / img.width, POINTS_H / img.height
    lo, hi = RING_SIZE[0] / sx, RING_SIZE[1] / sx
    best = None
    for x0, y0, x1, y1 in components(mask):
        w, h = x1 - x0, y1 - y0
        if not (lo <= w <= hi and lo <= h <= hi and 0.8 <= w / h <= 1.25):
            continue
        cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (w + h) / 4
        angles = np.linspace(0, 2 * np.pi, 24, endpoint=False)
        def frac(radius):
            px = np.clip((cx + radius * np.cos(angles)).astype(int), 0, mask.shape[1] - 1)
            py = np.clip((cy + radius * np.sin(angles)).astype(int), 0, mask.shape[0] - 1)
            return mask[py, px].mean()
        ring, hole = frac(r * 0.75), frac(r * 0.2)
        score = ring - hole
        if DEBUG:
            print(f"  candidate ({cx * sx:.0f}, {cy * sy:.0f}) size {w * sx:.0f}x{h * sy:.0f} "
                  f"ring {ring:.2f} hole {hole:.2f}")
        if ring >= 0.8 and hole <= 0.3 and (best is None or score > best[0]):
            best = (score, cx * sx, cy * sy)
    return best and (best[1], best[2])


def mirroring():
    """True while the iPhone is mirroring (start_uxplay.sh keeps this state file)."""
    try:
        return open(f"{FRAMES}/state").read().strip() == "mirroring"
    except OSError:
        return False


def hid_status():
    """'connected', 'waiting' (no iPhone), or 'offline' (hid_server not running)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(HID_SOCK)
            s.sendall(b"status\n")
            return s.makefile().readline().strip() or "offline"
    except OSError:
        return "offline"


class Phone:
    def __init__(self):
        self._connect()
        cal = json.load(open(CALIBRATION)) if os.path.exists(CALIBRATION) else {}
        # Points per HID unit for each report size; iOS accelerates bigger reports.
        self.gains = {int(k): v for k, v in cal.get("gains", {"2": 0.325, "8": 0.625}).items()}
        self.step = 8      # HID units per report for coarse moves
        self.fine_step = 2  # and for the last ~20 points
        self.pos = None    # last seen pointer position (points)

    # --- hands ---------------------------------------------------------
    def _connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(HID_SOCK)
        except OSError:
            raise RuntimeError("hid_server is not running (sudo systemctl start pi-iphone-hid)")
        self.io = self.sock.makefile("rw")

    def hid(self, line):
        for attempt in (1, 2):
            try:
                self.io.write(line + "\n")
                self.io.flush()
                reply = self.io.readline().strip()
                if reply:
                    break
            except OSError:
                pass
            if attempt == 2:
                raise RuntimeError("lost the connection to hid_server")
            self._connect()  # hid_server was restarted
        if reply != "ok":
            raise RuntimeError(reply.removeprefix("error "))

    def nudge(self, ux, uy, step=None):
        """Move by (ux, uy) HID units in reports of at most `step` units, 10 ms apart.

        iOS accelerates the pointer, so the distance per unit depends on `step`."""
        step = step or self.step
        ux, uy = round(ux), round(uy)
        while ux or uy:
            sx, sy = max(-step, min(step, ux)), max(-step, min(step, uy))
            self.hid(f"move {sx} {sy}")
            ux, uy = ux - sx, uy - sy
            time.sleep(0.01)

    def click(self):
        self.hid("click")

    # --- eyes ----------------------------------------------------------
    def latest(self):
        """(index, image) of the newest complete frame."""
        for path in frame_files():
            data = open(path, "rb").read()
            if data.endswith(b"\xff\xd9"):
                return frame_index(path), Image.open(io.BytesIO(data)).convert("RGB")
        raise RuntimeError("no mirrored frame; is the iPhone mirroring to Pi-Agent?")

    def frame_after(self, index, delay=0.25, timeout=2.0):
        """A frame captured at least `delay` s from now and newer than `index`.

        The iPhone streams continuously (~15 fps), so a fixed delay lets the move show up."""
        time.sleep(delay)
        start = time.time()
        while time.time() - start < timeout:
            files = frame_files()
            if files and frame_index(files[0]) > index + 1:
                break
            time.sleep(0.03)
        return self.latest()

    def observe(self):
        self.pos = find_ring(self.latest()[1])
        return self.pos

    def settled(self, index, timeout=1.5):
        """Pointer position once two consecutive frames newer than `index` agree.

        The mirror lags ~0.1 s and the pointer settles ~0.35 s after the last report."""
        start, last, prev = time.time(), index, None
        while time.time() - start < timeout:
            idx, img = self.latest()
            if idx != last:
                last = idx
                found = find_ring(img)
                if found and prev and abs(found[0] - prev[0]) <= 1 and abs(found[1] - prev[1]) <= 1:
                    return found
                prev = found
            time.sleep(0.02)
        return prev

    def move_observed(self, ux, uy, step=None):
        """Nudge, then return where the pointer settles (None if not visible)."""
        idx = self.latest()[0]
        self.nudge(ux, uy, step)
        found = self.settled(idx)
        if DEBUG:
            print(f"  nudge ({ux:.0f}, {uy:.0f}) step {step or self.step} -> {found}")
        return found

    def locate(self):
        """Find the pointer; after a finger touch iOS hides it until it moves, so nudge if needed."""
        if self.observe():
            return self.pos
        for ux, uy in ((20, 0), (-20, 0), (0, 20), (0, -20)):
            self.pos = self.move_observed(ux, uy)
            if self.pos:
                return self.pos
        raise RuntimeError("pointer not found; is it set to a yellow border and the iPhone mirroring?")

    def goto(self, x, y, tolerance=3, rounds=6):
        x = min(max(x, 5), POINTS_W - 5)
        y = min(max(y, 5), POINTS_H - 5)
        if self.pos is None:
            self.locate()
        for _ in range(rounds):
            ex, ey = x - self.pos[0], y - self.pos[1]
            if abs(ex) <= tolerance and abs(ey) <= tolerance:
                return self.pos
            step = self.step if max(abs(ex), abs(ey)) > 20 else self.fine_step
            gain = self.gains[step]
            seen = self.move_observed(ex / gain, ey / gain, step)
            if seen is None:
                self.locate()
            else:
                self.pos = seen
        if abs(x - self.pos[0]) > tolerance or abs(y - self.pos[1]) > tolerance:
            raise RuntimeError(f"could not reach ({x:.0f}, {y:.0f}); pointer at "
                               f"({self.pos[0]:.0f}, {self.pos[1]:.0f})")
        return self.pos

    def tap(self, x, y):
        self.goto(x, y)
        self.click()

    def swipe(self, x1, y1, x2, y2):
        """Press at (x1, y1), drag to (x2, y2) and release: a finger swipe for iOS.

        The drag itself is open loop (the content moves under the pointer)."""
        self.goto(x1, y1)
        self.hid("down")
        try:
            gain = self.gains[self.step]
            self.nudge((x2 - x1) / gain, (y2 - y1) / gain)
            time.sleep(0.05)
        finally:
            self.hid("up")
        self.pos = None  # re-locate on the next move

    def scroll(self, ticks, x=None, y=None):
        """Mouse-wheel scroll; positive ticks scroll the content up (towards the top)."""
        if x is not None:
            self.goto(x, y)
        self.hid(f"scroll {int(ticks)}")

    # --- calibration ---------------------------------------------------
    def calibrate(self, centre, steps=(2, 4, 8, 16), units=(40, 80, 120)):
        """Points moved per HID unit for each report size and distance, both axes."""
        self.locate()
        results = {}
        for step in steps:
            for n in units:
                gains = []
                for ux, uy in ((n, 0), (-n, 0), (0, n), (0, -n)):
                    self.goto(*centre, tolerance=8)
                    start = self.pos
                    seen = self.move_observed(ux, uy, step)
                    if seen is None:
                        continue
                    self.pos = seen
                    gains.append(((seen[0] - start[0]) / ux) if ux else ((seen[1] - start[1]) / uy))
                gx, gy = np.mean(gains[:2]), np.mean(gains[2:])
                results[(step, n)] = (gx, gy)
                print(f"step {step:2d} units {n:3d}: x {gx:.3f}  y {gy:.3f}   raw "
                      + " ".join(f"{g:.3f}" for g in gains), flush=True)
        return results


def perform(phone, action):
    """Carry out one action dict; coordinates are 0-1000 of the screen width/height.

    tap, double_tap, long_press (seconds), swipe (x1 y1 x2 y2), scroll (ticks, + = up),
    move, type (ASCII text), key (e.g. "enter", "cmd+a"), home, wait (seconds)."""
    def point(x="x", y="y"):
        return (min(max(float(action[x]), 0), 1000) * POINTS_W / 1000,
                min(max(float(action[y]), 0), 1000) * POINTS_H / 1000)

    kind = action["type"]
    if kind == "tap":
        phone.tap(*point())
    elif kind == "double_tap":
        phone.goto(*point())
        phone.click()
        time.sleep(0.08)
        phone.click()
    elif kind == "long_press":
        phone.goto(*point())
        phone.hid("down")
        time.sleep(min(float(action.get("seconds", 1)), 5))
        phone.hid("up")
    elif kind == "swipe":
        phone.swipe(*point("x1", "y1"), *point("x2", "y2"))
    elif kind == "scroll":
        phone.scroll(int(action["ticks"]), *(point() if "x" in action else (None, None)))
    elif kind == "move":
        phone.goto(*point())
    elif kind == "type":
        text = action["text"]
        if not text.isascii():
            raise ValueError("only ASCII text can be typed")
        for i, line in enumerate(text.split("\n")):
            if i:
                phone.hid("key enter")
            # hid_server strips each command line, so edge spaces go as separate keys.
            core = line.strip(" ")
            lead = len(line) - len(line.lstrip(" "))
            for _ in range(lead):
                phone.hid("key space")
            if core:
                phone.hid(f"type {core}")
            for _ in range(len(line) - lead - len(core)):
                phone.hid("key space")
    elif kind == "key":
        phone.hid(f"key {action['keys'].lower()}")
    elif kind == "home":
        phone.hid("key cmd+h")
    elif kind == "wait":
        time.sleep(min(float(action.get("seconds", 2)), 30))
    else:
        raise ValueError(f"unknown action type {kind!r}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], [float(a) for a in sys.argv[3 if sys.argv[1] == "shot" else 2:]]
    if cmd == "shot":
        Phone.latest(None)[1].save(sys.argv[2])
        return
    phone = Phone()
    if cmd == "locate":
        print("pointer at (%.0f, %.0f)" % phone.locate())
    elif cmd == "calibrate":
        results = phone.calibrate(tuple(args) if args else (POINTS_W / 2, POINTS_H / 2))
        gains = {}
        for (step, _), (gx, gy) in results.items():
            gains.setdefault(step, []).extend([gx, gy])
        gains = {str(step): round(float(np.nanmedian(g)), 4) for step, g in gains.items()}
        json.dump({"gains": gains}, open(CALIBRATION, "w"), indent=2)
        print(f"saved points per unit by report size {gains} to {CALIBRATION}")
    elif cmd == "goto":
        print("pointer at (%.0f, %.0f)" % phone.goto(*args))
    elif cmd == "tap":
        phone.tap(*args)
        print("tapped")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
