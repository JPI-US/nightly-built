#!/usr/bin/env python3
r"""
capture_supervisor.py - continuous Axum capture that never needs a Ctrl+C.

This is a drop-in replacement for capture_daemon.py. It keeps everything that
worked (11 PM rollover, dated UTF-16 slices, background report_and_retain.py,
capture-status.json heartbeat, reattach across device resets) and removes the
two manual steps that made the old routine janky:

  1. "Ctrl+R to get lines flowing."
     The supervisor watches for silence after espflash attaches. If nothing
     arrives within NB_AUTO_RESET_AFTER seconds it resets the board itself
     (`espflash reset`) and reattaches. It only does this while the attach has
     produced ZERO lines, at most NB_AUTO_RESET_MAX times - so a device that is
     legitimately quiet is never reset in a loop.

  2. "Ctrl+C, flash the new build, start capture again."
     The supervisor OWNS the serial port and exposes a control channel on
     127.0.0.1:NB_CONTROL_PORT. Ask it to flash and it does the whole dance
     itself - stop the monitor, release the port, erase, write, reattach - and
     annotates the day's capture log with what it flashed. Capture is down for
     the length of the flash and not one second more, and no human is involved.

Control protocol: one JSON object per line over TCP, one response per line.
  {"cmd":"status"}
  {"cmd":"pause"}    {"cmd":"resume"}
  {"cmd":"reset"}
  {"cmd":"note","text":"..."}
  {"cmd":"flash","image":"D:\\...\\axum-merged.bin","label":"a1b2c3d","erase":true}
Use nbctl.py rather than speaking it by hand.

Usage:
  python capture_supervisor.py
"""

import os
import sys
import json
import time
import socket
import datetime
import threading
import subprocess

import nb_common as nb

STATUS_INTERVAL   = int(os.environ.get("NB_STATUS_INTERVAL", "20"))
AUTO_RESET_AFTER  = float(os.environ.get("NB_AUTO_RESET_AFTER", "25"))
AUTO_RESET_MAX    = int(os.environ.get("NB_AUTO_RESET_MAX", "2"))
FLASH_TIMEOUT     = float(os.environ.get("NB_FLASH_TIMEOUT", "600"))
PYTHON            = sys.executable

# Test hooks (unset in production), kept compatible with capture_daemon.py:
#   AXUM_TEST_ROLL_SECS - roll every N seconds instead of at 23:00
#   AXUM_CAPTURE_CMD    - JSON list to run instead of espflash (a fake emitter)
_TEST_ROLL_SECS = os.environ.get("AXUM_TEST_ROLL_SECS")
_CAPTURE_CMD    = os.environ.get("AXUM_CAPTURE_CMD")


def capture_cmd():
    if _CAPTURE_CMD:
        return json.loads(_CAPTURE_CMD)
    return ["espflash", "monitor", "--port", nb.PORT]


def next_rollover(now):
    """The next 23:00 boundary at or after `now` (or +N s under the test hook)."""
    if _TEST_ROLL_SECS:
        return now + datetime.timedelta(seconds=float(_TEST_ROLL_SECS))
    today = now.replace(hour=nb.ROLLOVER_HOUR, minute=0, second=0, microsecond=0)
    return today if now < today else today + datetime.timedelta(days=1)


def slice_path(rollover_dt):
    return os.path.join(nb.LOG_DIR, f"capture-{rollover_dt:%Y%m%d}.log")


def open_slice(path):
    """Append-safe UTF-16 LE open; write a BOM only when the file is new."""
    new = (not os.path.exists(path)) or os.path.getsize(path) == 0
    f = open(path, "a", encoding="utf-16-le")
    if new:
        f.write("\ufeff")   # so process_log.py's decode("utf-16") picks LE
    return f


