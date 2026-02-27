#!/usr/bin/env python3
"""
FLIR Lepton (VoSPI) one-frame capture with HARD debugging.

Designed to answer, conclusively:
- Are we receiving valid VoSPI packets?
- Are we seeing a sane packet counter?
- Are we seeing segmented stream (Lepton 3.x: segments 1..4)?
- Can we lock to a frame boundary (seg=1, pkt=0) and collect a full frame?
- If not, WHERE does it fail (with clear prints + a final failure summary).

Works with Raspberry Pi spidev (e.g., /dev/spidev0.0), mode 3.

Outputs:
- frame.pgm (16-bit PGM) on success
- debug logs always

USAGE EXAMPLES:
  python3 lepton_debug_capture.py
  python3 lepton_debug_capture.py --hz 2000000 --timeout 8 --verbose
  python3 lepton_debug_capture.py --device 0.0 --out frame.pgm

NOTE:
- If you have Lepton 2.x (80x60), this script will detect "mostly segment=0" and will
  automatically fall back to a 1-segment capture mode (80x60).
"""

import argparse
import collections
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    import spidev
except ImportError:
    print("ERROR: spidev not installed. On Raspberry Pi OS: sudo apt install -y python3-spidev", file=sys.stderr)
    sys.exit(2)

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy not installed. Install: sudo apt install -y python3-numpy", file=sys.stderr)
    sys.exit(2)


# VoSPI constants
PACKET_SIZE = 164          # bytes per VoSPI packet (header+payload)
HEADER_SIZE = 4            # bytes: 2 for ID + 2 for CRC or reserved depending on module
INVALID_PKT_NUM = 0x0FFF   # lower 12 bits all ones => discard
DEFAULT_HZ = 4_000_000
DEFAULT_SPI_MODE = 0b11    # mode 3


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def parse_packet_id(pkt: List[int]) -> Tuple[int, int, int]:
    """
    Returns (raw_id, segment, packet_num).
    raw_id: 16-bit word formed from first 2 bytes.
    segment: upper 4 bits (0..15)
    packet_num: lower 12 bits (0..4095)
    """
    raw_id = (pkt[0] << 8) | pkt[1]
    segment = (raw_id >> 12) & 0xF
    packet_num = raw_id & 0x0FFF
    return raw_id, segment, packet_num


def is_discard(raw_id: int) -> bool:
    return (raw_id & 0x0FFF) == INVALID_PKT_NUM


def hexdump_ids(id_history: collections.deque) -> str:
    # Each entry: (t_ms, raw_id, seg, pkt)
    out = []
    for t_ms, raw_id, seg, pkt in list(id_history)[-20:]:
        out.append(f"{t_ms:>8}ms  id=0x{raw_id:04X}  seg={seg:>2}  pkt={pkt:>4}")
    return "\n".join(out) if out else "(none)"


def open_spi(bus: int, dev: int, hz: int, mode: int, verbose: bool) -> "spidev.SpiDev":
    spi = spidev.SpiDev()
    spi.open(bus, dev)
    spi.max_speed_hz = hz
    spi.mode = mode
    # bits_per_word is usually 8 by default; we keep it.
    if verbose:
        print(f"[SPI] Opened /dev/spidev{bus}.{dev}  mode={mode} (0b{mode:02b})  hz={hz}")
    return spi


def read_packet(spi: "spidev.SpiDev") -> List[int]:
    # Full-duplex read: clock out zeros to read PACKET_SIZE bytes.
    return spi.xfer2([0] * PACKET_SIZE)


