// Сборка static/css/tailwind.min.css (Tailwind CSS 3.4.1, без CDN — приложение
// работает офлайн в локальной сети). Файл статический: класс, которого в нём
// нет, просто не применяется, поэтому после добавления новых utility-классов в
// static/js/*.js или templates/*.html стили нужно пересобрать:
//
//   cd <корень проекта>
//   npm install --no-save tailwindcss@3.4.1
//   printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' > /tmp/tw-in.css
//   npx tailwindcss -c scripts/tailwind/tailwind.config.js -i /tmp/tw-in.css -o static/css/tailwind.min.css --minify
//
// Переменные --slate-* и --indigo-* (значения по умолчанию) лежат в static/css/theme.css.
//
// Запускать из корня проекта (пути content считаются от текущей папки).
// Светлая тема и собственные классы — в static/css/theme.css, Tailwind их не трогает.
// Оттенки slate (поверхности, текст, границы) и indigo (акцент) — не готовые цвета, а переменные CSS: «Тема» в приложении
// (static/js/theme.js) подменяет их целиком, и вся разметка перекрашивается без единой правки классов.
// Значения по умолчанию (прежние цвета Tailwind) заданы в static/css/theme.css на :root.
const shades = ["50", "100", "200", "300", "400", "500", "600", "700", "800", "900", "950"];
const palette = (name) => Object.fromEntries(shades.map((n) => [n, `rgb(var(--${name}-${n}) / <alpha-value>)`]));

module.exports = {
  content: ["static/js/*.js", "templates/*.html"],
  // Скругление углов — через переменную --lc-rs (конструктор тем): 1 — как раньше, 0 — квадратные, 2 — очень круглые.
  theme: {
    extend: {
      colors: { slate: palette("slate"), indigo: palette("indigo") },
      borderRadius: {
        DEFAULT: "calc(0.25rem * var(--lc-rs, 1))", md: "calc(0.375rem * var(--lc-rs, 1))", lg: "calc(0.5rem * var(--lc-rs, 1))",
        xl: "calc(0.75rem * var(--lc-rs, 1))", "2xl": "calc(1rem * var(--lc-rs, 1))", "3xl": "calc(1.5rem * var(--lc-rs, 1))",
      },
    },
  },
  plugins: [],
  // Классы из прежней сборки, которых нет в исходниках буквально (часть имён
  // собирается строками в JS) — оставлены, чтобы пересборка ничего не потеряла.
  safelist: [
    "bg-amber-500/70", "bg-emerald-500/70", "bg-rose-500/70", "bg-sky-500/70",
    "bg-slate-600", "bg-slate-600/60", "text-amber-300/80",
    "blur", "grayscale", "transform", "shadow",
    "break-all", "contents", "flow-root", "inline-grid", "inline-table", "invisible",
    "italic", "line-through", "list-item", "lowercase", "overline", "underline", "resize", "static",
    "table-caption", "table-cell", "table-column", "table-column-group",
    "table-footer-group", "table-header-group", "table-row", "table-row-group",
    "m-1", "grid-cols-3", "md:grid-cols-2",
  ],
};
