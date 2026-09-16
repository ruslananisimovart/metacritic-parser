// Точка входа: маршрут, поток статуса, каркас и текущий экран.
import { render } from "preact";
import { useRoute, useStatus } from "./api.js";
import { Catalog } from "./catalog.js";
import { GameScreen } from "./game.js";
import { Monitor } from "./monitor.js";
import { html } from "./lib.js";
import { Overlays, Sidebar } from "./shell.js";

function App() {
  const route = useRoute();
  const status = useStatus();
  let screen;
  if (route.screen === "game") screen = html`<${GameScreen} route=${route} status=${status} />`;
  else if (route.screen === "monitor") screen = html`<${Monitor} status=${status} />`;
  else screen = html`<${Catalog} route=${route} status=${status} />`;
  return html`<div class="layout">
    <${Sidebar} route=${route} status=${status} />
    <main class="main">${screen}</main>
    <${Overlays} />
  </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
