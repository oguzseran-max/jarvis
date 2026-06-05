/**
 * Build HUD — cinematic ("Hollywood") progress panel for background builds.
 *
 * `claude -p` builds are opaque (no real % is reported), so the bar shows a
 * TIME-ESTIMATED progress easing toward ~95% then snapping to 100% on
 * completion. The phase labels (INITIALISATION → GÉNÉRATION → …) are cosmetic —
 * they make it feel alive/pro, they are not real build stages. Status + elapsed
 * time + success/failure ARE real.
 */

export interface BuildHud {
  spawned(taskId: string, label: string): void;
  completed(taskId: string, status: string, summary?: string): void;
}

const C = "76, 168, 232";        // JARVIS cyan
const MONO = "'SF Mono', ui-monospace, 'JetBrains Mono', Menlo, monospace";

const CSS = `
#build-panel {
  position: fixed; right: 24px; top: 50%; transform: translateY(-50%);
  z-index: 5; display: flex; flex-direction: column; gap: 14px;
  width: 300px; pointer-events: none;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
.build-card {
  position: relative; padding: 14px 16px 15px;
  background:
    linear-gradient(180deg, rgba(10,16,26,0.82), rgba(6,10,18,0.9));
  border: 1px solid rgba(${C}, 0.3);
  box-shadow: 0 0 24px rgba(${C}, 0.12), inset 0 0 18px rgba(${C}, 0.06),
              0 10px 30px rgba(0,0,0,0.55);
  backdrop-filter: blur(10px);
  color: #d7ecff; overflow: hidden;
  clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 14px 100%, 0 calc(100% - 14px));
  opacity: 0; transform: translateX(22px) scale(0.97);
  animation: bc-flicker 0.55s steps(1) forwards;
}
@keyframes bc-flicker {
  0%   { opacity: 0; transform: translateX(22px) scale(0.97); }
  30%  { opacity: 0.4; } 35% { opacity: 0.1; } 45% { opacity: 0.7; }
  55%  { opacity: 0.2; } 70% { opacity: 1; transform: translateX(0) scale(1); }
  100% { opacity: 1; transform: translateX(0) scale(1); }
}
.build-card.out { animation: bc-out 0.4s ease forwards; }
@keyframes bc-out { to { opacity: 0; transform: translateX(22px) scale(0.97); } }

/* corner brackets */
.build-card .corner { position: absolute; width: 11px; height: 11px; pointer-events: none; }
.build-card .corner.tl { top: 5px; left: 5px; border-top: 2px solid rgba(${C},0.9); border-left: 2px solid rgba(${C},0.9); }
.build-card .corner.bl { bottom: 5px; left: 5px; border-bottom: 2px solid rgba(${C},0.9); border-left: 2px solid rgba(${C},0.9); }
.build-card .corner.br { bottom: 5px; right: 5px; border-bottom: 2px solid rgba(${C},0.9); border-right: 2px solid rgba(${C},0.9); }

/* sweeping scan line */
.build-card .scan {
  position: absolute; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, rgba(${C},0.85), transparent);
  filter: blur(0.5px); animation: bc-scan 2.6s linear infinite; opacity: 0.7;
}
@keyframes bc-scan { 0% { top: -2px; } 100% { top: 100%; } }
.build-card.done .scan, .build-card.failed .scan { display: none; }

.bc-head { display: flex; align-items: center; gap: 7px; margin-bottom: 9px; }
.bc-dot { width: 7px; height: 7px; border-radius: 50%; background: rgba(${C},1);
  box-shadow: 0 0 8px rgba(${C},0.9); animation: bc-pulse 1.1s ease-in-out infinite; }
@keyframes bc-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.25; } }
.build-card.done .bc-dot { background: #22c55e; box-shadow: 0 0 8px rgba(34,197,94,0.9); animation: none; }
.build-card.failed .bc-dot { background: #ef4444; box-shadow: 0 0 8px rgba(239,68,68,0.9); animation: none; }
.bc-label {
  font-size: 11px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase;
  color: #eaf6ff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;
}

.bc-meta { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 8px; }
.bc-phase {
  font-family: ${MONO}; font-size: 9.5px; letter-spacing: 1.5px; text-transform: uppercase;
  color: rgba(${C},0.95);
}
.bc-phase::after { content: '▮'; margin-left: 2px; animation: bc-blink 1s steps(1) infinite; }
@keyframes bc-blink { 50% { opacity: 0; } }
.build-card.done .bc-phase::after, .build-card.failed .bc-phase::after { display: none; }
.bc-pct {
  font-family: ${MONO}; font-size: 22px; font-weight: 700; line-height: 1;
  color: #eaf6ff; text-shadow: 0 0 12px rgba(${C},0.8);
}

.bc-bar {
  position: relative; height: 7px; border-radius: 2px;
  background: rgba(255,255,255,0.06); overflow: hidden;
  border: 1px solid rgba(${C},0.15);
}
.bc-fill {
  position: relative; height: 100%; width: 0%;
  background: linear-gradient(90deg, rgba(${C},0.45) 0%, rgba(140,210,255,1) 100%);
  background-size: 200% 100%; animation: bc-shimmer 1.6s linear infinite;
  box-shadow: 0 0 14px rgba(${C},0.9); transition: width 0.5s ease, background 0.3s ease;
}
@keyframes bc-shimmer { 0% { background-position: 100% 0; } 100% { background-position: -100% 0; } }
.build-card.done .bc-fill { background: linear-gradient(90deg,#16a34a,#22c55e); animation: none; box-shadow: 0 0 14px rgba(34,197,94,0.7); }
.build-card.failed .bc-fill { background: linear-gradient(90deg,#b91c1c,#ef4444); animation: none; box-shadow: 0 0 14px rgba(239,68,68,0.7); }
/* ticks overlay on the bar for a segmented, techy look */
.bc-bar::after {
  content: ''; position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(90deg, transparent 0 13px, rgba(0,0,0,0.35) 13px 14px);
}
.bc-foot { margin-top: 7px; font-family: ${MONO}; font-size: 9px; letter-spacing: 1px;
  color: rgba(${C},0.6); text-transform: uppercase; }
`;

