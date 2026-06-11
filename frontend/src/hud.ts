/**
 * JARVIS — Persistent corner HUD.
 *
 * Four always-on monitoring panels (one per screen corner) that come alive
 * after the boot hands off to the orb — Iron-Man-style ambience: a system
 * status board, a rotating "scan in progress" ring, a streaming diagnostics
 * feed and a live telemetry/clock readout. Reactive to JARVIS's state
 * (the scan spins faster while thinking, the status board pulses while
 * speaking). Pure-CSS animation; tiny JS only for the live text.
 */

import type { OrbState } from "./orb";

export interface CornerHud {
  /** Fade the panels in (call at the boot → orb handoff). */
  reveal(): void;
  /** React to JARVIS's state. */
  setState(s: OrbState): void;
}

const SVGNS = "http://www.w3.org/2000/svg";
const rnd = (a: number, b: number) => a + Math.random() * (b - a);
const hex = (n = 4) => Math.floor(Math.random() * 16 ** n).toString(16).toUpperCase().padStart(n, "0");

function el(tag: string, cls?: string, html?: string): HTMLElement {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}

export function createCornerHud(root: HTMLElement = document.body): CornerHud {
  const wrap = el("div", "hud-corners");

  // ── Top-left: SYSTEM STATUS ───────────────────────────────────────────────
  const tl = el("div", "hud-corner tl");
  tl.appendChild(el("div", "hud-head", "<span>SYSTEM STATUS</span><i class='hud-spark'></i>"));
  const rows = ["CORE", "NEURAL NET", "MEMORY", "UPLINK", "SENSORS"];
  const tlBody = el("div", "hud-rows");
  const valEls: HTMLElement[] = [];
  for (const r of rows) {
    const row = el("div", "hud-row");
    row.appendChild(el("span", "hud-dot"));
    row.appendChild(el("span", "hud-k", r));
    const v = el("span", "hud-v", "100%");
    valEls.push(v);
    row.appendChild(v);
    tlBody.appendChild(row);
  }
  tl.appendChild(tlBody);

  // ── Top-right: SCAN IN PROGRESS (rotating ring + sweeping %) ───────────────
  const tr = el("div", "hud-corner tr");
  tr.appendChild(el("div", "hud-head", "<i class='hud-spark'></i><span>SCAN IN PROGRESS</span>"));
  tr.appendChild(el("div", "hud-ringwrap", `
    <svg class="hud-ring" viewBox="0 0 100 100">
      <circle class="hud-ring-bg" cx="50" cy="50" r="42"/>
      <g class="hud-ring-spin">
        <circle class="hud-ring-arc" cx="50" cy="50" r="42"/>
        <circle class="hud-ring-arc2" cx="50" cy="50" r="34"/>
      </g>
      <g class="hud-ring-ticks"></g>
    </svg>
    <div class="hud-ring-pct">0%</div>`));
  // radial ticks
  const ticksG = tr.querySelector(".hud-ring-ticks")!;
  for (let i = 0; i < 24; i++) {
    const a = (i / 24) * Math.PI * 2;
    const ln = document.createElementNS(SVGNS, "line");
    ln.setAttribute("x1", String(50 + Math.cos(a) * 46));
    ln.setAttribute("y1", String(50 + Math.sin(a) * 46));
    ln.setAttribute("x2", String(50 + Math.cos(a) * (i % 6 === 0 ? 40 : 43)));
    ln.setAttribute("y2", String(50 + Math.sin(a) * (i % 6 === 0 ? 40 : 43)));
    ticksG.appendChild(ln);
  }
  const pctEl = tr.querySelector(".hud-ring-pct") as HTMLElement;
  const scanBar = el("div", "hud-bar", "<i></i>");
  tr.appendChild(scanBar);
  const scanFill = scanBar.querySelector("i") as HTMLElement;

  // ── Bottom-left: DIAGNOSTICS feed ─────────────────────────────────────────
  const bl = el("div", "hud-corner bl");
  bl.appendChild(el("div", "hud-head", "<span>DIAGNOSTICS</span><i class='hud-spark'></i>"));
  const feed = el("div", "hud-feed");
  bl.appendChild(feed);
  const LABELS = ["SUBSYSTEM", "MEM SECTOR", "I/O BUS", "VISION", "AUDIO DSP",
    "NAV CORE", "POWER CELL", "HEURISTIC", "KERNEL", "SENSOR", "LINK", "CACHE"];

  // ── Bottom-right: TELEMETRY + clock + reticle ─────────────────────────────
  const br = el("div", "hud-corner br");
  br.appendChild(el("div", "hud-head", "<i class='hud-spark'></i><span>TELEMETRY</span>"));
  br.appendChild(el("div", "hud-telemetry", `
    <svg class="hud-reticle" viewBox="0 0 100 100">
      <circle cx="50" cy="50" r="30" class="hud-ret-c"/>
      <g class="hud-ret-spin">
        <path class="hud-ret-arc" d="M50 8 A42 42 0 0 1 92 50"/>
        <path class="hud-ret-arc" d="M50 92 A42 42 0 0 1 8 50"/>
      </g>
      <line x1="50" y1="2" x2="50" y2="14" class="hud-ret-x"/>
      <line x1="50" y1="86" x2="50" y2="98" class="hud-ret-x"/>
      <line x1="2" y1="50" x2="14" y2="50" class="hud-ret-x"/>
      <line x1="86" y1="50" x2="98" y2="50" class="hud-ret-x"/>
    </svg>
    <div class="hud-tele-text">
      <div class="hud-clock">--:--:--</div>
      <div class="hud-line"><span>UPTIME</span><b class="hud-up">00:00:00</b></div>
      <div class="hud-line"><span>LAT</span><b class="hud-lat">--</b></div>
      <div class="hud-line"><span>LON</span><b class="hud-lon">--</b></div>
    </div>`));
  const clockEl = br.querySelector(".hud-clock") as HTMLElement;
  const upEl = br.querySelector(".hud-up") as HTMLElement;
  br.querySelector(".hud-lat")!.textContent = rnd(40, 49).toFixed(4) + "°N";
  br.querySelector(".hud-lon")!.textContent = rnd(-5, 9).toFixed(4) + "°E";

  wrap.append(tl, tr, bl, br);
  root.appendChild(wrap);

  // ── live updates (cheap, interval-driven) ─────────────────────────────────
  const t0 = Date.now();
  let scanPct = 0;
  const timers: number[] = [];

  const clamp = (n: number) => (n < 0 ? 0 : n > 100 ? 100 : n);
  function pad(n: number) { return String(n).padStart(2, "0"); }

  // clock + uptime, 1s
  timers.push(window.setInterval(() => {
    const now = new Date();
    clockEl.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
    const s = Math.floor((Date.now() - t0) / 1000);
    upEl.textContent = `${pad((s / 3600) | 0)}:${pad(((s % 3600) / 60) | 0)}:${pad(s % 60)}`;
  }, 1000));

  // status values jitter, 1.6s
  timers.push(window.setInterval(() => {
    for (const v of valEls) v.textContent = `${Math.round(rnd(94, 100))}%`;
  }, 1600));

  // scan % sweep, 90ms (loops)
  timers.push(window.setInterval(() => {
    scanPct = scanPct >= 100 ? 0 : clamp(scanPct + rnd(0.6, 2.4));
    pctEl.textContent = `${Math.round(scanPct)}%`;
    scanFill.style.width = `${scanPct}%`;
  }, 90));

  // diagnostics feed line, ~0.8s
  let feedRate = 800;
  function pushFeed() {
    const ok = Math.random() > 0.07;
    const line = el("div", "hud-feedline",
      `<span>${LABELS[(Math.random() * LABELS.length) | 0]} 0x${hex()}</span>` +
      `<em class="${ok ? "ok" : "wait"}">${ok ? "OK" : "···"}</em>`);
    feed.appendChild(line);
    while (feed.childElementCount > 6) feed.firstChild!.remove();
  }
  function scheduleFeed() {
    timers.push(window.setTimeout(() => { pushFeed(); scheduleFeed(); }, feedRate));
  }
  for (let i = 0; i < 4; i++) pushFeed();
  scheduleFeed();

  return {
    reveal() { wrap.classList.add("live"); },
    setState(s: OrbState) {
      wrap.classList.remove("state-thinking", "state-speaking", "state-listening", "state-idle");
      wrap.classList.add(`state-${s}`);
      feedRate = s === "thinking" ? 280 : s === "speaking" ? 600 : 800;
    },
  };
}
