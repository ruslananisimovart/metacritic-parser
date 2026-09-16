// Экран "Карточка игры": данные Metacritic, оценки по платформам, резюме ИИ, летсплей, видео, похожие игры.
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { api, link } from "./api.js";
import { LAST_GAME_KEY } from "./shell.js";
import {
  ScoreBadge, clockText, cueHide, cueShow, cueToggle, cx, dayOf, fmt, html, metaColor, platMark, plural, ruDate,
  scoreText, stampOf, store, tip, touchInput, userColor, STORES, CompleteStar,
} from "./lib.js";

const LANGS = {
  en: "английский", de: "немецкий", it: "итальянский", es: "испанский", ru: "русский", fr: "французский",
  pt: "португальский", ja: "японский", pl: "польский", tr: "турецкий", ko: "корейский", zh: "китайский", uk: "украинский",
};
const UNITS = {
  second: ["секунду", "секунды", "секунд"], minute: ["минуту", "минуты", "минут"], hour: ["час", "часа", "часов"],
  day: ["день", "дня", "дней"], week: ["неделю", "недели", "недель"], month: ["месяц", "месяца", "месяцев"], year: ["год", "года", "лет"],
};
const LP_LABEL = { queued: "Ищем летсплей", done: "Готово", not_found: "Летсплей не найден", failed: "Не получилось", off: "Поиск выключен" };
const LP_COLOR = { queued: "var(--acc)", done: "var(--green)", not_found: "var(--t6)", failed: "var(--red)", off: "var(--t8)" };

