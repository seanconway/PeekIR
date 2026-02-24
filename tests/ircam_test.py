import spidev
import numpy as np
import cv2
import time

SPI_BUS = 10
SPI_DEVICE = 0  # CE0 (GPIO7)
SPI_SPEED = 20000000
PACKET_SIZE = 164
PACKETS_PER_SEGMENT = 60
SEGMENTS = 4

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
print(f"[INIT] Starting frame capture loop...")

def read_packet():
    return bytes(spi.readbytes(PACKET_SIZE))

def get_frame():
    frame = np.zeros((120, 160), dtype=np.uint16)
    segments_received = set()
    packet_count = 0
    discard_count = 0
    invalid_segment_count = 0
    start_time = time.time()

    while len(segments_received) < 4:
        packet = read_packet()
        packet_count += 1

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
                print(f"[FRAME] Invalid segment #{segment_number} (count: {invalid_segment_count})")
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