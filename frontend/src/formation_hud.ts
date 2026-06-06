/**
 * Self-formation review panel — human gate for Phase 3's learned guidance.
 *
 * Surfaces Marion's latest self-assessment and the guidance rules she proposes
 * about her own behaviour. Proposed rules change NOTHING until you activate one
 * here (or set FORMATION_AUTOAPPLY=true). Polls /api/formation/report; actions
 * POST to /api/formation/guidance/{id}/{activate|propose|remove}. Right side,
 * appears only when there's a rule to review.
 */

const C = "76, 168, 232"; // JARVIS cyan
const AMBER = "255, 176, 46";
const RED = "255, 64, 64";
const GREEN = "60, 220, 130";
const VIOLET = "150, 130, 255";

interface Guidance { id: number; lang: string; text: string; area: string; status: string; }
interface Report {
  latest: { assessment: { summary: string } | null; guidance: Guidance[] };
}

const CSS = `
#formation-hud {
  position: fixed; right: 18px; top: 340px; width: 286px; max-width: 90vw; z-index: 5;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  opacity: 0; pointer-events: none; transition: opacity 0.45s ease;
}
#formation-hud.show { opacity: 1; pointer-events: auto; }
.fm-frame { padding: 13px 14px;
  background: linear-gradient(180deg, rgba(10,16,28,0.92), rgba(10,16,28,0.8));
  border: 1px solid rgba(${VIOLET}, 0.32); border-left: 3px solid rgba(${VIOLET},1);
  border-radius: 8px; box-shadow: 0 8px 28px rgba(0,0,0,0.5); }
.fm-head { display: flex; align-items: center; gap: 7px; font-size: 10px;
  letter-spacing: 2px; text-transform: uppercase; color: rgba(${VIOLET},1);
  border-bottom: 1px solid rgba(${VIOLET},0.2); padding-bottom: 7px; margin-bottom: 9px; }
.fm-head .spark { width: 7px; height: 7px; border-radius: 50%; background: rgba(${VIOLET},1);
  box-shadow: 0 0 8px rgba(${VIOLET},0.9); }
.fm-assess { font-size: 10.5px; line-height: 1.5; color: rgba(${C},0.85);
  font-style: italic; margin-bottom: 11px; }
.fm-sect { font-size: 8.5px; letter-spacing: 1.5px; text-transform: uppercase;
  color: rgba(255,255,255,0.4); margin: 4px 0 6px; }
.fm-rule { margin-bottom: 9px; padding: 9px 10px; border-radius: 6px;
  background: rgba(${C},0.05); border: 1px solid rgba(${C},0.14); animation: fm-in 0.3s ease; }
@keyframes fm-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; } }
.fm-rule.active { border-color: rgba(${GREEN},0.35); background: rgba(${GREEN},0.06); }
.fm-rule .area { font-size: 8px; letter-spacing: 1px; text-transform: uppercase;
  color: rgba(${AMBER},1); margin-bottom: 4px; }
.fm-rule.active .area { color: rgba(${GREEN},1); }
.fm-rule .txt { font-size: 11px; line-height: 1.45; color: #dbeef0; margin-bottom: 8px; }
.fm-acts { display: flex; gap: 6px; }
.fm-btn { flex: 1; cursor: pointer; font: inherit; font-size: 10px; letter-spacing: 0.5px;
  padding: 6px 8px; border-radius: 5px; text-transform: uppercase; transition: 0.15s;
  border: 1px solid rgba(${C},0.3); background: rgba(${C},0.06); color: rgba(${C},0.9); }
.fm-btn:hover { border-color: rgba(${C},0.7); }
.fm-btn.ok { border-color: rgba(${GREEN},0.45); color: rgba(${GREEN},1); background: rgba(${GREEN},0.08); }
.fm-btn.ok:hover { background: rgba(${GREEN},0.18); }
.fm-btn.no { border-color: rgba(${RED},0.4); color: rgba(${RED},1); background: rgba(${RED},0.07); }
.fm-btn.no:hover { background: rgba(${RED},0.16); }
.fm-btn[disabled] { opacity: 0.5; cursor: default; }
`;

export interface FormationHud { reveal(): void; destroy(): void; }

function esc(s: string): string {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function createFormationHud(): FormationHud {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "formation-hud";
  document.body.appendChild(el);

  let stopped = false, revealed = false;
  const busy = new Set<number>();
  let last: Report | null = null;

  async function act(id: number, action: "activate" | "propose" | "remove") {
    if (busy.has(id)) return;
    busy.add(id);
    if (last) render(last);
    try { await fetch(`/api/formation/guidance/${id}/${action}`, { method: "POST" }); }
    catch { /* next poll surfaces it */ }
    busy.delete(id);
    poll();
  }

  function ruleCard(g: Guidance): string {
    const dis = busy.has(g.id) ? "disabled" : "";
    if (g.status === "active") {
      return `<div class="fm-rule active">
        <div class="area">✓ ${esc(g.area || "actif")}</div>
        <div class="txt">${esc(g.text)}</div>
        <div class="fm-acts"><button class="fm-btn no" data-id="${g.id}" data-a="propose" ${dis}>Désactiver</button></div>
      </div>`;
    }
    return `<div class="fm-rule">
      <div class="area">${esc(g.area || "guidance")}</div>
      <div class="txt">${esc(g.text)}</div>
      <div class="fm-acts">
        <button class="fm-btn ok" data-id="${g.id}" data-a="activate" ${dis}>✓ Activer</button>
        <button class="fm-btn no" data-id="${g.id}" data-a="remove" ${dis}>✗ Rejeter</button>
      </div></div>`;
  }

  function render(rep: Report) {
    last = rep;
    const guidance = (rep.latest && rep.latest.guidance) || [];
    const proposed = guidance.filter((g) => g.status === "proposed");
    const active = guidance.filter((g) => g.status === "active");
    if (!revealed || !proposed.length) {   // only demand attention when there's a decision
      el.classList.remove("show");
      el.innerHTML = "";
      return;
    }
    el.classList.add("show");
    const summary = rep.latest?.assessment?.summary || "";
    el.innerHTML = `<div class="fm-frame">
      <div class="fm-head"><span class="spark"></span><span>Auto-évaluation</span></div>
      ${summary ? `<div class="fm-assess">« ${esc(summary)} »</div>` : ""}
      <div class="fm-sect">Guidance proposée (${proposed.length})</div>
      ${proposed.map(ruleCard).join("")}
      ${active.length ? `<div class="fm-sect">Active (${active.length})</div>` + active.map(ruleCard).join("") : ""}
    </div>`;
    el.querySelectorAll<HTMLButtonElement>(".fm-btn[data-id]").forEach((b) => {
      b.addEventListener("click", () =>
        act(Number(b.dataset.id), b.dataset.a as "activate" | "propose" | "remove"));
    });
  }

  async function poll() {
    if (stopped) return;
    try {
      const r = await fetch("/api/formation/report", { cache: "no-store" });
      if (r.ok) render(await r.json());
    } catch { /* keep last */ }
  }

  poll();
  const timer = window.setInterval(poll, 8000);

  return {
    reveal() { revealed = true; if (last) render(last); },
    destroy() { stopped = true; clearInterval(timer); el.remove(); style.remove(); },
  };
}
