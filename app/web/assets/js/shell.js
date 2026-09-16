// Каркас: боковое меню с расписанием, переключатель темы, уведомления, подсказки и карточки пунктов.
import { useEffect, useState } from "preact/hooks";
import { link, stopRun } from "./api.js";
import { ScheduleModal, toggleSchedule } from "./schedule.js";
import { cueHide, cueHold, cx, fmt, html, onOverlay, overlay, store, timeOf, until, zoneSuffix } from "./lib.js";

const THEME_KEY = "skytec-parser-theme";
const NAV_KEY = "skytec-parser-nav";
export const LAST_GAME_KEY = "skytec-parser-last-game";

function useOverlay() {
  const [, force] = useState(0);
  useEffect(() => onOverlay(() => force((n) => n + 1)), []);
  return overlay;
}

// раз в N секунд перерисовать: часы, обратные отсчёты, "5 с назад"
export function useTick(seconds = 1) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), seconds * 1000);
    return () => clearInterval(timer);
  }, [seconds]);
  return now;
}

// иконки разделов: прежние по рисунку, но перечерчены по целым координатам сетки 24 и выводятся
// размером 24 — один к одному, поэтому края линий ложатся на пиксели и не мылятся.
// Рамки обводкой 2, полоски внутри шириной 3: тоньше их при таком размере уже не видно
const nav = (shapes) => (c) => html`<svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">${shapes(c)}</svg>`;
const frame = (c, x, y, w, h) => html`<rect x=${x} y=${y} width=${w} height=${h} rx="4" stroke=${c} stroke-width="2"/>`;
const bar = (x, y, w, h, c, opacity) => html`<rect x=${x} y=${y} width=${w} height=${h} rx=${w / 2} fill=${c} opacity=${opacity}/>`;
const ICONS = {
  catalog: nav((c) => html`${frame(c, 3, 4, 18, 16)}${bar(7, 8.5, 3, 7, c)}${bar(14, 8.5, 3, 7, c)}`),
  game: nav((c) => html`${frame(c, 3, 4, 12, 16)}${bar(18, 8, 3, 8, c, 0.55)}${bar(6, 15, 6, 2, c)}`),
  monitor: nav((c) => html`${frame(c, 3, 4, 18, 16)}${bar(6, 12, 3, 4.5, c)}${bar(11, 8.5, 3, 8, c)}${bar(16, 10.5, 3, 6, c)}`),
};

export function Signal({ color, animated = true, small = false }) {
  const bars = small ? [6.6, 12, 9] : [13, 13, 13];
  return html`<span class=${cx("signal", small && "signal-small")} aria-hidden="true">
    ${bars.map((height, i) => html`<span style=${{ height: height + "px", background: color, animation: animated ? `sigBar ${1.1 + i * 0.15}s ease-in-out ${i * 0.18}s infinite` : "none" }}></span>`)}
  </span>`;
}

function applyTheme(mode) {
  document.documentElement.setAttribute("data-omtheme", mode);
  store.set(THEME_KEY, mode);
}

/** Окно подтверждения остановки прогона. Из мониторинга останавливается только прогон, из бокового
 * меню ещё и расписание ставится на паузу (pause=true): конвейер выключен, пока его не включат. */
export function StopRunModal({ pause, config, onClose }) {
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);
  const confirm = async () => {
    setBusy(true);
    const stopped = await stopRun();
    if (stopped && pause && config && config.enabled) await toggleSchedule(config);
    setBusy(false);
    onClose();
  };
  return html`<div class="modal-back" onClick=${onClose}>
    <div class="modal modal-sm" role="dialog" aria-modal="true" aria-label="Прервать прогон" onClick=${(e) => e.stopPropagation()}>
      <div class="modal-title">Прервать прогон?</div>
      <div class="modal-sub">${pause
        ? "Текущий шаг доделается, прогон будет помечен прерванным, а расписание встанет на паузу: новые прогоны не начнутся, пока вы не включите его снова. Собранное остаётся в базе."
        : "Текущий шаг доделается, прогон будет помечен прерванным. Собранное остаётся в базе, следующий прогон пойдёт по расписанию."}</div>
      <div class="modal-actions">
        <button class="btn btn-danger" onClick=${confirm} disabled=${busy}>${busy ? "Останавливаем…" : pause ? "Прервать и выключить" : "Прервать"}</button>
        <button class="btn btn-ghost" onClick=${onClose}>Отмена</button>
      </div>
    </div>
  </div>`;
}

