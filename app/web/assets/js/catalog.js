// Экран "Каталог игр": поиск, сортировка, фильтр по платформам, сетка карточек, подгрузка по 24.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, link, navigate, refreshStatus, rememberCatalog, requestRun } from "./api.js";
import {
  CompleteStar, STORES, ScoreBadge, count, cx, fmt, html, platMark, platStyle, plural, ruDate, scoreText, stampOf, store, tip, toast,
} from "./lib.js";
import { StorageModal, periodText } from "./storage.js";

const PAGE = 24;
const DENSITY_KEY = "skytec-parser-density";
// по умолчанию новые игры сверху: каталог пополняется свежими релизами, они интереснее всего
const DEFAULT_SORT = "release_date";
const SORTS = [
  { key: "release_date", label: "По дате выхода" },
  { key: "metascore", label: "По оценке критиков" },
  { key: "userscore", label: "По оценке игроков" },
  { key: "title", label: "По названию" },
];
// у игр без значения поля сортировки своя секция внизу: API уже ставит их в конец
const UNSCORED = {
  metascore: { test: (g) => g.metascore == null, text: "Свежие игры из SEE ALL: Metacritic ещё не посчитал оценки" },
  userscore: { test: (g) => g.userscore == null, text: "Игроки ещё не выставили оценку" },
  release_date: { test: (g) => !g.release_date, text: "Дата выхода неизвестна" },
  title: { test: () => false, text: "" },
};

function readParams(params) {
  return {
    q: params.q || "",
    platform: params.platform || "",
    sort: SORTS.some((s) => s.key === params.sort) ? params.sort : DEFAULT_SORT,
    order: params.order === "asc" ? "asc" : "desc",
    // фильтры готовности: что должно быть у игры, отметки складываются через "и"
    has: (params.has || "").split(",").filter((k) => FILTERS.some((f) => f.key === k)),
  };
}

// параметры по умолчанию в адрес не пишем: #/catalog чище, чем #/catalog?sort=release_date&order=desc
function catalogLink(params) {
  return link.catalog({
    q: params.q.trim() ? params.q : "",
    platform: params.platform,
    sort: params.sort === DEFAULT_SORT ? "" : params.sort,
    order: params.order === "desc" ? "" : params.order,
    has: params.has.join(","),
  });
}

function Cover({ game }) {
  const [broken, setBroken] = useState(false);
  const show = game.cover_url && !broken;
  // шапка Steam или иконка Google Play вписывается в постер целиком, пустые поля закрывает её же размытая копия
  const wide = show && game.cover_kind !== "portrait";
  const store = show && STORES[game.cover_source];
  return html`<div class=${cx("gcover", !show && "is-empty", wide && "is-wide")}>
    ${wide && html`<img class="gcover-bg" src=${game.cover_url} alt="" aria-hidden="true" loading="lazy" decoding="async" />`}
    ${show
      ? html`<img src=${game.cover_url} alt="" loading="lazy" decoding="async" width="176" height="260" onError=${() => setBroken(true)} />`
      : html`<span class="gcover-ph">${game.title}</span>`}
    ${store && html`<span class="gcover-src" ...${tip(`Обложки на Metacritic нет, картинка со страницы игры в ${store}`)}>${store}</span>`}
    ${(game.letsplay_status === "done" || game.complete) && html`<div class="gcover-tags">
      ${game.letsplay_status === "done" && html`<span class="gcover-lp" ...${tip("Летсплей найден, заключение в карточке игры")}>летсплей</span>`}
      ${game.complete && html`<${CompleteStar} />`}
    </div>`}
  </div>`;
}

// подпись под оценкой: "64 рецензии", "1208 оценок"; без оценки причина
function reviewsText(value, n, kind) {
  if (value == null) return kind === "critic" ? "нет рецензий" : "нет оценок";
  if (n == null) return kind === "critic" ? "рецензии Metacritic" : "оценки игроков";
  return kind === "critic" ? `${fmt(n)} ${plural(n, "рецензия", "рецензии", "рецензий")}` : `${fmt(n)} ${plural(n, "оценка", "оценки", "оценок")}`;
}

