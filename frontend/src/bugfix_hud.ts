/**
 * Bug-fix launcher — compact gate for Phase 2's autonomous fixer.
 *
 * Instead of an inline review stack, this shows a small bottom-centre BUTTON
 * when Marion has drafted a fix awaiting approval ("Correctif à valider (N)").
 * Clicking it opens the full review page (/bugfix.html, served same-origin) in
 * a new tab, where you read the diff and approve (merge) / dismiss. While she's
 * analyzing a bug it shows a subtle working indicator. Polls /api/bugfix/list.
 */

const C = "76, 168, 232"; // JARVIS cyan
const AMBER = "255, 176, 46";

const CSS = `
#bugfix-hud {
  position: fixed; left: 50%; transform: translateX(-50%) translateY(16px);
  bottom: 22px; z-index: 6; font-family: ui-monospace, "SF Mono", Menlo, monospace;
  opacity: 0; pointer-events: none; transition: opacity 0.4s ease, transform 0.4s ease;
}
#bugfix-hud.show { opacity: 1; transform: translateX(-50%) translateY(0); pointer-events: auto; }
.bf-btn { cursor: pointer; display: inline-flex; align-items: center; gap: 9px;
  font: inherit; font-size: 12px; letter-spacing: 1.5px; text-transform: uppercase;
  padding: 11px 18px; border-radius: 8px; color: rgba(${AMBER},1);
  background: linear-gradient(180deg, rgba(28,20,8,0.94), rgba(28,20,8,0.86));
  border: 1px solid rgba(${AMBER},0.5); box-shadow: 0 8px 28px rgba(0,0,0,0.5);
  transition: 0.15s; }
.bf-btn:hover { border-color: rgba(${AMBER},0.9); box-shadow: 0 0 22px rgba(${AMBER},0.4); }
.bf-btn .dot { width: 8px; height: 8px; border-radius: 50%; background: rgba(${AMBER},1);
  box-shadow: 0 0 9px rgba(${AMBER},0.9); animation: bf-pulse 1.5s ease-in-out infinite; }
@keyframes bf-pulse { 0%,100% { opacity: 0.4; } 50% { opacity: 1; } }
.bf-btn .count { background: rgba(${AMBER},0.9); color: #1a1206; border-radius: 10px;
  padding: 1px 8px; font-size: 11px; font-weight: 700; }
.bf-working { display: inline-flex; align-items: center; gap: 9px; font-size: 11px;
  letter-spacing: 1px; color: rgba(${C},0.85); padding: 9px 16px; border-radius: 8px;
  background: rgba(8,18,28,0.8); border: 1px solid rgba(${C},0.3); }
.bf-working .dot { width: 7px; height: 7px; border-radius: 50%; background: rgba(${C},1);
  box-shadow: 0 0 8px rgba(${C},0.9); animation: bf-pulse 1.5s ease-in-out infinite; }
`;

export interface BugfixHud { reveal(): void; destroy(): void; }

interface Fix { status: string; }

export function createBugfixHud(): BugfixHud {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "bugfix-hud";
  document.body.appendChild(el);

  let stopped = false, revealed = false;

  function render(fixes: Fix[]) {
    const ready = fixes.filter((f) => f.status === "ready").length;
    const analyzing = fixes.filter((f) => f.status === "analyzing").length;
    if (!revealed || (!ready && !analyzing)) {
      el.classList.remove("show");
      el.innerHTML = "";
      return;
    }
    el.classList.add("show");
    if (ready) {
      el.innerHTML = `<button class="bf-btn"><span class="dot"></span>`
        + `Correctif à valider <span class="count">${ready}</span></button>`;
      el.querySelector(".bf-btn")!.addEventListener("click",
        () => window.open("/bugfix.html", "_blank"));
    } else {
      el.innerHTML = `<div class="bf-working"><span class="dot"></span>Marion analyse un bug…</div>`;
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
    reveal() { revealed = true; poll(); },
    destroy() { stopped = true; clearInterval(timer); el.remove(); style.remove(); },
  };
}
