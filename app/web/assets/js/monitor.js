// Экран "Мониторинг сервиса": показатели, текущий прогон, здоровье машины, Whisper и видеокарта,
// модель ИИ, воркеры, ошибки, последние проверки, история прогонов и данные каталога.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, link, refreshStatus, requestRun } from "./api.js";
import { CalendarModal } from "./calendar.js";
import { StopRunModal, useTick } from "./shell.js";
import { StorageModal } from "./storage.js";
import {
  ACC, GREEN, RED, YELLOW, ScoreBadge, ago, count, cueHide, cueShow, cueToggle, cx, durationText, fmt, html, mmss,
  plural, timeOf, tip, toast, touchInput, until, whenText, zoneSuffix,
} from "./lib.js";

const TRIGGER = { schedule: "По расписанию", manual: "Вручную", cli: "Из консоли" };
const RUN_STATUS = {
  running: { label: "Идёт", color: ACC, bg: "rgba(59,166,255,.16)" },
  done: { label: "Завершён", color: GREEN, bg: "rgba(62,207,142,.14)" },
  failed: { label: "Ошибка", color: RED, bg: "rgba(242,85,90,.14)" },
  interrupted: { label: "Прерван", color: YELLOW, bg: "rgba(245,179,60,.14)" },
};
// trailer и cover: записи старых прогонов, когда трейлеры искались на YouTube, а обложки отдельно
const STAGE_ERR = { metacritic: "Загрузка с Metacritic", summary_critic: "Резюме критиков", summary_user: "Резюме игроков", media: "Запрос к магазинам", steam: "Запрос к Steam", trailer: "Поиск трейлера", cover: "Обложка из Steam", letsplay: "Летсплей на YouTube" };
const STAGE_WORKER = { metacritic: "metacritic", summary_critic: "gemini", summary_user: "gemini", media: "media", steam: "media", letsplay: "letsplay" };
const WORKER_KIND = { metacritic: "Парсинг Metacritic", gemini: "Резюме отзывов", media: "Обложки и трейлеры", letsplay: "Летсплеи", scheduler: "Расписание" };
const WORKER_STAGE = {
  metacritic: "загрузка карточки", summary_critic: "резюме критиков", summary_user: "резюме игроков",
  media: "обложка и трейлер: Steam, Google Play", letsplay: "поиск ролика, звук, расшифровка, заключение",
};
const WORKER_STATE = { working: "Работает", waiting: "Ждёт модель", idle: "Простаивает" };
const LLM_REASON = {
  no_key: "Нет ключа в .env", keys_disabled: "Ключи выключены", keys_resting: "Ключи ждут квоту или пополнения",
  server_unavailable: "LM Studio не запущен", model_not_found: "Модели нет в LM Studio",
};
const KEY_STATE = { active: ["Активен", GREEN], cooldown: ["Ждёт квоту", YELLOW], disabled: ["Выключен", RED] };
const PROVIDER_HINT = { kie: "агрегатор, платно", google: "напрямую в Google", local: "бесплатно, без интернета" };
const PROVIDER_TITLE = { local: "LM Studio (на этом ПК)" };
// скорость по сравнению моделей на одних и тех же отзывах
const SPEED = {
  "gemini-3-8-flash": "около 5 с на резюме, ошибок в проверке не найдено",
  "gemini-3.8-flash": "около 5 с на резюме, ошибок в проверке не найдено",
  "gemma-4-12b-it-qat": "около 3-5 с; первый запрос после простоя около 8 с",
  "qwen/qwen3.5-9b": "около 4 с; первый запрос после простоя около 10 с",
  "openai/gpt-oss-20b": "около 2,5 с",
};
const TINT = {
  acc: ["rgba(43,123,255,.14)", "rgba(110,160,255,.22)", "var(--acc-2)"],
  green: ["rgba(62,207,142,.12)", "rgba(62,207,142,.26)", "var(--green)"],
  violet: ["rgba(110,91,255,.14)", "rgba(110,91,255,.28)", "var(--violet)"],
  red: ["rgba(242,85,90,.12)", "rgba(242,85,90,.26)", "var(--red)"],
  yellow: ["rgba(245,179,60,.12)", "rgba(245,179,60,.26)", "var(--yellow)"],
};
const HISTORY_PER_PAGE = 20;
// стрелки листания страниц: история прогонов и ошибки
const PREV = html`<svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M15 20.67C14.81 20.67 14.62 20.6 14.47 20.45L7.95 13.93C6.89 12.87 6.89 11.13 7.95 10.07L14.47 3.55C14.76 3.26 15.24 3.26 15.53 3.55C15.82 3.84 15.82 4.32 15.53 4.61L9.01 11.13C8.53 11.61 8.53 12.39 9.01 12.87L15.53 19.39C15.82 19.68 15.82 20.16 15.53 20.45C15.38 20.59 15.19 20.67 15 20.67Z" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/></svg>`;
const NEXT = html`<svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8.91 20.67C8.72 20.67 8.53 20.6 8.38 20.45C8.09 20.16 8.09 19.68 8.38 19.39L14.9 12.87C15.38 12.39 15.38 11.61 14.9 11.13L8.38 4.61C8.09 4.32 8.09 3.84 8.38 3.55C8.67 3.26 9.15 3.26 9.44 3.55L15.96 10.07C16.47 10.58 16.76 11.27 16.76 12C16.76 12.73 16.48 13.42 15.96 13.93L9.44 20.45C9.29 20.59 9.1 20.67 8.91 20.67Z" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/></svg>`;
// значок "обновить" из дизайн-системы: круг с разрывом и стрелкой сверху
const REFRESH = "M12 22.75C6.8 22.75 2.58 18.52 2.58 13.33C2.58 8.14 6.8 3.9 12 3.9C13.07 3.9 14.11 4.05 15.11 4.36C15.51 4.48 15.73 4.9 15.61 5.3C15.49 5.7 15.07 5.92 14.67 5.8C13.82 5.54 12.92 5.4 12 5.4C7.63 5.4 4.08 8.95 4.08 13.32C4.08 17.69 7.63 21.24 12 21.24C16.37 21.24 19.92 17.69 19.92 13.32C19.92 11.74 19.46 10.22 18.59 8.92C18.36 8.58 18.45 8.11 18.8 7.88C19.14 7.65 19.61 7.74 19.84 8.09C20.88 9.64 21.43 11.45 21.43 13.33C21.42 18.52 17.2 22.75 12 22.75Z"
  + "M16.13 6.07C15.92 6.07 15.71 5.98 15.56 5.81L12.67 2.49C12.4 2.18 12.43 1.7 12.74 1.43C13.05 1.16 13.53 1.19 13.8 1.5L16.69 4.82C16.96 5.13 16.93 5.61 16.62 5.88C16.49 6.01 16.31 6.07 16.13 6.07Z"
  + "M12.76 8.53C12.53 8.53 12.3 8.42 12.15 8.22C11.91 7.89 11.98 7.42 12.31 7.17L15.68 4.71C16.01 4.46 16.48 4.54 16.73 4.87C16.98 5.2 16.9 5.67 16.57 5.92L13.2 8.39C13.07 8.49 12.92 8.53 12.76 8.53Z";

const since = (a, b) => (a ? Math.max(0, ((b ? Date.parse(b) : Date.now()) - Date.parse(a)) / 1000) : null);
const pct = (done, total) => (total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0);
const loadColor = (v, warn, bad) => (v == null ? "var(--t9)" : v >= bad ? RED : v >= warn ? YELLOW : GREEN);
const decimal = (v) => String(v).replace(".", ",");
const mbps = (bytes) => (bytes == null ? "—" : bytes >= 1024 * 1024 ? `${decimal((bytes / 1024 / 1024).toFixed(1))} МБ/с` : `${Math.round(bytes / 1024)} КБ/с`);

/** Изменение к прошлым суткам: доля в процентах и что считать улучшением. */
function delta(now, before, { less = false } = {}) {
  if (!before) return null;
  const change = Math.round(((now - before) / before) * 100);
  if (change === 0) return null;
  const good = less ? change < 0 : change > 0;
  return { text: `${change > 0 ? "+" : "−"}${Math.abs(change)}%`, arrow: change > 0 ? "↑" : "↓", color: good ? GREEN : RED };
}

