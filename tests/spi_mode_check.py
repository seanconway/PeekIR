import spidev
import time
from collections import Counter

SPI_BUS = 0
SPI_DEVICE = 0          # CE0 (/dev/spidev0.0)
SPEED = 8_000_000
PACKET_SIZE = 164
N = 4000                # packets to sample per mode

def read_packet(spi):
    return bytes(spi.xfer2([0]*PACKET_SIZE))

def is_discard(p):
    return (p[0] & 0x0F) == 0x0F

def seg(p):
    return (p[0] >> 4) & 0x0F

def pkt(p):
    return p[1]

for mode in [0,1,2,3]:
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEVICE)
    spi.mode = mode
    spi.max_speed_hz = SPEED
    time.sleep(0.1)

    discards = 0
    seg_counts = Counter()
    pkt_counts = Counter()
    plausible = 0

    for _ in range(N):
        p = read_packet(spi)
        if is_discard(p):
            discards += 1
            continue
        s = seg(p)
        k = pkt(p)
        seg_counts[s] += 1
        pkt_counts[k] += 1

        # "Plausible" for Lepton 3.x: segment mostly in 0..4, packet in 0..59
        if (0 <= s <= 4) and (0 <= k <= 59):
            plausible += 1

    spi.close()

    print(f"\nMODE {mode}:")
    print(f"  discards: {discards}/{N} ({discards/N*100:.1f}%)")
    print(f"  seg_counts (top): {seg_counts.most_common(8)}")
    print(f"  pkt_counts (top): {pkt_counts.most_common(8)}")
    print(f"  plausible non-discard packets: {plausible}")

print("\nPick the mode with:")
print("- much lower discard %,")
print("- seg_counts concentrated in 1..4 (and maybe some 0),")
print("- packet numbers mostly within 0..59, not random.")