#!/usr/bin/env python3
"""
Lepton VoSPI debug capture with *adaptive discard backoff* + *stability gate*.

Goal: capture ONE clean 160x120 frame OR print exactly why it can't.

Key behaviors:
- SPI transfer has delay_us between transfers (CS idle gap)
- If packet is DISCARD (pkt=0xFFF), we sleep discard_sleep_us (camera "catch up")
- After syncing to seg=1 pkt=0, we require N consecutive "good" packets before collecting

This targets the exact failure mode you showed: huge discards + many invalid segments.
"""

import argparse, sys, time, collections
from typing import List, Tuple, Optional, Dict

import spidev
import numpy as np

PACKET_SIZE = 164
HEADER_SIZE = 4
INVALID_PKT_NUM = 0x0FFF

def parse_id(pkt: List[int]) -> Tuple[int,int,int]:
    raw = (pkt[0] << 8) | pkt[1]
    seg = (raw >> 12) & 0xF
    pnum = raw & 0x0FFF
    return raw, seg, pnum

def is_discard(raw: int) -> bool:
    return (raw & 0x0FFF) == INVALID_PKT_NUM

def read_pkt(spi: spidev.SpiDev, delay_us: int) -> List[int]:
    return spi.xfer2([0]*PACKET_SIZE, 0, delay_us, 0)

def dump_last(hist: collections.deque, n=20) -> str:
    lines=[]
    for t_ms, raw, seg, pnum, h8 in list(hist)[-n:]:
        fb=" ".join(f"{b:02X}" for b in h8)
        lines.append(f"{t_ms:>8}ms id=0x{raw:04X} seg={seg:>2} pkt={pnum:>4} hdr8=[{fb}]")
    return "\n".join(lines) if lines else "(none)"

def capture_one(
    spi: spidev.SpiDev,
    delay_us: int,
    discard_sleep_us: int,
    timeout_s: float,
    gate_good_run: int,
    verbose: bool
) -> Tuple[Optional[np.ndarray], str, Dict, collections.deque]:

    hist = collections.deque(maxlen=4000)
    stats = {
        "discards": 0,
        "invalid_seg": 0,
        "bad_pnum": 0,
        "kept": 0,
        "duplicates": 0,
        "resyncs": 0,
        "gate_failures": 0,
        "seg_fill": {1:0,2:0,3:0,4:0},
        "missing": [],
    }

    base_ms = int(time.monotonic()*1000)

    def log(pkt):
        raw, seg, pnum = parse_id(pkt)
        hist.append((int(time.monotonic()*1000)-base_ms, raw, seg, pnum, pkt[:8]))
        return raw, seg, pnum

    def good_packet(raw, seg, pnum) -> bool:
        return (not is_discard(raw)) and (seg in (1,2,3,4)) and (0 <= pnum < 60)

    seg_packets = {1:[None]*60,2:[None]*60,3:[None]*60,4:[None]*60}

    t_start = time.monotonic()
    while time.monotonic() - t_start < timeout_s:

        # ---- SYNC to seg=1 pkt=0 ----
        synced = False
        t_sync = time.monotonic()
        while time.monotonic() - t_sync < timeout_s/2:
            pkt = read_pkt(spi, delay_us)
            raw, seg, pnum = log(pkt)

            if is_discard(raw):
                stats["discards"] += 1
                if discard_sleep_us:
                    time.sleep(discard_sleep_us/1e6)
                continue

            if seg == 1 and pnum == 0:
                synced = True
                if verbose:
                    print(f"[SYNC] seg=1 pkt=0 id=0x{raw:04X}")
                break

        if not synced:
            return None, "TIMEOUT_SYNC_SEG1_PKT0", stats, hist

        # ---- STABILITY GATE: require N consecutive good packets ----
        run = 0
        t_gate = time.monotonic()
        while time.monotonic() - t_gate < 1.5:  # short gate window
            pkt = read_pkt(spi, delay_us)
            raw, seg, pnum = log(pkt)

            if is_discard(raw):
                stats["discards"] += 1
                run = 0
                if discard_sleep_us:
                    time.sleep(discard_sleep_us/1e6)
                continue

            if not good_packet(raw, seg, pnum):
                if seg not in (1,2,3,4):
                    stats["invalid_seg"] += 1
                elif pnum >= 60:
                    stats["bad_pnum"] += 1
                run = 0
                continue

            run += 1
            if run >= gate_good_run:
                if verbose:
                    print(f"[GATE] Stream stable: {gate_good_run} good packets in a row.")
                break

        if run < gate_good_run:
            stats["gate_failures"] += 1
            stats["resyncs"] += 1
            if verbose:
                print("[GATE] Failed to get stable run, resyncing...")
            continue

        # ---- COLLECT until complete or timeout slice ----
        if verbose:
            print("[CAPTURE] Collecting packets...")

        t_cap = time.monotonic()
        while time.monotonic() - t_cap < timeout_s:
            pkt = read_pkt(spi, delay_us)
            raw, seg, pnum = log(pkt)

            if is_discard(raw):
                stats["discards"] += 1
                if discard_sleep_us:
                    time.sleep(discard_sleep_us/1e6)
                continue

            if seg not in (1,2,3,4):
                stats["invalid_seg"] += 1
                continue
            if pnum >= 60:
                stats["bad_pnum"] += 1
                continue

            if seg_packets[seg][pnum] is not None:
                stats["duplicates"] += 1
                continue

            seg_packets[seg][pnum] = bytes(pkt[HEADER_SIZE:])
            stats["kept"] += 1
            stats["seg_fill"][seg] = sum(1 for x in seg_packets[seg] if x is not None)

            if verbose and stats["kept"] % 25 == 0:
                print(f"[CAP] kept={stats['kept']}/240 disc={stats['discards']} invseg={stats['invalid_seg']} dup={stats['duplicates']} fill={stats['seg_fill']}")

            if all(stats["seg_fill"][s] == 60 for s in (1,2,3,4)):
                if verbose:
                    print("[CAPTURE] Frame complete.")
                break

        if all(stats["seg_fill"][s] == 60 for s in (1,2,3,4)):
            # assemble
            frame = np.zeros((120,160), dtype=np.uint16)
            for seg in (1,2,3,4):
                for p in range(60):
                    payload = seg_packets[seg][p]
                    if payload is None or len(payload) != (PACKET_SIZE-HEADER_SIZE):
                        return None, f"PAYLOAD_MISMATCH seg={seg} pkt={p}", stats, hist
                    row_in_seg = p//2
                    half = p%2
                    row = (seg-1)*30 + row_in_seg
                    col0 = 0 if half==0 else 80
                    vals = np.frombuffer(payload, dtype=">u2")
                    if vals.size != 80:
                        return None, f"DECODE_MISMATCH seg={seg} pkt={p} vals={vals.size}", stats, hist
                    frame[row, col0:col0+80] = vals.astype(np.uint16)
            return frame, "OK", stats, hist

        # if we reach here: capture slice timed out -> resync and try again (within overall timeout)
        stats["resyncs"] += 1
        if verbose:
            print("[CAPTURE] Timed out before full frame, resyncing...")

    return None, "OVERALL_TIMEOUT_NO_COMPLETE_FRAME", stats, hist

