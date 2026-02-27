#!/usr/bin/env python3
"""
Lepton VoSPI debug capture (single frame) with strong failure localization.

Key additions vs prior script:
- Uses spidev xfer2(..., delay_usecs=DELAY_US) to enforce CS idle gap
- Detects and reports:
   * "ALL_DISCARD" (never saw a valid packet)
   * "STUCK_HEADER" (same header repeating -> CS framing/MISO issue)
   * "INVALID_SEGMENT_DOMINANT" (seg=15 etc)
- Dumps raw first 8 bytes of recent packets for electrical sanity checks

Try in this order:
  python3 lepton_vospi_debug.py --hz 2000000 --delay-us 20 --timeout 10 --verbose
  python3 lepton_vospi_debug.py --hz 1000000 --delay-us 50 --timeout 12 --verbose
"""

import argparse
import collections
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    import spidev
except ImportError:
    print("ERROR: spidev not installed. sudo apt install -y python3-spidev", file=sys.stderr)
    sys.exit(2)

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy not installed. sudo apt install -y python3-numpy", file=sys.stderr)
    sys.exit(2)


PACKET_SIZE = 164
HEADER_SIZE = 4
INVALID_PKT_NUM = 0x0FFF  # lower 12 bits all ones => discard in many Lepton VoSPI stacks


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def parse_packet_id(pkt: List[int]) -> Tuple[int, int, int]:
    raw_id = (pkt[0] << 8) | pkt[1]
    seg = (raw_id >> 12) & 0xF
    pnum = raw_id & 0x0FFF
    return raw_id, seg, pnum


def is_discard(raw_id: int) -> bool:
    return (raw_id & 0x0FFF) == INVALID_PKT_NUM


def open_spi(bus: int, dev: int, hz: int, mode: int) -> "spidev.SpiDev":
    spi = spidev.SpiDev()
    spi.open(bus, dev)
    spi.max_speed_hz = hz
    spi.mode = mode
    return spi


def read_packet(spi: "spidev.SpiDev", delay_us: int) -> List[int]:
    # xfer2 args: data, speed_hz=0, delay_usecs=0, bits_per_word=0
    return spi.xfer2([0] * PACKET_SIZE, 0, delay_us, 0)


def dump_recent(id_history: collections.deque) -> str:
    # entries: (t_ms, raw_id, seg, pnum, first8bytes)
    lines = []
    for t_ms, raw_id, seg, pnum, first8 in list(id_history)[-20:]:
        fb = " ".join(f"{b:02X}" for b in first8)
        lines.append(f"{t_ms:>8}ms  id=0x{raw_id:04X} seg={seg:>2} pkt={pnum:>4}  hdr8=[{fb}]")
    return "\n".join(lines) if lines else "(none)"


def probe(spi, delay_us: int, n: int) -> Dict:
    seg_counts = collections.Counter()
    pnum_counts = collections.Counter()
    rawid_counts = collections.Counter()
    discards = 0
    ok = 0
    id_history = collections.deque(maxlen=400)

    t0 = now_ms()
    for _ in range(n):
        pkt = read_packet(spi, delay_us)
        raw_id, seg, pnum = parse_packet_id(pkt)
        first8 = pkt[:8]
        id_history.append((now_ms() - t0, raw_id, seg, pnum, first8))

        rawid_counts[raw_id] += 1

        if is_discard(raw_id):
            discards += 1
            continue

        ok += 1
        seg_counts[seg] += 1
        pnum_counts[pnum] += 1

    # Determine if header is "stuck" (top raw_id repeats overwhelmingly)
    most_common_rawid, mc_count = rawid_counts.most_common(1)[0]
    stuck_ratio = mc_count / max(n, 1)

    pkt_0_59 = sum(pnum_counts[i] for i in range(60))
    pkt_0_59_ratio = pkt_0_59 / max(ok, 1)

    return {
        "ok": ok,
        "discards": discards,
        "seg_counts": seg_counts,
        "pnum_0_59_ratio": pkt_0_59_ratio,
        "most_common_rawid": most_common_rawid,
        "stuck_ratio": stuck_ratio,
        "id_history": id_history,
    }


