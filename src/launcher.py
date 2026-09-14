#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launcher moderno para YT-Remote con interfaz gráfica.
"""

import sys
import os
import json
import subprocess
import threading
import time
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Optional, Dict, Any
from PIL import Image

# Agregar src al path para imports
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

try:
    from security import SessionManager
except ImportError:
    SessionManager = None

try:
    import pystray
    from pystray import MenuItem as TrayMenuItem
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False


class BotProcess:
    """Maneja el ciclo de vida del proceso del bot."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self, session_data: Optional[Dict[str, Any]] = None) -> bool:
        """Inicia el bot como subprocess usando el runtime python portable.

        En el .exe (frozen) NO se relanza sys.executable (eso abria otra
        ventana del launcher): se usa runtime\\python\\python.exe junto al
        ejecutable, con src\\main.py al lado. La config viaja por variables
        de entorno (nunca se escribe en disco).
        """
        with self._lock:
            if self.process and self.process.poll() is None:
                return True  # Ya está corriendo

            try:
                env = os.environ.copy()
                session_data = session_data or {}
                bot_token = session_data.get("bot_token")
                if bot_token:
                    env["TELEGRAM_TOKEN"] = bot_token
                owner_id = session_data.get("admin_id")
                if owner_id:
                    env["OWNER_ID"] = str(owner_id)
                kick = session_data.get("kick_after_hours")
                if kick is not None:
                    env["KICK_AFTER_HOURS"] = str(kick)
                mpv_path = session_data.get("mpv_path")
                if mpv_path:
                    env["MPV_PATH"] = str(mpv_path)

                if getattr(sys, "frozen", False):
                    # La carpeta que contiene al .exe (portable).
                    base_dir = Path(os.path.dirname(os.path.abspath(sys.executable)))
                    python_exe = base_dir / "runtime" / "python" / "python.exe"
                    bot_script = base_dir / "src" / "main.py"
                else:
                    python_exe = Path(sys.executable)
                    bot_script = SRC_DIR / "main.py"

                self.process = subprocess.Popen(
                    [str(python_exe), str(bot_script)],
                    env=env,
                    cwd=str(python_exe.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                # Pequeña pausa para verificar que arranca bien
                time.sleep(0.5)
                if self.process.poll() is not None:
                    stdout, stderr = self.process.communicate()
                    print(f"Bot falló al iniciar: {stderr.decode()}")
                    return False
                return True
            except Exception as e:
                print(f"Error iniciando bot: {e}")
                return False

    def stop(self) -> bool:
        """Detiene el proceso del bot."""
        with self._lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                    return True
                except Exception as e:
                    print(f"Error deteniendo bot: {e}")
                    return False
            return True

    def is_running(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def get_logs(self, lines: int = 50) -> list[str]:
        """Obtiene las últimas líneas de log del bot."""
        if not self.process:
            return []
        # Nota: en implementación real se usaría un pipe o archivo de log
        return []


class SessionManagerUI:
    """Maneja la sesión unificada (session.enc) desde la UI."""

    def __init__(self):
        self.session_manager = SessionManager() if SessionManager else None
        self.session_data: Optional[Dict[str, Any]] = None

    def load_session(self) -> bool:
        """Carga la sesión existente si existe y es válida."""
        if not self.session_manager:
            return False
        try:
            self.session_data = self.session_manager.load_session()
            return self.session_data is not None
        except Exception:
            return False

    def save_session(self, data: Dict[str, Any]) -> bool:
        """Guarda la sesión completa."""
        if not self.session_manager:
            return False
        try:
            self.session_manager.save_session(data)
            self.session_data = data
            return True
        except Exception as e:
            print(f"Error guardando sesión: {e}")
            return False

    def clear_session(self) -> bool:
        """Borra la sesión (logout)."""
        try:
            if self.session_manager:
                self.session_manager.clear_session()
            self.session_data = None
            return True
        except Exception as e:
            print(f"Error limpiando sesión: {e}")
            return False

    def is_logged_in(self) -> bool:
        return self.session_data is not None

    def get_bot_token(self) -> Optional[str]:
        if self.session_data:
            return self.session_data.get("bot_token")
        return None

    def get_admin_id(self) -> Optional[int]:
        if self.session_data:
            return self.session_data.get("admin_id")
        return None


class LauncherApp:
    """Aplicación principal del launcher."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("YT-Remote")
        self.root.geometry(self._center_geometry("500x600"))
        self.root.minsize(450, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Icono (opcional, se puede agregar .ico después)
        try:
            self.root.iconbitmap(default="")  # Placeholder
        except Exception:
            pass

        # Estado
        self.bot_process = BotProcess()
        self.session_ui = SessionManagerUI()
        self.is_connected = False
        self.minimized_to_tray = False

        # Tray icon (se crea DESPUÉS de que la ventana ya se mostró)
        self.tray_icon = None
        self.tray_icon_image = None

        # Inicializar UI
        self.setup_styles()
        self.build_ui()

        # Verificar sesión existente al iniciar
        self.check_existing_session()

        # Iniciar tray en segundo plano, una vez la ventana ya está visible
        self.root.after(800, self._start_tray_lazy)

    def _center_geometry(self, size: str) -> str:
        """Devuelve la geometria WxH+X+Y centrada en la pantalla."""
        w, h = (int(v) for v in size.split("x"))
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        return f"{w}x{h}+{max(0, x)}+{max(0, y)}"

    def _start_tray_lazy(self):
        """Crea y arranca el tray icon después de que la ventana sea visible."""
        if not HAS_TRAY or self.tray_icon:
            return
        self._create_tray_icon()
        if self.tray_icon:
            threading.Thread(target=self.run_tray, daemon=True).start()

    def _create_tray_icon(self):
        """Crea el icono del system tray."""
        if not HAS_TRAY:
            return
        try:
            # Crear imagen simple para el icono
            image = Image.new('RGBA', (64, 64), (26, 26, 46, 255))
            # Dibujar un icono simple (play button)
            from PIL import ImageDraw
            draw = ImageDraw.Draw(image)
            draw.polygon([(20, 16), (20, 48), (48, 32)], fill=(39, 174, 96, 255))
            self.tray_icon_image = image

            # Crear menú del tray
            menu = pystray.Menu(
                TrayMenuItem("YT-Remote", None, enabled=False),
                TrayMenuItem("Mostrar", self.show_window, default=True),
                TrayMenuItem("Detener bot", self.stop_bot_from_tray),
                TrayMenuItem("Salir", self.quit_app_from_tray),
            )

            self.tray_icon = pystray.Icon(
                "ytremote",
                image,
                "YT-Remote",
                menu
            )
        except Exception as e:
            print(f"No se pudo crear tray icon: {e}")

    def show_window(self):
        """Muestra la ventana principal."""
        self.root.after(0, self._show_window)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.minimized_to_tray = False
        # Volver a la pantalla principal según estado (evita reabrir en pantallas secundarias)
        self.back_to_main()

    def stop_bot_from_tray(self):
        """Detiene el bot desde el tray."""
        if self.is_connected:
            self.root.after(0, self.stop_bot)

    def quit_app_from_tray(self):
        """Cierra la aplicación desde el tray."""
        self.root.after(0, self.quit_app)

    def run_tray(self):
        """Ejecuta el loop del tray icon en hilo separado."""
        if self.tray_icon:
            self.tray_icon.run()

    def setup_styles(self):
        """Configura estilos visuales."""
        style = ttk.Style()
        style.theme_use("clam")

        # Colores
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"), foreground="#1a1a2e")
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground="#666666")
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Green.TLabel", foreground="#27ae60")
        style.configure("Red.TLabel", foreground="#e74c3c")
        style.configure("Yellow.TLabel", foreground="#f39c12")
        style.configure("BigButton.TButton", font=("Segoe UI", 12, "bold"), padding=15)
        style.configure("SmallButton.TButton", font=("Segoe UI", 10), padding=8)
        style.configure("Tray.TButton", font=("Segoe UI", 9), padding=5)

    def build_ui(self):
        """Construye la interfaz principal."""
        # Frame principal con padding
        main_frame = ttk.Frame(self.root, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Header
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 20))

        # Logo / Título
        title_label = ttk.Label(header_frame, text="YT-Remote", style="Title.TLabel")
        title_label.pack()

        subtitle_label = ttk.Label(
            header_frame,
            text="Control remoto de YouTube para Telegram",
            style="Subtitle.TLabel"
        )
        subtitle_label.pack(pady=(5, 0))

        # Separador
        separator = ttk.Separator(main_frame, orient="horizontal")
        separator.pack(fill=tk.X, pady=15)

        # Área de contenido principal (cambia según estado)
        self.content_frame = ttk.Frame(main_frame)
        self.content_frame.pack(fill=tk.BOTH, expand=True)

        # Barra de estado inferior
        self.status_frame = ttk.Frame(main_frame)
        self.status_frame.pack(fill=tk.X, pady=(15, 0))

        self.status_label = ttk.Label(self.status_frame, text="Desconectado", style="Red.TLabel")
        self.status_label.pack(side=tk.LEFT)

        # Botones de acción inferior
        self.action_frame = ttk.Frame(main_frame)
        self.action_frame.pack(fill=tk.X, pady=(10, 0))

    def show_connect_screen(self):
        """Pantalla de conexión inicial (sin sesión guardada): pedir token y admin."""
        self.show_config_screen(conectar=True)

    def show_connected_screen(self, user_name: str = "Usuario"):
        """Muestra la pantalla de conectado."""
        self.clear_content()

        conn_frame = ttk.Frame(self.content_frame)
        conn_frame.pack(fill=tk.BOTH, expand=True)

        # Estado conectado
        status_label = ttk.Label(conn_frame, text="✅ Conectado", style="Green.TLabel", font=("Segoe UI", 16, "bold"))
        status_label.pack(pady=(0, 5))

        user_label = ttk.Label(conn_frame, text=f"Conectado como {user_name}", style="Subtitle.TLabel")
        user_label.pack(pady=(0, 20))

        # Separador
        ttk.Separator(conn_frame, orient="horizontal").pack(fill=tk.X, pady=15)

        # Botones de acción
        btn_frame = ttk.Frame(conn_frame)
        btn_frame.pack()

        stop_btn = ttk.Button(
            btn_frame,
            text="⏹ Detener bot",
            style="BigButton.TButton",
            command=self.stop_bot
        )
        stop_btn.pack(pady=5, fill=tk.X, padx=50)

        logout_btn = ttk.Button(
            btn_frame,
            text="🔒 Cerrar sesión",
            style="BigButton.TButton",
            command=self.logout
        )
        logout_btn.pack(pady=5, fill=tk.X, padx=50)

        config_btn = ttk.Button(
            btn_frame,
            text="⚙️ Configuración",
            style="BigButton.TButton",
            command=self.show_config
        )
        config_btn.pack(pady=5, fill=tk.X, padx=50)

        exit_btn = ttk.Button(
            btn_frame,
            text="❌ Salir",
            style="BigButton.TButton",
            command=self.on_close
        )
        exit_btn.pack(pady=5, fill=tk.X, padx=50)

        # Info adicional
        info_frame = ttk.Frame(conn_frame)
        info_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=20)
        info_label = ttk.Label(
            info_frame,
            text="El bot está corriendo en segundo plano.\nSe ha minimizado al lado del reloj.",
            style="Subtitle.TLabel",
            justify=tk.CENTER
        )
        info_label.pack()

    def show_config_screen(self, conectar: bool = False):
        """Muestra la pantalla de configuración o conexión inicial.

        conectar=True: primer arranque sin sesión; el boto principal
        guarda la sesión y conecta (botón "Conectar").
        conectar=False: ajustes desde la pantalla conectada.
        """
        self.clear_content()

        config_frame = ttk.Frame(self.content_frame)
        config_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        title_text = "Conectar YT-Remote" if conectar else "Configuración"
        title = ttk.Label(config_frame, text=title_text, style="Title.TLabel")
        title.pack(pady=(0, 20))

        if conectar:
            hint = ttk.Label(
                config_frame,
                text="Ingrese el token del bot y su ID de administrador de Telegram\npara conectar el bot. No hay sesión guardada todavía.",
                style="Subtitle.TLabel",
                justify=tk.CENTER,
            )
            hint.pack(pady=(0, 10))

        # Formulario
        form_frame = ttk.Frame(config_frame)
        form_frame.pack(fill=tk.X, pady=10)

        ttk.Label(form_frame, text="Token del bot:", style="Subtitle.TLabel").pack(anchor=tk.W, pady=(10, 2))
        token_row = ttk.Frame(form_frame)
        token_row.pack(fill=tk.X, pady=(0, 10))
        self.token_var = tk.StringVar(value=self.session_ui.session_data.get("bot_token", "") if self.session_ui.session_data else "")
        self.token_entry = ttk.Entry(token_row, textvariable=self.token_var, width=50, show="*")
        self.token_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._token_visible = tk.BooleanVar(value=False)
        ttk.Button(
            token_row,
            text="👁",
            width=3,
            style="SmallButton.TButton",
            command=lambda: self._toggle_secret(self.token_entry, self._token_visible),
        ).pack(side=tk.RIGHT, padx=(6, 0))

        ttk.Label(form_frame, text="Su ID de Telegram (admin):", style="Subtitle.TLabel").pack(anchor=tk.W, pady=(10, 2))
        admin_row = ttk.Frame(form_frame)
        admin_row.pack(fill=tk.X, pady=(0, 10))
        self.admin_id_var = tk.StringVar(value=str(self.session_ui.session_data.get("admin_id", "")) if self.session_ui.session_data else "")
        self.admin_entry = ttk.Entry(admin_row, textvariable=self.admin_id_var, width=50, show="*")
        self.admin_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._admin_visible = tk.BooleanVar(value=False)
        ttk.Button(
            admin_row,
            text="👁",
            width=3,
            style="SmallButton.TButton",
            command=lambda: self._toggle_secret(self.admin_entry, self._admin_visible),
        ).pack(side=tk.RIGHT, padx=(6, 0))

        # kick_after_hours
        ttk.Label(form_frame, text="Expulsar usuarios tras horas (0 = off):", style="Subtitle.TLabel").pack(anchor=tk.W, pady=(10, 2))
        self.kick_var = tk.StringVar(value=str(self.session_ui.session_data.get("kick_after_hours", 0)) if self.session_ui.session_data else "0")
        kick_entry = ttk.Entry(form_frame, textvariable=self.kick_var, width=50)
        kick_entry.pack(fill=tk.X, pady=(0, 10))

        # Ruta MPV: valor fijo, no se muestra ni se modifica en la UI.
        self.mpv_var = tk.StringVar(value="runtime\\mpv\\mpv.exe")

        # Botones
        btn_frame = ttk.Frame(config_frame)
        btn_frame.pack(fill=tk.X, pady=10)

        save_text = "🔑 Conectar" if conectar else "💾 Guardar"
        back_text = "❌ Salir" if conectar else "⬅ Volver"
        save_btn = ttk.Button(
            btn_frame,
            text=save_text,
            style="BigButton.TButton",
            command=self.save_config
        )
        save_btn.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        back_btn = ttk.Button(
            btn_frame,
            text=back_text,
            style="BigButton.TButton",
            command=self.quit_app if conectar else self.back_to_main
        )
        back_btn.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

    def _toggle_secret(self, entry, visible_var):
        """Alterna entre mostrar y ocultar un campo secreto del formulario."""
        visible_var.set(not visible_var.get())
        entry.configure(show="" if visible_var.get() else "*")

    def show_logs_screen(self):
        """Muestra logs del bot."""
        self.clear_content()

        logs_frame = ttk.Frame(self.content_frame)
        logs_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        title = ttk.Label(logs_frame, text="Logs del bot", style="Title.TLabel")
        title.pack(pady=(0, 10))

        # Área de logs con scroll
        log_frame = ttk.Frame(logs_frame)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.logs_text = tk.Text(log_frame, wrap=tk.WORD, height=20, font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.logs_text.yview)
        self.logs_text.configure(yscrollcommand=scrollbar.set)

        self.logs_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Cargar logs
        self.refresh_logs()

        # Botones
        btn_frame = ttk.Frame(logs_frame)
        btn_frame.pack(fill=tk.X, pady=10)

        refresh_btn = ttk.Button(btn_frame, text="🔄 Actualizar", command=self.refresh_logs)
        refresh_btn.pack(side=tk.LEFT, padx=5)

        back_btn = ttk.Button(btn_frame, text="⬅ Volver", command=self.back_to_main)
        back_btn.pack(side=tk.LEFT, padx=5)

    def clear_content(self):
        """Limpia el área de contenido."""
        for widget in self.content_frame.winfo_children():
            widget.destroy()

    def start_bot(self):
        """Inicia el proceso del bot con la config de la sesión."""
        if self.bot_process.start(self.session_ui.session_data):
            self.update_status("Conectado", "green")
        else:
            self.update_status("Error al iniciar bot", "red")

    def stop_bot(self):
        """Detiene el bot."""
        if self.bot_process.stop():
            self.is_connected = False
            self.update_status("Desconectado", "red")
            self.back_to_main()

    def logout(self):
        """Cierra la sesión: borra session.enc, detiene el bot y vuelve a la conexión inicial."""
        if self.is_connected:
            self.bot_process.stop()
        self.session_ui.clear_session()  # Borra session.enc (logout real)
        self.is_connected = False
        self.update_status("Sesión cerrada", "red")
        self.show_connect_screen()

    def save_config(self):
        """Guarda la configuración. Desde la pantalla de conexión inicial
        además inicia el bot y pasa a la pantalla de conectado. Si el bot ya
        corre (edición desde pantalla conectada), se reinicia con la nueva config."""
        try:
            token = self.token_var.get().strip()
            admin_id_str = self.admin_id_var.get().strip()
            if not token or not admin_id_str.isdigit() or int(admin_id_str) <= 0:
                messagebox.showerror("Error", "Ingrese el token del bot y su ID de Telegram (admin).")
                return
            config_data = {
                "bot_token": token,
                "admin_id": int(admin_id_str),
                "kick_after_hours": int(self.kick_var.get()) if self.kick_var.get().isdigit() else 0,
                "mpv_path": self.mpv_var.get(),
            }
            if not self.session_ui.save_session(config_data):
                messagebox.showerror("Error", "No se pudo guardar la configuración")
                return

            # Si el bot ya corre (nueva config desde pantalla conectada), reiniciarlo
            if self.bot_process.is_running():
                self.bot_process.stop()

            if self.bot_process.start(self.session_ui.session_data):
                self.is_connected = True
                self.show_connected_screen()
                self.minimize_to_tray()
            else:
                self.is_connected = False
                messagebox.showerror("Error", "No se pudo iniciar el bot. Revise el token y el ID e intente de nuevo.")
                self.back_to_main()
        except Exception as e:
            messagebox.showerror("Error", f"Error guardando: {e}")

    def back_to_main(self):
        """Vuelve a la pantalla principal según estado."""
        if self.is_connected:
            self.show_connected_screen()
        else:
            self.show_connect_screen()

    def show_config(self):
        """Alias para compatibilidad."""
        self.show_config_screen()

    def refresh_logs(self):
        """Actualiza el área de logs."""
        if hasattr(self, 'logs_text'):
            self.logs_text.delete(1.0, tk.END)
            logs = self.bot_process.get_logs()
            for log in logs:
                self.logs_text.insert(tk.END, log + "\n")
            self.logs_text.see(tk.END)

    def update_status(self, text: str, color: str):
        """Actualiza la etiqueta de estado."""
        style_map = {"green": "Green.TLabel", "red": "Red.TLabel", "yellow": "Yellow.TLabel"}
        self.status_label.config(text=text, style=style_map.get(color, "Status.TLabel"))

    def check_existing_session(self):
        """Verifica si hay una sesión válida al iniciar."""
        if self.session_ui.load_session():
            self.is_connected = True
            self.show_connected_screen()
            self.start_bot()
        else:
            self.show_connect_screen()

    def on_close(self):
        """Maneja cierre de ventana."""
        if self.is_connected:
            # Preguntar si quiere minimizar o cerrar
            if messagebox.askyesno(
                "Cerrar",
                "¿Minimizar al lado del reloj? (El bot seguirá corriendo)\n\nNo = Detener bot y salir"
            ):
                self.minimize_to_tray()
            else:
                self.quit_app()
        else:
            self.quit_app()

    def minimize_to_tray(self):
        """Minimiza la ventana al área de notificaciones (system tray)."""
        self.root.withdraw()
        self.minimized_to_tray = True

    def quit_app(self):
        """Cierra completamente la aplicación."""
        if self.is_connected:
            self.session_ui.clear_session()  # Borra session.enc (logout real)
            self.bot_process.stop()
        # Detener tray icon
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()


def _acquire_singleton_mutex() -> bool:
    """Regla de instancia unica: solo UNA ventana del launcher por usuario.

    Usa un mutex con nombre de Windows (ctypes): si el mutex ya existe,
    otra copia del launcher esta abierta. Retorna True si esta instancia se
    queda con el mutex (primera apertura); False si ya hay otra.
    """
    if sys.platform != "win32":
        return True
    global _SINGLETON_MUTEX_HANDLE
    try:
        ERROR_ALREADY_EXISTS = 183
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ytremote_launcher")
        if not mutex:
            return True  # No se pudo crear (no bloqueamos el arranque)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(mutex)
            return False
        # Guardamos el handle en el modulo para que viva mientras corra la app
        _SINGLETON_MUTEX_HANDLE = mutex
        return True
    except Exception:
        return True


def main():
    """Punto de entrada principal."""
    # Regla de instancia unica: si ya hay un launcher abierto, avisar y salir.
    if not _acquire_singleton_mutex():
        try:
            messagebox.showwarning(
                "YT-Remote ya está abierto",
                "La ventana del launcher ya está abierta.\n"
                "Mire el ícono del programa en la zona del reloj "
                "(abajo a la derecha) y haga clic en 'Mostrar'.",
            )
        except Exception:
            pass
        sys.exit(0)

    # Verificar dependencias críticas
    try:
        import tkinter
        import pystray
        from PIL import Image
    except ImportError as e:
        print(f"Dependencia faltante: {e}")
        print("Instalar: pip install pystray pillow")
        sys.exit(1)

    # Configurar DPI awareness en Windows
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    app = LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()