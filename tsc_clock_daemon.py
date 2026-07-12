"""
TSC Clock Daemon - orologio testimone always-on (idle) per AWS
==============================================================
Gira come servizio systemd a priorita' minima (scheduler IDLE, nice 19)
su un'istanza sempre accesa. Ogni CICLO_S secondi:

  * aggiorna lo stato dell'orologio TSC (vedi tsc_clock_aws.TSCClockAWS);
  * scrive /run/tsc_clock/state.json (atomico: tmp + rename);
  * tiene aggiornato il payload servito dall'endpoint HTTP.

Endpoint HTTP (default 0.0.0.0:8080, GET qualunque path):
  {
    "tsc_epoch":   epoch (s) derivato dal TSC free-running,
    "sys_epoch":   epoch (s) del clock di sistema (NTP/chrony),
    "drift_us":    tsc_epoch - sys_epoch in microsecondi,
    "freq_ghz":    frequenza TSC calibrata,
    "boot_id":     boot corrente (cambia a ogni reboot/migrazione host),
    "calibrated_at": quando e' stata fatta la calibrazione,
    "started_at":  avvio del demone,
    "uptime_s":    vita del demone,
    "seq":         contatore cicli (deve crescere: prova di vitalita')
  }

Ruolo: TESTIMONE del tempo, non fonte primaria. I consumatori (bot)
continuano a usare il clock locale NTP e confrontano questo valore per
rilevare divergenze anomale — vedi fork2/timebase.py nel repo polytubot.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tsc_clock_aws import TSCClockAWS

CICLO_S = float(os.environ.get("TSC_DAEMON_INTERVAL_S", "10"))
HTTP_PORT = int(os.environ.get("TSC_DAEMON_PORT", "8080"))
STATE_DIR = Path(os.environ.get("TSC_DAEMON_STATE_DIR", "/run/tsc_clock"))
STATE_PATH = STATE_DIR / "state.json"
# Registro PERSISTENTE (non /run) dei rapporti di irraggiungibilità che i
# client inviano via POST quando l'orologio torna interrogabile: chi ha
# fallito, da quando, quanti tentativi, con quali errori.
REPORTS_PATH = Path(os.environ.get(
    "TSC_DAEMON_REPORTS", "/home/ec2-user/tsc_clock_outage_reports.jsonl"))
REPORTS_MAX_BODY = 64 * 1024

_lock = threading.Lock()
_payload: dict = {}
# Riferimenti vivi per calcolare il tempo FRESCO a ogni richiesta HTTP:
# servire lo snapshot dell'ultimo ciclo (fino a CICLO_S vecchio) farebbe
# misurare ai client la staleness del payload, non l'offset tra orologi
# (bug osservato il 2026-07-11: "offset" di -1.28 s = età dello snapshot).
_clk: "TSCClockAWS | None" = None
_started_at: float = 0.0
_seq = 0


def _update_payload(clk: TSCClockAWS, started_at: float, seq: int) -> dict:
    now_tsc = clk.tsc_time()
    now_sys = clk.sys_time()
    d = {
        "tsc_epoch": now_tsc,
        "sys_epoch": now_sys,
        "tsc_iso": datetime.fromtimestamp(
            now_tsc, tz=timezone.utc).isoformat(),
        "drift_us": (now_tsc - now_sys) * 1e6,
        "freq_ghz": clk.freq / 1e9,
        "boot_id": clk.boot_id,
        "calibrated_at": getattr(clk, "calibrated_at", None),
        "started_at": datetime.fromtimestamp(
            started_at, tz=timezone.utc).isoformat(),
        "uptime_s": now_sys - started_at,
        "seq": seq,
    }
    with _lock:
        _payload.clear()
        _payload.update(d)
    return d


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802  (firma imposta da BaseHTTPRequestHandler)
        if self.path.startswith("/reports"):
            # Ultimi rapporti di irraggiungibilità ricevuti dai client.
            lines: list = []
            try:
                if REPORTS_PATH.exists():
                    lines = REPORTS_PATH.read_text(
                        encoding="utf-8").splitlines()[-20:]
            except OSError:
                pass
            self._send_json({"count": len(lines),
                             "reports": [json.loads(l) for l in lines
                                         if l.strip()]})
            return
        # Tempo FRESCO a ogni richiesta (tsc_time/sys_time sono letture
        # locali a costo di nanosecondi), non lo snapshot del ciclo.
        if _clk is not None:
            self._send_json(_update_payload(_clk, _started_at, _seq))
        else:
            with _lock:
                self._send_json(dict(_payload))

    def do_POST(self):  # noqa: N802
        """Riceve il rapporto di un client che NON è riuscito a
        interrogare l'orologio: perché, da quando, quanti tentativi, con
        quale log di errori. Registrato con il timestamp dell'orologio
        stesso (ora che è di nuovo interrogabile) e l'IP del mittente."""
        try:
            length = min(int(self.headers.get("Content-Length") or 0),
                         REPORTS_MAX_BODY)
            raw = self.rfile.read(length) if length else b""
            try:
                report = json.loads(raw.decode())
            except (ValueError, UnicodeDecodeError):
                report = {"_raw": raw.decode(errors="replace")[:2000]}
            rec = {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "received_at_tsc": (
                    datetime.fromtimestamp(_clk.tsc_time(),
                                           tz=timezone.utc).isoformat()
                    if _clk is not None else None),
                "client_ip": self.client_address[0],
                "report": report,
            }
            with REPORTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"outage report da {rec['client_ip']}: "
                  f"source={report.get('source')} "
                  f"attempts={report.get('attempts')} "
                  f"first_fail={report.get('failed_first_at')}", flush=True)
            self._send_json({"ok": True})
        except Exception as e:  # mai far cadere il server per un report
            print(f"report POST err: {e!r}", flush=True)
            self._send_json({"ok": False}, status=500)

    def log_message(self, fmt, *args):  # silenzia l'access log
        pass


def main() -> int:
    global _clk, _started_at, _seq
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    clk = TSCClockAWS(calibration_secs=2.0)
    started_at = time.time()
    _clk, _started_at = clk, started_at
    print(f"tsc_clock_daemon: freq={clk.freq/1e9:.6f} GHz "
          f"boot_id={clk.boot_id} port={HTTP_PORT} ciclo={CICLO_S}s",
          flush=True)

    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    seq = 0
    while True:
        seq += 1
        _seq = seq
        d = _update_payload(clk, started_at, seq)
        tmp = STATE_PATH.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(d))
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            print(f"state write err: {e!r}", flush=True)
        # Log di vitalita' ogni ~10 minuti, non a ogni ciclo.
        if seq % max(1, int(600 / CICLO_S)) == 1:
            print(f"alive seq={seq} drift={d['drift_us']:+.1f}us "
                  f"uptime={d['uptime_s']:.0f}s", flush=True)
        time.sleep(CICLO_S)


if __name__ == "__main__":
    raise SystemExit(main())
