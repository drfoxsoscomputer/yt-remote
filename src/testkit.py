"""Descubrimiento y ejecucion automatica de las pruebas del proyecto.

Reemplaza las listas manuales de llamadas: asi una prueba nueva se ejecuta
siempre, sin que nadie pueda olvidarse de registrarla.
"""

import asyncio
import inspect
from typing import Any, Callable

TestFn = Callable[..., Any]


def collect_tests(namespace: dict[str, Any]) -> list[tuple[int, str, TestFn]]:
    """Pruebas test_* definidas en este modulo, en orden de definicion."""
    module = namespace.get("__name__")
    found: list[tuple[int, str, TestFn]] = []
    for name, fn in list(namespace.items()):
        if not name.startswith("test_"):
            continue
        if not callable(fn) or inspect.isclass(fn):
            continue
        if getattr(fn, "__module__", None) != module:
            continue
        code = getattr(fn, "__code__", None)
        if code is None:
            continue
        found.append((code.co_firstlineno, name, fn))
    found.sort(key=lambda item: item[0])
    return found


def run_sync_tests(namespace: dict[str, Any]) -> int:
    """Ejecuta todas las pruebas del modulo y devuelve cuantas corrieron."""
    tests = collect_tests(namespace)
    for _, _, fn in tests:
        if inspect.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
    return len(tests)


async def run_async_tests(namespace: dict[str, Any]) -> int:
    """Igual que run_sync_tests, pero desde un run() asincrono."""
    tests = collect_tests(namespace)
    for _, _, fn in tests:
        if inspect.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
    return len(tests)
