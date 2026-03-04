#!/usr/bin/env python3
"""
Deterministic Lepton bring-up + VoSPI sanity for Raspberry Pi 5 (Debian Trixie).

This script is self-adapting to the installed python 'gpiod' binding by:
  - introspecting available APIs at runtime
  - trying known-good request paths in a strict order
  - if all fail, printing an exhaustive symbol dump so we can lock it down.

GPIO goals:
  GPIO20 -> PW_DWN_L (hold HIGH)
  GPIO21 -> RESET_L  (pulse LOW then HIGH)

I2C:
  Proper 16-bit register addressing and 16-bit word reads per Lepton IDD. :contentReference[oaicite:0]{index=0}

SPI:
  Mode 3 VoSPI packet sampling.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
import sys
import inspect

import gpiod
from smbus2 import SMBus, i2c_msg
import spidev
import numpy as np

try:
    import cv2
except Exception:
    cv2 = None

LEPTON_I2C_7BIT_DEFAULT = 0x2A
LEPTON_STATUS_REG = 0x0002


# ---------------------------
# Lepton I2C helpers
# ---------------------------

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


def fmt_status(s: StatusBits) -> str:
    return f"STATUS=0x{s.raw:04x} (boot_status={s.boot_status} boot_mode={s.boot_mode} busy={s.busy})"


def read_reg16(bus: SMBus, addr7: int, reg16: int) -> int:
    """
    Lepton uses 16-bit register addresses and 16-bit data words. :contentReference[oaicite:1]{index=1}
    Write reg_hi, reg_lo then read 2 bytes MSB..LSB.
    """
    reg_hi = (reg16 >> 8) & 0xFF
    reg_lo = reg16 & 0xFF
    w = i2c_msg.write(addr7, [reg_hi, reg_lo])
    r = i2c_msg.read(addr7, 2)
    bus.i2c_rdwr(w, r)
    d = list(r)
    return (d[0] << 8) | d[1]


def wait_for_boot(bus: SMBus, addr7: int, timeout_s: float = 10.0, poll_s: float = 0.2) -> StatusBits:
    t0 = time.time()
    last = None
    while (time.time() - t0) < timeout_s:
        v = read_reg16(bus, addr7, LEPTON_STATUS_REG)
        st = StatusBits.from_u16(v)
        last = st
        if st.boot_status == 1 and st.busy == 0:
            return st
        time.sleep(poll_s)
    if last is None:
        raise RuntimeError("No readable STATUS register within timeout.")
    return last


# ---------------------------
# VoSPI helpers
# ---------------------------

def open_spi(bus: int, dev: int, hz: int) -> spidev.SpiDev:
    spi = spidev.SpiDev()
    spi.open(bus, dev)
    spi.mode = 0b11
    spi.bits_per_word = 8
    spi.max_speed_hz = hz
    return spi


def parse_packet_header(pkt: bytes) -> tuple[bool, int]:
    b0, b1 = pkt[0], pkt[1]
    discard = (b0 & 0xF0) == 0xF0
    pkt_num = ((b0 & 0x0F) << 8) | b1
    return discard, pkt_num


def vospi_sample(spi: spidev.SpiDev, seconds: float, packet_len: int) -> dict:
    t0 = time.time()
    total = discards = valids = 0
    headers = []
    payloads = []

    while (time.time() - t0) < seconds:
        pkt = bytes(spi.readbytes(packet_len))
        total += 1
        discard, pkt_num = parse_packet_header(pkt)
        if discard:
            discards += 1
        else:
            valids += 1
            if len(headers) < 40:
                headers.append((pkt[0], pkt[1], pkt[2], pkt[3], pkt_num))
            payloads.append(pkt[4:])

    return {
        "total": total,
        "discards": discards,
        "valids": valids,
        "headers": headers,
        "payloads": payloads,
    }


def best_effort_png(payloads: list[bytes], out_path: str):
    if cv2 is None:
        raise RuntimeError("opencv-python not installed; install or omit --save-png.")
    if len(payloads) < 240:
        raise RuntimeError(f"Need >=240 valid payloads, got {len(payloads)}")

    lines80 = []
    for p in payloads[:240]:
        if len(p) < 160:
            continue
        lines80.append(np.frombuffer(p[:160], dtype=">u2"))

    if len(lines80) < 240:
        raise RuntimeError(f"Only {len(lines80)} usable lines after parsing")

    lines80 = np.stack(lines80, axis=0)
    rows160 = np.concatenate([lines80[0::2], lines80[1::2]], axis=1)

    lo = np.percentile(rows160, 1)
    hi = np.percentile(rows160, 99)
    scale = max(1.0, hi - lo)
    img = np.clip((rows160 - lo) * 255.0 / scale, 0, 255).astype(np.uint8)

    if not cv2.imwrite(out_path, img):
        raise RuntimeError("cv2.imwrite failed")


# ---------------------------
# gpiod: zero-assumption adapter
# ---------------------------

class GPIOAdapterError(RuntimeError):
    pass


def _symdump(obj, name: str, limit: int = 200) -> str:
    try:
        items = sorted([x for x in dir(obj) if not x.startswith("_")])
    except Exception:
        return f"{name}: <dir() failed>"
    if len(items) > limit:
        items = items[:limit] + ["... (truncated)"]
    return f"{name} attrs: " + ", ".join(items)


def _print_gpiod_diagnostics():
    print("\n=== gpiod binding diagnostics ===")
    print("gpiod module path:", getattr(gpiod, "__file__", "<unknown>"))
    print("Chip signature:", end=" ")
    try:
        print(inspect.signature(gpiod.Chip))
    except Exception as e:
        print(f"<unavailable: {e}>")

    print(_symdump(gpiod, "gpiod"))
    # Some builds expose a submodule/namespace called "line"
    line_ns = getattr(gpiod, "line", None)
    if line_ns is not None:
        print(_symdump(line_ns, "gpiod.line"))
    else:
        # Sometimes it's importable but not attached
        try:
            import gpiod.line as line_mod  # type: ignore
            print(_symdump(line_mod, "import gpiod.line"))
        except Exception as e:
            print("gpiod.line not accessible:", e)

    # request_lines presence
    print("has gpiod.request_lines:", hasattr(gpiod, "request_lines"))
    # common classes
    for cls in ["LineSettings", "LineConfig", "LineRequest", "Request"]:
        print(f"has gpiod.{cls}:", hasattr(gpiod, cls))
    print("=== end gpiod diagnostics ===\n")


def _try_request_lines(chip_path: str, offsets: list[int], defaults: list[int]):
    """
    Attempt gpiod.request_lines() with several possible config object shapes.
    Returns (req_obj, set_value_callable, active_value, inactive_value)
    where set_value_callable(offset, is_high:bool) drives the line.
    """
    if not hasattr(gpiod, "request_lines"):
        raise GPIOAdapterError("gpiod.request_lines not available")

    request_lines = gpiod.request_lines

    # Helper: resolve "line namespace" if present
    line_ns = getattr(gpiod, "line", None)
    if line_ns is None:
        try:
            import gpiod.line as line_ns  # type: ignore
        except Exception:
            line_ns = None

    # Candidate sources to find classes/enums
    candidates = [gpiod]
    if line_ns is not None:
        candidates.insert(0, line_ns)

    def find_attr(*names):
        for src in candidates:
            for n in names:
                if hasattr(src, n):
                    return getattr(src, n)
        return None

    LineSettings = find_attr("LineSettings")
    LineConfig = find_attr("LineConfig")
    Direction = find_attr("Direction", "LineDirection")
    Value = find_attr("Value", "LineValue")

    # Determine enum members if present (otherwise fall back to 1/0)
    def enum_member(enum_obj, *member_names, fallback=None):
        if enum_obj is None:
            return fallback
        for mn in member_names:
            if hasattr(enum_obj, mn):
                return getattr(enum_obj, mn)
        return fallback

    DIR_OUT = enum_member(Direction, "OUTPUT", "DIRECTION_OUTPUT", fallback=None)
    VAL_HIGH = enum_member(Value, "ACTIVE", "HIGH", "ONE", fallback=1)
    VAL_LOW = enum_member(Value, "INACTIVE", "LOW", "ZERO", fallback=0)

    # Strategy 1: config dict of offset -> LineSettings(...)
    if LineSettings is not None and DIR_OUT is not None:
        try:
            cfg = {
                offsets[0]: LineSettings(direction=DIR_OUT, output_value=VAL_HIGH if defaults[0] else VAL_LOW),
                offsets[1]: LineSettings(direction=DIR_OUT, output_value=VAL_HIGH if defaults[1] else VAL_LOW),
            }
            req = request_lines(chip_path, consumer="lepton_diag", config=cfg)

            def set_line(off: int, is_high: bool):
                req.set_value(off, VAL_HIGH if is_high else VAL_LOW)

            return req, set_line, VAL_HIGH, VAL_LOW
        except Exception as e:
            last_e = e
    else:
        last_e = None

    # Strategy 2: LineConfig object (if exists)
    if LineConfig is not None and LineSettings is not None and DIR_OUT is not None:
        try:
            cfg_obj = LineConfig()
            # Common API: cfg_obj.add_line_settings([offsets], LineSettings(...))
            if hasattr(cfg_obj, "add_line_settings"):
                cfg_obj.add_line_settings(
                    offsets,
                    LineSettings(direction=DIR_OUT, output_value=VAL_HIGH),
                )
                req = request_lines(chip_path, consumer="lepton_diag", config=cfg_obj)

                def set_line(off: int, is_high: bool):
                    req.set_value(off, VAL_HIGH if is_high else VAL_LOW)

                return req, set_line, VAL_HIGH, VAL_LOW
        except Exception as e:
            last_e = e

    # Strategy 3: older keyword style request_lines (rare)
    # Some bindings accept: request_lines(chip_path, consumer=..., offsets=[...], config=...)
    try:
        req = request_lines(chip_path, consumer="lepton_diag", offsets=offsets)

        def set_line(off: int, is_high: bool):
            req.set_value(off, VAL_HIGH if is_high else VAL_LOW)

        return req, set_line, VAL_HIGH, VAL_LOW
    except Exception as e:
        last_e = e

    raise GPIOAdapterError(f"request_lines attempts failed; last error: {last_e}")


def gpio_acquire(chip_path: str, pwdn_gpio: int, reset_gpio: int):
    """
    Deterministic acquire: try request_lines path only (since your Chip lacks get_line).
    If it fails, print diagnostics and raise.
    """
    offsets = [pwdn_gpio, reset_gpio]
    defaults = [1, 1]  # both HIGH
    try:
        return _try_request_lines(chip_path, offsets, defaults)
    except Exception as e:
        _print_gpiod_diagnostics()
        raise GPIOAdapterError(str(e))


# ---------------------------
# Main
# ---------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chip", default="/dev/gpiochip0")
    ap.add_argument("--pwdn-gpio", type=int, default=20)
    ap.add_argument("--reset-gpio", type=int, default=21)
    ap.add_argument("--i2c-bus", type=int, default=1)
    ap.add_argument("--i2c-addr", type=lambda x: int(x, 0), default=LEPTON_I2C_7BIT_DEFAULT)
    ap.add_argument("--spi-bus", type=int, default=0)
    ap.add_argument("--spi-dev", type=int, default=0)
    ap.add_argument("--spi-hz", type=int, default=20_000_000)
    ap.add_argument("--packet-len", type=int, default=164)
    ap.add_argument("--capture-seconds", type=float, default=3.0)
    ap.add_argument("--save-png", default="")
    args = ap.parse_args()

    print("[GPIO] Acquiring GPIO lines via gpiod adapter...")
    try:
        req, set_line, VAL_HIGH, VAL_LOW = gpio_acquire(args.chip, args.pwdn_gpio, args.reset_gpio)
    except Exception as e:
        print(f"[FAIL] GPIO request failed: {e}")
        return 2

    # Force PW_DWN_L HIGH, RESET_L HIGH
    set_line(args.pwdn_gpio, True)
    set_line(args.reset_gpio, True)
    print("[GPIO] PW_DWN_L forced HIGH; RESET_L forced HIGH.")

    # Pulse RESET_L low then high
    print("[GPIO] Pulsing RESET_L LOW then HIGH...")
    set_line(args.reset_gpio, False)
    time.sleep(0.200)
    set_line(args.reset_gpio, True)

    # Wait > 950ms after reset release per IDD :contentReference[oaicite:2]{index=2}
    print("[BOOT] Waiting 2.0s after reset release before I2C access (>=950ms required)...")
    time.sleep(2.0)

    print(f"[I2C] Using /dev/i2c-{args.i2c_bus} addr=0x{args.i2c_addr:02x}")
    try:
        with SMBus(args.i2c_bus) as bus:
            for i in range(5):
                v = read_reg16(bus, args.i2c_addr, LEPTON_STATUS_REG)
                st = StatusBits.from_u16(v)
                print(f"[I2C] {i+1}/5 {fmt_status(st)}")
                time.sleep(0.2)

            print("[BOOT] Polling for boot_status=1 and busy=0...")
            st = wait_for_boot(bus, args.i2c_addr, timeout_s=10.0, poll_s=0.2)
            print(f"[BOOT] Final: {fmt_status(st)}")

            if st.boot_status != 1:
                print(
                    "\n[FAIL] boot_status never became 1. This is NOT a VoSPI problem yet.\n"
                    "Deterministic check while this script is running:\n"
                    "  - Measure J2 pin 20 (PW_DWN_L) to GND: should be IO-high\n"
                    "  - Measure J2 pin 17 (RESET_L)  to GND: should be IO-high except during the 200ms pulse\n"
                )
                return 3
    except Exception as e:
        print(f"[FAIL] I2C failed: {e}")
        return 4

    print(f"[SPI] Opening spidev{args.spi_bus}.{args.spi_dev}, mode=3, hz={args.spi_hz}, packet_len={args.packet_len}")
    try:
        spi = open_spi(args.spi_bus, args.spi_dev, args.spi_hz)
    except Exception as e:
        print(f"[FAIL] SPI open failed: {e}")
        return 5

    try:
        stats = vospi_sample(spi, args.capture_seconds, args.packet_len)
    finally:
        spi.close()

    total = stats["total"]
    disc = stats["discards"]
    val = stats["valids"]
    disc_pct = (disc / total * 100.0) if total else 0.0

    print("\n[VoSPI] Sample complete")
    print(f"  total packets:   {total}")
    print(f"  discard packets: {disc} ({disc_pct:.1f}%)")
    print(f"  valid packets:   {val} ({100.0 - disc_pct:.1f}%)")

    print("\n[VoSPI] First valid packet headers (up to 40):")
    for (b0, b1, b2, b3, pkt_num) in stats["headers"]:
        print(f"  hdr: {b0:02x} {b1:02x} {b2:02x} {b3:02x}  pkt_num={pkt_num:4d}")

    if args.save_png:
        try:
            best_effort_png(stats["payloads"], args.save_png)
            print(f"\n[PNG] Wrote best-effort image to: {args.save_png}")
        except Exception as e:
            print(f"\n[PNG] Could not write PNG: {e}")

    if val == 0:
        print(
            "\n[FAIL] 0 valid packets observed.\n"
            "Deterministic next steps:\n"
            "  - Scope J2 pin 10 (SPI_CS), J2 pin 7 (SPI_CLK), J2 pin 12 (SPI_MISO)\n"
            "  - Verify CS pulses low during reads and CLK is a clean square wave at configured hz\n"
            "  - Try --spi-hz 8000000 if wiring is long/noisy\n"
        )
        return 6

    if disc_pct > 90.0:
        print(
            "\n[WARN] Very high discard rate.\n"
            "Next: we will scope CS/CLK/MISO during capture and verify frame drain timing.\n"
        )

    print("\n[OK] Booted and observed non-trivial valid VoSPI traffic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())