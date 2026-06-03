"""
battery_manager.py — Gestionale Batteria compatto
Design: "Obsidian Technical" (da Figma H78ipw47OQhXfihWmljvNN)
Layout: 3 colonne — BATTERIA | CODA PROCESSI | LUCE/STATO
"""

import tkinter as tk
from tkinter import ttk
import threading, time, datetime, json, os, sys, subprocess, math
import psutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from win_subprocess import run_hidden
from tsc_engine import TSCEngine
from power_meter import PowerMeter
from process_power import ProcessPowerMeter
from process_freezer import ProcessFreezer, FreezeError, PROTECTED
from battery_monitor import (
    BatteryMonitor, BatterySnapshot, load_state, save_state,
    append_battery_log, load_battery_log, DATA_DIR,
    DESIGN_CAPACITY_MWH, FULL_CAPACITY_MWH, AVG_DRAW_MW, CHARGE_RATE_MW,
)

# ─── Palette Obsidian Technical ───────────────────────────────────────────────
P = {
    "bg":      "#0d0d0e",
    "surface": "#131318",
    "card":    "#191c22",
    "border":  "#272b32",
    "text":    "#e5e5e5",
    "dim":     "#8c9099",
    "blue":    "#6ba6ff",
    "green":   "#3bc363",
    "yellow":  "#e5b632",
    "orange":  "#ef8c32",
    "red":     "#f85148",
    "tsc":     "#79c0ff",
    "rowA":    "#13161c",
    "rowB":    "#191c22",
}

WIN_W, WIN_H = 860, 380
COL1, COL2, COL3 = 164, 340, 340
GAP = 5
REFRESH_MS = 800


# ─── Luminosità (background poll, non sul thread UI) ─────────────────────────
_brightness_cache: int | None = None
_brightness_lock  = threading.Lock()

def _brightness_poll_loop(interval_s: float = 5.0):
    global _brightness_cache
    while True:
        try:
            r = run_hidden(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness)"
                 ".CurrentBrightness"],
                capture_output=True, text=True, timeout=6)
            v = r.stdout.strip()
            with _brightness_lock:
                _brightness_cache = int(v) if v.isdigit() else None
        except Exception:
            pass
        time.sleep(interval_s)

def get_brightness_cached() -> int | None:
    with _brightness_lock:
        return _brightness_cache

# Avvia il thread daemon al caricamento del modulo
threading.Thread(target=_brightness_poll_loop, daemon=True, name="brightness-poll").start()


def fmt_elapsed(start: float) -> str:
    s = int(time.time() - start)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h:  return "%dh %02dm" % (h, m)
    if m:  return "%dm %02ds" % (m, s)
    return "%ds" % s


# ─── Widget: gauge circolare ──────────────────────────────────────────────────
class CircGauge(tk.Canvas):
    def __init__(self, parent, size=90, **kw):
        super().__init__(parent, width=size, height=size,
                         bg=P["card"], highlightthickness=0, **kw)
        self._sz = size
        self._val, self._lbl, self._col = 0, "", P["green"]

    def update(self, val: float, lbl: str, col: str):
        self._val, self._lbl, self._col = val, lbl, col
        self._draw()

    def _draw(self):
        self.delete("all")
        s, pad = self._sz, 8
        r = (s - pad*2) / 2
        cx = cy = s / 2
        # bg ring
        self.create_arc(pad, pad, s-pad, s-pad,
                        start=90, extent=-360,
                        style="arc", outline=P["border"], width=5)
        # value arc
        if self._val > 0:
            self.create_arc(pad, pad, s-pad, s-pad,
                            start=90, extent=-(self._val/100*360),
                            style="arc", outline=self._col, width=5)
        # text
        self.create_text(cx, cy-7, text="%.0f%%" % self._val,
                         font=("JetBrains Mono", 12, "bold"),
                         fill=self._col)
        self.create_text(cx, cy+8, text=self._lbl,
                         font=("Consolas", 7), fill=P["dim"])


# ─── Widget: barra segmentata ─────────────────────────────────────────────────
class SegBar(tk.Canvas):
    def __init__(self, parent, w=148, h=9, **kw):
        super().__init__(parent, width=w, height=h,
                         bg=P["card"], highlightthickness=0, **kw)
        self._bw, self._bh = w, h

    def update(self, pct: float, col: str):
        self.delete("all")
        w, h = self._bw, self._bh
        self.create_rectangle(0, 0, w, h, fill=P["border"], outline="")
        segs = 10
        sw = (w - 4) / segs - 1
        filled = round(pct / 100 * segs)
        for i in range(segs):
            x = 2 + i * (sw + 1)
            fill = col if i < filled else P["bg"]
            self.create_rectangle(x, 2, x+sw, h-2, fill=fill, outline="")