function Health({ status, now, onConfigure }) {
  const [stopping, setStopping] = useState(false);
  const data = status.data;
  const scheduler = data && data.scheduler;
  const running = !!(data && data.run);
  const active = !!(scheduler && scheduler.active);
  const label = !data ? "Подключение" : !status.online ? "Переподключение" : running ? "Идёт прогон" : "Сервис работает";
  const nextAt = scheduler && scheduler.next_run_at;
  // строки по макету: правило, ближайший запуск, сколько до него; состав прогона виден в окне настроек.
  // Подпись и значение читаются одной фразой ("Запуск только вручную"), поэтому слова не повторяются
  const rows = [
    ["Запуск", scheduler && scheduler.enabled ? (active ? scheduler.rule : "только вручную") : "только из консоли"],
    ["Следующий", running ? "идёт сейчас" : active && nextAt ? "в " + timeOf(nextAt, false) + zoneSuffix() : "не назначен"],
    ["Осталось", !running && active && nextAt ? until(nextAt, now) : "—"],
  ];
  return html`<div class="sb-health">
    <div class="sb-health-top">
      <div class="sb-health-row">
        <${Signal} color=${status.online ? "var(--green-live)" : "var(--yellow)"} />
        <span class="sb-health-label">${label}</span>
      </div>
      ${scheduler && scheduler.enabled && !active && html`<span class="sb-pill">на паузе</span>`}
    </div>
    <div class="sb-sched">
      ${rows.map(([label, value]) => html`<div class="sb-sched-row" key=${label}><span>${label}</span><b>${value}</b></div>`)}
    </div>
    ${scheduler && scheduler.enabled
      ? html`<div class="sb-sched-actions">
          <button onClick=${onConfigure}>Настроить</button>
          ${running
            ? html`<button class="is-stop" onClick=${() => setStopping(true)}>Остановить</button>`
            : html`<button onClick=${() => toggleSchedule(scheduler.config)}>${active ? "Пауза" : "Включить"}</button>`}
        </div>
        ${stopping && html`<${StopRunModal} pause=${true} config=${scheduler.config} onClose=${() => setStopping(false)} />`}`
      : html`<div class="sb-health-sub">Планировщик выключен настройкой SCHEDULER_ENABLED: прогоны только из консоли.</div>`}
    <div class="sb-health-clock">Обновлено в ${status.updatedAt ? timeOf(new Date(status.updatedAt).toISOString(), false) + zoneSuffix() : "—"}</div>
  </div>`;
}

