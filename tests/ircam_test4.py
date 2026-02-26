#!/usr/bin/env python3
"""
Lepton 3.x (UWFOV) VoSPI frame grabber for Raspberry Pi
- Improved synchronization & robustness
- Uses spidev.xfer2() for clocked reads
- Writes a few frames to disk (PNG + NPY), no GUI

Wiring (matches your description):
  SDA  -> GPIO2  (pin 3)
  SCL  -> GPIO3  (pin 5)
  SCLK -> GPIO11 (pin 23)
  MOSI -> GPIO10 (pin 19)
  MISO -> GPIO9  (pin 21)
  CS   -> GPIO8  (pin 24)  => /dev/spidev0.0
"""

import os
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np

# Optional: OpenCV for PNG writes (no imshow used)
try:
    import cv2
    HAS_CV2 = True
except Exception as e:
    print("[WARN] OpenCV import failed, PNG writing disabled:", repr(e))
    HAS_CV2 = False

# Optional: I2C presence check (won't break if not installed)
try:
    import smbus2
    HAS_I2C = True
except Exception:
    HAS_I2C = False

import spidev


# -----------------------------
# Configuration
# -----------------------------
SPI_BUS = 0
SPI_DEVICE = 0            # CE0 (GPIO8) -> /dev/spidev0.0
SPI_MODE = 3              # Lepton commonly uses SPI mode 3 for VoSPI
SPI_SPEED_HZ = 8_000_000  # start conservative; you can try 12-16MHz later

# Lepton VoSPI packet constants
PACKET_SIZE = 164         # 4-byte header + 160-byte payload
SEGMENTS_PER_FRAME = 4
PACKETS_PER_SEGMENT = 60  # Lepton 3.x segmented mode
VALID_PACKET_ROWS_PER_SEGMENT = 30  # first 30 packets carry image rows
WORDS_PER_PACKET = 80     # 160 bytes payload / 2

# Resulting image size from 4 segments * 30 rows each
HEIGHT = SEGMENTS_PER_FRAME * VALID_PACKET_ROWS_PER_SEGMENT  # 120
WIDTH = WORDS_PER_PACKET * 2                                 # 160 pixels

# How many frames to save
NUM_FRAMES_TO_SAVE = 5
OUTPUT_DIR = "frames"

# Timing / retries
FRAME_TIMEOUT_S = 2.0          # time budget per frame
MAX_RESYNC_ATTEMPTS = 30       # before giving up


# -----------------------------
# Helpers
# -----------------------------
def ensure_output_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def open_spi() -> spidev.SpiDev:
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEVICE)
    spi.mode = SPI_MODE
    spi.max_speed_hz = SPI_SPEED_HZ
    # bits_per_word is usually 8 by default; leave it
    print(f"[SPI] Opened spidev{SPI_BUS}.{SPI_DEVICE} mode={spi.mode} speed={spi.max_speed_hz}Hz")
    return spi


def try_i2c_presence_check() -> None:
    """Lightweight 'is the device there' sanity check. Does not configure the camera."""
    if not HAS_I2C:
        print("[I2C] smbus2 not installed; skipping I2C presence check.")
        return

    I2C_BUS = 1
    LEPTON_ADDR = 0x2A

    try:
        bus = smbus2.SMBus(I2C_BUS)
    except Exception as e:
        print("[I2C] Could not open I2C bus:", repr(e))
        return

    try:
        # Read a couple bytes from an arbitrary register address (best-effort)
        # Many breakouts will ACK the address even if this register isn't meaningful.
        # The key is: do we get an ACK / not-bus-error.
        # We'll do a "write register pointer" then "read" pattern.
        reg = 0x0002  # small address; just to elicit an ACK
        bus.write_i2c_block_data(LEPTON_ADDR, (reg >> 8) & 0xFF, [reg & 0xFF])
        data = bus.read_i2c_block_data(LEPTON_ADDR, 0x00, 2)
        print(f"[I2C] Device at 0x{LEPTON_ADDR:02X} responded, sample read={data}")
    except Exception as e:
        print("[I2C] Presence check failed (may still be OK if CCI protocol differs):", repr(e))
    finally:
        try:
            bus.close()
        except Exception:
            pass


def read_packet(spi: spidev.SpiDev) -> bytes:
    """
    Read exactly one VoSPI packet (164 bytes).

    Using xfer2 clocks out bytes reliably across a lot of Pi + kernel combos.
    """
    return bytes(spi.xfer2([0] * PACKET_SIZE))


def is_discard(packet: bytes) -> bool:
    """
    Discard packets have a header where the first byte's low nibble == 0xF.
    Many implementations treat packet[0] == 0x0F as discard; the low nibble test
    is slightly more general.
    """
    return (packet[0] & 0x0F) == 0x0F


