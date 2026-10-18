#!/usr/bin/env python3
"""Count the UBX messages coming out of a GPS Scope u-center relay port.

  python3 ubxpeek.py            # GPS1 relay (port 2101), 5 seconds
  python3 ubxpeek.py 2102 10    # GPS2 relay, 10 seconds
"""
import socket
import sys
import time

NAMES = {"01-07": "NAV-PVT", "01-35": "NAV-SAT", "01-43": "NAV-SIG", "01-04": "NAV-DOP", "0A-38": "MON-RF",
         "0A-31": "MON-SPAN", "0A-04": "MON-VER", "0A-09": "MON-HW", "05-01": "ACK-ACK", "05-00": "ACK-NAK",
         "06-8B": "CFG-VALGET"}


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2101
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 5
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.settimeout(0.5)
    buf, end = b"", time.time() + secs
    while time.time() < end:
        try:
            d = s.recv(65536)
            if not d:
                break
            buf += d
        except socket.timeout:
            pass
    s.close()
    counts, i = {}, 0
    while True:
        i = buf.find(b"\xb5\x62", i)
        if i < 0 or i + 6 > len(buf):
            break
        k = f"{buf[i + 2]:02X}-{buf[i + 3]:02X}"
        counts[k] = counts.get(k, 0) + 1
        i += 8 + buf[i + 4] + 256 * buf[i + 5]
    print(f"port {port}: {len(buf)} bytes in {secs:g} s")
    for k, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k}  {NAMES.get(k, ''):10} {n:5}  ({n / secs:.1f}/s)")
    if "0A-31" not in counts:
        print("  no MON-SPAN in the stream")


if __name__ == "__main__":
    main()
