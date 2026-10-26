"""Settings shared by every part of the project, read from config.env next to this file.

Environment variables override the file. See config.env.example for the keys.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "config.env")


def _load(path):
    values = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_FILE = _load(PATH)


def get(key, default=None):
    return os.environ.get(key) or _FILE.get(key) or default


# iPhone screen size in points (portrait); the mirror is requested at 2x this.
POINTS_W, POINTS_H = (int(v) for v in get("IPHONE_POINTS", "430x932").lower().split("x"))