// карточка по макету 15.09 (Skytec Parser UI v1): обложка со значком летсплея, название, разработчик,
// платформы, оценки столбиком с числом рецензий, внизу дата выхода и жанр
function GameCard({ game, plain }) {
  const platforms = game.platforms || [];
  const main = game.main_platform || platforms[0] || "";
  const others = Math.max(0, platforms.length - 1);
  const genre = (game.genres || [])[0];
  const developer = (game.developers || [])[0];
  return html`<a class=${cx("gcard", plain && "is-plain")} href=${link.game(game.slug)}>
    <${Cover} game=${game} />
    <div class="gbody">
      <div class="gtitle">${game.title}</div>
      <div class="gdev">${developer || "разработчик неизвестен"}</div>
      <div class="gplats">
        ${main && html`<span class="pmark" style=${platStyle(main)} ...${tip("Платформа: " + main)}>${platMark(main)}</span>`}
        ${others > 0 && html`<span class="pmark pmore" ...${tip(`Игра вышла ещё на ${others} ${plural(others, "платформе", "платформах", "платформах")}: ${platforms.slice(1).join(", ")}`)}>+${others}</span>`}
      </div>
      <div class="gscore-src">оценки metacritic</div>
      ${plain
        ? html`<div class="gscore">
            <span class="badge badge-md is-none">—</span>
            <div class="gscore-text"><div class="gscore-cap">Оценок пока нет</div><div class="gscore-sub">ни рецензий, ни отзывов игроков</div></div>
          </div>`
        : html`<div class="gscore">
            <${ScoreBadge} value=${game.metascore} empty="нет" hint=${game.metascore == null ? "Metacritic ещё не посчитал оценку критиков" : `Оценка критиков Metacritic: ${game.metascore} из 100`} />
            <div class="gscore-text"><div class="gscore-cap">Критики</div><div class="gscore-sub">${reviewsText(game.metascore, game.critic_reviews, "critic")}</div></div>
          </div>
          <div class="gscore">
            <${ScoreBadge} value=${game.userscore} isUser=${true} empty="нет" hint=${game.userscore == null ? "Игроки ещё не выставили оценку" : `Оценка игроков: ${scoreText(game.userscore, true)} из 10`} />
            <div class="gscore-text"><div class="gscore-cap">Игроки</div><div class="gscore-sub">${reviewsText(game.userscore, game.user_reviews, "user")}</div></div>
          </div>`}
      <div class="gfoot">
        <div class="gfact"><div class="gfact-label">дата выхода</div><div class="gfact-value">${game.release_date ? ruDate(game.release_date) : "неизвестна"}</div></div>
        <div class="gfact"><div class="gfact-label">жанр</div><div class="gfact-value">${genre || "неизвестен"}</div></div>
      </div>
    </div>
  </a>`;
}

