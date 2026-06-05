/**
 * Self-Evolution HUD — live self-improvement dashboard on Marion's screen.
 *
 * The mirror of the WatchGuard panel: where security (mid-left) is about threats
 * (red/amber), this one (mid-right) is about growth (green/cyan). It polls
 * /api/selfeval/stats and shows how many adjustments Marion auto-applied in the
 * last 24h, broken down by kind, her current tuning state, and a live feed of
 * what she just learned.
 */

const C = "76, 168, 232";      // JARVIS cyan
const GREEN = "60, 220, 130";  // growth
const AMBER = "255, 176, 46";  // "to correct" signal

interface SeChange { kind: string; detail: string; ts: number; }
interface SeStats {
  window_h: number; total: number; by_kind: Record<string, number>;
  recent: SeChange[]; vocab_total: number; prefs_total: number; max_tokens: number;
}

const CSS = `
#evo-hud {
  position: fixed; right: 22px; top: 50%; transform: translateY(-50%);
  width: 270px; z-index: 4; pointer-events: none;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  color: rgba(${C}, 0.92);
  opacity: 0; transition: opacity 0.8s ease;
  filter: drop-shadow(0 0 14px rgba(${GREEN}, 0.14));
}
#evo-hud.show { opacity: 1; }
.evo-frame {
  position: relative;
  background: linear-gradient(180deg, rgba(6,16,14,0.82), rgba(4,10,10,0.9));
  border: 1px solid rgba(${GREEN}, 0.3);
  border-radius: 6px; padding: 12px 13px 13px; overflow: hidden;
}
.evo-frame::after {
  content: ""; position: absolute; left: 0; right: 0; top: 0; height: 2px;
  background: linear-gradient(90deg, transparent, rgba(${GREEN}, 0.5), transparent);
  animation: evo-scan 4.2s linear infinite; opacity: 0.55;
}
@keyframes evo-scan { 0% { top: 0; } 100% { top: 100%; } }
.evo-frame > .br { position: absolute; width: 12px; height: 12px; border: 2px solid rgba(${GREEN}, 0.65); }
.evo-frame > .br.tl { top: 4px; left: 4px; border-right: 0; border-bottom: 0; }
.evo-frame > .br.tr { top: 4px; right: 4px; border-left: 0; border-bottom: 0; }
.evo-frame > .br.bl { bottom: 4px; left: 4px; border-right: 0; border-top: 0; }
.evo-frame > .br.br2 { bottom: 4px; right: 4px; border-left: 0; border-top: 0; }

.evo-head { display: flex; align-items: center; gap: 7px; font-size: 10px;
  letter-spacing: 2.2px; text-transform: uppercase; color: rgba(${GREEN}, 0.95);
  border-bottom: 1px solid rgba(${GREEN}, 0.16); padding-bottom: 7px; }
.evo-dot { width: 7px; height: 7px; border-radius: 50%; background: rgba(${GREEN}, 1);
  box-shadow: 0 0 8px rgba(${GREEN}, 0.9); animation: evo-blink 1.8s ease-in-out infinite; }
.evo-dot.off { background: rgba(150,150,150,1); box-shadow: none; }
@keyframes evo-blink { 50% { opacity: 0.35; } }
.evo-head .grow { flex: 1; }
.evo-head .live { font-size: 8px; letter-spacing: 1.5px; color: rgba(${GREEN}, 0.6); }

.evo-big { text-align: center; margin: 11px 0 9px; }
.evo-big .n { font-size: 30px; font-weight: 700; line-height: 1; color: rgba(${GREEN}, 1);
  text-shadow: 0 0 16px rgba(${GREEN}, 0.4); transition: transform 0.25s; }
.evo-big .lbl { font-size: 8px; letter-spacing: 2px; color: rgba(${C}, 0.55);
  text-transform: uppercase; margin-top: 4px; }

.evo-counts { display: flex; gap: 5px; margin-bottom: 10px; }
.evo-cell { flex: 1; text-align: center; border: 1px solid rgba(${C}, 0.14);
  border-radius: 4px; padding: 5px 0; background: rgba(${C}, 0.04); }
.evo-cell .n { font-size: 15px; font-weight: 700; line-height: 1; color: rgba(${GREEN}, 1); }
.evo-cell.flag .n { color: rgba(${AMBER}, 1); }
.evo-cell .k { font-size: 7px; letter-spacing: 0.8px; margin-top: 3px;
  text-transform: uppercase; color: rgba(255,255,255,0.42); }

.evo-state { font-size: 8.5px; letter-spacing: 0.5px; color: rgba(${C}, 0.7);
  text-align: center; margin-bottom: 10px; padding: 5px 0;
  border-top: 1px solid rgba(${C}, 0.1); border-bottom: 1px solid rgba(${C}, 0.1); }
.evo-state b { color: rgba(${GREEN}, 0.95); font-weight: 700; }

.evo-feed-title { font-size: 8px; letter-spacing: 2px; color: rgba(${GREEN}, 0.5);
  text-transform: uppercase; margin-bottom: 5px; }
.evo-feed { display: flex; flex-direction: column; gap: 4px; max-height: 150px; overflow: hidden; }
.evo-row { display: flex; gap: 6px; align-items: baseline; font-size: 9.5px; line-height: 1.25;
  animation: evo-in 0.4s ease; }
@keyframes evo-in { from { opacity: 0; transform: translateX(6px); } to { opacity: 1; } }
.evo-row .t { color: rgba(255,255,255,0.4); font-size: 8.5px; white-space: nowrap; }
.evo-row .sd { width: 6px; height: 6px; border-radius: 50%; margin-top: 4px; flex: none;
  background: rgba(${GREEN},1); box-shadow: 0 0 6px rgba(${GREEN},0.7); }
.evo-row.flag .sd { background: rgba(${AMBER},1); box-shadow: 0 0 6px rgba(${AMBER},0.8); }
.evo-row .d { color: rgba(255,255,255,0.72); overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.evo-empty { font-size: 9px; color: rgba(${C}, 0.5); letter-spacing: 1px; text-align: center; padding: 6px 0; }
`;

