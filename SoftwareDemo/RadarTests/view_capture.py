#!/usr/bin/env python3
"""
Load one DCA1000 .bin file (single frame from single_capture.lua), compute range FFT,
and show range profile + range-time heatmap. Requires numpy, matplotlib, scipy.
"""
import argparse
import numpy as np
import matplotlib.pyplot as plt
from scipy.fft import fft, fftshift


# Match single_capture.lua: 512 samples per chirp, 800 chirps per frame
SAMPLES = 512
NUM_CHIRPS = 800


def load_bin(filename, rx_mode=4):
    """
    Load DCA1000 raw .bin (int16). Returns complex array shape (samples, num_chirps).
    rx_mode: 4 (I1 I2 I3 I4 Q1 Q2 Q3 Q4) or 2 (I1 I2 Q1 Q2).
    Uses channel 1 only; change indexing to use others or average.
    """
    data_int = np.fromfile(filename, dtype=np.int16)
    n = len(data_int)

    if rx_mode == 2:
        # 2 RX: I1 I2 Q1 Q2 per sample
        ch1 = data_int[0::4] + 1j * data_int[2::4]
    else:
        # 4 RX: I1 I2 I3 I4 Q1 Q2 Q3 Q4
        ch1 = data_int[0::8] + 1j * data_int[4::8]

    # One chirp = SAMPLES points; num_chirps chirps (chirp0, chirp1, ... in order)
    expected = SAMPLES * NUM_CHIRPS
    if len(ch1) < expected:
        ch1 = np.pad(ch1, (0, max(0, expected - len(ch1))), constant_values=0)
    ch1 = ch1[:expected].reshape(NUM_CHIRPS, SAMPLES).T  # (samples, chirps)
    return ch1


def main():
    parser = argparse.ArgumentParser(description="View one DCA1000 capture .bin file")
    parser.add_argument("--file", "-f", type=str, default="captures/capture.bin", help="Path to .bin file")
    parser.add_argument("--nfft", type=int, default=1024, help="Range FFT size")
    parser.add_argument("--2rx", dest="rx2", action="store_true", help="Use 2-RX format (default 4-RX)")
    args = parser.parse_args()

    rx_mode = 2 if args.rx2 else 4
    data = load_bin(args.file, rx_mode=rx_mode)
    print(f"Loaded {args.file}: shape {data.shape} (samples x chirps)")

    # Range FFT (along fast-time)
    nfft = args.nfft
    spec = fft(data, n=nfft, axis=0)
    spec = fftshift(spec, axes=0)
    mag = np.abs(spec)

    # Range profile (average over chirps)
    profile = np.mean(mag, axis=1)
    # Range–time (optional: dB)
    mag_db = 20 * np.log10(mag + 1e-12)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))

    ax1.plot(profile)
    ax1.set_xlabel("Range bin")
    ax1.set_ylabel("Magnitude")
    ax1.set_title("Range profile (avg over chirps)")
    ax1.grid(True)

    im = ax2.imshow(mag_db, aspect="auto", cmap="jet", origin="lower")
    ax2.set_xlabel("Chirp index")
    ax2.set_ylabel("Range bin")
    ax2.set_title("Range–time (dB)")
    plt.colorbar(im, ax=ax2, label="dB")

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
