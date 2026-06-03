"""
battery_monitor.py — Monitor batteria con rilevamento anomalie e log persistente.

Batteria rilevata: Lenovo L19C4PDC (Celxpert)
  Design capacity : 71.000 mWh
  Full charged    : 57.530 mWh  → salute 81.0%
  Cicli           : 651
  Autonomia attesa: ~5.7h scarica  (71Wh × 81% / 10.1W draw medio)
  Ricarica attesa : ~1.6h          (57.5Wh / ~38W net charge rate)
"""

import ctypes, time, json, os, datetime, threading
from dataclasses import dataclass, asdict
from typing import Optional

# ── Costanti batteria (da WMI BatteryStaticData) ─────────────────────────────
DESIGN_CAPACITY_MWH  = 71_000     # mWh progetto
FULL_CAPACITY_MWH    = 57_530     # mWh attuale (aggiornato a runtime)
HEALTH_PCT           = FULL_CAPACITY_MWH / DESIGN_CAPACITY_MWH * 100  # 81.0%
CYCLE_COUNT_BASELINE = 651

# Stima consumi medi (mW) da profilo di utilizzo e capacità batteria:
#   7h autonomia design → 71000/7 = 10143 mW
#   Autonomia corrente  → 57530/10143 = 5.67h
AVG_DRAW_MW         = 10_143      # consumo medio stimato
SLEEP_DRAIN_MW      = 400         # consumo tipico in Modern Standby (mW)

# Carica: ThinkBook usa caricatore 65W, net ~38W dopo efficienze
CHARGE_RATE_MW      = 38_000      # tasso carica medio efficace (mW)
CHARGE_TIME_H       = FULL_CAPACITY_MWH / CHARGE_RATE_MW  # ~1.51h

# Soglie anomalia
SLEEP_DRAIN_ANOMALY_PCT  = 8.0    # >8% drain/ora in sleep = anomalia
CHARGE_GAIN_ANOMALY_PCT  = 5.0    # guadagno >5% inaspettato = anomalia
EXTRA_CYCLE_ANOMALY      = 3      # >3 cicli extra rispetto al previsto = anomalia


DATA_DIR = os.path.join(os.environ.get("APPDATA", "."), "TSCClock")
STATE_FILE   = os.path.join(DATA_DIR, "state.json")
BATTERY_LOG  = os.path.join(DATA_DIR, "battery_log.jsonl")

os.makedirs(DATA_DIR, exist_ok=True)


# ── WMI helpers ──────────────────────────────────────────────────────────────
def _wmi_query(ns: str, cls: str) -> list[dict]:
    import subprocess, re
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-WmiObject -Namespace '%s' -Class %s | ConvertTo-Json -Depth 2" % (ns, cls)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0 or not r.stdout.strip():
        return []
    txt = r.stdout.strip()
    try:
        obj = json.loads(txt)
        return [obj] if isinstance(obj, dict) else obj
    except Exception:
        return []