const KIND_LABEL: Record<string, string> = {
  vocab: "MOTS", preference: "PRÉFS", brevity: "VITESSE", frustration: "À CORR.",
};

export interface LearningHud { reveal(): void; destroy(): void; }

export function createLearningHud(): LearningHud {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "evo-hud";
  el.innerHTML = `
    <div class="evo-frame">
      <span class="br tl"></span><span class="br tr"></span>
      <span class="br bl"></span><span class="br br2"></span>
      <div class="evo-head">
        <span class="evo-dot" id="evo-dot"></span>
        <span>Self-Evolution</span><span class="grow"></span><span class="live">LIVE</span>
      </div>
      <div class="evo-big">
        <div class="n" id="evo-total">0</div>
        <div class="lbl">Améliorations / 24 h</div>
      </div>
      <div class="evo-counts">
        <div class="evo-cell"><div class="n" id="evo-vocab">0</div><div class="k">Mots</div></div>
        <div class="evo-cell"><div class="n" id="evo-prefs">0</div><div class="k">Préfs</div></div>
        <div class="evo-cell"><div class="n" id="evo-speed">0</div><div class="k">Vitesse</div></div>
        <div class="evo-cell flag"><div class="n" id="evo-flag">0</div><div class="k">À corr.</div></div>
      </div>
      <div class="evo-state" id="evo-state">—</div>
      <div class="evo-feed-title">// Apprentissages</div>
      <div class="evo-feed" id="evo-feed"></div>
    </div>`;
  document.body.appendChild(el);

  const $ = (id: string) => el.querySelector("#" + id) as HTMLElement;
  const dot = $("evo-dot"), totalEl = $("evo-total"), feed = $("evo-feed"), stateEl = $("evo-state");
  const vocabEl = $("evo-vocab"), prefsEl = $("evo-prefs"), speedEl = $("evo-speed"), flagEl = $("evo-flag");

  let lastTotal = -1;
  let stopped = false;

  function fmtTime(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString("fr-FR",
      { hour: "2-digit", minute: "2-digit" });
  }

  function render(s: SeStats) {
    dot.classList.toggle("off", false);
    const bk = s.by_kind || {};
    totalEl.textContent = String(s.total || 0);
    vocabEl.textContent = String(bk.vocab || 0);
    prefsEl.textContent = String(bk.preference || 0);
    speedEl.textContent = String(bk.brevity || 0);
    flagEl.textContent = String(bk.frustration || 0);
    stateEl.innerHTML = `RÉPONSE <b>${s.max_tokens}j</b> · `
      + `<b>${s.vocab_total}</b> mots · <b>${s.prefs_total}</b> préfs`;

    // pop the big number when it grows
    if (lastTotal >= 0 && (s.total || 0) > lastTotal) {
      totalEl.style.transform = "scale(1.35)";
      setTimeout(() => { totalEl.style.transform = "scale(1)"; }, 250);
    }
    lastTotal = s.total || 0;

    const rows = s.recent || [];
    if (!rows.length) {
      feed.innerHTML = `<div class="evo-empty">en apprentissage…</div>`;
      return;
    }
    feed.innerHTML = rows.map((e) => {
      const flag = e.kind === "frustration" ? " flag" : "";
      const detail = (e.detail || "").replace(/</g, "&lt;");
      return `<div class="evo-row${flag}"><span class="t">${fmtTime(e.ts)}</span>`
        + `<span class="sd"></span><span class="d">${detail}</span></div>`;
    }).join("");
  }

  async function poll() {
    if (stopped) return;
    try {
      const r = await fetch("/api/selfeval/stats", { cache: "no-store" });
      if (r.ok) render(await r.json());
    } catch {
      dot.classList.add("off");
    }
  }

  poll();
  const timer = window.setInterval(poll, 5000);

  return {
    reveal() { el.classList.add("show"); },
    destroy() { stopped = true; clearInterval(timer); el.remove(); style.remove(); },
  };
}
