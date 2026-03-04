#!/usr/bin/env python3
"""
Deterministic Lepton bring-up + VoSPI sanity for Raspberry Pi 5 (Debian Trixie).

- Drives PW_DWN_L (GPIO20) HIGH continuously
- Pulses RESET_L (GPIO21) LOW then HIGH
- Waits for boot, then reads Lepton STATUS using proper 16-bit register addressing
- Optionally samples VoSPI packets and reports discard/valid stats
- Best-effort PNG reconstruction for a sanity check (not a guaranteed perfect decode)

Run with sudo (needed for GPIO + /dev/spidev).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import gpiod
from smbus2 import SMBus, i2c_msg
import spidev
import numpy as np

try:
    import cv2  # opencv-python
except Exception:
    cv2 = None


LEPTON_STATUS_REG = 0x0002  # per Lepton Software IDD command interface start-up text
LEPTON_I2C_7BIT_DEFAULT = 0x2A  # IDD Table 1


@dataclass
class StatusBits:
    raw: int
    busy: int
    boot_mode: int
    boot_status: int

    @classmethod
    def from_u16(cls, v: int) -> "StatusBits":
        return cls(
            raw=v & 0xFFFF,
            busy=(v >> 0) & 1,
            boot_mode=(v >> 1) & 1,
            boot_status=(v >> 2) & 1,
        )


def read_reg16(bus: SMBus, addr7: int, reg16: int) -> int:
    """
    Lepton CCI/TWI uses 16-bit register addresses and 16-bit data words (MSB then LSB).
    Sequence: write reg_hi, reg_lo then read 2 bytes.
    """
    reg_hi = (reg16 >> 8) & 0xFF
    reg_lo = reg16 & 0xFF
    write = i2c_msg.write(addr7, [reg_hi, reg_lo])
    read = i2c_msg.read(addr7, 2)
    bus.i2c_rdwr(write, read)
    data = list(read)
    return (data[0] << 8) | data[1]


def fmt_status(s: StatusBits) -> str:
    return (
        f"STATUS=0x{s.raw:04x} "
        f"(boot_status={s.boot_status} boot_mode={s.boot_mode} busy={s.busy})"
    )


def gpio_setup(pwdn_gpio: int, reset_gpio: int) -> tuple[gpiod.LineRequest, gpiod.LineRequest]:
    """
    libgpiod v2 style requests. Uses gpiochip0 on Pi.
    """
    chip = gpiod.Chip("gpiochip0")

    # PW_DWN_L: output HIGH (active-low power down line, must be held HIGH)
    pwdn_line = chip.get_line(pwdn_gpio)
    pwdn_req = gpiod.LineRequest()
    pwdn_req.consumer = "lepton_diag"
    pwdn_req.request_type = gpiod.LINE_REQ_DIR_OUT
    pwdn_line.request(pwdn_req, default_vals=[1])

    # RESET_L: output, we will pulse low then high
    reset_line = chip.get_line(reset_gpio)
    reset_req = gpiod.LineRequest()
    reset_req.consumer = "lepton_diag"
    reset_req.request_type = gpiod.LINE_REQ_DIR_OUT
    reset_line.request(reset_req, default_vals=[1])

    # Return the line objects via their requests so we can set values
    # (Keep chip alive by keeping line objects referenced)
    return (pwdn_line, reset_line)


def pulse_reset(reset_line: gpiod.Line, low_ms: int = 200) -> None:
    reset_line.set_value(0)
    time.sleep(low_ms / 1000.0)
    reset_line.set_value(1)


def wait_for_boot(bus: SMBus, addr7: int, timeout_s: float = 10.0, poll_s: float = 0.2) -> StatusBits:
    t0 = time.time()
    last = None
    while (time.time() - t0) < timeout_s:
        try:
            v = read_reg16(bus, addr7, LEPTON_STATUS_REG)
            st = StatusBits.from_u16(v)
            last = st
            if st.boot_status == 1 and st.busy == 0:
                return st
        except Exception as e:
            print(f"[I2C] read error: {e}")
        time.sleep(poll_s)
    if last is None:
        raise RuntimeError("No readable STATUS at all on I2C within timeout.")
    return last


def open_spi(bus: int, dev: int, hz: int) -> spidev.SpiDev:
    spi = spidev.SpiDev()
    spi.open(bus, dev)
    spi.mode = 0b11  # SPI mode 3 required by Lepton VoSPI
    spi.bits_per_word = 8
    spi.max_speed_hz = hz
    # Some kernels benefit from explicit cs-high disabled; spidev uses active-low CS by default.
    return spi


def parse_packet_header(pkt: bytes) -> tuple[bool, int, int]:
    """
    Best-effort Lepton3/UWFOV style:
    - Discard packets often have upper nibble 0xF (0xFx ..)
    - Packet number is commonly in low 12 bits of first 2 bytes
    - Segment often encoded in bits [6:4] of byte0 for segmented sensors
    This function is intentionally conservative and used for stats/sync, not authoritative decoding.
    """
    b0 = pkt[0]
    b1 = pkt[1]
    discard = (b0 & 0xF0) == 0xF0
    pkt_num = ((b0 & 0x0F) << 8) | b1
    segment = (b0 >> 4) & 0x07
    return discard, pkt_num, segment


def vospi_sample(spi: spidev.SpiDev, seconds: float, packet_len: int = 164) -> dict:
    """
    Read packets continuously for `seconds`. Returns stats and first few headers.
    packet_len=164 is typical for many Lepton modes (4 header + 160 payload).
    """
    t0 = time.time()
    total = 0
    discards = 0
    valids = 0
    headers = []

    # Also store a chunk of payloads for an optional best-effort image attempt.
    payloads = []

    while (time.time() - t0) < seconds:
        pkt = bytes(spi.readbytes(packet_len))
        total += 1
        discard, pkt_num, seg = parse_packet_header(pkt)
        if discard:
            discards += 1
        else:
            valids += 1
            # store first N headers for debugging
            if len(headers) < 40:
                headers.append((pkt[0], pkt[1], pkt[2], pkt[3], pkt_num, seg))
            payloads.append(pkt[4:])  # raw payload

    return {
        "total": total,
        "discards": discards,
        "valids": valids,
        "headers": headers,
        "payloads": payloads,
        "packet_len": packet_len,
    }


def best_effort_png(payloads: list[bytes], out_path: str) -> None:
    """
    Best-effort reconstruction:
    Treat each payload as 80 pixels of 16-bit big-endian (160 bytes => 80 u16).
    Stack into [N][80]. If N>=240, take first 240 "lines" and pair them into 120 rows of 160 pixels.
    Then normalize to 8-bit and save.
    """
    if cv2 is None:
        raise RuntimeError("opencv-python not installed; cannot write PNG. Install or omit --save-png.")

    if len(payloads) < 240:
        raise RuntimeError(f"Not enough valid payloads for best-effort image (need >=240, have {len(payloads)}).")

    # Take first 240 payloads
    lines80 = []
    for p in payloads[:240]:
        if len(p) < 160:
            continue
        arr = np.frombuffer(p[:160], dtype=">u2")  # 80 uint16 big-endian
        lines80.append(arr)

    if len(lines80) < 240:
        raise RuntimeError(f"After parsing, only {len(lines80)} usable lines; expected 240.")

    lines80 = np.stack(lines80, axis=0)  # (240, 80)

    # Pair consecutive lines into 160-wide rows (120, 160)
    rows160 = np.concatenate([lines80[0::2, :], lines80[1::2, :]], axis=1)

    # Normalize to 8-bit for viewing
    lo = np.percentile(rows160, 1)
    hi = np.percentile(rows160, 99)
    img = np.clip((rows160 - lo) * 255.0 / max(1.0, (hi - lo)), 0, 255).astype(np.uint8)

    ok = cv2.imwrite(out_path, img)
    if not ok:
        raise RuntimeError(f"cv2.imwrite failed for {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--i2c-bus", type=int, default=1)
    ap.add_argument("--i2c-addr", type=lambda x: int(x, 0), default=LEPTON_I2C_7BIT_DEFAULT)
    ap.add_argument("--spi-bus", type=int, default=0)
    ap.add_argument("--spi-dev", type=int, default=0)
    ap.add_argument("--spi-hz", type=int, default=20_000_000)
    ap.add_argument("--packet-len", type=int, default=164)
    ap.add_argument("--capture-seconds", type=float, default=3.0)
    ap.add_argument("--save-png", type=str, default="")
    ap.add_argument("--pwdn-gpio", type=int, default=20)
    ap.add_argument("--reset-gpio", type=int, default=21)
    args = ap.parse_args()

    print("[GPIO] Requesting PW_DWN_L=HIGH and RESET_L control via libgpiod...")
    try:
        pwdn_line, reset_line = gpio_setup(args.pwdn_gpio, args.reset_gpio)
    except Exception as e:
        print(f"[FAIL] GPIO request failed: {e}")
        return 2

    print("[GPIO] Pulsing RESET_L low then high...")
    pulse_reset(reset_line, low_ms=200)

    # Give it longer than the minimum 950ms after releasing RESET_L (IDD requirement)
    print("[BOOT] Waiting 2.0s after RESET release before I2C access...")
    time.sleep(2.0)

    print(f"[I2C] Opening /dev/i2c-{args.i2c_bus}, addr=0x{args.i2c_addr:02x}")
    try:
        with SMBus(args.i2c_bus) as bus:
            # Read status a few times for stability
            for i in range(5):
                try:
                    v = read_reg16(bus, args.i2c_addr, LEPTON_STATUS_REG)
                    st = StatusBits.from_u16(v)
                    print(f"[I2C] {i+1}/5 {fmt_status(st)}")
                except Exception as e:
                    print(f"[I2C] {i+1}/5 read failed: {e}")
                time.sleep(0.2)

            print("[BOOT] Polling for boot_status=1 and busy=0...")
            st = wait_for_boot(bus, args.i2c_addr, timeout_s=10.0, poll_s=0.2)
            print(f"[BOOT] Final: {fmt_status(st)}")

            if st.boot_status != 1:
                print(
                    "\n[FAIL] boot_status never became 1.\n"
                    "This is NOT a VoSPI problem yet.\n"
                    "Most likely PW_DWN_L is not actually HIGH, RESET_L not reaching HIGH,\n"
                    "or the 1.2V core rail/clock is wrong.\n"
                )
                return 3

    except Exception as e:
        print(f"[FAIL] I2C open/read failed: {e}")
        return 4

    print(f"[SPI] Opening spidev{args.spi_bus}.{args.spi_dev} mode=3 hz={args.spi_hz} packet_len={args.packet_len}")
    try:
        spi = open_spi(args.spi_bus, args.spi_dev, args.spi_hz)
    except Exception as e:
        print(f"[FAIL] SPI open failed: {e}")
        return 5

    try:
        stats = vospi_sample(spi, seconds=args.capture_seconds, packet_len=args.packet_len)
    finally:
        try:
            spi.close()
        except Exception:
            pass

    total = stats["total"]
    disc = stats["discards"]
    val = stats["valids"]
    disc_pct = (disc / total * 100.0) if total else 0.0
    val_pct = (val / total * 100.0) if total else 0.0

    print("\n[VoSPI] Sample complete")
    print(f"  total packets:   {total}")
    print(f"  discard packets: {disc} ({disc_pct:.1f}%)")
    print(f"  valid packets:   {val} ({val_pct:.1f}%)")

    print("\n[VoSPI] First valid packet headers (up to 40):")
    for (b0, b1, b2, b3, pkt_num, seg) in stats["headers"]:
        print(f"  hdr: {b0:02x} {b1:02x} {b2:02x} {b3:02x}  pkt_num={pkt_num:4d} seg={seg}")

    if args.save_png:
        try:
            best_effort_png(stats["payloads"], args.save_png)
            print(f"\n[PNG] Wrote best-effort image to: {args.save_png}")
        except Exception as e:
            print(f"\n[PNG] Could not write PNG: {e}")

    # Deterministic decision hints
    if val == 0:
        print(
            "\n[FAIL] 0 valid packets observed.\n"
            "This usually means one of:\n"
            "  - SPI mode is wrong (must be mode 3)\n"
            "  - CS/CLK/MISO wiring issue\n"
            "  - camera not actually running (PW_DWN_L/RESET_L)\n"
            "  - SPI clock too fast *for your wiring* (try --spi-hz 8000000)\n"
        )
        return 6

    if disc_pct > 90.0:
        print(
            "\n[WARN] Very high discard rate.\n"
            "Common causes:\n"
            "  - not clocking fast enough to drain full frames\n"
            "  - long gaps between reads\n"
            "  - marginal signal integrity on SCLK/MISO/CS\n"
            "Next: we will scope CS, SCLK, and MISO during capture.\n"
        )
        return 0

    print("\n[OK] Booted and observed non-trivial valid VoSPI traffic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())