#!/usr/bin/env python3
"""Bluetooth Classic HID device (keyboard + mouse) that lets the Pi drive an iPhone.

Needs root and bluetoothd started with --noplugin=input (so PSM 17/19 are free).
On the iPhone turn on Settings > Accessibility > Touch > AssistiveTouch, then pair
"Pi-Agent HID" from Settings > Bluetooth.

Control it through the Unix socket /run/pi-hid.sock, one command per line:
  status | connect | move DX DY | click | down | up | scroll N | type TEXT | key COMBO (e.g. cmd+h)
When disconnected, any command except status first reconnects to the last iPhone.
"""
import os
import socket
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

import config

SOCK_PATH = "/run/pi-hid.sock"
LAST_HOST = "/var/lib/pi-hid-last-host"
PSM_CTRL, PSM_INTR = 0x11, 0x13
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
AGENT_PATH = "/pi_agent/agent"
PROFILE_PATH = "/pi_agent/hid"
ALIAS = config.get("BT_NAME", "Pi-Agent HID")

KEYBOARD_ID, MOUSE_ID = 1, 2
REPORT_MAP = (
    # Keyboard, report ID 1: modifiers, reserved, 6 keycodes
    "05010906a1018501"
    "050719e029e715002501750195088102"
    "950175088101"
    "95057501050819012905" "9102"
    "950175039101"
    "95067508150025650507190029658100"
    "c0"
    # Mouse, report ID 2: 3 buttons, relative X, Y, wheel
    "05010902a10185020901a100"
    "050919012903150025019503750181" "02"
    "950175058101"
    "0501093009310938158125" "7f" "750895038106"
    "c0c0"
)

SDP_RECORD = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124" /></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0011" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0006"><sequence>
    <uint16 value="0x656e" /><uint16 value="0x006a" /><uint16 value="0x0100" />
  </sequence></attribute>
  <attribute id="0x0009"><sequence>
    <sequence><uuid value="0x1124" /><uint16 value="0x0100" /></sequence>
  </sequence></attribute>
  <attribute id="0x000d"><sequence><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0013" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></sequence></attribute>
  <attribute id="0x0100"><text value="{ALIAS}" /></attribute>
  <attribute id="0x0101"><text value="Keyboard and mouse" /></attribute>
  <attribute id="0x0102"><text value="Raspberry Pi" /></attribute>
  <attribute id="0x0200"><uint16 value="0x0100" /></attribute>
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <attribute id="0x0202"><uint8 value="0xc0" /></attribute>
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <attribute id="0x0204"><boolean value="true" /></attribute>
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <attribute id="0x0206"><sequence><sequence>
    <uint8 value="0x22" /><text encoding="hex" value="{REPORT_MAP}" />
  </sequence></sequence></attribute>
  <attribute id="0x0207"><sequence><sequence>
    <uint16 value="0x0409" /><uint16 value="0x0100" />
  </sequence></sequence></attribute>
  <attribute id="0x020b"><uint16 value="0x0100" /></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <attribute id="0x020d"><boolean value="true" /></attribute>
  <attribute id="0x020e"><boolean value="false" /></attribute>
