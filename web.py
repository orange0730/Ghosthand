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
import re
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
    """Owns the one Phone connection; manual actions and the assistant take turns.

    With "confirm each step" on, the assistant waits for the owner's decision before every
    action (run, skip, run all the rest, stop). While it waits, the owner may operate the
    phone by hand, then skip so the assistant looks at the screen again."""

    def __init__(self):
        self.lock = threading.Lock()  # held by whoever drives the phone: a manual action or the assistant
        self.hand = threading.Lock()  # manual actions while the assistant waits for a decision
        self.phone = None
        self.agent = None  # thread running the assistant
        self.task = ""
        self.lines = []
        self.stop = threading.Event()
        self.run_dir = None  # runs/<time> of the latest run, for the step screenshots
        self.steps = []      # one dict per assistant step, for the page's timeline
        self.final = None    # the action the latest run ended with
        self.confirm_each = False
        self.pending = None  # number of the step waiting for the owner's decision
        self.decision = None
        self.decided = threading.Event()

    def agent_running(self):
        return self.agent is not None and self.agent.is_alive()

    def get_phone(self):
        if self.phone is None:
            self.phone = Phone()
        return self.phone

    def act(self, action):
        if self.agent_running():
            if self.pending is None:
                raise Busy("AI 助手正在操作手機。要手動操作,請先按「停止」")
            with self.hand:  # the assistant is blocked in ask_owner(), so the phone is free
                perform(self.get_phone(), action)
            return
        with self.lock:
            perform(self.get_phone(), action)

    def ask_owner(self, action):
        """The assistant's confirm callback: True, "skip" or False."""
        if not self.confirm_each:
            return True
        self.decision = None
        self.decided.clear()
        self.pending = self.steps[-1]["step"] if self.steps else 0
        try:
            while not self.decided.wait(0.2):
                if self.stop.is_set():
                    return False
        finally:
            self.pending = None
        with self.hand:  # let a manual action that is under way finish first
            pass
        return self.decision

    def decide(self, decision):
        if self.pending is None:
            raise Busy("目前沒有等待確認的步驟")
        if decision == "run":
            self.decision = True
        elif decision == "run_all":
            self.confirm_each = False
            self.decision = True
        elif decision == "skip":
            self.decision = "skip"
        elif decision == "stop":
            self.stop.set()
            self.decision = False
        else:
            raise ValueError(f"unknown decision: {decision}")
        self.decided.set()

    def on_event(self, event):
        if event["kind"] == "start":
            self.run_dir = event["run_dir"]
        elif event["kind"] == "step":
            step = {k: event[k] for k in ("step", "shot", "observation", "thought", "action")}
            self.steps.append({**step, "result": None})
        elif event["kind"] == "result":
            for step in reversed(self.steps):
                if step["step"] == event["step"]:
                    step["result"] = event["result"]
                    break

    def start_agent(self, task, confirm_each=False):
        from agent import Agent, make_client  # imports openai only when the assistant is used

        if self.agent_running():
            raise Busy("AI 助手已經在執行")
        client, model = make_client()
        self.stop.clear()
        self.task, self.lines = task, []
        self.run_dir, self.steps, self.final, self.pending = None, [], None, None
        self.confirm_each = confirm_each

        def log(text):
            self.lines.extend(text.split("\n"))
            del self.lines[:-500]

        def run():
            with self.lock:
                try:
                    self.final = Agent(client, model, phone=self.get_phone(), log=log,
                                       should_stop=self.stop.is_set, confirm=self.ask_owner,
                                       on_event=self.on_event).run(task)
                except Exception as err:
                    log(f"錯誤:{err}")
                    self.final = {"type": "error", "reason": str(err)}

        self.agent = threading.Thread(target=run, daemon=True)
        self.agent.start()

    def shot(self, run, name):
        """JPEG of one step of the latest run, or None."""
        if not self.run_dir or run != os.path.basename(self.run_dir) or not re.fullmatch(r"\d{3}\.jpg", name):
            return None
        try:
            with open(os.path.join(self.run_dir, name), "rb") as f:
                return f.read()
        except OSError:
            return None

    def status(self):
        return {
            "mirroring": mirroring(),
            "bluetooth": hid_status(),
            "points": [POINTS_W, POINTS_H],
            "mirror_name": config.get("MIRROR_NAME", "Pi-Agent"),
            "agent": {
                "running": self.agent_running(), "task": self.task, "lines": self.lines[-200:],
                "run": os.path.basename(self.run_dir) if self.run_dir else None,
                "steps": self.steps, "pending": self.pending, "confirm_each": self.confirm_each,
                "final": self.final,
            },
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
        elif path == "/api/agent/shot":
            query = parse_qs(urlsplit(self.path).query)
            data = console.shot(query.get("run", [""])[0], query.get("file", [""])[0])
            if data is None:
                return self.send_json({"error": "not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")  # never changes once written
            self.end_headers()
            self.wfile.write(data)
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
                console.start_agent(task, confirm_each=bool(body.get("confirm_each")))
            elif self.path == "/api/agent/decide":
                console.decide(str(body.get("decision", "")))
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
