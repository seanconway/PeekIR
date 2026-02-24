import spidev
import numpy as np
import cv2
import time
from collections import Counter

SPI_BUS = 10
SPI_DEVICE = 0  # CE0 (GPIO7)
SPI_SPEED = 20000000
PACKET_SIZE = 164
PACKETS_PER_SEGMENT = 60
SEGMENTS = 4
MAX_DISCARD_BEFORE_RESYNC = 750  # ~4 frames worth at 27Hz
MAX_RESYNC_ATTEMPTS = 5

print(f"[INIT] Opening SPI bus {SPI_BUS}, device {SPI_DEVICE}")
spi = spidev.SpiDev()
try:
    spi.open(SPI_BUS, SPI_DEVICE)
    print(f"[INIT] SPI opened successfully")
except Exception as e:
    print(f"[ERROR] Failed to open SPI: {e}")
    raise

spi.max_speed_hz = SPI_SPEED
spi.mode = 3
print(f"[INIT] SPI configured: speed={SPI_SPEED} Hz, mode={spi.mode}")

# Wait for camera initialization
print(f"[INIT] Waiting 3 seconds for camera initialization...")
time.sleep(3)

print(f"[INIT] Running packet analysis...")

def read_packet():
    return bytes(spi.readbytes(PACKET_SIZE))

def analyze_packets(num_packets=200):
    """Analyze packet headers to diagnose communication issues"""
    print(f"[DIAG] Analyzing {num_packets} packets...")
    
    header_bytes = []
    packet_numbers = []
    segment_numbers = []
    discard_packets = 0
    
    for i in range(num_packets):
        packet = read_packet()
        header = packet[0]
        packet_num = packet[1]
        
        header_bytes.append(header)
        packet_numbers.append(packet_num)
        
        # Check if discard packet
        if (header & 0x0F) == 0x0F:
            discard_packets += 1
        else:
            segment_num = (header >> 4)
            segment_numbers.append(segment_num)
    
    # Analysis
    print(f"[DIAG] Results from {num_packets} packets:")
    print(f"[DIAG]   Discard packets: {discard_packets} ({100*discard_packets/num_packets:.1f}%)")
    print(f"[DIAG]   Valid packets: {num_packets - discard_packets}")
    
    # Show unique header bytes (first 10)
    unique_headers = Counter(header_bytes).most_common(10)
    print(f"[DIAG]   Most common header bytes:")
    for header, count in unique_headers:
        print(f"[DIAG]     0x{header:02X}: {count} times ({100*count/num_packets:.1f}%)")
    
    if segment_numbers:
        print(f"[DIAG]   Segments seen: {sorted(set(segment_numbers))}")
        print(f"[DIAG]   Packet number range: {min(packet_numbers)}-{max(packet_numbers)}")
    else:
        print(f"[DIAG]   WARNING: No valid segments detected!")
        print(f"[DIAG]   This indicates VoSPI sync issues or camera not initialized")
    
    # Check for pattern
    if discard_packets == num_packets:
        print(f"[DIAG]   PROBLEM: 100% discard packets - camera out of sync or not streaming")
    
    return discard_packets < num_packets

def resync_vospi(attempt=1):
    """Attempt to resynchronize VoSPI by cycling the SPI connection"""
    print(f"[RESYNC] Attempt {attempt}: Cycling SPI connection...")
    
    try:
        spi.close()
        # Wait ~5 frame periods at 27Hz (~185ms)
        time.sleep(0.2)
        spi.open(SPI_BUS, SPI_DEVICE)
        spi.max_speed_hz = SPI_SPEED
        spi.mode = 3
        print(f"[RESYNC] SPI reconnected")
        
        # Flush any stale data by reading and discarding packets
        print(f"[RESYNC] Flushing stale data...")
        for _ in range(100):
            read_packet()
        
        return True
    except Exception as e:
        print(f"[RESYNC] Failed: {e}")
        return False