def write_pgm_u16(path: str, frame_u16: np.ndarray) -> None:
    h, w = frame_u16.shape
    with open(path, "wb") as f:
        f.write(f"P5\n{w} {h}\n65535\n".encode("ascii"))
        frame_u16.astype(np.uint16).byteswap().tofile(f)


def capture_segmented_160x120(spi, delay_us: int, timeout_s: float, verbose: bool):
    """
    4 segments x 60 packets = 240 total
    """
    id_history = collections.deque(maxlen=2000)
    stats = {
        "discards": 0,
        "kept": 0,
        "invalid_seg": 0,
        "bad_pnum": 0,
        "duplicate": 0,
        "seg_fill": {1: 0, 2: 0, 3: 0, 4: 0},
        "missing": [],
    }

    # Sync: wait for seg=1 pkt=0
    t0 = time.monotonic()
    base_ms = now_ms()
    synced = False
    while time.monotonic() - t0 < timeout_s:
        pkt = read_packet(spi, delay_us)
        raw_id, seg, pnum = parse_packet_id(pkt)
        id_history.append((now_ms() - base_ms, raw_id, seg, pnum, pkt[:8]))

        if is_discard(raw_id):
            stats["discards"] += 1
            continue

        if seg == 1 and pnum == 0:
            synced = True
            if verbose:
                print(f"[SYNC] seg=1 pkt=0 id=0x{raw_id:04X}")
            break

    if not synced:
        return None, "TIMEOUT_SYNC_SEG1_PKT0", stats, id_history

    seg_packets = {1: [None]*60, 2: [None]*60, 3: [None]*60, 4: [None]*60}
    t1 = time.monotonic()

    while time.monotonic() - t1 < timeout_s:
        pkt = read_packet(spi, delay_us)
        raw_id, seg, pnum = parse_packet_id(pkt)
        id_history.append((now_ms() - base_ms, raw_id, seg, pnum, pkt[:8]))

        if is_discard(raw_id):
            stats["discards"] += 1
            continue

        if seg not in (1, 2, 3, 4):
            stats["invalid_seg"] += 1
            continue

        if pnum >= 60:
            stats["bad_pnum"] += 1
            continue

        if seg_packets[seg][pnum] is not None:
            stats["duplicate"] += 1
            continue

        seg_packets[seg][pnum] = bytes(pkt[HEADER_SIZE:])
        stats["kept"] += 1
        stats["seg_fill"][seg] = sum(1 for x in seg_packets[seg] if x is not None)

        if verbose and stats["kept"] % 30 == 0:
            print(f"[CAP] kept={stats['kept']}/240 disc={stats['discards']} invseg={stats['invalid_seg']} dup={stats['duplicate']} fill={stats['seg_fill']}")

        if all(stats["seg_fill"][s] == 60 for s in (1, 2, 3, 4)):
            break

    missing = []
    for s in (1, 2, 3, 4):
        for p in range(60):
            if seg_packets[s][p] is None:
                missing.append((s, p))
    stats["missing"] = missing

    if missing:
        # More specific classification
        if stats["invalid_seg"] > 2000 and stats["kept"] < 20:
            return None, "INVALID_SEGMENT_DOMINANT_(CS/MISO/FRAMING)", stats, id_history
        return None, "INCOMPLETE_FRAME_TIMEOUT", stats, id_history

    # Assemble
    frame = np.zeros((120, 160), dtype=np.uint16)
    for seg in (1, 2, 3, 4):
        for p in range(60):
            payload = seg_packets[seg][p]
            if payload is None or len(payload) != (PACKET_SIZE - HEADER_SIZE):
                return None, f"PAYLOAD_SIZE_MISMATCH seg={seg} pkt={p}", stats, id_history

            row_in_seg = p // 2
            half = p % 2
            row = (seg - 1) * 30 + row_in_seg
            col0 = 0 if half == 0 else 80

            vals = np.frombuffer(payload, dtype=">u2")
            if vals.size != 80:
                return None, f"DECODE_SIZE_MISMATCH seg={seg} pkt={p} vals={vals.size}", stats, id_history

            frame[row, col0:col0+80] = vals.astype(np.uint16)

    return frame, "OK", stats, id_history


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="0.0")
    ap.add_argument("--hz", type=int, default=2_000_000)
    ap.add_argument("--mode", type=lambda x: int(x, 0), default=3)
    ap.add_argument("--delay-us", type=int, default=20, help="inter-transfer delay in microseconds (default 20)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--probe", type=int, default=400)
    ap.add_argument("--out", default="frame.pgm")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    bus_s, dev_s = args.device.split(".")
    bus, dev = int(bus_s), int(dev_s)

    print("=================================================")
    print("[START] Lepton VoSPI debug capture")
    print(f"  device=/dev/spidev{bus}.{dev}")
    print(f"  spi_mode={args.mode}  hz={args.hz}  delay_us={args.delay_us}")
    print(f"  timeout={args.timeout}s  probe_packets={args.probe}")
    print("=================================================")

    spi = open_spi(bus, dev, args.hz, args.mode)
    try:
        print("\n[PHASE 1] Probe stream...")
        pr = probe(spi, args.delay_us, args.probe)

        print("[PROBE] ok_packets=", pr["ok"], "discarded=", pr["discards"])
        print("[PROBE] seg_counts=", dict(pr["seg_counts"]))
        print("[PROBE] pnum(0..59)_ratio=", f"{pr['pnum_0_59_ratio']:.2f}")
        print(f"[PROBE] most_common_rawid=0x{pr['most_common_rawid']:04X}  stuck_ratio={pr['stuck_ratio']:.2f}")
        print("[PROBE] Recent headers:")
        print(dump_recent(pr["id_history"]))

        if pr["ok"] == 0 and pr["discards"] == args.probe:
            print("\n[FAIL] ALL_DISCARD: never saw a valid packet during probe.")
            print("Most likely: camera not streaming yet OR CS/CLK framing prevents valid headers.")
            print("Try: increase --delay-us (50), lower --hz (1000000), verify CS pin and MISO continuity.")
            # Still attempt capture once, because sometimes sync appears only later.

        if pr["stuck_ratio"] > 0.95:
            print("\n[WARN] STUCK_HEADER_PROBE: one raw_id dominates the probe window.")
            print("This strongly suggests CS framing issue or MISO not carrying real data.")

        print("\n[PHASE 2] Capture one 160x120 segmented frame...")
        frame, reason, stats, hist = capture_segmented_160x120(spi, args.delay_us, args.timeout, args.verbose)

        if frame is None:
            print("\n================ FAILURE SUMMARY ================")
            print("Reason:", reason)
            print("Stats:", stats)
            print("\nLast 20 headers:")
            print(dump_recent(hist))
            print("=================================================\n")

            if "INVALID_SEGMENT_DOMINANT" in reason or pr["stuck_ratio"] > 0.95:
                print("Most probable root cause: **CS framing / chip-select behavior** or **MISO not truly connected**.")
                print("Concrete checks:")
                print("  1) Confirm you're using CE0/CE1 that matches your wiring (GPIO8=CE0, GPIO7=CE1).")
                print("  2) Scope CS: it must pulse once per 164-byte transfer and go high between packets.")
                print("  3) Add more idle: try --delay-us 50 (or 100).")
                print("  4) Try slower clock: --hz 1000000.")
                print("  5) Verify MISO continuity end-to-end (camera MISO -> Pi GPIO9) and common ground.")
            else:
                print("Try: --hz 1000000 --delay-us 50 --timeout 12 --verbose")
            return 1

        print("[SUCCESS] Captured frame.")
        write_pgm_u16(args.out, frame)
        print(f"[OUT] Wrote {args.out} (160x120, 16-bit PGM)")
        print("Convert/view:")
        print(f"  convert {args.out} out.png")
        print(f"  eog out.png")
        return 0

    finally:
        spi.close()


if __name__ == "__main__":
    raise SystemExit(main())