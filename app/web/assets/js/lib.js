// Общие помощники интерфейса: разметка, числа, даты по часовому поясу сервиса, оценки, платформы,
// всплывающие уведомления и подсказки.
import { h } from "preact";
import htm from "htm";

export const html = htm.bind(h);
export const cx = (...names) => names.filter(Boolean).join(" ");

export const GREEN = "var(--green)";
export const YELLOW = "var(--yellow)";
export const RED = "var(--red)";
export const ACC = "var(--acc)";
export const TONES = { acc: ACC, green: GREEN, yellow: YELLOW, red: RED };

const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
let timeZone = "Europe/Moscow";
export function setTimeZone(zone) { if (zone) timeZone = zone; }
export function zoneLabel() { return timeZone === "Europe/Moscow" ? "МСК" : ""; }

// числа с неразрывным пробелом между разрядами: 9 949 667
export function fmt(n) { return n == null ? "" : String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " "); }
export function plural(n, one, few, many) {
  const m = Math.abs(n) % 100, k = m % 10;
  return m > 10 && m < 20 ? many : k === 1 ? one : k >= 2 && k <= 4 ? few : many;
}
export function count(n, one, few, many) { return fmt(n) + " " + plural(n, one, few, many); }

function parts(date) {
  const format = new Intl.DateTimeFormat("ru-RU", {
    timeZone, year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  const out = {};
  for (const p of format.formatToParts(date)) out[p.type] = p.value;
  return out;
}
const zone = () => (zoneLabel() ? " " + zoneLabel() : "");

// дата выхода игры приходит днём без времени: 2026-09-04 -> 4 сентября 2026
export function ruDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}
export function timeOf(iso, seconds = true) {
  if (!iso) return "—";
  const p = parts(new Date(iso));
  return seconds ? `${p.hour}:${p.minute}:${p.second}` : `${p.hour}:${p.minute}`;
}
export function stampOf(iso) {
  if (!iso) return "";
  const p = parts(new Date(iso));
  return `${Number(p.day)} ${MONTHS[Number(p.month) - 1]}, ${p.hour}:${p.minute}${zone()}`;
}
export function clockOf(date) {
  const p = parts(date);
  return `${Number(p.day)} ${MONTHS[Number(p.month) - 1]} ${p.year} · ${p.hour}:${p.minute}${zone()}`;
}
export function dayOf(iso) {
  if (!iso) return "";
  const p = parts(new Date(iso));
  return `${Number(p.day)} ${MONTHS[Number(p.month) - 1]}`;
}
export { zone as zoneSuffix };

// момент в поясе сервиса для расписания: "сегодня, 04:22 МСК", "завтра, 02:22 МСК", "16 сентября, 02:22 МСК"
export function whenText(iso, now = Date.now()) {
  if (!iso) return "—";
  const p = parts(new Date(iso));
  const same = (q) => q.year === p.year && q.month === p.month && q.day === p.day;
  const day = same(parts(new Date(now))) ? "сегодня" : same(parts(new Date(now + 86400000))) ? "завтра"
    : `${Number(p.day)} ${MONTHS[Number(p.month) - 1]}`;
  return `${day}, ${p.hour}:${p.minute}${zone()}`;
}
// сегодняшняя дата в поясе сервиса, ГГГГ-ММ-ДД: точка отсчёта расписания по умолчанию
export function todayIso() {
  const p = parts(new Date());
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

export function ago(iso, now = Date.now()) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 5) return "только что";
  if (s < 60) return `${s} с назад`;
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  return dayOf(iso);
}
export function until(iso, now = Date.now()) {
  if (!iso) return "";
  const s = Math.round((new Date(iso).getTime() - now) / 1000);
  if (s <= 0) return "вот-вот";
  if (s < 60) return `через ${s} с`;
  if (s < 3600) return `через ${Math.floor(s / 60)} мин ${s % 60} с`;
  const hours = Math.floor(s / 3600);
  const minutes = Math.round((s % 3600) / 60);
  if (hours < 24) return `через ${hours} ч${minutes ? " " + minutes + " мин" : ""}`;
  const days = Math.floor(hours / 24);
  return `через ${days} ${plural(days, "день", "дня", "дней")}${hours % 24 ? " " + (hours % 24) + " ч" : ""}`;
}
export function durationText(sec) {
  if (sec == null) return "—";
  const s = Math.round(sec);
  if (s < 60) return `${s} с`;
  if (s < 3600) return `${Math.floor(s / 60)} мин${s % 60 ? " " + (s % 60) + " с" : ""}`;
  return `${Math.floor(s / 3600)} ч ${Math.floor((s % 3600) / 60)} мин`;
}
const two = (n) => String(n).padStart(2, "0");
export function clockText(sec) {
  if (sec == null) return "";
  const s = Math.round(sec), hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60);
  return hh ? `${hh}:${two(mm)}:${two(s % 60)}` : `${mm}:${two(s % 60)}`;
}
export function mmss(sec) { const s = Math.max(0, Math.ceil(sec)); return `${Math.floor(s / 60)}:${two(s % 60)}`; }

