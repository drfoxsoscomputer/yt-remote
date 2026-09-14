/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        fondo: '#1a1a2e',
        panel: '#1f1f3a',
        input: '#16162a',
        borde: '#2d2d4a',
        texto: '#e8e8e8',
        'texto-dim': '#8a8aa3',
        azul: '#229ed9',
        'azul-hover': '#1c88ce',
        verde: '#22c55e',
        'verde-hover': '#16a34a',
        rojo: '#ef4444',
        'rojo-hover': '#dc2626',
      },
      fontFamily: {
        sans: ['Segoe UI', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
}