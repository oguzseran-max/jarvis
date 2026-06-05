/**
 * Security HUD — live WatchGuard diagnostic window for Marion's screen.
 *
 * A self-contained, Hollywood-style threat panel (mid-left of the screen) that
 * polls /api/watchguard/stats and shows the alert counts, the current threat
 * level, and a live feed of the latest intrusion events. Cyan JARVIS palette,
 * with amber/red escalation and a red flash when a new critical alert lands.
 */

const C = "76, 168, 232"; // JARVIS cyan
const AMBER = "255, 176, 46";
const RED = "255, 64, 64";
const GREEN = "60, 220, 130";

interface WgEvent {
  ts: number; severity: string; kind: string; source: string; detail: string;
}
interface WgStats {
  active: boolean; total: number; critical: number; warning: number;
  info: number; recent: WgEvent[];
}

const CSS = `
#sec-hud {
  position: fixed; right: 22px; top: 22px;
  width: 252px; z-index: 4; pointer-events: none;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  color: rgba(${C}, 0.92);
  opacity: 0; transition: opacity 0.8s ease;
  filter: drop-shadow(0 0 14px rgba(${C}, 0.18));
}
#sec-hud.show { opacity: 1; }
.sec-frame {
  position: relative;
  background: linear-gradient(180deg, rgba(6,12,20,0.82), rgba(4,8,14,0.9));
  border: 1px solid rgba(${C}, 0.35);
  border-radius: 6px; padding: 12px 13px 13px;
  overflow: hidden;
}
/* animated scanline */
.sec-frame::after {
  content: ""; position: absolute; left: 0; right: 0; top: 0; height: 2px;
  background: linear-gradient(90deg, transparent, rgba(${C}, 0.55), transparent);
  animation: sec-scan 3.6s linear infinite; opacity: 0.6;
}
@keyframes sec-scan { 0% { top: 0; } 100% { top: 100%; } }
/* corner brackets */
.sec-frame > .br { position: absolute; width: 12px; height: 12px; border: 2px solid rgba(${C}, 0.7); }
.sec-frame > .br.tl { top: 4px; left: 4px; border-right: 0; border-bottom: 0; }
.sec-frame > .br.tr { top: 4px; right: 4px; border-left: 0; border-bottom: 0; }
.sec-frame > .br.bl { bottom: 4px; left: 4px; border-right: 0; border-top: 0; }
.sec-frame > .br.br2 { bottom: 4px; right: 4px; border-left: 0; border-top: 0; }

.sec-head { display: flex; align-items: center; gap: 7px; font-size: 10px;
  letter-spacing: 2.5px; text-transform: uppercase; color: rgba(${C}, 0.95);
  border-bottom: 1px solid rgba(${C}, 0.18); padding-bottom: 7px; }
.sec-dot { width: 7px; height: 7px; border-radius: 50%; background: rgba(${GREEN}, 1);
  box-shadow: 0 0 8px rgba(${GREEN}, 0.9); animation: sec-blink 1.8s ease-in-out infinite; }
.sec-dot.off { background: rgba(${RED},1); box-shadow: 0 0 8px rgba(${RED},0.9); }
@keyframes sec-blink { 50% { opacity: 0.35; } }
.sec-head .grow { flex: 1; }
.sec-head .live { font-size: 8px; letter-spacing: 1.5px; color: rgba(${C}, 0.6); }

.sec-level { margin: 11px 0 9px; text-align: center; }
.sec-level .lbl { font-size: 9px; letter-spacing: 2px; color: rgba(${C}, 0.55); }
.sec-level .val { font-size: 20px; font-weight: 700; letter-spacing: 3px;
  margin-top: 2px; text-transform: uppercase; transition: color 0.3s; }

.sec-counts { display: flex; gap: 6px; margin-bottom: 11px; }
.sec-cell { flex: 1; text-align: center; border: 1px solid rgba(${C}, 0.16);
  border-radius: 4px; padding: 6px 0; background: rgba(${C}, 0.04); }
.sec-cell .n { font-size: 18px; font-weight: 700; line-height: 1; }
.sec-cell .k { font-size: 7.5px; letter-spacing: 1.5px; margin-top: 3px;
  text-transform: uppercase; color: rgba(255,255,255,0.45); }
.sec-cell.crit .n { color: rgba(${RED}, 1); }
.sec-cell.warn .n { color: rgba(${AMBER}, 1); }
.sec-cell.tot .n { color: rgba(${C}, 1); }

.sec-feed-title { font-size: 8px; letter-spacing: 2px; color: rgba(${C}, 0.5);
  text-transform: uppercase; margin-bottom: 5px; }
.sec-feed { display: flex; flex-direction: column; gap: 4px; max-height: 168px; overflow: hidden; }
.sec-row { display: flex; gap: 6px; align-items: baseline; font-size: 9.5px; line-height: 1.25;
  animation: sec-in 0.4s ease; }
@keyframes sec-in { from { opacity: 0; transform: translateX(-6px); } to { opacity: 1; } }
.sec-row .t { color: rgba(255,255,255,0.4); font-size: 8.5px; white-space: nowrap; }
.sec-row .sd { width: 6px; height: 6px; border-radius: 50%; margin-top: 4px; flex: none; }
.sec-row .d { color: rgba(255,255,255,0.72); overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.sec-row.crit .sd { background: rgba(${RED},1); box-shadow: 0 0 6px rgba(${RED},0.8); }
.sec-row.warn .sd { background: rgba(${AMBER},1); box-shadow: 0 0 6px rgba(${AMBER},0.8); }
.sec-row.info .sd { background: rgba(${C},1); }
.sec-empty { font-size: 9px; color: rgba(${GREEN}, 0.7); letter-spacing: 1px; text-align: center; padding: 6px 0; }

/* red alarm flash on a new critical */
#sec-hud.alarm .sec-frame { animation: sec-alarm 1.1s ease 2; }
@keyframes sec-alarm {
  0%,100% { border-color: rgba(${C}, 0.35); box-shadow: none; }
  50% { border-color: rgba(${RED}, 0.9); box-shadow: 0 0 22px rgba(${RED}, 0.5); }
}
`;