export function Sidebar({ route, status }) {
  const [open, setOpen] = useState(store.get(NAV_KEY, "open") !== "closed");
  const [theme, setTheme] = useState(document.documentElement.getAttribute("data-omtheme") || "light");
  const [schedule, setSchedule] = useState(false);
  const now = useTick(1);
  const data = status.data;
  const running = !!(data && data.run);
  const lastGame = store.get(LAST_GAME_KEY);

  const toggle = () => { const next = !open; setOpen(next); store.set(NAV_KEY, next ? "open" : "closed"); };
  const pickTheme = (mode) => { applyTheme(mode); setTheme(mode); };

  const items = [
    { id: "catalog", label: "Каталог игр", href: link.catalog(), meta: data ? fmt(data.games) : "" },
    // без открытой ранее игры "Карточка игры" ведёт в каталог: выбрать игру можно только там
    { id: "game", label: "Карточка игры", href: lastGame ? link.game(lastGame) : link.catalog(), meta: "" },
    { id: "monitor", label: "Мониторинг", href: link.monitor(), meta: running ? "прогон" : "" },
  ];

  return html`<aside class=${cx("sidebar", !open && "is-collapsed")}>
    <div class="sb-head">
      ${open && html`<a class="brand" href=${link.catalog()} aria-label="Каталог игр">
        <div class="brand-sky">SKYTEC</div><div class="brand-games">GAMES</div></a>`}
      <button class="sb-toggle" onClick=${toggle} title=${open ? "Свернуть меню" : "Развернуть меню"}
        aria-label=${open ? "Свернуть меню" : "Развернуть меню"}>
        <svg width="8" height="13" viewBox="0 0 8 13" fill="none" aria-hidden="true">
          <path d=${open ? "m6.4 1.4-4.8 5.1 4.8 5.1" : "m1.6 1.4 4.8 5.1-4.8 5.1"} stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg></button>
    </div>
    <nav class="sb-nav" aria-label="Разделы">
      ${items.map((item) => {
        const active = route.screen === item.id;
        const color = active ? "var(--sb-on)" : "var(--sb-t2)";
        return html`<a class=${cx("sb-item", active && "is-active")} href=${item.href} title=${open ? undefined : item.label}
          aria-current=${active ? "page" : undefined} key=${item.id}>
          ${ICONS[item.id](color)}
          ${open && html`<span class="sb-label">${item.label}</span>`}
          ${open && item.meta && html`<span class="sb-meta">${item.meta}</span>`}
        </a>`;
      })}
    </nav>
    <div class="sb-theme" role="group" aria-label="Тема">
      <button class=${cx(theme === "light" && "is-on")} onClick=${() => pickTheme("light")} title="Светлая тема" aria-pressed=${theme === "light"}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 19.25C8 19.25 4.75 16 4.75 12C4.75 8 8 4.75 12 4.75C16 4.75 19.25 8 19.25 12C19.25 16 16 19.25 12 19.25ZM12 6.25C8.83 6.25 6.25 8.83 6.25 12C6.25 15.17 8.83 17.75 12 17.75C15.17 17.75 17.75 15.17 17.75 12C17.75 8.83 15.17 6.25 12 6.25ZM12 22.96C11.45 22.96 11 22.55 11 22V21.92C11 21.37 11.45 20.92 12 20.92C12.55 20.92 13 21.37 13 21.92C13 22.47 12.55 22.96 12 22.96ZM19.14 20.14C18.88 20.14 18.63 20.04 18.43 19.85L18.3 19.72C17.91 19.33 17.91 18.7 18.3 18.31C18.69 17.92 19.32 17.92 19.71 18.31L19.84 18.44C20.23 18.83 20.23 19.46 19.84 19.85C19.65 20.04 19.4 20.14 19.14 20.14ZM4.86 20.14C4.6 20.14 4.35 20.04 4.15 19.85C3.76 19.46 3.76 18.83 4.15 18.44L4.28 18.31C4.67 17.92 5.3 17.92 5.69 18.31C6.08 18.7 6.08 19.33 5.69 19.72L5.56 19.85C5.37 20.04 5.11 20.14 4.86 20.14ZM22 13H21.92C21.37 13 20.92 12.55 20.92 12C20.92 11.45 21.37 11 21.92 11C22.47 11 22.96 11.45 22.96 12C22.96 12.55 22.55 13 22 13ZM2.08 13H2C1.45 13 1 12.55 1 12C1 11.45 1.45 11 2 11C2.55 11 3.04 11.45 3.04 12C3.04 12.55 2.63 13 2.08 13ZM19.01 5.99C18.75 5.99 18.5 5.89 18.3 5.7C17.91 5.31 17.91 4.68 18.3 4.29L18.43 4.16C18.82 3.77 19.45 3.77 19.84 4.16C20.23 4.55 20.23 5.18 19.84 5.57L19.71 5.7C19.52 5.89 19.27 5.99 19.01 5.99ZM4.99 5.99C4.73 5.99 4.48 5.89 4.28 5.7L4.15 5.56C3.76 5.17 3.76 4.54 4.15 4.15C4.54 3.76 5.17 3.76 5.56 4.15L5.69 4.28C6.08 4.67 6.08 5.3 5.69 5.69C5.5 5.89 5.24 5.99 4.99 5.99ZM12 3.04C11.45 3.04 11 2.63 11 2.08V2C11 1.45 11.45 1 12 1C12.55 1 13 1.45 13 2C13 2.55 12.55 3.04 12 3.04Z" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/></svg>
        ${open && html`<span>Светлая</span>`}
      </button>
      <button class=${cx(theme === "dark" && "is-on")} onClick=${() => pickTheme("dark")} title="Тёмная тема" aria-pressed=${theme === "dark"}>
        <svg width="20" height="20" viewBox="15.65 32.94 24 24" fill="none" aria-hidden="true"><path d="M28.46 55.75C28.29 55.75 28.12 55.75 27.95 55.74C22.35 55.49 17.67 50.98 17.28 45.48C16.94 40.76 19.67 36.35 24.07 34.5C25.32 33.98 25.98 34.38 26.26 34.67C26.54 34.95 26.93 35.6 26.41 36.79C25.95 37.85 25.72 38.98 25.73 40.14C25.75 44.57 29.43 48.33 33.92 48.51C34.57 48.54 35.21 48.49 35.83 48.38C37.15 48.14 37.7 48.67 37.91 49.01C38.12 49.35 38.36 50.08 37.56 51.16C35.44 54.06 32.07 55.75 28.46 55.75ZM18.77 45.37C19.11 50.13 23.17 54.03 28.01 54.24C31.3 54.4 34.42 52.9 36.34 50.28C36.49 50.07 36.56 49.92 36.59 49.84C36.5 49.83 36.34 49.82 36.09 49.87C35.36 50 34.6 50.05 33.85 50.02C28.57 49.81 24.25 45.38 24.22 40.16C24.22 38.78 24.49 37.45 25.04 36.2C25.14 35.98 25.16 35.83 25.17 35.75C25.08 35.75 24.92 35.77 24.66 35.88C20.85 37.48 18.49 41.3 18.77 45.37Z" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/></svg>
        ${open && html`<span>Тёмная</span>`}
      </button>
    </div>
    ${open && html`<${Health} status=${status} now=${now} onConfigure=${() => setSchedule(true)} />`}
    ${schedule && data && data.scheduler && data.scheduler.config
      && html`<${ScheduleModal} scheduler=${data.scheduler} letsplay=${data.letsplay_settings} running=${running} onClose=${() => setSchedule(false)} />`}
  </aside>`;
}