// YouTube отдаёт дату публикации по-английски: "2 years ago", "Streamed 7 days ago"
function publishedText(raw) {
  if (!raw) return "";
  const m = /(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago/i.exec(raw);
  if (!m) return raw;
  const n = Number(m[1]);
  const [one, few, many] = UNITS[m[2].toLowerCase()];
  return `${/streamed/i.test(raw) ? "трансляция" : "опубликован"} ${n} ${plural(n, one, few, many)} назад`;
}
// "из 1 рецензии", "из 99 рецензий": после "из" родительный падеж
function genitive(n, one, many) { return n % 10 === 1 && n % 100 !== 11 ? one : many; }
const reviewsOf = (n, kind) => (kind === "critic" ? plural(n, "рецензия", "рецензии", "рецензий") : plural(n, "оценка", "оценки", "оценок"));

function scoreSub(value, n, kind, platform) {
  if (value != null) return (platform ? platform + " · " : "") + fmt(n) + " " + reviewsOf(n, kind);
  if (n > 0) return kind === "critic" ? `мало рецензий (${n}), оценки пока нет` : `мало оценок (${n}), оценки пока нет`;
  return kind === "critic" ? "рецензий пока нет" : "оценок пока нет";
}
function rowSub(value, n, kind) {
  if (value != null) return fmt(n) + " " + reviewsOf(n, kind);
  return n > 0 ? `мало отзывов (${n})` : kind === "critic" ? "нет рецензий" : "нет оценок";
}

function Hero({ game }) {
  const [broken, setBroken] = useState(false);
  const cover = game.cover_url && !broken;
  // шапка Steam или иконка Google Play вписывается в постер целиком, пустые поля закрывает её же размытая копия
  const wide = cover && game.cover_kind !== "portrait";
  const store = cover && STORES[game.cover_source];
  const developers = (game.developers || []).join(", ");
  const publishers = (game.publishers || []).join(", ");
  return html`<div class="hero">
    <div class=${cx("hero-cover", !cover && "is-empty", wide && "is-wide")}>
      ${wide && html`<img class="gcover-bg" src=${game.cover_url} alt="" aria-hidden="true" />`}
      ${cover ? html`<img src=${game.cover_url} alt=${"Обложка " + game.title} onError=${() => setBroken(true)} />`
        : html`<span class="hero-cover-ph">${game.title} · обложки нет</span>`}
      ${store && html`<span class="gcover-src" ...${tip(`Обложки на Metacritic нет, картинка со страницы игры в ${store}`)}>${store}</span>`}
      ${game.complete && html`<div class="gcover-tags"><${CompleteStar} /></div>`}
    </div>
    <div class="hero-body">
      <h1 class="hero-title">${game.title}</h1>
      <div class="hero-meta">
        <span>${game.release_date ? ruDate(game.release_date) : "дата выхода неизвестна"}</span>
        ${developers && html`<span class="sep">·</span><span>Разработчик: <b>${developers}</b></span>`}
        ${publishers && html`<span class="sep">·</span><span>Издатель: <b>${publishers}</b></span>`}
      </div>
      <div class="hero-tags">
        ${(game.genres || []).map((genre) => html`<span class="tag">${genre}</span>`)}
        <span class=${cx("tag", "tag-age", !game.rating && "is-none")}>${game.rating ? game.rating + " · возрастной рейтинг" : "рейтинга нет"}</span>
      </div>
      <div class="hero-scores">
        <div class="score-box">
          <div class="score-src">metacritic</div>
          <div class="score-row">
            <${ScoreBadge} value=${game.metascore} size="lg" />
            <div><div class="score-box-label">Критики</div><div class="score-box-sub">${scoreSub(game.metascore, game.critic_reviews, "critic", game.main_platform)}</div></div>
          </div>
          <div class="score-row">
            <${ScoreBadge} value=${game.userscore} isUser=${true} size="lg" />
            <div><div class="score-box-label">Игроки</div><div class="score-box-sub">${scoreSub(game.userscore, game.user_reviews, "user")}</div></div>
          </div>
        </div>
        <div class="hero-links">
          <a href=${game.metacritic_url} target="_blank" rel="noopener noreferrer">Открыть на Metacritic</a>
          <span>обновлено ${stampOf(game.updated_at)}</span>
        </div>
      </div>
    </div>
  </div>`;
}

function Platforms({ rows }) {
  return html`<section class="panel">
    <div class="panel-head"><div class="panel-title">Оценки по платформам</div></div>
    <div class="ptable" role="table">
      <div class="ptable-head" role="row"><span>платформа</span><span>критики</span><span>игроки</span></div>
      ${rows.map((row) => html`<div class="prow" role="row" key=${row.slug}>
        <div class="prow-name">
          <span class="prow-mark">${platMark(row.name)}</span><span class="prow-title">${row.name}</span>
          ${row.is_main && html`<span class="main-label">основная</span>`}
        </div>
        <div class="prow-score"><${ScoreBadge} value=${row.metascore} size="sm" /><span class="prow-sub">${rowSub(row.metascore, row.critic_reviews, "critic")}</span></div>
        <div class="prow-score"><${ScoreBadge} value=${row.userscore} isUser=${true} size="sm" /><span class="prow-sub">${rowSub(row.userscore, row.user_reviews, "user")}</span></div>
      </div>`)}
    </div>
  </section>`;
}

// описание с Metacritic, а когда его там нет, со страницы магазина (Steam, Google Play)
function Description({ text, source }) {
  const [open, setOpen] = useState(false);
  const [long, setLong] = useState(false);
  const box = useRef(null);
  // кнопку "Показать полностью" показываем, только если текст правда не влезает
  useLayoutEffect(() => { if (box.current) setLong(box.current.scrollHeight > 150); }, [text]);
  const from = source === "metacritic" ? "Metacritic" : STORES[source] || null;
  return html`<section class="panel">
    <div class="panel-head"><div class="panel-title">Описание</div>${from && html`<span class="panel-note">источник: ${from}, en</span>`}</div>
    ${text
      ? html`<div ref=${box} class=${cx("desc", long && !open && "is-clamped", source !== "metacritic" && "is-store")}>${text}</div>
        ${long && html`<button class="btn-link desc-toggle" onClick=${() => setOpen(!open)} aria-expanded=${open}>${open ? "Свернуть" : "Показать полностью"}</button>`}`
      : html`<div class="desc is-empty">Описания нет ни на Metacritic, ни в магазинах</div>`}
  </section>`;
}

// записи до появления источников хранят пункт строкой, новые объектом с моментами или отзывами
const asPoint = (point) => (point && typeof point === "object" ? point : { text: String(point || ""), moments: [], sources: [] });

function scoreChip(value, isUser) {
  if (value == null) return { text: "—", style: { background: "var(--veil-07)", color: "var(--t9)" } };
  const color = isUser ? userColor(value) : metaColor(value);
  return { text: scoreText(value, isUser), style: { background: color, color: "#FFFFFF" } };
}

/** Карточка пункта: моменты ролика (kind "moment") или отзывы-источники (kind "critic" или "user"). */
function pointCue(point, group, kind) {
  const rows = kind === "moment"
    ? (point.moments || []).map((m) => ({ kind: "moment", label: m.label, quote: m.quote, url: m.url }))
    : (point.sources || []).map((s) => ({
        kind: "source",
        score: scoreChip(s.score, kind === "user"),
        author: s.author || (kind === "user" ? "игрок без имени" : "издание не указано"),
        date: s.date ? ruDate(s.date) : "",
        quote: s.quote,
        url: s.url,
        linkLabel: kind === "critic" ? "Читать рецензию" : "Отзывы игроков на Metacritic",
      }));
  if (!rows.length) return null;
  const sub = kind === "moment"
    ? `${rows.length} ${plural(rows.length, "момент", "момента", "моментов")} в ролике`
    : `${rows.length} ${plural(rows.length, "источник", "источника", "источников")}`;
  return { group, title: point.text, sub, rows };
}

function Point({ point, cue }) {
  if (!cue) return html`<li>${point.text}</li>`;
  // мышь: показываем по наведению и по фокусу с клавиатуры; сенсорный экран: по нажатию
  const show = (e) => { if (!touchInput()) cueShow(cue, e.currentTarget); };
  return html`<li class="cue-item" tabindex="0" aria-label=${`${point.text}. ${cue.sub}`}
    onPointerEnter=${show} onPointerLeave=${() => { if (!touchInput()) cueHide(); }}
    onFocus=${show} onBlur=${() => cueHide()}
    onClick=${(e) => { if (touchInput()) cueToggle(cue, e.currentTarget); }}>
    <span class="cue-text">${point.text}</span><span class="cue-count">${cue.rows.length}</span>
  </li>`;
}

function ProsCons({ pros, cons, prosTitle, consTitle, prosEmpty, consEmpty, cueOf }) {
  const list = (items, side) => html`<ul>${items.map((item, i) => {
    const point = asPoint(item);
    return html`<${Point} point=${point} cue=${cueOf ? cueOf(point, side) : null} key=${i} />`;
  })}</ul>`;
  return html`<div class="pc-grid">
    <div class="pc pc-pro">
      <div class="pc-title">${prosTitle}</div>
      ${pros.length ? list(pros, "pros") : html`<div class="pc-empty">${prosEmpty}</div>`}
    </div>
    <div class="pc pc-con">
      <div class="pc-title">${consTitle}</div>
      ${cons.length ? list(cons, "cons") : html`<div class="pc-empty">${consEmpty}</div>`}
    </div>
  </div>`;
}

function Reviews({ game }) {
  const { critic, user } = game.summaries;
  const [tab, setTab] = useState(critic || !user ? "critic" : "user");
  const summary = tab === "critic" ? critic : user;
  const reviews = tab === "critic" ? game.critic_reviews : game.user_reviews;
  const tabs = [["critic", "Что говорят критики"], ["user", "Что говорят игроки"]];
  let body;
  if (summary) {
    const groups = tab === "critic" ? ["Критики хвалят", "Критики ругают"] : ["Игрокам нравится", "Игрокам не нравится"];
    const cueOf = (point, side) => pointCue(point, side === "pros" ? groups[0] : groups[1], tab);
    const sourced = [...summary.likes, ...summary.dislikes].some((p) => asPoint(p).sources && asPoint(p).sources.length);
    body = html`
      <${ProsCons} pros=${summary.likes} cons=${summary.dislikes} prosTitle="Нравится" consTitle="Не нравится"
        prosEmpty="Явных плюсов в отзывах нет" consEmpty="Существенных минусов в отзывах нет" cueOf=${cueOf} />
      <div class="verdict"><div class="verdict-title">Вывод</div><p>${summary.summary}</p></div>
      <div class="source">
        <span>Составлено ИИ (${summary.model}) по ${summary.reviews_used} из ${fmt(summary.reviews_total)} ${tab === "critic" ? genitive(summary.reviews_total, "рецензии", "рецензий") : genitive(summary.reviews_total, "оценки", "оценок")}, обновлено ${dayOf(summary.updated_at)}</span>
        ${sourced && html`<span>Отзывы, из которых взят пункт: наведите на строку</span>`}
      </div>`;
  } else {
    const text = reviews > 0
      ? "Резюме пока нет: оно строится по текстовым отзывам при следующем обновлении, если такие отзывы есть."
      : tab === "critic" ? "Рецензий критиков пока нет." : "Отзывов игроков пока нет.";
    body = html`<div class="empty-note">${text}</div>`;
  }
  return html`<section class="panel">
    <div class="panel-head">
      <div class="panel-title">Резюме отзывов</div>
      <span class="chip-ai">составлено ИИ</span>
      <div class="tabs" role="tablist">
        ${tabs.map(([id, label]) => html`<button role="tab" aria-selected=${tab === id} class=${cx(tab === id && "is-on")} onClick=${() => setTab(id)}>${label}</button>`)}
      </div>
    </div>
    ${body}
  </section>`;
}

function Letsplay({ lp, enabled }) {
  const [showText, setShowText] = useState(false);
  const [showError, setShowError] = useState(false);
  const status = lp ? lp.status : enabled ? "queued" : "off";
  const max = lp ? lp.max_attempts : 3;
  let attempts = "";
  if (!lp || status === "queued") attempts = enabled ? "в очереди" : "";
  else if (status === "done") attempts = `попытка ${lp.attempts} из ${max}`;
  // летсплея нет: когда игру поищут снова по правилу из окна "Расписание и прогон"
  else if (status === "not_found") attempts = lp.next_search_at ? `следующий поиск ${ruDate(lp.next_search_at.slice(0, 10))}`
    : lp.recheck ? "лимит повторных поисков исчерпан" : "повторный поиск выключен";
  else attempts = lp.attempts >= max ? `попыток ${lp.attempts} из ${max} за сегодня, завтра снова` : `попыток ${lp.attempts} из ${max} за сегодня`;

  let body;
  if (status === "done") {
    const c = lp.conclusion || { summary: "", pros: [], cons: [], verdict: "" };
    const thumb = lp.video_id ? `https://i.ytimg.com/vi/${lp.video_id}/hqdefault.jpg` : null;
    const source = `${lp.transcript_source === "captions" ? "Субтитры YouTube" : "Расшифровка Whisper"}${lp.language ? ", " + (LANGS[lp.language] || lp.language) : ""}`;
    const coverage = lp.coverage ? `начало ${lp.coverage.head_minutes} мин и концовка ${lp.coverage.tail_minutes} мин` : "ролик целиком";
    const channel = [lp.channel, lp.views != null ? fmt(lp.views) + " " + plural(lp.views, "просмотр", "просмотра", "просмотров") : "", publishedText(lp.published)].filter(Boolean).join(" · ");
    body = html`<div>
      <div class="lp-top">
        <a class="lp-thumb" href=${lp.url} target="_blank" rel="noopener noreferrer" aria-label=${"Открыть ролик на YouTube: " + lp.title}>
          ${thumb && html`<img src=${thumb} alt="" loading="lazy" />`}
          <span class="play">▶</span>
          ${lp.duration ? html`<span class="lp-dur">${clockText(lp.duration)}</span>` : null}
        </a>
        <div class="lp-info">
          <a class="lp-title" href=${lp.url} target="_blank" rel="noopener noreferrer">${lp.title}</a>
          <div class="lp-channel">${channel}</div>
          <div class="lp-chips"><span class="mono-chip">${source}</span><span class="mono-chip is-violet">${coverage}</span></div>
          ${lp.segments.length > 0 && html`<button class="btn-link lp-toggle" onClick=${() => setShowText(!showText)} aria-expanded=${showText}>
            ${showText ? "Скрыть расшифровку" : "Показать расшифровку"}</button>`}
        </div>
      </div>
      <div class="lp-conclusion">
        <${ProsCons} pros=${c.pros} cons=${c.cons} prosTitle="Блогер хвалит" consTitle="Блогер ругает"
          prosEmpty="Похвалы в ролике не прозвучало" consEmpty="Претензий в ролике не прозвучало"
          cueOf=${(point, side) => pointCue(point, side === "pros" ? "Блогер хвалит" : "Блогер ругает", "moment")} />
        ${[...c.pros, ...c.cons].some((p) => asPoint(p).moments && asPoint(p).moments.length)
          && html`<div class="cue-note">Таймкоды ${lp.transcript_source === "captions" ? "из субтитров" : "из расшифровки Whisper"}: наведите на пункт, чтобы открыть момент в ролике</div>`}
      </div>
      <div class="verdict"><div class="verdict-title">О чём ролик и итог</div><p>${c.summary}</p>${c.verdict && html`<p>${c.verdict}</p>`}</div>
      <div class="source">Составлено ИИ по ${lp.transcript_source === "captions" ? "субтитрам" : "расшифровке"} ролика, обновлено ${dayOf(lp.updated_at)}</div>
      ${showText && html`<div class="transcript">
        <div class="transcript-note">Расшифровка на языке ролика, время ведёт на нужный момент в YouTube</div>
        ${lp.segments.map((s) => html`<div class="seg-row">
          <a href=${`${lp.url}${lp.url.includes("?") ? "&" : "?"}t=${Math.floor(s.start)}s`} target="_blank" rel="noopener noreferrer">${clockText(s.start)}</a>
          <span>${s.text}</span></div>`)}
      </div>`}
    </div>`;
  } else {
    const texts = {
      off: "Поиск летсплеев выключен в настройках сервиса (LETSPLAY_ENABLED).",
      queued: "Поиск ролика запланирован. Летсплеи считаются в фоне по одному, прогресс виден на экране мониторинга.",
      not_found: `Причина: ${(lp && lp.reason) || "поиск не дал подходящих роликов"}. Повтор не чаще раза в сутки, всего не больше ${max} попыток.`,
      failed: `Причина: ${(lp && lp.reason) || "не удалось обработать ролик"}. ${lp && lp.attempts >= max ? "Попытки исчерпаны." : "Попробуем снова через сутки."}`,
    };
    body = html`<div class="lp-empty">
      <span class="lp-empty-icon">${status === "queued" ? "◷" : status === "failed" ? "!" : "∅"}</span>
      <div>
        <div class="lp-empty-title">${LP_LABEL[status]}</div>
        <div class="lp-empty-text">${texts[status]}</div>
        ${status === "queued" && html`<a class="btn-link lp-empty-link" href=${link.monitor()}>Открыть мониторинг</a>`}
        ${status === "failed" && lp && lp.error && html`<button class="btn-link lp-empty-link" onClick=${() => setShowError(!showError)}>${showError ? "Скрыть текст ошибки" : "Показать текст ошибки"}</button>`}
        ${showError && html`<pre class="error-text">${lp.error}</pre>`}
      </div>
    </div>`;
  }
  return html`<section class="panel">
    <div class="panel-head">
      <div class="panel-title">Летсплей на YouTube</div>
      <span class="status-chip" style=${{ color: LP_COLOR[status] }}>${LP_LABEL[status]}</span>
      ${attempts && html`<span class="panel-note">${attempts}</span>`}
    </div>
    ${body}
  </section>`;
}

/** Плеер в окне поверх страницы: рамка одного размера с первого кадра. В узкой колонке карточки плеер
 * JW Player запоминал размер рамки до того, как колонка сжималась, и показывал четверть картинки. */
// Плеер JW Player по умолчанию 640x360; страница players/<id>.html подгоняет его под своё окно только если
// успела подвесить обработчик до готовности плеера (опрос раз в 100 мс), из кэша обычно не успевает.
// Поэтому рамка для него делается ровно 640x360, а масштабируется до ширины окна через transform
const JW_W = 640, JW_H = 360;

// Трейлер из Steam это поток HLS: Chrome и Firefox сами его не играют, нужен hls.js; Safari играет напрямую.
// Библиотека грузится с cdnjs при первом открытии такого трейлера, версия закреплена
const HLS_SRC = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.6.15/hls.min.js";
let hlsLoading = null;
function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsLoading) {
    hlsLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = HLS_SRC;
      script.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error("hls.js не загрузился")));
      script.onerror = () => { hlsLoading = null; reject(new Error("hls.js не загрузился")); };
      document.head.appendChild(script);
    });
  }
  return hlsLoading;
}

