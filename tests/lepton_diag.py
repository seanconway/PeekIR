#!/usr/bin/env python3
"""
Lepton 3.x / UWFOV VoSPI + CCI diagnostic & one-frame capture (Raspberry Pi)

What it does:
  1) I2C/CCI sanity: enforces >=950ms after RESET_L release, reads STATUS (0x0002)
     - Bit2: booted, Bit0: busy per Software IDD.
  2) VoSPI sanity: performs re-sync by deasserting /CS for >185ms (spec)
  3) Attempts to capture one full 160x120 frame (4 segments x 60 packets = 240 packets)
  4) Writes frame.pgm (always) and frame.png (if Pillow installed)

Key options:
  --cs-gpio <BCM#> : use manual chip-select via GPIO (recommended).
                     This avoids kernel spidev toggling CS between transfers.
  --reset-gpio <BCM#> : optionally drive RESET_L (active low) if wired
  --pwrdn-gpio <BCM#> : optionally drive PW_DWN_L (active low) if wired

Requires:
  pip install spidev smbus2 numpy
Optional:
  pip install pillow

Run examples:
  sudo python3 lepton_diag.py --i2c-bus 1 --spi-dev /dev/spidev0.0 --hz 8000000
  sudo python3 lepton_diag.py --cs-gpio 8 --spi-dev /dev/spidev0.0 --hz 12000000
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, List

import numpy as np

try:
    import spidev
except ImportError:
    print("[FATAL] Missing spidev. Install: pip install spidev")
    sys.exit(2)

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    print("[FATAL] Missing smbus2. Install: pip install smbus2")
    sys.exit(2)

# Try lgpio first (Pi 5 friendly). Fallback to RPi.GPIO.
GPIO_BACKEND = None
try:
    import lgpio  # type: ignore
    GPIO_BACKEND = "lgpio"
except ImportError:
    try:
        import RPi.GPIO as RGPIO  # type: ignore
        GPIO_BACKEND = "RPi.GPIO"
    except ImportError:
        GPIO_BACKEND = None


# ------------------------- Helpers / Diagnostics -------------------------

def now_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)

def hexdump(b: bytes, n: int = 16) -> str:
    return " ".join(f"{x:02X}" for x in b[:n])

def is_root() -> bool:
    return os.geteuid() == 0

@dataclass
class Stats:
    discards: int = 0
    kept: int = 0
    duplicates: int = 0
    bad_pnum: int = 0
    invalid_seg: int = 0
    resyncs: int = 0
    gate_failures: int = 0
    missing: List[Tuple[int, int]] = field(default_factory=list)
    seg_fill: Dict[int, int] = field(default_factory=lambda: {1: 0, 2: 0, 3: 0, 4: 0})

@dataclass
class PacketHdr:
    t_ms: int
    rawid: int
    seg: int
    pnum: int
    hdr8: bytes

def parse_vospi_header(pkt: bytes) -> Tuple[int, int, int]:
    """
    Lepton 3.x packet header: first 2 bytes are ID:
      seg = (id >> 12) & 0xF
      pnum = id & 0x0FFF
    """
    rawid = (pkt[0] << 8) | pkt[1]
    seg = (rawid >> 12) & 0xF
    pnum = rawid & 0x0FFF
    return rawid, seg, pnum

def is_discard(seg: int, pnum: int) -> bool:
    # Discard/invalid packets often show pnum==0x0FFF (4095), seg varying (including 0xF).
    return pnum == 0x0FFF

# ------------------------- GPIO control (optional) -------------------------

class GpioCtl:
    def __init__(self):
        self.backend = GPIO_BACKEND
        self.handle = None
        self.claimed = []

    def open(self):
        if self.backend is None:
            raise RuntimeError("No GPIO backend available (install lgpio or RPi.GPIO)")
        if self.backend == "lgpio":
            # 0 is usually gpiochip0 on Pi
            self.handle = lgpio.gpiochip_open(0)
        else:
            RGPIO.setmode(RGPIO.BCM)
            self.handle = True

    def close(self):
        if self.backend == "lgpio" and self.handle is not None:
            for pin in self.claimed:
                try:
                    lgpio.gpio_free(self.handle, pin)
                except Exception:
                    pass
            lgpio.gpiochip_close(self.handle)
        elif self.backend == "RPi.GPIO" and self.handle:
            try:
                RGPIO.cleanup()
            except Exception:
                pass
        self.handle = None
        self.claimed.clear()

    def setup_out(self, pin: int, initial: int = 1):
        if self.backend == "lgpio":
            lgpio.gpio_claim_output(self.handle, pin, initial)
        else:
            RGPIO.setup(pin, RGPIO.OUT, initial=RGPIO.HIGH if initial else RGPIO.LOW)
        self.claimed.append(pin)

    def write(self, pin: int, value: int):
        if self.backend == "lgpio":
            lgpio.gpio_write(self.handle, pin, value)
        else:
            RGPIO.output(pin, RGPIO.HIGH if value else RGPIO.LOW)

# ------------------------- I2C / CCI minimal read -------------------------

def i2c_scan(bus: SMBus) -> List[int]:
    found = []
    for addr in range(0x03, 0x78):
        try:
            # "quick" write
            bus.write_quick(addr)
            found.append(addr)
        except Exception:
            pass
    return found

def cci_read_u16(bus: SMBus, addr7: int, reg16: int) -> int:
    """
    Lepton CCI/TWI registers are 16-bit, and transfers are 16-bit oriented.
    To read a register:
      write 16-bit register address, then read 2 bytes.

    Many hosts do: write [reg_hi, reg_lo], repeated-start, read 2 bytes.
    """
    reg_hi = (reg16 >> 8) & 0xFF
    reg_lo = reg16 & 0xFF
    write = i2c_msg.write(addr7, [reg_hi, reg_lo])
    read = i2c_msg.read(addr7, 2)
    bus.i2c_rdwr(write, read)
    data = list(read)
    return (data[0] << 8) | data[1]

# ------------------------- VoSPI capture -------------------------

def open_spi(dev: str, mode: int, hz: int, bits: int = 8, no_cs: bool = False) -> spidev.SpiDev:
    sp = spidev.SpiDev()
    # dev looks like /dev/spidev0.0
    base = os.path.basename(dev)
    if not base.startswith("spidev") or "." not in base:
        raise ValueError(f"Bad spi device path: {dev}")
    bus, cs = base.replace("spidev", "").split(".")
    sp.open(int(bus), int(cs))
    sp.mode = mode
    sp.max_speed_hz = hz
    sp.bits_per_word = bits
    if hasattr(sp, "no_cs"):
        sp.no_cs = 1 if no_cs else 0
    return sp

def read_packet(spi: spidev.SpiDev, packet_len: int) -> bytes:
    # One atomic transfer per packet (no delay mid-packet).
    data = spi.xfer2([0x00] * packet_len)
    return bytes(data)

def assemble_frame(seg_payloads: Dict[Tuple[int, int], bytes]) -> np.ndarray:
    """
    Build 120x160 uint16 image from 4 segments * 60 packets.
    Each packet payload is 160 bytes = 80 pixels (uint16 each).
    Two packets per row (left/right halves).
    Segment has 60 packets => 30 rows.
    Mapping:
      seg in 1..4
      pnum in 0..59
      row_in_seg = pnum // 2
      half = pnum % 2  (0=left, 1=right)
      row = (seg-1)*30 + row_in_seg
      col_start = half*80
    """
    img = np.zeros((120, 160), dtype=np.uint16)

    for seg in range(1, 5):
        for pnum in range(60):
            payload = seg_payloads[(seg, pnum)]
            # payload is 160 bytes => 80 big-endian u16
            words = np.frombuffer(payload, dtype=">u2")
            # mask to 14-bit just in case
            words = (words & 0x3FFF).astype(np.uint16)

            row_in_seg = pnum // 2
            half = pnum % 2
            row = (seg - 1) * 30 + row_in_seg
            col0 = half * 80
            img[row, col0:col0 + 80] = words

    return img

def write_pgm(path: str, img: np.ndarray):
    # 16-bit binary PGM (P5)
    h, w = img.shape
    maxv = int(img.max()) if img.size else 65535
    maxv = max(1, min(65535, maxv))
    header = f"P5\n{w} {h}\n{maxv}\n".encode("ascii")
    # PGM expects big-endian for 16-bit values
    data = img.astype(">u2").tobytes()
    with open(path, "wb") as f:
        f.write(header)
        f.write(data)

def maybe_write_png(path: str, img: np.ndarray):
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return False

    # Normalize for visibility (simple min/max)
    a = img.astype(np.float32)
    mn, mx = float(np.min(a)), float(np.max(a))
    if mx <= mn:
        mx = mn + 1.0
    norm = (255.0 * (a - mn) / (mx - mn)).clip(0, 255).astype(np.uint8)
    im = Image.fromarray(norm, mode="L")
    im.save(path)
    return True

def vospi_resync(cs_gpio: Optional[int], gpio: Optional[GpioCtl], resync_ms: int, verbose: bool):
    """
    Spec for Lepton 3.x establishing/re-establishing sync:
      Deassert /CS and idle SCK for at least 5 frame periods (>185ms).
    We approximate: do not clock SPI and keep CS high for resync_ms.
    """
    if cs_gpio is not None and gpio is not None:
        gpio.write(cs_gpio, 1)
        if verbose:
            print(f"[VOSPI] RESYNC: CS(GPIO{cs_gpio})=HIGH for {resync_ms} ms (no SPI clocks)")
        time.sleep(resync_ms / 1000.0)
    else:
        if verbose:
            print(f"[VOSPI] RESYNC: (HW CS) sleeping {resync_ms} ms with no SPI clocks")
        time.sleep(resync_ms / 1000.0)

def capture_one_frame(
    spi: spidev.SpiDev,
    cs_gpio: Optional[int],
    gpio: Optional[GpioCtl],
    hz: int,
    packet_len: int,
    timeout_s: float,
    verbose: bool,
    gate_good_run: int = 0,
) -> Tuple[Optional[np.ndarray], Stats, List[PacketHdr], str]:
    """
    Attempts to acquire a full frame:
      - Wait for first valid (seg 1..4, pnum 0..59) after discards
      - Sync condition: look for seg=1 pnum=0 (start of frame)
      - Then collect all (seg,pnum) for seg=1..4 pnum=0..59
    """
    t0 = time.perf_counter()
    stats = Stats()
    last_hdrs: List[PacketHdr] = []
    seg_payloads: Dict[Tuple[int, int], bytes] = {}

    # If manual CS: assert low and keep it low until done.
    if cs_gpio is not None and gpio is not None:
        gpio.write(cs_gpio, 0)
        if verbose:
            print(f"[VOSPI] CS(GPIO{cs_gpio})=LOW (manual, held for entire capture)")

    def remember(pkt: bytes):
        rawid, seg, pnum = parse_vospi_header(pkt)
        last_hdrs.append(PacketHdr(
            t_ms=now_ms(t0),
            rawid=rawid,
            seg=seg,
            pnum=pnum,
            hdr8=pkt[:8],
        ))
        if len(last_hdrs) > 30:
            del last_hdrs[:len(last_hdrs) - 30]

    # -------- Phase A: find sync (seg=1, pnum=0) --------
    synced = False
    sync_deadline = t0 + min(timeout_s, 10.0)  # don't burn whole timeout on sync
    while time.perf_counter() < sync_deadline:
        pkt = read_packet(spi, packet_len)
        remember(pkt)
        rawid, seg, pnum = parse_vospi_header(pkt)

        if is_discard(seg, pnum):
            stats.discards += 1
            continue

        # Valid-ish packet number range?
        if pnum > 59:
            stats.bad_pnum += 1
            continue

        # Segment range expected 1..4 for 160x120 segmented frames
        if seg < 1 or seg > 4:
            stats.invalid_seg += 1
            continue

        if seg == 1 and pnum == 0:
            synced = True
            if verbose:
                print(f"[SYNC] Found start-of-frame: seg=1 pnum=0 rawid=0x{rawid:04X} @ {now_ms(t0)}ms")
            # Store it
            seg_payloads[(seg, pnum)] = pkt[4:4+160]
            stats.kept += 1
            stats.seg_fill[seg] += 1
            break

    if not synced:
        reason = "TIMEOUT_SYNC_SEG1_PKT0"
        if cs_gpio is not None and gpio is not None:
            gpio.write(cs_gpio, 1)
        return None, stats, last_hdrs, reason

    # -------- Optional "gate": require N consecutive good headers to reduce mid-chaos sync --------
    if gate_good_run > 0:
        good_run = 0
        gate_deadline = time.perf_counter() + 2.0
        while time.perf_counter() < gate_deadline and good_run < gate_good_run:
            pkt = read_packet(spi, packet_len)
            remember(pkt)
            rawid, seg, pnum = parse_vospi_header(pkt)
            if is_discard(seg, pnum):
                stats.discards += 1
                good_run = 0
                continue
            if seg < 1 or seg > 4:
                stats.invalid_seg += 1
                good_run = 0
                continue
            if pnum > 59:
                stats.bad_pnum += 1
                good_run = 0
                continue
            good_run += 1
        if good_run < gate_good_run:
            stats.gate_failures += 1
            # Release CS to resync next attempt (caller can loop)
            if cs_gpio is not None and gpio is not None:
                gpio.write(cs_gpio, 1)
            return None, stats, last_hdrs, "GATE_FAILED_UNSTABLE_STREAM"

    # -------- Phase B: collect remainder of frame --------
    deadline = t0 + timeout_s
    while time.perf_counter() < deadline and len(seg_payloads) < 240:
        pkt = read_packet(spi, packet_len)
        remember(pkt)
        rawid, seg, pnum = parse_vospi_header(pkt)

        if is_discard(seg, pnum):
            stats.discards += 1
            continue
        if pnum > 59:
            stats.bad_pnum += 1
            continue
        if seg < 1 or seg > 4:
            stats.invalid_seg += 1
            continue

        key = (seg, pnum)
        if key in seg_payloads:
            stats.duplicates += 1
            continue

        seg_payloads[key] = pkt[4:4+160]
        stats.kept += 1
        stats.seg_fill[seg] += 1

        if verbose and stats.kept % 30 == 0:
            print(f"[CAP] kept={stats.kept}/240 disc={stats.discards} invseg={stats.invalid_seg} dup={stats.duplicates} fill={stats.seg_fill}")

    if cs_gpio is not None and gpio is not None:
        gpio.write(cs_gpio, 1)
        if verbose:
            print(f"[VOSPI] CS(GPIO{cs_gpio})=HIGH (capture complete)")

    # Determine missing
    missing = []
    for seg in range(1, 5):
        for pnum in range(60):
            if (seg, pnum) not in seg_payloads:
                missing.append((seg, pnum))
    stats.missing = missing

    if missing:
        return None, stats, last_hdrs, "INCOMPLETE_FRAME_TIMEOUT"

    img = assemble_frame(seg_payloads)
    return img, stats, last_hdrs, "OK"

# ------------------------- Main program -------------------------

def print_last_headers(last_hdrs: List[PacketHdr]):
    print("\nLast headers (most recent last):")
    for h in last_hdrs[-20:]:
        print(f"  {h.t_ms:6d}ms  id=0x{h.rawid:04X} seg={h.seg:2d} pkt={h.pnum:4d}  hdr8=[{hexdump(h.hdr8,8)}]")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spi-dev", default="/dev/spidev0.0")
    ap.add_argument("--hz", type=int, default=12000000)
    ap.add_argument("--spi-mode", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--resync-ms", type=int, default=220)  # >185ms
    ap.add_argument("--i2c-bus", type=int, default=1)
    ap.add_argument("--i2c-addr", type=lambda x: int(x, 0), default=None,
                    help="7-bit I2C address. If omitted, script scans and picks the only device found (if unique).")
    ap.add_argument("--cs-gpio", type=int, default=None, help="BCM pin used as manual CS (active low).")
    ap.add_argument("--reset-gpio", type=int, default=None, help="BCM pin wired to RESET_L (active low). Optional.")
    ap.add_argument("--pwrdn-gpio", type=int, default=None, help="BCM pin wired to PW_DWN_L (active low). Optional.")
    ap.add_argument("--gate-good-run", type=int, default=0,
                    help="Require N consecutive valid packets after sync before collecting frame (helps mid-chaos sync).")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("=================================================")
    print("[START] Lepton diagnostic (CCI + VoSPI)")
    print(f"  spi={args.spi_dev} mode={args.spi_mode} hz={args.hz}")
    print(f"  i2c=/dev/i2c-{args.i2c_bus} addr={args.i2c_addr if args.i2c_addr is not None else 'AUTO'}")
    print(f"  timeout={args.timeout}s  resync_ms={args.resync_ms}  gate_good_run={args.gate_good_run}")
    print(f"  cs_gpio={args.cs_gpio} reset_gpio={args.reset_gpio} pwrdn_gpio={args.pwrdn_gpio}")
    print("=================================================")

    if not is_root():
        print("[WARN] Not running as root. If you see permission errors on spidev/i2c/gpio, run with sudo.\n")

    gpio = None
    try:
        if args.cs_gpio is not None or args.reset_gpio is not None or args.pwrdn_gpio is not None:
            if GPIO_BACKEND is None:
                print("[FATAL] GPIO requested but neither lgpio nor RPi.GPIO is available.")
                print("        Install one: sudo apt install python3-lgpio  (recommended on Pi 5)")
                return 2
            gpio = GpioCtl()
            gpio.open()
            if args.cs_gpio is not None:
                gpio.setup_out(args.cs_gpio, initial=1)  # deassert CS
            if args.reset_gpio is not None:
                gpio.setup_out(args.reset_gpio, initial=1)  # not in reset
            if args.pwrdn_gpio is not None:
                gpio.setup_out(args.pwrdn_gpio, initial=1)  # not powered down

        # Optional hard reset sequence (only if user wired it)
        if gpio is not None and args.reset_gpio is not None:
            print("[GPIO] Toggling RESET_L: LOW 20ms -> HIGH")
            gpio.write(args.reset_gpio, 0)
            time.sleep(0.020)
            gpio.write(args.reset_gpio, 1)

        # Per IDD: wait minimum 950 ms after releasing RESET_L before using I2C
        print("[I2C] Waiting 1.0s after reset release (spec minimum 950ms)...")
        time.sleep(1.0)

        # I2C open + scan
        with SMBus(args.i2c_bus) as bus:
            if args.i2c_addr is None:
                print("[I2C] Scanning bus for devices...")
                found = i2c_scan(bus)
                print(f"[I2C] Found {len(found)} device(s): {[hex(a) for a in found]}")
                if len(found) == 1:
                    addr = found[0]
                    print(f"[I2C] Using sole device address: 0x{addr:02X}")
                else:
                    print("[FATAL] I2C address not specified and scan was not unique.")
                    print("        Re-run with: --i2c-addr 0x2A (or whatever your scan shows).")
                    return 3
            else:
                addr = args.i2c_addr
                print(f"[I2C] Using provided address: 0x{addr:02X}")

            # Read STATUS register 0x0002 and decode bits per IDD startup procedure
            try:
                status = cci_read_u16(bus, addr, 0x0002)
                booted = (status >> 2) & 0x1
                busy = status & 0x1
                print(f"[I2C] STATUS(0x0002)=0x{status:04X}  boot(bit2)={booted} busy(bit0)={busy}")
                if booted == 0:
                    print("[FAIL] Camera not booted yet (STATUS bit2=0).")
                    print("       Meaning: power/clock/reset sequence not correct OR module not running.")
                    print("       Action: verify PW_DWN_L=HIGH, RESET_L=HIGH, 25MHz present, and wait longer.")
                    # Continue anyway to see what SPI does
                if busy == 1:
                    print("[I2C] Interface busy; polling until busy clears (max 2s)...")
                    t_end = time.time() + 2.0
                    while time.time() < t_end:
                        status = cci_read_u16(bus, addr, 0x0002)
                        busy = status & 0x1
                        if busy == 0:
                            break
                        time.sleep(0.02)
                    print(f"[I2C] STATUS now 0x{status:04X} busy(bit0)={busy}")
                    if busy == 1:
                        print("[FAIL] I2C STATUS busy bit never cleared.")
                        print("       Meaning: camera firmware stuck or host blocking access attempts.")
                        print("       Action: power-cycle with correct timing; ensure you waited >=950ms before first I2C access.")
            except Exception as e:
                print(f"[FAIL] Unable to read CCI STATUS register over I2C: {e}")
                print("       Meaning: wiring/address/pullups/power/reset sequence issue.")
                print("       Action: confirm SDA/SCL pullups and correct I2C address; confirm breakout power path.")
                return 4

        # SPI open
        no_cs = (args.cs_gpio is not None)
        spi = open_spi(args.spi_dev, mode=args.spi_mode, hz=args.hz, no_cs=no_cs)
        print(f"[SPI] Opened {args.spi_dev} mode={args.spi_mode} hz={args.hz} no_cs={int(no_cs)}")
        packet_len = 164  # 4-byte header + 160-byte payload (raw14 half-line)

        # Resync (spec): CS deasserted for >185ms with no clocks
        vospi_resync(args.cs_gpio, gpio, args.resync_ms, verbose=args.verbose)

        # Attempt capture
        img, stats, last_hdrs, reason = capture_one_frame(
            spi=spi,
            cs_gpio=args.cs_gpio,
            gpio=gpio,
            hz=args.hz,
            packet_len=packet_len,
            timeout_s=args.timeout,
            verbose=args.verbose,
            gate_good_run=args.gate_good_run,
        )

        # Close SPI
        try:
            spi.close()
        except Exception:
            pass

        if reason != "OK":
            print("\n================ FAILURE SUMMARY ================")
            print(f"Reason: {reason}")
            print(f"Stats: discards={stats.discards} kept={stats.kept} dup={stats.duplicates} "
                  f"invseg={stats.invalid_seg} bad_pnum={stats.bad_pnum} gate_failures={stats.gate_failures}")
            if stats.missing:
                print(f"Missing packets: {len(stats.missing)} (first 30): {stats.missing[:30]}")
            print(f"Seg fill: {stats.seg_fill}")
            print_last_headers(last_hdrs)
            print("=================================================\n")

            # Interpretation hints (explicit)
            if reason == "TIMEOUT_SYNC_SEG1_PKT0":
                print("[INTERPRETATION] Never saw seg=1 pkt=0 within sync window.")
                print("  Most common causes:")
                print("   1) CS is not behaving (not asserted when you think, or ringing/glitching)")
                print("   2) SCK idle level/mode wrong (Lepton wants SPI mode 3)")
                print("   3) MISO not actually connected / level shifting wrong")
                print("   4) Camera not outputting live video (power/reset/clock/boot issue)")
                if args.cs_gpio is None:
                    print("  STRONG NEXT STEP: use --cs-gpio to hold CS low for the whole capture.")
            elif reason == "INCOMPLETE_FRAME_TIMEOUT":
                print("[INTERPRETATION] You synced (saw seg=1 pkt=0) but couldn't collect all 240 packets in time.")
                print("  Most common causes:")
                print("   1) CS framing instability: packet boundaries are being broken => duplicates/invalid segments")
                print("   2) SPI clock too slow OR long delays between packets => you miss packets before next frame")
                print("   3) Signal integrity (long jumpers, weak GND reference, ringing)")
                if args.cs_gpio is None:
                    print("  STRONG NEXT STEP: manual CS via --cs-gpio and shorten wiring.")
            elif reason == "GATE_FAILED_UNSTABLE_STREAM":
                print("[INTERPRETATION] Stream is too chaotic right after sync; lots of discards/invalid segs.")
                print("  This points strongly at CS/clock integrity or capture timing.")
            return 10

        # Success -> write outputs
        print("\n[SUCCESS] Captured full 160x120 frame.")
        out_pgm = "frame.pgm"
        write_pgm(out_pgm, img)
        print(f"[WRITE] {out_pgm} (16-bit PGM)")

        out_png = "frame.png"
        if maybe_write_png(out_png, img):
            print(f"[WRITE] {out_png} (8-bit normalized preview)")
        else:
            print("[INFO] Pillow not installed; skipping PNG. Install: pip install pillow")

        # Quick sanity stats
        print(f"[FRAME] min={int(img.min())} max={int(img.max())} mean={float(img.mean()):.1f}")
        print("[DONE]")
        return 0

    finally:
        if gpio is not None:
            gpio.close()

if __name__ == "__main__":
    raise SystemExit(main())