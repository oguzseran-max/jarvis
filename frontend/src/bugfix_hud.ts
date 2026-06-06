/**
 * Bug-fix review panel — human gate for Phase 2's autonomous fixer.
 *
 * Unlike the passive HUDs, this one is INTERACTIVE: it only appears when Marion
 * has drafted a fix awaiting approval, and lets you read the diff and approve
 * (merge) or dismiss (discard) with one click. Polls /api/bugfix/list; actions
 * POST to /api/bugfix/{id}/approve|dismiss. Bottom-centre so it reads as an
 * action card, not background telemetry.
 */

const C = "76, 168, 232"; // JARVIS cyan
const AMBER = "255, 176, 46";
const RED = "255, 64, 64";
const GREEN = "60, 220, 130";

interface Fix {
  id: number; signature: string; status: string; branch: string;
  occurrences: number; summary: string; diffstat: string; error: string;
  created_at: number; updated_at: number;
}

const CSS = `
#bugfix-hud {
  position: fixed; left: 50%; transform: translateX(-50%) translateY(20px);
  bottom: 22px; width: 440px; max-width: 92vw; z-index: 6;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  opacity: 0; pointer-events: none; transition: opacity 0.4s ease, transform 0.4s ease;
}
#bugfix-hud.show { opacity: 1; transform: translateX(-50%) translateY(0); pointer-events: auto; }
.bf-card { position: relative; margin-top: 10px; padding: 13px 15px;
  background: linear-gradient(180deg, rgba(8,18,28,0.94), rgba(8,18,28,0.86));
  border: 1px solid rgba(${AMBER}, 0.4); border-left: 3px solid rgba(${AMBER},1);
  border-radius: 8px; box-shadow: 0 8px 30px rgba(0,0,0,0.55);
  animation: bf-in 0.35s ease; }
@keyframes bf-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; } }
.bf-head { display: flex; align-items: center; gap: 8px; font-size: 10px;
  letter-spacing: 2px; text-transform: uppercase; color: rgba(${AMBER},1); margin-bottom: 8px; }
.bf-head .spark { width: 7px; height: 7px; border-radius: 50%; background: rgba(${AMBER},1);
  box-shadow: 0 0 8px rgba(${AMBER},0.9); animation: bf-pulse 1.6s ease-in-out infinite; }
@keyframes bf-pulse { 0%,100% { opacity: 0.4; } 50% { opacity: 1; } }
.bf-head .grow { flex: 1; }
.bf-head .occ { font-size: 9px; color: rgba(255,255,255,0.45); letter-spacing: 1px; }
.bf-sig { font-size: 13px; color: #eafffb; margin-bottom: 5px; font-weight: 600; }
.bf-sum { font-size: 11px; line-height: 1.5; color: rgba(${C}, 0.9);
  max-height: 56px; overflow: hidden; margin-bottom: 7px; }
.bf-stat { font-size: 9.5px; color: rgba(255,255,255,0.4); white-space: pre-wrap;
  border-left: 2px solid rgba(${C},0.25); padding-left: 8px; margin-bottom: 9px; }
.bf-diff { display: none; margin: 0 0 9px; max-height: 230px; overflow: auto;
  background: rgba(0,0,0,0.5); border: 1px solid rgba(${C},0.18); border-radius: 5px;
  padding: 9px 11px; font-size: 10px; line-height: 1.45; white-space: pre; color: rgba(${C},0.85); }
.bf-diff.on { display: block; }
.bf-diff .add { color: rgba(${GREEN},1); }
.bf-diff .del { color: rgba(${RED},1); }
.bf-diff .hd { color: rgba(${AMBER},1); }
.bf-actions { display: flex; gap: 8px; }
.bf-btn { flex: 1; cursor: pointer; font: inherit; font-size: 11px; letter-spacing: 1px;
  padding: 8px 10px; border-radius: 6px; text-transform: uppercase; transition: 0.15s;
  border: 1px solid rgba(${C},0.3); background: rgba(${C},0.06); color: rgba(${C},0.9); }
.bf-btn:hover { border-color: rgba(${C},0.7); }
.bf-btn.ok { border-color: rgba(${GREEN},0.45); color: rgba(${GREEN},1); background: rgba(${GREEN},0.08); }
.bf-btn.ok:hover { background: rgba(${GREEN},0.18); box-shadow: 0 0 16px rgba(${GREEN},0.35); }
.bf-btn.no { border-color: rgba(${RED},0.4); color: rgba(${RED},1); background: rgba(${RED},0.07); }
.bf-btn.no:hover { background: rgba(${RED},0.16); }
.bf-btn[disabled] { opacity: 0.5; cursor: default; }
.bf-busy { font-size: 10px; color: rgba(${C},0.7); letter-spacing: 1px; padding: 4px 0; }
`;

