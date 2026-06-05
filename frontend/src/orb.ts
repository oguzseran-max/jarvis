/**
 * JARVIS — Multi-mode particle visualization (cinematic cut).
 *
 * A glowing energy orb: a round-particle cloud with a bright living core,
 * connection filaments and travelling electrons, all lit by UnrealBloom so it
 * reads like a real light source. Audio-reactive (bass swells the cloud and the
 * core; mid adds shimmer) and state-aware (idle / listening / thinking /
 * speaking). `ignite()` blooms it into existence for the boot handoff.
 */

import * as THREE from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";

export type OrbState = "idle" | "listening" | "thinking" | "speaking";

export interface Orb {
  setState(s: OrbState): void;
  setAnalyser(a: AnalyserNode | null): void;
  /** Bloom the orb into existence — used for the boot → orb handoff. */
  ignite(): void;
  /** Dim the bright core while "speaking" — used in Marion mode so the orb
   *  glow behind her doesn't wash her out as she talks. */
  setDimOnSpeak(v: boolean): void;
  destroy(): void;
}

const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);

export function createOrb(canvas: HTMLCanvasElement): Orb {
  let destroyed = false;
  const N = 2600;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  // Transparent clear so the orb can be shrunk behind Marion (FR/TR) without an
  // opaque black box — the page background (#050508 ≈ this colour) shows through.
  renderer.setClearColor(0x04050a, 0);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 1, 1000);
  camera.position.z = 80;

  // ── bloom composer ──
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(
    new THREE.Vector2(window.innerWidth, window.innerHeight), 0.6, 0.55, 0.2);
  composer.addPass(bloom);

  const group = new THREE.Group();
  scene.add(group);

  const dotTex = makeDotTexture();
  const glowTex = makeGlowTexture();

  // ── Particles (round, glowing) ──
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(N * 3);
  const vel = new Float32Array(N * 3);
  const phase = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    const r = Math.pow(Math.random(), 0.5) * 25;
    pos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    pos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    pos[i * 3 + 2] = r * Math.cos(phi);
    phase[i] = Math.random() * 1000;
  }
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.PointsMaterial({
    color: 0x4ca8e8, size: 0.5, map: dotTex, transparent: true, opacity: 0.7,
    sizeAttenuation: true, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const points = new THREE.Points(geo, mat);
  group.add(points);

  // ── Living core: a bright nucleus of dense particles + a soft glow flare ──
  const CORE = 260;
  const coreGeo = new THREE.BufferGeometry();
  const corePos = new Float32Array(CORE * 3);
  const coreBase = new Float32Array(CORE * 3);
  for (let i = 0; i < CORE; i++) {
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    const r = Math.pow(Math.random(), 0.7) * 5;
    const x = r * Math.sin(phi) * Math.cos(theta);
    const y = r * Math.sin(phi) * Math.sin(theta);
    const z = r * Math.cos(phi);
    coreBase[i * 3] = x; coreBase[i * 3 + 1] = y; coreBase[i * 3 + 2] = z;
    corePos[i * 3] = x; corePos[i * 3 + 1] = y; corePos[i * 3 + 2] = z;
  }
  coreGeo.setAttribute("position", new THREE.BufferAttribute(corePos, 3));
  const coreMat = new THREE.PointsMaterial({
    color: 0xbfe6ff, size: 0.7, map: dotTex, transparent: true, opacity: 0.9,
    sizeAttenuation: true, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const coreParticles = new THREE.Points(coreGeo, coreMat);
  group.add(coreParticles);

  const coreGlowMat = new THREE.SpriteMaterial({
    map: glowTex, color: 0x6ec4ff, transparent: true, opacity: 0.5,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const coreGlow = new THREE.Sprite(coreGlowMat);
  coreGlow.scale.set(22, 22, 1);
  group.add(coreGlow);

  // ── Connection filaments ──
  const MAX_LINES = 8000;
  const linePos = new Float32Array(MAX_LINES * 6);
  const lineGeo = new THREE.BufferGeometry();
  lineGeo.setAttribute("position", new THREE.BufferAttribute(linePos, 3));
  lineGeo.setDrawRange(0, 0);
  const lineMat = new THREE.LineBasicMaterial({
    color: 0x4ca8e8, transparent: true, opacity: 0.0,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const lines = new THREE.LineSegments(lineGeo, lineMat);
  group.add(lines);

  // ── Electrons — bright dots that travel along connections ──
  const MAX_ELECTRONS = 200;
  const electronGeo = new THREE.BufferGeometry();
  const electronPos = new Float32Array(MAX_ELECTRONS * 3);
  electronGeo.setAttribute("position", new THREE.BufferAttribute(electronPos, 3));
  electronGeo.setDrawRange(0, 0);
  const electronMat = new THREE.PointsMaterial({
    color: 0xffffff, size: 1.1, map: dotTex, transparent: true, opacity: 1.0,
    sizeAttenuation: true, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const electrons = new THREE.Points(electronGeo, electronMat);
  group.add(electrons);

  interface Electron { sx: number; sy: number; sz: number; ex: number; ey: number; ez: number; t: number; speed: number; }
  const activeElectrons: Electron[] = [];
  let electronSpawnRate = 0;
  let targetElectronRate = 0;
  let lastElectronSpawn = 0;
  let activeConnections: { x1: number; y1: number; z1: number; x2: number; y2: number; z2: number }[] = [];

  // ── State ──
  let state: OrbState = "idle";
  let targetRadius = 25, currentRadius = 25;
  let targetSpeed = 0.3, currentSpeed = 0.3;
  let targetBright = 0.6, currentBright = 0.6;
  let targetSize = 0.4, currentSize = 0.4;
  let lineAmount = 0, targetLineAmount = 0;
  const lineDistance = 8;

  let spinX = 0, spinY = 0, spinZ = 0;
  let transitionEnergy = 0;
  let lastState: OrbState = "idle";
  let cloudZ = 0, cloudZVel = 0;

  // ignite (handoff) flash
  let igniteAt = -100;

  // ── Audio ──
  let analyser: AnalyserNode | null = null;
  let freqData = new Uint8Array(64);
  let bass = 0, mid = 0;

  // Core-dim (Marion mode): smoothly fade the bright core down while speaking.
  let dimOnSpeak = false;
  let coreDim = 1;

  const baseCol = new THREE.Color(0x4ca8e8);
  const coreCol = new THREE.Color(0x6ec4ff);
  const clock = new THREE.Clock();

  function animate() {
    if (destroyed) return;
    requestAnimationFrame(animate);
    const t = clock.getElapsedTime();

    // handoff flash: decays over ~1.6s after ignite()
    const flash = Math.max(0, 1 - (t - igniteAt) / 1.6);

    let targetColHex = 0x4ca8e8, targetCoreHex = 0x6ec4ff;
    switch (state) {
      case "idle":
        targetRadius = 28; targetSpeed = 0.2; targetBright = 0.55; targetSize = 0.42;
        targetLineAmount = 0.15; targetElectronRate = 0; targetColHex = 0x4ca8e8; targetCoreHex = 0x5ab8f0; break;
      case "listening":
        targetRadius = 22; targetSpeed = 0.3; targetBright = 0.7; targetSize = 0.46;
        targetLineAmount = 0.4; targetElectronRate = 0; targetColHex = 0x57b6f0; targetCoreHex = 0x8fd4ff; break;
      case "thinking":
        targetRadius = 16; targetSpeed = 0.5; targetBright = 0.78; targetSize = 0.34;
        targetLineAmount = 1.0; targetElectronRate = 0.015; targetColHex = 0x6ec4ff; targetCoreHex = 0xbfe6ff; break;
      case "speaking":
        targetRadius = 18; targetSpeed = 0.22; targetBright = 0.78; targetSize = 0.46;
        targetLineAmount = 0.8; targetElectronRate = 0; targetColHex = 0x5ab8f0; targetCoreHex = 0xa6dcff; break;
    }
    // Marion mode: keep the orb expanded while she speaks (don't contract the
    // cloud) so it reads as a big energy core behind her, not a small one.
    if (dimOnSpeak && state === "speaking") { targetRadius = 30; targetSize = 0.5; }

    currentRadius += (targetRadius - currentRadius) * 0.02;
    currentSpeed += (targetSpeed - currentSpeed) * 0.02;
    currentBright += (targetBright - currentBright) * 0.02;
    currentSize += (targetSize - currentSize) * 0.02;
    lineAmount += (targetLineAmount - lineAmount) * 0.02;
    electronSpawnRate += (targetElectronRate - electronSpawnRate) * 0.02;

    if (state !== lastState) { transitionEnergy = 1.0; lastState = state; }
    transitionEnergy *= 0.985;
    if (transitionEnergy > 0.05) {
      spinX += transitionEnergy * 0.012 * Math.sin(t * 1.7);
      spinY += transitionEnergy * 0.015;
      spinZ += transitionEnergy * 0.008 * Math.cos(t * 1.3);
    }
    spinY += 0.0006; // gentle constant drift so the orb always feels alive

    // Audio
    bass = 0; mid = 0;
    if (analyser) {
      analyser.getByteFrequencyData(freqData);
      let bSum = 0, mSum = 0;
      for (let i = 0; i < 8; i++) bSum += freqData[i];
      for (let i = 8; i < 24; i++) mSum += freqData[i];
      bass = bSum / (8 * 255); mid = mSum / (16 * 255);
    }

    // Depth Z breathing
    let zTarget = Math.sin(t * 0.12) * 8;
    if (state === "thinking") zTarget = Math.sin(t * 0.3) * 15 + Math.sin(t * 0.9) * 6;
    else if (state === "speaking") zTarget = Math.sin(t * 0.15) * 6 - bass * 10;
    cloudZVel += (zTarget - cloudZ) * 0.008;
    cloudZVel *= 0.94;
    cloudZ += cloudZVel;

    group.rotation.set(spinX, spinY, spinZ);
    group.position.z = cloudZ;
    // ignite settle: the whole orb eases in from slightly expanded + bright
    group.scale.setScalar(1 + flash * 0.35);

    // ── Update particles ──
    const p = geo.getAttribute("position") as THREE.BufferAttribute;
    const a = p.array as Float32Array;
    for (let i = 0; i < N; i++) {
      const i3 = i * 3;
      const x = a[i3], y = a[i3 + 1], z = a[i3 + 2];
      const px = phase[i];
      vel[i3] += Math.sin(t * 0.05 + px) * 0.001 * currentSpeed;
      vel[i3 + 1] += Math.cos(t * 0.06 + px * 1.3) * 0.001 * currentSpeed;
      vel[i3 + 2] += Math.sin(t * 0.055 + px * 0.7) * 0.001 * currentSpeed;
      vel[i3] += Math.sin(t * 0.02 + px * 2.1 + y * 0.1) * 0.0008 * currentSpeed;
      vel[i3 + 1] += Math.cos(t * 0.025 + px * 1.7 + z * 0.1) * 0.0008 * currentSpeed;
      vel[i3 + 2] += Math.sin(t * 0.022 + px * 0.9 + x * 0.1) * 0.0008 * currentSpeed;

      const dist = Math.sqrt(x * x + y * y + z * z) || 0.01;
      const pull = Math.max(0, dist - currentRadius) * 0.002 + 0.0003;
      vel[i3] -= (x / dist) * pull;
      vel[i3 + 1] -= (y / dist) * pull;
      vel[i3 + 2] -= (z / dist) * pull;

      if (bass > 0.05) {
        vel[i3] += (x / dist) * bass * 0.02;
        vel[i3 + 1] += (y / dist) * bass * 0.02;
        vel[i3 + 2] += (z / dist) * bass * 0.02;
      }
      if (state === "speaking" && mid > 0.1) {
        const pulse = Math.sin(t * 8 + px);
        vel[i3] += (x / dist) * mid * 0.012 * pulse;
        vel[i3 + 1] += (y / dist) * mid * 0.012 * pulse;
      }

      vel[i3] *= 0.992; vel[i3 + 1] *= 0.992; vel[i3 + 2] *= 0.992;
      a[i3] += vel[i3]; a[i3 + 1] += vel[i3 + 1]; a[i3 + 2] += vel[i3 + 2];
    }
    p.needsUpdate = true;

    // ── Living core: gentle swirl + bass-driven breathing ──
    const cp = coreGeo.getAttribute("position") as THREE.BufferAttribute;
    const ca = cp.array as Float32Array;
    const coreBreath = 1 + Math.sin(t * 1.4) * 0.08 + bass * 0.6 + flash * 0.5;
    const sw = t * 0.5;
    const cs = Math.cos(sw), ss = Math.sin(sw);
    for (let i = 0; i < CORE; i++) {
      const bx = coreBase[i * 3], by = coreBase[i * 3 + 1], bz = coreBase[i * 3 + 2];
      ca[i * 3] = (bx * cs - bz * ss) * coreBreath;
      ca[i * 3 + 1] = by * coreBreath;
      ca[i * 3 + 2] = (bx * ss + bz * cs) * coreBreath;
    }
    cp.needsUpdate = true;

    // ── Update lines ──
    if (lineAmount > 0.01) {
      const lp = lineGeo.getAttribute("position") as THREE.BufferAttribute;
      const la = lp.array as Float32Array;
      let lineCount = 0;
      const maxDist = lineDistance * (1 + bass * 0.5);
      const maxDistSq = maxDist * maxDist;
      const step = Math.max(1, Math.floor(N / 600));
      for (let i = 0; i < N && lineCount < MAX_LINES; i += step) {
        const i3 = i * 3;
        const x1 = a[i3], y1 = a[i3 + 1], z1 = a[i3 + 2];
        for (let j = i + step; j < N && lineCount < MAX_LINES; j += step) {
          const j3 = j * 3;
          const dx = a[j3] - x1, dy = a[j3 + 1] - y1, dz = a[j3 + 2] - z1;
          if (dx * dx + dy * dy + dz * dz < maxDistSq) {
            const idx = lineCount * 6;
            la[idx] = x1; la[idx + 1] = y1; la[idx + 2] = z1;
            la[idx + 3] = a[j3]; la[idx + 4] = a[j3 + 1]; la[idx + 5] = a[j3 + 2];
            lineCount++;
          }
        }
      }
      lineGeo.setDrawRange(0, lineCount * 2);
      lp.needsUpdate = true;
      lineMat.opacity = lineAmount * 0.1;
      activeConnections = [];
      for (let c = 0; c < Math.min(lineCount, 500); c++) {
        const ci = c * 6;
        activeConnections.push({
          x1: la[ci], y1: la[ci + 1], z1: la[ci + 2],
          x2: la[ci + 3], y2: la[ci + 4], z2: la[ci + 5],
        });
      }
    } else {
      lineGeo.setDrawRange(0, 0);
      activeConnections = [];
    }

    // ── Electrons — only during thinking ──
    if (activeConnections.length > 0 && electronSpawnRate > 0.005) {
      if (activeElectrons.length < 3 && (t - lastElectronSpawn) > 1.0) {
        const conn = activeConnections[Math.floor(Math.random() * activeConnections.length)];
        activeElectrons.push({
          sx: conn.x1, sy: conn.y1, sz: conn.z1,
          ex: conn.x2, ey: conn.y2, ez: conn.z2,
          t: 0, speed: 0.003 + Math.random() * 0.003,
        });
        lastElectronSpawn = t;
      }
    }
    const ep = electronGeo.getAttribute("position") as THREE.BufferAttribute;
    const ea = ep.array as Float32Array;
    let aliveCount = 0;
    for (let e = activeElectrons.length - 1; e >= 0; e--) {
      const el = activeElectrons[e];
      el.t += el.speed;
      if (el.t >= 1) { activeElectrons.splice(e, 1); continue; }
      const ei = aliveCount * 3;
      ea[ei] = el.sx + (el.ex - el.sx) * el.t;
      ea[ei + 1] = el.sy + (el.ey - el.sy) * el.t;
      ea[ei + 2] = el.sz + (el.ez - el.sz) * el.t;
      aliveCount++;
    }
    electronGeo.setDrawRange(0, aliveCount);
    ep.needsUpdate = true;

    // ── Looks ──
    mat.opacity = currentBright + bass * 0.1 + flash * 0.28;
    mat.size = currentSize + bass * 0.06;
    baseCol.lerp(new THREE.Color(targetColHex), 0.02);
    coreCol.lerp(new THREE.Color(targetCoreHex), 0.02);
    mat.color.copy(baseCol);
    lineMat.color.copy(baseCol);
    // Ease the core dim toward its target (only dips while speaking in Marion mode).
    // Keep the orb BIG when speaking — only gently soften the bright nucleus so it
    // doesn't wash Marion out; size and bloom stay full.
    const dimTarget = (dimOnSpeak && state === "speaking") ? 0.7 : 1.0;
    coreDim += (dimTarget - coreDim) * 0.1;

    coreMat.color.copy(coreCol);
    coreMat.opacity = (0.85 + bass * 0.15 + flash * 0.15) * coreDim;
    coreGlowMat.color.copy(coreCol);
    coreGlowMat.opacity = (0.35 + Math.sin(t * 1.4) * 0.05 + bass * 0.4 + flash * 0.8) * coreDim;
    coreGlow.scale.setScalar(20 + bass * 14 + flash * 24);

    // bloom swells with the core and spikes (softly) on ignite
    bloom.strength = 0.55 + bass * 0.5 + flash * 1.1;

    camera.position.x = Math.sin(t * 0.02) * 5;
    camera.position.y = Math.cos(t * 0.03) * 3;
    camera.lookAt(0, 0, cloudZ * 0.2);

    composer.render();
  }

  function onResize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
    composer.setSize(window.innerWidth, window.innerHeight);
  }

  window.addEventListener("resize", onResize);
  animate();

  return {
    setState(s: OrbState) { state = s; },
    setAnalyser(a: AnalyserNode | null) {
      analyser = a;
      if (a) freqData = new Uint8Array(a.frequencyBinCount);
    },
    ignite() { igniteAt = clock.getElapsedTime(); transitionEnergy = 1.0; },
    setDimOnSpeak(v: boolean) { dimOnSpeak = v; },
    destroy() {
      destroyed = true;
      window.removeEventListener("resize", onResize);
      composer.dispose();
      renderer.dispose();
    },
  };
}

// ── procedural textures ──
function makeDotTexture(): THREE.Texture {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d")!;
  const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.4, "rgba(255,255,255,0.85)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.beginPath();
  g.arc(32, 32, 32, 0, Math.PI * 2);
  g.fill();
  const tex = new THREE.CanvasTexture(c);
  tex.needsUpdate = true;
  return tex;
}

function makeGlowTexture(): THREE.Texture {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d")!;
  const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.3, "rgba(170,220,255,0.55)");
  grd.addColorStop(1, "rgba(110,196,255,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.needsUpdate = true;
  return tex;
}