function HlsPlayer({ src, poster, title }) {
  const video = useRef(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    const el = video.current;
    if (!el) return undefined;
    let hls = null, gone = false;
    if (el.canPlayType("application/vnd.apple.mpegurl")) {
      el.src = src;
      el.play().catch(() => {});
    } else {
      loadHls().then((Hls) => {
        if (gone) return;
        if (!Hls.isSupported()) { setError("Браузер не умеет играть этот поток"); return; }
        hls = new Hls();
        hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) setError("Поток Steam не открылся"); });
        hls.loadSource(src);
        hls.attachMedia(el);
        hls.on(Hls.Events.MANIFEST_PARSED, () => el.play().catch(() => {}));
      }).catch((e) => !gone && setError(e.message));
    }
    return () => { gone = true; if (hls) hls.destroy(); };
  }, [src]);
  return html`<div class="video-frame-box">
    <video class="video-frame" ref=${video} poster=${poster || undefined} controls playsinline title=${title}></video>
    ${error && html`<div class="video-error">${error}</div>`}
  </div>`;
}

function VideoModal({ game, onClose }) {
  const { src, poster, steam, title, source, page } = videoEmbed(game);
  const box = useRef(null);
  const [scale, setScale] = useState(1);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    const fit = () => { if (box.current) setScale(box.current.getBoundingClientRect().width / JW_W); };
    addEventListener("keydown", onKey);
    addEventListener("resize", fit);
    fit();
    return () => { removeEventListener("keydown", onKey); removeEventListener("resize", fit); };
  }, []);
  const frameStyle = { width: JW_W + "px", height: JW_H + "px", transform: `scale(${scale})` };
  return html`<div class="modal-back" onClick=${onClose}>
    <div class="video-modal" role="dialog" aria-modal="true" aria-label=${title} onClick=${(e) => e.stopPropagation()}>
      <div class="video-modal-head"><div class="panel-title">Видео</div><button class="modal-x" onClick=${onClose} aria-label="Закрыть">✕</button></div>
      ${steam
        ? html`<${HlsPlayer} src=${src} poster=${poster} title=${title} />`
        : html`<div class="video-frame-box" ref=${box}>
          <iframe class="video-frame is-fixed" src=${src} title=${title} style=${frameStyle} width=${JW_W} height=${JW_H}
            allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe>
        </div>`}
      <div class="video-title">${title}</div>
      <div class="video-src">
        <span>${source}</span>
        <a href=${page} target="_blank" rel="noopener noreferrer">Открыть</a>
      </div>
    </div>
  </div>`;
}