# ─── Widget: sparkline ────────────────────────────────────────────────────────
class Sparkline(tk.Canvas):
    def __init__(self, parent, w=200, h=28, col=P["blue"], **kw):
        super().__init__(parent, width=w, height=h,
                         bg=P["card"], highlightthickness=0, **kw)
        self._sw, self._sh, self._col = w, h, col
        self._data: list[float] = []

    def push(self, v: float):
        self._data.append(v)
        if len(self._data) > 60: self._data = self._data[-60:]
        self._draw()

    def _draw(self):
        self.delete("all")
        d = self._data
        if len(d) < 2: return
        mn, mx = min(d), max(d)
        if mx == mn: mx = mn + 1
        w, h = self._sw, self._sh
        pts = []
        for i, v in enumerate(d):
            x = 1 + i / (len(d)-1) * (w-2)
            y = h-2 - (v-mn)/(mx-mn)*(h-4)
            pts += [x, y]
        if len(pts) >= 4:
            self.create_line(pts, fill=self._col, width=1.5, smooth=True)


# ─── App principale ───────────────────────────────────────────────────────────
class BatteryManagerApp:

    def __init__(self, engine: TSCEngine | None = None,
                 batt: BatteryMonitor | None = None):

        self.engine = engine or TSCEngine(calibration_s=2.0)
        self.batt   = batt
        if self.batt is None:
            self.batt = BatteryMonitor(poll_interval_s=30.0)
            self.batt.start(load_state(), self.engine.epoch_tsc)

        self.power = PowerMeter(interval_s=3.0,
                                brightness_cb=get_brightness_cached)
        self.power.start()
        # ProcessPowerMeter: usa il consumo totale come budget da distribuire
        self.proc_power = ProcessPowerMeter(
            interval_s=2.0,
            system_power_cb=lambda: self.power.current.power_mw
                            if self.power.current else None
        )
        self.proc_power.start()
        self.freezer = ProcessFreezer()
        self.freezer.start()
        self._bright_history: list[float] = []
        self._drift_history:  list[float] = []
        self._power_history:  list[float] = []

        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build(self):
        self.root = tk.Tk()
        self.root.title("Battery Manager")
        self.root.geometry("%dx%d" % (WIN_W, WIN_H))
        self.root.resizable(False, False)
        self.root.configure(bg=P["bg"])

        # Variabili tk create DOPO root
        self._sort_by_power = tk.BooleanVar(value=True)

        self._build_topbar()

        body = tk.Frame(self.root, bg=P["bg"])
        body.pack(fill="both", expand=True, padx=GAP, pady=(0, GAP))

        self._build_col1(body)   # BATTERIA
        self._build_col2(body)   # CODA PROCESSI
        self._build_col3(body)   # LUCE / STATO

        self._build_statusbar()
        self._tick()

    def _topbar_label(self, parent, text, fg, font, side="left", padx=8):
        lbl = tk.Label(parent, text=text, font=font, fg=fg, bg=P["surface"])
        lbl.pack(side=side, padx=padx, pady=0)
        return lbl

    def _build_topbar(self):
        tb = tk.Frame(self.root, bg=P["surface"], height=30)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        tk.Frame(self.root, bg=P["border"], height=1).pack(fill="x")

        tk.Label(tb, text="⚡  BATTERY MANAGER",
                 font=("Inter", 10, "bold"), fg=P["blue"],
                 bg=P["surface"]).pack(side="left", padx=12, pady=6)

        self._lbl_drift = tk.Label(tb, text="Δ--µs",
                                    font=("JetBrains Mono", 8), fg=P["green"],
                                    bg=P["surface"])
        self._lbl_drift.pack(side="right", padx=8)

        # Bottone Freeze All / Resume All
        self._btn_freeze_all = tk.Button(
            tb, text="[  FREEZE ALL  ]",
            font=("JetBrains Mono", 8, "bold"),
            fg=P["blue"], bg="#0d1a2e",
            activeforeground="#ffffff", activebackground="#1a3a5c",
            relief="groove", bd=2, cursor="hand2", padx=6, pady=1,
            command=self._toggle_freeze_all)
        self._btn_freeze_all.pack(side="right", padx=8, pady=4)

        self._lbl_tsc = tk.Label(tb, text="TSC --:--:--",
                                  font=("JetBrains Mono", 10, "bold"),
                                  fg=P["tsc"], bg=P["surface"])
        self._lbl_tsc.pack(side="right", padx=4)

        self._lbl_pill = tk.Label(tb, text="🔌 --%",
                                   font=("JetBrains Mono", 8, "bold"),
                                   fg=P["green"], bg=P["surface"],
                                   padx=6, pady=2,
                                   relief="flat")
        self._lbl_pill.pack(side="right", padx=10)

    # ── Col 1: BATTERIA ───────────────────────────────────────────────────────
    def _build_col1(self, parent):
        f = self._card(parent, "BATTERIA", COL1, side="left", fill="y")

        self._gauge1 = CircGauge(f, size=90)
        self._gauge1.pack(pady=(6, 2))

        self._seg1 = SegBar(f, w=COL1-16, h=9)
        self._seg1.pack(padx=8, pady=(0, 4))

        rows_f = tk.Frame(f, bg=P["card"])
        rows_f.pack(fill="x", padx=8)

        self._stat_lbls: dict[str, tk.Label] = {}
        stats = [
            ("Salute",    "--",  P["orange"]),
            ("Cicli",     "--",  P["yellow"]),
            ("Autonomia", "--",  P["green"] ),
            ("Ricarica",  "--",  P["blue"]  ),
            ("Tensione",  "--",  P["dim"]   ),
            ("Cap. att.", "--",  P["dim"]   ),
            ("Design",    "--",  P["border"]),
        ]
        for lbl, val, col in stats:
            row = tk.Frame(rows_f, bg=P["card"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=lbl, font=("Consolas", 7),
                     fg=P["dim"], bg=P["card"], anchor="w").pack(side="left")
            v = tk.Label(row, text=val, font=("JetBrains Mono", 8, "bold"),
                         fg=col, bg=P["card"], anchor="e")
            v.pack(side="right")
            self._stat_lbls[lbl] = v
            tk.Frame(rows_f, bg=P["border"], height=1).pack(fill="x")

        self._lbl_ac = tk.Label(f, text="",
                                 font=("JetBrains Mono", 7, "bold"),
                                 fg=P["green"], bg=P["card"],
                                 pady=2)
        self._lbl_ac.pack(fill="x", padx=8, pady=(4, 6))

    # ── Col 2: CODA PROCESSI ─────────────────────────────────────────────────
    def _build_col2(self, parent):
        f = self._card(parent, "CODA PROCESSI  — più recente ↑", COL2,
                       side="left", fill="both")
        self._col2_frame = f

        # Sezione congelati (appare solo quando ci sono processi frozen)
        self._frozen_banner = tk.Frame(f, bg="#0d2030")
        self._lbl_frozen_banner = tk.Label(
            self._frozen_banner,
            text="", font=("JetBrains Mono", 7, "bold"),
            fg="#79c0ff", bg="#0d2030", anchor="w")
        self._lbl_frozen_banner.pack(fill="x", padx=8, pady=2)
        # non pack ancora — lo mostriamo solo quando ci sono congelati

        # Header fisso
        hdr = tk.Frame(f, bg=P["surface"])
        hdr.pack(fill="x", padx=8, pady=(4, 0))
        for txt, w, anchor in [
            ("APPLICAZIONE", 14, "w"),
            ("POTENZA",       9, "e"),
            ("",              2, "c"),
            ("CPU%",          5, "e"),
            ("DISCO",         9, "e"),
            ("AZIONE",        6, "c"),
        ]:
            tk.Label(hdr, text=txt, font=("JetBrains Mono", 7, "bold"),
                     fg=P["dim"], bg=P["surface"],
                     width=w, anchor=anchor).pack(side="left")

        # Righe processi
        self._proc_rows: list[dict] = []
        rows_f = tk.Frame(f, bg=P["card"])
        rows_f.pack(fill="x", padx=8, pady=2)

        for i in range(11):
            bg = P["rowA"] if i % 2 == 0 else P["rowB"]
            row = tk.Frame(rows_f, bg=bg)
            row.pack(fill="x")

            labels = {}
            for key, w, anchor in [
                ("name",  14, "w"),
                ("power",  9, "e"),
                ("bar",    2, "c"),
                ("cpu",    5, "e"),
                ("disk",   9, "e"),
            ]:
                lbl = tk.Label(row, text="",
                               font=("JetBrains Mono", 8),
                               fg=P["text"], bg=bg,
                               width=w, anchor=anchor)
                lbl.pack(side="left")
                labels[key] = lbl

            # Bottone freeze/resume — tk.Button reale con sfondo visibile
            btn = tk.Button(row, text="", font=("Consolas", 7, "bold"),
                            fg=P["dim"], bg=bg,
                            activeforeground="#ffffff",
                            relief="flat", bd=1,
                            width=5, padx=2, pady=0,
                            cursor="hand2")
            btn.pack(side="left", padx=2, pady=1)
            labels["btn"] = btn

            self._proc_rows.append({
                "frame": row, "labels": labels,
                "bg": bg, "pid": None,
            })

        # Footer contatore + energia risparmiata
        footer = tk.Frame(f, bg=P["card"])
        footer.pack(fill="x", padx=8, pady=(2, 4))
        self._lbl_proc_count = tk.Label(footer, text="",
                                         font=("JetBrains Mono", 7),
                                         fg=P["dim"], bg=P["card"])
        self._lbl_proc_count.pack(side="left")
        self._lbl_saved_energy = tk.Label(footer, text="",
                                           font=("JetBrains Mono", 7),
                                           fg=P["tsc"], bg=P["card"])
        self._lbl_saved_energy.pack(side="right")

    # ── Col 3: LUCE / STATO ──────────────────────────────────────────────────
    def _build_col3(self, parent):
        f = self._card(parent, "LUCE  /  STATO", COL3,
                       side="left", fill="both", expand=True)

        # Brightness gauge
        bright_row = tk.Frame(f, bg=P["card"])
        bright_row.pack(fill="x", padx=8, pady=(4, 2))

        self._gauge3 = CircGauge(bright_row, size=72)
        self._gauge3.pack(side="left")

        bright_info = tk.Frame(bright_row, bg=P["card"])
        bright_info.pack(side="left", fill="both", expand=True, padx=8)
        self._lbl_bright_val = tk.Label(bright_info, text="-- %",
                                         font=("JetBrains Mono", 18, "bold"),
                                         fg=P["yellow"], bg=P["card"])
        self._lbl_bright_val.pack(anchor="w")
        self._lbl_bright_src = tk.Label(bright_info,
                                         text="WMI WmiMonitorBrightness",
                                         font=("Consolas", 7), fg=P["dim"],
                                         bg=P["card"])
        self._lbl_bright_src.pack(anchor="w")

        # Brightness sparkline
        self._spark_bright = Sparkline(f, w=COL3-16, h=26, col=P["yellow"])
        self._spark_bright.pack(padx=8, pady=(0, 2))
        self._lbl_spark_lbl = tk.Label(f, text="luminosità — 0 campioni",
                                        font=("Consolas", 6), fg=P["dim"],
                                        bg=P["card"])
        self._lbl_spark_lbl.pack(anchor="w", padx=8)

        tk.Frame(f, bg=P["border"], height=1).pack(fill="x", padx=8, pady=4)

        # Consumo energetico
        tk.Label(f, text="CONSUMO",
                 font=("JetBrains Mono", 7, "bold"), fg=P["dim"],
                 bg=P["card"]).pack(anchor="w", padx=8)

        power_f = tk.Frame(f, bg=P["card"])
        power_f.pack(fill="x", padx=8, pady=2)

        self._lbl_power_now = tk.Label(power_f, text="-- W",
                                        font=("JetBrains Mono", 14, "bold"),
                                        fg=P["yellow"], bg=P["card"])
        self._lbl_power_now.pack(anchor="w")

        self._lbl_power_method = tk.Label(power_f, text="",
                                           font=("Consolas", 6), fg=P["dim"],
                                           bg=P["card"])
        self._lbl_power_method.pack(anchor="w")

        power_stats_f = tk.Frame(f, bg=P["card"])
        power_stats_f.pack(fill="x", padx=8, pady=(0,4))
        self._power_stat_lbls: dict[str, tk.Label] = {}
        for lbl, col in [("Media", P["dim"]), ("Totale", P["dim"]),
                          ("Autonomia", P["green"])]:
            row = tk.Frame(power_stats_f, bg=P["card"])
            row.pack(fill="x")
            tk.Label(row, text=lbl, font=("Consolas", 7),
                     fg=P["dim"], bg=P["card"], anchor="w").pack(side="left")
            v = tk.Label(row, text="--", font=("JetBrains Mono", 8, "bold"),
                         fg=col, bg=P["card"], anchor="e")
            v.pack(side="right")
            self._power_stat_lbls[lbl] = v

        # Sparkline consumo
        self._spark_power = Sparkline(f, w=COL3-16, h=22, col=P["yellow"])
        self._spark_power.pack(padx=8, pady=(0,4))

        tk.Frame(f, bg=P["border"], height=1).pack(fill="x", padx=8, pady=4)

        # TSC section
        tk.Label(f, text="TSC CLOCK",
                 font=("JetBrains Mono", 7, "bold"), fg=P["dim"],
                 bg=P["card"]).pack(anchor="w", padx=8)

        tsc_f = tk.Frame(f, bg=P["card"])
        tsc_f.pack(fill="x", padx=8, pady=2)

        self._tsc_stat_lbls: dict[str, tk.Label] = {}
        for lbl, col in [("Freq", P["tsc"]), ("Drift", P["green"]),
                          ("PPM",  P["dim"]), ("Uptime", P["dim"])]:
            row = tk.Frame(tsc_f, bg=P["card"])
            row.pack(fill="x")
            tk.Label(row, text=lbl, font=("Consolas", 7),
                     fg=P["dim"], bg=P["card"], anchor="w", width=6).pack(side="left")
            v = tk.Label(row, text="--", font=("JetBrains Mono", 8, "bold"),
                         fg=col, bg=P["card"], anchor="e")
            v.pack(side="right")
            self._tsc_stat_lbls[lbl] = v
            tk.Frame(tsc_f, bg=P["border"], height=1).pack(fill="x")

        # Drift sparkline
        self._spark_drift = Sparkline(f, w=COL3-16, h=22, col=P["tsc"])
        self._spark_drift.pack(padx=8, pady=(4, 2))

        tk.Frame(f, bg=P["border"], height=1).pack(fill="x", padx=8, pady=4)

        # Anomalie
        tk.Label(f, text="ANOMALIE",
                 font=("JetBrains Mono", 7, "bold"), fg=P["dim"],
                 bg=P["card"]).pack(anchor="w", padx=8)

        self._lbl_anom = tk.Label(f, text="✓  Nessuna anomalia",
                                   font=("Consolas", 8, "bold"),
                                   fg=P["green"], bg=P["card"],
                                   anchor="w", pady=3)
        self._lbl_anom.pack(fill="x", padx=8)

    # ── Status bar ────────────────────────────────────────────────────────────
    def _build_statusbar(self):
        tk.Frame(self.root, bg=P["border"], height=1).pack(fill="x")
        sb = tk.Frame(self.root, bg=P["surface"], height=20)
        sb.pack(fill="x")
        sb.pack_propagate(False)
        self._lbl_statusbar = tk.Label(sb, text="",
                                        font=("JetBrains Mono", 7),
                                        fg=P["dim"], bg=P["surface"])
        self._lbl_statusbar.pack(side="left", padx=10)

    # ── Card helper ───────────────────────────────────────────────────────────
    def _card(self, parent, title: str, width: int,
              side="left", fill="y", expand=False) -> tk.Frame:
        outer = tk.Frame(parent, bg=P["border"], width=width)
        outer.pack(side=side, fill=fill, expand=expand,
                   padx=(0, GAP), pady=0)
        outer.pack_propagate(False)
        inner = tk.Frame(outer, bg=P["card"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, text=title,
                 font=("JetBrains Mono", 7, "bold"), fg=P["dim"],
                 bg=P["card"]).pack(anchor="w", padx=8, pady=(5, 0))
        tk.Frame(inner, bg=P["border"], height=1).pack(fill="x", padx=8, pady=(2, 0))
        return inner

    # ── Tick ─────────────────────────────────────────────────────────────────
    def _tick(self):
        try:
            self._refresh_topbar()
            self._refresh_col1()
            self._refresh_col2()
            self._refresh_col3()
            self._refresh_statusbar()
        except Exception:
            pass
        self.root.after(REFRESH_MS, self._tick)

    def _refresh_topbar(self):
        dt = datetime.datetime.fromtimestamp(self.engine.tsc_time())
        self._lbl_tsc.config(text="TSC  " + dt.strftime("%H:%M:%S"))
        d = self.engine.drift_us()
        dc = P["green"] if abs(d)<50 else P["yellow"] if abs(d)<500 else P["red"]
        self._lbl_drift.config(text="Δ%+.0fµs" % d, fg=dc)
        snap = self.batt.current
        if snap:
            pc = snap.charge_pct
            icon = "⚡" if snap.is_charging else ("🔌" if snap.is_on_ac else "🔋")
            pc_col = P["green"] if pc>50 else P["yellow"] if pc>20 else P["red"]
            self._lbl_pill.config(text="%s %.0f%%" % (icon, pc), fg=pc_col)

    def _refresh_col1(self):
        snap = self.batt.current
        if not snap: return
        pc = snap.charge_pct
        col = P["green"] if pc>50 else P["yellow"] if pc>20 else P["red"]
        self._gauge1.update(pc, "carica", col)
        self._seg1.update(pc, col)
        rt = snap.remaining_mwh / AVG_DRAW_MW
        ch = (snap.full_capacity_mwh - snap.remaining_mwh) / CHARGE_RATE_MW
        updates = {
            "Salute":    ("%.1f%%" % snap.health_pct,    P["orange"]),
            "Cicli":     (str(snap.cycle_count),          P["yellow"]),
            "Autonomia": ("%.1fh" % rt,                   P["green"] ),
            "Ricarica":  ("~%.1fh" % ch if snap.is_on_ac and not snap.is_charging else "--",
                          P["blue"]),
            "Tensione":  ("%.3fV" % (snap.voltage_mv/1000) if snap.voltage_mv else "--",
                          P["dim"]),
            "Cap. att.": ("%d mWh" % snap.full_capacity_mwh, P["dim"]),
            "Design":    ("%d mWh" % snap.design_capacity_mwh, P["dim"]),
        }
        for k, (v, c) in updates.items():
            if k in self._stat_lbls:
                self._stat_lbls[k].config(text=v, fg=c)
        ac = "AC — Non in carica" if snap.is_on_ac and not snap.is_charging else \
             "AC — IN CARICA" if snap.is_on_ac else \
             "Batteria — %.1fh residua" % rt
        self._lbl_ac.config(text=ac,
                            fg=P["green"] if snap.is_on_ac else col)

    @staticmethod
    def _power_color(mw: float) -> str:
        if mw < 300:   return P["dim"]
        if mw < 1000:  return P["green"]
        if mw < 3000:  return P["yellow"]
        if mw < 7000:  return P["orange"]
        return P["red"]

    def _toggle_freeze(self, pid: int, name: str):
        """Callback: congela o riprende il processo su click del bottone."""
        if self.freezer.is_frozen(pid):
            self.freezer.resume(pid)
        else:
            try:
                self.freezer.freeze(pid)
            except FreezeError as e:
                self._show_flash("⚠ %s" % e, P["red"])

    # Processi aggiuntivi da non congelare mai con Freeze All
    # (oltre a PROTECTED): runtime Python dell'app, shell di supporto, etc.
    _FREEZE_ALL_SKIP = frozenset({
        "python.exe", "pythonw.exe", "python3.exe",
        "SearchIndexer.exe", "SearchHost.exe",
        "sihost.exe", "ShellExperienceHost.exe",
        "StartMenuExperienceHost.exe", "TextInputHost.exe",
        "ApplicationFrameHost.exe", "ctfmon.exe",
        "spoolsv.exe", "WUDFHost.exe",
    })

    def _toggle_freeze_all(self):
        """Congela i processi utente non critici, o riprende tutti se già congelati."""
        frozen = self.freezer.frozen_list()

        if frozen:
            self.freezer.resume_all()
            self._btn_freeze_all.config(
                text="[  FREEZE ALL  ]", fg=P["blue"],
                bg="#0d1a2e", activebackground="#1a3a5c")
            return

        own_pid = self.freezer._own_pid
        with self.proc_power._lock:
            candidates = list(self.proc_power.samples.values())

        ok = 0
        for s in candidates:
            if s.pid == own_pid:
                continue
            if s.name in PROTECTED or s.name in self._FREEZE_ALL_SKIP:
                continue
            try:
                self.freezer.freeze(s.pid)
                ok += 1
            except FreezeError:
                pass

        self._btn_freeze_all.config(
            text="[ RESUME ALL ]", fg="#ffffff",
            bg="#1a4060", activebackground="#2a6090")
        self._show_flash("Congelati %d processi" % ok, P["tsc"])

    def _show_flash(self, msg: str, color: str = P["yellow"]):
        """Mostra un messaggio temporaneo nel banner frozen."""
        self._frozen_banner.pack(fill="x", padx=8, pady=(2, 0))
        self._lbl_frozen_banner.config(text=msg, fg=color)
        self.root.after(3000, self._update_frozen_banner)

    def _update_frozen_banner(self):
        frozen = self.freezer.frozen_list()
        if not frozen:
            self._frozen_banner.pack_forget()
            return
        self._frozen_banner.pack(fill="x", padx=8, pady=(2, 0))
        n = len(frozen)
        names = ", ".join(e.name for e in frozen[:3])
        if n > 3:
            names += " +%d altri" % (n - 3)
        saved = self.freezer.total_saved_mwh()
        self._lbl_frozen_banner.config(
            text="FREEZE: %d proc  [%s]  risparmio ~%.2f mWh" % (n, names, saved),
            fg=P["tsc"])

    def _refresh_col2(self):
        frozen_pids = {e.pid for e in self.freezer.frozen_list()}

        with self.proc_power._lock:
            all_samples = list(self.proc_power.samples.values())

        # Frozen sempre in cima (pinned), poi recenti
        all_samples.sort(key=lambda s: (0 if s.pid in frozen_pids else 1,
                                        -s.start_time))
        rows = all_samples[:11]

        for i, rw in enumerate(self._proc_rows):
            if i < len(rows):
                s      = rows[i]
                frozen = self.freezer.is_frozen(s.pid)
                safe   = self.freezer.can_freeze(s.name)

                # Colori: se congelato → azzurro/blu
                if frozen:
                    name_col  = P["tsc"]
                    power_col = P["tsc"]
                    cpu_col   = P["tsc"]
                    row_bg    = "#0d1a26"
                else:
                    name_col  = P["text"]
                    power_col = self._power_color(s.total_mw)
                    cpu_col   = (P["orange"] if s.cpu_pct > 15
                                 else P["yellow"] if s.cpu_pct > 5
                                 else P["green"])
                    row_bg    = P["rowA"] if i % 2 == 0 else P["rowB"]

                rw["frame"].config(bg=row_bg)
                for lbl in rw["labels"].values():
                    lbl.config(bg=row_bg)

                rw["labels"]["name"].config(
                    text=s.name[:14], fg=name_col)
                rw["labels"]["power"].config(
                    text=("❄ congelato" if frozen else s.power_str),
                    fg=power_col)
                rw["labels"]["bar"].config(
                    text="" if frozen else s.bar, fg=power_col)
                rw["labels"]["cpu"].config(
                    text=("0.0%" if frozen else "%.1f%%" % s.cpu_pct),
                    fg=cpu_col)
                rw["labels"]["disk"].config(
                    text="" if frozen else s.disk_str, fg=P["dim"])

                # Bottone freeze/resume
                pid_snap = s.pid
                if safe:
                    if frozen:
                        rw["labels"]["btn"].config(
                            text="RIPRENDI",
                            fg="#ffffff", bg="#1a4060",
                            activebackground="#2a6090",
                            relief="groove", bd=2, cursor="hand2",
                            command=lambda p=pid_snap, n=s.name:
                                self._toggle_freeze(p, n))
                    else:
                        rw["labels"]["btn"].config(
                            text="FREEZE",
                            fg=P["blue"], bg="#0d1520",
                            activebackground="#1a2a3a",
                            relief="groove", bd=1, cursor="hand2",
                            command=lambda p=pid_snap, n=s.name:
                                self._toggle_freeze(p, n))
                else:
                    rw["labels"]["btn"].config(
                        text="LOCK", fg=P["border"], bg=row_bg,
                        activebackground=row_bg,
                        relief="flat", bd=0, cursor="",
                        command="")

                rw["pid"] = s.pid
            else:
                rw["pid"] = None
                rw["frame"].config(bg=rw["bg"])
                for key, lbl in rw["labels"].items():
                    if key == "btn":
                        lbl.config(text="", bg=rw["bg"], relief="flat",
                                   bd=0, cursor="", command="")
                    else:
                        lbl.config(text="", bg=rw["bg"], cursor="")

        # Footer
        n_alive  = self.proc_power.count()
        tot_mw   = self.proc_power.total_estimated_mw()
        n_frozen = self.freezer.frozen_count()
        saved    = self.freezer.total_saved_mwh()

        count_txt = "%d processi  ·  ~%.1fW" % (n_alive, tot_mw/1000)
        if n_frozen:
            count_txt += "  ·  ❄ %d" % n_frozen
        self._lbl_proc_count.config(text=count_txt)

        if saved > 0.01:
            self._lbl_saved_energy.config(
                text="❄ risparmio: %.2f mWh" % saved)
        else:
            self._lbl_saved_energy.config(text="")

        # Aggiorna banner
        self._update_frozen_banner()

    def _refresh_col3(self):
        # Consumo energetico
        ps = self.power.current
        if ps:
            mw = ps.power_mw
            self._power_history.append(mw)
            if len(self._power_history) > 60:
                self._power_history = self._power_history[-60:]
            pwr_col = P["red"] if mw > 15000 else P["orange"] if mw > 8000 \
                      else P["yellow"] if mw > 3000 else P["green"]
            self._lbl_power_now.config(
                text="%.1f W" % (mw/1000) if mw > 0 else ("Su AC" if ps.on_ac else "N/D"),
                fg=pwr_col)
            self._lbl_power_method.config(text=ps.method_label)
            avg = self.power.avg_power_mw(10)
            total = self.power.energy_used_mwh()
            snap = self.batt.current
            rt = self.power.estimated_runtime_h(snap.remaining_mwh) if snap else 0
            self._power_stat_lbls["Media"].config(
                text="%.1fW" % (avg/1000) if avg > 0 else "N/D")
            self._power_stat_lbls["Totale"].config(
                text="%.2f mWh" % total if total > 0 else "N/D")
            self._power_stat_lbls["Autonomia"].config(
                text="%.1fh" % rt if rt > 0 else "N/D")
            self._spark_power.push(mw / 1000)  # in W

        # Aggiorna testo bottone freeze-all in base allo stato
        if self.freezer.frozen_count() > 0:
            self._btn_freeze_all.config(
                text="[ RESUME ALL ]", fg="#ffffff",
                bg="#1a4060", activebackground="#2a6090")
        else:
            self._btn_freeze_all.config(
                text="[  FREEZE ALL  ]", fg=P["blue"],
                bg="#0d1a2e", activebackground="#1a3a5c")

        # Brightness (valore cached dal thread background)
        b = get_brightness_cached()
        if b is not None:
            self._bright_history.append(float(b))
            if len(self._bright_history) > 120:
                self._bright_history = self._bright_history[-120:]
            self._gauge3.update(b, "luce", P["yellow"])
            self._lbl_bright_val.config(text="%d %%" % b)
            self._spark_bright.push(float(b))
            self._lbl_spark_lbl.config(
                text="luminosità — %d campioni" % len(self._bright_history))

        # TSC stats
        elapsed = self.engine.elapsed_seconds()
        drift   = self.engine.drift_us()
        self._drift_history.append(drift)
        if len(self._drift_history) > 60:
            self._drift_history = self._drift_history[-60:]

        dc = P["green"] if abs(drift)<50 else P["yellow"] if abs(drift)<500 else P["red"]
        self._tsc_stat_lbls["Freq"].config(
            text="%.6f GHz" % (self.engine.freq/1e9))
        self._tsc_stat_lbls["Drift"].config(
            text="%+.0f µs" % drift, fg=dc)
        self._tsc_stat_lbls["PPM"].config(
            text="%+.3f ppm" % self.engine.drift_ppm())
        h, r = divmod(int(elapsed), 3600)
        m, s = divmod(r, 60)
        uptime = ("%dh %02dm" % (h, m)) if h else ("%dm %02ds" % (m, s))
        self._tsc_stat_lbls["Uptime"].config(text=uptime)
        self._spark_drift.push(drift)

        # Anomalie
        anom = self.batt.wakeup_anomalies
        if anom:
            worst = max(anom, key=lambda a: ["info","warning","critical"].index(a.severity))
            icon  = {"info":"ℹ","warning":"⚠","critical":"✖"}.get(worst.severity,"?")
            col   = {"info":P["blue"],"warning":P["yellow"],"critical":P["red"]}.get(worst.severity)
            self._lbl_anom.config(
                text="%s  %s" % (icon, worst.message[:34]), fg=col)
        else:
            self._lbl_anom.config(text="✓  Nessuna anomalia", fg=P["green"])

    def _refresh_statusbar(self):
        snap = self.batt.current
        b_str = ("L19C4PDC  %.0f%%  %s  |  cicli %d  |  salute %.1f%%  |  " % (
            snap.charge_pct,
            "AC" if snap.is_on_ac else "batt",
            snap.cycle_count, snap.health_pct,
        )) if snap else ""
        self._lbl_statusbar.config(
            text="%sTSC %.6f GHz  |  drift %+.0fµs  |  %s" % (
                b_str,
                self.engine.freq / 1e9,
                self.engine.drift_us(),
                datetime.datetime.now().strftime("%d/%m/%Y  %H:%M"),
            ))

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        self.power.stop()
        self.proc_power.stop()
        self.freezer.stop()
        if self.batt.current:
            save_state(self.batt.current, self.engine.epoch_tsc)
            append_battery_log("session_end", self.batt.current.to_dict())
        self.batt.stop()
        self.root.destroy()


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    app = BatteryManagerApp()
    app.run()

if __name__ == "__main__":
    main()