function SecHead({ icon, tint = "acc", title, sub, children }) {
  const [bg, , fg] = TINT[tint];
  return html`<div class="sec-head">
    <span class="sec-icon" style=${{ background: bg }}>
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">${icon(fg)}</svg>
    </span>
    <div><div class="sec-title">${title}</div>${sub && html`<div class="sec-sub">${sub}</div>`}</div>
    ${children && html`<div class="sec-right">${children}</div>`}
  </div>`;
}
// иконки разделов из дизайн-системы: залитые контуры с тонкой обводкой того же цвета
const solid = (d) => (c) => html`<path d=${d} fill=${c} stroke=${c} stroke-width="0.5" stroke-linejoin="round"/>`;
const RING = "M12 22.75C6.07 22.75 1.25 17.93 1.25 12C1.25 6.07 6.07 1.25 12 1.25C17.93 1.25 22.75 6.07 22.75 12C22.75 17.93 17.93 22.75 12 22.75ZM12 2.75C6.9 2.75 2.75 6.9 2.75 12C2.75 17.1 6.9 21.25 12 21.25C17.1 21.25 21.25 17.1 21.25 12C21.25 6.9 17.1 2.75 12 2.75Z";
const CHECK = "M10.58 15.58C10.38 15.58 10.19 15.5 10.05 15.36L7.22 12.53C6.93 12.24 6.93 11.76 7.22 11.47C7.51 11.18 7.99 11.18 8.28 11.47L10.58 13.77L15.72 8.63C16.01 8.34 16.49 8.34 16.78 8.63C17.07 8.92 17.07 9.4 16.78 9.69L11.11 15.36C10.97 15.5 10.78 15.58 10.58 15.58Z";
const HANDS = "M15.71 15.93C15.58 15.93 15.45 15.9 15.33 15.82L12.23 13.97C11.46 13.51 10.89 12.5 10.89 11.61V7.51C10.89 7.1 11.23 6.76 11.64 6.76C12.05 6.76 12.39 7.1 12.39 7.51V11.61C12.39 11.97 12.69 12.5 13 12.68L16.1 14.53C16.46 14.74 16.57 15.2 16.36 15.56C16.21 15.8 15.96 15.93 15.71 15.93Z";
const I = {
  bolt: solid("M9.99 22.75C9.79 22.75 9.63 22.71 9.51 22.66C9.11 22.51 8.43 22.02 8.43 20.47V14.02H6.09C4.75 14.02 4.27 13.39 4.1 13.02C3.93 12.64 3.78 11.87 4.66 10.86L12.23 2.26C13.25 1.1 14.08 1.18 14.48 1.33C14.88 1.48 15.56 1.97 15.56 3.52V9.97H17.9C19.24 9.97 19.72 10.6 19.89 10.97C20.06 11.35 20.21 12.12 19.33 13.13L11.76 21.73C11.05 22.54 10.43 22.75 9.99 22.75ZM13.93 2.74C13.9 2.78 13.69 2.88 13.36 3.26L5.79 11.86C5.51 12.18 5.47 12.38 5.47 12.42C5.49 12.43 5.67 12.53 6.09 12.53H9.18C9.59 12.53 9.93 12.87 9.93 13.28V20.48C9.93 20.98 10.02 21.2 10.06 21.26C10.09 21.22 10.3 21.12 10.63 20.74L18.2 12.14C18.48 11.82 18.52 11.62 18.52 11.58C18.5 11.57 18.32 11.47 17.9 11.47H14.81C14.4 11.47 14.06 11.13 14.06 10.72V3.52C14.07 3.02 13.97 2.81 13.93 2.74Z"),
  grid: solid("M7 10.75H5C2.58 10.75 1.25 9.42 1.25 7V5C1.25 2.58 2.58 1.25 5 1.25H7C9.42 1.25 10.75 2.58 10.75 5V7C10.75 9.42 9.42 10.75 7 10.75ZM5 2.75C3.42 2.75 2.75 3.42 2.75 5V7C2.75 8.58 3.42 9.25 5 9.25H7C8.58 9.25 9.25 8.58 9.25 7V5C9.25 3.42 8.58 2.75 7 2.75H5Z"
    + "M19 10.75H17C14.58 10.75 13.25 9.42 13.25 7V5C13.25 2.58 14.58 1.25 17 1.25H19C21.42 1.25 22.75 2.58 22.75 5V7C22.75 9.42 21.42 10.75 19 10.75ZM17 2.75C15.42 2.75 14.75 3.42 14.75 5V7C14.75 8.58 15.42 9.25 17 9.25H19C20.58 9.25 21.25 8.58 21.25 7V5C21.25 3.42 20.58 2.75 19 2.75H17Z"
    + "M19 22.75H17C14.58 22.75 13.25 21.42 13.25 19V17C13.25 14.58 14.58 13.25 17 13.25H19C21.42 13.25 22.75 14.58 22.75 17V19C22.75 21.42 21.42 22.75 19 22.75ZM17 14.75C15.42 14.75 14.75 15.42 14.75 17V19C14.75 20.58 15.42 21.25 17 21.25H19C20.58 21.25 21.25 20.58 21.25 19V17C21.25 15.42 20.58 14.75 19 14.75H17Z"
    + "M7 22.75H5C2.58 22.75 1.25 21.42 1.25 19V17C1.25 14.58 2.58 13.25 5 13.25H7C9.42 13.25 10.75 14.58 10.75 17V19C10.75 21.42 9.42 22.75 7 22.75ZM5 14.75C3.42 14.75 2.75 15.42 2.75 17V19C2.75 20.58 3.42 21.25 5 21.25H7C8.58 21.25 9.25 20.58 9.25 19V17C9.25 15.42 8.58 14.75 7 14.75H5Z"),
  clock: solid(RING + HANDS),
  // в дизайн-системе наконечник стрелки стоит не на конце дуги; поворот и масштаб взяты из макета, где это исправлено
  history: (c) => html`<path d="M4.21 7.5A9 9 0 1 1 3.14 13.56" stroke=${c} stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <g transform="translate(3.15 9) rotate(112.6) translate(-16.5 -5.8)">${solid("M16.13 6.07C15.92 6.07 15.71 5.98 15.56 5.81L12.67 2.49C12.4 2.18 12.43 1.7 12.74 1.43C13.05 1.16 13.53 1.19 13.8 1.5L16.69 4.82C16.96 5.13 16.93 5.61 16.62 5.88C16.49 6.01 16.31 6.07 16.13 6.07Z"
      + "M12.76 8.53C12.53 8.53 12.3 8.42 12.15 8.22C11.91 7.89 11.98 7.42 12.31 7.17L15.68 4.71C16.01 4.46 16.48 4.54 16.73 4.87C16.98 5.2 16.9 5.67 16.57 5.92L13.2 8.39C13.07 8.49 12.92 8.53 12.76 8.53Z")(c)}</g>
    <g transform="translate(12 12) scale(0.88) translate(-12 -12)">${solid(HANDS)(c)}</g>`,
  // прежняя контурная микросхема; толщина 2 — столько же, сколько у залитых иконок рядом
  // (у них полоса заливки 1.5 плюс обводка 0.5)
  chip: (c) => html`<rect x="5.4" y="5.4" width="13.2" height="13.2" rx="3" stroke=${c} stroke-width="2"/>
    <rect x="9.4" y="9.4" width="5.2" height="5.2" rx="1.4" stroke=${c} stroke-width="2"/>
    <path d="M9.2 2.6v2.8M14.8 2.6v2.8M9.2 18.6v2.8M14.8 18.6v2.8M2.6 9.2h2.8M2.6 14.8h2.8M18.6 9.2h2.8M18.6 14.8h2.8"
      stroke=${c} stroke-width="2" stroke-linecap="round"/>`,
  // у значка ошибки в дизайн-системе смещённая область рисования: сдвигаем его в 0 0 24 24
  warn: (c) => html`<g transform="translate(-15.97 -36.94)">${solid("M28 51.75C27.59 51.75 27.25 51.41 27.25 51V46C27.25 45.59 27.59 45.25 28 45.25C28.41 45.25 28.75 45.59 28.75 46V51C28.75 51.41 28.41 51.75 28 51.75Z"
    + "M28 55C27.45 55 27 54.55 27 54C27 53.45 27.45 53 28 53C28.55 53 29 53.45 29 54C29 54.55 28.55 55 28 55Z"
    + "M34.06 59.16H21.94C19.99 59.16 18.5 58.45 17.74 57.17C16.99 55.89 17.09 54.24 18.04 52.53L24.1 41.63C25.1 39.83 26.48 38.84 28 38.84C29.52 38.84 30.9 39.83 31.9 41.63L37.96 52.54C38.91 54.25 39.02 55.89 38.26 57.18C37.5 58.45 36.01 59.16 34.06 59.16ZM28 40.34C27.06 40.34 26.14 41.06 25.41 42.36L19.36 53.27C18.68 54.49 18.57 55.61 19.04 56.42C19.51 57.23 20.55 57.67 21.95 57.67H34.07C35.47 57.67 36.5 57.23 36.98 56.42C37.46 55.61 37.34 54.5 36.66 53.27L30.59 42.36C29.86 41.06 28.94 40.34 28 40.34Z")(c)}</g>`,
  box: (c) => html`<path d="M3.2 7.4h17.6v12.2a1.6 1.6 0 0 1-1.6 1.6H4.8a1.6 1.6 0 0 1-1.6-1.6V7.4Z" stroke=${c} stroke-width="2" stroke-linejoin="round"/><path d="M2.2 3.4h19.6v4H2.2zM9.6 12.2h4.8" stroke=${c} stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`,
};

// что возьмёт следующий прогон: настройки задаются в окне расписания
// подсказка кнопки: сколько новинок возьмёт прогон и попадут ли в него старые игры, по настройкам и базе
export function staleWhen(iso, now = Date.now()) {
  const days = Math.max(0, (Date.parse(iso) - now) / 86400000);
  if (days < 1) return `через ${Math.max(1, Math.round(days * 24))} ч`;
  const d = Math.round(days);
  return `через ${d} ${plural(d, "день", "дня", "дней")}`;
}

function runHint(run, stale) {
  const games = (run && run.games) || 20;
  const lines = [`Новые игры: до ${games} ${plural(games, "игры", "игр", "игр")} из новинок Metacritic, обработанные сегодня пропускаются.`];
  if (!stale || !stale.enabled) lines.push("Старые игры (уже в базе) не обновляются: выключено в настройках.");
  else {
    const every = `раз в ${stale.days} ${plural(stale.days, "день", "дня", "дней")}`;
    lines.push(stale.due
      ? `Старые игры обновляются ${every}: в этот прогон попадут ${Math.min(stale.due, stale.games)} из ${stale.due}, которым пора.`
      : `Старые игры обновляются ${every}: в этот прогон не попадут${stale.next_at ? `, следующей пора ${staleWhen(stale.next_at)}` : ""}.`);
  }
  // два абзаца: новые игры и старые (подсказка понимает переносы строк)
  return lines.join("\n\n");
}

