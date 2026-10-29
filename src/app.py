"""
Nanuve iCloud Downloader
GUI de escritorio que envuelve icloudpd.exe para descargar todas las fotos
y vídeos de iCloud Photos a una carpeta local, organizados por día.

Estilo visual calcado de nanuve.net (fondo oscuro, acento cian, Hanken Grotesk).
"""
import ctypes
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox
from tkinter import ttk

# ---------------------------------------------------------------- recursos

def resource_path(name: str) -> str:
    """
    Ruta a un recurso empaquetado. En el .exe de PyInstaller vive en la
    carpeta temporal de extracción (_MEIPASS). En modo script (desarrollo)
    los recursos viven en gui/assets/ y icloudpd.exe en la raíz del proyecto,
    así que se prueban varias ubicaciones sin necesidad de copiar nada.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, name)

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "assets", name),
        os.path.join(here, "..", name),
        os.path.join(here, name),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def app_dir() -> str:
    """Carpeta donde vive el .exe real (persiste entre ejecuciones), no la carpeta temporal de extracción."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def set_dpi_awareness():
    """Evita que Windows escale/difumine la ventana en pantallas HiDPI."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def kill_process_tree(pid: int):
    """Mata un proceso y todos sus hijos (icloudpd empaquetado lanza un proceso hijo propio)."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def prevent_sleep(enable: bool):
    """Evita que Windows suspenda el equipo mientras hay una descarga larga en curso."""
    if sys.platform != "win32":
        return
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    try:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if enable else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def apply_dark_titlebar(window: tk.Misc):
    """Oscurece la barra de titulo nativa de Windows para que no desentone con el tema oscuro."""
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 20H1+/11). 19 = version anterior.
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        # Color exacto de la barra de titulo en Windows 11 (formato COLORREF 0x00BBGGRR).
        caption_color = ctypes.c_int(0x00130E0A)  # BGR de #0a0e13
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))
    except Exception:
        pass


CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".nanuve_icloud_downloader.json")
LOG_PATH = os.path.join(app_dir(), "nanuve_icloud_downloader.log")
MAX_LOG_LINES = 2000

# Lineas que icloudpd imprime listando telefonos de confianza antes del prompt de 2FA,
# por ejemplo "  a: +34 *** **44". Letra de una sola posicion + dos puntos + numero ofuscado.
TRUSTED_PHONE_RE = re.compile(r"^\s{0,4}([a-z]):\s*(\+?[\d\s*().-]{6,})$")

# Linea que icloudpd imprime una vez al principio con el total a descargar, por ejemplo:
# "Downloading 12345 original photos and videos to C:\destino ...". Si el total no se
# puede calcular de antemano (p.ej. con --recent) imprime "???" y no hay barra real posible.
TOTAL_COUNT_RE = re.compile(r"^Downloading\s+(\d+|\?\?\?)\s+\S.*\bto\b.+\.\.\.\s*$")

# ---------------------------------------------------------------- paleta Nanuve
BG = "#0a0e13"
BG_ELEV = "#0f1620"
LINE = "#22303d"
LINE_SOFT = "#1a2630"
TEXT = "#e8eef4"
MUTED = "#9aa9b8"
BRAND = "#00aeef"
BRAND_QUIET = "#5fc7ef"
BRAND_TEXT_ON = "#04222c"

FONT_FAMILY = "Hanken Grotesk"

# Traducciones de los mensajes mas habituales de icloudpd a algo entendible.
ERROR_TRANSLATIONS = [
    (("invalid email/password", "invalid username/password"), "El Apple ID o la contraseña no son correctos."),
    (("access_denied", "not activated", "setup is not complete"),
     "Apple ha denegado el acceso. Revisa que 'Acceso a datos de iCloud en la Web' esté activado y que "
     "'Protección de Datos Avanzada' esté desactivada en tu cuenta de Apple."),
    (("service not available", "503", "cannot connect to apple"),
     "Apple está limitando temporalmente el acceso. Espera una hora antes de reintentar: reintentar en bucle "
     "empeora el bloqueo."),
    (("failed to verify two-factor", "invalid code"), "El código de verificación introducido no era correcto."),
    (("no space left", "errno 28"), "No queda espacio libre en la carpeta de destino."),
    (("permission", "errno 13"), "No hay permisos para escribir en la carpeta de destino."),
    (("retrying", "error downloading"), "Hubo un problema de red durante la descarga; icloudpd reintenta solo."),
]


