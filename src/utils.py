import json
import os
import tempfile
from datetime import datetime


def log(msg):
    """Timestamped print with flush."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def ensure_dir(path):
    """Create directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)


def file_exists(path):
    """Check if a file exists and is non-empty."""
    return os.path.isfile(path) and os.path.getsize(path) > 0


def load_json(path):
    """Load JSON from file. Returns None if file doesn't exist."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """Atomic JSON write via tmpfile + rename. Creates parent dirs if needed."""
    ensure_dir(os.path.dirname(path))
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
