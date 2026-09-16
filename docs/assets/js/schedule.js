// Окно "Расписание и прогон" по макету 15.09: карточки "как часто" и "настройки прогона", полоса
// следующего запуска, карточка летсплеев. Списки времени и даты свои, а не системные: у системных
// формат зависит от языка Windows (там появлялись AM и PM и порядок ММ/ДД), и вид у них чужой для обеих тем.
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { api, refreshStatus } from "./api.js";
import { cx, html, plural, todayIso, toast, whenText, zoneLabel } from "./lib.js";

const MODES = [
  ["hourly", "Каждый час"], ["daily", "Каждый день"], ["days", "Каждые N дней"], ["monthly", "Раз в месяц"],
];
const MIN_GAMES = 1;
const MAX_GAMES = 500;
// летсплеи: границы длительности ролика в минутах, 0 сверху значит без ограничения
// повторный поиск для игр без летсплея: интервал в днях и лимит поисков на игру (0 без ограничения)
const RECHECK_DAYS = [[1, "Каждый день"], [7, "Раз в неделю"], [14, "Раз в 2 недели"], [30, "Раз в месяц"], [90, "Раз в 3 месяца"]];
const RECHECK_LIMIT_BACK = 3;
const LETSPLAY_DEFAULT = { min_minutes: 10, max_minutes: 120, recheck: true, recheck_days: 30, recheck_max: 3, enabled: true, full_max_minutes: 240, chunk_tokens: { local: 8000, cloud: 15000 } };
// какой предел ставится, когда "без ограничения" выключают обратно
const LETSPLAY_LIMIT_BACK = 120;
// длительность в подписи: "2 ч 30 мин", "45 мин", "без ограничения"
// пример правила по дням: "обновлённые 16 сентября пора обновить 23 сентября"
function staleExample(days) {
  const from = new Date(); from.setDate(from.getDate() - days);
  const fmtDay = (d) => `${d.getDate()} ${MONTHS_OF[d.getMonth()]}`;
  return `Счёт по дням: игры, обновлённые ${fmtDay(from)}, пора обновить сегодня, в каком бы часу их ни обновляли.`;
}

// короткая строка под вторым таймером: сколько игр ждут обновления
function staleShort(view, draft) {
  const due = view && view.stale_due ? view.stale_due[String(draft.stale_days)] : null;
  if (due == null) return "Самые давние идут первыми";
  return due ? `Пора обновить ${due} ${plural(due, "игру", "игры", "игр")}, самые давние первыми` : "Сейчас все игры обновлены вовремя";
}

// второй таймер окна: когда старые игры пойдут на обновление по выбранному сроку
function staleTimer(view, draft) {
  if (!draft.stale) return "выключено";
  if (!view || !view.stale_due) return "считаем…";
  const due = view.stale_due[String(draft.stale_days)];
  if (due) return `ближайший прогон, ${Math.min(due, draft.stale_games)} ${plural(Math.min(due, draft.stale_games), "игра", "игры", "игр")}`;
  if (!view.stale_next) return "пока не нужно";
  const at = view.stale_next[String(draft.stale_days)];
  return at ? `с ${whenText(at)}` : "игр в каталоге нет";
}


// "раз в месяц", "каждый день": интервал повторного поиска в подписи переключателя
function recheckText(days) {
  return { 1: "каждый день", 7: "раз в неделю", 14: "раз в 2 недели", 30: "раз в месяц", 90: "раз в 3 месяца" }[days] || `раз в ${days} дн.`;
}

