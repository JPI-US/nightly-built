#!/usr/bin/env python3
r"""
nbctl.py - talk to the running capture supervisor.

This is the replacement for "Ctrl+C, do the thing, start capture again". The
supervisor owns the serial port; you ask it for what you want and it sequences
the port itself.

  python nbctl.py status
  python nbctl.py pause                  # detach from the port (rare)
  python nbctl.py resume
  python nbctl.py reset                  # what Ctrl+R used to do
  python nbctl.py note "swapped the limit switch"
  python nbctl.py flash D:\scheduler\nightly\known-good\axum-known-good.bin
  python nbctl.py flash <image> --no-erase --label a1b2c3d

Also importable: nbctl.send({"cmd": "status"}) -> dict.
"""

import sys
import json
import socket
import argparse

import nb_common as nb


def send(payload, timeout=None, quiet=False):
    """One request, one response, over the supervisor's control channel."""
    timeout = timeout or (900 if payload.get("cmd") == "flash" else 20)
    try:
        with socket.create_connection((nb.CONTROL_HOST, nb.CONTROL_PORT), timeout=10) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8")) if buf.strip() else {
                "ok": False, "error": "empty response"}
    except (ConnectionRefusedError, OSError) as exc:
        msg = (f"supervisor not reachable on {nb.CONTROL_HOST}:{nb.CONTROL_PORT} "
               f"({exc}) - is capture_supervisor.py running?")
        if not quiet:
            nb.log("nbctl", msg)
        return {"ok": False, "error": msg}
    except ValueError as exc:
        return {"ok": False, "error": f"bad response: {exc}"}


def main():
    ap = argparse.ArgumentParser(description="Control the Axum capture supervisor.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("reset")

    p_note = sub.add_parser("note")
    p_note.add_argument("text")

    p_flash = sub.add_parser("flash")
    p_flash.add_argument("image")
    p_flash.add_argument("--label", default=None)
    p_flash.add_argument("--offset", default="0x0")
    p_flash.add_argument("--no-erase", action="store_true",
                         help="skip erase-flash (faster; keeps NVS heading/encoder state)")

    args = ap.parse_args()

    if args.cmd == "note":
        req = {"cmd": "note", "text": args.text}
    elif args.cmd == "flash":
        req = {"cmd": "flash", "image": args.image, "label": args.label,
               "offset": args.offset, "erase": not args.no_erase}
    else:
        req = {"cmd": args.cmd}

    resp = send(req)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
