#!/usr/bin/env python3
"""Web console: watch the mirrored iPhone and control it from a browser on the same network.

  .venv/bin/python web.py          serves http://<pi address>:WEB_PORT (8080 by default)

The page long-polls the frames UxPlay dumps (/frame.jpg) and turns clicks, drags, the mouse
wheel and the keyboard into actions for hid_server. It can also run the AI assistant.
"""
import base64
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import config
from iphone import POINTS_H, POINTS_W, Phone, frame_files, hid_status, mirroring, perform

STATIC = os.path.join(config.HERE, "static")
PASSWORD = config.get("WEB_PASSWORD", "")


class Busy(Exception):
    pass


class Console:
    """Owns the one Phone connection; manual actions and the assistant take turns."""

    def __init__(self):
        self.lock = threading.Lock()
        self.phone = None
        self.agent = None  # thread running the assistant
        self.task = ""
        self.lines = []
        self.stop = threading.Event()

    def agent_running(self):
        return self.agent is not None and self.agent.is_alive()

    def get_phone(self):
        if self.phone is None:
            self.phone = Phone()
        return self.phone

    def act(self, action):
        if self.agent_running():
            raise Busy("AI 助手正在操作手機,請先按「停止」")
        with self.lock:
            perform(self.get_phone(), action)

    def start_agent(self, task):
        from agent import Agent, make_client  # imports openai only when the assistant is used

        if self.agent_running():
            raise Busy("AI 助手已經在執行")
        client, model = make_client()
        self.stop.clear()
        self.task, self.lines = task, []

        def log(text):
            self.lines.extend(text.split("\n"))
            del self.lines[:-500]

        def run():
            with self.lock:
                try:
                    Agent(client, model, phone=self.get_phone(), log=log,
                          should_stop=self.stop.is_set).run(task)
                except Exception as err:
                    log(f"錯誤:{err}")

        self.agent = threading.Thread(target=run, daemon=True)
        self.agent.start()

    def status(self):
        return {
            "mirroring": mirroring(),
            "bluetooth": hid_status(),
            "points": [POINTS_W, POINTS_H],
            "mirror_name": config.get("MIRROR_NAME", "Pi-Agent"),
            "agent": {"running": self.agent_running(), "task": self.task, "lines": self.lines[-200:]},
        }


def newest_frame():
    """(path, bytes) of the newest complete JPEG frame, or (None, None)."""
    for path in frame_files():
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        if data.endswith(b"\xff\xd9"):
            return path, data
    return None, None


console = Console()


class Handler(BaseHTTPRequestHandler):
    server_version = "Ghosthand"

    def log_message(self, fmt, *args):
        pass  # one line per frame request would flood the journal

    def authorized(self):
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                given = ""
            if hmac.compare_digest(given.encode(), PASSWORD.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="iPhone remote"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def send_body(self, body, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_body(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)

    def do_GET(self):
        if not self.authorized():
            return
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                self.send_body(f.read(), "text/html; charset=utf-8")
        elif path == "/stream.mjpg":
            self.stream()
        elif path == "/frame.jpg":
            self.frame(parse_qs(urlsplit(self.path).query).get("after", [""])[0])
        elif path == "/api/status":
            self.send_json(console.status())
        else:
            self.send_json({"error": "not found"}, 404)

    def frame(self, after):
        """Newest frame. With ?after=<X-Frame of the last one>, wait up to 3 s for a different
        frame and answer 204 if none came: the page's frame loop recovers from anything."""
        deadline = time.time() + 3
        while True:
            path, data = newest_frame()
            name = os.path.basename(path) if path else ""
            if data and name != after:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame", name)
                self.end_headers()
                self.wfile.write(data)
                return
            if not after or time.time() > deadline:
                break
            time.sleep(0.03)
        if data:
            self.send_response(204)
            self.end_headers()
        else:
            self.send_json({"error": "no frame; is the iPhone mirroring?"}, 404)

    def stream(self):
        """MJPEG stream of the screen, e.g. for VLC; the page itself uses /frame.jpg."""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                path, data = newest_frame()
                if data and path != last:
                    last = path
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    self.wfile.flush()
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.authorized():
            return
        # A JSON content type forces a CORS preflight, so other web sites can't post here.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "expected application/json"}, 415)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "bad JSON"}, 400)
        try:
            if self.path == "/api/action":
                console.act(body)
            elif self.path == "/api/agent/start":
                task = str(body.get("task", "")).strip()
                if not task:
                    return self.send_json({"error": "請輸入任務"}, 400)
                console.start_agent(task)
            elif self.path == "/api/agent/stop":
                console.stop.set()
            else:
                return self.send_json({"error": "not found"}, 404)
        except Busy as err:
            return self.send_json({"error": str(err)}, 409)
        except Exception as err:
            return self.send_json({"error": str(err)}, 500)
        self.send_json({"ok": True})


def main():
    port = int(config.get("WEB_PORT", "8080"))
    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    print(f"web console on http://0.0.0.0:{port}" + ("" if PASSWORD else " (no password)"), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
