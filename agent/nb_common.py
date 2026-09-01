#!/usr/bin/env python3
"""
nb_common.py - config, atomic publishing and notification shared by the
nightly-built agent processes.

Standard library only, on purpose: this runs on the Monitor PC next to
capture_daemon.py / process_log.py, which have the same rule. Anything that
needs `pip install` is a thing that can be broken on the machine that is
supposed to be collecting data unattended.

Every path is env-overridable so the same code runs on the Monitor PC, on a
LAN-mounted share, and in a dry-run test directory.
"""

import os
import sys
import json
import time
import pathlib
import datetime
import subprocess


# ----------------------------------------------------------------- dotenv ---
def load_dotenv(path=None):
    """Same minimal loader process_log.py uses, so both read one D:\\scheduler\\.env."""
    candidates = [path] if path else [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.environ.get("NB_SCHEDULER_DIR", r"D:\scheduler"), ".env"),
    ]
    for p in candidates:
        if not p:
            continue
        try:
            for line in pathlib.Path(p).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except FileNotFoundError:
            continue


load_dotenv()

# ----------------------------------------------------------------- config ---
SCHEDULER_DIR = os.environ.get("NB_SCHEDULER_DIR", r"D:\scheduler")

# Shared with the existing scheduler scripts - same names, same defaults, so a
# single .env configures capture, reporting and nightly builds together.
PORT        = os.environ.get("AXUM_PORT", "COM11")
LOG_DIR     = os.environ.get("AXUM_LOG_DIR", os.path.join(SCHEDULER_DIR, "logs"))
REPORTS_DIR = os.environ.get("AXUM_REPORTS_DIR", os.path.join(SCHEDULER_DIR, "public", "reports"))
REPORTER    = os.environ.get("AXUM_REPORTER", os.path.join(SCHEDULER_DIR, "report_and_retain.py"))

# Nightly-build state: downloaded images, the known-good image, cursor files.
STATE_DIR     = os.environ.get("NB_STATE_DIR", os.path.join(SCHEDULER_DIR, "nightly"))
IMAGES_DIR    = os.path.join(STATE_DIR, "images")
KNOWN_GOOD_DIR = os.path.join(STATE_DIR, "known-good")

# GitHub
GH_REPO   = os.environ.get("NB_GITHUB_REPO", "JPI-US/nightly-built")
GH_TOKEN  = os.environ.get("NB_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
GH_WORKFLOW = os.environ.get("NB_WORKFLOW_FILE", "nightly-build.yml")
POLL_SECS = int(os.environ.get("NB_POLL_SECS", "180"))

# Serial / flashing
CHIP            = os.environ.get("NB_CHIP", "esp32s3")
CONTROL_HOST    = os.environ.get("NB_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT    = int(os.environ.get("NB_CONTROL_PORT", "8787"))
ERASE_BEFORE_FLASH = os.environ.get("NB_ERASE_FLASH", "1") not in ("0", "false", "no")
AUTO_FLASH      = os.environ.get("NB_AUTO_FLASH", "0") not in ("0", "false", "no")

# Published status files the React app can read over the same LAN mount as the
# reports (mirrors capture-status.json).
BUILD_STATUS_PATH   = os.path.join(REPORTS_DIR, "build-status.json")
CAPTURE_STATUS_PATH = os.path.join(REPORTS_DIR, "capture-status.json")
FAILED_MARKER       = os.path.join(REPORTS_DIR, "BUILD-FAILED.txt")

ROLLOVER_HOUR   = int(os.environ.get("NB_ROLLOVER_HOUR", "23"))
MIN_SLICE_LINES = int(os.environ.get("NB_MIN_SLICE_LINES", "50"))


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today_stamp(dt=None):
    return (dt or datetime.datetime.now()).strftime("%Y%m%d")


def log(tag, msg):
    print(f"[{tag}] {msg}", flush=True)


# --------------------------------------------------------------- io helpers -
def atomic_write(path, text, encoding="utf-8"):
    """Write via a temp file + os.replace, with a bounded retry.

    The reason for the retry is the same one capture_daemon.py documents: on
    Windows/SMB a reader (the web app) can hold the file briefly and os.replace
    raises PermissionError. Freshness is sacrificed, never the caller.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding, newline="\n") as f:
        f.write(text)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt == 2:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False
            time.sleep(0.1)
    return False


def atomic_write_json(path, payload):
    return atomic_write(path, json.dumps(payload, indent=2) + "\n")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ notification --
def notify(title, message, level="warning"):
    """Best-effort desktop notification + console bell + a marker file.

    Never raises: a failed toast must not take down the build agent.
    """
    sys.stdout.write("\a")
    sys.stdout.flush()
    log("notify", f"{title}: {message}")
    if os.name != "nt":
        return
    icon = {"error": "Error", "warning": "Warning", "info": "Info"}.get(level, "Info")
    # NotifyIcon balloon: no external module, and Windows 11 renders it as a
    # normal toast. The sleep is required - disposing immediately kills it.
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        f"$n.Icon = [System.Drawing.SystemIcons]::{icon};"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(15000, {_ps_quote(title)}, {_ps_quote(message)},"
        f" [System.Windows.Forms.ToolTipIcon]::{icon});"
        "Start-Sleep -Seconds 8; $n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:      # noqa: BLE001 - notification is never fatal
        log("notify", f"toast failed ({exc})")


def _ps_quote(s):
    """Single-quoted PowerShell literal; '' escapes an embedded quote."""
    return "'" + str(s).replace("'", "''") + "'"


# --------------------------------------------------------------- github api -
def gh_request(path, method="GET", token=None, raw=False, data=None):
    """Minimal GitHub REST call over urllib. Returns (status, body)."""
    import urllib.request
    import urllib.error

    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nightly-built-agent",
    }
    tok = token if token is not None else GH_TOKEN
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    body = json.dumps(data).encode() if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload or b"null")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        return exc.code, detail
    except Exception as exc:      # noqa: BLE001 - network flake is expected
        return 0, str(exc)


def ensure_dirs():
    for d in (LOG_DIR, REPORTS_DIR, STATE_DIR, IMAGES_DIR, KNOWN_GOOD_DIR):
        os.makedirs(d, exist_ok=True)
