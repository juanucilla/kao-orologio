"""
tsc_clock_app.py — Orologio TSC: system tray + overlay bottom-right + monitor batteria
========================================================================================
Avvio: python tsc_clock_app.py
Setup autostart: python tsc_clock_app.py --install
Rimozione:       python tsc_clock_app.py --uninstall
Debug console:   python tsc_clock_app.py --console
"""

import sys, os, time, datetime, threading, json, argparse, subprocess
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageFont

import pystray

from tsc_engine    import TSCEngine
from battery_monitor import (BatteryMonitor, BatterySnapshot, load_state,
                              save_state, DATA_DIR, append_battery_log,
                              load_battery_log, FULL_CAPACITY_MWH, AVG_DRAW_MW,
                              CYCLE_COUNT_BASELINE)

# ─────────────────────────────────────────────────────────────────────────────
#  Costanti UI
# ─────────────────────────────────────────────────────────────────────────────
OVERLAY_W     = 280    # larghezza overlay px
OVERLAY_H     = 56     # altezza overlay px
TASKBAR_H     = 40     # altezza taskbar stimata px
OVERLAY_ALPHA = 0.88   # trasparenza (0=invisibile, 1=opaco)
UPDATE_MS     = 500    # refresh overlay ms
ICON_SIZE     = 64     # dimensione icona tray px

# Colori
BG_COLOR       = "#0d1117"
TSC_COLOR      = "#58a6ff"   # azzurro TSC
SYS_COLOR      = "#8b949e"   # grigio sistema
DRIFT_OK       = "#3fb950"   # verde < 50 µs
DRIFT_WARN     = "#d29922"   # giallo < 500 µs
DRIFT_BAD      = "#f85149"   # rosso > 500 µs
BATT_OK        = "#3fb950"
BATT_WARN      = "#d29922"
BATT_CRIT      = "#f85149"