function Cue({ cue }) {
  const style = {
    left: cue.left + "px", top: cue.top + "px", width: cue.width + "px",
    transform: cue.middle ? "translateY(-50%)" : "none",
  };
  return html`<div class="cue" style=${style} onMouseEnter=${cueHold} onMouseLeave=${() => cueHide()} role="dialog" aria-label=${cue.title}>
    <div class="cue-body">
    <div class="cue-group">${cue.group}</div>
    <div class="cue-title">${cue.title}</div>
    <div class="cue-sub">${cue.sub}</div>
    <div class="cue-rows">
      ${cue.rows.map((row, i) => row.kind === "text"
        ? html`<pre class="cue-text-block" key=${i}>${row.text}</pre>`
        : row.kind === "moment"
        ? html`<div class="cue-row" key=${i}>
            <a class="cue-time" href=${row.url} target="_blank" rel="noopener noreferrer">${row.label}</a>
            <span class="cue-quote">«${row.quote}»</span>
          </div>`
        : html`<div class="cue-src" key=${i}>
            <div class="cue-src-top">
              <span class="cue-score" style=${row.score.style}>${row.score.text}</span>
              <span class="cue-author">${row.author}</span>
              <span class="cue-date">${row.date}</span>
            </div>
            ${row.quote && html`<span class="cue-quote">«${row.quote}»</span>`}
            ${row.url && html`<a class="cue-link" href=${row.url} target="_blank" rel="noopener noreferrer">${row.linkLabel}</a>`}
          </div>`)}
    </div>
    </div>
  </div>`;
}

export function Overlays() {
  const { toast, tip, cue } = useOverlay();
  // карточка привязана к месту пункта на экране: при прокрутке и по Esc закрываем
  useEffect(() => {
    const close = () => cueHide(0);
    const onKey = (e) => { if (e.key === "Escape") close(); };
    addEventListener("scroll", close, { passive: true });
    addEventListener("keydown", onKey);
    return () => { removeEventListener("scroll", close); removeEventListener("keydown", onKey); };
  }, []);
  return html`
    ${toast && html`<div class="toast" role="status">
      <${Signal} color=${toast.color} animated=${false} small=${true} /><span>${toast.text}</span></div>`}
    ${tip && html`<div class=${cx("tip", tip.below && "is-below")} style=${{ left: tip.x + "px", top: tip.y + "px" }}>${tip.text}</div>`}
    ${cue && html`<${Cue} cue=${cue} />`}`;
}