def detect_stream_type(spi: "spidev.SpiDev", probe_packets: int, verbose: bool) -> Dict:
    """
    Probe the stream for a short window and infer:
    - do we see mostly segment 1..4? (Lepton 3.x)
    - do we see mostly segment 0? (often Lepton 2.x behavior in practice)
    - do packet numbers look like 0..59 patterns?
    - how many discards?
    """
    seg_counts = collections.Counter()
    pkt_counts = collections.Counter()
    discard = 0
    ok = 0
    id_history = collections.deque(maxlen=200)

    t0 = now_ms()
    for _ in range(probe_packets):
        pkt = read_packet(spi)
        raw_id, seg, pnum = parse_packet_id(pkt)
        id_history.append((now_ms() - t0, raw_id, seg, pnum))
        if is_discard(raw_id):
            discard += 1
            continue
        ok += 1
        seg_counts[seg] += 1
        pkt_counts[pnum] += 1

    # Heuristic inference
    seg_1234 = sum(seg_counts[s] for s in (1, 2, 3, 4))
    seg_0 = seg_counts[0]
    total_ok = max(ok, 1)

    if seg_1234 / total_ok > 0.70:
        stream = "SEGMENTED_4"   # Lepton 3.x-style
    elif seg_0 / total_ok > 0.70:
        stream = "SINGLE_SEG"    # likely Lepton 2.x-style (or segmented field not used)
    else:
        stream = "MIXED/UNKNOWN"

    # Packet numbers: see if 0..59 appear frequently
    pkt_0_59 = sum(pkt_counts[i] for i in range(60))
    pkt_0_59_ratio = pkt_0_59 / total_ok

    result = {
        "stream": stream,
        "ok": ok,
        "discard": discard,
        "seg_counts": seg_counts,
        "pkt_0_59_ratio": pkt_0_59_ratio,
        "id_history": id_history,
    }

    if verbose:
        print("[PROBE] Results:")
        print(f"  ok_packets={ok}  discarded={discard}")
        print(f"  segment_counts={dict(seg_counts)}")
        print(f"  pkt(0..59)_ratio={pkt_0_59_ratio:.2f}")
        print(f"  inferred_stream={stream}")
        if stream == "MIXED/UNKNOWN":
            print("  [PROBE WARN] Segment field not strongly consistent. This can happen if you're sampling mid-chaos or wiring/timing is bad.")
        print("  Last packet headers seen (most recent last):")
        print(hexdump_ids(id_history))

    return result


def wait_for_frame_start_segmented(
    spi: "spidev.SpiDev",
    timeout_s: float,
    verbose: bool,
) -> Tuple[bool, str, collections.deque]:
    """
    For 4-segment mode:
    Wait until we see seg=1, pkt=0 (start of a frame boundary).
    """
    id_history = collections.deque(maxlen=500)
    t0 = time.monotonic()
    base_ms = now_ms()

    while (time.monotonic() - t0) < timeout_s:
        pkt = read_packet(spi)
        raw_id, seg, pnum = parse_packet_id(pkt)
        id_history.append((now_ms() - base_ms, raw_id, seg, pnum))

        if is_discard(raw_id):
            continue

        if seg == 1 and pnum == 0:
            if verbose:
                print(f"[SYNC] Locked to frame start: seg=1 pkt=0 (id=0x{raw_id:04X})")
            return True, "OK", id_history

    return False, "TIMEOUT_WAITING_FOR_SEG1_PKT0", id_history


