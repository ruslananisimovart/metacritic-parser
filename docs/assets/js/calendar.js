// Календарь прогонов по макету: слева месяц (день с полоской объёма, точки старых игр и ошибок, легенда,
// итог месяца), справа карточка дня (факт, сегодня или прогноз: четыре показателя, заметки, прогоны дня).
// Наведение показывает день, клик закрепляет. Факт из базы, прогноз по расписанию и темпу за две недели.
import { useEffect, useState } from "preact/hooks";
import { api } from "./api.js";
import { cx, html, plural } from "./lib.js";

const MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const MONTHS_OF = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"];
const GAMES = ["игра", "игры", "игр"];

const pad = (n) => String(n).padStart(2, "0");
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

const CAL_ICON = html`<svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8 5.75C7.59 5.75 7.25 5.41 7.25 5V2C7.25 1.59 7.59 1.25 8 1.25C8.41 1.25 8.75 1.59 8.75 2V5C8.75 5.41 8.41 5.75 8 5.75ZM16 5.75C15.59 5.75 15.25 5.41 15.25 5V2C15.25 1.59 15.59 1.25 16 1.25C16.41 1.25 16.75 1.59 16.75 2V5C16.75 5.41 16.41 5.75 16 5.75ZM20.5 9.84H3.5C3.09 9.84 2.75 9.5 2.75 9.09C2.75 8.68 3.09 8.34 3.5 8.34H20.5C20.91 8.34 21.25 8.68 21.25 9.09C21.25 9.5 20.91 9.84 20.5 9.84ZM16 22.75H8C4.35 22.75 2.25 20.65 2.25 17V8.5C2.25 4.85 4.35 2.75 8 2.75H16C19.65 2.75 21.75 4.85 21.75 8.5V17C21.75 20.65 19.65 22.75 16 22.75ZM8 4.25C5.14 4.25 3.75 5.64 3.75 8.5V17C3.75 19.86 5.14 21.25 8 21.25H16C18.86 21.25 20.25 19.86 20.25 17V8.5C20.25 5.64 18.86 4.25 16 4.25H8Z" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/><circle cx="8.5" cy="13.5" r="1" fill="currentColor"/><circle cx="12" cy="13.5" r="1" fill="currentColor"/><circle cx="15.5" cy="13.5" r="1" fill="currentColor"/><circle cx="8.5" cy="17" r="1" fill="currentColor"/><circle cx="12" cy="17" r="1" fill="currentColor"/><circle cx="15.5" cy="17" r="1" fill="currentColor"/></svg>`;

// объём дня: факт (игр обработано) в прошлом, факт плюс прогноз сегодня, прогноз в будущем
function volume(day, today, pace) {
  const fact = day.created + day.updated + day.rechecked;
  if (day.date < today) return fact;
  const plan = Math.round(day.planned * pace.games);
  return day.date === today ? fact + plan : plan;
}