# ─────────────────────────────────────────────────────────────────────────────
#  Icona tray dinamica
# ─────────────────────────────────────────────────────────────────────────────
def make_tray_icon(tsc_engine: TSCEngine, batt: BatteryMonitor) -> Image.Image:
    """Genera immagine 64×64 con ora TSC e barra batteria."""
    img  = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (13, 17, 23, 255))
    draw = ImageDraw.Draw(img)

    # Ora (HH:MM)
    dt  = datetime.datetime.fromtimestamp(tsc_engine.tsc_time())
    txt = dt.strftime("%H:%M")
    try:
        fnt = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 20)
    except Exception:
        fnt = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), txt, font=fnt)
    tw   = bbox[2] - bbox[0]
    draw.text(((ICON_SIZE - tw) // 2, 6), txt, font=fnt, fill=(88, 166, 255))

    # Barra batteria
    snap = batt.current if batt else None
    if snap:
        pct  = snap.charge_pct / 100
        bar_w = ICON_SIZE - 12
        bar_h = 8
        bx    = 6
        by    = ICON_SIZE - 14
        draw.rectangle([bx, by, bx + bar_w, by + bar_h], outline=(100, 100, 100))
        fill_w = int(bar_w * pct)
        if pct > 0.5:
            fill = (63, 185, 80)
        elif pct > 0.2:
            fill = (210, 153, 34)
        else:
            fill = (248, 81, 73)
        if fill_w > 0:
            draw.rectangle([bx + 1, by + 1, bx + fill_w, by + bar_h - 1], fill=fill)

        # "AC" se in carica
        if snap.is_on_ac:
            try:
                sf = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 10)
            except Exception:
                sf = ImageFont.load_default()
            draw.text((bx + bar_w - 14, by - 1), "AC", font=sf, fill=(255, 200, 50))

    return img


# ─────────────────────────────────────────────────────────────────────────────
#  Overlay tkinter bottom-right
# ─────────────────────────────────────────────────────────────────────────────
class OverlayWindow:
    def __init__(self, tsc_engine: TSCEngine, batt: BatteryMonitor):
        self.engine = tsc_engine
        self.batt   = batt
        self.root   = tk.Tk()
        self._setup_window()
        self._setup_widgets()
        self._position_window()
        self._update()

    def _setup_window(self):
        r = self.root
        r.overrideredirect(True)            # nessun bordo / titolo
        r.attributes("-topmost", True)
        r.attributes("-alpha", OVERLAY_ALPHA)
        r.configure(bg=BG_COLOR)
        r.resizable(False, False)

        # Drag
        r.bind("<ButtonPress-1>",   self._drag_start)
        r.bind("<B1-Motion>",       self._drag_motion)

    def _drag_start(self, e):
        self._dx = e.x
        self._dy = e.y

    def _drag_motion(self, e):
        x = self.root.winfo_x() + (e.x - self._dx)
        y = self.root.winfo_y() + (e.y - self._dy)
        self.root.geometry("+%d+%d" % (x, y))

    def _setup_widgets(self):
        r = self.root
        r.geometry("%dx%d" % (OVERLAY_W, OVERLAY_H))

        try:
            mono_big  = tkfont.Font(family="Consolas", size=13, weight="bold")
            mono_small = tkfont.Font(family="Consolas", size=8)
        except Exception:
            mono_big  = tkfont.Font(size=13, weight="bold")
            mono_small = tkfont.Font(size=8)

        # Riga 1: TSC time
        self.lbl_tsc = tk.Label(r, text="TSC --:--:--",
                                 fg=TSC_COLOR, bg=BG_COLOR, font=mono_big,
                                 anchor="w", padx=6)
        self.lbl_tsc.pack(fill="x", pady=(4, 0))

        # Riga 2: drift + batteria
        frame2 = tk.Frame(r, bg=BG_COLOR)
        frame2.pack(fill="x", padx=6)

        self.lbl_drift = tk.Label(frame2, text="drift --",
                                   fg=DRIFT_OK, bg=BG_COLOR, font=mono_small)
        self.lbl_drift.pack(side="left")

        self.lbl_batt = tk.Label(frame2, text="",
                                  fg=BATT_OK, bg=BG_COLOR, font=mono_small)
        self.lbl_batt.pack(side="right")

    def _position_window(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = sw - OVERLAY_W - 4
        y  = sh - TASKBAR_H - OVERLAY_H - 2
        self.root.geometry("+%d+%d" % (x, y))

    def _update(self):
        # TSC time
        dt      = datetime.datetime.fromtimestamp(self.engine.tsc_time())
        tsc_str = dt.strftime("%H:%M:%S.%f")[:-3]   # ms precision
        self.lbl_tsc.config(text="TSC " + tsc_str)

        # Drift
        drift = self.engine.drift_us()
        if abs(drift) < 50:
            dc = DRIFT_OK
        elif abs(drift) < 500:
            dc = DRIFT_WARN
        else:
            dc = DRIFT_BAD
        self.lbl_drift.config(text="Δ%+.0fµs" % drift, fg=dc)

        # Batteria
        snap = self.batt.current if self.batt else None
        if snap:
            pct = snap.charge_pct
            ch  = "⚡" if snap.is_charging else ("🔌" if snap.is_on_ac else "🔋")
            anom_marker = " ⚠" if self.batt.wakeup_anomalies else ""
            bc = BATT_OK if pct > 50 else (BATT_WARN if pct > 20 else BATT_CRIT)
            self.lbl_batt.config(text="%s%.0f%%%s" % (ch, pct, anom_marker), fg=bc)

        self.root.after(UPDATE_MS, self._update)

    def run(self):
        self.root.mainloop()

    def destroy(self):
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Menu dettagli batteria (finestra popup)
# ─────────────────────────────────────────────────────────────────────────────
class BatteryDetailWindow:
    _instance = None

    @classmethod
    def show(cls, engine: TSCEngine, batt: BatteryMonitor):
        if cls._instance and cls._instance.alive:
            cls._instance.win.lift()
            return
        inst = cls(engine, batt)
        cls._instance = inst
        t = threading.Thread(target=inst._run, daemon=True)
        t.start()

    def __init__(self, engine: TSCEngine, batt: BatteryMonitor):
        self.engine = engine
        self.batt   = batt
        self.alive  = True
        self.win    = None

    def _run(self):
        win = tk.Toplevel() if False else tk.Tk()
        self.win = win
        win.title("TSC Clock — Batteria e Anomalie")
        win.configure(bg=BG_COLOR)
        win.geometry("640x500")
        win.resizable(True, True)

        try:
            mf = tkfont.Font(family="Consolas", size=10)
        except Exception:
            mf = tkfont.Font(size=10)

        text = tk.Text(win, bg="#161b22", fg="#c9d1d9", font=mf,
                       relief="flat", padx=10, pady=10)
        text.pack(fill="both", expand=True)

        sb = tk.Scrollbar(win, command=text.yview)
        sb.pack(side="right", fill="y")
        text.config(yscrollcommand=sb.set)

        # Configura tag colori
        text.tag_configure("title",    foreground=TSC_COLOR,  font=(mf.cget("family"), 12, "bold"))
        text.tag_configure("warn",     foreground=DRIFT_WARN)
        text.tag_configure("crit",     foreground=DRIFT_BAD)
        text.tag_configure("ok",       foreground=DRIFT_OK)
        text.tag_configure("dim",      foreground=SYS_COLOR)

        btn = tk.Button(win, text="Aggiorna", bg="#21262d", fg="#c9d1d9",
                        relief="flat", command=lambda: self._refresh(text))
        btn.pack(fill="x", padx=4, pady=4)

        self._refresh(text)

        def on_close():
            self.alive = False
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)
        win.mainloop()
        self.alive = False

    def _refresh(self, text: tk.Text):
        text.config(state="normal")
        text.delete("1.0", "end")

        def w(s, tag=None):
            if tag:
                text.insert("end", s + "\n", tag)
            else:
                text.insert("end", s + "\n")

        w("═══ TSC CLOCK — STATO BATTERIA ═══", "title")
        w("")

        # Stato corrente
        snap = self.batt.current if self.batt else None
        if snap:
            w("  Batteria: %s  (SN: %s)" % (snap.full_capacity_mwh and "L19C4PDC" or "?", "3002"))
            w("  Carica corrente  : %.1f%%  (%d / %d mWh)" % (
                snap.charge_pct, snap.remaining_mwh, snap.full_capacity_mwh))
            health_tag = "ok" if snap.health_pct > 80 else ("warn" if snap.health_pct > 60 else "crit")
            w("  Salute           : %.1f%%  (progettata: %d mWh, attuale max: %d mWh)" % (
                snap.health_pct, snap.design_capacity_mwh, snap.full_capacity_mwh), health_tag)
            cycle_tag = "ok" if snap.cycle_count < 600 else ("warn" if snap.cycle_count < 900 else "crit")
            w("  Cicli ricarica   : %d  (Li-ion: ottimale <500, critico >900)" % snap.cycle_count, cycle_tag)
            w("  Tensione         : %.3f V" % (snap.voltage_mv / 1000) if snap.voltage_mv else "  Tensione         : n/d")

            w("")
            w("  Alimentazione    : %s%s" % (
                "AC (rete)" if snap.is_on_ac else "Batteria",
                " — in carica" if snap.is_charging else ""))

            # Autonomia
            runtime_h = snap.remaining_mwh / AVG_DRAW_MW
            design_runtime_h = snap.design_capacity_mwh / AVG_DRAW_MW
            current_full_runtime_h = snap.full_capacity_mwh / AVG_DRAW_MW
            w("")
            w("  AUTONOMIA STIMATA:", "title")
            w("    Design (nuovo)   : %.1fh a piena carica" % design_runtime_h)
            w("    Attuale (corrente): %.1fh a piena carica  (salute %.1f%%)" % (
                current_full_runtime_h, snap.health_pct))
            w("    Residua ora      : %.1fh  (%.0f minuti)" % (runtime_h, runtime_h * 60))

            if snap.is_charging and snap.is_on_ac:
                from battery_monitor import CHARGE_RATE_MW
                mwh_to_full = snap.full_capacity_mwh - snap.remaining_mwh
                ch_h = mwh_to_full / CHARGE_RATE_MW
                w("    Tempo a 100%%     : ~%.1fh  (~%.0f minuti)" % (ch_h, ch_h * 60))

        # TSC drift
        w("")
        w("  DERIVA TSC:", "title")
        drift = self.engine.drift_us()
        drift_tag = "ok" if abs(drift) < 50 else ("warn" if abs(drift) < 500 else "crit")
        w("    Corrente      : %+.3f µs" % drift, drift_tag)
        elapsed = self.engine.elapsed_seconds()
        if elapsed > 5:
            ppm = self.engine.drift_ppm()
            w("    Ppm           : %+.4f ppm  (su %.0fs)" % (ppm, elapsed))
            proj = self.engine.drift_projections()
            w("    Proiezioni:")
            for k, ms in proj.items():
                tag = "ok" if abs(ms) < 100 else ("warn" if abs(ms) < 1000 else "crit")
                w("      %-5s : %+.2f ms" % (k, ms), tag)

        # Anomalie wake-up
        w("")
        w("  ANOMALIE RILEVATE AL RIAVVIO:", "title")
        anom = self.batt.wakeup_anomalies if self.batt else []
        if not anom:
            w("    Nessuna anomalia rilevata.", "ok")
        else:
            for a in anom:
                tag = {"info": "dim", "warning": "warn", "critical": "crit"}.get(a.severity, "")
                w("    [%s] %s" % (a.code, a.message), tag)
                if a.detail:
                    w("         %s" % a.detail, "dim")

        # Log batteria recente
        w("")
        w("  LOG BATTERIA RECENTE (ultimi 10 eventi):", "title")
        log = load_battery_log(50)
        for entry in log[-10:]:
            evt  = entry.get("event", "?")
            t    = entry.get("time", "?")
            pct  = entry.get("charge_pct", "?")
            cyc  = entry.get("cycle_count", "?")
            w("    %s  %-25s  carica=%-6s cicli=%s" % (t, evt, pct, cyc), "dim")

        text.config(state="disabled")


# ─────────────────────────────────────────────────────────────────────────────
#  Finestra console debug (modalità --console)
# ─────────────────────────────────────────────────────────────────────────────
class ConsoleWindow:
    def __init__(self, engine: TSCEngine, batt: BatteryMonitor):
        self.engine = engine
        self.batt   = batt

    def run(self):
        import statistics as stats_mod
        drift_history: list[float] = []
        minute_stats:  list[dict]  = []
        last_min_t = time.time()

        print("\n  TSC CLOCK CONSOLE — Ctrl+C per uscire\n")
        try:
            while True:
                os.system("cls")
                tsc_now   = self.engine.now_tsc()
                ticks     = self.engine.elapsed_ticks()
                elapsed_s = self.engine.elapsed_seconds()
                tsc_dt    = datetime.datetime.fromtimestamp(self.engine.tsc_time())
                sys_dt    = datetime.datetime.now()
                drift     = self.engine.drift_us()
                drift_history.append(drift)

                # Stat per minuto
                if time.time() - last_min_t >= 60:
                    last_min_t = time.time()
                    minute_stats.append({
                        "t_min": round(elapsed_s / 60, 2),
                        "drift_us": round(drift, 2),
                        "ppm": round(self.engine.drift_ppm(), 4),
                    })

                print("=" * 72)
                print("  TSC CLOCK  —  i5-1135G7  —  Invariant TSC 2.4192 GHz")
                print("=" * 72)
                print("  TSC corrente      : %d" % tsc_now)
                print("  Tick trascorsi    : %d" % ticks)
                print("  Secondi trascorsi : %.9f s" % elapsed_s)
                print()
                print("  Ora TSC           : %s" % tsc_dt.strftime("%Y-%m-%d  %H:%M:%S.%f"))
                print("  Ora Sistema       : %s" % sys_dt.strftime("%Y-%m-%d  %H:%M:%S.%f"))
                print()
                print("  Frequenza TSC     : %.0f Hz  (%.6f GHz)" % (
                    self.engine.freq, self.engine.freq / 1e9))
                print("  Freq CPUID        : %.0f Hz  (%.6f GHz)" % (
                    self.engine.freq_cpuid, self.engine.freq_cpuid / 1e9))
                print("  Delta freq        : %+.3f ppm" % self.engine.freq_delta_ppm)
                print()
                print("  Deriva TSC-SYS    : %+.3f µs  (%+.4f ppm)" % (
                    drift, self.engine.drift_ppm()))

                # Proiezioni
                proj = self.engine.drift_projections()
                if proj:
                    print()
                    print("  Proiezioni deriva:")
                    for k, ms in proj.items():
                        print("    dopo %-5s : %+.3f ms" % (k, ms))

                # Statistiche per minuto
                if minute_stats:
                    print()
                    print("  Statistiche per minuto (%d campioni):" % len(minute_stats))
                    for st in minute_stats[-5:]:
                        print("    t=%6.1f min  deriva=%+8.2f µs  (%+.4f ppm)" % (
                            st["t_min"], st["drift_us"], st["ppm"]))

                # Grafico ASCII deriva
                if len(drift_history) >= 2:
                    print()
                    print("  Grafico deriva live (ultimo campione = destra):")
                    h  = drift_history[-60:]
                    mn = min(h); mx = max(h)
                    if mx == mn: mx = mn + 1
                    bar_rows = 6
                    grid = [[" "] * len(h) for _ in range(bar_rows)]
                    for ci, v in enumerate(h):
                        ri = int((v - mn) / (mx - mn) * (bar_rows - 1))
                        ri = bar_rows - 1 - max(0, min(bar_rows - 1, ri))
                        grid[ri][ci] = "•"
                    zero_r = int((0 - mn) / (mx - mn) * (bar_rows - 1))
                    zero_r = bar_rows - 1 - max(0, min(bar_rows - 1, zero_r))
                    for ci in range(len(h)):
                        if grid[zero_r][ci] == " ":
                            grid[zero_r][ci] = "─"
                    for ri, row in enumerate(grid):
                        if ri == 0:         lbl = "%+7.1f µs" % mx
                        elif ri == bar_rows-1: lbl = "%+7.1f µs" % mn
                        else:                  lbl = "          "
                        print("  %s |%s|" % (lbl, "".join(row)))

                # Batteria
                print()
                print("  BATTERIA:")
                for line in self.batt.get_status_lines():
                    print(line)

                print()
                print("  [Ctrl+C per uscire]  [aggiornamento ogni 0.5s]")
                print("=" * 72)

                time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n\n  Uscita.")
            # Report finale
            if drift_history:
                print("  Drift finale: media %.3f µs, std %.3f µs, min %+.3f µs, max %+.3f µs" % (
                    sum(drift_history)/len(drift_history),
                    stats_mod.stdev(drift_history) if len(drift_history) > 1 else 0,
                    min(drift_history), max(drift_history)))


# ─────────────────────────────────────────────────────────────────────────────
#  Autostart setup
# ─────────────────────────────────────────────────────────────────────────────
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                            r"Microsoft\Windows\Start Menu\Programs\Startup")
SHORTCUT_PATH = os.path.join(STARTUP_DIR, "TSCClock.lnk")
THIS_SCRIPT   = os.path.abspath(__file__)


