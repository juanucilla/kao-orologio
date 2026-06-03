"""
setup_installer.py — Installer grafico per TSC Clock + Battery Manager
=======================================================================
Avvio: python setup_installer.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import os, sys, subprocess, threading, json, shutil, time

APP_DIR   = os.path.dirname(os.path.abspath(__file__))
APPDATA   = os.environ.get("APPDATA", "")
STARTUP   = os.path.join(APPDATA, r"Microsoft\Windows\Start Menu\Programs\Startup")
DATA_DIR  = os.path.join(APPDATA, "TSCClock")

PYTHONW   = sys.executable.replace("python.exe", "pythonw.exe")
if not os.path.exists(PYTHONW):
    PYTHONW = sys.executable

C = {
    "bg":    "#0d1117",
    "card":  "#161b22",
    "bord":  "#30363d",
    "text":  "#c9d1d9",
    "dim":   "#8b949e",
    "blue":  "#58a6ff",
    "green": "#3fb950",
    "red":   "#f85149",
    "yellow":"#d29922",
}


def make_shortcut(lnk_path: str, target: str, args: str,
                  desc: str, window_style: int = 7):
    ps = (
        "$ws = New-Object -COM WScript.Shell;"
        "$s = $ws.CreateShortcut('%s');"
        "$s.TargetPath = '%s';"
        "$s.Arguments = '%s';"
        "$s.WorkingDirectory = '%s';"
        "$s.WindowStyle = %d;"
        "$s.Description = '%s';"
        "$s.Save()"
    ) % (lnk_path, target, args, APP_DIR, window_style, desc)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from win_subprocess import run_hidden
    run_hidden(["powershell", "-Command", ps], capture_output=True)


def remove_shortcut(lnk_path: str):
    if os.path.exists(lnk_path):
        os.remove(lnk_path)


def check_installed(lnk: str) -> bool:
    return os.path.exists(lnk)


# ─────────────────────────────────────────────────────────────────────────────
class InstallerApp:
    TSC_LNK  = os.path.join(STARTUP, "TSCClock.lnk")
    BATT_LNK = os.path.join(STARTUP, "BatteryManager.lnk")

    DESK     = os.path.join(os.path.expanduser("~"), "Desktop")
    TSC_DESK  = os.path.join(DESK, "TSC Clock.lnk")
    BATT_DESK = os.path.join(DESK, "Battery Manager.lnk")

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Setup — TSC Clock & Battery Manager")
        self.root.geometry("620x580")
        self.root.resizable(False, False)
        self.root.configure(bg=C["bg"])
        self._build()

    def _lbl(self, parent, text, fg=None, font_size=9, bold=False, **kw):
        f = ("Consolas", font_size, "bold" if bold else "normal")
        return tk.Label(parent, text=text, font=f,
                        fg=fg or C["text"], bg=C["bg"], **kw)

    def _btn(self, parent, text, cmd, color=C["blue"], w=18):
        return tk.Button(parent, text=text, command=cmd, width=w,
                         font=("Consolas", 9), bg="#21262d", fg=color,
                         activebackground="#30363d", relief="flat",
                         cursor="hand2", pady=5)

    def _sep(self, parent):
        tk.Frame(parent, bg=C["bord"], height=1).pack(fill="x", padx=16, pady=8)

    def _build(self):
        # Header
        hdr = tk.Frame(self.root, bg=C["card"], height=70)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚡  TSC Clock  &  Battery Manager",
                 font=("Consolas", 14, "bold"), fg=C["blue"],
                 bg=C["card"]).pack(pady=(14, 0))
        tk.Label(hdr, text="Setup e installazione — Intel i5-1135G7",
                 font=("Consolas", 9), fg=C["dim"],
                 bg=C["card"]).pack()

        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=12)

        # ── Sezione 1: TSC Clock ──────────────────────────────────────────────
        self._section(body, "1. TSC CLOCK  —  Orologio overlay bottom-right",
            "Orologio basato sul Time Stamp Counter (RDTSC, 2.4192 GHz).\n"
            "Mostra ora TSC, deriva µs e stato batteria in basso a destra\n"
            "accanto all'orologio di sistema Windows.",
            self.TSC_LNK,
            lambda: self._install("tsc", self.TSC_LNK),
            lambda: self._uninstall("tsc", self.TSC_LNK),
            lambda: self._run_now("tsc_clock_app.py"),
            "tsc_status")

        self._sep(body)

        # ── Sezione 2: Battery Manager ────────────────────────────────────────
        self._section(body, "2. BATTERY MANAGER  —  Gestionale batteria",
            "Dashboard batteria con coda operazioni, luminosità schermo,\n"
            "rilevamento anomalie (cicli, drain in sleep, AC) e storico.\n"
            "Batteria: L19C4PDC  |  Salute 81%%  |  Cicli 651",
            self.BATT_LNK,
            lambda: self._install("batt", self.BATT_LNK),
            lambda: self._uninstall("batt", self.BATT_LNK),
            lambda: self._run_now("battery_manager.py"),
            "batt_status")

        self._sep(body)

        # ── Sezione 3: Installa entrambi ──────────────────────────────────────
        both_frame = tk.Frame(body, bg=C["bg"])
        both_frame.pack(fill="x")
        self._btn(both_frame, "⚡ Installa entrambi",
                  self._install_both, color=C["green"], w=22).pack(side="left", padx=4)
        self._btn(both_frame, "✖ Rimuovi entrambi",
                  self._uninstall_both, color=C["red"], w=22).pack(side="left", padx=4)
        self._btn(both_frame, "🚀 Avvia entrambi ora",
                  self._run_both, color=C["blue"], w=22).pack(side="left", padx=4)

        self._sep(body)

        # Log
        self._log_widget = tk.Text(body, height=5, bg=C["card"], fg=C["dim"],
                             font=("Consolas", 8), relief="flat",
                             state="disabled", padx=6, pady=4)
        self._log_widget.pack(fill="x")
        self._log("Pronto. Seleziona un'azione.")

        # Footer
        tk.Label(self.root,
                 text="Dati in: %s  |  Avvio: %s" % (DATA_DIR, STARTUP),
                 font=("Consolas", 7), fg=C["dim"], bg=C["bg"]
                 ).pack(pady=(0, 6))

    def _section(self, parent, title, desc, lnk, install_fn, uninstall_fn,
                 run_fn, status_attr: str):
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=4)

        tk.Label(f, text=title, font=("Consolas", 10, "bold"),
                 fg=C["blue"], bg=C["bg"]).pack(anchor="w")
        tk.Label(f, text=desc, font=("Consolas", 8),
                 fg=C["dim"], bg=C["bg"], justify="left").pack(anchor="w", pady=(2,4))

        row = tk.Frame(f, bg=C["bg"])
        row.pack(fill="x")

        self._btn(row, "Installa autostart", install_fn).pack(side="left", padx=2)
        self._btn(row, "Rimuovi autostart",  uninstall_fn,
                  color=C["yellow"]).pack(side="left", padx=2)
        self._btn(row, "Avvia ora",          run_fn,
                  color=C["green"]).pack(side="left", padx=2)

        installed = check_installed(lnk)
        status_lbl = tk.Label(row,
            text=("✓ Autostart attivo" if installed else "○ Non installato"),
            font=("Consolas", 8),
            fg=(C["green"] if installed else C["dim"]),
            bg=C["bg"])
        status_lbl.pack(side="left", padx=10)
        setattr(self, status_attr, status_lbl)

    def _log(self, msg: str):
        w = self._log_widget
        w.config(state="normal")
        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        w.insert("end", "[%s] %s\n" % (ts, msg))
        w.see("end")
        w.config(state="disabled")

    def _install(self, which: str, lnk: str):
        script = "tsc_clock_app.py" if which == "tsc" else "battery_manager.py"
        script_path = os.path.join(APP_DIR, script)
        make_shortcut(lnk, PYTHONW, '"%s"' % script_path,
                      "TSC Clock" if which == "tsc" else "Battery Manager")
        attr = "tsc_status" if which == "tsc" else "batt_status"
        getattr(self, attr).config(text="✓ Autostart attivo", fg=C["green"])
        self._log("Installato: %s" % lnk)

    def _uninstall(self, which: str, lnk: str):
        remove_shortcut(lnk)
        attr = "tsc_status" if which == "tsc" else "batt_status"
        getattr(self, attr).config(text="○ Non installato", fg=C["dim"])
        self._log("Rimosso: %s" % lnk)

    def _run_now(self, script: str):
        path = os.path.join(APP_DIR, script)
        subprocess.Popen([PYTHONW, path], creationflags=subprocess.DETACHED_PROCESS)
        self._log("Avviato: %s" % script)

    def _install_both(self):
        self._install("tsc",  self.TSC_LNK)
        self._install("batt", self.BATT_LNK)
        # Crea anche scorciatoie sul Desktop
        make_shortcut(self.TSC_DESK,  PYTHONW,
                      '"%s"' % os.path.join(APP_DIR, "tsc_clock_app.py"),
                      "TSC Clock", window_style=7)
        make_shortcut(self.BATT_DESK, PYTHONW,
                      '"%s"' % os.path.join(APP_DIR, "battery_manager.py"),
                      "Battery Manager", window_style=1)
        self._log("Installati entrambi + scorciatoie Desktop")
        messagebox.showinfo("Installazione completata",
            "TSC Clock e Battery Manager installati.\n\n"
            "Verranno avviati automaticamente al prossimo avvio di Windows.\n"
            "Scorciatoie create sul Desktop.")

    def _uninstall_both(self):
        for lnk in (self.TSC_LNK, self.BATT_LNK, self.TSC_DESK, self.BATT_DESK):
            remove_shortcut(lnk)
        self.tsc_status.config(text="○ Non installato",  fg=C["dim"])
        self.batt_status.config(text="○ Non installato", fg=C["dim"])
        self._log("Rimossi tutti gli autostart e scorciatoie Desktop")

    def _run_both(self):
        self._run_now("tsc_clock_app.py")
        time.sleep(0.3)
        self._run_now("battery_manager.py")

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = InstallerApp()
    app.run()
