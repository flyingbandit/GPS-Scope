First downloadable release of GPS Scope.

## Windows (10/11): monitor the GPS on your quad through the flight controller
1. Install Python 3 from https://www.python.org/downloads/ (tick **Add python.exe to PATH**).
2. Download the **Windows** zip below and unzip it.
3. Plug the quad in by USB (battery connected if the GPS/receiver needs it) and **close Betaflight Configurator**.
4. Double-click **`start_windows.bat`**. The first run installs the Python packages, then it puts the FC into GPS passthrough (Betaflight / INAV) and opens the dashboard at http://127.0.0.1:8080.

Options: `start_windows.bat --fc COM5` names the FC's port; `start_windows.bat --port COM7` uses a GPS on a USB-UART adapter. Unplug the FC to leave passthrough; all GPS settings GPS Scope makes are RAM-only.

## Raspberry Pi
Download the **Pi** `.tar.gz` below, then on the Pi:
```
tar xzf GPS-Scope-*-pi.tar.gz && cd gpsscope && bash install.sh && sudo reboot
```
Then open `http://<your-pi>.local:8080`. Or clone the repo: `git clone https://github.com/flyingbandit/GPS-Scope.git gpsscope`. See the README.

## In this release
- Live RF spectrum (MON-SPAN, in dB as u-center shows it) with waterfall, baseline and peak hold
- Per-satellite C/N0, sky plot, front-end gain cut (PGA), jamming indicator, position error
- Several GPS logged at once, labelled phases (Baseline / RX on / RX off) and run analysis
- Read-only u-center relay (`tcp://<host>:2101`) and raw `.ubx` recording per run
- **Share** export with no position data, for posting logs publicly
- Windows support and Betaflight/INAV GPS passthrough

Raw run folders and `raw.ubx` files contain your exact position. Share the **Share** export instead.