class Supervisor:
    def __init__(self):
        # Before anything opens a file: on a fresh machine (or a fresh test
        # dir) none of these exist yet, and the first slice is opened right
        # here in the constructor.
        nb.ensure_dirs()

        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.paused = threading.Event()

        now = datetime.datetime.now()
        self.roll = next_rollover(now)
        self.path = slice_path(self.roll)
        self.f = open_slice(self.path)
        self.lines = 0
        self.started_at = nb.now_iso()
        self.last_line_at = None
        self.last_status = 0.0
        self.state = "starting"

        self.proc = None
        self.attached_at = 0.0
        self.lines_this_attach = 0
        self.resets_this_attach = 0

        # Pending out-of-band action, performed by the capture loop once the
        # monitor has exited. Only the capture loop ever drives the serial port.
        self.pending = None
        self.action_done = threading.Event()
        self.action_result = {}

        self.last_flash = nb.read_json(
            os.path.join(nb.STATE_DIR, "last-flash.json"), default=None)

    # -------------------------------------------------------------- output --
    def write_line(self, text, ts=None):
        """Caller holds the lock. Append one timestamped line to the slice."""
        ts = ts or datetime.datetime.now()
        if ts >= self.roll:
            self.do_rollover(ts)
        stamp = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.f.write(f"[{stamp}] {text}\n")
        self.f.flush()
        self.lines += 1

    def annotate(self, text):
        """Put a supervisor event into the capture log itself.

        This is what lets the 11 PM report say "firmware updated to a1b2c3d at
        22:41" instead of inventing an explanation for why the board rebooted.
        """
        with self.lock:
            self.write_line(f"=== NIGHTLY {text} ===")
        nb.log("supervisor", text)

    def publish_status(self, state=None):
        """Atomically publish capture health for the web app. Best effort."""
        if state:
            self.state = state
        self.last_status = time.monotonic()
        try:
            payload = {
                "state": self.state,          # capturing | reattaching | starting
                                              # | flashing | paused | resetting | stopped
                "heartbeat_at": nb.now_iso(),
                "last_line_at": self.last_line_at,
                "current_slice": os.path.basename(self.path),
                "next_rollover": self.roll.isoformat(timespec="seconds"),
                "lines_today": self.lines,
                "started_at": self.started_at,
                "pid": os.getpid(),
                # New vs capture_daemon.py: the UI can now show what is running
                # on the board and that no human intervention is pending.
                "supervisor": True,
                "control_port": nb.CONTROL_PORT,
                "auto_flash": nb.AUTO_FLASH,
                "last_flash": self.last_flash,
            }
            nb.atomic_write_json(nb.CAPTURE_STATUS_PATH, payload)
        except Exception:      # noqa: BLE001 - status is never worth a crash
            pass

    # ------------------------------------------------------------ rollover --
    def do_rollover(self, ts):
        """Caller holds the lock: close the slice, open the next, fire report."""
        self.f.close()
        closed_path, closed_lines = self.path, self.lines
        self.roll = next_rollover(ts)
        self.path = slice_path(self.roll)
        self.f = open_slice(self.path)
        self.lines = 0
        nb.log("supervisor", f"rolled over -> {os.path.basename(self.path)} "
                             f"(next {self.roll:%m-%d %H:%M})")
        self.publish_status("capturing")
        self.kick_report(closed_path, closed_lines)

    def kick_report(self, path, lines):
        """Hand a closed slice to report_and_retain.py, detached."""
        name = os.path.basename(path)
        if lines < nb.MIN_SLICE_LINES:
            nb.log("supervisor", f"{name}: only {lines} lines - skipping report.")
            return
        nb.log("supervisor", f"{name}: report + retention in the background ...")
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        try:
            subprocess.Popen([PYTHON, nb.REPORTER, path], creationflags=flags)
        except OSError as exc:
            nb.log("supervisor", f"could not launch reporter: {exc}")

    # ------------------------------------------------------- serial actions --
    def kill_monitor(self):
        """Stop espflash and wait for the OS to actually release the port."""
        proc = self.proc
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:      # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        # Windows does not free a COM handle the instant the process dies, and
        # espflash's next open fails with "Access is denied" if we race it.
        time.sleep(1.5)

    def run_tool(self, args, timeout):
        """Run an espflash subcommand with the port to ourselves."""
        nb.log("supervisor", "$ " + " ".join(args))
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode, out
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        except OSError as exc:
            return 127, str(exc)

    def do_reset(self):
        code, out = self.run_tool(["espflash", "reset", "--port", nb.PORT], 60)
        ok = code == 0
        self.annotate(f"AUTO-RESET {'ok' if ok else 'FAILED'}"
                      + ("" if ok else f": {out.splitlines()[-1] if out else code}"))
        return {"ok": ok, "code": code, "output": out}

    def do_flash(self, req):
        """Erase (optional), write the image, and hand the port back to capture."""
        image = req.get("image")
        label = req.get("label") or os.path.basename(image or "?")
        erase = req.get("erase", nb.ERASE_BEFORE_FLASH)
        offset = req.get("offset", "0x0")

        if not image or not os.path.isfile(image):
            return {"ok": False, "error": f"image not found: {image}"}

        self.annotate(f"FLASH {label} begin (image={os.path.basename(image)}, erase={erase})")
        steps = []

        if erase:
            code, out = self.run_tool(
                ["espflash", "erase-flash", "--port", nb.PORT, "--chip", nb.CHIP],
                FLASH_TIMEOUT)
            steps.append({"step": "erase-flash", "code": code, "output": out[-2000:]})
            if code != 0:
                self.annotate(f"FLASH {label} FAILED at erase (exit {code})")
                return {"ok": False, "error": "erase-flash failed", "steps": steps}

        # An .elf goes through `flash` (espflash derives bootloader + partition
        # table); the merged .bin CI produces is written raw at 0x0. Never pass
        # --monitor: the supervisor reattaches its own monitor afterwards.
        if image.lower().endswith(".elf"):
            args = ["espflash", "flash", "--non-interactive",
                    "--port", nb.PORT, "--chip", nb.CHIP]
            if req.get("no_stub"):
                args.append("--no-stub")
            args.append(image)
        else:
            args = ["espflash", "write-bin", "--non-interactive",
                    "--port", nb.PORT, offset, image]

        code, out = self.run_tool(args, FLASH_TIMEOUT)
        steps.append({"step": "write", "code": code, "output": out[-4000:]})
        ok = code == 0
        self.annotate(f"FLASH {label} {'ok' if ok else 'FAILED'} (exit {code})")

        record = {
            "ok": ok,
            "label": label,
            "image": image,
            "erased": erase,
            "offset": offset,
            "at": nb.now_iso(),
            "steps": steps,
        }
        self.last_flash = {k: record[k] for k in ("ok", "label", "image", "at")}
        nb.atomic_write_json(os.path.join(nb.STATE_DIR, "last-flash.json"), self.last_flash)
        nb.atomic_write_json(
            os.path.join(nb.REPORTS_DIR, f"{nb.scoped('flash')}-{nb.today_stamp()}.json"), record)
        if not ok:
            nb.notify("Axum flash FAILED", f"{label}: espflash exited {code}", "error")
        return record

    # ------------------------------------------------------- control server --
    def handle_command(self, req):
        cmd = (req.get("cmd") or "").lower()

        if cmd == "status":
            return {
                "ok": True, "state": self.state, "slice": os.path.basename(self.path),
                "lines_today": self.lines, "last_line_at": self.last_line_at,
                "next_rollover": self.roll.isoformat(timespec="seconds"),
                "paused": self.paused.is_set(), "last_flash": self.last_flash,
                "port": nb.PORT, "pid": os.getpid(),
            }

        if cmd == "note":
            self.annotate(f"NOTE {req.get('text', '')}")
            return {"ok": True}

        if cmd == "pause":
            self.paused.set()
            self.request_action({"kind": "idle"}, wait=False)
            self.publish_status("paused")
            return {"ok": True, "state": "paused"}

        if cmd == "resume":
            self.paused.clear()
            return {"ok": True, "state": "resuming"}

        if cmd == "reset":
            return self.request_action({"kind": "reset"})

        if cmd == "flash":
            if self.paused.is_set():
                return {"ok": False, "error": "supervisor is paused; resume first"}
            return self.request_action({"kind": "flash", "req": req},
                                       timeout=FLASH_TIMEOUT + 120)

        return {"ok": False, "error": f"unknown cmd {cmd!r}"}

    def request_action(self, action, wait=True, timeout=120):
        """Queue work for the capture loop and (optionally) wait for its result.

        Only the capture loop touches the serial port, so a control client never
        races espflash for the COM handle - that race is exactly what made the
        old flash-by-hand routine flaky.
        """
        with self.lock:
            if self.pending is not None:
                return {"ok": False, "error": f"busy: {self.pending['kind']} in progress"}
            self.pending = action
            self.action_done.clear()
        self.kill_monitor()          # unblocks the capture loop's readline
        if not wait:
            return {"ok": True, "queued": action["kind"]}
        if not self.action_done.wait(timeout):
            return {"ok": False, "error": f"{action['kind']} timed out after {timeout}s"}
        return self.action_result

    def control_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((nb.CONTROL_HOST, nb.CONTROL_PORT))
        except OSError as exc:
            nb.log("supervisor", f"control port {nb.CONTROL_PORT} unavailable ({exc}); "
                                 "another supervisor is probably already running.")
            return
        srv.listen(4)
        srv.settimeout(1.0)
        nb.log("supervisor", f"control channel on {nb.CONTROL_HOST}:{nb.CONTROL_PORT}")
        while not self.stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve_client, args=(conn,), daemon=True).start()
        srv.close()

    def serve_client(self, conn):
        conn.settimeout(FLASH_TIMEOUT + 180)
        try:
            with conn, conn.makefile("rwb") as stream:
                line = stream.readline()
                if not line:
                    return
                try:
                    req = json.loads(line.decode("utf-8"))
                except ValueError as exc:
                    resp = {"ok": False, "error": f"bad json: {exc}"}
                else:
                    resp = self.handle_command(req)
                stream.write((json.dumps(resp) + "\n").encode("utf-8"))
                stream.flush()
        except Exception as exc:      # noqa: BLE001 - one bad client, not a crash
            nb.log("supervisor", f"control client error: {exc}")

    # ----------------------------------------------------------- timer loop --
    def timer_loop(self):
        """Force the 23:00 rollover even if the device is silent, keep the
        heartbeat fresh, and auto-reset a board that never started talking."""
        while not self.stop.is_set():
            wait = (self.roll - datetime.datetime.now()).total_seconds()
            if wait > 0:
                self.stop.wait(min(wait, 15))
                with self.lock:
                    self.publish_status()
                self.check_silence()
                continue
            with self.lock:
                if datetime.datetime.now() >= self.roll:
                    self.do_rollover(datetime.datetime.now())

    def check_silence(self):
        """Replace the manual Ctrl+R: reset a board that attached but never spoke.

        Deliberately narrow - only while this attach has produced zero lines, and
        only AUTO_RESET_MAX times. Once a single line arrives we never reset
        again for that attach, so a device that is simply idle overnight is left
        alone.
        """
        if self.state != "capturing" or self.paused.is_set():
            return
        if self.lines_this_attach > 0 or self.resets_this_attach >= AUTO_RESET_MAX:
            return
        if not self.attached_at or (time.monotonic() - self.attached_at) < AUTO_RESET_AFTER:
            return
        with self.lock:
            if self.pending is not None:
                return
        nb.log("supervisor", f"silent for {AUTO_RESET_AFTER:.0f}s after attach "
                             f"- resetting the board (attempt {self.resets_this_attach + 1}"
                             f"/{AUTO_RESET_MAX}).")
        self.resets_this_attach += 1
        self.request_action({"kind": "reset"}, wait=False)

    # ------------------------------------------------------------ main loop --
    def perform_pending(self):
        """Run the queued action. Called from the capture loop only."""
        with self.lock:
            action = self.pending
        if not action:
            return
        kind = action["kind"]
        try:
            if kind == "reset":
                self.publish_status("resetting")
                result = self.do_reset()
            elif kind == "flash":
                self.publish_status("flashing")
                result = self.do_flash(action["req"])
                self.resets_this_attach = 0    # a flash resets the board anyway
            else:                               # "idle" - just stay detached
                result = {"ok": True}
        except Exception as exc:                # noqa: BLE001
            result = {"ok": False, "error": repr(exc)}
        with self.lock:
            self.action_result = result
            self.pending = None
        self.action_done.set()

    def run(self):
        nb.ensure_dirs()
        print("=" * 70)
        print("  AXUM CAPTURE SUPERVISOR")
        print("  Self-slicing at 11 PM, self-resetting when silent,")
        print("  self-flashing on request - no Ctrl+R, no Ctrl+C.")
        print(f"  Port          : {nb.PORT}")
        print(f"  Logging to    : {self.path}")
        print(f"  Next rollover : {self.roll:%Y-%m-%d %H:%M}")
        print(f"  Control       : {nb.CONTROL_HOST}:{nb.CONTROL_PORT}  (use nbctl.py)")
        print("  CTRL+C to stop.")
        print("=" * 70)

        self.publish_status("starting")
        threading.Thread(target=self.timer_loop, daemon=True).start()
        threading.Thread(target=self.control_loop, daemon=True).start()

        try:
            while not self.stop.is_set():
                if self.paused.is_set():
                    self.publish_status("paused")
                    self.stop.wait(2)
                    continue

                self.proc = subprocess.Popen(
                    capture_cmd(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=None,          # inherit the console, so a human CAN still
                                         # press Ctrl+R - they just never have to
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                )
                self.attached_at = time.monotonic()
                self.lines_this_attach = 0
                nb.log("supervisor", f"espflash attached (PID {self.proc.pid}) "
                                     f"-> {os.path.basename(self.path)}")
                with self.lock:
                    self.publish_status("capturing")

                for line in self.proc.stdout:
                    line = line.rstrip("\n")
                    ts = datetime.datetime.now()
                    with self.lock:
                        self.write_line(line, ts)
                        self.last_line_at = ts.isoformat(timespec="seconds")
                        if time.monotonic() - self.last_status >= STATUS_INTERVAL:
                            self.publish_status("capturing")
                    self.lines_this_attach += 1
                    if self.lines_this_attach == 1:
                        # The board is talking, so whatever it took to get here
                        # worked. Give the reset budget back, otherwise two
                        # resets in the process's whole lifetime would disable
                        # auto-reset forever.
                        self.resets_this_attach = 0
                    # nb.EOL rather than a bare newline: espflash holds the
                    # terminal in raw mode, so a linefeed on its own would
                    # staircase this echo down the screen instead of returning
                    # to column 0.
                    print(line, end=nb.EOL)   # echo so capture is visibly live
                self.proc.wait()
                self.proc = None

                if self.stop.is_set():
                    break

                if self.pending is not None:
                    self.perform_pending()
                    continue             # reattach immediately after the action

                # Unrequested exit: the device reset and its USB-CDC
                # re-enumerated. Reattaching is the whole point - the firmware
                # reboot-loops on the Wi-Fi ESP_ERR_TIMEOUT bug and we want to
                # CAPTURE that, not die with it.
                nb.log("supervisor", "espflash exited (device reset / USB re-enumerate) "
                                     "- reattaching in 3 s ...")
                with self.lock:
                    self.publish_status("reattaching")
                self.stop.wait(3)
        except KeyboardInterrupt:
            print("\n[supervisor] CTRL+C - stopping capture.")
        finally:
            self.shutdown()

    def shutdown(self):
        self.stop.set()
        self.kill_monitor()
        with self.lock:
            try:
                self.f.close()
            except Exception:      # noqa: BLE001
                pass
        self.publish_status("stopped")
        # espflash left the tty in raw mode if we killed it rather than letting
        # it exit; without this the user's shell is unusable afterwards.
        if sys.stdout.isatty():
            try:
                subprocess.run(["stty", "sane"], check=False)
            except OSError:
                pass
        nb.log("supervisor", "stopped.")


if __name__ == "__main__":
    Supervisor().run()