// шкала Metacritic: критики 75/50, игроки 7.5/5.0
export function metaColor(v) { return v == null ? null : v >= 75 ? GREEN : v >= 50 ? YELLOW : RED; }
export function userColor(v) { return v == null ? null : v >= 7.5 ? GREEN : v >= 5 ? YELLOW : RED; }
export function scoreText(v, isUser) { return v == null ? "—" : isUser ? Number(v).toFixed(1) : String(v); }
// значок оценки для всех экранов: цвет по шкале, свечение у выдающихся (критики от 90, игроки от 9.0)
export function ScoreBadge({ value, isUser, size = "md", hint, empty = "—" }) {
  const color = isUser ? userColor(value) : metaColor(value);
  const glow = value != null && (isUser ? value >= 9 : value >= 90);
  return html`<span class=${cx("badge", "badge-" + size, !color && "is-none", glow && "is-glow")}
    style=${color ? { background: color } : null} ...${tip(hint)} tabindex=${hint ? 0 : undefined}>
    ${value == null ? empty : scoreText(value, isUser)}</span>`;
}

const MARKS = {
  "PC": "PC", "PlayStation 5": "PS5", "PlayStation 4": "PS4", "Xbox Series X": "XSX", "Xbox Series S": "XSS",
  "Xbox One": "XB1", "Nintendo Switch 2": "NS2", "Nintendo Switch": "NSW", "iOS (iPhone/iPad)": "iOS",
};
export function platMark(name) { return MARKS[name] || String(name || "").replace(/[^A-Za-z0-9]/g, "").slice(0, 3).toUpperCase(); }
export function platStyle(name) {
  const n = String(name || "");
  const strong = /(5|Series X|Switch 2)$/.test(n);
  if (n === "PC") return { background: "rgba(146,161,186,.16)", color: "var(--plat-pc)" };
  if (n.startsWith("PlayStation")) return { background: `rgba(0,112,210,${strong ? .20 : .16})`, color: "var(--plat-ps)" };
  if (n.startsWith("Xbox")) return { background: `rgba(16,168,81,${strong ? .18 : .14})`, color: "var(--plat-xb)" };
  if (n.startsWith("Nintendo")) return { background: `rgba(230,60,60,${strong ? .18 : .14})`, color: "var(--plat-ns)" };
  return { background: "var(--veil-07)", color: "var(--plat-def)" };
}