/** Что играть и чем: видео Metacritic это страница плеера JW Player (постер по id медиа из адреса
 * players/<id>.html), трейлер из Steam это поток HLS со своим кадром-заставкой. */
function videoEmbed(game) {
  const steam = game.video_source === "steam";
  const jw = !steam && game.video_url ? (game.video_url.match(/cdn\.jwplayer\.com\/players\/([A-Za-z0-9]{8})/) || [])[1] : null;
  const poster = steam ? game.video_poster : jw ? `https://cdn.jwplayer.com/v2/media/${jw}/poster.jpg?width=640` : null;
  return {
    steam, src: game.video_url, poster,
    title: game.video_title || "Видео игры",
    source: steam ? "Трейлер из Steam" : "JW Player · metacritic.com",
    page: game.video_page || game.video_url,
  };
}

// окно плеера открывает экран целиком (onPlay): из приклеенной колонки оно не поднялось бы над боковым меню
function Video({ game, onPlay }) {
  const { poster, title, source, page } = videoEmbed(game);
  return html`<section class="panel">
    <div class="panel-head"><div class="panel-title">Видео</div></div>
    ${game.video_url
      ? html`<div>
        <button class="video-box" onClick=${onPlay}
          style=${poster ? { backgroundImage: `url(${poster})` } : null} aria-label="Смотреть видео">
          <span class="play play-acc">▶</span></button>
        <div class="video-title">${title}</div>
        <div class="video-src">
          <span>${source}</span>
          <a href=${page} target="_blank" rel="noopener noreferrer">Открыть</a>
        </div>
      </div>`
      : html`<div class="empty-note">Видео нет: ролика для этой игры нет ни у Metacritic, ни в Steam.</div>`}
  </section>`;
}

