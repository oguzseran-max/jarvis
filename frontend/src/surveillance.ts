/**
 * Surveillance — webcam watch mode for Marion.
 *
 * When the backend turns watch mode ON ("active la surveillance"), this opens
 * the Mac webcam and, every ~1.5s, checks for MOTION (cheap frame diff). Only
 * when something moves does it send a JPEG frame to the backend for local face
 * recognition — so the face service isn't hammered and the API/CPU stays idle
 * while the room is empty. The backend greets Leyla / Aylin by name.
 *
 * Privacy: no recording, frames are sent transiently for recognition only; the
 * Mac camera light is lit while active (intentionally visible).
 */

export interface Surveillance {
  start(): Promise<void>;
  stop(): void;
  readonly active: boolean;
}

const SAMPLE_W = 160, SAMPLE_H = 120;   // tiny frame for motion diff
const MOTION_THRESHOLD = 7;             // mean per-pixel luma delta to count as motion
const INTERVAL_MS = 1500;

export function createSurveillance(send: (m: Record<string, unknown>) => void): Surveillance {
  let stream: MediaStream | null = null;
  let video: HTMLVideoElement | null = null;
  let small: HTMLCanvasElement | null = null;
  let smallCtx: CanvasRenderingContext2D | null = null;
  let big: HTMLCanvasElement | null = null;
  let timer = 0;
  let prev: Uint8ClampedArray | null = null;
  let running = false;
  let badge: HTMLElement | null = null;

  function showBadge() {
    if (badge) { badge.style.display = "flex"; return; }
    const b = document.createElement("div");
    b.id = "watch-badge";
    b.innerHTML = `<span class="wb-dot"></span>SURVEILLANCE`;
    const s = document.createElement("style");
    s.textContent = `
      #watch-badge { position: fixed; bottom: 22px; left: 22px; z-index: 5;
        display: flex; align-items: center; gap: 7px; padding: 5px 11px;
        font-family: ui-monospace, Menlo, monospace; font-size: 10px; letter-spacing: 2px;
        color: #ff6b6b; background: rgba(20,6,6,0.7); border: 1px solid rgba(255,80,80,0.4);
        border-radius: 6px; pointer-events: none; }
      #watch-badge .wb-dot { width: 8px; height: 8px; border-radius: 50%; background: #ff3b3b;
        box-shadow: 0 0 8px #ff3b3b; animation: wbblink 1.4s ease-in-out infinite; }
      @keyframes wbblink { 50% { opacity: .25; } }`;
    document.head.appendChild(s);
    document.body.appendChild(b);
    badge = b;
  }

  function motion(cur: Uint8ClampedArray): number {
    if (!prev) return 999;
    let sum = 0, n = 0;
    // sample every 4th pixel's red channel as a luma proxy
    for (let i = 0; i < cur.length; i += 16) {
      sum += Math.abs(cur[i] - prev[i]);
      n++;
    }
    return n ? sum / n : 0;
  }

  function tick() {
    if (!running || !video || !smallCtx || !small) return;
    smallCtx.drawImage(video, 0, 0, SAMPLE_W, SAMPLE_H);
    const cur = smallCtx.getImageData(0, 0, SAMPLE_W, SAMPLE_H).data;
    const m = motion(cur);
    prev = cur;
    if (m >= MOTION_THRESHOLD && big) {
      const bctx = big.getContext("2d");
      if (bctx) {
        bctx.drawImage(video, 0, 0, big.width, big.height);
        const data = big.toDataURL("image/jpeg", 0.72);
        send({ type: "watch_frame", data });
      }
    }
  }

  return {
    get active() { return running; },
    async start() {
      if (running) return;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false,
        });
      } catch (e) {
        console.warn("[surveillance] camera unavailable", e);
        return;
      }
      video = document.createElement("video");
      video.srcObject = stream;
      video.muted = true; video.setAttribute("playsinline", "");
      await video.play().catch(() => {});
      small = document.createElement("canvas"); small.width = SAMPLE_W; small.height = SAMPLE_H;
      smallCtx = small.getContext("2d", { willReadFrequently: true });
      big = document.createElement("canvas"); big.width = 640; big.height = 480;
      prev = null;
      running = true;
      showBadge();
      timer = window.setInterval(tick, INTERVAL_MS);
      console.log("[surveillance] watch mode ON");
    },
    stop() {
      running = false;
      if (timer) { clearInterval(timer); timer = 0; }
      try { stream?.getTracks().forEach((t) => t.stop()); } catch {}
      stream = null; video = null; smallCtx = null; small = null; big = null; prev = null;
      if (badge) badge.style.display = "none";
      console.log("[surveillance] watch mode OFF");
    },
  };
}