def packet_segment(packet: bytes) -> int:
    """Segment number for Lepton 3.x is in the high nibble of byte 0."""
    return (packet[0] >> 4) & 0x0F


def packet_number(packet: bytes) -> int:
    """Packet number (0..59) in byte 1."""
    return packet[1]


def payload_to_words(packet: bytes) -> np.ndarray:
    """Convert payload to 80 big-endian uint16 words."""
    return np.frombuffer(packet[4:], dtype=">u2")


@dataclass
class FrameStats:
    discards: int = 0
    seg0: int = 0
    invalid_seg: int = 0
    out_of_order: int = 0
    packets_seen: int = 0
    segments_seen: Counter = None

    def __post_init__(self):
        if self.segments_seen is None:
            self.segments_seen = Counter()


def hunt_frame_start(spi: spidev.SpiDev, max_packets: int = 5000, verbose: bool = True) -> tuple[bytes, FrameStats] | tuple[None, FrameStats]:
    """
    Hunt for a clean start-of-frame packet:
      - not discard
      - segment == 1
      - packet_number == 0

    Returns (packet, stats) or (None, stats) if not found in max_packets reads.
    """
    stats = FrameStats()

    for i in range(max_packets):
        p = read_packet(spi)
        stats.packets_seen += 1

        if is_discard(p):
            stats.discards += 1
            continue

        seg = packet_segment(p)
        pkt = packet_number(p)
        stats.segments_seen[seg] += 1

        if seg == 0:
            stats.seg0 += 1
            continue

        if seg < 1 or seg > 4:
            stats.invalid_seg += 1
            continue

        if seg == 1 and pkt == 0:
            if verbose:
                print(f"[SYNC] Found frame start after {i+1} packets "
                      f"(discards={stats.discards}, seg0={stats.seg0}, invalid_seg={stats.invalid_seg}, seg_counts={dict(stats.segments_seen)})")
            return p, stats

    if verbose:
        print(f"[SYNC] Failed to find frame start in {max_packets} packets "
              f"(discards={stats.discards}, seg0={stats.seg0}, invalid_seg={stats.invalid_seg}, seg_counts={dict(stats.segments_seen)})")
    return None, stats


def read_full_frame(spi: spidev.SpiDev, timeout_s: float = FRAME_TIMEOUT_S, verbose: bool = True) -> tuple[np.ndarray, FrameStats] | tuple[None, FrameStats]:
    """
    Read a full 160x120 frame (raw uint16) using segmented VoSPI:
      - Wait for (seg=1, pkt=0)
      - Then read remaining packets for seg1..seg4 (60 packets each)
      - Use only pkt 0..29 for image rows; ignore pkt 30..59 (often telemetry/extra)

    If packet order breaks, we abort and resync.
    """
    deadline = time.time() + timeout_s
    stats = FrameStats()

    # Find the start-of-frame packet first
    start_packet, sync_stats = hunt_frame_start(spi, max_packets=5000, verbose=verbose)
    # merge stats
    stats.discards += sync_stats.discards
    stats.seg0 += sync_stats.seg0
    stats.invalid_seg += sync_stats.invalid_seg
    stats.packets_seen += sync_stats.packets_seen
    stats.segments_seen.update(sync_stats.segments_seen)

    if start_packet is None:
        return None, stats

    # Prepare output frame
    frame = np.zeros((HEIGHT, WIDTH), dtype=np.uint16)

    # Expecting to read seg 1..4 in order, pkt 0..59
    expected_seg = 1
    expected_pkt = 0

    def consume_packet(p: bytes) -> bool:
        nonlocal expected_seg, expected_pkt, frame, stats

        if is_discard(p):
            stats.discards += 1
            return True  # keep going, discards are normal occasionally

        seg = packet_segment(p)
        pkt = packet_number(p)
        stats.segments_seen[seg] += 1

        if seg == 0:
            stats.seg0 += 1
            return True

        if seg < 1 or seg > 4:
            stats.invalid_seg += 1
            return True

        # Order check
        if seg != expected_seg or pkt != expected_pkt:
            stats.out_of_order += 1
            if verbose:
                print(f"[FRAME] Out-of-order packet: got seg={seg},pkt={pkt} but expected seg={expected_seg},pkt={expected_pkt}. Resyncing.")
            return False  # abort

        # Store image row for pkt 0..29 only
        if pkt < VALID_PACKET_ROWS_PER_SEGMENT:
            row = (seg - 1) * VALID_PACKET_ROWS_PER_SEGMENT + pkt
            words = payload_to_words(p)
            if words.shape[0] != WORDS_PER_PACKET:
                if verbose:
                    print(f"[FRAME] Bad payload words count: {words.shape[0]} (expected {WORDS_PER_PACKET})")
                return False
            # Convert 80 words => 160 pixels
            # Each word corresponds to one pixel (16-bit container with RAW14 typically)
            frame[row, :] = words

        # Advance expected
        expected_pkt += 1
        if expected_pkt >= PACKETS_PER_SEGMENT:
            expected_pkt = 0
            expected_seg += 1

        return True

    # Consume the start packet as the first expected packet
    if not consume_packet(start_packet):
        return None, stats

    # Now consume until expected_seg == 5 (done) or timeout
    while expected_seg <= SEGMENTS_PER_FRAME:
        if time.time() > deadline:
            if verbose:
                print(f"[FRAME] Timeout while reading frame. expected_seg={expected_seg}, expected_pkt={expected_pkt}, "
                      f"stats: discards={stats.discards}, seg0={stats.seg0}, invalid_seg={stats.invalid_seg}, out_of_order={stats.out_of_order}, seg_counts={dict(stats.segments_seen)}")
            return None, stats

        p = read_packet(spi)
        stats.packets_seen += 1
        if not consume_packet(p):
            return None, stats

    return frame, stats