// уведомления и подсказки живут вне дерева компонентов: вызвать можно из любого обработчика
const listeners = new Set();
export const overlay = { toast: null, tip: null, cue: null };
export function onOverlay(fn) { listeners.add(fn); return () => listeners.delete(fn); }
const emit = () => listeners.forEach((fn) => fn());
let toastTimer = null;
export function toast(text, tone = "acc") {
  overlay.toast = { text, color: TONES[tone] || tone };
  emit();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { overlay.toast = null; emit(); }, 4200);
}
function showTip(text, el) {
  const r = el.getBoundingClientRect();
  // у верхнего края экрана подсказке нет места сверху: встаёт под элементом
  const below = r.top < 96;
  overlay.tip = { text, x: Math.round(r.left + r.width / 2), y: Math.round(below ? r.bottom : r.top), below };
  emit();
}
export function hideTip() { if (overlay.tip) { overlay.tip = null; emit(); } }
// золотая звезда в правом верхнем углу обложки: игра прошла все этапы, что для неё возможны
export function CompleteStar() {
  return html`<span class="gcover-star" ...${tip("Полный анализ: обложка, описание, видео, резюме отзывов и летсплей")}>
    <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M21.25 18.47L19.6 18.86C19.23 18.95 18.94 19.23 18.86 19.6L18.51 21.07C18.32 21.87 17.3 22.12 16.77 21.49L13.78 18.05C13.54 17.77 13.67 17.33 14.03 17.24C15.8 16.81 17.39 15.82 18.56 14.41C18.75 14.18 19.09 14.15 19.3 14.36L21.52 16.58C22.28 17.34 22.01 18.29 21.25 18.47Z" fill="currentColor"/>
      <path d="M2.7 18.47L4.35 18.86C4.72 18.95 5.01 19.23 5.09 19.6L5.44 21.07C5.63 21.87 6.65 22.12 7.18 21.49L10.17 18.05C10.41 17.77 10.28 17.33 9.92 17.24C8.15 16.81 6.56 15.82 5.39 14.41C5.2 14.18 4.86 14.15 4.65 14.36L2.43 16.58C1.67 17.34 1.94 18.29 2.7 18.47Z" fill="currentColor"/>
      <path d="M12 2C8.13 2 5 5.13 5 9C5 10.45 5.43 11.78 6.17 12.89C7.25 14.49 8.96 15.62 10.95 15.91C11.29 15.97 11.64 16 12 16C12.36 16 12.71 15.97 13.05 15.91C15.04 15.62 16.75 14.49 17.83 12.89C18.57 11.78 19 10.45 19 9C19 5.13 15.87 2 12 2ZM15.06 8.78L14.23 9.61C14.09 9.75 14.01 10.02 14.06 10.22L14.3 11.25C14.49 12.06 14.06 12.38 13.34 11.95L12.34 11.36C12.16 11.25 11.86 11.25 11.68 11.36L10.68 11.95C9.96 12.37 9.53 12.06 9.72 11.25L9.96 10.22C10 10.03 9.93 9.75 9.79 9.61L8.94 8.78C8.45 8.29 8.61 7.8 9.29 7.69L10.36 7.51C10.54 7.48 10.75 7.32 10.83 7.16L11.42 5.98C11.74 5.34 12.26 5.34 12.58 5.98L13.17 7.16C13.25 7.32 13.46 7.48 13.65 7.51L14.72 7.69C15.39 7.8 15.55 8.29 15.06 8.78Z" fill="currentColor"/>
    </svg>
  </span>`;
}
// магазины, откуда берутся обложка и трейлер, когда их нет на Metacritic (cover_source, video_source)
export const STORES = { steam: "Steam", gplay: "Google Play" };
// подсказка по наведению и по фокусу с клавиатуры
export function tip(text) {
  if (!text) return {};
  return {
    onMouseEnter: (e) => showTip(text, e.currentTarget), onMouseLeave: hideTip,
    onFocus: (e) => showTip(text, e.currentTarget), onBlur: hideTip,
  };
}

// Всплывающая карточка пункта: моменты ролика или отзывы-источники. Открывается по наведению
// и фокусу, на сенсорном экране по нажатию; наведение на саму карточку не даёт ей закрыться.
let cueTimer = null;
let lastPointer = "mouse";
addEventListener("pointerdown", (e) => { lastPointer = e.pointerType; }, true);
export const touchInput = () => lastPointer !== "mouse";

const CUE_WIDTH = 378;
export function cueShow(cue, el) {
  clearTimeout(cueTimer);
  // строка пункта тянется на всю ширину блока: карточку ставим у конца самого текста (с числом
  // источников), а не у края блока
  const tail = el.querySelector && (el.querySelector(".cue-count") || el.querySelector(".cue-text"));
  const box = el.getBoundingClientRect();
  const end = tail ? tail.getBoundingClientRect() : box;
  // по вертикали карточка встаёт напротив последней строки текста, где стоит число источников
  const r = { top: end.top, bottom: box.bottom, height: end.height, left: box.left, right: Math.min(box.right, end.right) };
  let place;
  if (innerWidth < 640) {
    // на узком экране карточка встаёт под пунктом во всю ширину
    place = { left: 12, width: innerWidth - 24, top: Math.min(r.bottom + 8, innerHeight - 240), middle: false };
  } else {
    const gap = 14;
    const right = r.right + gap + CUE_WIDTH <= innerWidth - 12;
    place = {
      left: Math.round(right ? r.right + gap : Math.max(12, r.left - gap - CUE_WIDTH)), width: CUE_WIDTH,
      top: Math.round(Math.min(Math.max(r.top + r.height / 2, 120), innerHeight - 120)), middle: true,
    };
  }
  overlay.cue = { ...cue, ...place, owner: el };
  emit();
}
export function cueHide(delay = 160) {
  clearTimeout(cueTimer);
  cueTimer = setTimeout(() => { if (overlay.cue) { overlay.cue = null; emit(); } }, delay);
}
export function cueHold() { clearTimeout(cueTimer); }
export function cueToggle(cue, el) {
  if (overlay.cue && overlay.cue.owner === el) { overlay.cue = null; emit(); } else cueShow(cue, el);
}

export const store = {
  get(key, fallback = null) { try { const v = localStorage.getItem(key); return v == null ? fallback : v; } catch (e) { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* приватный режим: просто не запоминаем */ } },
};
