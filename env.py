"""
env.py
Loads key=value pairs from a .env file located next to this module
into os.environ, without overwriting variables that are already set.
"""
import os

_LOADED = False
_ENV_FILENAME = ".env"

def _parse_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not key:
        return None
    return key, value

def load_env():
    global _LOADED
    if _LOADED:
        return
    _LOADED = True

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _ENV_FILENAME)
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parsed = _parse_line(line)
                if parsed is None:
                    continue
                key, value = parsed
                if key not in os.environ:
                    os.environ[key] = value
    except OSError as e:
        print(f"[env] could not read {path}: {e}")

load_env()