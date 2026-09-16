// Запросы к API сервиса, поток статуса и маршруты экранов.
import { useEffect, useState } from "preact/hooks";
import { mmss, setTimeZone, store, toast } from "./lib.js";

export class ApiError extends Error {
  constructor(status, detail, headers) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
    this.headers = headers;
  }
}

export async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch("/api" + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, "network", null);
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, data && data.detail, response.headers);
  return data;
}

// Поток /api/events: одно соединение на страницу, общее для меню, каталога и мониторинга.
// EventSource сам переподключается; пока связи нет, на экране остаются последние данные.
const status = { data: null, online: false, updatedAt: null, source: null };
const subscribers = new Set();
const notify = () => subscribers.forEach((fn) => fn());

function accept(data) {
  status.data = data;
  status.updatedAt = Date.now();
  if (data && data.timezone) setTimeZone(data.timezone);
  notify();
}

function connect() {
  if (status.source) return;
  const source = new EventSource("/api/events");
  status.source = source;
  source.onopen = () => { status.online = true; notify(); };
  source.onmessage = (event) => {
    status.online = true;
    try { accept(JSON.parse(event.data)); } catch (e) { /* битое событие пропускаем, следующее придёт через 15 с */ }
  };
  source.onerror = () => { status.online = false; notify(); };
}

// после действий (запуск прогона, смена модели) не ждём события, а сразу берём свежий снимок
export function refreshStatus() { return api("/status").then(accept).catch(() => {}); }

// ручной запуск прогона: кнопка мониторинга, повтор из истории, "Запустить чистый прогон" в каталоге
export async function requestRun() {
  try {
    await api("/runs", { method: "POST" });
    toast("Прогон запущен вручную", "acc");
    return true;
  } catch (e) {
    if (e.status === 409) toast("Прогон уже идёт", "yellow");
    else if (e.status === 429) toast(`Слишком рано: ручной запуск можно через ${mmss(Number(e.headers && e.headers.get("Retry-After")) || 0)}`, "yellow");
    else if (e.status === 503) toast("Планировщик выключен, ручной запуск недоступен", "red");
    else toast("Сервис не ответил, прогон не запущен", "red");
    return false;
  } finally {
    await refreshStatus();
  }
}

// остановка текущего прогона: кнопка мониторинга и "Остановить" в боковом меню
export async function stopRun() {
  try {
    await api("/runs/stop", { method: "POST" });
    toast("Прогон останавливается: текущий шаг доделается, прогон будет помечен прерванным", "yellow");
    return true;
  } catch (e) {
    if (e.status === 409) toast("Прогон сейчас не идёт", "yellow");
    else toast("Сервис не ответил, прогон не остановлен", "red");
    return false;
  } finally {
    await refreshStatus();
  }
}

export function useStatus() {
  const [, force] = useState(0);
  useEffect(() => {
    connect();
    const fn = () => force((n) => n + 1);
    subscribers.add(fn);
    return () => subscribers.delete(fn);
  }, []);
  return status;
}

// Маршруты: #/catalog?q=...&platform=..., #/game/<slug>, #/monitor. Параметры каталога живут в адресе,
// поэтому "назад" из карточки возвращает тот же поиск и фильтр, а ссылкой можно поделиться.
export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const [screen, slug] = path.split("/");
  const params = Object.fromEntries(new URLSearchParams(query));
  if (screen === "game" && slug) return { screen: "game", slug: decodeURIComponent(slug), params };
  if (screen === "monitor") return { screen: "monitor", params };
  return { screen: "catalog", params };
}

export function useRoute() {
  const [route, setRoute] = useState(parseHash);
  useEffect(() => {
    const onChange = () => {
      const next = parseHash();
      setRoute((prev) => {
        // смена фильтров каталога не должна прокручивать страницу наверх
        if (prev.screen !== next.screen || prev.slug !== next.slug) window.scrollTo(0, 0);
        return next;
      });
    };
    addEventListener("hashchange", onChange);
    return () => removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

// replace: без новой записи в истории (поиск, фильтры), иначе "назад" листал бы каждую букву
export function navigate(hash, { replace = false } = {}) {
  if (!replace) { location.hash = hash; return; }
  if (location.hash === hash) return;
  history.replaceState(null, "", hash);
  dispatchEvent(new HashChangeEvent("hashchange"));
}

// Поиск, фильтры и сортировка каталога держатся, пока их не сбросят: каталог запоминает свой адрес,
// и ссылки "в каталог" (меню, логотип, хлебные крошки карточки) ведут в него, в том числе после перезагрузки
const CATALOG_KEY = "skytec-parser-catalog";
export function rememberCatalog(hash) { store.set(CATALOG_KEY, hash); }

export const link = {
  catalog: (params) => {
    if (params === undefined) {
      const saved = store.get(CATALOG_KEY, "");
      return /^#\/catalog(\?|$)/.test(saved) ? saved : "#/catalog";
    }
    const query = new URLSearchParams(Object.entries(params || {}).filter(([, v]) => v !== "" && v != null)).toString();
    return "#/catalog" + (query ? "?" + query : "");
  },
  game: (slug) => "#/game/" + encodeURIComponent(slug),
  monitor: () => "#/monitor",
};