def capture_one_frame_segmented(
    spi: "spidev.SpiDev",
    timeout_s: float,
    verbose: bool,
) -> Tuple[Optional[np.ndarray], str, Dict, collections.deque]:
    """
    Capture one 160x120 frame for Lepton 3.x (4 segments, 60 packets each).
    Returns (frame_or_none, status, stats, id_history).
    """
    id_history = collections.deque(maxlen=2000)
    stats = {
        "discards": 0,
        "packets_kept": 0,
        "bad_order": 0,
        "duplicate": 0,
        "missing": [],
        "seg_complete": {1: 0, 2: 0, 3: 0, 4: 0},
    }

    ok, reason, sync_history = wait_for_frame_start_segmented(spi, timeout_s, verbose)
    id_history.extend(sync_history)
    if not ok:
        return None, reason, stats, id_history

    # Store raw packets payloads by [seg][pnum]
    seg_packets: Dict[int, List[Optional[bytes]]] = {1: [None]*60, 2: [None]*60, 3: [None]*60, 4: [None]*60}

    start = time.monotonic()
    base_ms = now_ms()
    # We already consumed seg=1 pkt=0 in sync stage; store it by re-reading? No—sync stage already read it but didn't keep payload.
    # Easiest: after sync, continue and accept that we might have missed that exact packet; we will keep reading until all 240 are filled.
    # This is robust because we're already aligned to the boundary.
    if verbose:
        print("[CAPTURE] Collecting 4 segments × 60 packets...")

    last_seen = {1: -1, 2: -1, 3: -1, 4: -1}
    while (time.monotonic() - start) < timeout_s:
        pkt = read_packet(spi)
        raw_id, seg, pnum = parse_packet_id(pkt)
        id_history.append((now_ms() - base_ms, raw_id, seg, pnum))

        if is_discard(raw_id):
            stats["discards"] += 1
            continue

        if seg not in (1, 2, 3, 4):
            # For Lepton 3.x, seg should be 1..4. seg=0 or weird means we lost alignment or stream is not segmented.
            # We don't immediately fail; we count as bad_order and continue until timeout.
            stats["bad_order"] += 1
            continue

        if pnum >= 60:
            stats["bad_order"] += 1
            continue

        if seg_packets[seg][pnum] is not None:
            stats["duplicate"] += 1
            continue

        # Keep payload starting after 4-byte header
        seg_packets[seg][pnum] = bytes(pkt[HEADER_SIZE:])
        stats["packets_kept"] += 1

        # Ordering hint: packet numbers should monotonically increase within a segment
        if pnum < last_seen[seg]:
            stats["bad_order"] += 1
        last_seen[seg] = pnum

        # Update completeness counts
        stats["seg_complete"][seg] = sum(1 for x in seg_packets[seg] if x is not None)

        if verbose and (stats["packets_kept"] % 30 == 0):
            print(f"[CAPTURE] kept={stats['packets_kept']}/240  discards={stats['discards']}  bad_order={stats['bad_order']}  dup={stats['duplicate']}  seg_fill={stats['seg_complete']}")

        if stats["packets_kept"] >= 240 and all(stats["seg_complete"][s] == 60 for s in (1, 2, 3, 4)):
            if verbose:
                print("[CAPTURE] All segments complete.")
            break

    # Verify completeness
    missing = []
    for s in (1, 2, 3, 4):
        for p in range(60):
            if seg_packets[s][p] is None:
                missing.append((s, p))
    stats["missing"] = missing

    if missing:
        # Provide a *precise* failure reason
        if any(stats["seg_complete"][s] == 0 for s in (1, 2, 3, 4)):
            reason = "FAILED_NO_PACKETS_IN_ONE_OR_MORE_SEGMENTS"
        else:
            reason = "FAILED_INCOMPLETE_FRAME_TIMEOUT"
        return None, reason, stats, id_history

    # Assemble 160x120, 16-bit big-endian values per pixel
    frame = np.zeros((120, 160), dtype=np.uint16)

    # For Lepton 3.x: each segment covers 30 rows; within each row: two packets (left 80, right 80)
    # packet p: row_in_seg = p // 2, half = p % 2
    # global_row = (seg-1)*30 + row_in_seg
    for seg in (1, 2, 3, 4):
        for p in range(60):
            payload = seg_packets[seg][p]
            # payload length should be 160 bytes (80 pixels * 2 bytes)
            if payload is None or len(payload) != (PACKET_SIZE - HEADER_SIZE):
                return None, f"ASSEMBLY_PAYLOAD_SIZE_MISMATCH seg={seg} pkt={p} len={0 if payload is None else len(payload)}", stats, id_history

            row_in_seg = p // 2
            half = p % 2
            global_row = (seg - 1) * 30 + row_in_seg
            col0 = 0 if half == 0 else 80

            # Convert big-endian u16
            row_pixels = np.frombuffer(payload, dtype=">u2")  # 80 values
            if row_pixels.size != 80:
                return None, f"ASSEMBLY_DECODE_MISMATCH seg={seg} pkt={p} values={row_pixels.size}", stats, id_history

            frame[global_row, col0:col0+80] = row_pixels.astype(np.uint16)

    return frame, "OK", stats, id_history


