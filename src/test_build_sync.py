"""El exe construido debe llevar el codigo actual del arbol de trabajo.

El bot no corre del _internal de PyInstaller sino de dist/ytremote/src/main.py,
y el helper post-build restaura ese src/ desde el ZIP con sobreescritura: si
eso ocurre, el exe queda con codigo viejo y ninguna otra prueba lo detecta.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from testkit import run_sync_tests  # noqa: E402

DIST_SRC = SRC.parent / "dist" / "ytremote" / "src"

# La lista de modulos se DERIVA del arbol, no se escribe a mano. Escribida a
# mano se quedo vieja dos veces y ninguna prueba lo dijo: `store.py` (el
# almacenamiento) y `card.py` (la tarjeta) quedaron fuera del chequeo mientras
# el exe los llevaba —o no— sin que nadie se enterara. Un modulo nuevo que el
# helper post-build no copie tiene que romper ESTA prueba, no el bot del usuario.
NO_SE_EMPAQUETAN = {
    "demo_roles.py",  # demo en terminal, no es parte de la app
    "launcher.py",    # GUI vieja: la app usa launcher_web (main_launcher.py)
}


def _modulos_del_arbol() -> list[str]:
    return sorted(
        p.name
        for p in SRC.glob("*.py")
        if not p.name.startswith("test_")
        and p.name != "testkit.py"
        and p.name not in NO_SE_EMPAQUETAN
    )


def _normalizado(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def test_modulos_del_exe_iguales_al_arbol():
    if not DIST_SRC.is_dir():
        return
    desactualizados: list[str] = []
    for nombre in _modulos_del_arbol():
        en_arbol = SRC / nombre
        en_exe = DIST_SRC / nombre
        if not en_exe.is_file():
            desactualizados.append(f"{nombre} (falta en el exe)")
        elif _normalizado(en_arbol) != _normalizado(en_exe):
            desactualizados.append(f"{nombre} (distinto)")
    assert not desactualizados, (
        "El exe lleva codigo viejo: "
        + ", ".join(desactualizados)
        + ". Copia el src/ del arbol a dist\\ytremote\\src\\ y repite."
    )


def run():
    total = run_sync_tests(globals())
    print(f"BUILD SYNC TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()
