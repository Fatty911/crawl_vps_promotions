#!/usr/bin/env python3
"""Run a command with flushed process-liveness logs and its real exit status."""

import argparse
import math
import signal
import subprocess
import sys
import time


class StopRequested(BaseException):
    def __init__(self, signum):
        self.signum = signum


def run_command(command, interval):
    child = None
    previous_handlers = {}
    started = time.monotonic()

    def stop(signum, frame):
        raise StopRequested(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, stop)
        child = subprocess.Popen(command)
        while True:
            try:
                returncode = child.wait(timeout=interval)
                return 128 - returncode if returncode < 0 else returncode
            except subprocess.TimeoutExpired:
                if child.poll() is None:
                    print(f"heartbeat: child pid={child.pid} alive "
                          f"elapsed={time.monotonic() - started:.1f}s", flush=True)
    except (StopRequested, KeyboardInterrupt) as exc:
        return 128 + getattr(exc, "signum", signal.SIGINT)
    finally:
        try:
            for signum in previous_handlers:
                signal.signal(signum, signal.SIG_IGN)
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be a finite positive number")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a child command is required after --")
    return run_command(command, args.interval)


if __name__ == "__main__":
    sys.exit(main())