@dataclass
class BatterySnapshot:
    timestamp:        float
    remaining_mwh:    int
    full_capacity_mwh:int
    design_capacity_mwh: int
    health_pct:       float
    charge_pct:       float
    cycle_count:      int
    is_charging:      bool
    is_on_ac:         bool
    voltage_mv:       int
    charge_rate_mw:   int    # positivo = carica, negativo = scarica
    discharge_rate_mw:int

    @classmethod
    def capture(cls) -> "BatterySnapshot":
        now = time.time()

        # Win32_Battery
        status_rows = _wmi_query("root\\wmi", "BatteryStatus")
        full_rows   = _wmi_query("root\\wmi", "BatteryFullChargedCapacity")
        cycle_rows  = _wmi_query("root\\wmi", "BatteryCycleCount")
        static_rows = _wmi_query("root\\wmi", "BatteryStaticData")

        s  = status_rows[0] if status_rows else {}
        f  = full_rows[0]   if full_rows   else {}
        cy = cycle_rows[0]  if cycle_rows  else {}
        st = static_rows[0] if static_rows else {}

        remaining  = int(s.get("RemainingCapacity", 0))
        full       = int(f.get("FullChargedCapacity", FULL_CAPACITY_MWH))
        design     = int(st.get("DesignedCapacity", DESIGN_CAPACITY_MWH))
        cycles     = int(cy.get("CycleCount", CYCLE_COUNT_BASELINE))
        charging   = bool(s.get("Charging", False))
        on_ac      = bool(s.get("PowerOnline", False))
        voltage    = int(s.get("Voltage", 0))
        ch_rate    = int(s.get("ChargeRate", 0))
        dis_rate   = int(s.get("DischargeRate", 0))

        if full > 0:
            charge_pct = remaining / full * 100
            health     = full / design * 100 if design > 0 else 0.0
        else:
            charge_pct = 0.0
            health     = HEALTH_PCT

        return cls(
            timestamp=now,
            remaining_mwh=remaining,
            full_capacity_mwh=full,
            design_capacity_mwh=design,
            health_pct=round(health, 2),
            charge_pct=round(charge_pct, 2),
            cycle_count=cycles,
            is_charging=charging,
            is_on_ac=on_ac,
            voltage_mv=voltage,
            charge_rate_mw=ch_rate,
            discharge_rate_mw=dis_rate,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["iso_time"] = datetime.datetime.fromtimestamp(self.timestamp).isoformat(timespec="seconds")
        return d


# ── Persistenza stato ─────────────────────────────────────────────────────────
def save_state(snap: BatterySnapshot, tsc_epoch: int, cycle_count_today: int = 0):
    state = snap.to_dict()
    state["tsc_epoch"]         = tsc_epoch
    state["cycle_count_today"] = cycle_count_today
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state() -> Optional[dict]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def append_battery_log(event: str, data: dict):
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
             "event": event, **data}
    with open(BATTERY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_battery_log(last_n: int = 200) -> list[dict]:
    if not os.path.exists(BATTERY_LOG):
        return []
    lines = []
    with open(BATTERY_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
    return lines[-last_n:]


# ── Analisi anomalie al wake-up ───────────────────────────────────────────────
@dataclass
class Anomaly:
    severity: str   # "info" | "warning" | "critical"
    code:     str
    message:  str
    detail:   str = ""


def check_wakeup_anomalies(prev: dict, curr: BatterySnapshot) -> list[Anomaly]:
    anomalies: list[Anomaly] = []

    prev_time    = prev.get("timestamp", 0.0)
    prev_mwh     = prev.get("remaining_mwh", 0)
    prev_cycles  = prev.get("cycle_count", CYCLE_COUNT_BASELINE)
    prev_on_ac   = prev.get("is_on_ac", False)

    now   = time.time()
    gap_s = now - prev_time
    gap_h = gap_s / 3600.0

    if gap_s < 60:      # riavvio quasi immediato → nessuna analisi sleep
        return anomalies

    # ── 1. Analisi gap temporale ──────────────────────────────────────────────
    expected_tsc_gap_s = gap_s   # il TSC dovrebbe corrispondere esattamente
    tsc_epoch_saved = prev.get("tsc_epoch")
    # (confronto TSC vs wall clock gestito da TSCEngine.drift_us — qui solo batteria)

    # ── 2. Drain in sleep ────────────────────────────────────────────────────
    mwh_delta  = prev_mwh - curr.remaining_mwh   # positivo = consumato
    drain_rate = mwh_delta / gap_h if gap_h > 0 else 0  # mW equivalente

    if not prev_on_ac and not curr.is_on_ac:
        # Era su batteria, ora su batteria
        expected_drain = SLEEP_DRAIN_MW * gap_h   # mWh attesi in sleep
        excess_mwh     = mwh_delta - expected_drain
        excess_pct_per_h = (mwh_delta / curr.full_capacity_mwh * 100) / gap_h if gap_h else 0

        if excess_pct_per_h > SLEEP_DRAIN_ANOMALY_PCT:
            anomalies.append(Anomaly(
                severity="warning",
                code="HIGH_SLEEP_DRAIN",
                message="Consumo elevato durante la sospensione",
                detail="Drenato %.0f mWh in %.1fh (%.1f%%/h, atteso <%.0f%%/h). "
                       "Cause: app in background, aggiornamenti, WakeOnLAN." % (
                    mwh_delta, gap_h, excess_pct_per_h, SLEEP_DRAIN_ANOMALY_PCT)
            ))

        if mwh_delta < 0:
            anomalies.append(Anomaly(
                severity="warning",
                code="CHARGE_WHILE_SLEEPING",
                message="Batteria aumentata durante la sospensione (senza AC)",
                detail="mWh prima: %d, dopo: %d. Delta: %+d mWh. Possibile lettura erronea o ricarica wireless." % (
                    prev_mwh, curr.remaining_mwh, -mwh_delta)
            ))

    elif not prev_on_ac and curr.is_on_ac:
        # Staccato → collegato durante il sonno
        anomalies.append(Anomaly(
            severity="info",
            code="AC_CONNECTED_DURING_SLEEP",
            message="Alimentatore collegato durante la sospensione",
            detail="Era su batteria a %.1f%%, ora in carica a %.1f%% dopo %.1fh." % (
                prev_mwh / curr.full_capacity_mwh * 100, curr.charge_pct, gap_h)
        ))

    elif prev_on_ac and not curr.is_on_ac:
        # Collegato → staccato durante il sonno
        anomalies.append(Anomaly(
            severity="info",
            code="AC_DISCONNECTED_DURING_SLEEP",
            message="Alimentatore scollegato durante la sospensione",
            detail="Era in carica, ora su batteria a %.1f%% dopo %.1fh." % (
                curr.charge_pct, gap_h)
        ))

    # ── 3. Cicli di ricarica anomali ─────────────────────────────────────────
    cycle_delta = curr.cycle_count - prev_cycles
    # 1 ciclo completo = 100% capacità caricata
    # In gap_h ore: se era in scarica, max 1 ciclo ogni ~5.7h
    expected_max_cycles = max(1, int(gap_h / (FULL_CAPACITY_MWH / AVG_DRAW_MW)) + 1)

    if cycle_delta > expected_max_cycles + EXTRA_CYCLE_ANOMALY:
        anomalies.append(Anomaly(
            severity="warning",
            code="EXCESS_CHARGE_CYCLES",
            message="Cicli di ricarica eccessivi durante la sospensione",
            detail="+%d cicli in %.1fh (attesi max %d). "
                   "Possibile problema termico o micro-cicli di ricarica." % (
                cycle_delta, gap_h, expected_max_cycles)
        ))

    if cycle_delta < 0:
        anomalies.append(Anomaly(
            severity="critical",
            code="CYCLE_COUNT_DECREASED",
            message="Contatore cicli diminuito — possibile sostituzione batteria o errore firmware",
            detail="Prima: %d, Ora: %d" % (prev_cycles, curr.cycle_count)
        ))

    # ── 4. Salute batteria degradata ─────────────────────────────────────────
    prev_health = prev.get("health_pct", HEALTH_PCT)
    health_drop = prev_health - curr.health_pct
    if health_drop > 2.0:
        anomalies.append(Anomaly(
            severity="warning",
            code="HEALTH_DEGRADED",
            message="Salute batteria calata di %.1f%% dall'ultima sessione" % health_drop,
            detail="Prima: %.1f%%, Ora: %.1f%%" % (prev_health, curr.health_pct)
        ))

    return anomalies


# ── Monitor in background ─────────────────────────────────────────────────────
class BatteryMonitor:
    def __init__(self, poll_interval_s: float = 30.0):
        self.interval       = poll_interval_s
        self.current:       Optional[BatterySnapshot] = None
        self.history:       list[BatterySnapshot]     = []
        self.wakeup_anomalies: list[Anomaly]          = []
        self._running       = False
        self._thread:       Optional[threading.Thread] = None
        self._lock          = threading.Lock()

    def start(self, prev_state: Optional[dict], tsc_epoch: int):
        self._running = True
        self._thread  = threading.Thread(target=self._loop,
                                          args=(prev_state, tsc_epoch),
                                          daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self, prev_state: Optional[dict], tsc_epoch: int):
        # Prima snapshot + analisi wake-up
        snap = BatterySnapshot.capture()
        with self._lock:
            self.current = snap
            self.history.append(snap)

        if prev_state:
            anomalies = check_wakeup_anomalies(prev_state, snap)
            with self._lock:
                self.wakeup_anomalies = anomalies
            if anomalies:
                append_battery_log("wakeup_anomalies",
                    {"count": len(anomalies),
                     "items": [{"code": a.code, "msg": a.message} for a in anomalies]})

        append_battery_log("session_start", snap.to_dict())
        save_state(snap, tsc_epoch)

        while self._running:
            time.sleep(self.interval)
            if not self._running:
                break
            try:
                snap = BatterySnapshot.capture()
            except Exception:
                continue
            with self._lock:
                self.current = snap
                self.history.append(snap)
                if len(self.history) > 500:
                    self.history = self.history[-500:]
            save_state(snap, tsc_epoch)

    def get_status_lines(self) -> list[str]:
        with self._lock:
            snap = self.current
            anom = list(self.wakeup_anomalies)
        if snap is None:
            return ["  Batteria: lettura in corso..."]

        lines = []
        ac_str   = "AC" if snap.is_on_ac else "Batteria"
        ch_str   = " [CARICA]" if snap.is_charging else ""
        dis_str  = " [SCARICA]" if not snap.is_on_ac and not snap.is_charging else ""

        lines.append("  %-22s %s%s%s" % (
            "Fonte alimentazione:", ac_str, ch_str, dis_str))
        lines.append("  %-22s %.1f%%  (%d / %d mWh)" % (
            "Carica:", snap.charge_pct, snap.remaining_mwh, snap.full_capacity_mwh))
        lines.append("  %-22s %.1f%%  (design: %d mWh, attuale: %d mWh)" % (
            "Salute batteria:", snap.health_pct,
            snap.design_capacity_mwh, snap.full_capacity_mwh))
        lines.append("  %-22s %d  (Li-ion max ~500-1000)" % (
            "Cicli di ricarica:", snap.cycle_count))

        # Autonomia stimata
        remaining_mwh = snap.remaining_mwh
        runtime_h = remaining_mwh / AVG_DRAW_MW
        lines.append("  %-22s %.1fh  (%.0f min) a consumo medio" % (
            "Autonomia residua:", runtime_h, runtime_h * 60))

        if snap.is_charging and not snap.is_on_ac:
            lines.append("  %-22s ??  (su batteria — impossibile)" % "Tempo ricarica:")
        elif snap.is_charging:
            mwh_to_full = snap.full_capacity_mwh - snap.remaining_mwh
            ch_h = mwh_to_full / CHARGE_RATE_MW if mwh_to_full > 0 else 0
            lines.append("  %-22s ~%.1fh  (~%.0f min) al 100%%" % (
                "Tempo ricarica:", ch_h, ch_h * 60))

        if snap.voltage_mv:
            lines.append("  %-22s %d mV  (%.3f V)" % (
                "Tensione:", snap.voltage_mv, snap.voltage_mv / 1000))

        if snap.charge_rate_mw:
            lines.append("  %-22s %d mW" % ("Tasso carica:", snap.charge_rate_mw))
        if snap.discharge_rate_mw:
            lines.append("  %-22s %d mW" % ("Tasso scarica:", snap.discharge_rate_mw))

        # Anomalie
        if anom:
            lines.append("")
            lines.append("  ⚠  ANOMALIE RILEVATE AL RIAVVIO (%d):" % len(anom))
            for a in anom:
                sev_icon = {"info": "ℹ", "warning": "⚠", "critical": "✖"}.get(a.severity, "?")
                lines.append("    %s [%s] %s" % (sev_icon, a.code, a.message))
                if a.detail:
                    lines.append("       %s" % a.detail)

        return lines

    def get_tray_tooltip(self) -> str:
        with self._lock:
            snap = self.current
        if snap is None:
            return "Batteria: ..."
        ac = "AC" if snap.is_on_ac else "Batt"
        return "%.0f%% %s | Salute %.0f%% | Cicli %d" % (
            snap.charge_pct, ac, snap.health_pct, snap.cycle_count)
