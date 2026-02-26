#!/usr/bin/env python3
"""
Lepton UWFOV / Lepton 3.x VoSPI viewer for Raspberry Pi 5

Fixes:
- Use SPI0 by default (GPIO8/9/10/11 -> /dev/spidev0.0)
- Correctly handles 80 words (160 bytes) payload per packet (RAW14 over 16-bit container)
- Avoids crashing on (80,) -> (160,) broadcast error
- Displays a stable live feed on desktop (OpenCV)
"""

import time
from collections import Counter

import cv2
import numpy as np
import smbus2
import spidev

# -----------------------------
# Hardware / Bus configuration
# -----------------------------
# Your wiring uses SPI0 pins:
#   SCLK=GPIO11, MOSI=GPIO10, MISO=GPIO9, CS=GPIO8 (CE0)
SPI_BUS = 0
SPI_DEVICE = 0  # CE0 (GPIO8)
SPI_SPEED = 20_000_000  # if unstable, try 16_000_000

# Lepton VoSPI packet size for 160x120 Lepton family = 164 bytes
PACKET_SIZE = 164
PACKETS_PER_SEGMENT = 60
SEGMENTS_PER_FRAME = 4

# Payload in RAW14 mode is 160 bytes => 80 uint16 words
WORDS_PER_PACKET = 80
RAW_HEIGHT = 120
RAW_WIDTH_WORDS = 80  # display at 80x120 (then upscale to 160x120)

# I2C / CCI (we only use it to sanity-check presence)
I2C_BUS = 1
LEPTON_I2C_ADDRESS = 0x2A
CCI_STATUS_REG = 0x0002

# Resync parameters
MAX_DISCARD_BEFORE_RESYNC = 750
MAX_RESYNC_ATTEMPTS = 5

# Display options
WINDOW_TITLE = "Lepton UWFOV (80x120 upscaled to 160x120)"
UPSCALE_TO_160 = True
APPLY_COLORMAP = True
SHOW_DEBUG = False  # set True for verbose packet logs


# -----------------------------
# I2C helpers (light touch)
# -----------------------------
def cci_read_register(bus: smbus2.SMBus, reg_addr: int) -> int | None:
    """Read a 16-bit value from a Lepton CCI register (best-effort)."""
    try:
        bus.write_byte_data(LEPTON_I2C_ADDRESS, (reg_addr >> 8) & 0xFF, reg_addr & 0xFF)
        time.sleep(0.001)
        data = bus.read_i2c_block_data(LEPTON_I2C_ADDRESS, 0, 2)
        return (data[0] << 8) | data[1]
    except Exception:
        return None


def init_lepton_i2c() -> smbus2.SMBus | None:
    print(f"[INIT] Opening I2C bus {I2C_BUS} for camera control...")
    try:
        i2c = smbus2.SMBus(I2C_BUS)
        print("[INIT] I2C bus opened successfully")
    except Exception as e:
        print(f"[WARN] Failed to open I2C bus: {e}")
        return None

    print(f"[I2C] Checking camera at 0x{LEPTON_I2C_ADDRESS:02X}...")
    time.sleep(0.5)
    status = cci_read_register(i2c, CCI_STATUS_REG)
    if status is None:
        print("[WARN] Could not read CCI status register (camera may still work over SPI).")
    else:
        print(f"[I2C] CCI Status register: 0x{status:04X} (busy={status & 0x01})")
        print("[I2C] I2C communication established")
    return i2c


# -----------------------------
# SPI / VoSPI helpers
# -----------------------------
def open_spi() -> spidev.SpiDev:
    print(f"[INIT] Opening SPI bus {SPI_BUS}, device {SPI_DEVICE}...")
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEVICE)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 3  # Lepton requires SPI mode 3
    print(f"[INIT] SPI configured: speed={SPI_SPEED} Hz, mode={spi.mode}")
    return spi


def vospi_sync(spi: spidev.SpiDev):
    # Deassert CS for >= ~185ms (5 frame periods at ~27Hz timing domain) then restart
    print("[INIT] Performing VoSPI synchronization (CS high pause)...")
    spi.close()
    time.sleep(0.200)
    spi.open(SPI_BUS, SPI_DEVICE)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 3
    # Flush a bit of stale data
    for _ in range(50):
        spi.readbytes(PACKET_SIZE)
    print("[INIT] VoSPI sync complete")


def read_packet(spi: spidev.SpiDev) -> bytes:
    return bytes(spi.readbytes(PACKET_SIZE))


def is_discard(packet: bytes) -> bool:
    return (packet[0] & 0x0F) == 0x0F


def packet_segment(packet: bytes) -> int:
    return packet[0] >> 4  # 0=invalid frame region, 1-4 valid for 160x120


def packet_number(packet: bytes) -> int:
    return packet[1]


