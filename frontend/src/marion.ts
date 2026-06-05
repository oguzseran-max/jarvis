/**
 * Marion — voice-reactive face avatar for the FR/TR personas.
 *
 * JARVIS's default visualization is the Three.js particle orb (orb.ts). For the
 * French ("Marion") and Turkish personas we instead show a real face: a still
 * portrait that breathes and glows with the TTS audio, reusing the SAME
 * AnalyserNode the orb reads (the audio player's), so it pulses in sync with her
 * voice. This is Phase 1 — a still image with audio-reactive glow/scale, not
 * true lip-sync. (Lip-sync via a streaming avatar API is Phase 2.)
 */

export type MarionState = "idle" | "listening" | "thinking" | "speaking";

export interface Marion {
  setState(s: MarionState): void;
  setAnalyser(a: AnalyserNode | null): void;
  /** Show/hide the face. Hidden = the orb is the active visualization. */
  setVisible(v: boolean): void;
  /**
   * Phase 2 — play a D-ID lip-sync video over the still portrait. The mp4
   * carries Marion's voice, so this is the audio source while it plays.
   * Resolves when playback ends (or fails). `muted` mirrors the mute button.
   */
  playVideo(url: string, muted: boolean): Promise<void>;
  /** Phase 3 — attach a live D-ID WebRTC MediaStream (real-time lip-sync). */
  showLiveStream(stream: MediaStream, muted: boolean): void;
  hideLiveStream(): void;
  /** Mirror the mute button onto the live video's audio. */
  setMuted(muted: boolean): void;
  /** Weather accessory worn by Marion: "umbrella", "sunglasses", or null. */
  setAccessory(kind: "umbrella" | "sunglasses" | null): void;
  /** Swap the still portrait (weather-dependent outfit). */
  setImage(src: string): void;
  destroy(): void;
}

const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);

// Polished sunglasses (inline SVG — crisp at any size). The umbrella is a 3D PNG.
const _SUNGLASSES_SVG = `
<svg viewBox="0 0 200 78" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="mlens" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5a6678"/><stop offset=".4" stop-color="#1b2330"/><stop offset="1" stop-color="#04060b"/>
    </linearGradient>
  </defs>
  <path d="M16 24 L40 30" stroke="#0a0d13" stroke-width="6" stroke-linecap="round"/>
  <path d="M184 24 L160 30" stroke="#0a0d13" stroke-width="6" stroke-linecap="round"/>
  <path d="M36 26 Q100 12 164 26" stroke="#0a0d13" stroke-width="6" fill="none" stroke-linecap="round"/>
  <rect x="34" y="22" width="56" height="40" rx="18" fill="url(#mlens)" stroke="#05070c" stroke-width="3.5"/>
  <rect x="110" y="22" width="56" height="40" rx="18" fill="url(#mlens)" stroke="#05070c" stroke-width="3.5"/>
  <path d="M44 50 Q50 30 74 28" stroke="rgba(255,255,255,.30)" stroke-width="4" fill="none" stroke-linecap="round"/>
  <path d="M120 50 Q126 30 150 28" stroke="rgba(255,255,255,.24)" stroke-width="4" fill="none" stroke-linecap="round"/>
</svg>`;

// JARVIS cyan palette — matches the orb (0x4ca8e8) so the personas feel unified.
const GLOW = "76, 168, 232"; // rgb