function Header({ status, now, starting, onStart, onStop, onCalendar }) {
  const data = status.data;
  const s = data.scheduler;
  const running = !!data.run || s.run_in_progress;
  const cooldown = s.manual_available_at ? Math.max(0, (Date.parse(s.manual_available_at) - now) / 1000) : 0;
  let btn;
  if (!s.enabled) btn = { label: "Расписание выключено", off: true, hint: "Сервис запущен с SCHEDULER_ENABLED=false: прогоны только из консоли" };
  else if (starting) btn = { label: "Запускается…", soft: true };
  else if (running) btn = { label: "Остановить прогон", danger: true, hint: "Текущий шаг доделается, прогон будет помечен прерванным; следующий пойдёт по расписанию" };
  else if (cooldown > 0) btn = { label: "Можно через " + mmss(cooldown), off: true, hint: "Между ручными запусками не меньше 5 минут, чтобы случайные клики не тратили лимиты модели" };
  else btn = { label: "Запустить сейчас", primary: true, hint: runHint(s.run, data.stale) };
  // во время прогона показываем, с какого времени он идёт, а не "следующий запуск" с временем текущего
  const next = !s.enabled ? { label: "Следующий запуск", at: "выключено", in: "" }
    : running ? { label: "Прогон идёт с", at: data.run ? timeOf(data.run.started_at) + zoneSuffix() : "—", in: data.run ? durationText(since(data.run.started_at)) : "" }
    : !s.active ? { label: "Следующий запуск", at: "на паузе", in: "только вручную" }
    // "сегодня" в плашке лишнее: день показываем, только если запуск не сегодня
    : s.next_run_at ? { label: "Следующий запуск", at: whenText(s.next_run_at, now).replace(/^сегодня,\s*/, ""), in: until(s.next_run_at, now) }
    : { label: "Следующий запуск", at: "—", in: "" };
  return html`<div class="mon-head">
    <div class="mon-title-box">
      <h1 class="page-title">Мониторинг сервиса</h1>
      <p class="page-sub">Парсинг, резюме и летсплеи в реальном времени. Обновление через поток событий.</p>
    </div>
    <div class="mon-actions">
      ${!status.online && html`<div class="pill pill-live" ...${tip(status.updatedAt ? "Последние данные: " + ago(new Date(status.updatedAt).toISOString(), now) : "Данных ещё нет")}>
        <span class="live-dot" style=${{ background: YELLOW, boxShadow: `0 0 8px ${YELLOW}` }}></span>
        <span class="pill-label" style=${{ color: YELLOW }}>Переподключение</span>
      </div>`}
      <div class="pill">
        <span class="pill-muted">${next.label}</span>
        <button type="button" class="pill-cal" onClick=${onCalendar} ...${tip("Календарь: что было по дням и что запланировано")} aria-label="Открыть календарь">
        <svg class="pill-icon" width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M8 5.75C7.59 5.75 7.25 5.41 7.25 5V2C7.25 1.59 7.59 1.25 8 1.25C8.41 1.25 8.75 1.59 8.75 2V5C8.75 5.41 8.41 5.75 8 5.75ZM16 5.75C15.59 5.75 15.25 5.41 15.25 5V2C15.25 1.59 15.59 1.25 16 1.25C16.41 1.25 16.75 1.59 16.75 2V5C16.75 5.41 16.41 5.75 16 5.75ZM20.5 9.84H3.5C3.09 9.84 2.75 9.5 2.75 9.09C2.75 8.68 3.09 8.34 3.5 8.34H20.5C20.91 8.34 21.25 8.68 21.25 9.09C21.25 9.5 20.91 9.84 20.5 9.84ZM16 22.75H8C4.35 22.75 2.25 20.65 2.25 17V8.5C2.25 4.85 4.35 2.75 8 2.75H16C19.65 2.75 21.75 4.85 21.75 8.5V17C21.75 20.65 19.65 22.75 16 22.75ZM8 4.25C5.14 4.25 3.75 5.64 3.75 8.5V17C3.75 19.86 5.14 21.25 8 21.25H16C18.86 21.25 20.25 19.86 20.25 17V8.5C20.25 5.64 18.86 4.25 16 4.25H8Z"
            fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/>
          <circle cx="8.5" cy="13.5" r="1" fill="currentColor"/><circle cx="12" cy="13.5" r="1" fill="currentColor"/><circle cx="15.5" cy="13.5" r="1" fill="currentColor"/>
          <circle cx="8.5" cy="17" r="1" fill="currentColor"/><circle cx="12" cy="17" r="1" fill="currentColor"/><circle cx="15.5" cy="17" r="1" fill="currentColor"/>
        </svg>
        </button>
        <span class="pill-time">${next.at}</span>
        ${next.in && html`<span class="pill-chip">${next.in}</span>`}
      </div>
      <button class=${cx("btn", "run-btn", btn.primary && "is-primary", btn.danger && "btn-danger is-danger", btn.soft && "is-soft", btn.off && "is-off")}
        aria-disabled=${!btn.primary && !btn.danger} onClick=${() => (btn.primary ? onStart() : btn.danger ? onStop() : null)} ...${tip(btn.hint)}>
        ${btn.primary && html`<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7.87 21.28C7.08 21.28 6.33 21.09 5.67 20.71C4.11 19.81 3.25 17.98 3.25 15.57V8.44C3.25 6.02 4.11 4.2 5.67 3.3C7.23 2.4 9.24 2.57 11.34 3.78L17.51 7.34C19.6 8.55 20.76 10.21 20.76 12.01C20.76 13.81 19.61 15.47 17.51 16.68L11.34 20.24C10.13 20.93 8.95 21.28 7.87 21.28ZM7.87 4.22C7.33 4.22 6.85 4.34 6.42 4.59C5.34 5.21 4.75 6.58 4.75 8.44V15.56C4.75 17.42 5.34 18.78 6.42 19.41C7.5 20.04 8.98 19.86 10.59 18.93L16.76 15.37C18.37 14.44 19.26 13.25 19.26 12C19.26 10.75 18.37 9.56 16.76 8.63L10.59 5.07C9.61 4.51 8.69 4.22 7.87 4.22Z" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/></svg>`}
        <span>${btn.label}</span>
      </button>
    </div>
  </div>`;
}

// иконки показателей из дизайн-системы: залитые контуры, у резюме и воркеров линия толщиной 2
const KPI_ICON = {
  db: { d: [
    "M18.32 12.75H15C12.51 12.75 11.25 11.34 11.25 8.55V5.68C11.25 4.66 11.37 3.2 12.43 2.4C13.32 1.74 14.6 1.69 16.49 2.24C18.97 2.96 21.04 5.03 21.76 7.51C22.31 9.39 22.26 10.68 21.6 11.56C20.8 12.63 19.34 12.75 18.32 12.75ZM14.28 3.36C13.87 3.36 13.55 3.44 13.34 3.6C12.95 3.89 12.76 4.57 12.76 5.68V8.56C12.76 10.8 13.62 11.26 15.01 11.26H18.33C19.43 11.26 20.11 11.07 20.41 10.68C20.76 10.22 20.73 9.3 20.34 7.95C19.76 5.98 18.06 4.27 16.09 3.7C15.35 3.47 14.75 3.36 14.28 3.36Z",
    "M11.07 22.75C10.54 22.75 10 22.71 9.46 22.62C5.37 21.96 2.04 18.64 1.38 14.55C0.53 9.29 3.92 4.33 9.11 3.27C9.52 3.19 9.91 3.45 10 3.85C10.08 4.26 9.82 4.65 9.42 4.74C5.03 5.64 2.15 9.84 2.88 14.31C3.44 17.77 6.25 20.58 9.71 21.14C14.2 21.86 18.39 18.97 19.28 14.56C19.36 14.15 19.76 13.89 20.16 13.97C20.57 14.05 20.83 14.45 20.75 14.85C19.8 19.52 15.72 22.75 11.07 22.75Z",
  ] },
  check: { d: [
    "M12 22.75C6.07 22.75 1.25 17.93 1.25 12C1.25 6.07 6.07 1.25 12 1.25C17.93 1.25 22.75 6.07 22.75 12C22.75 17.93 17.93 22.75 12 22.75ZM12 2.75C6.9 2.75 2.75 6.9 2.75 12C2.75 17.1 6.9 21.25 12 21.25C17.1 21.25 21.25 17.1 21.25 12C21.25 6.9 17.1 2.75 12 2.75Z",
    "M10.58 15.58C10.38 15.58 10.19 15.5 10.05 15.36L7.22 12.53C6.93 12.24 6.93 11.76 7.22 11.47C7.51 11.18 7.99 11.18 8.28 11.47L10.58 13.77L15.72 8.63C16.01 8.34 16.49 8.34 16.78 8.63C17.07 8.92 17.07 9.4 16.78 9.69L11.11 15.36C10.97 15.5 10.78 15.58 10.58 15.58Z",
  ] },
  gem: { line: true, d: [
    "M9.4 1.2C9.4 4.32 14.48 9.4 17.6 9.4C14.48 9.4 9.4 14.48 9.4 17.6C9.4 14.48 4.32 9.4 1.2 9.4C4.32 9.4 9.4 4.32 9.4 1.2Z",
    "M18.4 13.4C18.4 15.22 21.38 18.2 23.2 18.2C21.38 18.2 18.4 21.18 18.4 23C18.4 21.18 15.42 18.2 13.6 18.2C15.42 18.2 18.4 15.22 18.4 13.4Z",
  ] },
  warn: { vb: "15.97 36.94 24 24", d: [
    "M28 51.75C27.59 51.75 27.25 51.41 27.25 51V46C27.25 45.59 27.59 45.25 28 45.25C28.41 45.25 28.75 45.59 28.75 46V51C28.75 51.41 28.41 51.75 28 51.75Z",
    "M28 55C27.45 55 27 54.55 27 54C27 53.45 27.45 53 28 53C28.55 53 29 53.45 29 54C29 54.55 28.55 55 28 55Z",
    "M34.06 59.16H21.94C19.99 59.16 18.5 58.45 17.74 57.17C16.99 55.89 17.09 54.24 18.04 52.53L24.1 41.63C25.1 39.83 26.48 38.84 28 38.84C29.52 38.84 30.9 39.83 31.9 41.63L37.96 52.54C38.91 54.25 39.02 55.89 38.26 57.18C37.5 58.45 36.01 59.16 34.06 59.16ZM28 40.34C27.06 40.34 26.14 41.06 25.41 42.36L19.36 53.27C18.68 54.49 18.57 55.61 19.04 56.42C19.51 57.23 20.55 57.67 21.95 57.67H34.07C35.47 57.67 36.5 57.23 36.98 56.42C37.46 55.61 37.34 54.5 36.66 53.27L30.59 42.36C29.86 41.06 28.94 40.34 28 40.34Z",
  ] },
  clock: { d: [
    "M12 22.75C6.07 22.75 1.25 17.93 1.25 12C1.25 6.07 6.07 1.25 12 1.25C17.93 1.25 22.75 6.07 22.75 12C22.75 17.93 17.93 22.75 12 22.75ZM12 2.75C6.9 2.75 2.75 6.9 2.75 12C2.75 17.1 6.9 21.25 12 21.25C17.1 21.25 21.25 17.1 21.25 12C21.25 6.9 17.1 2.75 12 2.75Z",
    "M15.71 15.93C15.58 15.93 15.45 15.9 15.33 15.82L12.23 13.97C11.46 13.51 10.89 12.5 10.89 11.61V7.51C10.89 7.1 11.23 6.76 11.64 6.76C12.05 6.76 12.39 7.1 12.39 7.51V11.61C12.39 11.97 12.69 12.5 13 12.68L16.1 14.53C16.46 14.74 16.57 15.2 16.36 15.56C16.21 15.8 15.96 15.93 15.71 15.93Z",
  ] },
  loop: { line: true, d: ["M22 12C22 17.52 17.52 22 12 22C6.48 22 3.11 16.44 3.11 16.44M3.11 21.44V16.44H7.63M2 12C2 6.48 6.44 2 12 2C18.67 2 22 7.56 22 7.56M17.56 7.56H22V2.56"] },
};

function KpiIcon({ name, color }) {
  const icon = KPI_ICON[name];
  return html`<svg width="22" height="22" viewBox=${icon.vb || "0 0 24 24"} fill="none" aria-hidden="true">
    ${/* контурные иконки рисуются обводкой 2.1: столько же, сколько намеряно у залитых (полоса 1.5 плюс обводка 0.5) */ ""}
    ${icon.d.map((d) => html`<path d=${d} fill=${icon.line ? "none" : color} stroke=${color} stroke-width=${icon.line ? 2.1 : 0.5}
      stroke-linecap="round" stroke-linejoin="round"/>`)}
  </svg>`;
}

// расписание анимации из макета: на каждом кадре семисекундного цикла отрисована такая доля линии
const SPARK_STOPS = [[0, 0], [2.63, 0.17], [5.25, 0.84], [7.88, 2.13], [10.5, 4.12], [13.13, 6.89], [15.75, 10.48],
  [18.38, 14.94], [21, 20.31], [23.63, 26.62], [26.25, 33.93], [28.88, 42.24], [31.5, 51.6], [34.13, 62.03],
  [36.75, 73.56], [39.38, 86.21], [42, 100]];
// спарклайны карточек скопированы из макета: путь, цвет, свечение и доля открытой заливки на каждом кадре.
// Линии декоративные, данные прогонов в них не участвуют
const DESIGN_SPARKS = {
  games: {
    d: "M3,41 C10,36 18,45 24,39 L35,33 L46,37 C54,28 62,35 70,29 L79,22 L88,26 C96,21 103,16 110,13 L124,4",
    color: "#3aa6d8", glow: "rgba(58,166,216,.55)",
    scale: [.0227, .0242, .03, .0423, .0627, .0908, .1269, .1713, .2185, .2763, .3469, .4211, .5156, .6018, .7069, .8188, .9394],
  },
  processed: {
    d: "M3,48 C4.0,47.3 7.0,44.0 9,44 C11.0,44.0 13.0,48.5 15,48 C17.0,47.5 19.7,43.7 21,41 C22.3,38.3 22.0,32.5 23,32"
      + " C24.0,31.5 25.3,38.3 27,38 C28.7,37.7 31.0,30.0 33,30 C35.0,30.0 37.3,35.7 39,38 C40.7,40.3 41.3,44.3 43,44"
      + " C44.7,43.7 47.0,36.3 49,36 C51.0,35.7 52.7,44.7 55,42 C57.3,39.3 60.7,22.5 63,20 C65.3,17.5 67.0,27.2 69,27"
      + " C71.0,26.8 72.7,18.5 75,19 C77.3,19.5 80.7,29.3 83,30 C85.3,30.7 86.8,25.3 89,23 C91.2,20.7 93.7,17.8 96,16"
      + " C98.3,14.2 100.7,11.8 103,12 C105.3,12.2 107.7,17.8 110,17 C112.3,16.2 114.7,9.2 117,7 C119.3,4.8 122.8,4.5 124,4",
    color: "var(--green)", glow: "rgba(62,207,142,.5)",
    scale: [.0227, .0248, .0325, .0478, .0757, .108, .1484, .1682, .2066, .2667, .3262, .3996, .4565, .5393, .6467, .7866, .9394],
  },
  summaries: {
    d: "M3,40 C4.8,39.7 10.7,39.0 14,38 C17.3,37.0 19.7,34.3 23,34 C26.3,33.7 30.2,37.3 34,36 C37.8,34.7 42.0,26.7 46,26"
      + " C50.0,25.3 54.7,32.7 58,32 C61.3,31.3 62.3,22.7 66,22 C69.7,21.3 76.0,28.7 80,28 C84.0,27.3 86.0,19.0 90,18"
      + " C94.0,17.0 99.5,23.3 104,22 C108.5,20.7 113.7,12.7 117,10 C120.3,7.3 122.8,6.7 124,6",
    color: "var(--violet)", glow: "rgba(110,91,255,.5)",
    scale: [.0227, .0246, .0319, .046, .0678, .0979, .1341, .1798, .2367, .2946, .3582, .4367, .5008, .6047, .6982, .8239, .9394],
  },
  failed: {
    d: "M3,43 C6.5,42.8 18.7,42.2 24,42 C29.3,41.8 31.5,45.7 35,42 C38.5,38.3 41.7,20.0 45,20 C48.3,20.0 51.5,38.3 55,42"
      + " C58.5,45.7 61.2,41.7 66,42 C70.8,42.3 79.2,49.7 84,44 C88.8,38.3 91.3,8.0 95,8 C98.7,8.0 101.2,38.2 106,44"
      + " C110.8,49.8 121.0,43.2 124,43",
    color: "var(--red)", glow: "rgba(242,85,90,.45)",
    scale: [.0227, .0254, .0359, .0563, .0877, .1313, .188, .2559, .2945, .3301, .3836, .4593, .6005, .6739, .7308, .7771, .9394],
  },
  time: {
    d: "M3,30 C10,18 18,14 26,22 C34,30 38,44 48,44 C58,44 62,26 72,20 C82,14 88,22 96,32 C104,42 112,38 124,22",
    color: "var(--yellow)", glow: "rgba(245,179,60,.45)",
    scale: [.0227, .0238, .028, .0367, .0513, .0747, .1125, .1659, .215, .2596, .318, .412, .4806, .576, .6942, .8161, .9394],
  },
  workers: {
    d: "M3,42 L10,30 L17,44 L24,28 L31,42 L38,26 L45,40 L52,24 L59,38 L66,22 L73,36 L80,18 L87,32 L94,14 L101,28 L108,10 L116,22 L124,4",
    color: "#2563eb", glow: "rgba(37,99,235,.65)",
    scale: [.0227, .0246, .0318, .0459, .0676, .0953, .1299, .1685, .2189, .2753, .3441, .4187, .503, .5942, .6946, .8038, .9394],
  },
};
for (const [key, spark] of Object.entries(DESIGN_SPARKS)) {
  spark.area = `${spark.d} L124,52 L3,52 Z`;
  spark.reveal = `@keyframes spark-${key}-reveal{`
    + SPARK_STOPS.slice(0, -1).map(([time], i) => `${time}%{transform:scaleX(${spark.scale[i]});}`).join("")
    + `42%,100%{transform:scaleX(${spark.scale[16]});}}`;
}

/** Спарклайн по макету: линия дорисовывается, заливка открывается следом, точка едет по её кончику.
    Стили стоят на самих элементах, как в макете; путь точки задан здесь, а не внутри кадров. */
function Spark({ id, chart, color }) {
  const url = (name) => `url(#${id}-${name})`;
  const glow = chart.glow || `color-mix(in srgb, ${color} 55%, transparent)`;
  const cycle = "7s linear infinite";
  return html`<svg class="kpi-spark" width="96" height="38" viewBox="0 0 132 52" fill="none" aria-hidden="true">
    <defs>
      <linearGradient id=${`${id}-area`} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color=${color} stop-opacity=".30"/><stop offset="60%" stop-color=${color} stop-opacity=".08"/><stop offset="100%" stop-color=${color} stop-opacity="0"/>
      </linearGradient>
      <linearGradient id=${`${id}-line`} x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color=${color} stop-opacity=".45"/><stop offset="55%" stop-color=${color} stop-opacity=".8"/><stop offset="100%" stop-color=${color} stop-opacity="1"/>
      </linearGradient>
      <linearGradient id=${`${id}-fat`} x1="0" y1="0" x2="1" y2="0">
        <stop offset="35%" stop-color=${color} stop-opacity="0"/><stop offset="100%" stop-color=${color} stop-opacity=".9"/>
      </linearGradient>
      <linearGradient id=${`${id}-fade`} x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="#fff" stop-opacity="0"/><stop offset="14%" stop-color="#fff" stop-opacity="1"/>
      </linearGradient>
      <mask id=${`${id}-reveal`} maskUnits="userSpaceOnUse" x="-8" y="-14" width="148" height="80">
        <rect class="spark-mask" x="0" y="-14" width="132" height="80" fill="#fff"
          style=${`animation:${id}-reveal ${cycle};transform-box:view-box;transform-origin:left center;`}/>
      </mask>
      <mask id=${`${id}-edge`} maskUnits="userSpaceOnUse" x="-8" y="-14" width="148" height="80">
        <rect x="0" y="-14" width="140" height="80" fill=${url("fade")}/>
      </mask>
    </defs>
    <g mask=${url("edge")}>
      <path class="spark-area" d=${chart.area} fill=${url("area")} stroke="none" mask=${url("reveal")}
        style=${`animation:sparkFade ${cycle};`}/>
      <path class="spark-line" d=${chart.d} stroke=${url("line")} stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" pathLength="100"
        style=${`stroke-dasharray:100;animation:sparkDraw ${cycle}, sparkFade ${cycle};filter:drop-shadow(0 -3px 7px ${glow});`}/>
      <path class="spark-line" d=${chart.d} stroke=${url("fat")} stroke-width="3" stroke-linecap="round" stroke-linejoin="round" fill="none" pathLength="100"
        style=${`stroke-dasharray:100;animation:sparkDraw ${cycle}, sparkFade ${cycle};filter:drop-shadow(0 -4px 9px ${glow});`}/>
      <circle class="spark-tip" cx="0" cy="0" r="2.8" fill=${color}
        style=${`offset-path:path('${chart.d}');offset-rotate:0deg;offset-distance:0%;animation:sparkTip ${cycle}, sparkFade ${cycle};filter:drop-shadow(0 0 5px ${glow});`}/>
    </g>
  </svg>`;
}

function Kpis({ data }) {
  const last = data.runs.find((r) => r.status !== "running");
  const kpi = data.kpi || {};
  const sums = data.summaries || { critic: 0, user: 0 };
  const skipped = data.today.skipped_until_tomorrow.length;
  const workers = data.workers.length;
  const busy = data.workers.filter((w) => w.state === "working").length;
  const spg = data.seconds_per_game;
  const items = [
    { key: "games", label: "всего игр", value: fmt(data.games), sub: "в каталоге", icon: "db", tint: "acc",
      delta: last && last.created ? { text: "+" + last.created, arrow: "↑", color: GREEN } : null,
      deltaHint: "добавлено последним прогоном" },
    { key: "processed", label: "обработано", value: fmt(kpi.processed_24h || 0), sub: "за последние 24 часа", icon: "check", tint: "green",
      delta: delta(kpi.processed_24h, kpi.processed_prev_24h), deltaHint: "к тем же суткам днём раньше" },
    { key: "summaries", label: "резюме ИИ", value: fmt(sums.critic + sums.user), sub: `критики ${fmt(sums.critic)} · игроки ${fmt(sums.user)}`,
      icon: "gem", tint: "violet",
      delta: kpi.summaries_24h ? { text: "+" + kpi.summaries_24h, arrow: "↑", color: GREEN } : null,
      deltaHint: "обновлено за сутки" },
    { key: "failed", label: "ошибки", value: fmt(kpi.failures_24h || 0), icon: "warn", tint: "red",
      sub: skipped ? `${skipped} ${plural(skipped, "игра пропущена", "игры пропущены", "игр пропущено")} до завтра` : "пропущенных до завтра нет",
      color: kpi.failures_24h ? RED : null,
      delta: delta(kpi.failures_24h, kpi.failures_prev_24h, { less: true }), deltaHint: "к тем же суткам днём раньше" },
    { key: "time", label: "среднее время", value: spg == null ? "—" : decimal(spg), unit: spg == null ? "" : "с",
      // подпись в одну строку: в две её задевает спарклайн, лежащий поверх карточки
      sub: "на игру по последним прогонам", icon: "clock", tint: "yellow",
      delta: spg == null ? null : delta(spg, kpi.seconds_per_game_prev, { less: true }), deltaHint: "к предыдущим прогонам" },
    { key: "workers", label: "активных воркеров", value: String(busy), unit: workers ? "из " + workers : "",
      sub: workers ? "выполняют задачи сейчас" : "воркеры не запущены", icon: "loop", tint: "acc", color: ACC },
  ];
  const frames = Object.values(DESIGN_SPARKS).map((spark) => spark.reveal).join("");
  return html`<style>${frames}</style>
  <div class="kpis">${items.map((k) => {
    const [bg, , fg] = TINT[k.tint];
    const chart = DESIGN_SPARKS[k.key];
    return html`<div class="kpi" key=${k.key}>
      <div class="kpi-top">
        <span class="kpi-icon" style=${{ background: bg }}><${KpiIcon} name=${k.icon} color=${fg}/></span>
        <span class="kpi-label">${k.label}</span>
      </div>
      <div class="kpi-value-row">
        <span class="kpi-value" style=${k.color ? { color: k.color } : null}>${k.value}</span>
        ${k.unit && html`<span class="kpi-unit">${k.unit}</span>`}
        ${k.delta && html`<span class="kpi-delta" style=${{ color: k.delta.color }} ...${tip(k.deltaHint)}>${k.delta.arrow} ${k.delta.text}</span>`}
      </div>
      <div class="kpi-sub">${k.sub}</div>
      ${chart && html`<${Spark} id=${"spark-" + k.key} chart=${chart} color=${chart.color || fg}/>`}
    </div>`;
  })}</div>`;
}

function CurrentRun({ data }) {
  const run = data.run;
  const last = data.runs.find((r) => r.status !== "running");
  // что и сколько берёт прогон, задаётся в окне расписания
  const cfg = (data.scheduler && data.scheduler.run) || { games: 20, stale: true };
  // откуда игры прогона: "13 новинок + 10 старых на обновление"
  const pickText = (total, stale) => {
    const fresh = (total || 0) - (stale || 0);
    const news = `${fresh} ${plural(fresh, "новинка", "новинки", "новинок")} Metacritic`;
    return stale ? `${news} + ${stale} ${plural(stale, "старая", "старые", "старых")} на обновление` : cfg.stale ? `${news}, старым обновление пока не нужно` : news;
  };
  let chip, detail, stages, overall, counters;
  // единый формат блока этапа: сверху число с единицей ("20 игр", "3 резюме"), ниже что оно значит
  // и из скольких. Слов вместо числа ("не нужны") нет: пустой этап это "0 ..." с пояснением
  const GAMES = ["игра", "игры", "игр"], SUMMARIES = ["резюме", "резюме", "резюме"], LETSPLAYS = ["летсплей", "летсплея", "летсплеев"];
  const num = (n, forms) => `${n || 0} ${plural(n || 0, ...forms)}`;
  // летсплеи этой итерации: игры прогона и добранные после него старые, итог пишется в счётчики прогона
  // (lp_*), поэтому цифры сходятся с самим прогоном, а не с каталогом
  const lpRun = run ? data.runs.find((r) => r.id === run.id) || {} : last || {};
  const lpDone = (lpRun.lp_found || 0) + (lpRun.lp_not_found || 0) + (lpRun.lp_failed || 0);
  const lpTotal = Math.max(lpRun.lp_total || 0, lpDone);
  const letsplayStage = {
    name: "Летсплеи",
    value: num(lpRun.lp_found, LETSPLAYS),
    note: lpTotal
      ? `найдено, проверено ${lpDone} из ${lpTotal}${lpRun.lp_not_found ? ` · ${lpRun.lp_not_found} без летсплея` : ""}${lpRun.lp_failed ? ` · ${num(lpRun.lp_failed, ["сбой", "сбоя", "сбоев"])}` : ""}`
      : run ? "игры прогона встанут в очередь после загрузки" : "в этой итерации искать нечего",
    state: lpTotal && lpDone < lpTotal ? "active" : run && !lpTotal ? "wait" : "done", pct: lpTotal ? pct(lpDone, lpTotal) : 100,
  };
  if (run) {
    const order = ["selecting", "metacritic", "media", "summaries"];
    const at = Math.max(0, order.indexOf(run.stage));
    const selected = run.selected || 0;
    const defs = [
      { name: "Выбор игр", value: num(selected, GAMES), note: at > 0 ? pickText(selected, run.stale_selected) : "смотрим списки Metacritic", done: at > 0 ? 1 : 0, total: 1 },
      { name: "Загрузка с Metacritic", value: num(run.processed - (run.failed || 0), GAMES),
        note: `загружено, обработано ${run.processed} из ${selected || cfg.games}${run.failed ? ` · ${run.failed} не загрузилось` : ""}`,
        done: run.processed, total: selected || cfg.games },
      { name: "Обложки и трейлеры", value: num(run.media_done, GAMES),
        note: run.media_total ? `проверено в Steam и Google Play, всего ${run.media_total}` : at > 2 ? "у всех игр есть на Metacritic, искать нечего" : "игры без обложки или трейлера после загрузки",
        done: run.media_done, total: run.media_total },
      { name: "Резюме отзывов", value: num(run.summaries_saved, SUMMARIES),
        note: run.summaries_total ? `сохранено, проверено ${run.summaries_done} из ${run.summaries_total}${run.summary_failed ? ` · ${num(run.summary_failed, ["ошибка", "ошибки", "ошибок"])}` : ""}` : at > 3 ? "отзывы не изменились, пересобирать нечего" : "после загрузки игр",
        done: run.summaries_done, total: run.summaries_total },
    ];
    stages = defs.map((d, i) => ({ ...d, state: i < at || (i === at && d.total > 0 && d.done >= d.total && i !== 3) ? "done" : i === at ? "active" : "wait", pct: i < at ? 100 : pct(d.done, d.total) }));
    stages.push(letsplayStage);
    const weights = [0.05, 0.5, 0.15, 0.3];
    overall = Math.min(99, Math.round(100 * defs.reduce((sum, d, i) => sum + weights[i] * (i < at ? 1 : i === at ? (d.total ? d.done / d.total : 0) : 0), 0)));
    chip = { label: "Идёт прогон", color: ACC, bg: "rgba(59,166,255,.14)" };
    detail = `Прогон #${run.id}, ${(TRIGGER[run.trigger] || run.trigger).toLowerCase()}, идёт ${durationText(since(run.started_at))}`;
    counters = { selected, created: run.created, updated: run.updated, rechecked: run.rechecked, failed: run.failed };
  } else if (last) {
    const failed = last.status === "failed", broken = last.status === "interrupted";
    chip = failed ? { label: "Последний прогон с ошибкой", color: RED, bg: "rgba(242,85,90,.14)" }
      : broken ? { label: "Последний прогон прерван", color: YELLOW, bg: "rgba(245,179,60,.14)" }
      : { label: "Ожидание", color: GREEN, bg: "rgba(62,207,142,.12)" };
    detail = `Прогон #${last.id}, ${(TRIGGER[last.trigger] || last.trigger).toLowerCase()}, `
      + `${failed ? "завершился ошибкой" : broken ? "прерван" : "завершён"} за ${durationText(since(last.started_at, last.finished_at))}: `
      + `${last.selected} ${plural(last.selected, "игра", "игры", "игр")}, ${last.summaries} ${plural(last.summaries, "резюме", "резюме", "резюме")}, `
      + (last.failed ? `${last.failed} ${plural(last.failed, "ошибка", "ошибки", "ошибок")}` : "без ошибок") + (last.error ? `. ${last.error}` : "");
    const saved = last.created + last.updated;
    stages = [
      // у завершённого прогона своя выборка: сколько игр было задано тогда, в базе не хранится
      { name: "Выбор игр", value: num(last.selected, GAMES), note: pickText(last.selected, last.stale_selected), state: "done", pct: 100 },
      { name: "Загрузка с Metacritic", value: num(saved, GAMES),
        note: last.failed ? `загружено из ${last.selected} · ${last.failed} не загрузилось` : `загружено из ${last.selected}, все без ошибок`,
        state: saved >= last.selected ? "done" : "wait", pct: pct(saved, last.selected) },
      { name: "Обложки и трейлеры", value: num(last.media_found, GAMES),
        note: last.media_checked ? `получили из Steam и Google Play, проверено ${last.media_checked}` : "у всех игр есть на Metacritic, искать нечего",
        state: "done", pct: 100 },
      { name: "Резюме отзывов", value: num(last.summaries, SUMMARIES),
        note: last.summaries_checked ? `сохранено из ${last.summaries_checked} проверенных, у остальных отзывы не изменились`
          : last.summaries ? "сохранено" : "отзывы не изменились, пересобирать нечего",
        state: "done", pct: 100 },
      letsplayStage,
    ];
    overall = 100;
    counters = { selected: last.selected, created: last.created, updated: last.updated, rechecked: last.rechecked, failed: last.failed };
  } else {
    chip = { label: "Прогонов ещё не было", color: "var(--t8)", bg: "var(--veil-07)" };
    detail = "Первый прогон стартует по расписанию или по кнопке";
    stages = [];
    overall = 0;
    counters = { selected: 0, created: 0, updated: 0, rechecked: 0, failed: 0 };
  }
  const source = run || last;
  return html`<section class="panel">
    <${SecHead} icon=${I.bolt} title="Текущий прогон">
      <span class="state-chip" style=${{ background: chip.bg, color: chip.color }} ...${tip(detail)}>${chip.label}</span>
      <span ...${tip("Набор правил в промтах: для облачной модели строже к цитатам, для локальной проще формулировки")}>
        Профиль: <b style=${{ color: ACC }}>${data.llm && data.llm.provider === "local" ? "локальный" : "облачный"}</b></span>
      <span>Источник: <b>Metacritic</b></span>
      ${source && html`<span>Запуск: <b class="mono">${timeOf(source.started_at)}${zoneSuffix()}</b></span>`}
    </${SecHead}>
    <div class="run-detail">${detail}</div>
    <div class="progress"><div class="bar"><div style=${{ width: overall + "%" }}></div></div><span class="progress-pct">${overall}%</span></div>
    <div class="stages">${stages.map((st, i) => html`<div class=${cx("stage", st.state === "active" && "is-active", st.state === "done" && "is-done")} key=${st.name}>
      <div class="stage-top">
        <span class="stage-num">${st.state === "done"
          ? html`<svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-label="Завершено">
              <path d=${RING} fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linecap="round" stroke-linejoin="round"/>
              <path d=${CHECK} fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`
          : i + 1}</span>
        <div><div class="stage-name">${st.name}</div><div class="stage-state">${st.state === "done" ? "Завершено" : st.state === "active" ? "В процессе" : run ? "Ожидание" : "Не запущен"}</div></div>
      </div>
      <div class="stage-val"><b>${st.value}</b><span>${st.note}</span></div>
      ${st.bar !== false && html`<div class="stage-bar"><div style=${{ width: st.pct + "%" }}></div></div>`}
    </div>`)}</div>
    <div class="counters">
      ${[
        ["выбрано", counters.selected, "var(--t1)", "Всего игр в прогоне"],
        ["новые", counters.created, GREEN, "Новые игры: их не было в базе"],
        ["обновлены", counters.updated, ACC, "Старые игры, которые снова попали в новинки Metacritic: данные обновлены"],
        ["старые по сроку", counters.rechecked || 0, "var(--violet)", "Старые игры, обновлённые по настройке «Обновлять игру раз в»"],
        ["ошибки", counters.failed, counters.failed ? RED : "var(--t9)", "Игры, которые не загрузились с Metacritic"],
      ].map(([label, value, color, hint]) => html`<div class="counter" key=${label} ...${tip(hint)}><div class="counter-label">${label}</div><div class="counter-value" style=${{ color }}>${value}</div></div>`)}
    </div>
  </section>`;
}

function Health({ status }) {
  const sys = status.data.system || {};
  const running = !!status.data.run;
  const cpu = sys.cpu_percent, mem = sys.memory, disk = sys.disk, net = sys.network;
  const tiles = [
    { label: "Процессор", value: cpu == null ? "—" : Math.round(cpu) + "%", pct: cpu, color: loadColor(cpu, 60, 85), sub: "загрузка системы" },
    { label: "Память", value: mem ? Math.round(mem.percent) + "%" : "—", pct: mem && mem.percent, color: mem && mem.percent > 90 ? RED : "var(--acc-2)",
      sub: mem ? `${decimal((mem.used_mb / 1024).toFixed(1))} из ${decimal((mem.total_mb / 1024).toFixed(0))} ГБ · сервис ${sys.process_mb ? fmt(sys.process_mb) + " МБ" : "—"}` : "" },
    { label: "Диск базы", value: disk ? Math.round(disk.percent) + "%" : "—", pct: disk && disk.percent, color: loadColor(disk && disk.percent, 80, 92),
      sub: disk ? `свободно ${decimal(disk.free_gb)} ГБ` : "" },
    { label: "Сеть", value: net ? decimal(net.percent) + "%" : "—", pct: net && net.percent, color: "var(--violet)",
      sub: net ? `приём ${mbps(net.rx_bps)} · отдача ${mbps(net.tx_bps)}` : "адаптер не найден" },
  ];
  const state = !status.online ? { label: "Нет связи", color: YELLOW, bg: "rgba(245,179,60,.14)" }
    : (cpu != null && cpu > 80) || running ? { label: "Под нагрузкой", color: ACC, bg: "rgba(43,123,255,.14)" }
    : { label: "Стабильно", color: GREEN, bg: "rgba(62,207,142,.12)" };
  return html`<section class="panel">
    <${SecHead} icon=${I.chip} title="Здоровье сервиса">
      <span class="state-chip dot-chip" style=${{ background: state.bg, color: state.color }}><i></i>${state.label}</span>
    </${SecHead}>
    <div class="infra">${tiles.map((t) => html`<div class="infra-tile" key=${t.label}>
      <div class="infra-label">${t.label}</div>
      <div class="infra-value" style=${{ color: t.pct == null ? "var(--t9)" : t.color }}>${t.value}</div>
      <div class="infra-bar"><div style=${{ width: (t.pct || 0) + "%", background: t.color }}></div></div>
      ${t.sub && html`<div class="infra-sub">${t.sub}</div>`}
    </div>`)}</div>
  </section>`;
}

// cookies YouTube из профиля Chrome сервиса: жив ли вход и когда обновляли
function youtubeLogin(yt) {
  if (!yt) return "—";
  if (!yt.profile) return "профиль Chrome не задан (YOUTUBE_PROFILE)";
  const c = yt.cookies;
  if (!c) return "cookies ещё не обновлялись";
  const when = ago(c.at, Date.now());
  if (c.logged_in) return html`<span style=${{ color: GREEN }}>аккаунт вошёл</span> · обновлено ${when}`;
  return html`<span style=${{ color: RED }}>нужен вход: пункт "Войти в YouTube" в лаунчере</span>`;
}

function Gpu({ data }) {
  const gpu = data.system && data.system.gpu;
  const w = data.whisper;
  const letsplay = data.workers.find((x) => x.kind === "letsplay");
  const used = gpu && gpu.memory_used_mb, total = gpu && gpu.memory_total_mb;
  const last = w && w.last;
  const download = w && w.download;
  const speed = last && last.took_seconds ? Math.round(last.audio_seconds / last.took_seconds) : null;
  // видеокарту может занимать и локальная модель: пока она пишет резюме, так и показываем
  const localBusy = data.llm && data.llm.provider === "local" && data.workers.some((x) => x.kind === "gemini" && x.state === "working");
  const stage = !w ? "Whisper выключен (LETSPLAY_ENABLED=false)"
    : w.busy ? `расшифровка${w.busy_seconds ? ", " + durationText(w.busy_seconds) : ""}`
    : download ? `загрузка звука${download.percent != null ? " " + decimal(download.percent.toFixed(0)) + " %" : ""}`
    : localBusy ? "локальная модель пишет резюме"
    : letsplay && letsplay.state === "working" ? `летсплей: ${letsplay.game || "игра"}, поиск ролика`
    : letsplay && letsplay.state === "waiting" ? "летсплеи ждут модель" : "простаивает";
  const rows = [
    ["Текущий этап", stage],
    ["Скорость загрузки", download && download.speed ? mbps(download.speed) : "—"],
    ["Расшифровано", last ? `${decimal((last.audio_seconds / 60).toFixed(1))} мин за ${decimal(last.took_seconds)} с` : "ещё не было"],
    ["Скорость расшифровки", speed ? `×${speed} реального времени` : "—"],
    ["Очередь роликов", `${data.letsplay_queue} в очереди · ${letsplay && letsplay.state === "working" ? 1 : 0} в работе`],
    ["Вход в YouTube", youtubeLogin(data.youtube)],
  ];
  return html`<section class="gpu">
    <div class="gpu-head"><div class="gpu-title">Whisper · GPU</div>
      ${w && html`<span class="gpu-state">${w.loaded ? "модель в памяти" : "модель не загружена"}</span>`}</div>
    <div class="gpu-line">${w ? `${w.model} · ${w.compute_type}` : "Whisper выключен"} · ${gpu ? `${gpu.name} · ${Math.round(total / 1024)} ГБ` : "видеокарта NVIDIA не найдена"}</div>
    <div class="gpu-vram"><span>Видеопамять</span><span class="mono">${gpu ? `${decimal((used / 1024).toFixed(1))} / ${decimal((total / 1024).toFixed(1))} ГБ` : "—"}</span></div>
    <div class="gpu-bar"><div style=${{ width: gpu ? pct(used, total) + "%" : "0%" }}></div></div>
    <div class="gpu-rows">${rows.map(([label, value]) => html`<div class="gpu-row" key=${label}><span>${label}</span><span>${value}</span></div>`)}</div>
  </section>`;
}

function LlmModal({ onClose }) {
  const [opts, setOpts] = useState(null);
  const [draft, setDraft] = useState(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    api("/llm").then((o) => { setOpts(o); setDraft({ provider: o.provider, model: o.model }); })
      .catch(() => setError("Не удалось получить список моделей, попробуйте ещё раз"));
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);
  const dirty = opts && draft && (draft.provider !== opts.provider || draft.model !== opts.model);
  async function apply() {
    if (!dirty || saving) return;
    setSaving(true);
    setError("");
    try {
      const next = await api("/llm", { method: "PUT", body: draft });
      toast(`Модель изменена: ${next.title}, ${next.model}. Действует для следующих запросов`, "acc");
      await refreshStatus();
      onClose();
    } catch (e) {
      setError(e.status === 409 ? "У провайдера нет ключа в .env" : e.status === 503 ? "LM Studio не отвечает: запустите его и включите сервер"
        : e.status === 422 ? "Такой модели у провайдера нет" : "Сервис не ответил, модель не изменена");
    } finally {
      setSaving(false);
    }
  }
  return html`<div class="modal-back" onClick=${onClose}>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Модель ИИ" onClick=${(e) => e.stopPropagation()}>
      <div class="modal-head"><div class="modal-title">Модель ИИ</div><button class="modal-x" onClick=${onClose} aria-label="Закрыть">✕</button></div>
      <div class="modal-sub">Смена действует для следующих запросов, уже начатые доработают на прежней модели.</div>
      ${!opts ? html`<div class="modal-loading">${error || "Загружаем список моделей…"}</div>` : html`<div class="providers">
        ${opts.providers.map((p) => {
          const on = draft.provider === p.id, off = !p.available;
          const pick = () => { if (off) return; const model = p.models.some((m) => m.id === draft.model) && on ? draft.model : (p.models.find((m) => m.id === p.default_model) || p.models[0] || {}).id || ""; setDraft({ provider: p.id, model }); setError(""); };
          return html`<div class=${cx("prov", on && "is-on", off && "is-off")} key=${p.id}>
            <button class="prov-head" onClick=${pick} aria-pressed=${on} disabled=${off}>
              <span class="radio"><i style=${{ background: on ? "var(--acc)" : "transparent" }}></i></span>
              <span class="prov-title">${PROVIDER_TITLE[p.id] || p.title}</span>
              <span class="prov-hint">${off ? LLM_REASON[p.reason] || "недоступен" : PROVIDER_HINT[p.id]}</span>
            </button>
            ${on && !off && html`<div class="models">${p.models.map((m) => {
              const mOn = draft.model === m.id;
              return html`<button class=${cx("model", mOn && "is-on")} onClick=${() => { setDraft({ provider: p.id, model: m.id }); setError(""); }} aria-pressed=${mOn} key=${m.id}>
                <div class="model-top">
                  <span class="radio radio-sm"><i style=${{ background: mOn ? "var(--acc)" : "transparent" }}></i></span>
                  <span class="model-id">${m.id}</span>
                  ${m.recommended && html`<span class="rec">рекомендуем</span>`}
                </div>
                ${m.note && html`<div class=${cx("model-note", !m.recommended && "is-warn")}>${m.note}</div>`}
                ${SPEED[m.id] && html`<div class="model-speed">${SPEED[m.id]}</div>`}
              </button>`;
            })}${p.models.length === 0 && html`<div class="model-note">В LM Studio нет скачанных моделей</div>`}</div>`}
          </div>`;
        })}
      </div>`}
      ${error && opts && html`<div class="modal-err">${error}</div>`}
      <div class="modal-actions">
        <button class="btn" onClick=${apply} disabled=${!dirty || saving}>${saving ? "Применяем…" : "Применить"}</button>
        <button class="btn btn-ghost" onClick=${onClose}>Отмена</button>
      </div>
    </div>
  </div>`;
}

function Llm({ data }) {
  const [modal, setModal] = useState(false);
  const [keysOpen, setKeysOpen] = useState(false);
  const [opts, setOpts] = useState(null);
  const [switching, setSwitching] = useState("");
  const llm = data.llm;
  const keys = data.gemini_keys || [];
  const reason = llm && llm.reason;
  // список остальных моделей для быстрого переключения: состав и доступность берём у сервиса
  useEffect(() => {
    let alive = true;
    api("/llm").then((o) => alive && setOpts(o)).catch(() => alive && setOpts(null));
    return () => { alive = false; };
  }, [llm && llm.provider, llm && llm.model, modal]);

  async function use(provider, model) {
    setSwitching(provider);
    try {
      const next = await api("/llm", { method: "PUT", body: { provider, model } });
      toast(`Модель изменена: ${next.title}, ${next.model}. Действует для следующих запросов`, "acc");
      await refreshStatus();
    } catch (e) {
      toast(e.status === 503 ? "LM Studio не запустился: включите Local LLM Service в его настройках"
        : e.status === 409 ? "У провайдера нет ключа в .env" : "Сервис не ответил, модель не изменена", "red");
    } finally {
      setSwitching("");
    }
  }

  const others = (opts ? opts.providers : []).filter((p) => !llm || p.id !== llm.provider).map((p) => ({
    ...p, model: (p.models.find((m) => m.recommended) || p.models[0] || {}).id || "",
  }));
  return html`<section class="panel">
    <div class="panel-head">
      <div class="panel-title">Модель ИИ</div>
      <span class="state-chip" style=${{ marginLeft: "auto", background: reason ? "rgba(245,179,60,.14)" : "rgba(62,207,142,.14)", color: reason ? YELLOW : GREEN }}>
        ${!llm ? "Не настроена" : reason ? LLM_REASON[reason] || "Недоступна" : "Работает"}</span>
    </div>
    <div class="llm-current">
      <div class="llm-text"><div class="llm-title">${llm ? (PROVIDER_TITLE[llm.provider] || llm.title) : "—"}</div><div class="llm-model">${llm ? llm.model : ""}</div></div>
      <button class="btn btn-change" onClick=${() => setModal(true)}>Изменить</button>
    </div>
    ${reason && html`<div class="llm-warn">Резюме и летсплеи ждут, пока модель недоступна</div>`}    ${others.length > 0 && html`<div>
      <div class="field-label llm-alt-label">остальные модели</div>
      <div class="llm-alt">${others.map((p) => html`<div class=${cx("llm-alt-row", !p.available && "is-off", p.available && "is-plain")} key=${p.id}>
        <span class="llm-alt-title">${PROVIDER_TITLE[p.id] || p.title}</span>
        <span class="llm-alt-model">${p.model || "нет моделей"}</span>
        ${!p.available && html`<span class="llm-alt-state" style=${{ background: "var(--veil-06)", color: "var(--t9)" }}>
          ${LLM_REASON[p.reason] || "Недоступна"}</span>`}
        <button class="btn llm-alt-btn" disabled=${!p.available || !p.model || switching === p.id} onClick=${() => use(p.id, p.model)}>
          ${switching === p.id ? "Меняем…" : p.available ? "Выбрать" : "Нет доступа"}</button>
      </div>`)}</div>
    </div>`}
    ${keys.length > 0 && html`<button class="btn-link keys-toggle" onClick=${() => setKeysOpen(!keysOpen)} aria-expanded=${keysOpen}>
      ${keysOpen ? "Свернуть" : "Подробнее"}</button>`}
    ${keysOpen && html`<div class="keys">${keys.map((k, i) => {
      const [label, color] = KEY_STATE[k.state] || ["—", "var(--t9)"];
      const t = k.tokens || {};
      return html`<div class="key" key=${k.name}>
        <div class="key-top"><span class="key-dot" style=${{ background: color }}></span><span class="key-name">${k.name}</span><span class="key-state" style=${{ color }}>${label}</span></div>
        <div class="key-row"><span>${fmt(k.calls)} ${plural(k.calls, "вызов", "вызова", "вызовов")} за сеанс</span>
          <span>${k.state === "cooldown" ? "ждёт ещё " + durationText(k.cooldown_seconds) : k.credits ? `потрачено ${decimal(k.credits)} кредита` : i === 0 ? "основной" : "запасной"}</span></div>
        <div class="key-tokens">токены: вход ${fmt(t.prompt || 0)} · ответ ${fmt(t.output || 0)} · рассуждения ${fmt(t.thinking || 0)}</div>
        ${k.last_error && html`<div class="key-err">${k.last_error}</div>`}
      </div>`;
    })}</div>`}
    ${modal && html`<${LlmModal} onClose=${() => setModal(false)} />`}
  </section>`;
}

// всплывающая карточка с полным текстом ошибки воркера: в самой карточке он обрезан до двух строк
function errorCue(w) {
  return {
    group: "ошибка воркера", title: w.name, sub: w.updated_at ? "последняя задача, " + timeOf(w.updated_at, false) + zoneSuffix() : "",
    rows: [{ kind: "text", text: w.last_error }],
  };
}

function Workers({ data, now }) {
  const [details, setDetails] = useState(false);
  const list = [...data.workers];
  if (data.scheduler.enabled) {
    const next = data.scheduler.next_run_at;
    list.push({
      name: "scheduler", kind: "scheduler", state: data.run ? "working" : "idle", game: null,
      stage: data.run ? `прогон #${data.run.id}` : data.scheduler.active && next ? `ждёт ${timeOf(next, false)}${zoneSuffix()}` : "расписание на паузе",
      processed: data.runs_total || data.runs.length, errors: 0, last_error: null, last_failed: false,
      load: data.run ? 100 : 0,
      updated_at: data.run ? data.run.started_at : data.runs[0] && (data.runs[0].finished_at || data.runs[0].started_at),
    });
  }
  const busy = list.filter((w) => w.state === "working").length;
  return html`<section class="panel">
    <${SecHead} icon=${I.grid} title="Рабочие процессы" sub="Параллельная обработка задач через воркеры">
      <span class="sec-count"><b>${busy}</b> / ${list.length} ${plural(list.length, "воркер", "воркера", "воркеров")}</span>
    </${SecHead}>
    ${list.length === 0
      ? html`<div class="empty-note">Воркеры не запущены: сервис работает без планировщика и без летсплеев, данные только из базы.</div>`
      : html`<div class="workers">${list.map((w) => {
        const failed = w.last_failed && w.state !== "working";
        const color = w.state === "working" ? GREEN : w.state === "waiting" ? YELLOW : failed ? RED : "var(--t9)";
        const task = w.state === "working"
          ? (w.game ? `${w.game} · ${WORKER_STAGE[w.stage] || w.stage}` : w.stage)
          : w.state === "waiting" ? `ждёт модель: ${LLM_REASON[(w.stage || "").replace(/^llm_/, "")] || w.stage}`
          : w.kind === "scheduler" ? w.stage : "нет задач";
        const load = w.load == null ? 0 : w.load;
        return html`<div class=${cx("worker", w.state === "working" && "is-working", w.state === "waiting" && "is-waiting", failed && "is-error")} key=${w.name}>
          <div class="worker-top"><span class="wdot" style=${{ background: color, boxShadow: `0 0 7px ${color}` }}></span><span class="worker-name">${w.name}</span></div>
          <div class="worker-now">${task}</div>
          <div class="worker-when">${w.updated_at ? ago(w.updated_at, now) : ""}</div>
          ${failed && w.last_error && html`<div class="worker-err cue-item" tabindex="0"
            onMouseEnter=${(e) => !touchInput() && cueShow(errorCue(w), e.currentTarget)} onMouseLeave=${() => cueHide()}
            onFocus=${(e) => cueShow(errorCue(w), e.currentTarget)} onBlur=${() => cueHide()}
            onClick=${(e) => touchInput() && cueToggle(errorCue(w), e.currentTarget)}>${w.last_error}</div>`}
          <div class="worker-load">
            <span class="worker-pct" style=${{ color: load ? "var(--t1)" : "var(--t9)" }}>${load}%</span>
            <span class="worker-bar"><span style=${{ width: load + "%", background: color }}></span></span>
          </div>
          ${details && html`<div class="worker-more">
            <div>${WORKER_KIND[w.kind] || w.kind}</div>
            <div class="worker-more-row">
              <span>${w.kind === "scheduler" ? `прогонов ${w.processed}` : `готово ${w.processed}${w.errors ? " · ошибок " + w.errors : ""}`}</span>
              <span>${WORKER_STATE[w.state] || w.state}</span>
            </div>
          </div>`}
        </div>`;
      })}</div>`}
    ${list.length > 0 && html`<button class="btn-link workers-toggle" onClick=${() => setDetails(!details)} aria-expanded=${details}>${details ? "Свернуть" : "Подробнее"}</button>`}
  </section>`;
}

function checkState(g) {
  if (g.failed_stages.length) return { state: "fail", label: "Ошибка", note: STAGE_ERR[g.failed_stages[0]] || "ошибка" };
  if (g.metascore == null && g.userscore == null) return { state: "part", label: "Частично", note: "нет оценок" };
  if ((g.critic_reviews || 0) + (g.user_reviews || 0) > 0 && !g.summaries) return { state: "part", label: "Частично", note: "без резюме" };
  return { state: "ok", label: "Успешно", note: g.metascore != null ? "критики" : "игроки" };
}
const CHECK_TONE = { ok: [GREEN, "rgba(62,207,142,.12)"], part: [YELLOW, "rgba(245,179,60,.14)"], fail: [RED, "rgba(242,85,90,.14)"] };

function Checks({ data, now }) {
  const items = data.recent || [];
  return html`<section class="panel">
    <${SecHead} icon=${I.clock} tint="green" title="Последние проверки" sub="Игры, обработанные в этом прогоне">
      <a class="btn-link" href=${link.catalog()}>Весь каталог</a>
    </${SecHead}>
    ${items.length === 0
      ? html`<div class="empty-note">В последнем прогоне пока нет обработанных игр.</div>`
      : html`<div class="checks">${items.map((g) => {
        const c = checkState(g);
        const [fg, bg] = CHECK_TONE[c.state];
        const isUser = g.metascore == null && g.userscore != null;
        const score = isUser ? g.userscore : g.metascore;
        return html`<a class=${cx("check", c.state === "fail" && "is-fail")} href=${link.game(g.slug)} key=${g.slug}>
          <span class="check-badge" style=${{ background: bg, color: fg }}><i></i>${c.label}</span>
          <div class="check-title">${g.title || g.slug}</div>
          <div class="check-meta">Metacritic · ${ago(g.at, now)}</div>
          <div class="check-foot">
            <${ScoreBadge} value=${score} isUser=${isUser} size="xs" />
            <span class="note">${c.note}</span>
          </div>
        </a>`;
      })}</div>`}
  </section>`;
}

function pageChips(page, pages) {
  // окно из семи страниц вокруг текущей: при полусотне прогонов кнопки не расползаются на всю карточку
  const from = Math.max(0, Math.min(page - 3, pages - 7));
  return Array.from({ length: Math.min(7, pages) }, (_, i) => from + i);
}

function History({ data, onStart }) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const [view, setView] = useState(null);
  const [error, setError] = useState(false);
  const last = data.runs[0];
  // история перечитывается, когда прогон стартовал или закончился
  const liveKey = last ? `${last.id}:${last.status}:${data.run ? data.run.processed : ""}` : "";
  useEffect(() => {
    if (!open) return undefined;
    let alive = true;
    api(`/runs?status=${filter}&limit=${HISTORY_PER_PAGE}&offset=${page * HISTORY_PER_PAGE}`)
      .then((r) => { if (alive) { setView(r); setError(false); } })
      .catch(() => { if (alive) setError(true); });
    return () => { alive = false; };
  }, [open, filter, page, liveKey]);

  const rows = (view ? view.items : []).map((r) => ({ ...r, seconds: since(r.started_at, r.finished_at) }));
  const max = Math.max(1, ...rows.map((r) => r.seconds || 0));
  const total = view ? view.total : 0;
  const pages = Math.max(1, Math.ceil(total / HISTORY_PER_PAGE));
  const runsTotal = data.runs_total || data.runs.length;
  const sub = !open ? `всего ${count(runsTotal, "прогон", "прогона", "прогонов")}`
    : filter === "all" ? `всего ${count(total, "прогон", "прогона", "прогонов")}`
    : `${total} из ${runsTotal} прогонов`;
  return html`<section class="panel">
    <${SecHead} icon=${I.history} title="История прогонов" sub=${sub}>
      ${open && html`<div class="seg" role="group" aria-label="Фильтр истории">
        ${[["all", "Все"], ["ok", "Успешные"], ["bad", "С ошибками"]].map(([id, label]) => html`<button key=${id} class=${cx(filter === id && "is-on")}
          onClick=${() => { setFilter(id); setPage(0); }} aria-pressed=${filter === id}>${label}</button>`)}
      </div>`}
      <button class="btn" onClick=${() => setOpen(!open)} aria-expanded=${open}>${open ? "Свернуть" : "Показать историю"}</button>
    </${SecHead}>
    ${open && html`<div>
      ${error && html`<div class="empty-note">Не удалось получить историю: сервис не ответил.</div>`}
      <div class="hist-scroll">
        <div class="hist-head"><span>№</span><span>запуск</span><span>начало</span><span>длит.</span><span>выбр.</span><span>новые</span><span>обн.</span><span>рез.</span><span>ошиб.</span><span>статус</span><span></span></div>
        ${rows.map((r) => {
          const st = RUN_STATUS[r.status] || RUN_STATUS.done;
          const barColor = r.status === "failed" ? RED : r.status === "interrupted" ? YELLOW : r.status === "running" ? ACC : "rgba(120,150,210,.5)";
          return html`<div class="hist-row" key=${r.id}>
            <span class="mono muted">#${r.id}</span>
            <span class="t3">${TRIGGER[r.trigger] || r.trigger}</span>
            <span class="mono t4">${timeOf(r.started_at)}</span>
            <span class="dur"><span class="mono t4">${durationText(r.seconds)}</span><span class="dur-bar"><span style=${{ width: Math.max(6, Math.round(((r.seconds || 0) / max) * 100)) + "%", background: barColor }}></span></span></span>
            <span class="mono t4">${r.selected}</span>
            <span class="mono" style=${{ color: GREEN }}>${r.created}</span>
            <span class="mono t4">${r.updated}</span>
            <span class="mono" style=${{ color: "var(--violet)" }}>${r.summaries}</span>
            <span class="mono" style=${{ color: r.failed ? RED : "var(--t9)" }}>${r.failed}</span>
            <span class="status-pill" style=${{ background: st.bg, color: st.color }} ...${tip(r.error)}><i></i>${st.label}</span>
            ${r.status === "failed" || r.status === "interrupted"
              ? html`<button class="btn btn-icon" onClick=${onStart} aria-label="Запустить прогон заново" ...${tip("Запустить новый прогон: игры, которые не обработались, он возьмёт первыми")}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d=${REFRESH} fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/></svg></button>`
              : html`<span></span>`}
          </div>`;
        })}
      </div>
      ${view && rows.length === 0 && html`<div class="hist-empty">${runsTotal ? "Прогонов с таким статусом нет" : "Прогонов ещё не было"}</div>`}
      ${pages > 1 && html`<div class="hist-pager">
        <span class="mono muted">${page * HISTORY_PER_PAGE + 1}–${Math.min((page + 1) * HISTORY_PER_PAGE, total)} из ${total}</span>
        <div class="hist-pages">
          <button class="btn page-btn" disabled=${page === 0} onClick=${() => setPage(page - 1)} aria-label="Предыдущая страница">${PREV}</button>
          ${pageChips(page, pages).map((n) => html`<button key=${n} class=${cx("btn", "page-btn", n === page && "is-on")} onClick=${() => setPage(n)}>${n + 1}</button>`)}
          <button class="btn page-btn" disabled=${page >= pages - 1} onClick=${() => setPage(page + 1)} aria-label="Следующая страница">${NEXT}</button>
        </div>
      </div>`}
    </div>`}
  </section>`;
}