export interface SecurityHud {
  reveal(): void;
  destroy(): void;
}

export function createSecurityHud(): SecurityHud {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "sec-hud";
  el.innerHTML = `
    <div class="sec-frame">
      <span class="br tl"></span><span class="br tr"></span>
      <span class="br bl"></span><span class="br br2"></span>
      <div class="sec-head">
        <span class="sec-dot" id="sec-dot"></span>
        <span>WatchGuard</span><span class="grow"></span><span class="live">LIVE</span>
      </div>
      <div class="sec-level">
        <div class="lbl">THREAT LEVEL</div>
        <div class="val" id="sec-level">—</div>
      </div>
      <div class="sec-counts">
        <div class="sec-cell crit"><div class="n" id="sec-crit">0</div><div class="k">Critical</div></div>
        <div class="sec-cell warn"><div class="n" id="sec-warn">0</div><div class="k">Warning</div></div>
        <div class="sec-cell tot"><div class="n" id="sec-tot">0</div><div class="k">Total</div></div>
      </div>
      <div class="sec-feed-title">// Live feed</div>
      <div class="sec-feed" id="sec-feed"></div>
    </div>`;
  document.body.appendChild(el);

  const $ = (id: string) => el.querySelector("#" + id) as HTMLElement;
  const dot = $("sec-dot"), levelEl = $("sec-level"), feed = $("sec-feed");
  const critEl = $("sec-crit"), warnEl = $("sec-warn"), totEl = $("sec-tot");

  let lastCritical = -1;
  let stopped = false;

  function fmtTime(ts: number): string {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function sevClass(s: string): string {
    const x = (s || "").toLowerCase();
    if (x === "critical") return "crit";
    if (x === "warning") return "warn";
    return "info";
  }

  function render(stats: WgStats) {
    dot.classList.toggle("off", !stats.active);
    critEl.textContent = String(stats.critical || 0);
    warnEl.textContent = String(stats.warning || 0);
    totEl.textContent = String(stats.total || 0);

    // Threat level
    let label = "SECURE", color = `rgba(${GREEN},1)`;
    if (!stats.active) { label = "OFFLINE"; color = `rgba(${RED},1)`; }
    else if (stats.critical > 0) { label = "CRITICAL"; color = `rgba(${RED},1)`; }
    else if (stats.warning > 0) { label = "ELEVATED"; color = `rgba(${AMBER},1)`; }
    levelEl.textContent = label;
    levelEl.style.color = color;

    // Red alarm flash when a NEW critical appears
    if (lastCritical >= 0 && (stats.critical || 0) > lastCritical) {
      el.classList.remove("alarm"); void el.offsetWidth; el.classList.add("alarm");
    }
    lastCritical = stats.critical || 0;

    // Feed
    const rows = stats.recent || [];
    if (!rows.length) {
      feed.innerHTML = `<div class="sec-empty">✓ NO THREATS DETECTED</div>`;
      return;
    }
    feed.innerHTML = rows.map((e) => {
      const c = sevClass(e.severity);
      const detail = (e.detail || "").replace(/</g, "&lt;");
      return `<div class="sec-row ${c}"><span class="t">${fmtTime(e.ts)}</span>`
        + `<span class="sd"></span><span class="d">${detail}</span></div>`;
    }).join("");
  }

  async function poll() {
    if (stopped) return;
    try {
      const r = await fetch("/api/watchguard/stats", { cache: "no-store" });
      if (r.ok) render(await r.json());
    } catch {
      dot.classList.add("off");
      levelEl.textContent = "OFFLINE";
      levelEl.style.color = `rgba(${RED},1)`;
    }
  }

  poll();
  const timer = window.setInterval(poll, 4000);

  return {
    reveal() { el.classList.add("show"); },
    destroy() { stopped = true; clearInterval(timer); el.remove(); style.remove(); },
  };
}
