"""Utilidades para la primera configuracion desde ytremote.bat.

Se ejecuta con el Python embebido de la carpeta (runtime\\python). No
importa dependencias externas. Maneja el archivo .env de forma simple:

- is_configured(): True si .env ya tiene TELEGRAM_TOKEN y OWNER_ID validos.
- write_env(token, owner_id): crea/sobreescribe el .env con esos datos.
- get_allowed_chat_id() / set_allowed_chat_id(): para que el bot guarde el
  ID del grupo permitido de forma automatica en el primer /start.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def _read() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _write(values: dict[str, str]) -> None:
    ORDER = ["TELEGRAM_TOKEN", "OWNER_ID", "ALLOWED_CHAT_ID"]
    lines: list[str] = []
    for key in ORDER:
        value = values.get(key, "")
        if value is not None and str(value).strip() != "":
            lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_configured() -> bool:
    env = _read()
    token = env.get("TELEGRAM_TOKEN", "")
    owner = env.get("OWNER_ID", "")
    return bool(token and token not in ("TU_TOKEN_AQUI",)) and owner.strip() != ""


def write_env(token: str, owner_id: str) -> None:
    env = _read()
    token = token.strip()
    owner_id = owner_id.strip()
    if not token or token == "TU_TOKEN_AQUI":
        raise ValueError("El token no puede estar vacío.")
    if not owner_id:
        raise ValueError("El ID de dueño no puede estar vacío.")
    env["TELEGRAM_TOKEN"] = token
    env["OWNER_ID"] = owner_id
    _write(env)


def get_allowed_chat_id() -> str:
    return _read().get("ALLOWED_CHAT_ID", "")


def set_allowed_chat_id(chat_id: int | str | None) -> None:
    if chat_id is None:
        return
    env = _read()
    env["ALLOWED_CHAT_ID"] = str(chat_id).strip()
    _write(env)


def launch_wizard() -> int:
    """Guía interactiva de primera configuración con caracteres acentuados.

    Este texto vive en Python (y no en un .bat) porque la consola de
    Windows no muestra bien los acentos y las ñ en archivos de lote. Una
    vez configurado, arranca el bot de forma bloqueante.
    """
    import subprocess
    import sys

    runtime = PROJECT_ROOT / "runtime"
    py_exe = runtime / "python" / "python.exe"
    mpv_exe = runtime / "mpv" / "mpv.exe"
    main_py = PROJECT_ROOT / "src" / "main.py"

    if not py_exe.exists() or not mpv_exe.exists():
        print()
        print("=" * 50)
        print("  YT-Remote")
        print("=" * 50)
        print()
        print("Algo no está bien instalado en esta carpeta.")
        print("Vuelve a descargar la versión completa desde el repositorio")
        print("y descomprímela de nuevo. La guía está en GUIA.txt")
        print()
        input("Pulsa Enter para cerrar...")
        return 1

    if not is_configured():
        while True:
            print()
            print("=" * 50)
            print("  YT-Remote - Primera configuración")
            print("=" * 50)
            print()
            print("Faltan tus datos. Solo se hace esto una vez.")
            print()
            print("El TOKEN es la llave de tu bot. Para conseguirlo:")
            print("  1. Abre Telegram y busca a @BotFather")
            print("  2. Escríbele  /newbot   y sigue los pasos")
            print("  3. Te dará un código tipo:  123456:AAHxh...")
            print("     Ese es tu TOKEN. Pega el código completo aquí.")
            print()
            token = input("  Pega tu TOKEN aquí: ").strip()
            if not token or token == "TU_TOKEN_AQUI":
                print()
                print("[Error] El TOKEN no puede estar vacío. Vuelve a intentar.")
                print()
                continue

            print()
            print("Tu ID numérico de Telegram (para que te reconozca como dueño):")
            print("  1. Abre Telegram y busca a @userinfobot")
            print("  2. Escríbele cualquier mensaje (por ejemplo: hola)")
            print("  3. Te dirá tu ID, tipo:  123456789")
            print("     Ese es tu ID. Escríbelo aquí.")
            print()
            owner = input("  Escribe tu ID aquí: ").strip()
            if not owner:
                print()
                print("[Error] El ID no puede estar vacío. Vuelve a intentar.")
                print()
                continue

            try:
                write_env(token, owner)
            except (ValueError, OSError):
                print()
                print("[Error] No se pudieron guardar los datos. Revisa la guía GUIA.txt")
                print("y verifica que la carpeta no tenga permisos de solo lectura.")
                print()
                input("Pulsa Enter para cerrar...")
                return 1
            print()
            print("¡Listo! Tus datos quedaron guardados.")
            print("Arrancando el bot...")
            print()
            break

    print("Iniciando YT-Remote...")
    rc = subprocess.call([str(py_exe), str(main_py)])
    print()
    print("El bot se detuvo.")
    return rc


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada invocado desde ytremote.bat.

    Uso:  python setup_cli.py <comando> [args]
      - is-configured         imprime SI o NO segun .env este configurado.
      - save <token> <owner>  guarda token y dueno en .env.
      - set-chat <id>         guarda el chat permitido.
      - launch                guia interactiva con acentos y luego arranca el bot.
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    cmd = args[0].lower()
    if cmd == "is-configured":
        print("SI" if is_configured() else "NO")
    elif cmd == "save":
        try:
            write_env(args[1], args[2])
        except (IndexError, ValueError):
            return 1
    elif cmd == "set-chat":
        set_allowed_chat_id(args[1] if len(args) > 1 else None)
    elif cmd == "launch":
        return launch_wizard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