interface Card {
  root: HTMLDivElement;
  fill: HTMLDivElement;
  phase: HTMLSpanElement;
  pct: HTMLSpanElement;
  foot: HTMLSpanElement;
  start: number;
  done: boolean;
}

// Cosmetic phase labels by progress — not real build stages (the build is opaque).
function phaseLabel(pct: number): string {
  if (pct < 18) return "initialisation";
  if (pct < 42) return "génération du code";
  if (pct < 66) return "assemblage";
  if (pct < 88) return "compilation";
  return "vérification";
}

export function createBuildHud(): BuildHud {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const panel = document.createElement("div");
  panel.id = "build-panel";
  document.body.appendChild(panel);

  const cards = new Map<string, Card>();
  const estimate = (s: number) => 95 * (1 - Math.exp(-s / 60));

  setInterval(() => {
    const now = performance.now();
    for (const c of cards.values()) {
      if (c.done) continue;
      const elapsed = (now - c.start) / 1000;
      const pct = estimate(elapsed);
      c.fill.style.width = `${pct.toFixed(0)}%`;
      c.pct.textContent = `${pct.toFixed(0)}%`;
      c.phase.textContent = phaseLabel(pct);
      c.foot.textContent = `temps écoulé · ${elapsed.toFixed(0)}s`;
    }
  }, 400);

  return {
    spawned(taskId: string, label: string) {
      if (cards.has(taskId)) return;
      const root = document.createElement("div");
      root.className = "build-card";
      root.innerHTML =
        `<span class="corner tl"></span><span class="corner bl"></span><span class="corner br"></span>` +
        `<div class="scan"></div>` +
        `<div class="bc-head"><span class="bc-dot"></span><span class="bc-label"></span></div>` +
        `<div class="bc-meta"><span class="bc-phase">initialisation</span><span class="bc-pct">0%</span></div>` +
        `<div class="bc-bar"><div class="bc-fill"></div></div>` +
        `<div class="bc-foot">temps écoulé · 0s</div>`;
      (root.querySelector(".bc-label") as HTMLElement).textContent = label || "Construction";
      panel.appendChild(root);
      cards.set(taskId, {
        root,
        fill: root.querySelector(".bc-fill") as HTMLDivElement,
        phase: root.querySelector(".bc-phase") as HTMLSpanElement,
        pct: root.querySelector(".bc-pct") as HTMLSpanElement,
        foot: root.querySelector(".bc-foot") as HTMLSpanElement,
        start: performance.now(),
        done: false,
      });
    },
    completed(taskId: string, status: string, summary?: string) {
      const c = cards.get(taskId);
      if (!c) return;
      c.done = true;
      const ok = status === "completed" || status === "success" || status === "done";
      c.root.classList.add(ok ? "done" : "failed");
      c.fill.style.width = "100%";
      c.pct.textContent = "100%";
      c.phase.textContent = ok ? "terminé" : status;
      c.foot.textContent = ok ? "✓ build complete" : `✗ ${status}`;
      if (summary) (c.root.querySelector(".bc-label") as HTMLElement).title = summary;
      setTimeout(() => {
        c.root.classList.add("out");
        setTimeout(() => { c.root.remove(); cards.delete(taskId); }, 450);
      }, 7000);
    },
  };
}
