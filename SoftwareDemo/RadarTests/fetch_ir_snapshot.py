#!/usr/bin/env python3
"""
Run on your Mac or PC. Fetches one JPEG from the Pi IR snapshot server and saves it.

Usage:
  python fetch_ir_snapshot.py http://192.168.1.50:8765/snapshot
  python fetch_ir_snapshot.py http://peekir.local:8765/snapshot ./my_ir.jpg

Optional: on macOS, opens the image with the default viewer after save.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.error
import urllib.request


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch one IR snapshot from Pi")
    p.add_argument(
        "url",
        nargs="?",
        default="http://raspberrypi.local:8765/snapshot",
        help="Full URL to /snapshot on the Pi",
    )
    p.add_argument(
        "output",
        nargs="?",
        default="ir_snapshot.jpg",
        help="Local output path",
    )
    p.add_argument("--no-open", action="store_true", help="Do not open image after save (macOS)")
    args = p.parse_args()

    req = urllib.request.Request(args.url, headers={"User-Agent": "fetch_ir_snapshot"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} {e.reason}", file=sys.stderr)
        if e.fp:
            print(e.fp.read().decode(errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Connection failed: {e.reason}", file=sys.stderr)
        return 1

    if not data.startswith(b"\xff\xd8"):
        print("Warning: response does not look like JPEG", file=sys.stderr)

    with open(args.output, "wb") as f:
        f.write(data)
    print(f"Saved {len(data)} bytes to {args.output}")

    if sys.platform == "darwin" and not args.no_open:
        subprocess.run(["open", args.output], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
