# Historial de planes

Registro de los planes ya cerrados. El plan vigente vive en el tablero de
`todowrite`; aqui queda written lo que ya se termino, con el commit de cada
cambio para poder revertirlo por separado.

---

## Plan 2 - "11 tareas: que no se rompa, y que no se rompa otra vez" (cerrado 30/sep/2026)

Objetivo: cerrar los huecos que dejo el plan 1, uno por commit, y hacer que el
codigo dejara de ser un archivo de 3522 lineas. Cada tarea es un commit
atomico y revertible por separado.

### Completadas

| N | Que | Commit |
|---|-----|--------|
| 1/11 | La tarjeta reaparece al arrancar (`_secuencia_arranque`) | `cf90a77` |
| 2/11 | El listado de `/buscar` se lee en el celular (texto, sin miniaturas) | `67a4576` |
| 3/11 | La busqueda respeta al artista pedido (por canal, no por titulo) | `9bfe2b4` |
| 4/11 | Una sola lectura de la tanda de red | `903b607` |
| 5/11 | El aviso de red vive en el PC, al instante ("Sin conexion") | `1efc17d` |
| 6/11 | El token del bot deja de volver a la pantalla | `ea204a1` |
| 7/11 | La lista no se borra en silencio y el token sale del log | `ae4f1bc`, `95b1a46` |
| 8/11 | Un solo almacenamiento con esquema real (`store.py`) | `8b87e9a`, `31e2fe6`, `484c223` |
| 9/11 | La tarjeta sale de `bot.py` a `card.CardManager` | `a77aa09` |
| 10/11 | Constantes, funciones puras y motor fuera de `bot.py` | `5453840`, `6b96d3e`, `b0965f5`, `1959ef6` |
| 11/11 | E2E, push, docs y release v1.1.3 | `46c1840`, `a9aa3a5`, `f2c3737` |

Estado al cerrar: suite **156/156**, roles **14**, almacen **15**, launcher **5**,
guardian de sincronizacion **2**. `bot.py` bajo de **3522 a 3122 lineas**. Los 24
commits quedaron pusheados. Release **v1.1.3** publicado
(`yt-remote-1.1.3-portable.zip`, 92.1 MB, 3487 entradas).

### Las tres cosas que este plan dejo escritas

**1. Un archivo por entidad no es solo mas archivos: es un archivo mas que se
puede renombrar por debajo de los pies del otro.** La 8/11 quedo a medias
(`StateStore` paso a `store.Store`, `RoleManager` no) y el `store` renombraba
`roles.db` a `.migrado` al migrar. A partir de ahi `register_user` abria un
`roles.db` nuevo y vacio y reventaba con `no such table: roles`; como se llama
en `_passive_card_reposition` **antes** de reposicionar, el bot moria ahi y la
tarjeta no bajaba al escribir cualquier mensaje. Lo encontró la E2E del usuario.

**2. El suite no lo veia porque todas las pruebas aislaban la ruta.** Cada
prueba parcheaba `roles.DB_PATH` a una carpeta temporal, donde el cruce con la
migracion nunca ocurre. La garantia que faltaba no era "el rol se guarda" sino
"el rol sobrevive a la migracion del legacy", y se verifico en ambos sentidos.

**3. Una lista escrita a mano siempre se queda vieja. Dos veces, en un dia.**
`test_build_sync.py` comparaba contra una lista de modulos y le quedaron fuera
`store.py` y `card.py`; `release_rules.json` tenia la misma clase de lista y el
ZIP de 1.1.3 salio **roto**, sin `store.py`, que `persistence` importa. Las dos
se derivan ahora del arbol. El guardian de sincronizacion daba verde en el
segundo caso porque compara el arbol contra `dist/`, y esa copia si tenia todo:
**no veia el ZIP**. Por eso la prueba nueva mira la otra mitad.

### Pendientes

Ninguna. Queda una sola cosa, del usuario y no del codigo: **re-probar en la app
que la tarjeta baja al escribir un mensaje**. El fix esta verificado por prueba
en ambos sentidos, pero no por la mano del usuario.

### Lo que este plan NO hizo, y por que

- **No se invirtio la dependencia de `card.CardManager` ni de
  `playback.PlaybackEngine`.** Ambos reciben el bot entero en vez de colaboradores
  sueltos. Invertirla no compra nada mientras no haya segundo cliente ni prueba
  sin Telegram, y las 31 delegaciones son el contrato real. Esta escrito en el
  docstring de los dos modulos: cuando aparezca un panel web o una prueba sin
  Telegram, se invierte.
- **No se unificaron `_normalizar` y `clave_texto`.** Parecen el mismo
  normalizador y NO lo son: el primero se queda con `[a-z0-9]` y una "ñ"
  desaparece entera; el segundo usa `isalnum()` y la conserva. Unificarlos seria
  cambiar comportamiento, no un refactor.
- **No se bumpeo la version durante el trabajo.** Siguió en 1.1.2 hasta la 11/11,
  cuando la E2E dio el visto bueno para publicar. v1.1.2 ya estaba publicada, asi
  que por convencion el release salio como **1.1.3**.

---

## Plan 1 — "Fase 1: que no se rompa lo que funciona" (cerrado 30/sep/2026)

Objetivo: dejar el bot estable y verificable antes de refactorizar. Cada tarea
era un commit atomico y revertible por separado.

