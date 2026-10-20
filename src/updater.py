"""Ayudante de /actualizar: reemplaza la instalacion de YT-Remote y la reabre.

Corre APARTE del bot (con runtime\\python\\python.exe) por una razon dura:
el programa que esta funcionando NO puede pisar su propio exe. El bot baja
el ZIP del release a data\\update\\, deja este ayudante corriendo, marca
salir_total.flag y se apaga; el ayudante:

1. Espera a que terminen el bot (--pid) y el launcher (--ppid).
2. Con --zip: descomprime el release ENCIMA de la instalacion (--dest),
   conservando SIEMPRE data\\ y el config.json del usuario, y de paso
   cualquier prefijo de --skip (piezas que quedan ms nuevas instaladas).
3. Borra los restos .viejo, deja el log en --log y vuelve a abrir
   ytremote.exe (--exe).

Sin --zip solo reinicia: espera el cierre y reabre. El log (data\\
actualizacion.log) es la verdad de que paso; el ZIP solo se borra cuando
la instalacion quedo verificada.
"""

from __future__ import annotations

import argparse
import ctypes
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# Lo que SIEMPRE se conserva aunque el release lo traiga: data/ es el
# mundo del usuario (sesion, base de datos, historial, temporizador...);
# config.json es la configuracion que ya tiene la instalacion. Todo lo
# demas (exe, _internal, runtime, src, docs) es PROGRAMA y se reemplaza.
PROTEGIDOS_FILES = ("config.json",)
PROTEGIDOS_DIR = "data"

# Piezas sin las que el ZIP NO es el paquete de YT-Remote de verdad.
CLAVES_PAQUETE = ("ytremote.exe", "runtime/python/python.exe", "src/main.py")


class Log:
    """Lineas con fecha y hora, al log del ayudante (y de yapa a stdout).

    Cada linea se abre y cierra al toque: si la PC se muere a mitad, el
    log tiene hasta la ultima linea escrita.
    """

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def __call__(self, mensaje: str) -> None:
        linea = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {mensaje}"
        print(linea)
        try:
            with self.ruta.open("a", encoding="utf-8") as f:
                f.write(linea + "\n")
        except OSError:
            pass


def detectar_prefijo(nombres: list[str]) -> str:
    """Si las entradas del ZIP viven todas dentro de UNA carpeta
    contenedora (como "yt-remote-1.2.0/"), ese nombre es el prefijo que
    se descuenta; si no, el paquete va sin carpeta y el prefijo es "".
    """
    segmentos: set[str] = set()
    for nombre in nombres:
        limpio = nombre.replace("\\", "/").strip("/")
        if not limpio or limpio.startswith("__MACOSX"):
            continue
        segmentos.add(limpio.split("/", 1)[0] + "/")
    if len(segmentos) == 1:
        unico = next(iter(segmentos))
        if re.match(r"^yt-remote-[0-9][^/]*$", unico[:-1]):
            return unico
    return ""


def es_paquete(nombres: list[str], prefijo: str) -> bool:
    """El ZIP tiene que traer las piezas clave del portable. Un ZIP que no
    las tenga no es YT-Remote (o es una build rota): NUNCA pase a pisar
    la instalacion."""
    presentes = set(nombres)
    return all(prefijo + clave in presentes for clave in CLAVES_PAQUETE)


