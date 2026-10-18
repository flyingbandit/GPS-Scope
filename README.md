# GPS Scope

![GPS Scope dashboard (simulated data)](docs/screenshot.png)


A Raspberry Pi tool for finding out whether an ELRS receiver (or VTX, camera, ESCs…) is degrading a u-blox M8 or M10 GPS. It talks to the GPS directly, so the flight controller is not involved. A live web UI on your phone or laptop shows:

- **RF spectrum** (M10 only). The receiver's built-in spectrum analyser covers about 1520–1650 MHz, so you can see GPS L1, Galileo E1, BeiDou B1I and GLONASS L1. It has max-hold, a frozen **baseline** overlay, and a **Δ vs baseline** view.
- **Per-satellite C/N0**, elevation, azimuth, used/not used, and per-signal detail, shown in a sky plot and a bar chart.
- **Per-constellation signal strength over time**, with alerts and your markers drawn on the chart.
- **Receiver front end**: jamming indicator, AGC, noise, PGA gain, and L1 in-band power, each on its own small chart.
- **Position drift**: set your true position, and the UI shows how far the reported fix wanders, with alerts above a threshold.
- **Events**: satellite losses (unexpected ones above 20° flagged separately), mass dropouts, simultaneous C/N0 dips, jamming state, fix changes and drift.
- **Labelled test runs** logged to CSV, plus a comparison table with Δ against your baseline run.

Every setting is written to the GPS's **RAM only**, so power-cycling it restores whatever your flight controller saved.

## Wiring (Pi UART)

| GPS | Raspberry Pi |
|---|---|
| TX | GPIO15 / RXD (pin 10) |
| RX | GPIO14 / TXD (pin 8) |
| GND | GND (pin 6) |
| VCC (5 V) | 5 V (pin 2 or 4). Check your board; most drone GPS boards take 5 V. |

The Pi's GPIO is 3.3 V. Drone GPS modules output 3.3 V logic, so they're safe to connect directly. A USB-UART adapter also works; use `GPS_PORT=/dev/ttyUSB0`.

Power the ELRS receiver from a **separate** 5 V supply (a BEC or USB power bank). It only needs power and to be bound to your radio. Don't connect its TX/RX to anything.

## Install

On the Pi (Raspberry Pi OS Bookworm or later):

```bash
git clone https://github.com/flyingbandit/GPS-Scope.git gpsscope
cd gpsscope && bash install.sh
sudo reboot
```

Or copy the folder over from a PC: `scp -r gpsscope pi@raspberrypi.local:~`, then run `install.sh` as above. Use your Pi's own user and hostname throughout.

Then open `http://raspberrypi.local:8080`. The installer sets GPS Scope up as a service, so it starts on every boot.

**Updating from a Windows PC:** `powershell -ExecutionPolicy Bypass -File .\update.ps1 -Pi pi@raspberrypi.local` copies the files, restarts the service and prints the running version. Put `user@host` in `pi-target.txt` next to the script to make that the default. The **Commands** panel in the UI lists the other handy one-liners, already filled in with your Pi's address.

**In the field with no Wi-Fi**, make the Pi its own hotspot:
`sudo nmcli dev wifi hotspot ssid GPSScope password gpsscope123`.
Join that network on your phone and open `http://10.42.0.1:8080`. Map tiles won't load without internet, but everything else works, including the drift plot.

**Try it without hardware:** `python3 gpsscope.py --sim`. Use the *Sim: interference* button in the header to toggle a fake noisy receiver.

## Test procedure