function Similar({ items }) {
  return html`<section class="panel">
    <div class="panel-head"><div class="panel-title">Похожие игры</div></div>
    ${items.length
      ? html`<div class="similar">${items.map((s) => html`<a class="sim" href=${link.game(s.slug)} key=${s.slug}>
          <div class=${cx("sim-cover", s.cover_url && s.cover_kind !== "portrait" && "is-wide")}>
            ${s.cover_url && s.cover_kind !== "portrait" && html`<img class="gcover-bg" src=${s.cover_url} alt="" aria-hidden="true" loading="lazy" />`}
            ${s.cover_url ? html`<img src=${s.cover_url} alt="" loading="lazy" />` : null}
          </div>
          <div class="sim-body">
            <div class="sim-title">${s.title}</div>
            <div class="sim-row">
              <${ScoreBadge} value=${s.metascore} size="xs" /><${ScoreBadge} value=${s.userscore} isUser=${true} size="xs" />
              <span class="sim-match">похожесть ${s.score.toFixed(2)}</span>
            </div>
          </div>
          <span class="sim-arrow">›</span>
        </a>`)}</div>`
      : html`<div class="empty-note">Похожих игр в каталоге пока нет: каталог ещё наполняется.</div>`}
  </section>`;
}

function Skeleton() {
  return html`<div class="hero hero-skel">
    <div class="shimmer" style=${{ flex: "0 0 168px", height: "224px", borderRadius: "14px" }}></div>
    <div style=${{ flex: 1, display: "flex", flexDirection: "column", gap: "12px" }}>
      <div class="shimmer" style=${{ height: "30px", width: "60%" }}></div>
      <div class="skel" style=${{ height: "14px", width: "40%" }}></div>
      <div class="skel" style=${{ height: "52px", width: "320px", marginTop: "18px", borderRadius: "14px" }}></div>
    </div>
  </div>`;
}