function DayCard({ day, today, pace, rule, staleDays }) {
  const past = day.date < today, isToday = day.date === today;
  const d = new Date(day.date + "T00:00:00");
  const kind = past ? { label: "Факт", cls: "is-fact" } : isToday ? { label: "Сегодня", cls: "is-today" } : { label: "Прогноз", cls: "is-plan" };
  const fact = day.created + day.updated + day.rechecked;
  // прогноз со знаком "~"; если на день нет плановых запусков, прогнозировать нечего
  const approx = (n) => (past ? String(n) : !isToday && !day.planned ? "—" : "~" + n);
  const planGames = Math.round(day.planned * pace.games);
  const stats = [
    { label: "прогонов", value: isToday ? `${day.runs} / ${day.runs + day.planned}` : String(past ? day.runs : day.planned),
      sub: past ? (day.runs ? "по факту" : "сервис не запускался") : isToday ? `осталось ${day.planned}` : rule || "расписание выключено" },
    { label: past || isToday ? "игр обработано" : "ожидаем игр", value: past ? fact : isToday ? `${fact}${day.planned ? " +~" + planGames : ""}` : approx(planGames),
      sub: past || isToday ? `${day.created} новых · ${day.updated + day.rechecked} обновлено` : day.planned ? "по темпу за 2 недели" : "нет плановых запусков", tone: "acc" },
    { label: "резюме отзывов", value: past ? day.summaries : isToday ? day.summaries : approx(Math.round(day.planned * pace.summaries)), sub: "критики и игроки" },
    { label: "летсплеев", value: past || isToday ? day.letsplays : approx(Math.round(day.planned * pace.letsplays)),
      sub: past && day.letsplays_missing ? `у ${day.letsplays_missing} ${plural(day.letsplays_missing, "игры", "игр", "игр")} нет` : "найдено и разобрано", tone: "violet" },
  ];
  const notes = [];
  if (!past && day.stale_due) notes.push({ tone: "violet", text: `Обновление старых игр: ${day.stale_due} ${plural(day.stale_due, ...GAMES)} ${day.stale_due === 1 ? "созревает" : "созревают"} по сроку${staleDays ? ` (раз в ${staleDays} ${plural(staleDays, "день", "дня", "дней")})` : ""}, идут по квоте за прогон` });
  if (past && day.rechecked) notes.push({ tone: "violet", text: `Обновлено старых игр по сроку: ${day.rechecked}` });
  if (!past && day.letsplay_due) notes.push({ tone: "green", text: `Повторный поиск летсплея: ${day.letsplay_due} ${plural(day.letsplay_due, ...GAMES)}` });
  if (!past && day.media_due) notes.push({ tone: "muted", text: `Повторный запрос к Steam и Google Play: ${day.media_due} ${plural(day.media_due, ...GAMES)}` });
  if ((past || isToday) && day.errors) notes.push({ tone: "red", text: `${day.errors} ${plural(day.errors, "ошибка", "ошибки", "ошибок")} за день${day.failed ? `, не загрузилось ${day.failed} ${plural(day.failed, ...GAMES)}` : ""}` });
  if (past && day.runs && !day.errors) notes.push({ tone: "green", text: "День прошёл без ошибок" });
  if (!past && !isToday) notes.push({ tone: "muted", text: day.planned
    ? "Прогноз по текущему расписанию и среднему темпу, фактические числа будут отличаться"
    : "Расписание выключено или на паузе: плановых прогонов нет, запуск только вручную" });

  // прогоны дня: сделанные с числом игр, запланированные с ожиданием
  const maxGames = Math.max(1, pace.games, ...day.run_list.map((r) => r.games));
  const runs = [
    ...day.run_list.map((r) => ({ time: r.time, done: true, games: r.games, failed: r.status !== "done" })),
    ...(past ? [] : day.planned_times.map((t) => ({ time: t, done: false, games: Math.round(pace.games) }))),
  ];
  const shown = runs.slice(0, 6);
  const tail = past ? (day.runs ? `Всего ${day.runs} ${plural(day.runs, "прогон", "прогона", "прогонов")}` : "")
    : isToday ? `Прошло ${day.runs} из ${day.runs + day.planned}${day.planned_times[0] ? `, следующий в ${day.planned_times[0]}` : ""}`
    : day.planned ? `По расписанию ${day.planned} ${plural(day.planned, "прогон", "прогона", "прогонов")}` : "";

  return html`<section class="cal-card cal-day">
    <div class="cal-day-head">
      <div>
        <div class="cal-cap">${WEEKDAYS_FULL[(d.getDay() + 6) % 7]}</div>
        <div class="cal-day-title">${d.getDate()} ${MONTHS_OF[d.getMonth()]} ${d.getFullYear()}</div>
      </div>
      <span class=${cx("cal-kind", kind.cls)}>${kind.label}</span>
    </div>
    <div class="cal-stats">
      ${stats.map((s) => html`<div class="cal-stat" key=${s.label}>
        <div class="cal-cap">${s.label}</div>
        <div class=${cx("cal-stat-v", s.tone && "is-" + s.tone)}>${s.value}</div>
        <div class="cal-stat-sub">${s.sub}</div>
      </div>`)}
    </div>
    ${notes.length > 0 && html`<div class="cal-notes">
      ${notes.map((n, i) => html`<div class=${"cal-note is-" + n.tone} key=${i}><i></i><span>${n.text}</span></div>`)}
    </div>`}
    ${runs.length > 0 && html`<div class="cal-runs">
      <div class="cal-cap">прогоны за день</div>
      <div class="cal-run-list">
        ${shown.map((r, i) => html`<div class="cal-run" key=${i}>
          <span class=${cx("cal-run-time", r.done && "is-done")}>${r.time}</span>
          <span class="cal-run-bar"><span class=${cx(r.done ? (r.failed ? "is-red" : "is-green") : isToday ? "is-acc" : "is-plan")} style=${{ width: Math.min(100, Math.round((r.games / maxGames) * 100)) + "%" }}></span></span>
          <span class="cal-run-text">${r.done ? "" : "~"}${r.games} ${plural(r.games, ...GAMES)}</span>
        </div>`)}
      </div>
      <div class="cal-runs-tail">${tail}${runs.length > shown.length ? ` · показаны первые ${shown.length}` : ""}</div>
    </div>`}
  </section>`;
}

