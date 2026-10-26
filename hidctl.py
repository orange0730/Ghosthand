#!/usr/bin/env python3
"""Send one command to hid_server.py, e.g. `hidctl.py move 100 -50` or `hidctl.py click`."""
import socket
import sys

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/run/pi-hid.sock")
f = s.makefile("rw")
f.write(" ".join(sys.argv[1:]) + "\n")
f.flush()
print(f.readline().strip())