def entradas_a_extraer(
    nombres: list[str], saltos: list[str]
) -> list[tuple[str, str]]:
    """(member_raw, destino_relativo) del ZIP que SI se descomprime.

    Se salta: entradas de carpeta (solo archivos), data/** (el mundo del
    usuario), config.json, y los prefijos de --skip (piezas que la
    instalacion ya tiene ms nuevas que el release). El destino va sin
    prefijo; rutas raras (absolutas, '..' o letra de unidad) quedan
    fuera: nada del ZIP sale fuera de la carpeta destino.
    """
    prefijo = detectar_prefijo(nombres)
    plan: list[tuple[str, str]] = []
    for nombre in nombres:
        limpio = nombre.replace("\\", "/")
        if limpio.endswith("/") or limpio.startswith("__MACOSX"):
            continue
        destino = limpio[len(prefijo):] if prefijo and limpio.startswith(prefijo) else limpio
        partes = [p for p in destino.split("/") if p not in ("", ".")]
        if not partes or ".." in partes:
            continue
        if re.match(r"^[A-Za-z]:", partes[0]) or destino.startswith("/"):
            continue
        destino = "/".join(partes)
        if destino in PROTEGIDOS_FILES:
            continue
        if destino == PROTEGIDOS_DIR or destino.startswith(PROTEGIDOS_DIR + "/"):
            continue
        if destino in saltos or any(
            destino == s or destino.startswith(s + "/") for s in saltos
        ):
            continue
        plan.append((nombre, destino))
    return plan


def esperar_proceso(pid: int, tope_s: float, log: Log, quien: str) -> bool:
    """Espera (sobre WaitForSingleObject) a que el proceso del PID
    termine. Si OpenProcess no lo encuentra, ya murio. Devuelve False si
    no termino en el tope: el flujo entonces aborta SIN tocar la
    instalacion (log con ABORTADO).
    """
    sincronizar = 0x00100000
    manejador = ctypes.windll.kernel32.OpenProcess(sincronizar, False, int(pid))
    if not manejador:
        log(f"{quien} (pid {pid}) ya no esta en ejecucion.")
        return True
    try:
        tope = time.monotonic() + tope_s
        # WAIT_OBJECT_0 = 0 (senal: murio); WAIT_TIMEOUT = 0x102 (vivo).
        while ctypes.windll.kernel32.WaitForSingleObject(
            manejador, 1000
        ) == 0x102:
            if time.monotonic() > tope:
                log(
                    f"ABORTADO: {quien} (pid {pid}) sigue vivo tras "
                    f"{int(tope_s)} s; la instalacion no se toca."
                )
                return False
        return True
    finally:
        ctypes.windll.kernel32.CloseHandle(manejador)


