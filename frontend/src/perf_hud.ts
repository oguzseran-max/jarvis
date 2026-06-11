/**
 * Performance HUD — live voice-loop telemetry on Marion's screen (Phase 0).
 *
 * A self-contained panel (left side) that polls /api/perf/stats and shows the
 * per-stage latency (transcription / reasoning / speech), p50/p95, and a live
 * feed of flagged turns (slow / off-target / errored). Same JARVIS cyan palette
 * as the WatchGuard panel; amber/red escalation so a bug is visible the instant
 * it happens. Mirrors createSecurityHud()'s structure.
 */

const C = "76, 168, 232"; // JARVIS cyan
const AMBER = "255, 176, 46";
const RED = "255, 64, 64";
const GREEN = "60, 220, 130";

interface PerfTurn {
  ts: number; lang: string; total_ms: number; severity: string;
  flags: string[]; user_text: string; reply_text: string;
  stages: Record<string, number>;
}
interface PerfStats {
  turns: number; warn: number; bad: number; p50_ms: number; p95_ms: number;
  avg_stt_ms: number; avg_reason_ms: number; avg_tts_ms: number;
  recent: PerfTurn[];
}

const CSS = `
#perf-hud {
  position: fixed; left: 18px; top: 320px;
  width: 250px; z-index: 4; pointer-events: none;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  color: rgba(${C}, 0.95); opacity: 0; transition: opacity 0.6s ease;
}
#perf-hud.show { opacity: 1; }
.perf-frame {
  position: relative; padding: 12px 13px 13px;
  background: linear-gradient(180deg, rgba(6,16,26,0.82), rgba(6,16,26,0.62));
  border: 1px solid rgba(${C}, 0.22); border-radius: 7px;
  box-shadow: 0 0 22px rgba(0,0,0,0.4), inset 0 0 22px rgba(${C}, 0.04);
}
.perf-head { display: flex; align-items: center; gap: 7px; font-size: 10.5px;
  letter-spacing: 2.5px; text-transform: uppercase;
  border-bottom: 1px solid rgba(${C}, 0.18); padding-bottom: 7px; margin-bottom: 9px; }
.perf-head .grow { flex: 1; }
.perf-head .live { font-size: 8px; letter-spacing: 2px; color: rgba(${GREEN},1); }
.perf-dot { width: 7px; height: 7px; border-radius: 50%; background: rgba(${GREEN},1);
  box-shadow: 0 0 8px rgba(${GREEN},0.9); }
.perf-dot.off { background: rgba(${RED},1); box-shadow: 0 0 8px rgba(${RED},0.9); }
.perf-lat { display: flex; gap: 8px; margin-bottom: 10px; }
.perf-cell { flex: 1; text-align: center; padding: 6px 2px; border-radius: 5px;
  background: rgba(${C}, 0.05); border: 1px solid rgba(${C}, 0.12); }
.perf-cell .n { font-size: 17px; font-weight: 700; }
.perf-cell .k { font-size: 7.5px; letter-spacing: 1.5px; margin-top: 3px;
  text-transform: uppercase; color: rgba(255,255,255,0.45); }
.perf-bars { margin-bottom: 10px; }
.perf-bar { display: flex; align-items: center; gap: 6px; margin: 4px 0; font-size: 9px; }
.perf-bar .bk { width: 52px; letter-spacing: 1px; text-transform: uppercase;
  color: rgba(255,255,255,0.5); }
.perf-bar .track { flex: 1; height: 5px; border-radius: 3px; background: rgba(${C},0.08); overflow: hidden; }
.perf-bar .fill { height: 100%; background: rgba(${C}, 0.7); border-radius: 3px; }
.perf-bar .ms { width: 42px; text-align: right; color: rgba(${C},0.9); }
.perf-feed-title { font-size: 8.5px; letter-spacing: 2px; color: rgba(${C}, 0.5);
  text-transform: uppercase; margin-bottom: 5px; }
.perf-feed { display: flex; flex-direction: column; gap: 3px; max-height: 150px; overflow: hidden; }
.perf-row { display: flex; gap: 6px; font-size: 9px; line-height: 1.35;
  animation: perf-in 0.3s ease; }
@keyframes perf-in { from { opacity: 0; transform: translateX(-6px); } to { opacity: 1; } }
.perf-row .sd { width: 6px; height: 6px; border-radius: 50%; margin-top: 3px; flex: none;
  background: rgba(${GREEN},0.9); }
.perf-row.warn .sd { background: rgba(${AMBER},1); }
.perf-row.bad .sd { background: rgba(${RED},1); }
.perf-row .ms { color: rgba(${C},0.85); flex: none; width: 44px; }
.perf-row .d { color: rgba(255,255,255,0.62); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.perf-empty { font-size: 9px; color: rgba(${GREEN},0.8); letter-spacing: 1px; }
.perf-frame.alarm { animation: perf-alarm 1.1s ease 2; }
@keyframes perf-alarm { 0%,100% { box-shadow: 0 0 22px rgba(0,0,0,0.4); }
  50% { box-shadow: 0 0 26px rgba(${RED},0.6), inset 0 0 22px rgba(${RED},0.12);
        border-color: rgba(${RED},0.7); } }
`;