// хлебные крошки ведут в каталог с тем же поиском и фильтрами
const catalogBack = () => link.catalog();

export function GameScreen({ route, status }) {
  const slug = route.slug;
  const [view, setView] = useState({ phase: "loading", game: null });
  const [retry, setRetry] = useState(0);
  const [video, setVideo] = useState(false);
  const request = useRef(0);
  const data = status.data;
  useEffect(() => setVideo(false), [slug]);

  function load(quiet) {
    const id = ++request.current;
    if (!quiet) setView({ phase: "loading", game: null });
    api("/games/" + encodeURIComponent(slug))
      .then((game) => {
        if (id !== request.current) return;
        setView({ phase: "ready", game });
        store.set(LAST_GAME_KEY, slug);
      })
      .catch((e) => {
        if (id !== request.current || quiet) return;
        setView({ phase: e.status === 404 ? "missing" : "error", game: null });
      });
  }
  useEffect(() => { load(false); }, [slug, retry]);

  // фоновые воркеры закончили летсплей или прошёл прогон: подтягиваем свежую карточку без мигания
  const liveKey = data ? JSON.stringify([data.letsplays, data.runs && data.runs[0] && data.runs[0].finished_at]) : "";
  const lastLive = useRef(liveKey);
  useEffect(() => {
    if (lastLive.current && liveKey && lastLive.current !== liveKey && view.phase === "ready") load(true);
    lastLive.current = liveKey;
  }, [liveKey]);

  const game = view.game;
  const crumbs = html`<nav class="crumbs" aria-label="Путь">
    <a href=${catalogBack()}>Каталог игр</a><span class="sep">›</span><span class="here">${game ? game.title : slug}</span></nav>`;

  if (view.phase === "missing") {
    return html`<div>${crumbs}<div class="empty-box">
      <div class="empty-title">Игра не найдена</div>
      <div class="empty-text">Такой игры нет в каталоге: возможно, адрес устарел или игра ещё не обработана.</div>
      <a class="btn" href=${catalogBack()}>Вернуться в каталог</a></div></div>`;
  }
  if (view.phase === "error") {
    return html`<div>${crumbs}<div class="error-box">
      <div class="empty-title">Не удалось загрузить карточку</div>
      <div class="empty-text">Сервис не ответил на запрос. Проверьте, что он запущен, и попробуйте ещё раз.</div>
      <button class="btn" onClick=${() => setRetry((n) => n + 1)}>Повторить</button></div></div>`;
  }
  if (!game) return html`<div>${crumbs}<${Skeleton} /></div>`;

  const letsplaysEnabled = !data || data.whisper != null;
  return html`<div>
    ${crumbs}
    <${Hero} game=${game} />
    <div class="card-grid">
      <div class="col">
        <${Platforms} rows=${game.platform_scores} />
        <${Description} text=${game.description} source=${game.description_source} />
        <${Reviews} game=${game} key=${game.slug} />
        <${Letsplay} lp=${game.letsplay} enabled=${letsplaysEnabled} key=${game.slug + "-lp"} />
      </div>
      <div class="col side">
        <${Video} game=${game} onPlay=${() => setVideo(true)} />
        <${Similar} items=${game.similar} />
      </div>
    </div>
    ${video && html`<${VideoModal} game=${game} onClose=${() => setVideo(false)} />`}
  </div>`;
}