### Completadas

| N | Que | Commit |
|---|-----|--------|
| 1/8 | Las pruebas se descubren solas y el exe no puede quedar viejo | `d7285df` |
| 2/8 | Las 3 garantias del producto escritas como pruebas (en rojo a proposito) | `d3e12da` |
| 3/8a | `/buscar` transaccional + la radio sigue al tema real | `dea82c9` |
| 3/8b | Listado con paginacion, info completa y miniatura | `2f04c4e` |
| 3/8c | La playlist se carga a ventanas de 30, no entera | `6b18d11` |
| 4/8 | Punto unico de salida de mensajes: la tarjeta vuelve sola al final | `af2c67b` |
| 5/8 | Verificacion y reconstruccion del exe con la rutina corregida | `2633795` |

Estado al cerrar: suite **137/137 en verde**, exe de 18.1 MB, version 1.1.2 sin
tocar. Los 7 commits quedaron **sin pushear**.

### Pendientes que NO se cerraron

| N | Que paso con ella |
|---|------------------|
| 6/8 | Pausa de prueba del usuario. No se ejecuto: la propia prueba destapo bugs que habian que arreglar antes. Su contenido completo esta en la tarea 11/11 del plan 2. |
| 7/8 | Separar la tarjeta del archivo gigante. Cambio de motivo: no era el tamano del archivo, sino que no hay frontera entre el programa y el almacenamiento. Se fusiono con la migracion (tarea 9/11 del plan 2). |
| 8/8 | Separar el motor de reproduccion y constantes con nombre. Sigue viva, va despues de la migracion (tarea 10/11 del plan 2). |

### Lo que este plan dejo sin resolver

Se documenta aqui porque explica por que el plan 2 existe.

1. **La tarjeta desaparece al arrancar.** `src/bot.py:435-442` borra la tarjeta
   persistida y luego comprueba `_card_message_id is not None` para re-crearla;
   pero `_remove_card()` (linea 1194) pone esa variable en `None`, asi que la
   condicion **nunca puede ser cierta**. Se descubrio al hacer la prueba 6/8 del
   usuario. Lo mas grave: las pruebas de garantia estaban en verde porque arman
   el estado a mano (`_card_message_id = 7`) en vez de arrancar el bot de verdad.
   Ese hueco es lo que dejo pasar el bug.

2. **La busqueda no respeta al artista.** `/buscar <artista> - <cancion>` hace
   una busqueda de texto plano (`search.py:116`) y trae lo que YouTube rankea
   primero. Con "gp band - impactante" salio GP Band en 4o lugar. Y como
   `bot.py:2321` anota como artista **lo que escribio el usuario**, elegir una
   cancion de Mafe Restrepo dejaba la radio buscando musica de GP Band.

3. **Dos lectores de la misma tanda de red.** Al volver la conexion, el polling
   de Telegram se recupera solo, ejecuta el comando, y hasta 15 segundos despues
   `_net_watch_job` lee esa misma tanda y la declara descartada. El usuario ve
   "se perdio, mandalo de nuevo" **al lado de los resultados que si salieron**.
   La carrera esta en `bot.py:553-596` (vigilante) contra la recuperacion
   automatica del updater.

4. **El aviso de "comandos perdidos" es inutil.** Llega despues del hecho y le
   pide al usuario reenviar algo que a veces ya se ejecuto. El propio usuario lo
   dijo: hay que saber **antes** si se puede, no despues.

5. **El almacenamiento esta en el lugar equivocado.** `roles.db` son dos tablas
   clave-valor (`key TEXT, value TEXT`) con un JSON adentro: no es un esquema.
   Y el listado de canciones —lo unico que crece— vive en `state.json`, que se
   reescribe completo en cada cambio. Ademas `persistence.py:143-148` **borra la
   lista del usuario en silencio** si la version del formato no coincide, y el
   docstring dice "futuro: migracion": esa migracion nunca llego.

6. **El token se descifra y vuelve a la pantalla.** `launcher_web.py:128-137`
   devuelve `bot_token` en claro a la ventana, que lo pinta en un `<input>`.
   Guardarlo bien (DPAPI) y despues mostrarlo es peor que no guardarlo bien. El
   servidor si esta atado a `127.0.0.1` (`main_launcher.py:454`), asi que no es
   exposicion de red; es exposicion local y visual.

7. **El listado de `/buscar` no se podia leer en el celular.** Metia titulo,
   canal y duracion en el mismo boton; en un boton de Telegram nada salta de
   linea, asi que se recortaba. El usuario lo comprobo con una captura. Un boton
   que se recorta es un boton donde la informacion no existe.

### Leccion que se lleva el plan 2

La invariante de "la tarjeta es la ultima visualizacion" estaba escrita a mano en
tres lugares distintos. Cuando aparecio un camino nuevo (la bienvenida, los avisos
de red), la regla no se aplico y el bug volvio. **Una invariante de UI necesita
un solo punto de entrada, no tres.** Lo mismo paso con el arranque: un helper
(borrar la tarjeta) muto estado que otro leyo dos lineas despues.

Tambien: las pruebas pueden estar 100% en verde y aun asi el producto estar roto,
si arman el estado en vez de reproducir el flujo. La 1/11 del plan 2 ataca justo
eso: arrancar el bot de verdad en la prueba.