// каталог опустошили кнопкой в мониторинге: собранное лежит итерациями в хранилище на диске
function Archived({ storage }) {
  const [picker, setPicker] = useState(false);
  const latest = (storage.snapshots || [])[0];
  const period = latest ? periodText(latest) : "";
  return html`<div class="empty-box is-archived">
    <span class="archive-icon">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3.2 7.4h17.6v12.2a1.6 1.6 0 0 1-1.6 1.6H4.8a1.6 1.6 0 0 1-1.6-1.6V7.4Z" stroke="var(--t8)" stroke-width="1.7" stroke-linejoin="round"/><path d="M2.2 3.4h19.6v4H2.2v-4ZM9.6 12.2h4.8" stroke="var(--t8)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </span>
    <div class="empty-title">Каталог очищен, данные в хранилище</div>
    <div class="empty-text">В хранилище ${count(storage.count, "итерация", "итерации", "итераций")}${latest
      ? `, последняя${period ? " собрана " + period + " и" : ""} сохранена ${stampOf(latest.archived_at)}` : ""}.
      Интерфейс работает как после чистого прогона, файлы остались на диске.</div>
    <div class="empty-actions">
      <button class="btn" onClick=${() => setPicker(true)}>
        <svg width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 3.6v9M6.4 9.2 10 12.8l3.6-3.6M4 15.4h12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span>Подгрузить из хранилища</span>
      </button>
      <button class="btn btn-ghost" onClick=${requestRun}>Запустить чистый прогон</button>
    </div>
    <div class="archive-path">${storage.path}</div>
    ${picker && html`<${StorageModal} storage=${storage} catalogGames=${0} onClose=${() => setPicker(false)} />`}
  </div>`;
}

function Skeletons({ count = 6 }) {
  return Array.from({ length: Math.max(1, Math.min(6, count)) }, (_, n) => html`<div class="gskel" key=${n}>
    <div class="shimmer gskel-cover"></div>
    <div class="gskel-body">
      <div class="shimmer" style=${{ height: "14px", width: "80%" }}></div>
      <div class="skel" style=${{ height: "11px", width: "48%" }}></div>
      <div class="gskel-row"><div class="skel gskel-badge"></div><div class="skel gskel-badge"></div></div>
      <div class="skel" style=${{ height: "11px", width: "66%", marginTop: "auto" }}></div>
    </div>
  </div>`);
}

