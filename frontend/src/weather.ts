/**
 * Cinematic weather FX for JARVIS / Marion.
 *
 * Polls /api/weather → {condition} and stages a "Hollywood" effect:
 *   - rain / storm → layered, depth-parallax rain with motion-blurred streaks,
 *     splash ripples + bouncing micro-droplets at the bottom, occasional
 *     lightning, a moody blue color grade, and Marion under an umbrella.
 *   - clear        → a volumetric glowing sun with rotating god-rays + warm grade,
 *     and Marion in sunglasses.
 *   - otherwise    → nothing.
 *
 * All canvas/CSS — no assets. Particle counts are capped for performance.
 */

interface Drop { x: number; y: number; len: number; spd: number; w: number; a: number; }
interface Splash { x: number; y: number; vx: number; vy: number; life: number; }
interface Ripple { x: number; y: number; r: number; life: number; }

// onLook("rain"|"sun"|"default") lets the app swap Marion's outfit photo (and the
// talking-video portrait) to match the weather.
export function createWeather(onLook: (look: string) => void) {
  const RAINC = "175, 205, 255";

  // ── Color-grade overlay (mood) ──
  const grade = document.createElement("div");
  grade.style.cssText =
    "position:fixed;inset:0;z-index:5;pointer-events:none;opacity:0;transition:opacity 1.2s ease;mix-blend-mode:multiply;";
  document.body.appendChild(grade);

  // ── Lightning flash overlay ──
  const flash = document.createElement("div");
  flash.style.cssText =
    "position:fixed;inset:0;z-index:7;pointer-events:none;opacity:0;background:radial-gradient(ellipse at 50% 0%, rgba(220,235,255,.9), rgba(180,210,255,.3) 40%, transparent 70%);";
  document.body.appendChild(flash);

  // ── Rain canvas ──
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "position:fixed;inset:0;z-index:6;pointer-events:none;display:none;";
  document.body.appendChild(canvas);
  const ctx = canvas.getContext("2d")!;

  // ── Volumetric sun ──
  const sun = document.createElement("div");
  sun.id = "sun-fx";
  sun.innerHTML = `<div class="sun-core"></div><div class="sun-rays"></div><div class="sun-glow"></div>`;
  document.body.appendChild(sun);

  const style = document.createElement("style");
  style.textContent = `
    #sun-fx{position:fixed;top:-60px;right:90px;width:340px;height:340px;z-index:5;pointer-events:none;opacity:0;transition:opacity 1.4s ease;}
    #sun-fx.show{opacity:1;}
    #sun-fx>div{position:absolute;inset:0;border-radius:50%;}
    .sun-core{background:radial-gradient(circle at 50% 50%, #fff 0%, #ffe9a8 14%, #ffcf4d 26%, rgba(255,180,60,.35) 42%, transparent 60%);
      filter:blur(2px);animation:sun-pulse 5s ease-in-out infinite;}
    .sun-glow{background:radial-gradient(circle, rgba(255,210,110,.55) 0%, rgba(255,180,70,.18) 35%, transparent 65%);filter:blur(14px);}
    .sun-rays{background:conic-gradient(from 0deg, rgba(255,225,150,.55) 0deg, transparent 7deg, transparent 22deg,
      rgba(255,225,150,.5) 29deg, transparent 36deg, transparent 52deg, rgba(255,225,150,.55) 59deg, transparent 66deg,
      transparent 82deg, rgba(255,225,150,.5) 89deg, transparent 96deg, transparent 112deg, rgba(255,225,150,.55) 119deg,
      transparent 126deg, transparent 142deg, rgba(255,225,150,.5) 149deg, transparent 156deg, transparent 172deg,
      rgba(255,225,150,.55) 179deg, transparent 186deg, transparent 202deg, rgba(255,225,150,.5) 209deg, transparent 216deg,
      transparent 232deg, rgba(255,225,150,.55) 239deg, transparent 246deg, transparent 262deg, rgba(255,225,150,.5) 269deg,
      transparent 276deg, transparent 292deg, rgba(255,225,150,.55) 299deg, transparent 306deg, transparent 322deg,
      rgba(255,225,150,.5) 329deg, transparent 336deg, transparent 352deg, rgba(255,225,150,.55) 359deg);
      -webkit-mask:radial-gradient(circle, transparent 18%, #000 26%, #000 52%, transparent 70%);
      mask:radial-gradient(circle, transparent 18%, #000 26%, #000 52%, transparent 70%);
      filter:blur(3px);transform-origin:50% 50%;animation:sun-spin 60s linear infinite;}
    @keyframes sun-spin{to{transform:rotate(360deg);}}
    @keyframes sun-pulse{50%{transform:scale(1.06);filter:blur(1px);}}
  `;
  document.head.appendChild(style);

  let W = 0, H = 0;
  const resize = () => { W = canvas.width = innerWidth; H = canvas.height = innerHeight; };
  resize();
  addEventListener("resize", resize);

  // 3 depth layers: far (small/slow/faint) → near (big/fast/bright)
  const LAYERS = [
    { count: 0, spd: [3, 5], len: [6, 12], w: 0.8, a: 0.22, scale: 0.5 },
    { count: 0, spd: [6, 9], len: [12, 22], w: 1.1, a: 0.38, scale: 0.75 },
    { count: 0, spd: [10, 15], len: [20, 38], w: 1.7, a: 0.6, scale: 1 },
  ];
  const drops: Drop[] = [];
  const splashes: Splash[] = [];
  const ripples: Ripple[] = [];
  const WIND = 1.6;  // slight slant
  let raining = false, raf = 0, lastBolt = 0;

  function seed() {
    drops.length = 0;
    const base = Math.max(60, Math.round(W / 4));
    LAYERS[0].count = Math.round(base * 0.5);
    LAYERS[1].count = Math.round(base * 0.35);
    LAYERS[2].count = Math.round(base * 0.2);
    for (const L of LAYERS) {
      for (let i = 0; i < L.count; i++) {
        drops.push({
          x: Math.random() * (W + 200) - 100, y: Math.random() * H,
          len: L.len[0] + Math.random() * (L.len[1] - L.len[0]),
          spd: L.spd[0] + Math.random() * (L.spd[1] - L.spd[0]),
          w: L.w, a: L.a,
        });
      }
    }
  }

  function frame(t: number) {
    if (!raining) return;
    raf = requestAnimationFrame(frame);
    ctx.clearRect(0, 0, W, H);

    // streaks (motion-blurred via gradient)
    for (const d of drops) {
      const g = ctx.createLinearGradient(d.x, d.y, d.x + WIND * d.len * 0.3, d.y + d.len);
      g.addColorStop(0, `rgba(${RAINC},0)`);
      g.addColorStop(1, `rgba(${RAINC},${d.a})`);
      ctx.strokeStyle = g; ctx.lineWidth = d.w;
      ctx.beginPath();
      ctx.moveTo(d.x, d.y);
      ctx.lineTo(d.x + WIND * d.len * 0.3, d.y + d.len);
      ctx.stroke();
      d.y += d.spd; d.x += WIND;
      if (d.y > H) {
        if (d.a > 0.3 && splashes.length < 260) {  // only near drops splash
          ripples.push({ x: d.x, y: H - 1, r: 1, life: 1 });
          for (let k = 0; k < 3; k++)
            splashes.push({ x: d.x, y: H - 2, vx: (Math.random() - 0.5) * 3.5, vy: -(2.5 + Math.random() * 3.5), life: 1 });
        }
        d.y = -d.len; d.x = Math.random() * (W + 200) - 100;
      }
    }

    // splash droplets (gravity + fade)
    for (let i = splashes.length - 1; i >= 0; i--) {
      const s = splashes[i];
      s.x += s.vx; s.y += s.vy; s.vy += 0.4; s.life -= 0.045;
      if (s.life <= 0) { splashes.splice(i, 1); continue; }
      ctx.fillStyle = `rgba(${RAINC},${Math.max(0, s.life) * 0.75})`;
      ctx.fillRect(s.x, s.y, 2, 2);
    }
    // ripples (expanding rings on the floor)
    for (let i = ripples.length - 1; i >= 0; i--) {
      const r = ripples[i];
      r.r += 0.7; r.life -= 0.05;
      if (r.life <= 0) { ripples.splice(i, 1); continue; }
      ctx.strokeStyle = `rgba(${RAINC},${Math.max(0, r.life) * 0.4})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.ellipse(r.x, r.y, r.r * 2, r.r * 0.7, 0, 0, Math.PI * 2);
      ctx.stroke();
    }

    // occasional lightning
    if (t - lastBolt > 6000 && Math.random() < 0.004) {
      lastBolt = t;
      flash.style.transition = "opacity .06s";
      flash.style.opacity = "0.9";
      setTimeout(() => { flash.style.opacity = "0.2"; }, 70);
      setTimeout(() => { flash.style.opacity = "0.7"; }, 140);
      setTimeout(() => { flash.style.transition = "opacity .5s"; flash.style.opacity = "0"; }, 220);
    }
  }

  function startRain() {
    if (!raining) { raining = true; seed(); canvas.style.display = "block"; raf = requestAnimationFrame(frame); }
    grade.style.background = "linear-gradient(180deg, rgba(70,90,130,.55), rgba(30,45,75,.7))";
    grade.style.opacity = "1";
  }
  function stopRain() {
    raining = false; cancelAnimationFrame(raf);
    splashes.length = 0; ripples.length = 0;
    ctx.clearRect(0, 0, W, H); canvas.style.display = "none";
  }

  let lastLook = "";
  function apply(condition: string) {
    let look: string;
    if (condition === "rain" || condition === "storm") {
      startRain(); grade.style.mixBlendMode = "multiply";
      sun.classList.remove("show"); look = "rain";
    } else if (condition === "clear") {
      stopRain(); grade.style.background = "radial-gradient(ellipse at 75% 0%, rgba(255,220,150,.4), transparent 60%)";
      grade.style.mixBlendMode = "screen"; grade.style.opacity = "1";
      sun.classList.add("show"); look = "sun";
    } else {
      stopRain(); grade.style.opacity = "0"; sun.classList.remove("show"); look = "default";
    }
    if (look !== lastLook) { lastLook = look; onLook(look); }
  }

  async function poll() {
    try {
      const r = await fetch("/api/weather");
      if (r.ok) { const d = await r.json(); if (d && d.condition) apply(d.condition); }
    } catch { /* ignore */ }
  }

  // Manual override (e.g. the voice command "mets-toi en bikini"). While set, it
  // wins over real weather and pauses polling — until cleared by auto().
  let override: string | null = null;
  const _forced = () => {
    if (override) return override;
    const f = decodeURIComponent(location.hash.replace("#", "")).trim();
    return ["rain", "storm", "clear", "clouds"].includes(f) ? f : null;
  };
  return {
    start() {
      // Preview override: open .../#rain, #storm, #clear or #clouds to force an
      // effect (sticky — real weather polling is paused while a hash is set).
      const f = _forced();
      if (f) {
        apply(f);
      } else {
        poll();
      }
      // Poll often (90s) so a real sky change is reflected live, without a reload.
      // The backend lazily re-fetches the sky so this stays fresh, not hourly.
      setInterval(() => { if (!_forced()) poll(); }, 90 * 1000);
      addEventListener("hashchange", () => {
        const ff = _forced();
        if (ff) apply(ff); else poll();
      });
    },
    // Force a look on demand (voice command). condition is a weather word, so it
    // reuses the same FX + outfit mapping as real weather.
    force(condition: string) { override = condition; apply(condition); },
    // Resume weather-driven outfit.
    auto() { override = null; poll(); },
    _apply: apply,
  };
}