def extraer_zip(ruta_zip: Path, dest: Path, saltos: list[str], log: Log) -> None:
    """El reemplazo EN SITIO conservando lo protegido (lanza en el error):

    1. Renombra las carpetas del programa a <top>.viejo (rename al
       instante, mismo disco, cero copiado).
    2. Descomprime el plan encima (las .viejo ya no dan conflicto).
    3. Verifica las piezas clave EN DISCO.
    4. Con exito: borra los .viejo. Con fallo: vuelve a poner las .viejo
       en su lugar (la instacion queda como estaba, con su version
       anterior) y deja el ZIParriba para el proximo intento.
    """

    def restaurar(tops: dict[str, str], motivo: str) -> None:
        for top, viejo in tops.items():
            destino_dir = dest / top
            viejo_ruta = dest / viejo
            if destino_dir.exists():
                shutil.rmtree(destino_dir, ignore_errors=True)
            if viejo_ruta.exists():
                try:
                    viejo_ruta.rename(destino_dir)
                    log(f"Restaurado: {top} ({motivo})")
                except OSError as exc:
                    log(f"NO se pudo restaurar {top}: {exc}")

    tops: dict[str, str] = {}
    try:
        with zipfile.ZipFile(ruta_zip) as zf:
            nombres = [n.replace("\\", "/") for n in zf.namelist()]
            prefijo = detectar_prefijo(nombres)
            if not es_paquete(nombres, prefijo):
                raise RuntimeError("el ZIP no trae las piezas clave del paquete")
            plan = entradas_a_extraer(nombres, saltos)
            if not plan:
                raise RuntimeError("el plan de descompresion quedo vacio")

            for member, destino in plan:
                if "/" not in destino:
                    continue
                top = destino.split("/", 1)[0]
                if top in tops or not (dest / top).exists():
                    continue
                viejo = top + ".viejo"
                (dest / top).rename(dest / viejo)
                tops[top] = viejo

            for member, destino in plan:
                destino_f = dest / destino
                destino_f.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as f, destino_f.open("wb") as g:
                    shutil.copyfileobj(f, g)

        faltan = [c for c in CLAVES_PAQUETE if not (dest / c).is_file()]
        if faltan:
            raise RuntimeError(f"la instalacion quedo sin piezas clave: {faltan}")
        # Los --skip viven DENTRO de carpetas renombradas (.viejo): se
        # copian de ahi a la instalacion nueva ANTES de borrar los restos.
        for salto in saltos:
            partes = [p for p in salto.split("/") if p]
            if not partes:
                continue
            viejo_top = tops.get(partes[0])
            if not viejo_top:
                continue
            origen = dest / viejo_top / "/".join(partes[1:])
            destino_s = dest / salto
            if origen.is_dir():
                shutil.copytree(origen, destino_s, dirs_exist_ok=True)
                log(f"--skip aplicado: {salto} conservado de la instalacion anterior.")
            elif origen.is_file():
                destino_s.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origen, destino_s)
                log(f"--skip aplicado: {salto} conservado de la instalacion anterior.")
        # Limpieza: cada carpeta .viejo que quedo del paso 1 se va.
        for top, viejo in tops.items():
            shutil.rmtree(dest / viejo, ignore_errors=True)
        log("Reemplazo hecho; carpetas viejas eliminadas.")
    except Exception as exc:
        restaurar(tops, "fallo del reemplazo")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ayudante de /actualizar de YT-Remote."
    )
    parser.add_argument("--dest", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--ppid", type=int, required=True)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--zip", default=None)
    parser.add_argument("--skip", action="append", default=[])
    args = parser.parse_args(argv)

    dest = Path(args.dest)
    log = Log(Path(args.log))
    log(
        "Ayudante arrancado: "
        + " ".join(
            [
                f"pid={args.pid}",
                f"ppid={args.ppid}",
                f"zip={args.zip or '(solo reinicio)'}",
                f"skip={','.join(args.skip) or '-'}",
            ]
        )
    )

    if args.pid == args.ppid:
        log("ABORTADO: pid y ppid son el mismo proceso; alguien invoco mal.")
        return 1
    if not esperar_proceso(args.pid, 90.0, log, "bot"):
        return 1
    if not esperar_proceso(args.ppid, 120.0, log, "launcher"):
        return 1
    time.sleep(3.0)  # Un programa cortesia para Windows: deja soltar locks.

    try:
        if args.zip:
            zip_ruta = Path(args.zip)
            if not zip_ruta.is_file():
                log(f"ABORTADO: el ZIP indicado no existe ({zip_ruta}).")
                return 1
            try:
                extraer_zip(zip_ruta, dest, args.skip, log)
            except Exception as exc:  # noqa: BLE001
                log(f"FALLO al reemplazar: {exc}")
                return 1
            parcial = zip_ruta.with_suffix(zip_ruta.suffix + ".parcial")
            if parcial.is_file():
                try:
                    parcial.unlink()
                except OSError as exc:
                    log(f"El .parcial no se pudo borrar: {exc}")
            try:
                zip_ruta.unlink(missing_ok=True)
                log("ZIP aplicado y borrado.")
            except OSError as exc:
                log(f"El ZIP quedo sin borrar (revisarlo a mano): {exc}")

        for bandera in ("salir_total.flag", "salir_total.flag.tmp"):
            marca = dest / "data" / bandera
            if marca.is_file():
                try:
                    marca.unlink()
                    log(f"Marca remanente borrada: data/{bandera}")
                except OSError as exc:
                    log(f"No se pudo borrar la marca {bandera}: {exc}")

        exe = Path(args.exe)
        if not exe.is_file():
            log(f"ABORTADO: no esta el exe para reabrir ({exe}).")
            return 1
        subprocess.Popen(
            [str(exe)],
            cwd=str(dest),
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        log("REINICIO: ytremote.exe lanzado. Fin OK.")
        return 0
    except Exception as exc:  # noqa: BLE001 - el log es lo que queda
        log(f"FALLO del ayudante: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