def wait_for_frame_start_single(
    spi: "spidev.SpiDev",
    timeout_s: float,
    verbose: bool,
) -> Tuple[bool, str, collections.deque]:
    """
    For single-segment mode:
    Wait until pkt=0 (and seg often 0).
    """
    id_history = collections.deque(maxlen=500)
    t0 = time.monotonic()
    base_ms = now_ms()

    while (time.monotonic() - t0) < timeout_s:
        pkt = read_packet(spi)
        raw_id, seg, pnum = parse_packet_id(pkt)
        id_history.append((now_ms() - base_ms, raw_id, seg, pnum))
        if is_discard(raw_id):
            continue
        if pnum == 0:
            if verbose:
                print(f"[SYNC] Locked to frame start (single-seg): seg={seg} pkt=0 (id=0x{raw_id:04X})")
            return True, "OK", id_history

    return False, "TIMEOUT_WAITING_FOR_PKT0", id_history


def capture_one_frame_single(
    spi: "spidev.SpiDev",
    timeout_s: float,
    verbose: bool,
) -> Tuple[Optional[np.ndarray], str, Dict, collections.deque]:
    """
    Capture one 80x60 frame for Lepton 2.x-ish behavior (1 segment, 60 packets).
    (Many Lepton 2.x streams present 60 packets per frame.)
    """
    id_history = collections.deque(maxlen=2000)
    stats = {
        "discards": 0,
        "packets_kept": 0,
        "bad_order": 0,
        "duplicate": 0,
        "missing": [],
    }

    ok, reason, sync_history = wait_for_frame_start_single(spi, timeout_s, verbose)
    id_history.extend(sync_history)
    if not ok:
        return None, reason, stats, id_history

    packets: List[Optional[bytes]] = [None] * 60
    start = time.monotonic()
    base_ms = now_ms()
    last_seen = -1

    if verbose:
        print("[CAPTURE] Collecting 1 segment × 60 packets (80x60)...")

    while (time.monotonic() - start) < timeout_s:
        pkt = read_packet(spi)
        raw_id, seg, pnum = parse_packet_id(pkt)
        id_history.append((now_ms() - base_ms, raw_id, seg, pnum))

        if is_discard(raw_id):
            stats["discards"] += 1
            continue
        if pnum >= 60:
            stats["bad_order"] += 1
            continue
        if packets[pnum] is not None:
            stats["duplicate"] += 1
            continue

        packets[pnum] = bytes(pkt[HEADER_SIZE:])
        stats["packets_kept"] += 1
        if pnum < last_seen:
            stats["bad_order"] += 1
        last_seen = pnum

        if verbose and (stats["packets_kept"] % 15 == 0):
            print(f"[CAPTURE] kept={stats['packets_kept']}/60  discards={stats['discards']}  bad_order={stats['bad_order']}  dup={stats['duplicate']}")

        if stats["packets_kept"] == 60:
            break

    missing = [p for p in range(60) if packets[p] is None]
    stats["missing"] = missing
    if missing:
        return None, "FAILED_INCOMPLETE_FRAME_TIMEOUT", stats, id_history

    frame = np.zeros((60, 80), dtype=np.uint16)
    for p in range(60):
        payload = packets[p]
        if payload is None or len(payload) != (PACKET_SIZE - HEADER_SIZE):
            return None, f"ASSEMBLY_PAYLOAD_SIZE_MISMATCH pkt={p} len={0 if payload is None else len(payload)}", stats, id_history

        row = p
        row_pixels = np.frombuffer(payload, dtype=">u2")  # should be 80 values
        if row_pixels.size != 80:
            return None, f"ASSEMBLY_DECODE_MISMATCH pkt={p} values={row_pixels.size}", stats, id_history
        frame[row, :] = row_pixels.astype(np.uint16)

    return frame, "OK", stats, id_history