function spanText(minutes) {
  if (!minutes) return "без ограничения";
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return [h && `${h} ч`, m && `${m} мин`].filter(Boolean).join(" ");
}
// порог пересборки резюме: насколько должно вырасти число отзывов
const CHANGES = [
  [0, "Любой отзыв", "Резюме обновляется от каждого нового отзыва, модель работает чаще всего."],
  [0.05, "+5% отзывов", "Резюме обновляется почти сразу: для игры со 100 отзывами это 5 новых."],
  [0.1, "+10% отзывов", "Обычный порог: для игры со 100 отзывами это 10 новых отзывов."],
  [0.2, "+20% отзывов", "Редкая пересборка: для игры со 100 отзывами это 20 новых отзывов."],
  [0.5, "+50% отзывов", "Самый редкий порог: резюме ждёт, пока отзывов станет в полтора раза больше."],
];
const RUN_DEFAULT = { games: 20, stale: true, stale_days: 7, stale_games: 10, change: 0.1 };
// перепроверка каталога: через сколько дней игра считается устаревшей
const STALE_DAYS = [[1, "1 день"], [3, "3 дня"], [7, "Неделя"], [14, "2 недели"], [30, "Месяц"]];
const STALE_TEXT = { 1: "раз в день", 3: "раз в 3 дня", 7: "раз в неделю", 14: "раз в 2 недели", 30: "раз в месяц" };
const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
const MONTHS_OF = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const startOf = (d) => { const [y, m, day] = d.start.split("-").map(Number); return `${day} ${MONTHS_OF[m - 1]} ${y}`; };
// список из трёх дат читался как ограничение ("поработает три часа и всё"), поэтому показываем
// ближайший запуск и правило повтора
const REPEAT = {
  hourly: () => "Дальше каждый час, пока расписание включено",
  daily: (d) => `Дальше каждый день в ${d.time}, пока расписание включено`,
  days: (d) => `Дальше каждые ${d.every} дн. в ${d.time}, отсчёт с ${startOf(d)}`,
  monthly: (d) => `Дальше ${d.month_day}-го числа каждого месяца в ${d.time}`,
};
const zoneSuffix = () => (zoneLabel() ? ", " + zoneLabel() : "");
const two = (n) => String(n).padStart(2, "0");
const range = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
const daysInMonth = (year, month) => new Date(year, month, 0).getDate();

const body = (draft) => ({
  enabled: true, mode: draft.mode, time: draft.time, every: draft.every, month_day: draft.month_day,
  start: draft.mode === "daily" || draft.mode === "days" ? draft.start : null,
  run: { games: draft.games, stale: draft.stale, stale_days: draft.stale_days, stale_games: draft.stale_games, change: draft.change },
  letsplay: {
    min_minutes: draft.lp_min, max_minutes: draft.lp_max,
    recheck: draft.lp_recheck, recheck_days: draft.lp_recheck_days, recheck_max: draft.lp_recheck_max,
  },
});
const same = (a, b) => JSON.stringify(body(a)) === JSON.stringify(body(b));

/** Выпадающий список в стиле интерфейса: свой вид в обеих темах и никакой зависимости от локали системы. */
function Picker({ value, options, onPick, label, width }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  const menu = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey, true); };
  }, [open]);
  // длинный список (минуты, дни) открывается на выбранном значении, а не в начале
  useLayoutEffect(() => {
    if (!open || !menu.current) return;
    const active = menu.current.querySelector(".is-on");
    if (active) active.scrollIntoView({ block: "center" });
  }, [open]);
  const current = options.find(([v]) => v === value);
  return html`<div class="picker-box" ref=${box} style=${width ? { width: width + "px" } : null}>
    <button type="button" class=${cx("picker-btn", open && "is-open")} onClick=${() => setOpen(!open)}
      aria-haspopup="listbox" aria-expanded=${open} aria-label=${label}>
      <span>${current ? current[1] : value}</span>
      <svg width="12" height="8" viewBox="0 0 14 9" fill="none" aria-hidden="true"><path d="m1.5 1.8 5.5 5.4 5.5-5.4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    ${open && html`<div class="picker-menu">
      <div class="picker-list" role="listbox" ref=${menu}>
        ${options.map(([v, text]) => html`<button type="button" role="option" key=${v} aria-selected=${v === value}
          class=${cx(v === value && "is-on")} onClick=${() => { onPick(v); setOpen(false); }}>${text}</button>`)}
      </div>
    </div>`}
  </div>`;
}

/** Число вводится руками: готовые варианты тут только мешают.
 *
 * Значение уходит наружу сразу при вводе, иначе кнопка "Сохранить" могла бы отправить прежнее число.
 * Больше max сразу опускается до max, а пустое поле восстанавливается, когда из него уходят.
 */
