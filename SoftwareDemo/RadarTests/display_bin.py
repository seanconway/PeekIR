#!/usr/bin/env python3
"""
Display a single IWR1443/DCA1000 .bin file like the SAR reconstruction viewer:
- X axis = Horizontal (mm)
- Y axis = Vertical (mm), from range/depth (positive only)
- Depth Z slider = select which depth (Z) slice/window to view (mm)
Usage: python display_bin.py /path/to/adc_data_Raw_0.bin
"""
import argparse
import sys
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider
from scipy.fft import fft, fftshift

# IWR1443 typical
SAMPLES_PER_CHIRP = 512

# Same as mainSARneuronauts2py_rev3_2.py
C = 299792458.0
F_S = 9121e3
K = 63.343e12
DX_MM = 18 * 0.018  # 0.324 mm

# Depth window half-width (mm) shown around selected Z
Z_WINDOW_HALF_MM = 50.0


def load_bin(path, rx_antennas=4, samples_per_chirp=SAMPLES_PER_CHIRP):
    """Load DCA1000 raw .bin (int16). Returns complex (samples_per_chirp, num_chirps)."""
    raw = np.fromfile(path, dtype=np.int16)
    if rx_antennas == 2:
        ch = raw[0::4] + 1j * raw[2::4]
    else:
        ch = raw[0::8] + 1j * raw[4::8]
    total_complex = len(ch)
    num_chirps = total_complex // samples_per_chirp
    if num_chirps == 0:
        raise ValueError(
            f"File too small: {total_complex} complex samples, need at least {samples_per_chirp} per chirp"
        )
    keep = num_chirps * samples_per_chirp
    ch = ch[:keep].reshape(num_chirps, samples_per_chirp).T  # (samples, chirps)
    return ch


def range_bin_to_mm(n_fft, f_s=F_S, k=K, c=C):
    """Return range in mm for each FFT bin (fftshifted). Second half = positive range."""
    range_res_m = (c * f_s) / (2 * k * n_fft)
    bin_centers = np.arange(n_fft) - n_fft // 2
    range_m = bin_centers * range_res_m
    return range_m * 1e3  # mm


def main():
    parser = argparse.ArgumentParser(
        description="Display .bin: Horizontal (mm), Vertical (mm), Depth Z slider"
    )
    parser.add_argument("file", nargs="?", type=str, help="Path to .bin file")
    parser.add_argument("--file", "-f", dest="file_opt", type=str, help="Path to .bin file (alternative)")
    parser.add_argument("--2rx", dest="rx2", action="store_true", help="2 RX antennas (default 4 RX)")
    parser.add_argument("--nfft", type=int, default=1024, help="Range FFT size")
    parser.add_argument("--dx", type=float, default=DX_MM, help="Horizontal step per chirp (mm)")
    args = parser.parse_args()

    path = args.file or args.file_opt
    if not path:
        print("Usage: python display_bin.py <path/to/file.bin>", file=sys.stderr)
        sys.exit(1)

    rx = 2 if args.rx2 else 4
    data = load_bin(path, rx_antennas=rx)
    nfft = args.nfft
    spec = fft(data, n=nfft, axis=0)
    spec = fftshift(spec, axes=0)
    mag = np.abs(spec)
    mag_db = 20 * np.log10(mag + 1e-12)

    # Use only positive range (depth) so Vertical axis is 0 to max mm
    half = nfft // 2
    mag_db = mag_db[half:, :]   # (range_bins, chirps), range >= 0
    range_mm = range_bin_to_mm(nfft)[half:]  # positive range only (0 to max mm)

    num_chirps = data.shape[1]
    x_mm = args.dx * (np.arange(num_chirps) - (num_chirps - 1) / 2.0)

    z_min_mm = float(range_mm[0])
    z_max_mm = float(range_mm[-1])
    z_center_init = min(350.0, (z_min_mm + z_max_mm) / 2)

    print(f"Loaded: {path}")
    print(f"Horizontal: {x_mm[0]:.2f} to {x_mm[-1]:.2f} mm ({num_chirps} chirps)")
    print(f"Vertical (depth): {z_min_mm:.1f} to {z_max_mm:.1f} mm")

    fig, ax = plt.subplots(figsize=(10, 6))
    plt.subplots_adjust(bottom=0.2)

    im = ax.imshow(
        mag_db,
        extent=[x_mm[0], x_mm[-1], z_min_mm, z_max_mm],
        aspect="auto",
        origin="lower",
        cmap="jet",
    )
    ax.set_xlabel("Horizontal (mm)")
    ax.set_ylabel("Vertical (mm)")
    ax.set_title(f"SAR Reconstruction (Z={z_center_init:.1f} mm)")

    # Depth Z slider: select which depth slice/window to view (like first image)
    ax_slider = plt.axes([0.2, 0.08, 0.6, 0.03])
    slider = Slider(
        ax_slider,
        "Depth Z: ",
        z_min_mm,
        z_max_mm,
        valinit=z_center_init,
        valstep=max(1.0, (z_max_mm - z_min_mm) / 200.0),
    )

    def update(z_val):
        half_win = Z_WINDOW_HALF_MM
        y_lo = max(z_min_mm, z_val - half_win)
        y_hi = min(z_max_mm, z_val + half_win)
        ax.set_ylim(y_lo, y_hi)
        ax.set_title(f"SAR Reconstruction (Z={z_val:.1f} mm)")
        fig.canvas.draw_idle()

    slider.on_changed(update)
    update(z_center_init)  # set initial window

    cbar = plt.colorbar(im, ax=ax, label="Intensity")
    plt.show()


if __name__ == "__main__":
    main()
