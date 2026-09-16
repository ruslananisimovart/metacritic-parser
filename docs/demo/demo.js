// Демонстрация сервиса без сервиса: интерфейс тот же, а ответы API и поток статуса берутся из записи
// настоящей работы (demo/data). Часы страницы идут по времени записи, поэтому таймеры, "N минут назад"
// и календарь показывают ровно то, что было видно в тот момент. Скрипт подключается до модулей интерфейса.
(function () {
  "use strict";
  const D = window.DEMO;
  const base = new URL(".", document.baseURI);
  const RealDate = Date;
  const realNow = () => RealDate.now();

  // ---- шкала: паузы без событий дольше GAP секунд сжимаются, чтобы запись не простаивала ----
  const GAP = D.gap || 6;
  const times = D.times; // все моменты событий по возрастанию, секунды записи
  const plays = [0];
  for (let i = 1; i < times.length; i++) plays.push(plays[i - 1] + Math.min(times[i] - times[i - 1], GAP));
  const duration = plays[plays.length - 1] + 2;

  function lastLE(arr, value, key) {
    let lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if ((key ? key(arr[mid]) : arr[mid]) <= value) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }
  function recordTime(p) {
    const i = Math.max(0, lastLE(plays, p));
    return times[i] + Math.min(p - plays[i], i + 1 < times.length ? times[i + 1] - times[i] : Infinity);
  }
  function playPos(t) {
    const i = Math.max(0, lastLE(times, t));
    return plays[i] + Math.min(t - times[i], GAP);
  }

  // ---- плеер: позиция, скорость, пауза ----
  const SPEEDS = [1, 4, 16];
  const clock = { pos: 0, anchor: realNow(), playing: true, speed: D.speed || 4 };
  function position() {
    if (!clock.playing) return clock.pos;
    return Math.min(duration, clock.pos + (realNow() - clock.anchor) / 1000 * clock.speed);
  }
  function setPos(p) { clock.pos = Math.max(0, Math.min(duration, p)); clock.anchor = realNow(); }
  const now = () => Math.round(recordTime(position()) * 1000);

  // часы страницы: new Date() и Date.now() отдают время записи
  class DemoDate extends RealDate {
    constructor(...args) { if (args.length === 0) super(now()); else super(...args); }
    static now() { return now(); }
  }
  window.Date = DemoDate;

  // ---- состояние на момент t ----
  function folder(list, apply, init) {
    // list: [[t, ...], ...] по времени; ключевые кадры для быстрой перемотки назад
    const frames = [];
    let state = init();
    list.forEach((item, i) => {
      state = apply(state, item);
      if (i % 50 === 0) frames.push([i, JSON.parse(JSON.stringify(state))]);
    });
    let cacheIndex = -2, cacheState = null;
    return function at(t) {
      const i = lastLE(list, t, (x) => x[0]);
      if (i === cacheIndex) return cacheState;
      let start = -1, s = init();
      if (cacheIndex >= -1 && cacheIndex < i) { start = cacheIndex; s = cacheState; }
      else {
        const f = frames[lastLE(frames, i, (x) => x[0])];
        if (f) { start = f[0]; s = JSON.parse(JSON.stringify(f[1])); }
      }
      for (let k = start + 1; k <= i; k++) s = apply(s, list[k]);
      cacheIndex = i; cacheState = s;
      return s;
    };
  }
  const patchApply = (state, [, patch, gone]) => {
    const next = Object.assign({}, state, patch);
    (gone || []).forEach((k) => { delete next[k]; });
    return next;
  };
  const statusAt = folder(D.status, patchApply, () => ({}));
  const catalogAt = folder(D.catalog, patchApply, () => ({}));
  const flagsAt = folder(D.flags, patchApply, () => ({}));
  const pick = (list, t) => { const i = lastLE(list || [], t, (x) => x[0]); return list && list.length ? list[Math.max(0, i)][1] : null; };

  // ---- ответы API ----
  const PLATFORM_NAMES = D.platform_names; // slug -> name
  const HAS = ["complete", "cover", "description", "video", "scores", "critic_summary", "user_summary", "letsplay"];

  function catalog(params, t) {
    const games = catalogAt(t), flags = flagsAt(t);
    const q = (params.get("q") || "").trim().toLowerCase();
    const platform = params.get("platform") || "";
    const sort = params.get("sort") || "release_date";
    const order = params.get("order") === "asc" ? "asc" : "desc";
    const limit = Number(params.get("limit") || 24), offset = Number(params.get("offset") || 0);
    const has = (params.get("has") || "").split(",").filter((k) => HAS.includes(k));
    let items = Object.values(games);
    if (q) items = items.filter((g) => (g.title || "").toLowerCase().includes(q));
    if (platform) items = items.filter((g) => (D.game_platforms[g.slug] || []).includes(platform));
    // flags[slug]: отметки игры через запятую, например "cover,video,letsplay"
    const hasKey = (g, k) => ("," + (flags[g.slug] || "") + ",").includes("," + k + ",");
    const facets = {};
    HAS.forEach((k) => { facets[k] = items.filter((g) => hasKey(g, k)).length; });
    has.forEach((k) => { items = items.filter((g) => hasKey(g, k)); });
    const value = (g) => sort === "title" ? (g.title || "").toLowerCase() : sort === "release_date" ? g.release_date : g[sort];
    items.sort((a, b) => {
      const va = value(a), vb = value(b);
      if (va == null && vb != null) return 1;
      if (vb == null && va != null) return -1;
      if (va != null && vb != null && va !== vb) return (va < vb ? -1 : 1) * (order === "asc" ? 1 : -1);
      return (a.title || "").toLowerCase().localeCompare((b.title || "").toLowerCase());
    });
    return { total: items.length, facets, items: items.slice(offset, offset + limit) };
  }

  function platforms(t) {
    const counts = {};
    Object.keys(catalogAt(t)).forEach((slug) => (D.game_platforms[slug] || []).forEach((p) => { counts[p] = (counts[p] || 0) + 1; }));
    return Object.entries(counts).map(([slug, games]) => ({ slug, name: PLATFORM_NAMES[slug] || slug, games }))
      .sort((a, b) => b.games - a.games || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  }

  function runs(params, t) {
    const page = pick(D.runs, t) || { items: [], total: 0 };
    const filter = params.get("status") || "all";
    const all = page.items.filter((r) => filter === "all" || (filter === "ok" ? r.status === "done" : r.status !== "done"));
    const limit = Number(params.get("limit") || 20), offset = Number(params.get("offset") || 0);
    return { items: all.slice(offset, offset + limit), total: all.length };
  }

  const realFetch = window.fetch.bind(window);
  const cardFiles = {};
  function card(slug, t) {
    if (!catalogAt(t)[slug]) return Promise.resolve(null);
    if (!cardFiles[slug]) {
      cardFiles[slug] = realFetch(new URL("demo/data/cards/" + encodeURIComponent(slug) + ".json", base)).then((r) => r.ok ? r.json() : []);
    }
    return cardFiles[slug].then((versions) => pick(versions, t));
  }

  function answer(path, params, t) {
    if (path === "/api/games") return catalog(params, t);
    if (path.startsWith("/api/games/")) return card(decodeURIComponent(path.slice(11)), t);
    if (path === "/api/platforms") return platforms(t);
    if (path === "/api/status") return statusAt(t);
    if (path === "/api/runs") return runs(params, t);
    if (path === "/api/calendar") {
      const key = `calendar-${params.get("year")}-${String(params.get("month")).padStart(2, "0")}`;
      return pick(D.misc[key], t);
    }
    const misc = { "/api/schedule": "schedule", "/api/storage": "storage", "/api/llm": "llm", "/api/health": "health" }[path];
    if (misc) return pick(D.misc[misc], t);
    return null;
  }

  // ---- действия: в записи их не повторить, но запуск прогона перематывает к настоящему прогону ----
  function runStartAfter(t) {
    return (D.marks || []).find((m) => m.kind === "run" && m.t > t + 1);
  }

  let noteTimer = null;
  function note(text) {
    let el = document.getElementById("demo-note");
    if (!el) {
      el = document.createElement("div");
      el.id = "demo-note";
      el.className = "toast demo-note";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.hidden = false;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { el.hidden = true; }, 4200);
  }

  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  window.fetch = function (input, init) {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!raw.startsWith("/api")) return realFetch(input, init);
    const url = new URL(raw, "http://demo");
    const method = ((init && init.method) || "GET").toUpperCase();
    const t = now() / 1000;
    if (method === "POST" && url.pathname === "/api/schedule/preview") return Promise.resolve(json(pick(D.misc.schedule, t)));
    if (method === "POST" && url.pathname === "/api/runs") {
      const mark = runStartAfter(t);
      if (mark && !statusAt(t).run) {
        jump(mark.t - 3, true);
        return Promise.resolve(json({ status: "started" }, 202));
      }
      note("Демонстрация: в записи дальше нет прогона, запустите запись сначала кнопкой ↺ внизу");
      return Promise.resolve(json({ detail: "demo" }, 409));
    }
    if (method !== "GET") {
      note("Демонстрация: настройки и действия меняются только на запущенном сервисе");
      return Promise.reject(new TypeError("demo"));
    }
    return Promise.resolve(answer(url.pathname, url.searchParams, t))
      .then((body) => body == null ? json({ detail: "not found" }, 404) : json(body));
  };

  // выгрузки CSV и ZIP: файлы из записи
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a[href^='/api/']");
    if (!a) return;
    e.preventDefault();
    const href = a.getAttribute("href");
    const m = href.match(/^\/api\/export\/(\w+)\.csv$/);
    const file = m ? `${m[1]}.csv` : href === "/api/export.zip" ? "export.zip" : null;
    if (!file) { note("Демонстрация: архивы хранилища скачиваются только с запущенного сервиса"); return; }
    const link = document.createElement("a");
    link.href = new URL("demo/files/" + file, base).href;
    link.download = "skytec-" + file;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }, true);

  // ---- поток статуса: новый снимок уходит интерфейсу, как только запись до него дошла ----
  const sources = new Set();
  let lastSent = null;
  function emit(force) {
    const state = statusAt(now() / 1000);
    if (!force && state === lastSent) return;
    lastSent = state;
    const data = JSON.stringify(state);
    sources.forEach((s) => { if (s.onmessage) s.onmessage({ data }); });
  }
  window.EventSource = function () {
    const source = this;
    sources.add(source);
    setTimeout(() => { if (source.onopen) source.onopen({}); emit(true); }, 20);
    this.close = () => sources.delete(source);
  };

  // ---- панель плеера ----
  const fmt = new Intl.DateTimeFormat("ru-RU", { timeZone: D.timezone, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  let ui = null;
  function jump(p, play) {
    setPos(p);
    if (play !== undefined) clock.playing = play;
    emit(true);
    // экраны, которые читают API по запросу (каталог, карточка, история), перечитываются сами при
    // следующем снимке статуса; открытую карточку обновляем по смене хеша
    dispatchEvent(new CustomEvent("demo:jump"));
    render();
  }
  function render() {
    if (!ui) return;
    const p = position();
    ui.fill.style.width = (p / duration * 100).toFixed(3) + "%";
    ui.time.textContent = fmt.format(new RealDate(now()));
    const ended = p >= duration;
    ui.play.innerHTML = ended ? ICON_RESTART : clock.playing ? ICON_PAUSE : ICON_PLAY;
    ui.play.setAttribute("aria-label", ended ? "Сначала" : clock.playing ? "Пауза" : "Играть");
    ui.speeds.forEach((b) => b.classList.toggle("is-on", Number(b.dataset.speed) === clock.speed));
    const state = statusAt(now() / 1000);
    ui.state.textContent = state.run ? "идёт прогон" : (state.whisper && state.whisper.busy) || (state.letsplay_queue > 0) ? "летсплеи" : "ожидание";
    ui.state.className = "demo-state" + (state.run ? " is-run" : ui.state.textContent === "летсплеи" ? " is-lp" : "");
  }
  const ICON_PLAY = '<svg width="14" height="14" viewBox="0 0 14 14"><path d="M3.5 2.2v9.6a.6.6 0 0 0 .9.5l7.6-4.8a.6.6 0 0 0 0-1L4.4 1.7a.6.6 0 0 0-.9.5Z" fill="currentColor"/></svg>';
  const ICON_PAUSE = '<svg width="14" height="14" viewBox="0 0 14 14"><rect x="3" y="2" width="2.8" height="10" rx="1" fill="currentColor"/><rect x="8.2" y="2" width="2.8" height="10" rx="1" fill="currentColor"/></svg>';
  const ICON_RESTART = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2.6 7a4.4 4.4 0 1 0 1.3-3.1M2.4 1.6v2.6H5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function mount() {
    const bar = document.createElement("div");
    bar.className = "demo-bar";
    bar.innerHTML = `
      <button type="button" class="demo-btn demo-play"></button>
      <div class="demo-mid">
        <div class="demo-top"><span class="demo-label">Демо · запись работы сервиса</span><span class="demo-state"></span><span class="demo-time"></span></div>
        <div class="demo-track"><div class="demo-fill"></div>${(D.marks || []).filter((m) => m.kind === "run").map((m) =>
          `<i class="demo-mark" style="left:${(playPos(m.t) / duration * 100).toFixed(3)}%" title="${m.label}"></i>`).join("")}</div>
      </div>
      <div class="demo-speeds">${SPEEDS.map((s) => `<button type="button" class="demo-speed" data-speed="${s}">×${s}</button>`).join("")}</div>`;
    document.body.appendChild(bar);
    ui = {
      play: bar.querySelector(".demo-play"), fill: bar.querySelector(".demo-fill"), time: bar.querySelector(".demo-time"),
      state: bar.querySelector(".demo-state"), speeds: [...bar.querySelectorAll(".demo-speed")],
    };
    ui.play.addEventListener("click", () => {
      if (position() >= duration) { jump(0, true); return; }
      setPos(position());
      clock.playing = !clock.playing;
      render();
    });
    ui.speeds.forEach((b) => b.addEventListener("click", () => { setPos(position()); clock.speed = Number(b.dataset.speed); render(); }));
    const track = bar.querySelector(".demo-track");
    const seek = (e) => {
      const r = track.getBoundingClientRect();
      jump((Math.min(Math.max(e.clientX - r.left, 0), r.width) / r.width) * duration);
    };
    track.addEventListener("pointerdown", (e) => {
      track.setPointerCapture(e.pointerId);
      seek(e);
      const move = (ev) => seek(ev);
      track.addEventListener("pointermove", move);
      track.addEventListener("pointerup", () => track.removeEventListener("pointermove", move), { once: true });
    });
    render();
  }

  setInterval(() => {
    if (clock.playing && position() >= duration) { setPos(duration); clock.playing = false; }
    emit(false);
    render();
  }, 250);

  if (document.readyState === "loading") addEventListener("DOMContentLoaded", mount); else mount();
})();