export function NumberField({ value, min, max, unit, label, onChange }) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  const type = (raw) => {
    const digits = raw.replace(/\D/g, "").slice(0, String(max).length);
    const typed = Number(digits);
    const fixed = typed > max ? max : typed;
    setText(fixed && fixed !== typed ? String(fixed) : digits);
    if (fixed >= min && fixed !== value) onChange(fixed);
  };
  return html`<div class="num-field">
    <input class="num-input" inputmode="numeric" autocomplete="off" aria-label=${label} value=${text}
      onInput=${(e) => type(e.target.value)}
      onBlur=${() => (text !== "" && Number(text) >= min) || setText(String(value))}
      onKeyDown=${(e) => { if (e.key === "Enter") e.target.blur(); }} />
    <span class="num-unit">${unit(Number(text) || value)}</span>
  </div>`;
}

/** Переключатель настройки: по нажатию и по пробелу, состояние читают и с клавиатуры. */
export function Switch({ on, onToggle, title, note }) {
  return html`<div class="toggle-card">
    <div class="toggle-text">
      <b>${title}</b>
      <span>${note}</span>
    </div>
    <button type="button" role="switch" aria-checked=${on} aria-label=${title}
      class=${cx("switch", on && "is-on")} onClick=${onToggle}><span class="switch-knob"></span></button>
  </div>`;
}

