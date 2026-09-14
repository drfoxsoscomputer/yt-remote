// launcher.js — Lógica del launcher (puente JS ↔ Python via pywebview)
//
// State-Driven UI: una sola fuente de verdad define cómo se ve el estado
// (botón central, chip, detalle, footer, link). El botón de poder unifica
// Estado + Acción:
//   Encendido     → click = Detener
//   Desconectado  → click = Conectar (si hay sesión)
//   Sin sesión    → click = abre el formulario
//   Error         → click = reintentar
// Regla UX: JAMÁS excepciones crudas de JS a la vista. Los errores solo se
// cuentan con motivo real + una acción clara (Ir a Configuración / Reintentar).
// Los capturadores globales escriben a consola (data/bot.log), nunca a la UI.

(function () {
  'use strict';

  // ─── Helpers seguros: nunca lanzan si el elemento falta o el token viene vacío ──
  function el(id) {
    return document.getElementById(id);
  }
  // Parte cada entrada por espacios y descarta vacíos: así classList.add/remove
  // recibe SOLO tokens válidos (un token con espacios tira InvalidCharacterError).
  // OJO: NO usar Array.prototype.flatMap: tokens() recibe STRINGS (no arrays)
  // y String.prototype.flatMap no existe en ningún motor (crash garantizado).
  function tokens(lista) {
    if (!lista) return [];
    if (!Array.isArray(lista)) lista = [lista];
    var salida = [];
    for (var i = 0; i < lista.length; i++) {
      var partes = String(lista[i]).split(/\s+/);
      for (var j = 0; j < partes.length; j++) {
        if (partes[j]) salida.push(partes[j]);
      }
    }
    return salida;
  }
  function setClases(node, add, remove) {
    if (!node) return;
    const a = tokens(add);
    const r = tokens(remove);
    if (r.length) node.classList.remove(...r);
    if (a.length) node.classList.add(...a);
  }
  function ocultar(node) {
    if (node) node.classList.add('hidden');
  }
  function mostrar(node) {
    if (node) node.classList.remove('hidden');
  }

  // ─── Telemetría: cada paso relevante queda impreso en data/bot.log ──
  let t0Log = Date.now();
  function uiLog(msg) {
    try {
      fetch('/api/log', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ msg: '+' + (Date.now() - t0Log) + 'ms ' + msg }),
      }).catch(() => {});
    } catch (e) {
      /* la telemetría jamás tumba la UI */
    }
  }

  // ─── fetch con timeout de seguridad: la UI jamás queda clavada ─────
  async function req(url, opts, timeoutMs) {
    const ms = timeoutMs || 15000;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    try {
      const r = await fetch(url, Object.assign({}, opts, { signal: ctrl.signal }));
      const texto = await r.text();
      let data;
      try {
        data = JSON.parse(texto);
      } catch (e) {
        uiLog('RESPUESTA_NO_JSON HTTP ' + r.status + ' ' + url + ': ' + texto.slice(0, 200).replace(/\s+/g, ' '));
        throw new Error('Respuesta inesperada (HTTP ' + r.status + ') en ' + url);
      }
      return data;
    } catch (e) {
      if (e && e.name === 'AbortError') {
        uiLog('TIMEOUT_' + ms + 'ms ' + url);
        throw new Error('Se agotó el tiempo de espera en ' + url + ' (' + ms + 'ms)');
      }
      uiLog('FALLO ' + url + ': ' + (e && e.message ? e.message : String(e)));
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  // ─── Elementos del DOM (vista estado) ─────────────────────────────
  const vistaEstado = el('vista-estado');
  const vistaForm = el('vista-form');
  const powerBtn = el('power-btn');
  const powerIcon = el('power-icon');
  const estadoBadge = el('estado-badge');
  const badgeDot = el('badge-dot');
  const badgeText = el('badge-text');
  const avatarDot = el('avatar-dot');
  const estadoBotname = el('estado-botname');
  const estadoDetalle = el('estado-detalle');
  const estadoAccion = el('estado-accion');
  const footerEstado = el('footer-estado');
  const menuBtn = el('menu-btn');
  const dropdownMenu = el('dropdown-menu');
  const menuConfig = el('menu-config');
  const menuLogout = el('menu-logout');

  // ─── Elementos del DOM (form) ─────────────────────────────────────
  const formAlert = el('form-alert');
  const errorMsg = el('error-msg');
  const toggleToken = el('toggle-token');
  const inputToken = el('bot_token');
  const inputAdmin = el('admin_id');
  const inputKick = el('kick_hours');
  const btnSave = el('btn-save');
  const btnBack = el('btn-back');

  // ─── Estados explícitos (nunca deducidos de los botones) ─────────
  const E = {
    SIN_SESION: 'sin-sesion',
    CONECTANDO: 'conectando',
    ENCENDIDO: 'encendido',
    APAGADO: 'apagado',
    ERROR: 'error',
  };

  let estadoUI = E.SIN_SESION;
  let estaConectando = false;
  let botName = ''; // handle del bot sin @, p. ej. "ytremoto_bot"
  // Acción contextual del estado ERROR: { mensaje, accion: {texto, handler} }.
  let accionError = null;

  // Símbolo universal de Power ON/OFF: uso SIEMPRE la misma forma (la del
  // asset assets/power-button.svg, viewBox 800, solo las 2 rutas visibles
  // clase .st0 — aro abierto + vástago; el disco blanco y el aro exterior
  // tienen display:none en el SVG original). Se tiñe por estado vía
  // fill="currentColor" (verde En línea / rojo Desconectado / gris Sin sesión).
  const ICONO_POWER =
    '<path stroke="currentColor" stroke-width="25" fill="none" d="M400.23,764c-201.13,0-364.23-163.03-364.23-364.16S199.11,35.61,400.23,35.61s364.16,163.03,364.16,364.16-163.03,364.23-364.16,364.23h0Z"/>' +
    '<path d="M573.17,193.73c14.6-15.18,50.43,15.18,50.43,15.18,39.14,50.43,62.53,114.44,62.53,183.93,0,163.05-128.47,295.17-287.07,295.17S111.99,555.87,111.99,392.82c0-70.63,24.08-135.43,64.24-186.21,0,0,31.38-27.84,47.35-13.01,15.97,14.83,11.87,40.96,2.62,52.6-32.52,41.08-50.54,92.3-50.54,146.61,0,128.13,100.41,231.39,223.29,231.39s223.29-103.14,223.29-231.39c0-53.51-17.57-104.06-49.17-144.9-9.01-11.64-14.6-39.02.11-54.19h0Z"/>' +
    '<path d="M367.11,145.8c0-17.57,14.15-31.83,31.94-31.83s31.94,14.26,31.94,31.83v223.4c0,17.57-14.15,31.83-31.94,31.83s-31.94-14.26-31.94-31.83v-223.4Z"/>';

  const BADGE_BASE =
    'inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold tracking-wide uppercase mb-2 transition-colors';
  // Consigna del usuario: la ventana queda visible mientras conecta y, cuando
  // el bot queda "En línea", espera ~3 segundos y se va sola al tray.
  const TRAY_TRAS_EN_LINEA = 3000;

  // Descripción de cómo se ve cada estado.
  function configEstado(e) {
    if (e === E.SIN_SESION) {
      return {
        badgeText: 'Sin sesión',
        badge: 'bg-texto-dim/10 border-borde text-texto-dim',
        dot: 'bg-texto-dim',
        avatar: 'bg-texto-dim',
        icon: 'power',
        iconCls: 'text-texto-dim',
        power: 'power-neutral',
        detail: 'Aún no hay sesión guardada. Haga clic para iniciar sesión.',
        footer: 'ⓘ Sin sesión guardada',
        accion: { texto: 'Iniciar sesión', handler: abrirFormVacio },
      };
    }
    if (e === E.CONECTANDO) {
      return {
        badgeText: 'Conectando',
        badge: 'bg-verde/10 border-verde/25 text-verde',
        dot: 'bg-verde animate-pulse',
        avatar: 'bg-verde',
        icon: 'power',
        iconCls: 'text-verde',
        power: 'power-connecting',
        detail: 'Conectando...',
        footer: 'ⓘ Estableciendo conexión',
      };
    }
    if (e === E.ENCENDIDO) {
      return {
        badgeText: 'En línea',
        badge: 'bg-verde/10 border-verde/25 text-verde',
        dot: 'bg-verde animate-pulse',
        avatar: 'bg-verde',
        icon: 'power',
        iconCls: 'text-verde group-hover:text-verde-hover',
        power: 'power-on',
        detail: 'El bot está funcionando activamente. Haga clic en el botón para apagarlo.',
        footer: 'ⓘ En segundo plano (System Tray)',
      };
    }
    if (e === E.APAGADO) {
      return {
        badgeText: 'Desconectado',
        badge: 'bg-rojo/10 border-rojo/30 text-rojo',
        dot: 'bg-rojo',
        avatar: 'bg-rojo',
        icon: 'power',
        iconCls: 'text-rojo group-hover:text-rojo-hover',
        power: 'power-idle',
        detail: 'Haga clic para conectar.',
        footer: 'ⓘ Desconectado',
      };
    }
    // ERROR: el mensaje y la acción reales vienen de accionError.
    const err = accionError || {};
    return {
      badgeText: 'Error',
      badge: 'bg-rojo/10 border-rojo/30 text-rojo',
      dot: 'bg-rojo',
      avatar: 'bg-rojo',
      icon: 'power',
      iconCls: 'text-rojo group-hover:text-rojo-hover',
      power: 'power-error',
      detail: err.mensaje || 'No se pudo iniciar el bot. Haga clic en el botón para reintentar.',
      footer: 'ⓘ Error — revise la configuración',
      accion: err.accion || { texto: 'Reintentar', handler: conectar },
    };
  }

  function pintarEstado() {
    const c = configEstado(estadoUI);

    // Remove-lists: el pintado nuevo reemplaza SIEMPRE las clases de color
    // anteriores. Sin esto, un chip que fue azul conserva restos del azul.
    const CLS_CHIP = 'bg-azul/10 bg-rojo/10 bg-verde/10 bg-texto-dim/10 border-azul/25 border-rojo/30 border-verde/25 border-borde text-azul text-rojo text-verde text-texto-dim';
    const CLS_DOT = 'bg-azul bg-rojo bg-verde bg-texto-dim animate-pulse';

    badgeText.textContent = c.badgeText;
    setClases(estadoBadge, [BADGE_BASE, c.badge], CLS_CHIP);
    setClases(badgeDot, ['w-2 h-2 rounded-full transition-colors', c.dot], CLS_DOT);
    setClases(avatarDot, ['absolute -bottom-1 -right-1 z-10 w-3 h-3 rounded-full border-2 border-fondo transition-colors', c.avatar], CLS_DOT);

    setClases(powerBtn, [c.power], ['power-neutral', 'power-idle', 'power-on', 'power-error', 'power-connecting']);

    setClases(
      powerIcon,
      ['transition-colors', c.iconCls],
      ['text-verde', 'text-rojo', 'text-azul', 'text-texto-dim', 'animate-spin']
    );
    powerIcon.innerHTML = ICONO_POWER;

    if (estadoDetalle) estadoDetalle.textContent = c.detail;
    if (footerEstado) footerEstado.textContent = c.footer;

    // Botón de acción contextual (Iniciar sesión / Ir a Configuración / Reintentar).
    if (c.accion) {
      estadoAccion.textContent = c.accion.texto;
      estadoAccion.onclick = c.accion.handler;
      mostrar(estadoAccion);
    } else {
      ocultar(estadoAccion);
      estadoAccion.onclick = null;
    }

    // Identidad: @handle del bot cuando lo conocemos.
    if (botName && estadoBotname) {
      estadoBotname.textContent = '@' + botName;
      mostrar(estadoBotname);
    } else {
      ocultar(estadoBotname);
    }
  }

  function mostrarVista(vista) {
    ocultar(vistaEstado);
    ocultar(vistaForm);
    if (vista === 'estado') mostrar(vistaEstado);
    else mostrar(vistaForm);
    ocultar(dropdownMenu);
  }

  // ─── Error: causa real + acción, sin cajas de alerta genéricas ────
  function armarError(tecnico, esRed) {
    const tex = tecnico || '';
    let msj = 'El bot no arrancó. Haga clic en Reintentar para probar de nuevo.';
    let accion = { texto: 'Reintentar', handler: conectar };
    if (/token/i.test(tex)) {
      msj = 'Telegram rechazó el token. Revise la Configuración e intente de nuevo.';
      accion = { texto: 'Ir a Configuración', handler: () => { cargarForm(); } };
    } else if (/sesi|inicia sesi|credencial/i.test(tex)) {
      msj = 'Necesita iniciar sesión: faltan credenciales guardadas.';
      accion = { texto: 'Ir a Configuración', handler: () => { cargarForm(); } };
    } else if (esRed) {
      msj = 'Sin conexión con el servidor local. Haga clic en Reintentar en unos segundos.';
      accion = { texto: 'Reintentar', handler: conectar };
    }
    accionError = { mensaje: msj, accion: accion };
    estadoUI = E.ERROR;
    pintarEstado();
    uiLog('ERROR_UI: ' + msj + ' | real: ' + (tecnico ? String(tecnico).slice(0, 300) : ''));
  }

  // Consigna del usuario: una vez el bot queda "En línea", la ventana espera
  // ~3 segundos visibles y recién ahí se va sola al tray.
  async function esperarYMinimizar() {
    uiLog('ventana: En línea -> espera ' + TRAY_TRAS_EN_LINEA + 'ms');
    await new Promise((r) => setTimeout(r, TRAY_TRAS_EN_LINEA));
    if (window.pywebview && window.pywebview.api && window.pywebview.api.minimizar_tray) {
      try {
        await window.pywebview.api.minimizar_tray();
        uiLog('ventana: minimizó al tray');
      } catch (e) {
        uiLog('ventana: minimizar_tray excepción: ' + (e && e.message || String(e)));
      }
    } else {
      uiLog('ventana: minimizar_tray no disponible (sin pywebview)');
    }
  }

  // Red de seguridad SILENCIOSA: el motivo va al log (data/bot.log), jamás a la UI.
  window.addEventListener('error', (ev) => {
    console.error('YT-Remote (JS):', ev && ev.message);
    uiLog('JS_error: ' + (ev && ev.message));
  });
  window.addEventListener('unhandledrejection', (ev) => {
    var r = ev && ev.reason;
    console.error('YT-Remote (JS promesa):', r);
    uiLog('JS_promesa: ' + (r && r.message ? r.message : String(r)));
  });

  // ─── Menú contextual propio (WebView2 no crea menú nativo) ─────────
  const ctxMenu = el('ctx-menu');
  const btnCtxPaste = el('ctx-paste');
  const btnCtxCopy = el('ctx-copy');
  let ctxTarget = null;

  function cleanInvisible(text) {
    return String(text).replace(/[\u200B-\u200D\u2060\uFEFF\u00AD]/g, '');
  }

  function hideCtxMenu() {
    ctxMenu.style.display = 'none';
    ctxTarget = null;
    document.removeEventListener('mousedown', closeCtxOnOutside, true);
  }

  function closeCtxOnOutside(e) {
    if (!ctxMenu.contains(e.target)) hideCtxMenu();
  }

  function showCtxMenu(e, input) {
    e.preventDefault();
    ctxTarget = input;
    ctxMenu.style.left = Math.min(e.clientX, window.innerWidth - 180) + 'px';
    ctxMenu.style.top = Math.min(e.clientY, window.innerHeight - 90) + 'px';
    ctxMenu.style.display = 'block';
    document.addEventListener('mousedown', closeCtxOnOutside, true);
  }

  async function insertClipboard() {
    if (!ctxTarget) return;
    let text = '';
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.pegar) {
        text = await window.pywebview.api.pegar();
      }
      if (!text) {
        text = await navigator.clipboard.readText().catch(() => '');
      }
    } catch (e) {
      console.warn('No se pudo leer el portapapeles:', e);
    }
    if (!text) return;
    const input = ctxTarget;
    const cleaned = cleanInvisible(text);
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? start;
    input.value = input.value.slice(0, start) + cleaned + input.value.slice(end);
    input.focus();
    const pos = start + cleaned.length;
    input.setSelectionRange(pos, pos);
  }

  document.addEventListener('contextmenu', (e) => {
    const input = e.target.matches('input') ? e.target : e.target.closest('input');
    if (input) {
      showCtxMenu(e, input);
      return;
    }
  });
  btnCtxPaste.addEventListener('click', insertClipboard);
  btnCtxCopy.addEventListener('click', async () => {
    if (!ctxTarget) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(ctxTarget.value);
      }
    } catch (e) {
      console.warn('No se pudo copiar:', e);
    }
    hideCtxMenu();
  });

  // ─── Toggle mostrar/ocultar token (solo el token; el ID es visible) ──
  toggleToken.addEventListener('click', () => {
    const isPassword = inputToken.type === 'password';
    inputToken.type = isPassword ? 'text' : 'password';
    toggleToken.innerHTML = isPassword
      ? '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 114.24 4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>'
      : '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  });

  // ─── Formulario (iniciar sesión / configurar) ─────────────────────
  function showError(msg) {
    errorMsg.textContent = msg;
    mostrar(errorMsg);
  }

  function clearError() {
    errorMsg.textContent = '';
    ocultar(errorMsg);
  }

  function formVacio() {
    inputToken.value = '';
    inputAdmin.value = '';
    inputKick.value = '0';
    clearError();
  }

  function abrirFormVacio() {
    formVacio();
    mostrar(formAlert); // banner: faltan datos para iniciar sesión
    mostrarVista('form');
    inputToken.focus();
  }

  async function cargarForm() {
    try {
      const s = await req('/api/session');
      inputToken.value = s.bot_token || '';
      inputAdmin.value = s.admin_id ? String(s.admin_id) : '';
      inputKick.value = s.kick_after_hours != null ? String(s.kick_after_hours) : '0';
    } catch (e) {
      /* sin sesión: formulario vacío */
    }
    clearError();
    ocultar(formAlert); // vino a editar config, no a iniciar sesión
    mostrarVista('form');
    inputToken.focus();
  }

  btnBack.addEventListener('click', () => cargarEstado(false));
  const iconoGuardar = '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>';
  function restaurarBotonGuardar() {
    btnSave.innerHTML = iconoGuardar + '<span class="btn-save-label">Guardar</span>';
  }
  btnSave.addEventListener('click', async () => {
    const token = cleanInvisible(inputToken.value.trim());
    const adminId = cleanInvisible(inputAdmin.value.trim());
    const kickHours = parseInt(inputKick.value, 10) || 0;

    if (!token) {
      showError('Ingrese el token del bot');
      inputToken.focus();
      return;
    }
    if (!/^\d+$/.test(adminId) || parseInt(adminId, 10) <= 0) {
      showError('ID de admin inválido (debe ser número positivo)');
      inputAdmin.focus();
      return;
    }

    btnSave.disabled = true;
    btnSave.textContent = 'Guardando...';
    clearError();
    try {
      const data = await req('/api/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bot_token: token,
          admin_id: adminId,
          kick_after_hours: kickHours,
        }),
      }, 20000);
      if (data.ok) {
        await cargarEstado(false);
      } else {
        showError(data.error || 'No se pudo guardar');
      }
    } catch (e) {
      showError('Error: ' + e.message);
    } finally {
      btnSave.disabled = false;
      restaurarBotonGuardar();
    }
  });

  // ─── Acciones reales del botón de poder ───────────────────────────
  async function conectar() {
    if (estaConectando) return;
    estaConectando = true;
    accionError = null;
    estadoUI = E.CONECTANDO;
    pintarEstado();
    uiLog('conectar: Conectando pintado');
    try {
      // Sin credenciales → nunca un estado Error: a Configuración directo.
      const s = await req('/api/session');
      uiLog('conectar: session has_session=' + !!(s && s.has_session));
      if (!s || !s.has_session) {
        abrirFormVacio();
        return;
      }
      const data = await req('/api/start', { method: 'POST' }, 20000);
      uiLog('conectar: /api/start ok=' + !!(data && data.ok) + (data && data.error ? ' err=' + data.error : ''));
      if (data.ok) {
        if (data.bot_username) botName = data.bot_username;
        await cargarEstado(false);
        // Consigna UX: minimizar al tray SOLO si el bot quedó En línea. Si el
        // bot murió justo después de /api/start, la ventana se queda visible
        // mostrando Desconectado en vez de esconderse con un bot caído.
        if (estadoUI === E.ENCENDIDO) {
          await esperarYMinimizar();
        } else {
          uiLog('conectar: bot no quedo Encendido, sin minimizar (estado=' + estadoUI + ')');
        }
      } else {
        armarError(data.error, false);
      }
    } catch (e) {
      armarError(e && (e.message || String(e)), true);
    } finally {
      estaConectando = false;
    }
  }

  async function detener() {
    try {
      await req('/api/stop_bot', { method: 'POST' });
    } catch (e) {
      console.warn('Error al detener:', e);
    }
    await cargarEstado(false);
  }

  powerBtn.addEventListener('click', () => {
    if (estaConectando) return;
    if (estadoUI === E.ENCENDIDO) {
      detener();
    } else if (estadoUI === E.SIN_SESION) {
      abrirFormVacio();
    } else {
      conectar(); // DESCONECTADO o ERROR → arrancar/reintentar
    }
  });

  // ─── Menú engranaje / cerrar sesión / minimizar ───────────────────
  menuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdownMenu.classList.toggle('hidden');
  });
  document.addEventListener('mousedown', (e) => {
    if (!dropdownMenu.contains(e.target) && !menuBtn.contains(e.target)) {
      ocultar(dropdownMenu);
    }
  });
  menuConfig.addEventListener('click', cargarForm);

  menuLogout.addEventListener('click', async () => {
    ocultar(dropdownMenu);
    if (!confirm('¿Cerrar sesión? Se borrará la configuración guardada y se detendrá el bot.')) return;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.logout) {
        // logout() nativo ya navega a /launcher (load_url): recargar de nuevo
        // sería una doble navegación.
        await window.pywebview.api.logout();
      } else {
        await req('/api/stop_bot', { method: 'POST' });
        location.reload();
      }
    } catch (e) {
      console.warn('Error en logout:', e);
    }
  });

  // ─── Estado veraz en vivo ─────────────────────────────────────────
  async function cargarEstado(primerPaso) {
    // Atrás y el retorno tras Guardar/Conectar vuelven a la vista Estado.
    mostrarVista('estado');
    uiLog('cargarEstado(primerPaso=' + (primerPaso ? 'S' : 'N') + ')');
    try {
      const s = await req('/api/session');
      uiLog('session has_session=' + !!(s && s.has_session) + ' user=' + ((s && s.bot_username) || ''));
      if (!s || !s.has_session) {
        botName = '';
        accionError = null;
        estadoUI = E.SIN_SESION;
        pintarEstado();
        return;
      }
      if (s.bot_username) botName = s.bot_username;

      const leer = () => req('/api/status', null, 10000);
      let st = await leer();
      uiLog('status bot_running=' + !!(st && st.bot_running));

      // Primer cargado con sesión: "Conectando..." hasta que el bot quede
      // "En línea"; ahí la ventana espera ~3 s y se va sola al tray.
      if (primerPaso) {
        estadoUI = E.CONECTANDO;
        pintarEstado();
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 1000));
          st = await leer();
          uiLog('boot: poll #' + (i + 1) + ' bot_running=' + !!(st && st.bot_running));
          if (st && st.bot_running) break;
        }
        accionError = null;
        estadoUI = st && st.bot_running ? E.ENCENDIDO : E.APAGADO;
        pintarEstado();
        uiLog('estado final boot=' + estadoUI);
        if (st && st.bot_running) await esperarYMinimizar();
        return;
      }

      accionError = null;
      estadoUI = st && st.bot_running ? E.ENCENDIDO : E.APAGADO;
      pintarEstado();
      uiLog('estado pintado=' + estadoUI);
    } catch (e) {
      armarError(e && (e.message || String(e)), true);
    }
  }
  window.__refresh_estado = cargarEstado;

  // ─── Splash listo: avisa a Python que la página está pintada ──────
  function notifySplashReady() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.splash_listo) {
      window.pywebview.api.splash_listo();
    }
  }

  // ─── Inicialización ───────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    notifySplashReady();
    cargarEstado(true);
    // Polling 4s: si el bot muere o reactiva, la UI cambia sola.
    setInterval(async () => {
      if (estaConectando) return;
      if (vistaEstado && !vistaEstado.classList.contains('hidden')) {
        try {
          const st = await req('/api/status', null, 10000);
          if (estadoUI !== E.ENCENDIDO && st.bot_running) {
            await cargarEstado(false);
          } else if (estadoUI === E.ENCENDIDO && !st.bot_running) {
            await cargarEstado(false);
          }
        } catch (e) {
          /* servidor ocupado o cerrando: se ignora y se reintenta */
          uiLog('poll 4s: /api/status fallo: ' + (e && e.message || String(e)));
        }
      }
    }, 4000);
  });

  // Sondeo de splash_listo (backup si el evento pywebviewready falla)
  let intentos = 0;
  const maxIntentos = 40; // 10s total
  const intervalo = setInterval(() => {
    intentos++;
    if (notifySplashReady() || intentos >= maxIntentos) {
      clearInterval(intervalo);
    }
  }, 250);
})();