def write_pgm(path: str, frame: np.ndarray):
    h,w = frame.shape
    with open(path,"wb") as f:
        f.write(f"P5\n{w} {h}\n65535\n".encode("ascii"))
        frame.astype(np.uint16).byteswap().tofile(f)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="0.0")
    ap.add_argument("--hz", type=int, default=1_000_000)
    ap.add_argument("--mode", type=int, default=3)
    ap.add_argument("--delay-us", type=int, default=200)
    ap.add_argument("--discard-sleep-us", type=int, default=800, help="extra sleep after discard packets (default 800us)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--gate-good-run", type=int, default=12, help="good packets in a row before collecting (default 12)")
    ap.add_argument("--out", default="frame.pgm")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    bus_s, dev_s = args.device.split(".")
    bus, dev = int(bus_s), int(dev_s)

    print("=================================================")
    print("[START] Lepton VoSPI debug capture (adaptive)")
    print(f"  device=/dev/spidev{bus}.{dev}")
    print(f"  spi_mode={args.mode} hz={args.hz} delay_us={args.delay_us} discard_sleep_us={args.discard_sleep_us}")
    print(f"  timeout={args.timeout}s gate_good_run={args.gate_good_run}")
    print("=================================================")

    spi = spidev.SpiDev()
    spi.open(bus, dev)
    spi.max_speed_hz = args.hz
    spi.mode = args.mode

    try:
        frame, reason, stats, hist = capture_one(
            spi, args.delay_us, args.discard_sleep_us, args.timeout, args.gate_good_run, args.verbose
        )
        if frame is None:
            print("\n================ FAILURE SUMMARY ================")
            print("Reason:", reason)
            print("Stats:", stats)
            print("\nLast 20 headers:")
            print(dump_last(hist))
            print("=================================================\n")

            # Strong interpretation
            if stats["discards"] > 2000 and stats["kept"] < 60:
                print("Interpretation: You are living in DISCARD packets. Increase discard_sleep_us (e.g., 2000) and/or delay_us (e.g., 500).")
            if stats["invalid_seg"] > 500:
                print("Interpretation: Many invalid segments -> CS framing / timing instability. Increase delay_us, verify CS behavior, and slow SPI clock.")
            print("Next runs to try:")
            print("  python3 lepton_vospi_debug2.py --hz 1000000 --delay-us 500 --discard-sleep-us 2000 --timeout 20 --verbose")
            print("  python3 lepton_vospi_debug2.py --hz 500000  --delay-us 800 --discard-sleep-us 3000 --timeout 25 --verbose")
            return 1

        write_pgm(args.out, frame)
        print(f"[SUCCESS] Wrote {args.out} (160x120 16-bit PGM)")
        print("Convert/view:")
        print(f"  convert {args.out} out.png && eog out.png")
        return 0

    finally:
        spi.close()

if __name__ == "__main__":
    raise SystemExit(main())