export function ScheduleModal({ scheduler, letsplay, running, onClose }) {
  const lp = { ...LETSPLAY_DEFAULT, ...(letsplay || {}) };
  const saved = {
    ...scheduler.config, start: scheduler.config.start || todayIso(), ...RUN_DEFAULT, ...(scheduler.run || {}),
    lp_min: lp.min_minutes, lp_max: lp.max_minutes,
    lp_recheck: lp.recheck !== false, lp_recheck_days: lp.recheck_days || 30, lp_recheck_max: lp.recheck_max ?? 3,
  };
  const [draft, setDraft] = useState(saved);
  const [next, setNext] = useState(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const set = (patch) => setDraft((d) => ({ ...d, ...patch }));

  // ближайшие запуски считает сервис: у него пояс сервиса и та же логика, что у планировщика
  useEffect(() => {
    let alive = true;
    const timer = setTimeout(() => {
      api("/schedule/preview", { method: "POST", body: body(draft) })
        .then((view) => alive && setNext(view))
        .catch(() => alive && setNext(null));
    }, 200);
    return () => { alive = false; clearTimeout(timer); };
  }, [JSON.stringify(body(draft))]);

  // закрытие мимо окна, по Esc или крестиком с несохранёнными изменениями: сначала спрашиваем,
  // сохранить их или откатить. "Отмена" откатывает явно и не спрашивает
  const [asking, setAsking] = useState(false);
  const changed = !same(draft, saved);
  const tryClose = useRef(null);
  tryClose.current = () => (changed ? setAsking(true) : onClose());
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (asking) setAsking(false);
      else tryClose.current();
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [asking]);

  const unlimited = draft.lp_max === 0;
  // при снятой верхней границе поле показывает предел, который вернётся после включения
  const lpMax = unlimited ? LETSPLAY_LIMIT_BACK : draft.lp_max;
  // нижняя граница выше верхней: ни один ролик не подойдёт, сохранять такое нельзя
  const lpConflict = !unlimited && draft.lp_min > draft.lp_max;
  const dirty = !same(draft, saved) || !scheduler.config.enabled;
  async function apply() {
    if (!dirty || lpConflict || saving) return;
    setSaving(true);
    setError("");
    try {
      const view = await api("/schedule", { method: "PUT", body: body(draft) });
      toast(`Расписание обновлено: ${view.rule}. Ближайший запуск ${whenText(view.next[0])}`, "acc");
      await refreshStatus();
      onClose();
    } catch (e) {
      setError(e.status === 422 ? "Сервис не принял такие настройки" : "Сервис не ответил, расписание не изменено");
    } finally {
      setSaving(false);
    }
  }

  const minutes = draft.time.slice(3);
  // дата отсчёта нужна только режимам "каждый день" и "каждые N дней"; в остальных поле остаётся
  // на месте приглушённым, чтобы окно не меняло высоту при смене режима
  const dated = draft.mode === "daily" || draft.mode === "days";
  const startPast = draft.start < todayIso();
  const change = CHANGES.find(([value]) => value === draft.change) || CHANGES[2];
  const [year, month, day] = draft.start.split("-").map(Number);
  // подсказка под временем зависит от режима
  const timeHint = draft.mode === "hourly" ? `Берутся только минуты: запуск каждый час в :${minutes}.`
    : draft.mode === "daily" ? (startPast ? "Дата в прошлом это точка отсчёта: ближайший запуск считается от неё." : `Запуск каждый день в ${draft.time}${zoneSuffix()}.`)
    : draft.mode === "days" ? "Интервал считается от даты отсчёта, ручные запуски его не сдвигают."
    : "В коротких месяцах 29, 30 и 31 сдвигаются на последнее число.";
  // около 1,6 минуты на игру: загрузка с Metacritic и два запроса к модели
  const hoursRun = Math.max(1, Math.round(draft.games * 1.6 / 60));
  const canApply = dirty && !lpConflict && !saving;
  return html`<div class="modal-back" onClick=${() => tryClose.current()}>
    <div class="modal modal-wide" role="dialog" aria-modal="true" aria-label="Расписание и прогон" onClick=${(e) => e.stopPropagation()}>
      <div class="modal-head modal-head-wide">
        <span class="sec-icon" style=${{ background: "rgba(43,123,255,.14)" }}>
          <svg width="23" height="23" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="3" stroke="var(--acc-2)" stroke-width="1.8"/><path d="M3 10h18M8 2.6v3.8M16 2.6v3.8" stroke="var(--acc-2)" stroke-width="1.8" stroke-linecap="round"/><path d="M12 13.4v3l2.2 1.4" stroke="var(--acc-2)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
        <div class="modal-head-text">
          <div class="modal-title">Расписание и прогон</div>
          <div class="modal-sub">Время по поясу сервиса${zoneLabel() ? " (" + zoneLabel() + ")" : ""}. Ручной запуск работает независимо от расписания.</div>
        </div>
        <button type="button" class="modal-close" onClick=${() => tryClose.current()} aria-label="Закрыть">✕</button>
      </div>

      <div class="sched-grid">
        <section class="sched-card">
          <div class="field-label">как часто</div>
          <div class="mode-grid">
            ${MODES.map(([id, label]) => html`<button class=${cx("mode", draft.mode === id && "is-on")} key=${id}
              onClick=${() => set({ mode: id })} aria-pressed=${draft.mode === id}>${label}</button>`)}
          </div>
          <div class="sched-fields">
            <div class="field">
              <span>Время запуска${zoneSuffix()}</span>
              <div class="time-grid">
                <${Picker} value=${draft.time.slice(0, 2)} label="Часы" options=${range(0, 23).map((h) => [two(h), two(h)])}
                  onPick=${(h) => set({ time: `${h}:${minutes}` })} />
                <span class="picker-sep">:</span>
                <${Picker} value=${minutes} label="Минуты" options=${range(0, 59).map((m) => [two(m), two(m)])}
                  onPick=${(m) => set({ time: `${draft.time.slice(0, 2)}:${m}` })} />
              </div>
            </div>
            ${draft.mode === "days" && html`<div class="field">
              <span>Интервал</span>
              <${Picker} value=${String(draft.every)} label="Интервал в днях"
                options=${range(1, 60).map((n) => [String(n), `${n} дн.`])}
                onPick=${(every) => set({ every: Number(every) })} />
            </div>`}
            ${draft.mode === "monthly" && html`<div class="field">
              <span>День месяца</span>
              <${Picker} value=${String(draft.month_day)} label="День месяца"
                options=${range(1, 31).map((n) => [String(n), `${n}-го`])}
                onPick=${(d) => set({ month_day: Number(d) })} />
            </div>`}
            <div class=${cx("field field-span", !dated && "is-off")} aria-hidden=${!dated}>
              <span>Отсчёт с даты</span>
              <div class="date-grid">
                <${Picker} value=${String(day)} label="День" options=${range(1, daysInMonth(year, month)).map((d) => [String(d), String(d)])}
                  onPick=${(d) => set({ start: `${year}-${two(month)}-${two(Math.min(Number(d), daysInMonth(year, month)))}` })} />
                <${Picker} value=${String(month)} label="Месяц" options=${MONTHS.map((name, i) => [String(i + 1), name])}
                  onPick=${(m) => set({ start: `${year}-${two(Number(m))}-${two(Math.min(day, daysInMonth(year, Number(m))))}` })} />
                <${Picker} value=${String(year)} label="Год" options=${[...new Set([year, Number(todayIso().slice(0, 4))])].sort().map((y) => [String(y), String(y)])}
                  onPick=${(y) => set({ start: `${y}-${two(month)}-${two(Math.min(day, daysInMonth(Number(y), month)))}` })} />
              </div>
            </div>
          </div>
          <div class="field-hint">${timeHint}</div>
        </section>

        <section class="sched-card">
          <div class="field-label">настройки прогона</div>
          <div class="run-main">
            <div class="field">
              <span>Новых игр за прогон <i class="field-note">${MIN_GAMES}–${MAX_GAMES}</i></span>
              <${NumberField} value=${draft.games} min=${MIN_GAMES} max=${MAX_GAMES} label="Сколько новинок берём за прогон"
                unit=${(n) => plural(n, "игра", "игры", "игр")} onChange=${(games) => set({ games })} />
            </div>
            <div class="run-main-note">Новинки Metacritic: первый прогон дня из New Releases, следующие из SEE ALL. Обработанные сегодня не повторяются.</div>
          </div>
          ${draft.games > 100 && html`<div class="field-hint is-warn">Прогон на ${draft.games} игр займёт около ${hoursRun} ч, следующий запуск подождёт его конца.</div>`}

          <div class="toggle-card run-stale">
            <div class="toggle-text">
              <b>Обновлять старые игры</b>
              <span>${draft.stale
                ? `Старые игры (все, что уже есть в базе) обновляются с Metacritic ${STALE_TEXT[draft.stale_days]}: оценки, отзывы и резюме.`
                : "Старые игры не обновляются: прогон берёт только новые игры с Metacritic."}</span>
            </div>
            <button type="button" role="switch" aria-checked=${draft.stale} aria-label="Обновлять старые игры"
              class=${cx("switch", draft.stale && "is-on")} onClick=${() => set({ stale: !draft.stale })}><span class="switch-knob"></span></button>
          </div>
          ${/* настройки перепроверки на месте и при выключенном переключателе, только приглушены: карточка
               не меняет размер, и обе карточки ряда одной высоты в любом состоянии */ ""}
          <div class=${cx("run-stale-body", !draft.stale && "is-off")} aria-hidden=${!draft.stale}>
            <div class="field">
              <span>Обновлять игру раз в</span>
              <div class="mode-grid run-stale-days">
                ${STALE_DAYS.map(([days, label]) => html`<button type="button" key=${days} class=${cx("mode", draft.stale_days === days && "is-on")}
                  aria-pressed=${draft.stale_days === days} onClick=${() => set({ stale_days: days })}>${label}</button>`)}
              </div>
            </div>
            <div class="sched-fields">
              <div class="field">
                <span>Старых игр за прогон</span>
                <${NumberField} value=${draft.stale_games} min=${MIN_GAMES} max=${MAX_GAMES} label="Сколько старых игр обновляем за прогон"
                  unit=${(n) => plural(n, "игра", "игры", "игр")} onChange=${(stale_games) => set({ stale_games })} />
              </div>
              <div class="field">
                <span>Пересобирать резюме, когда</span>
                <${Picker} value=${String(draft.change)} label="Порог пересборки резюме"
                  options=${CHANGES.map(([value, label]) => [String(value), label])}
                  onPick=${(value) => set({ change: Number(value) })} />
              </div>
            </div>
            <div class="field-hint">${staleExample(draft.stale_days)} ${change[2]}</div>
          </div>
        </section>

        <section class="sched-hero">
          <div class="sched-hero-cell">
            <div class="sched-hero-main">
              <div class="sched-hero-label">следующий запуск</div>
              <div class="sched-hero-when">${next ? whenText(next.next[0]) : "считаем…"}</div>
            </div>
            <div class="sched-hero-side">
              <div class="sched-hero-rule">${REPEAT[draft.mode](draft).replace(", пока расписание включено", "")}</div>
              <div class="sched-hero-note">${draft.mode === "hourly" ? "Ручной запуск не чаще раза в 5 минут" : "Пока расписание включено"}</div>
            </div>
          </div>
          <div class="sched-hero-cell">
            <div class="sched-hero-main">
              <div class="sched-hero-label">обновление старых</div>
              <div class="sched-hero-when">${staleTimer(next, draft)}</div>
            </div>
            <div class="sched-hero-side">
              <div class="sched-hero-rule">${draft.stale
                ? `Каждая игра ${STALE_TEXT[draft.stale_days]}, до ${draft.stale_games} за прогон`
                : "Старые игры не обновляются"}</div>
              <div class="sched-hero-note">${draft.stale ? staleShort(next, draft) : "Включается в настройках прогона"}</div>
            </div>
          </div>
        </section>

        <section class="sched-card sched-card-wide">
          <div class="field-label">летсплеи</div>
          <div class="lp-row">
            <div class="field lp-min">
              <span>Ролик не короче</span>
              <${NumberField} value=${draft.lp_min} min=${1} max=${1440} label="Минимальная длительность ролика, минут"
                unit=${() => "мин"} onChange=${(lp_min) => set({ lp_min })} />
            </div>
            <div class=${cx("field lp-max", unlimited && "is-off")} aria-hidden=${unlimited}>
              <span>Не длиннее</span>
              <div class="lp-max-grid">
                <${NumberField} value=${Math.floor(lpMax / 60)} min=${0} max=${12} label="Часы" unit=${() => "ч"}
                  onChange=${(h) => set({ lp_max: h * 60 + lpMax % 60 })} />
                <${NumberField} value=${lpMax % 60} min=${0} max=${59} label="Минуты" unit=${() => "мин"}
                  onChange=${(m) => set({ lp_max: Math.floor(lpMax / 60) * 60 + m })} />
              </div>
            </div>
            <div class="toggle-card lp-cap">
              <div class="toggle-text">
                <b>Без ограничения</b>
                <span>${unlimited
                  ? `Подходят и многочасовые стримы: ролик до ${spanText(lp.full_max_minutes)} расшифровывается целиком, длиннее только начало и концовка.`
                  : `Ролики длиннее ${spanText(draft.lp_max)} не рассматриваются.`}</span>
              </div>
              <button type="button" role="switch" aria-checked=${unlimited} aria-label="Без ограничения"
                class=${cx("switch", unlimited && "is-on")} onClick=${() => set({ lp_max: unlimited ? LETSPLAY_LIMIT_BACK : 0 })}><span class="switch-knob"></span></button>
            </div>
          </div>
          <div class="toggle-card lp-recheck">
            <div class="toggle-text">
              <b>Искать летсплеи повторно</b>
              <span>${draft.lp_recheck
                ? `Игры каталога, для которых летсплей не нашёлся, перепроверяются ${recheckText(draft.lp_recheck_days)}${draft.lp_recheck_max ? `, не больше ${draft.lp_recheck_max} ${plural(draft.lp_recheck_max, "раза", "раз", "раз")} на игру`.replace(/не больше 1 раза/, "один раз") : ", без ограничения по числу поисков"}: со временем ролик может появиться.`
                : "Если летсплей не нашёлся, игра больше не ищется."}</span>
            </div>
            <button type="button" role="switch" aria-checked=${draft.lp_recheck} aria-label="Искать летсплеи повторно"
              class=${cx("switch", draft.lp_recheck && "is-on")} onClick=${() => set({ lp_recheck: !draft.lp_recheck })}><span class="switch-knob"></span></button>
          </div>
          ${/* как у старых игр: настройки не пропадают при выключенном переключателе, только приглушены */ ""}
          <div class=${cx("lp-recheck-body", !draft.lp_recheck && "is-off")} aria-hidden=${!draft.lp_recheck}>
            <div class="field">
              <span>Как часто</span>
              <div class="mode-grid lp-recheck-days">
                ${RECHECK_DAYS.map(([days, label]) => html`<button type="button" key=${days} class=${cx("mode", draft.lp_recheck_days === days && "is-on")}
                  aria-pressed=${draft.lp_recheck_days === days} onClick=${() => set({ lp_recheck_days: days })}>${label}</button>`)}
              </div>
            </div>
            <div class="lp-recheck-limit">
              <div class=${cx("field", !draft.lp_recheck_max && "is-off")} aria-hidden=${!draft.lp_recheck_max}>
                <span>Поисков на игру <i class="field-note">до 100</i></span>
                <${NumberField} value=${draft.lp_recheck_max || RECHECK_LIMIT_BACK} min=${1} max=${100} label="Сколько раз искать летсплей повторно"
                  unit=${(n) => plural(n, "раз", "раза", "раз")} onChange=${(lp_recheck_max) => set({ lp_recheck_max })} />
              </div>
              <div class="toggle-card lp-cap">
                <div class="toggle-text">
                  <b>Без ограничения</b>
                  <span>${draft.lp_recheck_max ? "Игра ищется не больше заданного числа раз." : "Игра ищется, пока летсплей не найдётся."}</span>
                </div>
                <button type="button" role="switch" aria-checked=${!draft.lp_recheck_max} aria-label="Без ограничения по числу поисков"
                  class=${cx("switch", !draft.lp_recheck_max && "is-on")}
                  onClick=${() => set({ lp_recheck_max: draft.lp_recheck_max ? 0 : RECHECK_LIMIT_BACK })}><span class="switch-knob"></span></button>
              </div>
            </div>
          </div>
          <div class="lp-note">Сбой (YouTube не ответил, модель недоступна) повторяется сам: через полчаса в фоне и с каждым прогоном, не больше 3 попыток в сутки.</div>
          ${!lp.enabled && html`<div class="lp-off">Поиск летсплеев выключен в настройках сервиса: границы сохранятся и применятся после включения.</div>`}
          ${lpConflict && html`<div class="field-hint is-warn lp-hint">Нижняя граница больше верхней: ни один ролик не подойдёт.</div>`}
        </section>
      </div>

      ${(!scheduler.config.enabled || running || error) && html`<div class="sched-notes">
        ${!scheduler.config.enabled && html`<div class="modal-note">Расписание на паузе: сохранение включит его снова.</div>`}
        ${running && html`<div class="modal-note is-muted">Сейчас идёт прогон: он не прервётся, новые настройки вступят в силу после него.</div>`}
        ${error && html`<div class="modal-err">${error}</div>`}
      </div>`}

      <div class="modal-actions">
        <button class=${cx("btn", canApply ? "btn-primary" : "btn-ghost")} onClick=${apply} disabled=${!canApply}>${saving ? "Сохраняем…" : "Сохранить"}</button>
        <button class="btn btn-ghost" onClick=${onClose}>Отмена</button>
      </div>
    </div>
    ${asking && html`<div class="modal-back is-top" onClick=${(e) => { e.stopPropagation(); setAsking(false); }}>
      <div class="modal unsaved" role="alertdialog" aria-modal="true" aria-label="Несохранённые изменения" onClick=${(e) => e.stopPropagation()}>
        <div class="modal-title">Сохранить изменения?</div>
        <div class="modal-hint unsaved-text">Настройки расписания, прогона или летсплеев изменены, но не сохранены. Если закрыть окно без сохранения, они вернутся к прежним.</div>
        ${lpConflict && html`<div class="modal-err">Сохранить нельзя: нижняя граница длительности роликов больше верхней.</div>`}
        <div class="modal-actions">
          <button class="btn btn-primary" disabled=${!canApply} onClick=${() => { setAsking(false); apply(); }}>${saving ? "Сохраняем…" : "Сохранить"}</button>
          <button class="btn btn-danger" onClick=${onClose}>Не сохранять</button>
          <button class="btn btn-ghost" onClick=${() => setAsking(false)}>Вернуться к настройкам</button>
        </div>
      </div>
    </div>`}
  </div>`;
}

// пауза и включение расписания из бокового меню
export async function toggleSchedule(config) {
  const paused = !config.enabled;
  try {
    const view = await api("/schedule", { method: "PUT", body: { ...config, enabled: paused } });
    toast(paused ? `Расписание включено: ${view.rule}` : "Расписание на паузе: остались только запуски вручную", paused ? "green" : "yellow");
    await refreshStatus();
  } catch (e) {
    toast("Сервис не ответил, расписание не изменено", "red");
  }
}