def write_pgm_u16(path: str, frame_u16: np.ndarray, verbose: bool) -> None:
    """
    Write a 16-bit PGM (binary P5) with maxval 65535.
    PGM expects big-endian for 16-bit; our array is host-endian, so byteswap to big-endian.
    """
    h, w = frame_u16.shape
    with open(path, "wb") as f:
        header = f"P5\n{w} {h}\n65535\n".encode("ascii")
        f.write(header)
        # ensure big-endian on disk
        frame_u16.astype(np.uint16).byteswap().tofile(f)
    if verbose:
        print(f"[OUT] Wrote {path} ({w}x{h}, 16-bit PGM)")


def print_failure_summary(reason: str, stats: Dict, id_history: collections.deque) -> None:
    print("\n================ FAILURE SUMMARY ================")
    print(f"Reason: {reason}")
    if stats:
        print("Stats:")
        for k, v in stats.items():
            if k == "missing":
                # show only a small sample; missing can be large
                if isinstance(v, list) and len(v) > 30:
                    print(f"  missing: {len(v)} items (showing first 30): {v[:30]}")
                else:
                    print(f"  missing: {v}")
            else:
                print(f"  {k}: {v}")
    print("\nLast 20 packet headers observed:")
    print(hexdump_ids(id_history))
    print("=================================================\n")

    # Actionable next hints based on failure class
    print("Next-step interpretation hints:")
    if reason.startswith("TIMEOUT_WAITING_FOR_"):
        print("- You never saw the expected frame boundary condition.")
        print("  Common causes:")
        print("   * wrong SPI mode (should be MODE 3)")
        print("   * CS not toggling / wrong chip select pin")
        print("   * too-high SPI clock or signal integrity issue")
        print("   * Lepton not streaming (needs proper power-up / reset / I2C init depending on module)")
    elif reason == "FAILED_NO_PACKETS_IN_ONE_OR_MORE_SEGMENTS":
        print("- You saw some valid segment data but at least one segment never produced packets.")
        print("  Common causes:")
        print("   * treating Lepton 2.x as 3.x (or vice-versa)")
        print("   * losing sync mid-frame (try lower hz, shorten wiring, ensure clean GND)")
        print("   * CS framing issues (packet boundaries getting corrupted)")
    elif reason == "FAILED_INCOMPLETE_FRAME_TIMEOUT":
        print("- You started capturing but couldn't collect all required packets in time.")
        print("  Common causes:")
        print("   * discards too high (timing/noise)")
        print("   * clock too fast; try --hz 2000000")
        print("   * stream out-of-order or duplicated (CS/timing)")
    elif reason.startswith("ASSEMBLY_"):
        print("- Packet payload sizes/decodes are inconsistent—this usually indicates corrupted packet reads.")
        print("  Try lowering SPI clock, shortening wires, confirming ground and level compatibility.")
    else:
        print("- See the header dump above; segment and packet numbers should be stable and patterned.")