export interface PerfHud {
  reveal(): void;
  destroy(): void;
}

function ms(v: number): string {
  return v >= 1000 ? (v / 1000).toFixed(1) + "s" : Math.round(v) + "ms";
}
function latColor(v: number): string {
  if (v > 6000) return `rgba(${RED},1)`;
  if (v > 3000) return `rgba(${AMBER},1)`;
  return `rgba(${GREEN},1)`;
}

export function createPerfHud(): PerfHud {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "perf-hud";
  el.innerHTML = `
    <div class="perf-frame">
      <div class="perf-head">
        <span class="perf-dot" id="perf-dot"></span>
        <span>Perf Monitor</span><span class="grow"></span><span class="live">LIVE</span>
      </div>
      <div class="perf-lat">
        <div class="perf-cell"><div class="n" id="perf-p50">—</div><div class="k">P50</div></div>
        <div class="perf-cell"><div class="n" id="perf-p95">—</div><div class="k">P95</div></div>
        <div class="perf-cell"><div class="n" id="perf-bad">0</div><div class="k">Issues</div></div>
      </div>
      <div class="perf-bars">
        <div class="perf-bar"><span class="bk">Écoute</span><span class="track"><span class="fill" id="perf-b-stt"></span></span><span class="ms" id="perf-stt">—</span></div>
        <div class="perf-bar"><span class="bk">Réflex.</span><span class="track"><span class="fill" id="perf-b-rsn"></span></span><span class="ms" id="perf-rsn">—</span></div>
        <div class="perf-bar"><span class="bk">Voix</span><span class="track"><span class="fill" id="perf-b-tts"></span></span><span class="ms" id="perf-tts">—</span></div>
      </div>
      <div class="perf-feed-title">// Tours signalés</div>
      <div class="perf-feed" id="perf-feed"></div>
    </div>`;
  document.body.appendChild(el);

  const $ = (id: string) => el.querySelector("#" + id) as HTMLElement;
  const dot = $("perf-dot");
  const p50El = $("perf-p50"), p95El = $("perf-p95"), badEl = $("perf-bad");
  const feed = $("perf-feed");

  let lastBad = -1;
  let stopped = false;

  function fmtTime(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString("fr-FR",
      { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function render(s: PerfStats) {
    dot.classList.remove("off");
    p50El.textContent = s.turns ? ms(s.p50_ms) : "—";
    p50El.style.color = latColor(s.p50_ms);
    p95El.textContent = s.turns ? ms(s.p95_ms) : "—";
    p95El.style.color = latColor(s.p95_ms);
    badEl.textContent = String((s.warn || 0) + (s.bad || 0));
    badEl.style.color = s.bad ? `rgba(${RED},1)` : (s.warn ? `rgba(${AMBER},1)` : `rgba(${GREEN},1)`);

    // Stage bars scaled against the slowest stage (so they're always readable).
    const mx = Math.max(s.avg_stt_ms, s.avg_reason_ms, s.avg_tts_ms, 1);
    const setBar = (barId: string, msId: string, v: number) => {
      ($(barId)).style.width = Math.round((v / mx) * 100) + "%";
      ($(msId)).textContent = ms(v);
    };
    setBar("perf-b-stt", "perf-stt", s.avg_stt_ms);
    setBar("perf-b-rsn", "perf-rsn", s.avg_reason_ms);
    setBar("perf-b-tts", "perf-tts", s.avg_tts_ms);

    // Alarm flash when a NEW bad turn lands.
    if (lastBad >= 0 && (s.bad || 0) > lastBad) {
      const f = el.querySelector(".perf-frame")!;
      f.classList.remove("alarm"); void (f as HTMLElement).offsetWidth; f.classList.add("alarm");
    }
    lastBad = s.bad || 0;

    const flagged = (s.recent || []).filter((t) => t.severity !== "ok");
    if (!flagged.length) {
      feed.innerHTML = `<div class="perf-empty">✓ AUCUN PROBLÈME</div>`;
      return;
    }
    feed.innerHTML = flagged.slice(0, 6).map((t) => {
      const label = (t.flags[0] || t.user_text || "—").replace(/</g, "&lt;");
      return `<div class="perf-row ${t.severity}"><span class="sd"></span>`
        + `<span class="ms">${ms(t.total_ms)}</span>`
        + `<span class="d" title="${fmtTime(t.ts)}">${label}</span></div>`;
    }).join("");
  }

  async function poll() {
    if (stopped) return;
    try {
      const r = await fetch("/api/perf/stats", { cache: "no-store" });
      if (r.ok) render(await r.json());
    } catch {
      dot.classList.add("off");
    }
  }

  poll();
  const timer = window.setInterval(poll, 4000);

  return {
    reveal() { el.classList.add("show"); },
    destroy() { stopped = true; clearInterval(timer); el.remove(); style.remove(); },
  };
}
