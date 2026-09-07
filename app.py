"""
Detector de Silencio (versión notificación push) - App para Windows
=====================================================================
Monitorea una entrada O una salida de audio (placa de sonido) y, si
detecta silencio sostenido durante un tiempo configurable, envía una
notificación push (vía ntfy.sh, sin necesidad de cuenta) y, opcionalmente,
ejecuta un programa y/o reproduce un audio de respaldo.

El monitoreo de salida usa "loopback" de WASAPI (nativo de Windows): capta
lo que se está reproduciendo por esa salida sin necesitar cables virtuales
ni hardware extra.
"""

import json
import math
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
import winsound
from tkinter import filedialog, messagebox, ttk

import numpy as np
import sounddevice as sd

try:
    import soundcard as sc
except ImportError:
    sc = None  # se avisa al usuario si intenta usar el modo "Salida" sin esta librería

try:
    import comtypes  # necesario para inicializar COM en el hilo de loopback (Windows)
except ImportError:
    comtypes = None


def _app_dir() -> str:
    """Carpeta donde vive la app 'de verdad': la del .exe cuando está
    compilada (NO la carpeta temporal donde PyInstaller la descomprime en
    modo --onefile, que se borra sola al cerrar), o la del script .py."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(relative_path: str) -> str:
    """Ruta a un recurso empaquetado (ej. un ícono) incluido con --add-data.
    A diferencia de _app_dir(), acá SÍ corresponde usar la carpeta temporal
    de PyInstaller, porque ahí es donde deja los archivos de solo lectura
    que se empaquetaron junto con el .exe."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


CONFIG_PATH = os.path.join(_app_dir(), "config.json")

DB_MIN = -80  # piso del medidor de nivel (dBFS)
DB_MAX = 0    # techo del medidor de nivel (dBFS)
LOOPBACK_SAMPLERATE = 48000  # frecuencia estándar del mezclador WASAPI en Windows


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def rms_to_dbfs(block: np.ndarray) -> float:
    """Convierte un bloque de audio (float32, -1..1) a dBFS."""
    if block.size == 0:
        return DB_MIN
    rms = np.sqrt(np.mean(np.square(block)))
    if rms <= 1e-10:
        return DB_MIN
    db = 20 * math.log10(rms)
    return max(DB_MIN, min(DB_MAX, db))