function errorTitle(stage, text) {
  const t = (text || "").toLowerCase();
  if (/sign in to confirm/.test(t)) return "YouTube требует вход (проверка на робота)";
  if (/временный сбой youtube|youtube blocked|youtubeblocked/.test(t)) return "YouTube временно не отвечает";
  if (/credits are depleted|prepayment|insufficient credits|\b402\b/.test(t)) return "Кончились кредиты модели";
  if (/\b429\b|quota|resource_exhausted/.test(t)) return "Квота модели исчерпана (429)";
  if (/timeout|timed out/.test(t)) return "Таймаут запроса";
  if (/does not match schema|cut off|empty answer|no candidates/.test(t)) return "Ответ модели не по схеме";
  if (/geminiunavailable|no answer after|all llm keys/.test(t)) return "Модель не ответила";
  if (/parseerror|validation/.test(t)) return "Не удалось разобрать ответ Metacritic";
  if (/notfound|\b404\b/.test(t)) return "Игра не найдена на Metacritic (404)";
  if (/http 5\d\d|\b50[0-4]\b/.test(t)) return "Сервис ответил ошибкой сервера";
  if (stage === "media" || stage === "steam") return "Магазин не ответил";
  return (text || "Ошибка").split(":")[0].slice(0, 80);
}