1. Put the GPS outside with a clear sky, on something non-metallic, and leave it alone for **at least 5 minutes** so the satellite set and accuracy settle.
2. **Set the reference position** with *Use 60 s average*. A position read off Google Maps is also fine.
3. With the ELRS RX unpowered, start a run labelled *Baseline* and press **Capture baseline** on the spectrum. Record about 2 minutes.
4. Change **one thing**, start a new run, and record 2 minutes. Repeat for each condition, for example:
   - RX powered but unbound, then bound with telemetry 1:2, then telemetry off
   - RX at max power, and in WiFi mode (older ELRS starts WiFi about 60 s after boot if it isn't bound)
   - RX at 2, 5, 10 and 15 cm, and above or below the GPS
   - Copper tape or shielding, then VTX on, then the full quad armed
5. Go back to the baseline at the end. If it doesn't match the first baseline, the sky changed under you and the comparison is less reliable.

Satellites move, so compare runs taken close together, and only trust differences bigger than about 1–2 dB.

## Several GPS at once

GPS Scope finds every u-blox receiver on its own:
- the Pi's GPIO UARTs (`/dev/serial0` and `/dev/ttyAMA*`)
- USB-UART adapters (`/dev/ttyUSB*`, `/dev/ttyACM*`)
- anything you plug in while it's running.

It finds them by **listening only**. It holds the DTR/RTS lines low so ESP32 boards don't reset, never opens the Bluetooth UART, and never writes to a port unless it has already heard a GPS on it. To keep it away from a port entirely, use `--exclude /dev/ttyACM0` or `GPS_SCAN_EXCLUDE=...`.

- **More GPIO UARTs (Pi 4 / Pi 5):** `GPS_UARTS=5 bash install.sh`, then reboot. **Wiring → Several GPS** shows the pin for each UART. Pi 3 / Zero 2 W have only one reliable GPIO UART, so add more GPS through USB-UART adapters. Use a **powered** USB hub beyond a few units.
- **Spec per port:** click **Spec** (or **Edit spec** in Overview) for each GPS. Fill in the product, antenna, mounting, distance from the RX and notes, plus the **role**:
  - **Control:** well away from the RX
  - **Test:** near the RX
  - **Ignore**
  
  The spec belongs to the physical port: the GPIO UART, or a USB socket via `/dev/serial/by-path`. It's stamped into every run (`receiver.json`). If a GPS with a different chip ID turns up on a port, you get a warning to check that port's spec.
- **One run, all receivers:** Start/Stop and the phase buttons apply to every receiver at once. A run folder now holds one subfolder per receiver plus `run.json`.
- **Overview tab:** a card for each receiver, and Top-6 C/N0, gain cut and movement for every receiver on shared charts. The **Receiver comparison** shows each receiver's change from its own baseline, and **vs control**: that change minus the control receiver's change in the same phase. Because they all see the same sky at the same moment, the vs-control figure removes satellite movement and weather. What's left is the effect at that receiver's position.
- Try it without hardware: `python3 gpsscope.py --sim --sim-count 4` (receiver 1 is a control).

Each receiver runs at 10 Hz, so a Pi 4 or 5 comfortably handles several. On a Pi 3, expect about 3.

## Live view in u-center

Each GPS also streams its raw UBX data on a TCP port while GPS Scope keeps logging: GPS1 on **2101**, GPS2 on **2102**, and so on. The port is shown on each receiver card. In **u-center 2**, add a TCP data source (Ctrl+T) and enter `tcp://raspberrypi:2101`: use the Pi's bare hostname or its IP address, because u-center 2 can't look up `.local` names. In u-center classic it's *Receiver → Connection → Network connection*. The relay is **read-only** for settings: u-center's queries (MON-VER, polls, CFG-VALGET) reach the GPS so it can identify the receiver, but anything that would change it, such as CFG-VALSET, saving config or a reset, is discarded. Start GPS Scope with `--ubx-tcp-write` if you want u-center to configure it, or `--ubx-tcp 0` to turn the relay off.

To check what a relay port is sending, run `python3 ubxpeek.py 2101` on the Pi: it counts each UBX message type for 5 seconds.

Every run also saves `raw.ubx` in each receiver's folder: the GPS's own byte stream for the whole run. u-center opens it with *File → Open* and can play it back, so anyone can step through a run in u-center's own panes. `raw.ubx` holds real positions, so it is left out of the **Share** export.

## Phases and run analysis

During a run, press **Baseline**, **RX on** or **RX off** in the header at the moment you make the change. The **+** button adds a custom phase, such as "RX 15 cm" or "Telemetry off". The **Run analysis** panel splits the run at those marks. **Analyse** in the run table does the same for a finished run, including older runs, where markers are used as phase boundaries.

The rules it follows keep the data unbiased:
- **The phases are yours.** They are never guessed from the data. What the data *detected*, such as a gain cut or a spike burst starting or ending, is listed separately. You get a warning if it happened well before or after your mark, because that phase then contains data belonging to its neighbour.
- **Values are medians with their IQR and sample count.** Nothing is trimmed. Phases shorter than 60 s are flagged as "short".
- **Paired C/N0 change** compares each satellite only with itself, using satellites tracked in both phases. Satellites rising or setting can't skew it. Baseline satellites that disappear are counted separately as "lost".
- **Movement** is measured from the mean of that run's own baseline-phase fixes, so it doesn't depend on the drift reference point. "GPS over-confident" is the share of fixes where the real movement was more than 2× the GPS's own hAcc.
- **Recovery after an "off" phase** is the time until satellites used are ≥ baseline − 1, top-6 C/N0 is ≥ baseline − 1 dB, PGA is ≥ baseline − 1 dB, and position error is ≤ max(1 m, 2× the baseline p95), all held for 10 s. If that never happens, it reports "not recovered within N s" rather than a time.

From the command line: `python3 analyse_run.py runs/<run folder>` prints the same report and writes `analysis.json`.

Suggested run shape: **≥ 60 s Baseline → ≥ 60 s RX on → ≥ 120 s RX off**.

## Reading the results

| You see | It means |
|---|---|
| C/N0 drops by the **same few dB on every constellation at once** (a *mass dip* alert) | Broadband noise is reaching the antenna. This is the classic signature of a noisy receiver. |
| One satellite drops at low elevation | Normal sky geometry. Logged as info only. |
| Satellites above 20° lost while strong | Unexpected. Check the Events log and sky plot for direction. |
| **Gain cut** tile/chart rises (PGA below baseline; on M8, AGC drop) | The receiver is turning its own gain down because strong energy is arriving in band. This is the most reliable sign of a noisy ELRS receiver, and it raises an alert at 3 dB. |
| **CW jamming indicator falls** while things get worse | Expected. It only detects single-tone (narrowband) jammers and is measured after the gain stage, so broadband noise drags it down. Don't use it on its own. |
| **PGA gain drops versus baseline** | The receiver turned its own gain down because something strong is in band. The spectrum trace then sits lower overall, so the Δ view **understates** the problem. Watch the PGA tile. |
| Narrow spikes on the spectrum | A spur from a clock or harmonic, e.g. a digital line or oscillator. Hover to read the frequency. |
| A raised floor across the whole band | Broadband noise, e.g. power-supply switching or digital noise. |
| Drift grows while hAcc stays small | The receiver is confidently wrong. This is the drone equivalent of poshold wander or toilet-bowling. |

The M8 has no spectrum analyser. It still reports the jamming indicator, AGC and noise, which are enough to A/B test.

## Notes

- Spectrum values are the receiver's raw relative dB, measured *after* its PGA (programmable gain amplifier). They're good for comparing like-for-like, not absolute power. That's why the PGA value is always shown and logged.
- An **always-on log** is written to `runs/_continuous/YYYYMMDD_timeline.csv` and `_events.csv` whether or not a run is active. Download it with **Download continuous log** in the run table.
- Spectrum **PGA comp.** is off by default, so the trace is exactly what the receiver reports. Turned on, it adds back the gain the receiver removed (about 2 dB of spectrum per PGA dB) as an estimate of the energy arriving. Once the PGA reaches its minimum the front end is saturated and the estimate is unreliable; the page says so. The PGA reduction tile is the reliable measure of how hard the receiver is being hit.
- Logs are saved in `runs/<timestamp>_<label>/`: `sats.csv`, `rf.csv`, `pvt.csv`, `spectrum.csv`, `timeline.csv`, `events.csv` and `summary.json`. The UI's **CSV** button zips a run for download.
- **Sharing a run:** the **Share** button in the run table, or `python3 share.py runs/<run folder>`, makes a copy that is safe to post. It has no latitude, longitude or altitude: position is kept as metres from each receiver's own baseline average. Times become seconds from the start of the run. Satellites get random names, azimuth is removed and elevation is rounded to 10°. The chip serial, the drift reference point and the spec's location and notes fields are also removed. GPS model, firmware, product and every RF figure stay in, and `analyse_run.py` works on the shared copy. The **CSV** button still gives the full log.
- Command-line options: `--port`, `--baud` (default: auto-detect), `--set-baud 115200`, `--keep-nmea`, `--http 0.0.0.0:8080`, `--logdir`.

## Files

| File | What it does |
|---|---|
| `gpsscope.py` | The logger and web server. Talks UBX to each GPS, writes runs, serves the UI. |
| `static/index.html` | The whole web UI, in one file. |
| `analysis.py` | Phase analysis used by the UI and the CLI. |
| `analyse_run.py` | Re-analyse a recorded run from the command line. |
| `share.py` | Makes the anonymised **Share** copy of a run. |
| `ubxpeek.py` | Counts the UBX messages on a u-center relay port. |
| `install.sh` | Pi installer: packages, UARTs, systemd service. |
| `update.ps1` | Push an update from a Windows PC to the Pi. |

## Contributing

Issues and pull requests are welcome, especially logs from other GPS modules or RF sources. Please post the **Share** export rather than a raw run folder: raw runs, and `raw.ubx` in particular, contain your exact position.

## Licence

MIT. See [LICENSE](LICENSE).