</record>
"""

MODIFIERS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "cmd": 0x08}
NAMED_KEYS = {
    "enter": 40, "esc": 41, "backspace": 42, "tab": 43, "space": 44,
    "right": 79, "left": 80, "down": 81, "up": 82,
}
PLAIN = "-=[]\\;'`,./"
SHIFTED = "_+{}|:\"~<>?"
PLAIN_CODES = [45, 46, 47, 48, 49, 51, 52, 53, 54, 55, 56]


def char_to_key(ch):
    """Return (modifier, keycode) for an ASCII character."""
    if "a" <= ch <= "z":
        return 0, 4 + ord(ch) - ord("a")
    if "A" <= ch <= "Z":
        return MODIFIERS["shift"], 4 + ord(ch) - ord("A")
    if "1" <= ch <= "9":
        return 0, 30 + ord(ch) - ord("1")
    if ch == "0":
        return 0, 39
    if ch in "!@#$%^&*()":
        return MODIFIERS["shift"], 30 + "!@#$%^&*()".index(ch)
    if ch in PLAIN:
        return 0, PLAIN_CODES[PLAIN.index(ch)]
    if ch in SHIFTED:
        return MODIFIERS["shift"], PLAIN_CODES[SHIFTED.index(ch)]
    if ch == " ":
        return 0, 44
    if ch == "\n":
        return 0, 40
    raise ValueError(f"unsupported character {ch!r}")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class HidDevice:
    def __init__(self):
        self.ctrl = None
        self.intr = None
        self.buttons = 0
        self.lock = threading.Lock()

    @staticmethod
    def _listen(psm):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((socket.BDADDR_ANY, psm))
        s.listen(1)
        return s

    def serve(self):
        lctrl, lintr = self._listen(PSM_CTRL), self._listen(PSM_INTR)
        log("waiting for host on PSM 17/19")
        while True:
            ctrl, addr = lctrl.accept()
            log(f"control channel from {addr[0]}")
            intr, _ = lintr.accept()
            log("interrupt channel up, connected")
            self._session(ctrl, intr, addr[0])

    def _session(self, ctrl, intr, host):
        self.ctrl, self.intr = ctrl, intr
        with open(LAST_HOST, "w") as f:
            f.write(host)
        try:
            self._handle_control(ctrl)
        except OSError as e:
            log(f"control channel error: {e}")
        self.ctrl = self.intr = None
        ctrl.close()
        intr.close()
        log("disconnected")

    def connect(self):
        """Reconnect to the last host ourselves, as a real mouse does (iOS only reconnects on its
        own when the user taps the device in Settings). Called only when a command needs the
        phone, so a disconnect made on the iPhone is respected while nothing is being done."""
        if self.intr is not None:
            return True
        if not os.path.exists(LAST_HOST):
            return False
        host = open(LAST_HOST).read().strip()
        socks = []
        try:
            for psm in (PSM_CTRL, PSM_INTR):
                s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
                socks.append(s)
                s.settimeout(8)
                s.connect((host, psm))
                s.settimeout(None)
        except OSError as e:
            log(f"reconnect to {host} failed: {e}")
            for s in socks:
                s.close()
            return False
        log(f"reconnected to {host}")
        self.ctrl, self.intr = socks
        threading.Thread(target=self._session, args=(socks[0], socks[1], host), daemon=True).start()
        return True

    def _handle_control(self, ctrl):
        while True:
            data = ctrl.recv(64)
            if not data:
                return
            kind, param = data[0] >> 4, data[0] & 0x0F
            log(f"control message {data.hex()}")
            if kind == 0x1 and param == 0x5:  # HID_CONTROL: virtual cable unplug
                return
            if kind in (0x5, 0x7, 0x9):  # SET_REPORT, SET_PROTOCOL, SET_IDLE
                ctrl.send(b"\x00")
            elif kind == 0x6:  # GET_PROTOCOL: report protocol
                ctrl.send(b"\xa0\x01")
            elif kind == 0x4:  # GET_REPORT: answer with an idle report
                report_id = data[1] if len(data) > 1 else MOUSE_ID
                size = 8 if report_id == KEYBOARD_ID else 4
                ctrl.send(bytes([0xA0 | (param & 0x03), report_id]) + bytes(size))
            elif kind != 0x1:
                ctrl.send(b"\x03")  # HANDSHAKE: unsupported request

    def _send(self, report_id, payload):
        if self.intr is None:
            raise RuntimeError("iPhone not connected")
        self.intr.send(bytes([0xA1, report_id]) + bytes(payload))

    def _mouse(self, dx=0, dy=0, wheel=0):
        self._send(MOUSE_ID, [self.buttons, dx & 0xFF, dy & 0xFF, wheel & 0xFF])

    def move(self, dx, dy):
        while dx or dy:
            sx, sy = max(-127, min(127, dx)), max(-127, min(127, dy))
            self._mouse(sx, sy)
            dx, dy = dx - sx, dy - sy
            time.sleep(0.01)

    def press(self, down):
        self.buttons = 1 if down else 0
        self._mouse()

    def click(self):
        self.press(True)
        time.sleep(0.05)
        self.press(False)

    def scroll(self, n):
        step = 1 if n > 0 else -1
        for _ in range(abs(n)):
            self._mouse(wheel=step)
            time.sleep(0.02)

    def _tap_key(self, modifier, keycode):
        self._send(KEYBOARD_ID, [modifier, 0, keycode, 0, 0, 0, 0, 0])
        time.sleep(0.02)
        self._send(KEYBOARD_ID, [0] * 8)
        time.sleep(0.02)

    def type_text(self, text):
        for ch in text:
            self._tap_key(*char_to_key(ch))

    def key_combo(self, combo):
        modifier, keycode = 0, None
        for part in combo.lower().split("+"):
            if part in MODIFIERS:
                modifier |= MODIFIERS[part]
            elif part in NAMED_KEYS:
                keycode = NAMED_KEYS[part]
            else:
                extra, keycode = char_to_key(part)
                modifier |= extra
        self._tap_key(modifier, keycode or 0)

    def command(self, line):
        cmd, _, arg = line.partition(" ")
        if cmd == "status":
            return "connected" if self.intr else "waiting"
        if cmd == "connect":
            return "ok" if self.connect() else "error iPhone not reachable"
        if self.intr is None and not self.connect():
            return "error iPhone not connected (is Bluetooth on and the iPhone unlocked?)"
        with self.lock:
            if cmd == "move":
                dx, dy = map(int, arg.split())
                self.move(dx, dy)
            elif cmd == "click":
                self.click()
            elif cmd == "down":
                self.press(True)
            elif cmd == "up":
                self.press(False)
            elif cmd == "scroll":
                self.scroll(int(arg))
            elif cmd == "type":
                self.type_text(arg)
            elif cmd == "key":
                self.key_combo(arg)
            else:
                return f"error unknown command {cmd!r}"
        return "ok"


def control_server(dev):
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        with conn, conn.makefile("rw") as f:
            for line in f:
                try:
                    reply = dev.command(line.strip())
                except Exception as e:
                    reply = f"error {e}"
                f.write(reply + "\n")
                f.flush()


class Agent(dbus.service.Object):
    """Pairing agent that accepts every request (Just Works pairing)."""

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log(f"authorize service {uuid} for {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log(f"passkey {passkey:06d} for {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log(f"pin code {pincode} for {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log(f"confirm passkey {passkey:06d} for {device}")
        dbus.Interface(dbus.SystemBus().get_object("org.bluez", device),
                       "org.freedesktop.DBus.Properties").Set("org.bluez.Device1", "Trusted", True)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log(f"authorize {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log("pairing cancelled")


class Profile(dbus.service.Object):
    """Only publishes the HID SDP record; the L2CAP channels are handled by HidDevice."""

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, properties):
        os.close(fd.take())

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        pass


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    adapter = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"),
                             "org.freedesktop.DBus.Properties")
    adapter.Set("org.bluez.Adapter1", "Alias", ALIAS)
    adapter.Set("org.bluez.Adapter1", "Pairable", True)
    adapter.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))
    adapter.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(0))
    adapter.Set("org.bluez.Adapter1", "Discoverable", True)

    bluez = bus.get_object("org.bluez", "/org/bluez")
    Agent(bus, AGENT_PATH)
    agents = dbus.Interface(bluez, "org.bluez.AgentManager1")
    agents.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    agents.RequestDefaultAgent(AGENT_PATH)

    Profile(bus, PROFILE_PATH)
    dbus.Interface(bluez, "org.bluez.ProfileManager1").RegisterProfile(PROFILE_PATH, HID_UUID, {
        "ServiceRecord": SDP_RECORD,
        "Role": "server",
        "RequireAuthentication": False,
        "RequireAuthorization": False,
    })
    log(f"registered HID profile, discoverable as {ALIAS!r}")

    dev = HidDevice()
    threading.Thread(target=dev.serve, daemon=True).start()
    threading.Thread(target=control_server, args=(dev,), daemon=True).start()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