// свёрнутый блок: две последние ошибки одной строкой каждая; "Подробнее" раскрывает список
// страницами по 5 с полным текстом, как в истории прогонов
const ERRORS_BRIEF = 3;
const ERRORS_PER_PAGE = 5;

function errorMeta(e, skipped) {
  // пропущенная до завтра игра важнее всего; кончившиеся кредиты или квота останавливают все резюме
  const high = skipped.has(e.slug);
  const mid = !high && (e.stage === "metacritic" || /credits|quota|resource_exhausted|\b402\b|\b429\b/i.test(e.error || ""));
  const sev = high ? ["Высокая", RED, "rgba(242,85,90,.16)"] : mid ? ["Средняя", YELLOW, "rgba(245,179,60,.16)"] : ["Низкая", "var(--t6)", "var(--veil-12)"];
  const flag = high ? ["3-я ошибка за день · пропущена до завтра", YELLOW]
    : e.stage === "metacritic" ? ["игра повторится в следующем прогоне", "var(--t9)"]
    : ["media", "steam", "trailer", "cover"].includes(e.stage) ? ["запрос к магазинам повторится через неделю", "var(--t9)"]
    : e.stage === "letsplay" ? (/входа нет/i.test(e.error || "") ? ["нужен вход в YouTube: пункт \"Войти в YouTube\" в лаунчере", RED]
      : [/временный сбой youtube/i.test(e.error || "") ? "YouTube отказал, игра вернётся в очередь через несколько минут" : "повтор через полчаса и с каждым прогоном, до 3 попыток в сутки", "var(--t9)"])
    : ["резюме повторится в следующем прогоне", ACC];
  return { sev, flag };
}

