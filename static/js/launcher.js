// launcher.js — Lógica del formulario de conexión (puente JS ↔ Python via pywebview)

(function () {
  'use strict';

  // Elementos del DOM
  const form = document.getElementById('connect-form');
  const connectedView = document.getElementById('connected-view');
  const errorMsg = document.getElementById('error-msg');
  const btnConnect = document.getElementById('btn-connect');
  const btnText = document.getElementById('btn-text');
  const btnLoading = document.getElementById('btn-loading');
  const btnExit = document.getElementById('btn-exit');
  const btnStop = document.getElementById('btn-stop');
  const btnLogout = document.getElementById('btn-logout');
  const toggleToken = document.getElementById('toggle-token');
  const toggleAdmin = document.getElementById('toggle-admin');
  const inputToken = document.getElementById('bot_token');
  const inputAdmin = document.getElementById('admin_id');
  const inputKick = document.getElementById('kick_hours');

  // Estado
  let tokenVisible = false;
  let adminVisible = false;

  // Utilidades
  function showError(msg) {
    errorMsg.textContent = msg;
    errorMsg.classList.remove('hidden');
  }

  function clearError() {
    errorMsg.textContent = '';
    errorMsg.classList.add('hidden');
  }

  function setLoading(loading) {
    btnConnect.disabled = loading;
    btnExit.disabled = loading;
    if (loading) {
      btnText.classList.add('hidden');
      btnLoading.classList.remove('hidden');
    } else {
      btnText.classList.remove('hidden');
      btnLoading.classList.add('hidden');
    }
  }

  function showConnected() {
    form.classList.add('hidden');
    connectedView.classList.remove('hidden');
  }

  function showForm() {
    connectedView.classList.add('hidden');
    form.classList.remove('hidden');
    clearError();
  }

  // Toggle mostrar/ocultar contraseña
  function setupToggle(toggleBtn, input) {
    toggleBtn.addEventListener('click', () => {
      const isPassword = input.type === 'password';
      input.type = isPassword ? 'text' : 'password';
      // Cambiar icono (ojo abierto/cerrado)
      toggleBtn.innerHTML = isPassword
        ? '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 114.24 4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>'
        : '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
    });
  }

  setupToggle(toggleToken, inputToken);
  setupToggle(toggleAdmin, inputAdmin);

  // Verificar sesión existente al cargar
  async function checkSession() {
    try {
      const resp = await fetch('/api/session');
      const data = await resp.json();
      if (data.has_session) {
        // La sesión existe: main_launcher (Python) ya la carga y arranca el bot
        // automáticamente. Acá solo reflejamos el estado, sin volver a conectar.
        await reflectSessionStatus();
      }
    } catch (e) {
      console.warn('No se pudo verificar sesión:', e);
    }
  }

  // Refleja el estado real del bot según /api/status (evita doble start)
  async function reflectSessionStatus() {
    setLoading(true);
    clearError();
    try {
      const resp = await fetch('/api/status');
      const data = await resp.json();
      if (data.bot_running) {
        showConnected();
      } else {
        showForm();
        showError('Hay una sesión guardada pero el bot no está corriendo. Usá Conectar para reanudar.');
        inputToken.focus();
      }
    } catch (e) {
      showError('Error de conexión: ' + e.message);
      showForm();
    } finally {
      setLoading(false);
    }
  }

  // Conectar (formulario manual)
  btnConnect.addEventListener('click', async () => {
    const token = inputToken.value.trim();
    const adminId = inputAdmin.value.trim();
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

    setLoading(true);
    clearError();

    try {
      const resp = await fetch('/api/connect', {
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
        showConnected();
      } else {
        showError(data.error || 'Error al conectar');
      }
    } catch (e) {
      showError('Error de conexión: ' + e.message);
    } finally {
      setLoading(false);
    }
  });

  // Salir (sin sesión) → cierra la app via pywebview
  btnExit.addEventListener('click', () => {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.quit_app();
    } else {
      window.close();
    }
  });

  // Detener bot (con sesión) → para bot, mantiene sesión
  btnStop.addEventListener('click', async () => {
    btnStop.disabled = true;
    btnStop.textContent = 'Deteniendo...';
    try {
      const resp = await fetch('/api/stop_bot', { method: 'POST' });
      const data = await resp.json();
      if (data.ok) {
        showForm();
      } else {
        showError(data.error || 'No se pudo detener');
      }
    } catch (e) {
      showError('Error: ' + e.message);
    } finally {
      btnStop.disabled = false;
      btnStop.textContent = 'Detener bot';
    }
  });

  // Cerrar sesión → borra session.enc, para bot, vuelve al formulario
  btnLogout.addEventListener('click', async () => {
    if (!confirm('¿Cerrar sesión? Se borrará la configuración guardada y se detendrá el bot.')) return;
    btnLogout.disabled = true;
    btnLogout.textContent = 'Cerrando...';
    try {
      // Detener bot primero
      await fetch('/api/stop_bot', { method: 'POST' });
      // Borrar sesión (el launcher Python maneja session.enc)
      if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.logout();
      }
      showForm();
      // Limpiar campos
      inputToken.value = '';
      inputAdmin.value = '';
      inputKick.value = '0';
    } catch (e) {
      showError('Error: ' + e.message);
    } finally {
      btnLogout.disabled = false;
      btnLogout.textContent = 'Cerrar sesión';
    }
  });

  // Splash listo: avisa a Python que la página está pintada
  function notifySplashReady() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.splash_listo) {
      window.pywebview.api.splash_listo();
    }
  }

  // Inicialización
  document.addEventListener('DOMContentLoaded', () => {
    notifySplashReady();
    checkSession();
    inputToken.focus();
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