def install_autostart():
    """Crea collegamento nella cartella Startup."""
    import winreg
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    # Crea .lnk via PowerShell
    ps_cmd = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; "
        "$s.Arguments = '\"%s\"'; "
        "$s.WorkingDirectory = '%s'; "
        "$s.WindowStyle = 7; "   # 7 = minimized
        "$s.Description = 'TSC Clock'; "
        "$s.Save()"
    ) % (SHORTCUT_PATH, pythonw, THIS_SCRIPT,
         os.path.dirname(THIS_SCRIPT))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from win_subprocess import run_hidden
    result = run_hidden(["powershell", "-Command", ps_cmd],
                        capture_output=True, text=True)
    if result.returncode == 0:
        print("  Autostart installato: %s" % SHORTCUT_PATH)
    else:
        print("  Errore autostart: %s" % result.stderr)


def uninstall_autostart():
    if os.path.exists(SHORTCUT_PATH):
        os.remove(SHORTCUT_PATH)
        print("  Autostart rimosso.")
    else:
        print("  Nessun autostart trovato.")


# ─────────────────────────────────────────────────────────────────────────────
#  App principale con system tray
# ─────────────────────────────────────────────────────────────────────────────
class TSCClockApp:
    def __init__(self, console_mode: bool = False):
        self.console_mode = console_mode

        # Carica stato precedente
        self.prev_state = load_state()

        # Avvia motore TSC
        print("Calibrazione TSC...", flush=True)
        self.engine = TSCEngine(calibration_s=3.0)
        print("  freq: %.3f GHz  (delta %+.3f ppm)" % (
            self.engine.freq / 1e9, self.engine.freq_delta_ppm), flush=True)

        # Avvia monitor batteria
        self.batt = BatteryMonitor(poll_interval_s=30.0)
        self.batt.start(self.prev_state, self.engine.epoch_tsc)

        # Aspetta prima snapshot batteria
        for _ in range(20):
            if self.batt.current:
                break
            time.sleep(0.2)

        self.overlay:      OverlayWindow | None = None
        self.tray:         pystray.Icon  | None = None
        self._tray_thread: threading.Thread | None = None

    # ── Tray icon ─────────────────────────────────────────────────────────────
    def _build_tray_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Mostra overlay",   self._toggle_overlay, default=True),
            pystray.MenuItem("Dettagli batteria", lambda: self._open_battery_detail()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ricalibra TSC",    self._recalibrate),
            pystray.MenuItem("Risincronizza epoch", self._resync),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Console debug",    self._open_console),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Esci",             self._quit),
        )

    def _start_tray(self):
        icon_img = make_tray_icon(self.engine, self.batt)
        self.tray = pystray.Icon(
            "TSCClock",
            icon_img,
            "TSC Clock",
            menu=self._build_tray_menu()
        )

        def update_icon():
            while self.tray and self.tray.visible:
                try:
                    img = make_tray_icon(self.engine, self.batt)
                    self.tray.icon = img
                    # Aggiorna tooltip
                    snap = self.batt.current
                    if snap:
                        dt  = datetime.datetime.fromtimestamp(self.engine.tsc_time())
                        tip = "TSC %s | %s" % (
                            dt.strftime("%H:%M:%S"),
                            self.batt.get_tray_tooltip())
                        self.tray.title = tip
                except Exception:
                    pass
                time.sleep(1)

        t = threading.Thread(target=update_icon, daemon=True)
        t.start()

        self.tray.run()

    def _toggle_overlay(self):
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None
        else:
            self._start_overlay_thread()

    def _start_overlay_thread(self):
        def run():
            self.overlay = OverlayWindow(self.engine, self.batt)
            self.overlay.run()
            self.overlay = None
        t = threading.Thread(target=run, daemon=True)
        t.start()

    def _open_battery_detail(self):
        def run():
            BatteryDetailWindow.show(self.engine, self.batt)
        t = threading.Thread(target=run, daemon=True)
        t.start()

    def _open_console(self):
        def run():
            cw = ConsoleWindow(self.engine, self.batt)
            cw.run()
        t = threading.Thread(target=run, daemon=True)
        t.start()

    def _recalibrate(self):
        def run():
            self.engine.recalibrate(duration_s=2.0)
        threading.Thread(target=run, daemon=True).start()

    def _resync(self):
        self.engine.resync_epoch()

    def _quit(self):
        # Salva stato finale
        if self.batt.current:
            save_state(self.batt.current, self.engine.epoch_tsc)
            append_battery_log("session_end", self.batt.current.to_dict())
        self.batt.stop()
        if self.overlay:
            self.overlay.destroy()
        if self.tray:
            self.tray.stop()

    # ── Entry point ───────────────────────────────────────────────────────────
    def run(self):
        if self.console_mode:
            cw = ConsoleWindow(self.engine, self.batt)
            cw.run()
            if self.batt.current:
                save_state(self.batt.current, self.engine.epoch_tsc)
            self.batt.stop()
            return

        # Avvia overlay
        self._start_overlay_thread()

        # Avvia tray (blocca qui)
        self._start_tray()


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="TSC Clock — orologio basato su Time Stamp Counter")
    parser.add_argument("--install",   action="store_true", help="Installa autostart all'avvio di Windows")
    parser.add_argument("--uninstall", action="store_true", help="Rimuovi autostart")
    parser.add_argument("--console",   action="store_true", help="Modalità console (nessuna GUI)")
    args = parser.parse_args()

    if args.install:
        install_autostart()
        return
    if args.uninstall:
        uninstall_autostart()
        return

    app = TSCClockApp(console_mode=args.console)
    app.run()


if __name__ == "__main__":
    main()