def analyze_packets(spi: spidev.SpiDev, num_packets: int = 200) -> bool:
    """Quick health check: are we seeing anything besides pure discards?"""
    header_bytes = []
    segment_numbers = []
    discard_packets = 0
    seg0 = 0

    for _ in range(num_packets):
        p = read_packet(spi)
        header = p[0]
        header_bytes.append(header)
        if is_discard(p):
            discard_packets += 1
        else:
            seg = packet_segment(p)
            if seg == 0:
                seg0 += 1
            else:
                segment_numbers.append(seg)

    print(f"[DIAG] Analyzing {num_packets} packets...")
    print(f"[DIAG]   Discard packets (0x?F): {discard_packets} ({100*discard_packets/num_packets:.1f}%)")
    print(f"[DIAG]   Segment #0 packets:     {seg0} ({100*seg0/num_packets:.1f}%)")
    print(f"[DIAG]   Non-zero segments:      {len(segment_numbers)}")

    common = Counter(header_bytes).most_common(10)
    print("[DIAG]   Most common header bytes:")
    for h, c in common:
        print(f"[DIAG]     0x{h:02X}: {c} ({100*c/num_packets:.1f}%)")

    if segment_numbers:
        print(f"[DIAG]   Segments seen: {sorted(set(segment_numbers))}")

    # Good enough if not 100% discard
    return discard_packets < num_packets


def resync_vospi(spi: spidev.SpiDev, attempt: int = 1):
    print(f"[RESYNC] Attempt {attempt}: cycling SPI...")
    vospi_sync(spi)


# -----------------------------
# Frame capture (fixed width)
# -----------------------------
def get_frame_80x120(spi: spidev.SpiDev) -> np.ndarray:
    """
    Capture a valid Lepton 160x120 segmented frame, but store payload as 80x120 words
    (because each packet contains 80 uint16 values = 160 bytes).

    We skip segment 0 frames (common between valid frames).
    We accept segments 1..4. Each segment has 60 packets, but only 30 packets map into
    the 120-row image (4 segments * 30 rows = 120).
    """
    frame = np.zeros((RAW_HEIGHT, RAW_WIDTH_WORDS), dtype=np.uint16)

    segments_received: set[int] = set()
    discard_count = 0
    invalid_seg_count = 0
    seg0_packet_streak = 0
    start_time = time.time()
    resync_triggered = False

    if SHOW_DEBUG:
        print("[FRAME] Waiting for valid frame (skipping segment #0 frames)...")

    while len(segments_received) < SEGMENTS_PER_FRAME:
        if time.time() - start_time > 10.0:
            raise TimeoutError("Timeout waiting for a complete valid frame")

        if discard_count > MAX_DISCARD_BEFORE_RESYNC and not resync_triggered:
            resync_vospi(spi, 1)
            resync_triggered = True
            discard_count = 0
            segments_received.clear()
            seg0_packet_streak = 0
            continue

        p = read_packet(spi)

        if is_discard(p):
            discard_count += 1
            continue

        seg = packet_segment(p)
        pkt = packet_number(p)

        # Segment 0 = invalid frames between valid ones
        if seg == 0:
            seg0_packet_streak += 1
            # After roughly a segment's worth, reset and keep waiting
            if seg0_packet_streak >= PACKETS_PER_SEGMENT:
                seg0_packet_streak = 0
                segments_received.clear()
            continue

        seg0_packet_streak = 0

        # Only 1..4 are valid for 160x120 Lepton segmented frames
        if seg < 1 or seg > 4:
            invalid_seg_count += 1
            continue

        # Mark segment seen
        segments_received.add(seg)

        # Map to row: each segment contributes 30 rows (0..29)
        # Many implementations ignore packets >=30 for 160x120 assembly.
        if pkt >= 30:
            continue

        row = (seg - 1) * 30 + pkt
        if row >= RAW_HEIGHT:
            continue

        # Payload is 160 bytes => 80 uint16 values, big-endian.
        data = np.frombuffer(p[4:], dtype=">u2")
        if data.shape[0] != WORDS_PER_PACKET:
            # If something odd happens, skip
            continue

        frame[row, :WORDS_PER_PACKET] = data

    return frame


def make_display_image(frame80: np.ndarray) -> np.ndarray:
    """
    Convert 80x120 uint16 frame to 8-bit display image.
    Optionally upscale to 160x120 for nicer viewing.
    """
    # Normalize to 8-bit
    img8 = cv2.normalize(frame80, None, 0, 255, cv2.NORM_MINMAX)
    img8 = np.uint8(img8)

    if UPSCALE_TO_160:
        img8 = cv2.resize(img8, (160, 120), interpolation=cv2.INTER_NEAREST)

    if APPLY_COLORMAP:
        img8 = cv2.applyColorMap(img8, cv2.COLORMAP_INFERNO)

    return img8


def main():
    i2c = None
    spi = None
    try:
        print("[INIT] Initializing Lepton camera...")
        i2c = init_lepton_i2c()

        spi = open_spi()

        print("[INIT] Waiting 2 seconds for camera initialization...")
        time.sleep(2.0)

        vospi_sync(spi)

        analyze_packets(spi, 200)

        print("\n[RUN] Starting live viewer. Press 'q' to quit.")
        cv2.namedWindow(WINDOW_TITLE, cv2.WINDOW_NORMAL)

        while True:
            frame80 = get_frame_80x120(spi)

            # Basic stats (optional)
            if SHOW_DEBUG:
                print(
                    f"[STAT] min={frame80.min()} max={frame80.max()} mean={frame80.mean():.1f}"
                )

            disp = make_display_image(frame80)
            cv2.imshow(WINDOW_TITLE, disp)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break

    except KeyboardInterrupt:
        pass
    finally:
        print("[CLEANUP] Closing...")
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        try:
            if spi is not None:
                spi.close()
        except Exception:
            pass
        try:
            if i2c is not None:
                i2c.close()
        except Exception:
            pass
        print("[CLEANUP] Done.")


if __name__ == "__main__":
    main()