const CSS = `
#marion-stage {
  position: fixed; inset: 0; z-index: 1;
  display: none; align-items: center; justify-content: center;
  pointer-events: none;
  /* The live D-ID video has an opaque BLACK background (no alpha). "screen"
     blends it with the orb behind: black → shows the orb, Marion stays visible
     and fused into the energy. Also keys the black off the still cutout. */
  mix-blend-mode: screen;
}
#marion-stage.visible { display: flex; }
.marion-frame {
  position: relative;
  width: min(46vh, 88vw);
  aspect-ratio: 768 / 1344;
  transform: scale(var(--marion-scale, 1));
  transition: transform 0.06s linear;
  will-change: transform;
  /* A touch transparent so she blends into the orb's energy behind her. */
  opacity: 0.9;
}
.marion-glow {
  position: absolute; inset: -14%;
  border-radius: 50%;
  background: radial-gradient(ellipse at center,
    rgba(${GLOW}, 0.55) 0%, rgba(${GLOW}, 0.22) 38%, rgba(${GLOW}, 0) 70%);
  filter: blur(22px);
  opacity: var(--marion-glow, 0.25);
  transform: scale(var(--marion-glow-scale, 1));
  transition: opacity 0.08s linear, transform 0.08s linear;
}
.marion-img {
  position: absolute; inset: 0;
  width: 100%; height: 100%;
  object-fit: contain;
  /* No frame — the transparent cutout floats freely over the orb behind her. */
  filter: brightness(var(--marion-bright, 0.96)) saturate(1.02)
          drop-shadow(0 0 18px rgba(${GLOW}, var(--marion-ring-op, 0.3)));
  transition: filter 0.12s linear;
}
/* Lip-sync video sits on top of the still and fades in while she speaks. */
.marion-video {
  position: absolute; inset: 0;
  width: 100%; height: 100%;
  object-fit: contain;
  opacity: 0;
  transition: opacity 0.25s ease;
  pointer-events: none;
}
.marion-video.playing { opacity: 1; }
/* Weather accessories worn by Marion (emoji overlays positioned over the frame). */
.marion-acc {
  position: absolute; pointer-events: none; opacity: 0; transition: opacity 0.4s ease;
  filter: drop-shadow(0 4px 10px rgba(0,0,0,0.5)); z-index: 2;
}
.marion-acc.show { opacity: 1; }
.marion-acc svg, .marion-acc img { display: block; width: 100%; height: auto; }
/* 3D umbrella PNG — wide, resting on her shoulder (viewer-right), leaning back. */
.marion-acc.umbrella { top: -20%; left: 66%; width: 126%;
  transform: translateX(-50%) rotate(22deg); transform-origin: 46% 64%; }
/* Sunglasses on her eyes (face sits a touch left of centre). */
.marion-acc.sunglasses { top: 14.5%; left: 44%; width: 25%; transform: translateX(-50%); }
/* Slow conic sweep while thinking */
.marion-frame.thinking::after {
  content: ""; position: absolute; inset: -3px;
  border-radius: 22px; padding: 3px;
  background: conic-gradient(from var(--marion-spin, 0deg),
    rgba(${GLOW}, 0) 0%, rgba(${GLOW}, 0.7) 12%, rgba(${GLOW}, 0) 28%);
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor; mask-composite: exclude;
  pointer-events: none;
}
`;