def main():
    ap = argparse.ArgumentParser(description="FLIR Lepton VoSPI one-frame capture with debugging.")
    ap.add_argument("--device", default="0.0", help="spidev bus.device (default: 0.0)")
    ap.add_argument("--hz", type=int, default=DEFAULT_HZ, help=f"SPI clock Hz (default: {DEFAULT_HZ})")
    ap.add_argument("--mode", type=lambda x: int(x, 0), default=DEFAULT_SPI_MODE, help="SPI mode (default: 3). Accepts 3 or 0b11.")
    ap.add_argument("--timeout", type=float, default=6.0, help="Timeout seconds per major phase (default: 6.0)")
    ap.add_argument("--probe", type=int, default=400, help="Number of probe packets to infer stream type (default: 400)")
    ap.add_argument("--out", default="frame.pgm", help="Output PGM path (default: frame.pgm)")
    ap.add_argument("--verbose", action="store_true", help="More frequent progress prints")
    args = ap.parse_args()

    # Parse device like "0.0"
    try:
        bus_s, dev_s = args.device.split(".")
        bus = int(bus_s)
        dev = int(dev_s)
    except Exception:
        print("ERROR: --device must be like '0.0' or '0.1'", file=sys.stderr)
        sys.exit(2)

    print("=================================================")
    print("[START] Lepton VoSPI debug capture")
    print(f"  device=/dev/spidev{bus}.{dev}")
    print(f"  spi_mode={args.mode}  hz={args.hz}")
    print(f"  timeout={args.timeout}s  probe_packets={args.probe}")
    print("=================================================")

    spi = None
    try:
        spi = open_spi(bus, dev, args.hz, args.mode, verbose=True)

        print("\n[PHASE 1] Probing stream to infer Lepton type...")
        probe = detect_stream_type(spi, args.probe, verbose=True)
        stream = probe["stream"]

        # If probe suggests mixed/unknown, still attempt segmented first (most likely for Lepton 3.x UWFOV),
        # but we will clearly log the ambiguity.
        if stream == "MIXED/UNKNOWN":
            print("\n[WARN] Stream inference unclear; attempting SEGMENTED_4 first (common for Lepton 3.x UWFOV).")

        # Choose capture order: segmented first unless strongly single
        attempts = []
        if stream == "SINGLE_SEG":
            attempts = ["SINGLE_SEG", "SEGMENTED_4"]
        else:
            attempts = ["SEGMENTED_4", "SINGLE_SEG"]

        for attempt in attempts:
            print(f"\n[PHASE 2] Attempting capture mode: {attempt}")
            if attempt == "SEGMENTED_4":
                frame, reason, stats, hist = capture_one_frame_segmented(spi, args.timeout, verbose=args.verbose)
                if frame is not None:
                    print("[SUCCESS] Captured full 160x120 frame (segmented).")
                    write_pgm_u16(args.out, frame, verbose=True)
                    print("[DONE] Open the PGM with eog, ImageMagick, or convert to PNG:")
                    print(f"       convert {args.out} out.png")
                    return 0
                else:
                    print_failure_summary(reason, stats, hist)
            else:
                frame, reason, stats, hist = capture_one_frame_single(spi, args.timeout, verbose=args.verbose)
                if frame is not None:
                    print("[SUCCESS] Captured full 80x60 frame (single-segment).")
                    write_pgm_u16(args.out, frame, verbose=True)
                    print("[DONE] Open the PGM with eog, ImageMagick, or convert to PNG:")
                    print(f"       convert {args.out} out.png")
                    return 0
                else:
                    print_failure_summary(reason, stats, hist)

        print("[FINAL] All capture attempts failed.")
        print("Most likely causes (ranked):")
        print("  1) SPI signal integrity / timing (try --hz 2000000, short wires, strong GND)")
        print("  2) CS framing issue (wrong CS pin, CS not asserted per packet)")
        print("  3) Stream type mismatch (Lepton 2.x vs 3.x handling)")
        print("  4) Camera not actually streaming (power/reset/I2C init path)")
        return 1

    except KeyboardInterrupt:
        print("\n[ABORT] KeyboardInterrupt")
        return 130
    except Exception as e:
        print("\n[ERROR] Unhandled exception:")
        print(repr(e))
        return 3
    finally:
        if spi is not None:
            try:
                spi.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())