# Run initial diagnostics
if not analyze_packets(200):
    print(f"\n[DIAG] Camera appears to be out of sync. Attempting resync...")
    for attempt in range(1, MAX_RESYNC_ATTEMPTS + 1):
        if resync_vospi(attempt):
            time.sleep(0.5)
            if analyze_packets(100):
                print(f"[DIAG] SUCCESS: Resync successful on attempt {attempt}!")
                break
        if attempt == MAX_RESYNC_ATTEMPTS:
            print(f"[DIAG] FAILED: Could not sync after {MAX_RESYNC_ATTEMPTS} attempts")
            print(f"[DIAG] Possible issues:")
            print(f"[DIAG]   1. Camera not powered or not connected")
            print(f"[DIAG]   2. Camera needs I2C initialization first")
            print(f"[DIAG]   3. Wrong SPI bus or device")
            print(f"[DIAG]   4. Hardware issue with camera")
            response = input("[DIAG] Continue anyway? (y/n): ")
            if response.lower() != 'y':
                raise RuntimeError("Camera synchronization failed")

print(f"\n[INIT] Starting frame capture loop...")

def get_frame():
    """Capture a complete frame with automatic resync on excessive discards"""
    frame = np.zeros((120, 160), dtype=np.uint16)
    segments_received = set()
    packet_count = 0
    discard_count = 0
    invalid_segment_count = 0
    start_time = time.time()
    resync_triggered = False

    while len(segments_received) < 4:
        # Check if we need to resync
        if discard_count > MAX_DISCARD_BEFORE_RESYNC and not resync_triggered:
            print(f"[FRAME] Too many discards ({discard_count}), triggering resync...")
            resync_vospi(1)
            resync_triggered = True
            discard_count = 0
            packet_count = 0
            segments_received.clear()
            continue
        
        packet = read_packet()
        packet_count += 1

        # Debug first few packets
        if packet_count <= 5:
            print(f"[FRAME] Packet {packet_count}: header=0x{packet[0]:02X}, byte1=0x{packet[1]:02X}, byte2=0x{packet[2]:02X}, byte3=0x{packet[3]:02X}")

        # Discard packets
        if (packet[0] & 0x0F) == 0x0F:
            discard_count += 1
            if discard_count % 100 == 0:
                print(f"[FRAME] Discarded {discard_count} packets so far...")
            continue

        packet_number = packet[1]
        segment_number = (packet[0] >> 4)

        if segment_number < 1 or segment_number > 4:
            invalid_segment_count += 1
            if invalid_segment_count % 50 == 0:
                print(f"[FRAME] Invalid segment #{segment_number} from header 0x{packet[0]:02X} (count: {invalid_segment_count})")
            continue

        if segment_number not in segments_received:
            segments_received.add(segment_number)
            print(f"[FRAME] Received segment {segment_number}/4 (packets read: {packet_count})")

        row = (segment_number - 1) * 30 + packet_number
        data = np.frombuffer(packet[4:], dtype=">u2")

        if row < 120:
            frame[row] = data

    elapsed = time.time() - start_time
    print(f"[FRAME] Complete! Time: {elapsed:.2f}s, Packets: {packet_count}, Discarded: {discard_count}, Invalid: {invalid_segment_count}")
    
    if resync_triggered:
        print(f"[FRAME] Note: Frame required VoSPI resync")
    
    return frame

try:
    frame_num = 0
    while True:
        frame_num += 1
        print(f"\n[MAIN] === Capturing frame {frame_num} ===")
        
        img16 = get_frame()
        
        # Show frame statistics
        min_val, max_val = img16.min(), img16.max()
        mean_val = img16.mean()
        print(f"[MAIN] Frame {frame_num} stats: min={min_val}, max={max_val}, mean={mean_val:.1f}")

        # Normalize for display
        img8 = cv2.normalize(img16, None, 0, 255, cv2.NORM_MINMAX)
        img8 = np.uint8(img8)
        
        print(f"[MAIN] Displaying frame {frame_num}...")
        cv2.imshow("Lepton 3.x Thermal 160x120", img8)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            print("[MAIN] 'q' pressed, exiting...")
            break
        elif key != 255:
            print(f"[MAIN] Key pressed: {chr(key)} (code {key})")

except KeyboardInterrupt:
    print("\n[MAIN] Keyboard interrupt received, shutting down...")
except Exception as e:
    print(f"\n[ERROR] Exception occurred: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
finally:
    print("[CLEANUP] Closing windows and SPI...")
    cv2.destroyAllWindows()
    spi.close()
    print("[CLEANUP] Done.")