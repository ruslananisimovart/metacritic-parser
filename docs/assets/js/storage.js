// Окно "Подгрузить из хранилища": сохранённые итерации сбора данных с периодом прогонов и выбор, как подгрузить.
import { useEffect, useState } from "preact/hooks";
import { api, refreshStatus } from "./api.js";
import { count, cx, html, ruDate, stampOf, toast } from "./lib.js";

/** Период итерации: "11 сентября 2026", "с 11 по 12 сентября 2026", "с 30 августа по 2 сентября 2026". */
export function periodText(item) {
  if (!item.first_day) return "";
  const last = ruDate(item.last_day || item.first_day);
  if (!item.last_day || item.first_day === item.last_day) return last;
  const [day, month, year] = ruDate(item.first_day).split(" ");
  const [, lastMonth, lastYear] = last.split(" ");
  const first = year !== lastYear ? `${day} ${month} ${year}` : month !== lastMonth ? `${day} ${month}` : day;
  return `с ${first} по ${last}`;
}

function metaText(item) {
  return [
    `${count(item.runs, "прогон", "прогона", "прогонов")} за ${count(item.days, "день", "дня", "дней")}`,
    count(item.games, "игра", "игры", "игр"),
    `резюме ${item.summaries}`,
    `летсплеев ${item.letsplays}`,
  ].join(" · ");
}

const MODES = [["merge", "Добавить к текущему"], ["replace", "Заменить текущий"]];
const MODE_HINT = {
  merge: "Игры, которые уже есть в каталоге, останутся как есть; из итерации добавятся остальные вместе с историей прогонов.",
  replace: "Текущий каталог сначала сохранится в хранилище новой итерацией, затем в интерфейсе останется только выбранная.",
};

export function StorageModal({ storage, catalogGames, onClose }) {
  const items = storage.snapshots || [];
  // по умолчанию выбрана последняя итерация, которой ещё нет в каталоге
  const [chosen, setChosen] = useState((items.find((i) => !i.loaded) || items[0] || {}).id);
  const [mode, setMode] = useState("merge");
  const [busy, setBusy] = useState(false);
  const empty = !catalogGames;
  const item = items.find((i) => i.id === chosen);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);

  async function apply() {
    if (!item || busy) return;
    setBusy(true);
    try {
      const result = await api("/storage/restore", { method: "POST", body: { snapshot: item.id, mode: empty ? "merge" : mode } });
      const saved = result.saved ? ", текущий каталог сохранён новой итерацией" : "";
      toast(`Подгружена итерация ${periodText(item) || item.id}: ${count(result.restored, "игра", "игры", "игр")}${saved}`, "green");
      await refreshStatus();
      onClose();
    } catch (e) {
      toast(e.status === 409 ? "Идёт прогон: подгрузить можно после него"
        : e.status === 404 ? "Этой итерации уже нет в хранилище" : "Сервис не ответил, данные не подгружены", "red");
    } finally {
      setBusy(false);
    }
  }

  return html`<div class="modal-back" onClick=${onClose}>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Подгрузить из хранилища" onClick=${(e) => e.stopPropagation()}>
      <div class="modal-title">Подгрузить из хранилища</div>
      <div class="modal-sub">Итерация это всё, что сервис собрал между двумя очистками: прогоны за период, игры, резюме и летсплеи. Файлы итераций остаются на диске.</div>
      <div class="field-label">итерации, новые сверху</div>
      <div class="iter-list" role="radiogroup" aria-label="Итерации">
        ${items.map((i) => html`<button type="button" key=${i.id} role="radio" aria-checked=${i.id === chosen}
            class=${cx("iter", i.id === chosen && "is-on")} onClick=${() => setChosen(i.id)}>
          <span class="iter-top">
            <span class="iter-period">${periodText(i) || "период неизвестен"}</span>
            ${i.loaded && html`<span class="iter-badge">в каталоге</span>`}
          </span>
          <span class="iter-meta">${metaText(i)}</span>
          <span class="iter-saved">сохранена ${stampOf(i.archived_at)}</span>
        </button>`)}
      </div>
      ${empty
        ? html`<div class="field-hint">Каталог сейчас пуст: итерация загрузится как есть.</div>`
        : html`<div class="field-label">как подгрузить</div>
          <div class="mode-grid">
            ${MODES.map(([id, label]) => html`<button key=${id} class=${cx("mode", mode === id && "is-on")}
              onClick=${() => setMode(id)} aria-pressed=${mode === id}>${label}</button>`)}
          </div>
          <div class="field-hint">${MODE_HINT[mode]}</div>`}
      ${item && item.loaded && !empty && mode === "merge"
        && html`<div class="modal-note is-muted">Эта итерация уже в каталоге: повторная подгрузка ничего не добавит.</div>`}
      <div class="modal-actions">
        <button class="btn" onClick=${apply} disabled=${!item || busy}>${busy ? "Подгружаем…" : "Подгрузить"}</button>
        ${item && html`<a class="btn btn-ghost" href=${`/api/storage/${item.id}/export.zip`}
          title="Скачать CSV этой итерации, не подгружая её в интерфейс">Выгрузить CSV</a>`}
        <button class="btn btn-ghost" onClick=${onClose}>Отмена</button>
      </div>
    </div>
  </div>`;
}