// фильтры готовности материалов: первый пункт "Полный анализ" включает всё сразу
const FILTERS = [
  { key: "complete", label: "Полный анализ", sub: "прошла все этапы: обложка, описание, видео, резюме, летсплей" },
  { key: "letsplay", label: "Есть летсплей", sub: "найден и расшифрован ролик блогера" },
  { key: "critic_summary", label: "Резюме критиков", sub: "что хвалят и ругают критики" },
  { key: "user_summary", label: "Резюме игроков", sub: "что хвалят и ругают игроки" },
  { key: "scores", label: "Есть оценки", sub: "Metascore или оценка игроков" },
  { key: "video", label: "Есть видео", sub: "с Metacritic или трейлер из Steam" },
  { key: "cover", label: "Есть обложка", sub: "с Metacritic или из магазина" },
  { key: "description", label: "Есть описание", sub: "с Metacritic или из магазина" },
];
const CHECK = html`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

function FilterMenu({ params, facets, onChange }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const picked = params.has;
  const toggle = (key) => onChange(picked.includes(key) ? picked.filter((k) => k !== key) : [...picked, key]);
  const caption = !picked.length ? "Все игры"
    : picked.length === 1 ? FILTERS.find((f) => f.key === picked[0]).label
    : `Выбрано: ${picked.length}`;
  const option = (f, lead) => html`<button type="button" key=${f.key} role="menuitemcheckbox" aria-checked=${picked.includes(f.key)}
    class=${cx("filter-opt", lead && "is-lead", picked.includes(f.key) && "is-on")} onClick=${() => toggle(f.key)}>
    <span class="check-box">${CHECK}</span>
    <span class="filter-name">${f.label}<small>${f.sub}</small></span>
    <span class="chip-count">${facets && facets[f.key] != null ? fmt(facets[f.key]) : "…"}</span>
  </button>`;
  return html`<div class="sort" ref=${box}>
    <button class=${cx("sort-btn filter-btn", picked.length && "is-on")} onClick=${() => setOpen(!open)} aria-haspopup="menu" aria-expanded=${open}>
      <span class="sort-cap"><small>Фильтры</small><b>${caption}</b></span>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 16.8C11.3 16.8 10.6 16.53 10.07 16L3.55 9.48C3.26 9.19 3.26 8.71 3.55 8.42C3.84 8.13 4.32 8.13 4.61 8.42L11.13 14.94C11.61 15.42 12.39 15.42 12.87 14.94L19.39 8.42C19.68 8.13 20.16 8.13 20.45 8.42C20.74 8.71 20.74 9.19 20.45 9.48L13.93 16C13.4 16.53 12.7 16.8 12 16.8Z" fill="var(--t9)" stroke="var(--t9)" stroke-width="0.5" stroke-linejoin="round"/></svg>
    </button>
    ${open && html`<div class="sort-menu filter-menu" role="menu">
      ${option(FILTERS[0], true)}
      <div class="filter-sep"></div>
      ${FILTERS.slice(1).map((f) => option(f, false))}
      <div class="filter-foot">
        <span class="muted">${picked.length > 1 ? "Показаны игры, у которых есть всё отмеченное" : "Отметьте, что должно быть у игры"}</span>
        <button type="button" class="btn-link" disabled=${!picked.length} onClick=${() => onChange([])}>Сбросить</button>
      </div>
    </div>`}
  </div>`;
}

function SortMenu({ params, onPick }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    // меню закрывается кликом мимо и по Esc
    const onDown = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const current = SORTS.find((s) => s.key === params.sort);
  return html`<div class="sort" ref=${box}>
    <button class="sort-btn" onClick=${() => setOpen(!open)} aria-haspopup="listbox" aria-expanded=${open}>
      <span class="sort-cap"><small>Сортировка</small><b>${current.label}</b></span>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 16.8C11.3 16.8 10.6 16.53 10.07 16L3.55 9.48C3.26 9.19 3.26 8.71 3.55 8.42C3.84 8.13 4.32 8.13 4.61 8.42L11.13 14.94C11.61 15.42 12.39 15.42 12.87 14.94L19.39 8.42C19.68 8.13 20.16 8.13 20.45 8.42C20.74 8.71 20.74 9.19 20.45 9.48L13.93 16C13.4 16.53 12.7 16.8 12 16.8Z" fill="var(--t9)" stroke="var(--t9)" stroke-width="0.5" stroke-linejoin="round"/></svg>
    </button>
    ${open && html`<div class="sort-menu" role="listbox">
      ${SORTS.map((s) => html`<button role="option" aria-selected=${s.key === params.sort} class=${cx(s.key === params.sort && "is-on")}
        onClick=${() => { setOpen(false); onPick(s.key); }}>${s.label}</button>`)}
    </div>`}
  </div>`;
}

export function Catalog({ route, status }) {
  const params = readParams(route.params);
  const key = JSON.stringify(params);
  const [query, setQuery] = useState(params.q);
  const [dense, setDense] = useState(store.get(DENSITY_KEY) === "dense");
  const [platforms, setPlatforms] = useState(null);
  const [view, setView] = useState({ phase: "loading", items: [], total: 0, key: null });
  const [loadingMore, setLoadingMore] = useState(false);
  const [retry, setRetry] = useState(0);
  const request = useRef(0);
  const items = useRef([]);
  const data = status.data;
  const catalogTotal = data ? data.games : null;
  const filtered = !!(params.q.trim() || params.platform || params.has.length);

  const go = (patch) => navigate(catalogLink({ ...params, ...patch }), { replace: true });

  // поле ввода реагирует сразу, запрос уходит после паузы 300 мс
  useEffect(() => { setQuery(params.q); }, [params.q]);
  useEffect(() => {
    if (query === params.q) return undefined;
    const timer = setTimeout(() => go({ q: query }), 300);
    return () => clearTimeout(timer);
  }, [query]);

  function fetchPage(offset, limit) {
    const qs = new URLSearchParams({ sort: params.sort, order: params.order, limit: String(limit), offset: String(offset) });
    if (params.q.trim()) qs.set("q", params.q.trim());
    if (params.platform) qs.set("platform", params.platform);
    if (params.has.length) qs.set("has", params.has.join(","));
    return api("/games?" + qs.toString());
  }

  // первая страница при смене параметров; при фоновом обновлении держим столько же карточек, сколько видно
  function load(quiet) {
    const id = ++request.current;
    const limit = quiet ? Math.max(PAGE, Math.min(100, items.current.length)) : PAGE;
    if (!quiet) setView((v) => ({ ...v, phase: v.items.length ? "refreshing" : "loading" }));
    fetchPage(0, limit)
      .then((page) => {
        if (id !== request.current) return;
        items.current = page.items;
        setView({ phase: "ready", items: page.items, total: page.total, facets: page.facets || {}, key });
      })
      .catch(() => {
        if (id !== request.current) return;
        if (quiet) return;  // фоновое обновление молча пропускаем, покажем прежнее
        items.current = [];
        setView({ phase: "error", items: [], total: 0, key });
      });
  }
  useEffect(() => { load(false); }, [key, retry]);
  // поиск и фильтры запоминаются: меню и хлебные крошки карточки возвращают в тот же каталог
  // адрес без параметров при открытии каталога (сайт открыли заново) возвращает запомненные фильтры.
  // Только при открытии: внутри каталога возврат к умолчаниям (сортировка снова "по убыванию",
  // снятая последняя отметка) это выбор пользователя, его нельзя подменять запомненным
  const opened = useRef(false);
  useEffect(() => {
    const current = catalogLink(params);
    const first = !opened.current;
    opened.current = true;
    const saved = link.catalog();
    if (first && current === "#/catalog" && saved !== current) { navigate(saved, { replace: true }); return; }
    rememberCatalog(current);
  }, [key]);

  // новые игры приходят во время прогона: обновляем список, не сбрасывая прокрутку
  const lastTotal = useRef(catalogTotal);
  useEffect(() => {
    if (catalogTotal == null) return;
    if (lastTotal.current != null && lastTotal.current !== catalogTotal && view.phase === "ready") load(true);
    lastTotal.current = catalogTotal;
    api("/platforms").then(setPlatforms).catch(() => {});
  }, [catalogTotal]);

  // следующая страница подгружается сама, когда до конца списка остаётся около двух экранов;
  // busy в ref, чтобы наблюдатель не запускал второй запрос, пока идёт первый
  const busy = useRef(false);
  const total = useRef(0);
  total.current = view.total;
  const [moreFailed, setMoreFailed] = useState(false);
  function loadMore() {
    if (busy.current || items.current.length >= total.current) return;
    const id = request.current;
    busy.current = true;
    setLoadingMore(true);
    setMoreFailed(false);
    fetchPage(items.current.length, PAGE)
      .then((page) => {
        if (id !== request.current) return;
        items.current = items.current.concat(page.items);
        setView((v) => ({ ...v, items: items.current, total: page.total }));
      })
      .catch(() => { if (id === request.current) setMoreFailed(true); })
      .finally(() => { busy.current = false; setLoadingMore(false); });
  }
  const sentinel = useRef(null);
  useEffect(() => {
    const node = sentinel.current;
    if (!node || typeof IntersectionObserver === "undefined") return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore();
    }, { rootMargin: "0px 0px 1200px 0px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [view.phase, view.key]);
  // страница могла не заполнить экран (или сторож остался в зоне после подгрузки): наблюдатель
  // повторно не сработает, поэтому положение сторожа проверяется и после каждой загрузки
  useEffect(() => {
    const node = sentinel.current;
    if (!node || view.phase !== "ready" || moreFailed || loadingMore) return;
    if (node.getBoundingClientRect().top < window.innerHeight + 1200) loadMore();
  }, [view.items.length, view.phase, loadingMore]);

  const setDensity = (next) => { setDense(next); store.set(DENSITY_KEY, next ? "dense" : "comfy"); };
  const reset = () => { setQuery(""); rememberCatalog("#/catalog"); navigate(link.catalog({}), { replace: true }); };

  const storage = data && data.storage;
  const rule = UNSCORED[params.sort];
  const scored = view.items.filter((g) => !rule.test(g));
  const unscored = view.items.filter(rule.test);
  const shown = view.items.length;
  const run = data && data.run;
  const sortLabel = SORTS.find((s) => s.key === params.sort).label;

  let body;
  if (view.phase === "loading") {
    body = html`<div class=${cx("grid", dense && "is-dense")}><${Skeletons} /></div>`;
  } else if (view.phase === "error") {
    body = html`<div class="error-box">
      <div class="empty-title">Не удалось загрузить каталог</div>
      <div class="empty-text">Сервис не ответил на запрос. Проверьте, что он запущен, и попробуйте ещё раз.</div>
      <button class="btn" onClick=${() => setRetry((n) => n + 1)}>Повторить</button>
    </div>`;
  } else if (view.total === 0 && filtered) {
    body = html`<div class="empty-box">
      <div class="empty-title">Ничего не найдено</div>
      <div class="empty-text">По запросу и фильтрам нет игр. Попробуйте другое название или платформу.</div>
      <button class="btn" onClick=${reset}>Сбросить фильтры</button>
    </div>`;
  } else if (view.total === 0 && !run && storage && storage.archived) {
    body = html`<${Archived} storage=${storage} />`;
  } else if (view.total === 0 && run) {
    const done = run.processed || 0, total = run.selected || 20;
    body = html`<div class="empty-box">
      <div class="empty-title">Каталог наполняется: ${done} из ${total} игр</div>
      <div class="empty-text">Идёт первое обновление. Игры появятся в списке по мере обработки.</div>
      <div class="fill-bar"><div style=${{ width: Math.round((done / total) * 100) + "%" }}></div></div>
      <a class="btn" href=${link.monitor()}>Открыть мониторинг</a>
    </div>`;
  } else if (view.total === 0) {
    body = html`<div class="empty-box">
      <div class="empty-title">Каталог пока пуст</div>
      <div class="empty-text">Игры появятся после первого прогона. Он стартует сам по расписанию, запустить его сейчас можно на экране мониторинга.</div>
      <a class="btn" href=${link.monitor()}>Открыть мониторинг</a>
    </div>`;
  } else {
    body = html`
      ${scored.length > 0 && html`<div class=${cx("grid", dense && "is-dense", view.phase === "refreshing" && "is-refreshing")}>
        ${scored.map((g) => html`<${GameCard} key=${g.slug} game=${g} />`)}
      </div>`}
      ${unscored.length > 0 && html`<div class="sect">
          <span class="sect-label">${params.sort === "release_date" ? "без даты" : "без оценки"}</span><span class="sect-line"></span>
          <span class="sect-text">${rule.text}</span>
        </div>
        <div class=${cx("grid", dense && "is-dense", view.phase === "refreshing" && "is-refreshing")}>
          ${unscored.map((g) => html`<${GameCard} key=${g.slug} game=${g} plain=${params.sort !== "release_date"} />`)}
        </div>`}
      ${loadingMore && html`<div class=${cx("grid", dense && "is-dense")} aria-hidden="true"><${Skeletons} count=${Math.min(PAGE, view.total - shown)} /></div>`}
      <div class="pager" ref=${sentinel}>
        ${moreFailed
          ? html`<span>Не удалось загрузить продолжение списка</span><button class="btn" onClick=${loadMore}>Повторить</button>`
          : shown < view.total
            ? html`<span class="pager-done">Показано ${fmt(shown)} из ${fmt(view.total)}</span>`
            : html`<span class="pager-done">Все игры показаны: ${fmt(view.total)}</span>`}
      </div>`;
  }

  const allCount = catalogTotal == null ? "" : fmt(catalogTotal);
  return html`<div>
    <div class="page-head">
      <div class="page-head-main">
        <h1 class="page-title">Каталог игр</h1>
        <p class="page-sub">Данные Metacritic, резюме отзывов от ИИ и похожие игры. Каталог пополняется по расписанию.</p>
      </div>
      <div class="page-head-side">
        <div class="head-label">игр в каталоге</div>
        <div class="head-count">${allCount}</div>
      </div>
    </div>

    <div class="toolbar">
      <div class="tb-row">
        <label class="search">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M11.5 21.75C5.85 21.75 1.25 17.15 1.25 11.5C1.25 5.85 5.85 1.25 11.5 1.25C17.15 1.25 21.75 5.85 21.75 11.5C21.75 17.15 17.15 21.75 11.5 21.75ZM11.5 2.75C6.67 2.75 2.75 6.68 2.75 11.5C2.75 16.32 6.67 20.25 11.5 20.25C16.33 20.25 20.25 16.32 20.25 11.5C20.25 6.68 16.33 2.75 11.5 2.75ZM22 22.75C21.81 22.75 21.62 22.68 21.47 22.53L19.47 20.53C19.18 20.24 19.18 19.76 19.47 19.47C19.76 19.18 20.24 19.18 20.53 19.47L22.53 21.47C22.82 21.76 22.82 22.24 22.53 22.53C22.38 22.68 22.19 22.75 22 22.75Z" fill="var(--t9)" stroke="var(--t9)" stroke-width="0.5" stroke-linejoin="round"/></svg>
          <input type="search" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} placeholder="Например, Elden Ring"
            aria-label="Поиск по названию" maxlength="100" autocomplete="off" />
          ${query && html`<button class="btn btn-icon search-clear" onClick=${(e) => { e.preventDefault(); setQuery(""); }} aria-label="Очистить поиск">✕</button>`}
        </label>
        <${FilterMenu} params=${params} facets=${view.facets} onChange=${(has) => go({ has })} />
        <${SortMenu} params=${params} onPick=${(sort) => go({ sort })} />
        <button class="order-btn" onClick=${() => go({ order: params.order === "desc" ? "asc" : "desc" })}
          title=${params.order === "desc" ? "По убыванию, нажмите для возрастания" : "По возрастанию, нажмите для убывания"}
          aria-label="Направление сортировки">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true"
            style=${{ transform: params.order === "desc" ? "none" : "rotate(180deg)", transition: "transform .2s" }}>
            <path d="M12 21.25C11.81 21.25 11.62 21.18 11.47 21.03L5.4 14.96C5.11 14.67 5.11 14.19 5.4 13.9C5.69 13.61 6.17 13.61 6.46 13.9L12 19.44L17.54 13.9C17.83 13.61 18.31 13.61 18.6 13.9C18.89 14.19 18.89 14.67 18.6 14.96L12.53 21.03C12.38 21.18 12.19 21.25 12 21.25ZM12 21.08C11.59 21.08 11.25 20.74 11.25 20.33V3.5C11.25 3.09 11.59 2.75 12 2.75C12.41 2.75 12.75 3.09 12.75 3.5V20.33C12.75 20.74 12.41 21.08 12 21.08Z"
              fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>

      <div class="chips" role="group" aria-label="Платформы">
        <button class=${cx("chip", !params.platform && "is-on")} onClick=${() => go({ platform: "" })} aria-pressed=${!params.platform}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round" aria-hidden="true">
            <path d="M7.24 2H5.34C3.15 2 2 3.15 2 5.33V7.23C2 9.41 3.15 10.56 5.33 10.56H7.23C9.41 10.56 10.56 9.41 10.56 7.23V5.33C10.57 3.15 9.42 2 7.24 2ZM18.67 13.43H16.77C14.59 13.43 13.44 14.58 13.44 16.76V18.66C13.44 20.84 14.59 21.99 16.77 21.99H18.67C20.85 21.99 22 20.84 22 18.66V16.76C22 14.58 20.85 13.43 18.67 13.43Z"/>
            <path opacity="0.4" d="M18.67 2H16.77C14.59 2 13.44 3.15 13.44 5.33V7.23C13.44 9.41 14.59 10.56 16.77 10.56H18.67C20.85 10.56 22 9.41 22 7.23V5.33C22 3.15 20.85 2 18.67 2ZM7.24 13.43H5.34C3.15 13.43 2 14.58 2 16.76V18.66C2 20.85 3.15 22 5.33 22H7.23C9.41 22 10.56 20.85 10.56 18.67V16.77C10.57 14.58 9.42 13.43 7.24 13.43Z"/>
          </svg>
          <span class="chip-mark">Все платформы</span><span class="chip-count">${allCount}</span>
        </button>
        ${(platforms || []).map((p) => html`<button class=${cx("chip", params.platform === p.slug && "is-on")} key=${p.slug}
            onClick=${() => go({ platform: params.platform === p.slug ? "" : p.slug })} aria-pressed=${params.platform === p.slug} ...${tip(p.name)}>
          <span class="chip-mark">${platMark(p.name)}</span><span class="chip-count">${fmt(p.games)}</span>
        </button>`)}
      </div>

      <div class="tb-foot">
        <span class="muted">${view.phase === "loading" ? "Загружаем…" : `Найдено ${count(view.total, "игра", "игры", "игр")}${filtered && catalogTotal != null ? " из " + fmt(catalogTotal) : ""}`}</span>
        <span class="dot">·</span>
        <span class="muted">${sortLabel}${params.order === "desc" ? ", по убыванию" : ", по возрастанию"}</span>
        <div class="seg" role="group" aria-label="Плотность сетки">
          <button class=${cx(!dense && "is-on")} onClick=${() => setDensity(false)} aria-pressed=${!dense}>Просторно</button>
          <button class=${cx(dense && "is-on")} onClick=${() => setDensity(true)} aria-pressed=${dense}>Плотно</button>
        </div>
        <button class="btn" onClick=${reset} disabled=${!filtered && params.sort === DEFAULT_SORT && params.order === "desc"}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linejoin="round" aria-hidden="true"><path d="M12 22.75C6.8 22.75 2.58 18.52 2.58 13.33C2.58 8.14 6.8 3.9 12 3.9C13.07 3.9 14.11 4.05 15.11 4.36C15.51 4.48 15.73 4.9 15.61 5.3C15.49 5.7 15.07 5.92 14.67 5.8C13.82 5.54 12.92 5.4 12 5.4C7.63 5.4 4.08 8.95 4.08 13.32C4.08 17.69 7.63 21.24 12 21.24C16.37 21.24 19.92 17.69 19.92 13.32C19.92 11.74 19.46 10.22 18.59 8.92C18.36 8.58 18.45 8.11 18.8 7.88C19.14 7.65 19.61 7.74 19.84 8.09C20.88 9.64 21.43 11.45 21.43 13.33C21.42 18.52 17.2 22.75 12 22.75ZM16.13 6.07C15.92 6.07 15.71 5.98 15.56 5.81L12.67 2.49C12.4 2.18 12.43 1.7 12.74 1.43C13.05 1.16 13.53 1.19 13.8 1.5L16.69 4.82C16.96 5.13 16.93 5.61 16.62 5.88C16.49 6.01 16.31 6.07 16.13 6.07ZM12.76 8.53C12.53 8.53 12.3 8.42 12.15 8.22C11.91 7.89 11.98 7.42 12.31 7.17L15.68 4.71C16.01 4.46 16.48 4.54 16.73 4.87C16.98 5.2 16.9 5.67 16.57 5.92L13.2 8.39C13.07 8.49 12.92 8.53 12.76 8.53Z"/></svg>
          <span>Сбросить фильтры</span>
        </button>
      </div>
    </div>

    ${body}
  </div>`;
}