def normalize_to_8bit(frame16: np.ndarray) -> np.ndarray:
    """
    Normalize a uint16 frame to 8-bit for PNG preview.
    Uses min/max normalization for visibility.
    """
    # Avoid divide-by-zero
    mn = int(frame16.min())
    mx = int(frame16.max())
    if mx <= mn:
        return np.zeros(frame16.shape, dtype=np.uint8)

    # Scale to 0..255
    img = ((frame16.astype(np.float32) - mn) * (255.0 / (mx - mn))).clip(0, 255).astype(np.uint8)
    return img


def save_frame(idx: int, frame16: np.ndarray) -> None:
    base = os.path.join(OUTPUT_DIR, f"frame_{idx:03d}")
    npy_path = base + ".npy"
    png_path = base + ".png"

    np.save(npy_path, frame16)
    print(f"[SAVE] Wrote raw frame to {npy_path} (shape={frame16.shape}, dtype={frame16.dtype})")

    if HAS_CV2:
        img8 = normalize_to_8bit(frame16)
        ok = cv2.imwrite(png_path, img8)
        if ok:
            print(f"[SAVE] Wrote preview PNG to {png_path} (8-bit normalized)")
        else:
            print(f"[SAVE] Failed to write PNG to {png_path} (cv2.imwrite returned False)")
    else:
        print("[SAVE] OpenCV not available; skipping PNG write.")


def main():
    ensure_output_dir(OUTPUT_DIR)

    print("[INIT] Optional I2C presence check (non-fatal)...")
    try_i2c_presence_check()

    print("[INIT] Opening SPI...")
    spi = open_spi()

    try:
        # Give camera time after boot/power-up
        print("[INIT] Sleeping 2 seconds for camera startup...")
        time.sleep(2.0)

        saved = 0
        resync_attempts = 0

        while saved < NUM_FRAMES_TO_SAVE:
            resync_attempts += 1
            if resync_attempts > MAX_RESYNC_ATTEMPTS:
                print(f"[FAIL] Too many resync attempts ({MAX_RESYNC_ATTEMPTS}). Giving up.")
                break

            print(f"\n[TRY] Capturing frame {saved+1}/{NUM_FRAMES_TO_SAVE} (attempt {resync_attempts}) ...")
            frame, stats = read_full_frame(spi, timeout_s=FRAME_TIMEOUT_S, verbose=True)

            if frame is None:
                print("[TRY] Frame capture failed; will resync and try again.")
                # small pause can help reduce tight-loop desync
                time.sleep(0.05)
                continue

            # Quick sanity stats
            mn = int(frame.min())
            mx = int(frame.max())
            mean = float(frame.mean())
            print(f"[OK] Got frame: min={mn} max={mx} mean={mean:.1f} "
                  f"(discards={stats.discards}, seg0={stats.seg0}, invalid_seg={stats.invalid_seg}, out_of_order={stats.out_of_order}, seg_counts={dict(stats.segments_seen)})")

            save_frame(saved, frame)
            saved += 1

            # Reset resync counter after success
            resync_attempts = 0

            # small spacing between frames
            time.sleep(0.1)

        print(f"\n[DONE] Saved {saved}/{NUM_FRAMES_TO_SAVE} frames to ./{OUTPUT_DIR}/")

    finally:
        try:
            spi.close()
        except Exception:
            pass
        print("[CLEANUP] SPI closed.")


if __name__ == "__main__":
    main()