function Errors({ data, now }) {
  const [details, setDetails] = useState(false);
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState({});
  const skipped = new Set(data.today.skipped_until_tomorrow);
  const list = data.failures || [];
  const pages = Math.max(1, Math.ceil(list.length / ERRORS_PER_PAGE));
  const current = Math.min(page, pages - 1);
  const rows = details ? list.slice(current * ERRORS_PER_PAGE, (current + 1) * ERRORS_PER_PAGE) : list.slice(0, ERRORS_BRIEF);
  const game = (e) => (e.title ? html`<a class="err-game" href=${link.game(e.slug)}>${e.title}</a>` : html`<span class="err-game">${e.slug}</span>`);
  return html`<section class="panel">
    <${SecHead} icon=${I.warn} tint="red" title="Последние ошибки">
      <span class="count-chip" ...${tip("Ошибки за последние 24 часа, как в карточке сверху; в списке ниже до 20 последних")}>${(data.kpi && data.kpi.failures_24h) || 0} за сутки</span>
    </${SecHead}>
    ${list.length === 0 ? html`<div class="empty-note">Ошибок нет.</div>` : html`<div class="err-wrap">
      <div class="err-head"><span>время</span><span>игра и этап</span><span style=${{ textAlign: "right" }}>воркер</span></div>
      <div class=${cx("err-list", !details && "is-brief")}>
      ${rows.map((e, n) => {
        const i = details ? current * ERRORS_PER_PAGE + n : n;
        const { sev, flag } = errorMeta(e, skipped);
        // формат строки один и тот же в свёрнутом и развёрнутом виде; в развёрнутом добавляется текст ошибки
        return html`<div class="err-row" key=${i}>
          <span class="err-time" ...${tip(ago(e.created_at, now))}>${timeOf(e.created_at, false)}</span>
          <div class="err-body">
            <div class="err-line"><span class="sev" style=${{ background: sev[2], color: sev[1] }}>${sev[0]}</span>${game(e)}</div>
            <div class="err-title" ...${tip(errorTitle(e.stage, e.error))}>${errorTitle(e.stage, e.error)}</div>
            <div class="err-brief"><span class="err-stage">${STAGE_ERR[e.stage] || e.stage}</span><span class="err-flag" style=${{ color: flag[1] }} ...${tip(flag[0])}>${flag[0]}</span></div>
            ${details && html`<button class="btn-link err-toggle" onClick=${() => setOpen({ ...open, [i]: !open[i] })} aria-expanded=${!!open[i]}>${open[i] ? "Скрыть текст ошибки" : "Показать текст ошибки"}</button>`}
            ${details && open[i] && html`<pre class="error-text">${e.error}</pre>`}
          </div>
          <span class="err-worker">${STAGE_WORKER[e.stage] || "—"}</span>
        </div>`;
      })}
      </div>
      ${details && pages > 1 && html`<div class="hist-pager">
        <span class="mono muted">${current * ERRORS_PER_PAGE + 1}–${Math.min((current + 1) * ERRORS_PER_PAGE, list.length)} из ${list.length}</span>
        <div class="hist-pages">
          <button class="btn page-btn" disabled=${current === 0} onClick=${() => setPage(current - 1)} aria-label="Предыдущая страница">${PREV}</button>
          ${pageChips(current, pages).map((n) => html`<button key=${n} class=${cx("btn", "page-btn", n === current && "is-on")} onClick=${() => setPage(n)}>${n + 1}</button>`)}
          <button class="btn page-btn" disabled=${current >= pages - 1} onClick=${() => setPage(current + 1)} aria-label="Следующая страница">${NEXT}</button>
        </div>
      </div>`}
    </div>`}
    ${list.length > ERRORS_BRIEF && html`<button class="btn-link err-more" onClick=${() => { setDetails(!details); setPage(0); }} aria-expanded=${details}>${details ? "Свернуть" : "Подробнее"}</button>`}
  </section>`;
}