export interface BugfixHud { reveal(): void; destroy(): void; }

function escapeHtml(s: string): string {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function colorizeDiff(diff: string): string {
  return escapeHtml(diff).split("\n").map((l) => {
    if (l.startsWith("+") && !l.startsWith("+++")) return `<span class="add">${l}</span>`;
    if (l.startsWith("-") && !l.startsWith("---")) return `<span class="del">${l}</span>`;
    if (l.startsWith("@@") || l.startsWith("diff ")) return `<span class="hd">${l}</span>`;
    return l;
  }).join("\n");
}

export function createBugfixHud(): BugfixHud {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "bugfix-hud";
  document.body.appendChild(el);

  let stopped = false;
  let revealed = false;
  let busy = new Set<number>();   // ids mid-action, so we don't double-submit

  async function act(id: number, what: "approve" | "dismiss") {
    if (busy.has(id)) return;
    busy.add(id);
    render(lastFixes); // re-render to disable buttons
    try {
      await fetch(`/api/bugfix/${id}/${what}`, { method: "POST" });
    } catch { /* surfaced on next poll */ }
    busy.delete(id);
    poll(); // immediate refresh
  }

  async function toggleDiff(id: number, box: HTMLElement) {
    if (box.classList.contains("on")) { box.classList.remove("on"); return; }
    box.classList.add("on");
    if (box.dataset.loaded) return;
    box.textContent = "chargement du diff…";
    try {
      const r = await fetch(`/api/bugfix/${id}/diff`);
      const d = (await r.json()).diff || "(diff vide)";
      box.innerHTML = colorizeDiff(d);
      box.dataset.loaded = "1";
    } catch { box.textContent = "diff indisponible"; }
  }

  let lastFixes: Fix[] = [];
  function render(fixes: Fix[]) {
    lastFixes = fixes;
    // Only fixes that need a human decision.
    const pending = fixes.filter((f) => f.status === "ready");
    const working = fixes.filter((f) => f.status === "analyzing");
    if (!revealed || (!pending.length && !working.length)) {
      el.classList.remove("show");
      el.innerHTML = "";
      return;
    }
    el.classList.add("show");
    el.innerHTML = "";

    if (working.length) {
      const w = document.createElement("div");
      w.className = "bf-card";
      w.style.borderLeftColor = `rgba(${C},1)`;
      w.style.borderColor = `rgba(${C},0.4)`;
      w.innerHTML = `<div class="bf-head"><span class="spark"></span>`
        + `<span>Marion analyse un bug…</span></div>`
        + `<div class="bf-sig">${escapeHtml(working[0].signature)}</div>`;
      el.appendChild(w);
    }

    for (const f of pending) {
      const card = document.createElement("div");
      card.className = "bf-card";
      const disabled = busy.has(f.id) ? "disabled" : "";
      card.innerHTML = `
        <div class="bf-head"><span class="spark"></span>
          <span>Correctif à valider</span><span class="grow"></span>
          <span class="occ">×${f.occurrences} occurrences</span></div>
        <div class="bf-sig">${escapeHtml(f.signature)}</div>
        <div class="bf-sum">${escapeHtml(f.summary || "—")}</div>
        <div class="bf-stat">${escapeHtml(f.diffstat || "")}</div>
        <div class="bf-diff" id="bf-diff-${f.id}"></div>
        <div class="bf-actions">
          <button class="bf-btn" data-act="diff" ${disabled}>Voir le diff</button>
          <button class="bf-btn ok" data-act="approve" ${disabled}>✓ Valider</button>
          <button class="bf-btn no" data-act="dismiss" ${disabled}>✗ Rejeter</button>
        </div>${busy.has(f.id) ? '<div class="bf-busy">traitement…</div>' : ""}`;
      const diffBox = card.querySelector(`#bf-diff-${f.id}`) as HTMLElement;
      card.querySelector('[data-act="diff"]')!.addEventListener("click", () => toggleDiff(f.id, diffBox));
      card.querySelector('[data-act="approve"]')!.addEventListener("click", () => act(f.id, "approve"));
      card.querySelector('[data-act="dismiss"]')!.addEventListener("click", () => act(f.id, "dismiss"));
      el.appendChild(card);
    }
  }

  async function poll() {
    if (stopped) return;
    try {
      const r = await fetch("/api/bugfix/list", { cache: "no-store" });
      if (r.ok) render((await r.json()).fixes || []);
    } catch { /* keep last */ }
  }

  poll();
  const timer = window.setInterval(poll, 6000);

  return {
    reveal() { revealed = true; render(lastFixes); },
    destroy() { stopped = true; clearInterval(timer); el.remove(); style.remove(); },
  };
}