export function createMarion(imgSrc: string): Marion {
  let destroyed = false;
  let visible = false;
  let state: MarionState = "idle";
  let analyser: AnalyserNode | null = null;
  let freqData = new Uint8Array(64);

  // smoothed audio level + decorative spin
  let amp = 0;
  let spin = 0;

  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  const stage = document.createElement("div");
  stage.id = "marion-stage";
  const frame = document.createElement("div");
  frame.className = "marion-frame";
  const glow = document.createElement("div");
  glow.className = "marion-glow";
  const img = document.createElement("img");
  img.className = "marion-img";
  img.src = imgSrc;
  img.alt = "Marion";
  const video = document.createElement("video");
  video.className = "marion-video";
  video.setAttribute("playsinline", "");
  video.preload = "auto";
  const acc = document.createElement("div");
  acc.className = "marion-acc";
  frame.appendChild(glow);
  frame.appendChild(img);
  frame.appendChild(video);
  frame.appendChild(acc);
  stage.appendChild(frame);
  document.body.appendChild(stage);

  let last = performance.now();
  function animate() {
    if (destroyed) return;
    requestAnimationFrame(animate);
    const now = performance.now();
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;

    if (!visible) return; // nothing to draw when the orb is active

    // Same bass/mid read as the orb, condensed into a single level.
    let level = 0;
    if (analyser) {
      analyser.getByteFrequencyData(freqData);
      let bSum = 0, mSum = 0;
      for (let i = 0; i < 8; i++) bSum += freqData[i];
      for (let i = 8; i < 24; i++) mSum += freqData[i];
      const bass = bSum / (8 * 255);
      const mid = mSum / (16 * 255);
      level = clamp01(bass * 0.75 + mid * 0.6);
    }

    // Attack fast, release slow — feels like speech, not a VU meter.
    const k = level > amp ? 0.45 : 0.12;
    amp += (level - amp) * k;

    // Idle/listening breathing so she never looks frozen.
    const t = now / 1000;
    const breath = (Math.sin(t * 1.3) * 0.5 + 0.5);

    let baseGlow: number, scaleK: number, bright: number;
    switch (state) {
      case "speaking":
        baseGlow = 0.4; scaleK = 0.05; bright = 0.98; break;
      case "listening":
        baseGlow = 0.3 + breath * 0.12; scaleK = 0.012; bright = 0.95; break;
      case "thinking":
        baseGlow = 0.28 + breath * 0.1; scaleK = 0.01; bright = 0.9; break;
      default: // idle
        baseGlow = 0.18 + breath * 0.08; scaleK = 0.008; bright = 0.88; break;
    }

    const reactive = state === "speaking" ? amp : amp * 0.4;
    frame.style.setProperty("--marion-scale", (1 + reactive * scaleK).toFixed(4));
    frame.style.setProperty("--marion-glow", (baseGlow + reactive * 0.6).toFixed(3));
    frame.style.setProperty("--marion-glow-scale", (1 + reactive * 0.12).toFixed(4));
    frame.style.setProperty("--marion-bright", (bright + reactive * 0.18).toFixed(3));
    frame.style.setProperty("--marion-ring", `${(reactive * 26).toFixed(1)}px`);
    frame.style.setProperty("--marion-ring-op", (0.35 + reactive * 0.5).toFixed(3));

    spin = (spin + dt * 110) % 360;
    frame.style.setProperty("--marion-spin", `${spin.toFixed(1)}deg`);
  }
  animate();

  return {
    setState(s: MarionState) {
      state = s;
      frame.classList.toggle("thinking", s === "thinking");
    },
    setAnalyser(a: AnalyserNode | null) {
      analyser = a;
      if (a) freqData = new Uint8Array(a.frequencyBinCount);
    },
    setVisible(v: boolean) {
      visible = v;
      stage.classList.toggle("visible", v);
    },
    showLiveStream(stream: MediaStream, muted: boolean) {
      video.srcObject = stream;
      video.muted = muted;
      // Reveal the live video only once it's actually rendering frames — until
      // then the still portrait stays visible underneath (never a blank Marion).
      video.addEventListener("playing", () => video.classList.add("playing"), { once: true });
      video.play().catch(() => {});
    },
    hideLiveStream() {
      video.classList.remove("playing");
      try { video.pause(); } catch {}
      video.srcObject = null;
    },
    setMuted(muted: boolean) {
      video.muted = muted;
    },
    setAccessory(kind: "umbrella" | "sunglasses" | null) {
      acc.className = "marion-acc" + (kind ? ` ${kind} show` : "");
      acc.innerHTML = kind === "umbrella" ? `<img src="/umbrella.png" alt="">`
        : kind === "sunglasses" ? _SUNGLASSES_SVG : "";
    },
    setImage(src: string) {
      if (img.getAttribute("src") !== src) img.src = src;
    },
    playVideo(url: string, muted: boolean) {
      return new Promise<void>((resolve) => {
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          video.classList.remove("playing");
          video.removeEventListener("ended", finish);
          video.removeEventListener("error", finish);
          // Let the fade-out run before clearing the source.
          setTimeout(() => { try { video.pause(); video.removeAttribute("src"); video.load(); } catch {} }, 300);
          resolve();
        };
        video.addEventListener("ended", finish);
        video.addEventListener("error", finish);
        video.muted = muted;
        video.src = url;
        video.classList.add("playing");
        video.play().catch(() => finish());
      });
    },
    destroy() {
      destroyed = true;
      stage.remove();
      style.remove();
    },
  };
}
