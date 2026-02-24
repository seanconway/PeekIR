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

spi = spidev.SpiDev()
spi.open(SPI_BUS, SPI_DEVICE)
spi.max_speed_hz = SPI_SPEED
spi.mode = 3

def read_packet():
    return bytes(spi.readbytes(PACKET_SIZE))

def get_frame():
    frame = np.zeros((120, 160), dtype=np.uint16)
    segments_received = set()

    while len(segments_received) < 4:
        packet = read_packet()

        # Discard packets
        if (packet[0] & 0x0F) == 0x0F:
            continue

        packet_number = packet[1]
        segment_number = (packet[0] >> 4)

        if segment_number < 1 or segment_number > 4:
            continue

        segments_received.add(segment_number)

        row = (segment_number - 1) * 30 + packet_number
        data = np.frombuffer(packet[4:], dtype=">u2")

        if row < 120:
            frame[row] = data

    return frame

try:
    while True:
        img16 = get_frame()

        # Normalize for display
        img8 = cv2.normalize(img16, None, 0, 255, cv2.NORM_MINMAX)
        img8 = np.uint8(img8)

        cv2.imshow("Lepton 3.x Thermal 160x120", img8)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

finally:
    cv2.destroyAllWindows()
    spi.close()