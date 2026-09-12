// launcher.js — Lógica del launcher (puente JS ↔ Python via pywebview)
//
// Flujo: vista de ESTADO (principal) y vista de FORM (iniciar sesión /
// configurar). Guardar SOLO guarda; Conectar arranca el bot; Detener NO
// navega ni borra nada; Cerrar sesión es lo único que borra.

(function () {
  'use strict';

  // Elementos del DOM (vista estado)
  const vistaEstado = document.getElementById('vista-estado');
  const vistaForm = document.getElementById('vista-form');
  const estadoTitulo = document.getElementById('estado-titulo');
  const estadoDetalle = document.getElementById('estado-detalle');
  const estadoDot = document.getElementById('estado-dot');
  const btnStartSession = document.getElementById('btn-start-session');
  const btnConnect = document.getElementById('btn-connect');
  const btnStop = document.getElementById('btn-stop');
  const btnSettings = document.getElementById('btn-settings');
  const btnLogout = document.getElementById('btn-logout');
  const btnExit = document.getElementById('btn-exit');
  const btnSave = document.getElementById('btn-save');
  const btnCancel = document.getElementById('btn-cancel');
  const btnFormExit = document.getElementById('btn-form-exit');

  // Elementos del DOM (vista form)
  const errorMsg = document.getElementById('error-msg');
  const toggleToken = document.getElementById('toggle-token');
  const toggleAdmin = document.getElementById('toggle-admin');
  const inputToken = document.getElementById('bot_token');
  const inputAdmin = document.getElementById('admin_id');
  const inputKick = document.getElementById('kick_hours');

  // Estados: 'sin-sesion' | 'conectando' | 'conectado' | 'detenido' | 'error'
  const COLOR = {
    sin: '#e8e8e8',
    connect: '#229ed9',
    ok: '#31c471',
    detenido: '#a1a1aa',
    error: '#e53935',
  };

  function showError(msg) {
    errorMsg.textContent = msg;
    errorMsg.classList.remove('hidden');
  }

  function clearError() {
    errorMsg.textContent = '';
    errorMsg.classList.add('hidden');
  }

  function mostrarVista(vista) {
    vistaEstado.classList.toggle('hidden', vista !== 'estado');
    vistaForm.classList.toggle('hidden', vista !== 'form');
  }

  let estadoUI = 'otro';

  function setEstado(titulo, detalle, color, acciones) {
    estadoTitulo.textContent = titulo;
    estadoDetalle.textContent = detalle || '';
    estadoDot.style.background = color;
    btnStartSession.classList.toggle('hidden', !acciones.includes('iniciar'));
    btnConnect.classList.toggle('hidden', !acciones.includes('conectar'));
    btnStop.classList.toggle('hidden', !acciones.includes('detener'));
    btnSettings.classList.toggle('hidden', !acciones.includes('configurar'));
    btnLogout.classList.toggle('hidden', !acciones.includes('logout'));
    mostrarVista('estado');
    estadoUI = acciones.includes('detener')
      ? 'conectado'
      : acciones.includes('conectar')
      ? 'detenido'
      : acciones.includes('iniciar')
      ? 'sin-sesion'
      : 'otro';
  }

  async function cargarEstado(primerPaso) {
    try {
      const s = await fetch('/api/session').then((r) => r.json());
      if (!s.has_session) {
        setEstado(
          'Debes iniciar sesión',
          'Guardá el token y tu ID para poder conectar el bot.',
          COLOR.sin,
          ['iniciar']
        );
        return;
      }

      const leer = () => fetch('/api/status').then((r) => r.json());
      let st = await leer();

      // En el primer cargado (arranque con datos guardados) el bot puede
      // estar arrancando solo: mostrar "Conectando..." mientras Python
      // termina de levantarlo y oculta la ventana al tray.
      if (primerPaso && !st.bot_running) {
        setEstado(
          'Conectando...',
          'Arrancando el bot...',
          COLOR.connect,
          []
        );
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 1000));
          st = await leer();
          if (st.bot_running) break;
        }
      }

      if (st.bot_running) {
        setEstado(
          'Conectado',
          'El bot corre en segundo plano. Podés cerrar la ventana y seguirá activo.',
          COLOR.ok,
          ['detener', 'configurar', 'logout']
        );
      } else {
        setEstado(
          'Detenido',
          'El bot no está corriendo. Pulsá Conectar para arrancarlo.',
          COLOR.detenido,
          ['conectar', 'configurar', 'logout']
        );
      }
    } catch (e) {
      setEstado('Error', 'Error de conexión: ' + (e && e.message ? e.message : e), COLOR.error, ['conectar', 'configurar']);
    }
  }
  window.__refresh_estado = cargarEstado;

  // ─── Menú contextual propio (WebView2 no crea menú nativo) ─────────
  const ctxMenu = document.getElementById('ctx-menu');
  const btnCtxPaste = document.getElementById('ctx-paste');
  const btnCtxCopy = document.getElementById('ctx-copy');
  let ctxTarget = null;

  // Caracteres invisibles que se cuelan al pegar (zero-width, BOM, etc.):
  // un .trim() no los saca y invalidan el token.
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

  // ─── Toggle mostrar/ocultar contraseña ─────────────────────────────
  function setupToggle(toggleBtn, input) {
    toggleBtn.addEventListener('click', () => {
      const isPassword = input.type === 'password';
      input.type = isPassword ? 'text' : 'password';
      toggleBtn.innerHTML = isPassword
        ? '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 114.24 4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>'
        : '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
    });
  }

  setupToggle(toggleToken, inputToken);
  setupToggle(toggleAdmin, inputAdmin);

  // ─── Formulario (iniciar sesión / configurar) ─────────────────────
  function formVacio() {
    inputToken.value = '';
    inputAdmin.value = '';
    inputKick.value = '0';
    clearError();
  }

  async function cargarForm() {
    try {
      const s = await fetch('/api/session').then((r) => r.json());
      inputToken.value = s.bot_token || '';
      inputAdmin.value = s.admin_id ? String(s.admin_id) : '';
      inputKick.value = s.kick_after_hours != null ? String(s.kick_after_hours) : '0';
    } catch (e) {
      /* sin sesión: formulario vacío */
    }
    clearError();
    mostrarVista('form');
    inputToken.focus();
  }

  btnStartSession.addEventListener('click', () => {
    formVacio();
    mostrarVista('form');
    inputToken.focus();
  });

  btnSettings.addEventListener('click', cargarForm);
  btnCancel.addEventListener('click', cargarEstado.bind(null, false));
  btnFormExit.addEventListener('click', () => {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.quit_app();
    } else {
      window.close();
    }
  });

  btnSave.addEventListener('click', async () => {
    const token = cleanInvisible(inputToken.value.trim());
    const adminId = cleanInvisible(inputAdmin.value.trim());
    const kickHours = parseInt(inputKick.value, 10) || 0;

    if (!token) {
      showError('Ingresa el token del bot');
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
      const resp = await fetch('/api/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bot_token: token,
          admin_id: adminId,
          kick_after_hours: kickHours,
        }),
      });
      const data = await resp.json();
      if (data.ok) {
        await cargarEstado(false);
      } else {
        showError(data.error || 'No se pudo guardar');
      }
    } catch (e) {
      showError('Error: ' + e.message);
    } finally {
      btnSave.disabled = false;
      btnSave.textContent = 'Guardar';
    }
  });

  // ─── Conectar / Detener / Cerrar sesión / Salir ───────────────────
  btnConnect.addEventListener('click', async () => {
    setEstado('Conectando...', 'Verificando el token y arrancando el bot...', COLOR.connect, []);
    try {
      const resp = await fetch('/api/start', { method: 'POST' });
      const data = await resp.json();
      if (data.ok) {
        setEstado(
          'Conectado',
          data.message || 'El bot corre en segundo plano.',
          COLOR.ok,
          []
        );
        if (window.pywebview && window.pywebview.api && window.pywebview.api.minimizar_tray) {
          window.pywebview.api.minimizar_tray();
        }
      } else {
        setEstado('Error', data.error || 'No se pudo iniciar el bot', COLOR.error, ['conectar', 'configurar', 'logout']);
      }
    } catch (e) {
      setEstado('Error', 'Error de conexión: ' + e.message, COLOR.error, ['conectar', 'configurar', 'logout']);
    }
  });

  btnStop.addEventListener('click', async () => {
    btnStop.disabled = true;
    btnStop.textContent = 'Deteniendo...';
    try {
      await fetch('/api/stop_bot', { method: 'POST' });
    } catch (e) {
      console.warn('Error al detener:', e);
    } finally {
      btnStop.disabled = false;
      btnStop.textContent = 'Detener';
    }
    // Se queda en la vista de estado mostrando "Detenido": no navega y no borra.
    await cargarEstado(false);
  });

  btnLogout.addEventListener('click', async () => {
    if (!confirm('¿Cerrar sesión? Se borrará la configuración guardada y se detendrá el bot.')) return;
    btnLogout.disabled = true;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.logout) {
        window.pywebview.api.logout();
      } else {
        await fetch('/api/stop_bot', { method: 'POST' });
      }
    } catch (e) {
      showError('Error: ' + e.message);
    }
    btnLogout.disabled = false;
    // logout() (Python) borra la sesión y recarga la página: al volver,
    // cargarEstado muestra la vista sin sesión.
  });

  btnExit.addEventListener('click', () => {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.quit_app();
    } else {
      window.close();
    }
  });

  // ─── Splash listo: avisa a Python que la página está pintada ──────
  function notifySplashReady() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.splash_listo) {
      window.pywebview.api.splash_listo();
    }
  }

  // ─── Inicialización ───────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    notifySplashReady();
    // Primer cargado: si hay sesión, el bot puede estar arrancando solo
    // (auto-conexión desde Python) → el estado pinta "Conectando...".
    cargarEstado(true);
    // Estado veraz en vivo: si el bot muere (o reactiva), la UI cambia sola.
    // Solo cuando la vista Estado está visible, para no patear al usuario
    // que está en el formulario.
    setInterval(async () => {
      if (!vistaEstado.classList.contains('hidden')) {
        try {
          const st = await fetch('/api/status').then((r2) => r2.json());
          if (estadoUI === 'detenido' && st.bot_running) {
            await cargarEstado(false);
          } else if (estadoUI === 'conectado' && !st.bot_running) {
            await cargarEstado(false);
          }
        } catch (e) {
          /* servidor ocupado o cerrando: se ignora y se reintenta */
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