DEFAULT_CONFIG = {
    "device_mode": "loopback",  # "loopback" (salida) o "input" (entrada)
    "device_index": None,
    "threshold_db": -50,
    "silence_seconds": 10,
    "cooldown_seconds": 120,
    "auto_start_monitoring": False,
    "schedule_enabled": False,
    "schedule_ranges": [],  # cada franja: {"start": "HH:MM", "end": "HH:MM", "days": [0..6]}
    "auto_save_on_close": False,
    "ntfy_enabled": True,
    "ntfy_topic": "",
    "action_run_program": False,
    "program_path": "",
    "action_play_audio": False,
    "audio_path": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception:
            pass
    # Migración: versiones viejas guardaban un único "schedule_days" global
    # aplicado a todas las franjas. Ahora cada franja tiene sus propios días.
    legacy_days = cfg.pop("schedule_days", None)
    if legacy_days is not None:
        ranges = []
        for r in cfg.get("schedule_ranges", []):
            r = dict(r)
            r.setdefault("days", legacy_days)
            ranges.append(r)
        cfg["schedule_ranges"] = ranges
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("No se pudo guardar la configuración:", e)


# ---------------------------------------------------------------------------
# Lógica de monitoreo de audio (corre en un hilo aparte)
# ---------------------------------------------------------------------------

class SilenceMonitor:
    def __init__(self, device_index, threshold_db, silence_seconds,
                 on_level, on_silence_triggered, on_error, on_sound_restored,
                 loopback=False):
        self.device_index = device_index  # int (sounddevice) o id string (soundcard, si loopback)
        self.threshold_db = threshold_db
        self.silence_seconds = silence_seconds
        self.on_level = on_level
        self.on_silence_triggered = on_silence_triggered
        self.on_sound_restored = on_sound_restored
        self.on_error = on_error
        self.loopback = loopback

        self._stream = None
        self._loopback_thread = None
        self._running = False
        self._last_above_ts = time.time()
        self._alerted = False

    def _process_block(self, block: np.ndarray):
        """Lógica común de detección de silencio, usada por ambos backends."""
        db = rms_to_dbfs(block)
        now = time.time()

        if db >= self.threshold_db:
            self._last_above_ts = now
            if self._alerted:
                self._alerted = False
                self.on_sound_restored()
        else:
            elapsed = now - self._last_above_ts
            if elapsed >= self.silence_seconds and not self._alerted:
                self._alerted = True
                self.on_silence_triggered(elapsed)

        self.on_level(db)

    # -- backend "entrada" (sounddevice) ---------------------------------
    def _sd_callback(self, indata, frames, time_info, status):
        block = indata[:, 0] if indata.ndim > 1 else indata
        self._process_block(block)

    def _start_input(self):
        device_info = sd.query_devices(self.device_index, "input")
        samplerate = int(device_info["default_samplerate"])
        self._stream = sd.InputStream(
            device=self.device_index,
            channels=1,
            samplerate=samplerate,
            callback=self._sd_callback,
            blocksize=int(samplerate * 0.1),  # bloques de ~100ms
        )
        self._stream.start()

    # -- backend "salida / loopback" (soundcard) -------------------------
    def _start_loopback(self):
        if sc is None:
            raise RuntimeError(
                "Falta instalar la librería 'soundcard'. Ejecutá: pip install soundcard")
        # Todo (obtener el micrófono Y grabar) se hace DENTRO del mismo hilo,
        # con COM inicializado en modo multithreaded (MTA), que es lo que
        # necesita WASAPI para grabación. Mezclar hilos o apartments de COM
        # es justamente lo que provoca el error 0x800401F0.
        self._loopback_thread = threading.Thread(target=self._loopback_loop, daemon=True)
        self._loopback_thread.start()

    def _loopback_loop(self):
        if sys.platform == "win32" and comtypes is None:
            self._running = False
            self.on_error(
                "Falta instalar la librería 'comtypes' (necesaria para el modo Salida/loopback). "
                "Ejecutá: pip install comtypes")
            return

        com_initialized = False
        if comtypes is not None and sys.platform == "win32":
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
                com_initialized = True
            except OSError:
                # Ya había un modelo de apartment inicializado en este hilo:
                # no es un error real, seguimos.
                com_initialized = True
            except Exception as e:
                self._running = False
                self.on_error(f"No se pudo inicializar COM para el audio: {e}")
                return

        try:
            mic = sc.get_microphone(id=self.device_index, include_loopback=True)
            blocksize = int(LOOPBACK_SAMPLERATE * 0.1)  # bloques de ~100ms
            with mic.recorder(samplerate=LOOPBACK_SAMPLERATE) as recorder:
                while self._running:
                    data = recorder.record(numframes=blocksize)
                    self._process_block(data)
        except Exception as e:
            if self._running:
                self._running = False
                self.on_error(str(e))
        finally:
            if com_initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    # -- control general ---------------------------------------------------
    def start(self):
        self._last_above_ts = time.time()
        self._alerted = False
        self._running = True
        try:
            if self.loopback:
                self._start_loopback()
            else:
                self._start_input()
        except Exception as e:
            self._running = False
            self.on_error(str(e))

    def stop(self):
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        # El hilo de loopback termina solo al ver self._running en False
        # (sale del "with mic.recorder(...)" en la próxima vuelta del loop).
        self._loopback_thread = None

    @property
    def running(self):
        return self._running


# ---------------------------------------------------------------------------
# Envío de notificación push y ejecución de acciones (en hilos aparte, para
# no trabar la interfaz)
# ---------------------------------------------------------------------------

NTFY_SERVER = "https://ntfy.sh"


def generate_random_ntfy_topic() -> str:
    """Genera un nombre de canal difícil de adivinar (el canal funciona como
    contraseña: cualquiera que sepa el nombre puede suscribirse a él)."""
    return "silencio-" + secrets.token_hex(4)


def send_ntfy(cfg: dict, elapsed_seconds: float, log_fn):
    topic = cfg.get("ntfy_topic", "").strip()
    if not topic:
        log_fn("ERROR: no hay canal de notificación push (ntfy) configurado.")
        return
    message = (f"Se detectó silencio durante más de {int(elapsed_seconds)} segundos.\n"
               f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
    url = f"{NTFY_SERVER}/{topic}"
    try:
        req = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            method="POST",
            headers={
                "Title": "Alerta: silencio detectado",
                "Priority": "urgent",
                "Tags": "warning",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        log_fn(f"Notificación push enviada al canal '{topic}'.")
    except urllib.error.URLError as e:
        log_fn(f"ERROR enviando notificación push: {e}")
    except Exception as e:
        log_fn(f"ERROR enviando notificación push: {e}")


def run_program(path: str, log_fn):
    try:
        subprocess.Popen(path, shell=True)
        log_fn(f"Programa ejecutado: {path}")
    except Exception as e:
        log_fn(f"ERROR ejecutando programa: {e}")


def play_backup_audio(path: str, log_fn):
    try:
        # winsound solo reproduce WAV, pero es nativo de Windows (sin
        # dependencias extra) y permite reproducción asíncrona.
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        log_fn(f"Reproduciendo audio de respaldo: {path}")
    except Exception as e:
        log_fn(f"ERROR reproduciendo audio: {e}")


WEEKDAY_LABELS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]  # 0=Lunes .. 6=Domingo


def _hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------------------
# Widget reutilizable: lista de franjas horarias
# ---------------------------------------------------------------------------

class TimeRangeListEditor(ttk.Frame):
    """Permite agregar, editar y quitar franjas horarias (HH:MM a HH:MM),
    cada una con sus propios días de la semana activos."""

    def __init__(self, parent, initial_ranges=None):
        super().__init__(parent)
        self.ranges = [dict(r) for r in (initial_ranges or [])]
        self._editing_index = None  # None = modo "agregar"; int = editando esa franja

        row1 = ttk.Frame(self)
        row1.pack(fill="x")

        ttk.Label(row1, text="Desde:").pack(side="left")
        self.start_hour = tk.StringVar(value="08")
        self.start_min = tk.StringVar(value="00")
        ttk.Spinbox(row1, from_=0, to=23, width=3, textvariable=self.start_hour,
                    format="%02.0f", wrap=True).pack(side="left", padx=(4, 0))
        ttk.Label(row1, text=":").pack(side="left")
        ttk.Spinbox(row1, from_=0, to=59, width=3, textvariable=self.start_min,
                    format="%02.0f", increment=5, wrap=True).pack(side="left", padx=(0, 10))

        ttk.Label(row1, text="Hasta:").pack(side="left")
        self.end_hour = tk.StringVar(value="20")
        self.end_min = tk.StringVar(value="00")
        ttk.Spinbox(row1, from_=0, to=23, width=3, textvariable=self.end_hour,
                    format="%02.0f", wrap=True).pack(side="left", padx=(4, 0))
        ttk.Label(row1, text=":").pack(side="left")
        ttk.Spinbox(row1, from_=0, to=59, width=3, textvariable=self.end_min,
                    format="%02.0f", increment=5, wrap=True).pack(side="left", padx=(0, 10))

        row2 = ttk.Frame(self)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Días:").pack(side="left", padx=(0, 4))
        self.day_vars = {}
        for idx, label in enumerate(WEEKDAY_LABELS):
            var = tk.BooleanVar(value=True)  # por defecto, todos los días tildados
            self.day_vars[idx] = var
            ttk.Checkbutton(row2, text=label, variable=var).pack(side="left", padx=(0, 6))

        row3 = ttk.Frame(self)
        row3.pack(fill="x", pady=(8, 0))
        self.add_btn = ttk.Button(row3, text="+ Agregar franja", command=self._add_or_save)
        self.add_btn.pack(side="left", padx=(0, 5))
        ttk.Button(row3, text="✏ Editar seleccionada", command=self._edit_selected).pack(side="left", padx=(0, 5))
        ttk.Button(row3, text="Cancelar edición", command=self._cancel_edit).pack(side="left")

        self.listbox = tk.Listbox(self, height=5, width=55, selectmode="extended")
        self.listbox.pack(fill="x", pady=(8, 0))

        ttk.Button(self, text="Quitar seleccionada(s)", command=self._remove_selected).pack(anchor="w", pady=(5, 0))

        self._refresh_listbox()

    def _read_fields(self):
        """Lee y valida los campos actuales. Devuelve la franja o None si algo está mal
        (y ya mostró el aviso correspondiente)."""
        try:
            start = f"{int(self.start_hour.get()):02d}:{int(self.start_min.get()):02d}"
            end = f"{int(self.end_hour.get()):02d}:{int(self.end_min.get()):02d}"
        except ValueError:
            messagebox.showwarning("Franja inválida", "Revisá los valores de hora.", parent=self)
            return None
        if start == end:
            messagebox.showwarning("Franja inválida", "La hora de inicio y fin no pueden ser iguales.", parent=self)
            return None
        days = sorted(idx for idx, var in self.day_vars.items() if var.get())
        if not days:
            messagebox.showwarning("Franja inválida", "Elegí al menos un día para esta franja.", parent=self)
            return None
        return {"start": start, "end": end, "days": days}

    def _add_or_save(self):
        entry = self._read_fields()
        if entry is None:
            return

        if self._editing_index is not None:
            self.ranges[self._editing_index] = entry
            self._exit_edit_mode()
        else:
            if entry in self.ranges:
                messagebox.showinfo(
                    "Ya agregada", "Esa franja (con esos mismos días) ya está en la lista.", parent=self)
                return
            self.ranges.append(entry)
        self._refresh_listbox()

    def _edit_selected(self):
        sel = self.listbox.curselection()
        if len(sel) != 1:
            messagebox.showwarning(
                "Elegí una franja", "Seleccioná exactamente una franja de la lista para editar.", parent=self)
            return
        idx = sel[0]
        entry = self.ranges[idx]

        sh, sm = entry["start"].split(":")
        eh, em = entry["end"].split(":")
        self.start_hour.set(sh)
        self.start_min.set(sm)
        self.end_hour.set(eh)
        self.end_min.set(em)
        selected_days = set(entry.get("days", list(range(7))))
        for i, var in self.day_vars.items():
            var.set(i in selected_days)

        self._editing_index = idx
        self.add_btn.config(text="💾 Guardar cambios")

    def _cancel_edit(self):
        self._exit_edit_mode()

    def _exit_edit_mode(self):
        self._editing_index = None
        self.add_btn.config(text="+ Agregar franja")

    def _remove_selected(self):
        for idx in reversed(self.listbox.curselection()):
            del self.ranges[idx]
        self._exit_edit_mode()  # los índices pueden haber cambiado; evitamos editar el que no es
        self._refresh_listbox()

    def _refresh_listbox(self):
        self.listbox.delete(0, "end")
        for r in self.ranges:
            days = r.get("days", list(range(7)))
            if len(days) == 7:
                days_txt = "todos los días"
            else:
                days_txt = ", ".join(WEEKDAY_LABELS[d] for d in days)
            self.listbox.insert("end", f"{r['start']} a {r['end']}  —  {days_txt}")

    def get_ranges(self):
        return [dict(r) for r in self.ranges]

    def set_ranges(self, ranges):
        self.ranges = [dict(r) for r in ranges]
        self._exit_edit_mode()
        self._refresh_listbox()


# ---------------------------------------------------------------------------
# Interfaz gráfica
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Detector de Silencio")
        self.geometry("720x980")
        self.minsize(680, 760)
        self.resizable(True, True)
        self._set_app_icon()

        self.cfg = load_config()
        self.monitor: SilenceMonitor | None = None
        self.ui_queue = queue.Queue()
        self.last_alert_ts = 0

        self._build_ui()
        self._refresh_devices()
        self.after(100, self._poll_queue)
        self.after(500, self._initial_autostart_check)
        self.after(1000, self._schedule_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_app_icon(self):
        """Aplica el ícono a la ventana y a la barra de tareas. Busca un
        archivo llamado 'icono.ico' junto al .exe/script (y, si la app
        está compilada, también empaquetado con --add-data)."""
        icon_path = resource_path("icono.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(_app_dir(), "icono.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass  # si el .ico tiene un formato raro, seguimos sin ícono

    # -- construcción de la interfaz -----------------------------------
    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        monitor_tab = ttk.Frame(notebook)
        config_tab = ttk.Frame(notebook)
        notebook.add(monitor_tab, text="Monitor")
        notebook.add(config_tab, text="Configuración")

        self._build_monitor_tab(monitor_tab)
        self._build_config_tab(config_tab)

    def _build_monitor_tab(self, parent):
        # Selección de placa
        frm_device = ttk.LabelFrame(parent, text="Fuente de audio")
        frm_device.pack(fill="x", padx=5, pady=5)

        self.mode_var = tk.StringVar(value=self.cfg.get("device_mode", "loopback"))
        frm_mode = ttk.Frame(frm_device)
        frm_mode.pack(fill="x", padx=5, pady=(8, 0))
        ttk.Radiobutton(frm_mode, text="Salida (lo que se está reproduciendo - loopback)",
                         variable=self.mode_var, value="loopback",
                         command=self._on_mode_changed).pack(side="left", padx=(0, 15))
        ttk.Radiobutton(frm_mode, text="Entrada (micrófono / línea)",
                         variable=self.mode_var, value="input",
                         command=self._on_mode_changed).pack(side="left")

        frm_dev_row = ttk.Frame(frm_device)
        frm_dev_row.pack(fill="x", padx=5, pady=8)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(frm_dev_row, textvariable=self.device_var, state="readonly", width=55)
        self.device_combo.pack(side="left", padx=(0, 5))
        ttk.Button(frm_dev_row, text="Actualizar", command=self._refresh_devices).pack(side="left", padx=5)

        self.device_hint_label = ttk.Label(frm_device, text="", foreground="gray", wraplength=580, justify="left")
        self.device_hint_label.pack(fill="x", padx=5, pady=(0, 8))
        self._update_device_hint()

        # Nivel en vivo
        frm_level = ttk.LabelFrame(parent, text="Nivel de audio en vivo")
        frm_level.pack(fill="x", padx=5, pady=5)

        self.level_bar = ttk.Progressbar(frm_level, length=560, maximum=100)
        self.level_bar.pack(padx=5, pady=8)
        self.level_label = ttk.Label(frm_level, text="-- dBFS")
        self.level_label.pack(pady=(0, 8))

        # Umbral y tiempo
        frm_params = ttk.LabelFrame(parent, text="Parámetros de detección")
        frm_params.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_params, text="Umbral de silencio (dBFS):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.threshold_var = tk.IntVar(value=self.cfg["threshold_db"])
        ttk.Scale(frm_params, from_=DB_MIN, to=0, orient="horizontal", variable=self.threshold_var,
                  length=300, command=lambda v: self.threshold_label.config(text=f"{int(float(v))} dBFS")
                  ).grid(row=0, column=1, padx=5)
        self.threshold_label = ttk.Label(frm_params, text=f"{self.threshold_var.get()} dBFS")
        self.threshold_label.grid(row=0, column=2, padx=5)

        ttk.Label(frm_params, text="Tiempo de silencio antes de avisar (segundos):").grid(
            row=1, column=0, sticky="w", padx=5, pady=5)
        self.duration_var = tk.IntVar(value=self.cfg["silence_seconds"])
        ttk.Spinbox(frm_params, from_=1, to=3600, textvariable=self.duration_var, width=8).grid(
            row=1, column=1, sticky="w", padx=5)

        ttk.Label(frm_params, text="Espera mínima entre avisos (segundos):").grid(
            row=2, column=0, sticky="w", padx=5, pady=5)
        self.cooldown_var = tk.IntVar(value=self.cfg["cooldown_seconds"])
        ttk.Spinbox(frm_params, from_=0, to=86400, textvariable=self.cooldown_var, width=8).grid(
            row=2, column=1, sticky="w", padx=5)

        # Botones de inicio/parada
        frm_ctrl = ttk.Frame(parent)
        frm_ctrl.pack(fill="x", padx=5, pady=10)
        self.start_btn = ttk.Button(frm_ctrl, text="▶ Iniciar monitoreo", command=self._start_monitoring)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(frm_ctrl, text="■ Detener", command=self._stop_monitoring, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        self.status_label = ttk.Label(frm_ctrl, text="Detenido", foreground="gray")
        self.status_label.pack(side="left", padx=15)

        self.auto_start_var = tk.BooleanVar(value=self.cfg.get("auto_start_monitoring", False))
        ttk.Checkbutton(
            parent, text="Iniciar el monitoreo automáticamente al abrir el programa",
            variable=self.auto_start_var,
        ).pack(anchor="w", padx=5, pady=(0, 4))
        ttk.Label(
            parent,
            text="Útil si configurás Windows para que abra el programa solo al iniciar la PC. "
                 "Si además activás la programación por horario de abajo, esa programación manda "
                 "y esta opción se ignora. No te olvides de guardar la configuración.",
            foreground="gray", wraplength=620, justify="left",
        ).pack(anchor="w", padx=5, pady=(0, 8))

        # Programación automática por horario
        frm_schedule = ttk.LabelFrame(parent, text="Programación automática por horario")
        frm_schedule.pack(fill="x", padx=5, pady=(0, 8))

        self.schedule_enabled_var = tk.BooleanVar(value=self.cfg.get("schedule_enabled", False))
        ttk.Checkbutton(
            frm_schedule, text="Activar programación por horario (enciende/apaga el monitoreo solo)",
            variable=self.schedule_enabled_var,
        ).pack(anchor="w", padx=5, pady=(8, 4))

        ttk.Label(frm_schedule, text="Franjas horarias en las que el monitoreo debe estar ACTIVO "
                                      "(cada una con sus propios días):").pack(anchor="w", padx=5, pady=(4, 2))
        self.time_range_editor = TimeRangeListEditor(
            frm_schedule, initial_ranges=self.cfg.get("schedule_ranges", []))
        self.time_range_editor.pack(anchor="w", padx=5, pady=(0, 4))

        ttk.Label(
            frm_schedule,
            text="Por ejemplo, cargá 13:00 a 20:00 (todos los días) y 05:00 a 08:00 (solo fines de "
                 "semana) si querés que el monitoreo esté prendido solo en esos horarios y días. Podés "
                 "agregar todas las franjas que necesites, cada una con sus propios días tildados. "
                 "Fuera de los días/horarios elegidos, el programa detiene el monitoreo "
                 "automáticamente, incluso si lo habías iniciado a mano.",
            foreground="gray", wraplength=620, justify="left",
        ).pack(anchor="w", padx=5, pady=(0, 8))

        # Log
        frm_log = ttk.LabelFrame(parent, text="Registro de eventos")
        frm_log.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_text = tk.Text(frm_log, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

    def _build_config_tab(self, parent):
        # Preferencias generales
        frm_prefs = ttk.LabelFrame(parent, text="Preferencias")
        frm_prefs.pack(fill="x", padx=5, pady=5)

        self.auto_save_close_var = tk.BooleanVar(value=self.cfg.get("auto_save_on_close", False))
        ttk.Checkbutton(
            frm_prefs, text="Guardar la configuración automáticamente al cerrar el programa",
            variable=self.auto_save_close_var,
        ).pack(anchor="w", padx=5, pady=8)

        # Notificación push (ntfy.sh)
        frm_ntfy = ttk.LabelFrame(parent, text="Aviso por notificación push (ntfy.sh) — sin cuenta, gratis")
        frm_ntfy.pack(fill="x", padx=5, pady=5)

        self.ntfy_enabled_var = tk.BooleanVar(value=self.cfg.get("ntfy_enabled", True))
        ttk.Checkbutton(frm_ntfy, text="Activar notificación push", variable=self.ntfy_enabled_var).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=5, pady=(8, 4))

        self.ntfy_topic_var = tk.StringVar(value=self.cfg.get("ntfy_topic", ""))
        ttk.Label(frm_ntfy, text="Nombre de canal:").grid(row=1, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(frm_ntfy, textvariable=self.ntfy_topic_var, width=35).grid(row=1, column=1, sticky="w", padx=5)
        ttk.Button(frm_ntfy, text="Generar aleatorio", command=self._generate_ntfy_topic).grid(
            row=1, column=2, padx=5)

        ttk.Label(frm_ntfy,
                  text="Este nombre funciona como una 'contraseña': cualquiera que lo sepa puede\n"
                       "suscribirse a este canal, así que usá uno difícil de adivinar (el botón\n"
                       "'Generar aleatorio' ya te arma uno). Cada persona que quiera recibir el\n"
                       "aviso instala la app ntfy (o entra a la página de abajo desde el celular)\n"
                       "y se suscribe a ese mismo nombre de canal.",
                  foreground="gray", justify="left").grid(row=2, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 8))

        frm_ntfy_btns = ttk.Frame(frm_ntfy)
        frm_ntfy_btns.grid(row=3, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 8))
        ttk.Button(frm_ntfy_btns, text="Abrir canal en el navegador",
                   command=self._open_ntfy_channel).pack(side="left", padx=(0, 5))
        ttk.Button(frm_ntfy_btns, text="Enviar notificación de prueba",
                   command=self._send_test_ntfy).pack(side="left")

        # Acciones
        frm_actions = ttk.LabelFrame(parent, text="Acción automática al detectar silencio")
        frm_actions.pack(fill="x", padx=5, pady=5)

        self.run_program_var = tk.BooleanVar(value=self.cfg["action_run_program"])
        self.program_path_var = tk.StringVar(value=self.cfg["program_path"])
        ttk.Checkbutton(frm_actions, text="Ejecutar programa / script:", variable=self.run_program_var).grid(
            row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(frm_actions, textvariable=self.program_path_var, width=45).grid(row=0, column=1, padx=5)
        ttk.Button(frm_actions, text="Elegir...", command=self._pick_program).grid(row=0, column=2, padx=5)

        self.play_audio_var = tk.BooleanVar(value=self.cfg["action_play_audio"])
        self.audio_path_var = tk.StringVar(value=self.cfg["audio_path"])
        ttk.Checkbutton(frm_actions, text="Reproducir audio de respaldo (.wav):", variable=self.play_audio_var).grid(
            row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(frm_actions, textvariable=self.audio_path_var, width=45).grid(row=1, column=1, padx=5)
        ttk.Button(frm_actions, text="Elegir...", command=self._pick_audio).grid(row=1, column=2, padx=5)

        ttk.Label(frm_actions, text="Se puede elegir una, ambas o ninguna acción.",
                  foreground="gray").grid(row=2, column=1, sticky="w", padx=5, pady=(0, 5))

        ttk.Button(parent, text="Guardar configuración", command=self._save_all).pack(pady=(15, 5))

        # Créditos, discretos, al pie de la pestaña
        credits_frame = ttk.Frame(parent)
        credits_frame.pack(pady=(5, 15))
        ttk.Label(credits_frame, text="Hecho por", foreground="gray",
                  font=("", 8)).pack(side="left")
        credits_link = ttk.Label(credits_frame, text="twitch.tv/luisochannel", foreground="#9147ff",
                                  cursor="hand2", font=("", 8, "underline"))
        credits_link.pack(side="left", padx=(4, 4))
        credits_link.bind("<Button-1>", lambda e: webbrowser.open("https://twitch.tv/luisochannel"))
        ttk.Label(credits_frame, text="junto a Claude (Anthropic)", foreground="gray",
                  font=("", 8)).pack(side="left")

    # -- dispositivos de audio -------------------------------------------
    def _on_mode_changed(self):
        self._update_device_hint()
        self._refresh_devices()

    def _update_device_hint(self):
        if self.mode_var.get() == "loopback":
            self.device_hint_label.config(
                text="Elegí el dispositivo de SALIDA cuyo audio querés monitorear (por ejemplo, "
                     "los parlantes o la salida que va hacia el streaming/transmisor). "
                     "Usa loopback de WASAPI, nativo de Windows 10/11.")
        else:
            self.device_hint_label.config(
                text="Elegí el dispositivo de ENTRADA a monitorear (micrófono, línea, o el canal "
                     "de entrada de una interfaz de audio).")

    def _refresh_devices(self):
        loopback = self.mode_var.get() == "loopback"
        self._device_list = []
        display_values = []

        if loopback:
            if sc is None:
                self.device_combo["values"] = []
                self.device_var.set("")
                messagebox.showerror(
                    "Falta un componente",
                    "Para monitorear una salida hace falta instalar la librería 'soundcard'.\n\n"
                    "Cerrá la app y ejecutá en la consola:\n  pip install soundcard")
                return
            try:
                mics = sc.all_microphones(include_loopback=True)
            except Exception as e:
                messagebox.showerror("Error", f"No se pudieron listar las salidas de audio:\n{e}")
                mics = []
            for m in mics:
                if getattr(m, "isloopback", False):
                    self._device_list.append(m.id)
                    display_values.append(m.name)
        else:
            devices = sd.query_devices()
            for idx, d in enumerate(devices):
                if d["max_input_channels"] > 0:
                    self._device_list.append(idx)
                    display_values.append(f"[{idx}] {d['name']}")

        self.device_combo["values"] = display_values

        saved_mode = self.cfg.get("device_mode", "loopback")
        saved_idx = self.cfg.get("device_index")
        if saved_mode == self.mode_var.get() and saved_idx is not None and saved_idx in self._device_list:
            pos = self._device_list.index(saved_idx)
            self.device_combo.current(pos)
        elif display_values:
            self.device_combo.current(0)
        else:
            self.device_var.set("")

    def _selected_device_index(self):
        pos = self.device_combo.current()
        if pos < 0 or pos >= len(self._device_list):
            return None
        return self._device_list[pos]

    # -- monitoreo --------------------------------------------------------
    def _start_monitoring(self, silent=False):
        if self.monitor is not None and self.monitor.running:
            return  # ya estaba andando, no hay nada que hacer

        device_index = self._selected_device_index()
        if device_index is None:
            if silent:
                self._log("No se pudo iniciar el monitoreo automáticamente: no hay dispositivo seleccionado.")
            else:
                messagebox.showwarning("Atención", "Elegí una fuente de audio antes de iniciar.")
            return

        loopback = self.mode_var.get() == "loopback"

        self.monitor = SilenceMonitor(
            device_index=device_index,
            threshold_db=self.threshold_var.get(),
            silence_seconds=self.duration_var.get(),
            on_level=lambda db: self.ui_queue.put(("level", db)),
            on_silence_triggered=lambda elapsed: self.ui_queue.put(("silence", elapsed)),
            on_sound_restored=lambda: self.ui_queue.put(("restored", None)),
            on_error=lambda msg: self.ui_queue.put(("error", msg)),
            loopback=loopback,
        )
        self.monitor._started_silently = silent
        self.monitor.start()
        if self.monitor.running:
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.status_label.config(text="Monitoreando...", foreground="green")
            modo_txt = "salida (loopback)" if loopback else "entrada"
            self._log(f"Monitoreo iniciado en modo {modo_txt}.")

    def _stop_monitoring(self):
        if self.monitor:
            self.monitor.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text="Detenido", foreground="gray")
        self._log("Monitoreo detenido.")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "level":
                    self._update_level(payload)
                elif kind == "silence":
                    self._handle_silence(payload)
                elif kind == "restored":
                    self._log("El audio volvió a niveles normales.")
                elif kind == "error":
                    self._log(f"ERROR: {payload}")
                    # Si el monitoreo se había iniciado automáticamente (por horario o
                    # auto-inicio), NO mostramos una ventana bloqueante: nadie tiene por
                    # qué estar mirando la pantalla, y una ventana sin cerrar dejaría todo
                    # trabado en vez de dejar que el programador reintente solo.
                    was_silent = getattr(self.monitor, "_started_silently", False)
                    if not was_silent:
                        messagebox.showerror("Error de audio", payload)
                    self._stop_monitoring()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _update_level(self, db):
        pct = (db - DB_MIN) / (DB_MAX - DB_MIN) * 100
        self.level_bar["value"] = max(0, min(100, pct))
        self.level_label.config(text=f"{db:.1f} dBFS")

    def _handle_silence(self, elapsed):
        now = time.time()
        cooldown = self.cooldown_var.get()
        if now - self.last_alert_ts < cooldown:
            self._log(f"Silencio detectado ({elapsed:.0f}s) pero en período de espera; no se reenvía aviso.")
            return
        self.last_alert_ts = now
        self._log(f"¡Silencio detectado durante {elapsed:.0f} segundos! Disparando alertas...")

        cfg = self._collect_config()

        if cfg.get("ntfy_enabled") and cfg.get("ntfy_topic"):
            threading.Thread(target=send_ntfy, args=(cfg, elapsed, self._log), daemon=True).start()

        if cfg["action_run_program"] and cfg["program_path"]:
            threading.Thread(target=run_program, args=(cfg["program_path"], self._log), daemon=True).start()

        if cfg["action_play_audio"] and cfg["audio_path"]:
            threading.Thread(target=play_backup_audio, args=(cfg["audio_path"], self._log), daemon=True).start()

    def _log(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # -- configuración ------------------------------------------------
    def _pick_program(self):
        path = filedialog.askopenfilename(title="Elegir programa o script")
        if path:
            self.program_path_var.set(path)

    def _pick_audio(self):
        path = filedialog.askopenfilename(title="Elegir audio de respaldo",
                                           filetypes=[("Archivos WAV", "*.wav")])
        if path:
            self.audio_path_var.set(path)

    def _collect_config(self) -> dict:
        return {
            "device_mode": self.mode_var.get(),
            "device_index": self._selected_device_index(),
            "threshold_db": self.threshold_var.get(),
            "silence_seconds": self.duration_var.get(),
            "cooldown_seconds": self.cooldown_var.get(),
            "auto_start_monitoring": self.auto_start_var.get(),
            "schedule_enabled": self.schedule_enabled_var.get(),
            "schedule_ranges": self.time_range_editor.get_ranges(),
            "auto_save_on_close": self.auto_save_close_var.get(),
            "ntfy_enabled": self.ntfy_enabled_var.get(),
            "ntfy_topic": self.ntfy_topic_var.get().strip(),
            "action_run_program": self.run_program_var.get(),
            "program_path": self.program_path_var.get(),
            "action_play_audio": self.play_audio_var.get(),
            "audio_path": self.audio_path_var.get(),
        }

    def _save_all(self, silent=False):
        self.cfg = self._collect_config()
        save_config(self.cfg)
        if not silent:
            messagebox.showinfo("Configuración", "Configuración guardada correctamente.")

    def _generate_ntfy_topic(self):
        self.ntfy_topic_var.set(generate_random_ntfy_topic())

    def _open_ntfy_channel(self):
        topic = self.ntfy_topic_var.get().strip()
        if not topic:
            messagebox.showwarning("Atención", "Primero elegí (o generá) un nombre de canal.")
            return
        webbrowser.open(f"{NTFY_SERVER}/{topic}")

    def _send_test_ntfy(self):
        topic = self.ntfy_topic_var.get().strip()
        if not topic:
            messagebox.showwarning("Atención", "Primero elegí (o generá) un nombre de canal.")
            return
        cfg = self._collect_config()
        self._log("Enviando notificación push de prueba...")
        threading.Thread(target=send_ntfy, args=(cfg, 0, self._log), daemon=True).start()

    # -- auto-inicio y programación por horario ---------------------------
    def _initial_autostart_check(self):
        if self.cfg.get("schedule_enabled"):
            self._log("Programación por horario activada: se revisa cada 30 segundos si corresponde "
                       "estar monitoreando según el día/hora actual.")
            return  # la programación por horario manda; se resuelve sola en _schedule_tick
        if self.cfg.get("auto_start_monitoring"):
            self._log("Auto-inicio activado: iniciando monitoreo automáticamente...")
            self._start_monitoring(silent=True)

    def _schedule_wants_monitoring_on(self) -> bool:
        now_struct = time.localtime()
        today = now_struct.tm_wday
        now_minutes = now_struct.tm_hour * 60 + now_struct.tm_min
        for r in self.time_range_editor.get_ranges():
            if today not in r.get("days", list(range(7))):
                continue
            start_m = _hhmm_to_minutes(r["start"])
            end_m = _hhmm_to_minutes(r["end"])
            if start_m <= end_m:
                if start_m <= now_minutes < end_m:
                    return True
            else:  # franja que cruza la medianoche (ej. 22:00 a 06:00)
                if now_minutes >= start_m or now_minutes < end_m:
                    return True
        return False

    def _schedule_tick(self):
        try:
            if self.schedule_enabled_var.get():
                desired_on = self._schedule_wants_monitoring_on()
                currently_on = self.monitor is not None and self.monitor.running
                if desired_on and not currently_on:
                    self._log("Programación automática: iniciando monitoreo (dentro de la franja configurada).")
                    self._start_monitoring(silent=True)
                elif not desired_on and currently_on:
                    self._log("Programación automática: deteniendo monitoreo (fuera de la franja configurada).")
                    self._stop_monitoring()
        except Exception as e:
            self._log(f"ERROR en programación automática: {e}")
        finally:
            self.after(30000, self._schedule_tick)  # vuelve a revisar cada 30 segundos

    def _on_close(self):
        if self.auto_save_close_var.get():
            self._save_all(silent=True)
        self._stop_monitoring()
        self.destroy()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Aviso: esta app usa 'winsound', que solo funciona en Windows. "
              "En otros sistemas operativos la reproducción de audio de respaldo fallará.")
    app = App()
    app.mainloop()