// выгрузка CSV: браузер скачивает файл по ссылке, сервис отдаёт его с Content-Disposition
const EXPORTS = [
  { href: "/api/export.zip", label: "Все файлы (ZIP)" },
  { href: "/api/export/all.csv", label: "Всё в одном файле", key: "games" },
  { href: "/api/export/games.csv", label: "Игры", key: "games" },
  { href: "/api/export/summaries.csv", label: "Резюме", key: "summaries" },
  { href: "/api/export/letsplays.csv", label: "Летсплеи", key: "letsplays" },
  { href: "/api/export/runs.csv", label: "История прогонов", key: "runs" },
];

function CatalogData({ data }) {
  const [ask, setAsk] = useState(false);
  const [picker, setPicker] = useState(false);
  const [menu, setMenu] = useState(false);
  const [busy, setBusy] = useState(false);
  const exportBox = useRef(null);
  useEffect(() => {
    if (!menu) return undefined;
    // меню закрывается кликом мимо и по Esc, как список сортировки в каталоге
    const onDown = (e) => { if (exportBox.current && !exportBox.current.contains(e.target)) setMenu(false); };
    const onKey = (e) => { if (e.key === "Escape") setMenu(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [menu]);
  const storage = data.storage || { archived: false, path: "", count: 0, snapshots: [] };
  const games = data.games;
  const iterations = storage.count ? count(storage.count, "итерация", "итерации", "итераций") : "";
  const hidden = storage.archived && games === 0;
  // плашка нужна только когда каталог убран в хранилище: обычное состояние и так видно по строке ниже
  const state = games > 0
    ? `${count(games, "игра", "игры", "игр")} ${plural(games, "показана", "показаны", "показаны")} в каталоге`
      + (iterations ? `, в хранилище ${iterations}` : "")
    : storage.archived ? `Каталог скрыт из интерфейса, в хранилище ${iterations}`
    : "Каталог пуст: игры появятся после первого прогона";
  // сколько строк будет в каждом файле: видно прямо в меню выгрузки
  const sums = data.summaries || { critic: 0, user: 0 };
  const rows = {
    games,
    summaries: sums.critic + sums.user,
    letsplays: Object.values(data.letsplays || {}).reduce((total, n) => total + n, 0),
    runs: data.runs_total || data.runs.length,
  };

  async function clear() {
    setBusy(true);
    try {
      await api("/storage/clear", { method: "POST" });
      toast("Данные убраны из интерфейса и сохранены итерацией в хранилище", "acc");
      setAsk(false);
      await refreshStatus();
    } catch (e) {
      toast(e.status === 409 && (e.detail || "").includes("run") ? "Идёт прогон: данные можно тронуть после него"
        : e.status === 409 ? "Каталог уже пуст" : "Сервис не ответил, данные не тронуты", "red");
    } finally {
      setBusy(false);
    }
  }

  return html`<section class="panel">
    <${SecHead} icon=${I.box} tint="violet" title="Данные каталога" sub="Игры, резюме, летсплеи и история прогонов в интерфейсе и итерации в хранилище на диске">
      ${hidden && html`<span class="state-chip" style=${{ background: "rgba(245,179,60,.14)", color: YELLOW }}>В хранилище</span>`}
    </${SecHead}>
    <div class="store-row">
      <div class="store-text">
        <div class="store-state">${state}</div>
        <div class="store-path">${storage.path}</div>
      </div>
      ${storage.archived && html`<button class="btn" onClick=${() => setPicker(true)} disabled=${busy}>Подгрузить</button>`}
      <div class="store-export" ref=${exportBox}>
        <button class="btn" onClick=${() => setMenu(!menu)} aria-haspopup="menu" aria-expanded=${menu}>Выгрузить</button>
        ${menu && html`<div class="store-menu" role="menu">
          ${EXPORTS.map((item) => html`<a role="menuitem" key=${item.href} href=${item.href} onClick=${() => setMenu(false)}>
            <span>${item.label}</span>${item.key && html`<span class="store-menu-count">${fmt(rows[item.key])}</span>`}
          </a>`)}
        </div>`}
      </div>
      ${games > 0 && html`<button class="btn btn-danger" onClick=${() => setAsk(true)} disabled=${busy}>Опустошить</button>`}
    </div>
    <div class="store-note">"Выгрузить" отдаёт данные в CSV: игры, резюме, летсплеи и историю прогонов. Те же файлы сервис сохраняет сам после каждого прогона, в папке exports рядом с хранилищем. "Опустошить" сохраняет всё, что сервис собрал с прошлой очистки, отдельной итерацией с периодом прогонов и очищает интерфейс для чистого прогона. "Подгрузить" открывает список итераций: любую можно добавить к текущему каталогу или заменить им текущий. Настройки сервиса, модель и расписание не трогаются.</div>
    ${ask && html`<div class="modal-back" onClick=${() => setAsk(false)}>
      <div class="modal modal-sm" role="dialog" aria-modal="true" aria-label="Опустошить данные" onClick=${(e) => e.stopPropagation()}>
        <div class="modal-title">Опустошить данные?</div>
        <div class="modal-sub">Каталог, резюме, летсплеи и история прогонов скроются из интерфейса: сервис будет выглядеть как чистый. Всё собранное с прошлой очистки сохранится в хранилище итерацией с периодом прогонов, её можно подгрузить из списка.</div>
        <div class="store-path store-path-box">${storage.path}</div>
        <div class="modal-actions">
          <button class="btn btn-danger" onClick=${clear} disabled=${busy}>${busy ? "Убираем…" : "Опустошить"}</button>
          <button class="btn btn-ghost" onClick=${() => setAsk(false)}>Отмена</button>
        </div>
      </div>
    </div>`}
    ${picker && html`<${StorageModal} storage=${storage} catalogGames=${games} onClose=${() => setPicker(false)} />`}
  </section>`;
}

export function Monitor({ status }) {
  const now = useTick(1);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [calendar, setCalendar] = useState(false);
  const data = status.data;
  if (!data) {
    return html`<div><div class="page-head"><h1 class="page-title">Мониторинг сервиса</h1></div>
      <div class="empty-box"><div class="empty-title">Получаем состояние сервиса…</div>
      <div class="empty-text">${status.online ? "Соединение открыто, ждём первые данные." : "Подключаемся к потоку событий сервиса."}</div></div></div>`;
  }
  const onStart = async () => { setStarting(true); await requestRun(); setStarting(false); };
  return html`<div>
    <${Header} status=${status} now=${now} starting=${starting} onStart=${onStart} onStop=${() => setStopping(true)} onCalendar=${() => setCalendar(true)} />
    ${stopping && html`<${StopRunModal} pause=${false} onClose=${() => setStopping(false)} />`}
    ${calendar && html`<${CalendarModal} onClose=${() => setCalendar(false)} />`}
    <${Kpis} data=${data} />
    <div class="mon-row"><${CurrentRun} data=${data} /></div>
    <div class="mon-row mon-row-3">
      <${Health} status=${status} />
      <${Gpu} data=${data} />
      <${Llm} data=${data} />
    </div>
    <div class="mon-row mon-row-2">
      <${Workers} data=${data} now=${now} />
      <${Errors} data=${data} now=${now} />
    </div>
    <div class="mon-row"><${Checks} data=${data} now=${now} /></div>
    <div class="mon-row"><${History} data=${data} onStart=${onStart} /></div>
    <div class="mon-row"><${CatalogData} data=${data} /></div>
  </div>`;
}