def translate_error(recent_log: str) -> str | None:
    lower = recent_log.lower()
    for keywords, message in ERROR_TRANSLATIONS:
        if any(k in lower for k in keywords):
            return message
    return None


def load_private_fonts():
    """Carga las fuentes Hanken Grotesk como privadas de la sesión (no se instalan en el sistema)."""
    if sys.platform != "win32":
        return
    FR_PRIVATE = 0x10
    for fname in ("HankenGrotesk-Regular.ttf", "HankenGrotesk-SemiBold.ttf", "HankenGrotesk-Bold.ttf"):
        path = resource_path(fname)
        if os.path.exists(path):
            ctypes.windll.gdi32.AddFontResourceExW(path, FR_PRIVATE, 0)


def font_available(family: str) -> bool:
    try:
        return family in tkfont.families()
    except Exception:
        return False


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


class TwoFactorDialog(tk.Toplevel):
    """
    Dialogo propio (con el estilo oscuro de la app) para pedir el codigo de
    verificacion en 2 pasos. Apple solo permite mandar ese codigo a un
    dispositivo de confianza (aviso push) o, si lo pides explicitamente, por
    SMS/llamada a un numero de telefono de confianza — nunca por correo, eso
    no es una opcion que exponga Apple en ningun sitio.

    self.result queda como:
      None                  -> el usuario cancelo
      ("code", "123456")    -> quiere validar ese codigo (llegado por push o SMS)
      ("sms", "a")          -> pide que Apple mande un SMS al telefono "a"
    """

    def __init__(
        self,
        parent: "NanuveICloudDownloader",
        mode: str = "code",
        error_message: str | None = None,
        phone_options: list[tuple[str, str]] | None = None,
    ):
        super().__init__(parent)
        self.result: tuple[str, str] | None = None
        self.title("Verificación en 2 pasos")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        font_family = parent.active_font
        pad = ttk.Frame(self, style="TFrame", padding=24)
        pad.pack(fill="both", expand=True)

        if mode == "sms_code":
            headline = "Apple va a mandarte un SMS con el código. Introdúcelo aquí:"
        else:
            headline = "Apple ha avisado a tus dispositivos de confianza con un código."
        ttk.Label(pad, text=headline, style="TLabel", wraplength=340, justify="left").pack(anchor="w")

        if error_message:
            tk.Label(pad, text=error_message, bg=BG, fg="#ff6b6b",
                     font=(font_family, 9), wraplength=340, justify="left").pack(anchor="w", pady=(6, 0))

        self.code_var = tk.StringVar()
        entry = ttk.Entry(pad, textvariable=self.code_var, style="TEntry",
                           font=(font_family, 16), justify="center")
        entry.pack(fill="x", pady=(16, 10))
        entry.focus_set()

        if mode == "code" and phone_options:
            ttk.Label(
                pad,
                text="¿Prefieres recibirlo por SMS en tu móvil en vez de en tus dispositivos Apple?",
                style="Muted.TLabel", wraplength=340, justify="left",
            ).pack(anchor="w", pady=(6, 6))
            for letter, number in phone_options:
                ttk.Button(
                    pad, text=f"Enviarme un SMS a {number}", style="Ghost.TButton",
                    command=lambda letter=letter: self._request_sms(letter),
                ).pack(fill="x", pady=(0, 6))

        buttons = ttk.Frame(pad, style="TFrame")
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Confirmar código", style="Brand.TButton",
                   command=self._confirm).pack(side="left")
        ttk.Button(buttons, text="Cancelar descarga", style="Ghost.TButton",
                   command=self._cancel).pack(side="left", padx=(10, 0))

        self.bind("<Return>", lambda _e: self._confirm())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        apply_dark_titlebar(self)
        self.wait_window(self)

    def _confirm(self):
        self.result = ("code", self.code_var.get().strip())
        self.destroy()

    def _request_sms(self, letter: str):
        self.result = ("sms", letter)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class NanuveICloudDownloader(tk.Tk):
    def __init__(self):
        super().__init__()
        load_private_fonts()
        self.active_font = FONT_FAMILY if font_available(FONT_FAMILY) else "Segoe UI"

        self.title("Nanuve · iCloud Downloader")
        self.configure(bg=BG)
        self.geometry("740x640")
        self.minsize(660, 580)

        icon_path = resource_path("nanuve.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        self.process: subprocess.Popen | None = None
        self.waiting_for_code = False
        self.user_requested_stop = False
        self._run_id = 0
        self._trusted_phones: list[tuple[str, str]] = []
        self._awaiting_sms_code = False
        self._job: dict = {}
        self._progress_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._progress_total: int | None = None
        self._progress_done = 0
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._log_line_count = 0
        self._log_file = None
        try:
            self._log_file = open(LOG_PATH, "a", encoding="utf-8")
            self._log_file.write(f"\n--- sesión iniciada {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
            self._log_file.flush()
        except Exception:
            self._log_file = None

        self._build_style()
        self._build_ui()
        self._load_saved_values()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_tk_exception

        self.after(50, apply_dark_titlebar, self)
        self.after(80, self._drain_log_queue)
        self.after(120, self._drain_progress_queue)

    def _on_tk_exception(self, exc_type, exc_value, exc_tb):
        import traceback
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        if self._log_file:
            try:
                self._log_file.write(text + "\n")
                self._log_file.flush()
            except Exception:
                pass
        messagebox.showerror("Error inesperado", f"{exc_value}\n\nDetalles en {LOG_PATH}")

    def _on_close(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(
                "Descarga en curso",
                "Hay una descarga en curso. Si cierras ahora se interrumpe "
                "(puedes reanudarla la próxima vez). ¿Cerrar de todas formas?",
            ):
                return
            kill_process_tree(self.process.pid)
            prevent_sleep(False)
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        self.destroy()

    # ---------------------------------------------------------- estilo ttk
    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=BG)
        style.configure("Elev.TFrame", background=BG_ELEV)

        style.configure("TLabel", background=BG, foreground=TEXT, font=(self.active_font, 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(self.active_font, 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=(self.active_font, 17, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(self.active_font, 10))

        style.configure(
            "TEntry",
            fieldbackground=BG_ELEV, background=BG_ELEV, foreground=TEXT,
            bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
            insertcolor=TEXT, selectbackground=BRAND, selectforeground=BRAND_TEXT_ON,
            padding=8,
        )
        style.map("TEntry", bordercolor=[("focus", BRAND)])
        style.map(
            "TEntry",
            fieldbackground=[("disabled", LINE_SOFT)],
            foreground=[("disabled", MUTED)],
        )

        style.configure(
            "Brand.TButton",
            background=BRAND, foreground=BRAND_TEXT_ON,
            font=(self.active_font, 10, "bold"),
            borderwidth=0, focuscolor=BRAND, padding=(18, 10),
        )
        style.map(
            "Brand.TButton",
            background=[("disabled", LINE), ("active", BRAND_QUIET)],
            foreground=[("disabled", MUTED)],
        )

        style.configure(
            "Ghost.TButton",
            background=BG_ELEV, foreground=TEXT,
            font=(self.active_font, 10),
            borderwidth=1, focuscolor=BG_ELEV, padding=(14, 9),
            bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
        )
        style.map(
            "Ghost.TButton",
            background=[("disabled", BG), ("active", LINE_SOFT)],
            foreground=[("disabled", MUTED)],
            bordercolor=[("active", BRAND), ("disabled", LINE)],
        )

        style.configure("TCheckbutton", background=BG, foreground=MUTED, font=(self.active_font, 9))
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", TEXT)])

        style.configure(
            "TProgressbar",
            background=BRAND, troughcolor=BG_ELEV, bordercolor=BG_ELEV,
            lightcolor=BRAND, darkcolor=BRAND,
        )

        style.configure(
            "Vertical.TScrollbar",
            background=BG_ELEV, troughcolor=BG, bordercolor=BG,
            arrowcolor=MUTED, lightcolor=BG_ELEV, darkcolor=BG_ELEV,
        )
        style.map("Vertical.TScrollbar", background=[("active", LINE)])

    # ---------------------------------------------------------- UI
    def _build_ui(self):
        wrap = ttk.Frame(self, style="TFrame", padding=28)
        wrap.pack(fill="both", expand=True)

        header = ttk.Frame(wrap, style="TFrame")
        header.pack(fill="x", pady=(0, 22))

        logo_path = resource_path("nanuve_logo_2x.png")
        if os.path.exists(logo_path):
            try:
                img = tk.PhotoImage(file=logo_path)
                img = img.subsample(2, 2)  # fuente a 2x, mostrada a 1x: se ve nitida sin dentado
                self._logo_img = img
                tk.Label(header, image=img, bg=BG).pack(side="left")
            except Exception:
                pass

        title_box = ttk.Frame(header, style="TFrame")
        title_box.pack(side="left", padx=(16, 0))
        ttk.Label(title_box, text="iCloud Downloader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="Descarga tu fototeca de iCloud a este equipo, organizada por día",
                  style="Sub.TLabel").pack(anchor="w")

        tk.Frame(wrap, bg=LINE, height=1).pack(fill="x", pady=(0, 22))

        form = ttk.Frame(wrap, style="TFrame")
        form.pack(fill="x")

        ttk.Label(form, text="Apple ID").pack(anchor="w")
        self.apple_id_var = tk.StringVar()
        self.apple_id_entry = ttk.Entry(form, textvariable=self.apple_id_var, style="TEntry")
        self.apple_id_entry.pack(fill="x", pady=(4, 16))

        ttk.Label(form, text="Contraseña").pack(anchor="w")
        pass_row = ttk.Frame(form, style="TFrame")
        pass_row.pack(fill="x", pady=(4, 6))
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(pass_row, textvariable=self.password_var, style="TEntry", show="•")
        self.password_entry.pack(side="left", fill="x", expand=True)
        self.show_pass_var = tk.BooleanVar(value=False)
        self.show_pass_check = ttk.Checkbutton(
            pass_row, text="Mostrar", variable=self.show_pass_var,
            command=self._toggle_password_visibility,
        )
        self.show_pass_check.pack(side="left", padx=(10, 0))
        ttk.Label(
            form,
            text="Solo se usa para conectar con iCloud en esta sesión; no queda guardada en ningún archivo.\n"
                 "La sesión (no la contraseña) sí queda guardada para no tener que repetir el login cada vez.",
            style="Muted.TLabel", justify="left",
        ).pack(anchor="w", pady=(0, 16))

        ttk.Label(form, text="Carpeta de destino").pack(anchor="w")
        dest_row = ttk.Frame(form, style="TFrame")
        dest_row.pack(fill="x", pady=(4, 16))
        self.dest_var = tk.StringVar()
        self.dest_entry = ttk.Entry(dest_row, textvariable=self.dest_var, style="TEntry")
        self.dest_entry.pack(side="left", fill="x", expand=True)
        self.browse_btn = ttk.Button(dest_row, text="Examinar...", style="Ghost.TButton",
                                      command=self._browse_folder)
        self.browse_btn.pack(side="left", padx=(10, 0))

        actions = ttk.Frame(wrap, style="TFrame")
        actions.pack(fill="x", pady=(4, 18))
        self.start_btn = ttk.Button(actions, text="Descargar fotos", style="Brand.TButton",
                                     command=self._start_download)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Detener", style="Ghost.TButton",
                                    command=self._stop_download, state="disabled")
        self.stop_btn.pack(side="left", padx=(10, 0))
        self.open_folder_btn = ttk.Button(actions, text="Abrir carpeta", style="Ghost.TButton",
                                           command=self._open_destination_folder, state="disabled")
        self.open_folder_btn.pack(side="left", padx=(10, 0))

        self.progress = ttk.Progressbar(wrap, mode="indeterminate", style="TProgressbar")
        self.progress.pack(fill="x", pady=(0, 4))
        self.progress_label_var = tk.StringVar(value="")
        ttk.Label(wrap, textvariable=self.progress_label_var, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        ttk.Label(wrap, text="Actividad", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        log_frame = tk.Frame(wrap, bg=BG_ELEV, highlightbackground=LINE, highlightthickness=1)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame, bg=BG_ELEV, fg=TEXT, insertbackground=TEXT,
            relief="flat", wrap="word", font=("Consolas", 9),
            padx=12, pady=10, state="disabled",
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview, style="Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(wrap, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

    def _load_saved_values(self):
        cfg = load_config()
        self.apple_id_var.set(cfg.get("apple_id", ""))
        self.dest_var.set(cfg.get("destination", ""))

    def _toggle_password_visibility(self):
        self.password_entry.configure(show="" if self.show_pass_var.get() else "•")

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Elige la carpeta donde guardar tus fotos")
        if folder:
            self.dest_var.set(folder)

    def _open_destination_folder(self):
        destination = self.dest_var.get().strip()
        if destination and os.path.isdir(destination):
            os.startfile(destination)

    def _set_form_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.apple_id_entry.configure(state=state)
        self.password_entry.configure(state=state)
        self.dest_entry.configure(state=state)
        self.browse_btn.configure(state=state)
        self.start_btn.configure(state=state)
        self.stop_btn.configure(state=("disabled" if enabled else "normal"))

    # ---------------------------------------------------------- log helpers
    def _log(self, line: str):
        """Llamado solo desde el hilo principal (via la cola de registro)."""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self._log_line_count += 1
        if self._log_line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", "2.0")
            self._log_line_count -= 1
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if self._log_file:
            try:
                timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                self._log_file.write(f"[{timestamp}] {line}\n")
                self._log_file.flush()
            except Exception:
                pass

    def _queue_log(self, line: str):
        """Llamado desde el hilo de lectura del subproceso: solo encola, no toca la UI."""
        self._log_queue.put(line)

    def _drain_log_queue(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._log(line)
        except queue.Empty:
            pass
        self.after(80, self._drain_log_queue)

    def _drain_progress_queue(self):
        try:
            while True:
                kind, payload = self._progress_queue.get_nowait()
                if kind == "total":
                    total = int(payload)
                    self._progress_total = total
                    self._progress_done = 0
                    if total > 0:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=total, value=0)
                    self._update_progress_label()
                elif kind == "tick" and self._progress_total:
                    self._progress_done = min(self._progress_done + 1, self._progress_total)
                    self.progress.configure(value=self._progress_done)
                    self._update_progress_label()
        except queue.Empty:
            pass
        self.after(120, self._drain_progress_queue)

    def _update_progress_label(self):
        if not self._progress_total:
            self.progress_label_var.set("")
            return
        percent = int(self._progress_done * 100 / self._progress_total)
        label = f"{self._progress_done:,} de {self._progress_total:,} ({percent}%)".replace(",", ".")
        self.progress_label_var.set(label)

    def _reset_progress(self):
        self._progress_total = None
        self._progress_done = 0
        self.progress_label_var.set("")
        self.progress.stop()
        self.progress.configure(mode="indeterminate")

    def _set_status(self, text: str):
        self.status_var.set(text)

    # ---------------------------------------------------------- descarga
    def _prepare_directory(self, path: str) -> str | None:
        """Crea la carpeta si hace falta y comprueba que se puede escribir. Devuelve un mensaje de error o None."""
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".nanuve_write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return None
        except OSError as exc:
            return str(exc)

    def _build_cmd(self, directory: str, album: str | None = None) -> list[str]:
        job = self._job
        cmd = [
            job["icloudpd_path"],
            "--directory", directory,
            "--username", job["apple_id"],
            "--password", job["password"],
            "--password-provider", "parameter",
            "--mfa-provider", "console",
            "--folder-structure", "{:%Y/%m/%d}",
            "--set-exif-datetime",
            "--no-progress-bar",
        ]
        if album:
            cmd += ["--album", album]
        return cmd

    def _start_download(self):
        apple_id = self.apple_id_var.get().strip()
        password = self.password_var.get()
        destination = self.dest_var.get().strip()

        if not apple_id:
            messagebox.showwarning("Falta el Apple ID", "Escribe tu Apple ID antes de continuar.")
            return
        if not password:
            messagebox.showwarning("Falta la contraseña", "Escribe tu contraseña de iCloud antes de continuar.")
            return
        if not destination:
            messagebox.showwarning("Falta la carpeta", "Elige la carpeta donde guardar las fotos.")
            return
        destination = os.path.abspath(destination)

        error = self._prepare_directory(destination)
        if error:
            messagebox.showerror(
                "No se puede escribir en esa carpeta",
                f"No se pudo crear o escribir en:\n{destination}\n\n{error}",
            )
            return

        icloudpd_path = resource_path("icloudpd.exe")
        if not os.path.exists(icloudpd_path):
            messagebox.showerror("Falta icloudpd.exe", "No se encontró el motor de descarga junto a la aplicación.")
            return

        save_config({"apple_id": apple_id, "destination": destination})

        self._job = {
            "apple_id": apple_id,
            "password": password,
            "icloudpd_path": icloudpd_path,
            "destination": destination,
        }
        self._set_form_enabled(False)
        self.open_folder_btn.configure(state="normal")
        self._launch_download()

    def _launch_download(self):
        job = self._job
        directory = job["destination"]
        cmd = self._build_cmd(directory)
        self._set_status("Conectando con iCloud...")
        self._log(f"Iniciando descarga hacia: {directory}")

        self._run_id += 1
        this_run = self._run_id
        self.user_requested_stop = False
        self._trusted_phones = []
        self._awaiting_sms_code = False
        self._reset_progress()
        self.progress.start(12)

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=directory,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError as exc:
            messagebox.showerror(
                "No se pudo iniciar la descarga",
                f"No se pudo lanzar icloudpd.exe (puede que el antivirus lo haya bloqueado):\n{exc}",
            )
            self._finish_all(status="Error al iniciar.")
            return

        prevent_sleep(True)
        thread = threading.Thread(target=self._read_process_output, args=(this_run,), daemon=True)
        thread.start()

    def _finish_all(self, status: str):
        prevent_sleep(False)
        self.progress.stop()
        self._set_form_enabled(True)
        self._set_status(status)
        self.process = None

    def _read_process_output(self, run_id: int):
        """
        Lee el stdout del subproceso caracter a caracter en vez de por lineas.
        El prompt de codigo 2FA de icloudpd no termina en '\\n', asi que un
        `for line in stdout` (que bloquea hasta el siguiente salto de linea)
        se queda colgado sin verlo nunca.
        """
        process = self.process
        assert process is not None
        recent_tail = []
        try:
            stdout = process.stdout
            buf = ""
            while True:
                ch = stdout.read(1)
                if ch == "":
                    break
                if ch == "\n":
                    phone_match = TRUSTED_PHONE_RE.match(buf)
                    if phone_match:
                        self._trusted_phones.append((phone_match.group(1), phone_match.group(2).strip()))

                    total_match = TOTAL_COUNT_RE.match(buf)
                    if total_match and total_match.group(1).isdigit():
                        self._progress_queue.put(("total", int(total_match.group(1))))
                    elif buf.startswith("Downloaded ") or buf.endswith("already exists") or buf.endswith("deduplicated"):
                        self._progress_queue.put(("tick", None))

                    self._queue_log(buf)
                    recent_tail.append(buf)
                    recent_tail = recent_tail[-30:]
                    buf = ""
                    continue
                buf += ch
                if self._check_prompt(buf, run_id):
                    buf = ""

            if buf:
                self._queue_log(buf)
                recent_tail.append(buf)
        except Exception as exc:
            self._queue_log(f"[error interno leyendo la salida: {exc}]")
        finally:
            try:
                return_code = process.wait(timeout=10)
            except Exception:
                return_code = -1
            self.after(0, self._on_process_finished, run_id, return_code, "\n".join(recent_tail))

    def _check_prompt(self, buf: str, run_id: int) -> bool:
        """Detecta el prompt del codigo 2FA (sin salto de linea) en el buffer parcial."""
        stripped = buf.rstrip()
        if not stripped.endswith(":"):
            return False
        lower = stripped.lower()
        if not self.waiting_for_code and "code" in lower and (
            "enter" in lower or "verification" in lower or "codigo" in lower or "código" in lower
        ):
            self._queue_log(buf)
            mode = "sms_code" if self._awaiting_sms_code else "code"
            self.after(0, self._ask_for_2fa_code, run_id, mode)
            return True
        return False

    def _ask_for_2fa_code(self, run_id: int, mode: str = "code", error_message: str | None = None):
        if run_id != self._run_id or self.waiting_for_code:
            return
        self.waiting_for_code = True
        self._set_status("Apple pide el código de verificación en 2 pasos...")
        phone_options = list(self._trusted_phones) if mode == "code" else None
        dialog = TwoFactorDialog(self, mode=mode, error_message=error_message, phone_options=phone_options)
        self.waiting_for_code = False

        if run_id != self._run_id or not self.process:
            return

        if dialog.result is None:
            self._log("Verificación cancelada por el usuario; deteniendo la descarga.")
            self._stop_download()
            return

        kind, value = dialog.result
        if kind == "sms":
            self._awaiting_sms_code = True
            self._trusted_phones = []
            self._log(f"Pidiendo a Apple que mande el código por SMS (opción {value})...")
        else:
            self._awaiting_sms_code = False
        try:
            self.process.stdin.write(value + "\n")
            self.process.stdin.flush()
        except Exception:
            pass

    def _on_process_finished(self, run_id: int, return_code: int, recent_tail: str):
        if run_id != self._run_id:
            return  # una ejecucion anterior (ya detenida/reemplazada) termina tarde: se ignora
        self.process = None

        prevent_sleep(False)
        self.progress.stop()
        self._set_form_enabled(True)
        if self.user_requested_stop:
            self._set_status("Detenido por el usuario.")
        elif return_code == 0:
            if self._progress_total:
                self._progress_done = self._progress_total
                self.progress.configure(value=self._progress_total)
                self._update_progress_label()
            self._set_status("Descarga completa.")
            self._log("Proceso terminado correctamente.")
        else:
            friendly = translate_error(recent_tail)
            if friendly:
                self._set_status(friendly)
                messagebox.showwarning("La descarga se detuvo", friendly)
            else:
                self._set_status("El proceso se detuvo o encontró un error. Revisa el registro de arriba.")
            self._log(f"El proceso terminó con código {return_code}.")
        self.process = None

    def _stop_download(self):
        self.user_requested_stop = True
        if self.process:
            kill_process_tree(self.process.pid)
            self._log("Descarga detenida por el usuario. Puedes volver a lanzarla y continuará donde se quedó.")
        prevent_sleep(False)
        self._set_form_enabled(True)
        self.progress.stop()
        self._set_status("Detenido.")


if __name__ == "__main__":
    set_dpi_awareness()
    app = NanuveICloudDownloader()
    app.mainloop()