export function CalendarModal({ onClose }) {
  const now = new Date();
  const [cursor, setCursor] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 });
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [hover, setHover] = useState(null);
  const [pinned, setPinned] = useState(null);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => {
    let alive = true;
    api(`/calendar?year=${cursor.year}&month=${cursor.month}`)
      .then((view) => { if (alive) { setData(view); setError(""); setPinned((p) => p || view.today); } })
      .catch(() => alive && setError("Сервис не ответил, календарь не загрузился"));
    return () => { alive = false; };
  }, [cursor.year, cursor.month]);

  const move = (delta) => setCursor((c) => {
    const m = c.month + delta;
    return m < 1 ? { year: c.year - 1, month: 12 } : m > 12 ? { year: c.year + 1, month: 1 } : { year: c.year, month: m };
  });
  const today = data ? data.today : isoOf(now);
  const byDate = data ? Object.fromEntries(data.days.map((d) => [d.date, d])) : {};
  const shownIso = hover || pinned || today;
  const shown = byDate[shownIso];
  const pace = data ? data.pace : { games: 0, summaries: 0, letsplays: 0 };

  // сетка 6 недель с понедельника; дни соседних месяцев приглушены и без данных
  const first = new Date(cursor.year, cursor.month - 1, 1);
  const shift = (first.getDay() + 6) % 7;
  const cells = Array.from({ length: 42 }, (_, i) => {
    const dt = new Date(cursor.year, cursor.month - 1, 1 - shift + i);
    const iso = isoOf(dt);
    return { iso, num: dt.getDate(), inMonth: dt.getMonth() === cursor.month - 1, day: byDate[iso] };
  });
  const maxVolume = Math.max(1, ...cells.filter((c) => c.day).map((c) => volume(c.day, today, pace)));
  const monthDays = data ? data.days : [];
  const monthGames = monthDays.reduce((s, d) => s + (d.date <= today ? d.created : 0), 0);
  const monthRuns = monthDays.reduce((s, d) => s + d.runs, 0);
  const monthPlanned = monthDays.reduce((s, d) => s + d.planned, 0);
  const monthLabel = `${MONTHS[cursor.month - 1]} ${cursor.year}`;
  const onToday = cursor.year === now.getFullYear() && cursor.month === now.getMonth() + 1 && pinned === today;

  return html`<div class="modal-back" onClick=${onClose}>
    <div class="modal modal-wide cal-modal" role="dialog" aria-modal="true" aria-label="Календарь прогонов" onClick=${(e) => e.stopPropagation()}>
      <div class="modal-head modal-head-wide">
        <span class="sec-icon" style=${{ background: "rgba(43,123,255,.14)", color: "var(--acc-2)" }}>${CAL_ICON}</span>
        <div class="modal-head-text">
          <div class="modal-title">Календарь прогонов</div>
          <div class="modal-sub">Наведите на день, чтобы увидеть сводку: до сегодня факт, после прогноз по текущему расписанию. Клик закрепляет день.</div>
        </div>
        <button type="button" class="modal-close" onClick=${onClose} aria-label="Закрыть">✕</button>
      </div>
      ${error ? html`<div class="modal-err">${error}</div>` : html`<div class="cal-layout">
        <section class="cal-card cal-month-card">
          <div class="cal-nav">
            <button type="button" class="cal-arrow" onClick=${() => move(-1)} aria-label="Предыдущий месяц">
              <svg width="8" height="13" viewBox="0 0 8 13" fill="none"><path d="M6.4 1.4 1.6 6.5l4.8 5.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <div class="cal-month">${monthLabel}</div>
            <button type="button" class="cal-arrow" onClick=${() => move(1)} aria-label="Следующий месяц">
              <svg width="8" height="13" viewBox="0 0 8 13" fill="none"><path d="m1.6 1.4 4.8 5.1-4.8 5.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <button type="button" class=${cx("cal-today-btn", onToday && "is-on")}
              onClick=${() => { setCursor({ year: now.getFullYear(), month: now.getMonth() + 1 }); setPinned(today); setHover(null); }}>Сегодня</button>
          </div>
          <div class="cal-dows">${WEEKDAYS.map((w, i) => html`<div class=${cx("cal-dow", i >= 5 && "is-weekend")} key=${w}>${w}</div>`)}</div>
          <div class="cal-grid" onMouseLeave=${() => setHover(null)}>
            ${cells.map((c) => {
              const d = c.day;
              const vol = d ? volume(d, today, pace) : 0;
              const tone = !d ? "" : d.date < today ? "is-green" : d.date === today ? "is-acc" : "is-plan";
              return html`<button type="button" key=${c.iso} disabled=${!c.inMonth}
                class=${cx("cal-cell", !c.inMonth && "is-out", c.iso === today && "is-today", c.iso === pinned && "is-pinned", c.iso === shownIso && "is-shown")}
                onMouseEnter=${() => c.inMonth && setHover(c.iso)} onClick=${() => c.inMonth && setPinned(c.iso)}>
                <span class="cal-num">${c.num}</span>
                ${c.inMonth && html`<span class="cal-bar"><span class=${tone} style=${{ width: Math.round((vol / maxVolume) * 100) + "%" }}></span></span>`}
                ${d && html`<span class="cal-dots">
                  ${d.date >= today && d.stale_due > 0 && html`<i class="is-violet"></i>`}
                  ${d.date < today && d.rechecked > 0 && html`<i class="is-violet"></i>`}
                  ${d.errors > 0 && html`<i class="is-red"></i>`}
                </span>`}
              </button>`;
            })}
          </div>
          <div class="cal-legend">
            <span><i class="cal-leg-bar is-green"></i>факт</span>
            <span><i class="cal-leg-bar is-plan"></i>план</span>
            <span><i class="cal-leg-dot is-violet"></i>обновление старых игр</span>
            <span><i class="cal-leg-dot is-red"></i>были ошибки</span>
          </div>
          <div class="cal-month-sum">
            ${MONTHS[cursor.month - 1]}: ${monthGames} ${plural(monthGames, "новая игра", "новые игры", "новых игр")} в каталоге, ${monthRuns} ${plural(monthRuns, "прогон", "прогона", "прогонов")}${monthPlanned ? ` и ещё ${monthPlanned} по плану` : ""}
          </div>
        </section>
        ${shown ? html`<${DayCard} day=${shown} today=${today} pace=${pace} rule=${data.schedule_rule} staleDays=${data.stale_days} />`
          : html`<section class="cal-card cal-day"><div class="empty-note">Загружаем…</div></section>`}
      </div>`}
    </div>
  </div>`;
}
