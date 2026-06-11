/**
 * JARVIS — Cinematic boot sequence (Hollywood cut).
 *
 * A real-time, film-grade HUD power-up that replaces the old pre-rendered boot
 * video. Driven by one shared clock (the boot audio):
 *
 *   1. A Three.js scene with UnrealBloom post-processing (depth + glow): a
 *      nebula of soft round particles, a layered igniting reactor core wrapped
 *      in an energy shell, three assembling gimbal rings, a scanning disc,
 *      converging particles that implode then explode, and staggered shock-waves
 *      with a white flash at the climax.
 *   2. An SVG/DOM HUD overlay (crisp): arc-reactor, radial progress, corner
 *      brackets, streaming diagnostics and a bottom "INITIATING SYSTEM" rail.
 *
 * No on-screen captions — the welcome line plays as voice-over only.
 * Everything blooms then dissolves into the live particle orb at the handoff.
 */

import * as THREE from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";

export interface Boot {
  /** Begin the internal render loop (call on the same user gesture as audio). */
  begin(): void;
  /** Feed the audio's currentTime (seconds); drives the whole timeline. */
  setTime(t: number): void;
  /** Anchor the climax (burst) and handoff on musical hits, in seconds. */
  setCue(burst: number, handoff?: number): void;
  /** Fade the whole boot layer out, revealing the orb. */
  fadeOut(): void;
  dispose(): void;
}

// ── tiny math helpers ──────────────────────────────────────────────────────
const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);
const smooth = (x: number) => { x = clamp01(x); return x * x * (3 - 2 * x); };
const ramp = (t: number, a: number, b: number) => clamp01((t - a) / (b - a));
const eramp = (t: number, a: number, b: number) => smooth(ramp(t, a, b));
const lerp = (a: number, b: number, x: number) => a + (b - a) * x;

// Palette — black space, orb-cyan primary, amber ignition accent.
const CYAN = 0x5ac8ff;
const CYAN_SOFT = 0x4ca8e8;
const AMBER = 0xffb24a;

export function createBoot(root: HTMLElement): Boot {
  // ── shared clock: follow audio time, extrapolate between updates ──────────
  let audioTime = 0;
  let audioWall = performance.now();
  let running = false;
  let fading = false;
  let cueBurst = 19;    // climax time (s) — overridden by setCue() to a musical hit
  let cueHandoff = 22.5; // when the boot dissolves into the orb (next beat)
  const now = () => audioTime + (performance.now() - audioWall) / 1000;

  // ── WebGL canvas + bloom composer ─────────────────────────────────────────
  const canvas = document.createElement("canvas");
  canvas.className = "boot-gl";
  root.appendChild(canvas);

  let renderer: THREE.WebGLRenderer | null = null;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setClearColor(0x03040a, 1);
  } catch {
    renderer = null; // No WebGL — the DOM overlay alone still boots.
  }

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(
    50, window.innerWidth / window.innerHeight, 0.1, 1000);
  camera.position.z = 42;

  let composer: EffectComposer | null = null;
  let bloom: UnrealBloomPass | null = null;
  if (renderer) {
    composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    bloom = new UnrealBloomPass(
      new THREE.Vector2(window.innerWidth, window.innerHeight), 0.95, 0.6, 0.18);
    composer.addPass(bloom);
  }

  const world = new THREE.Group();
  scene.add(world);

  const dotTex = makeDotTexture();   // soft round particle sprite
  const glowTex = makeGlowTexture(); // warm radial flare

  // ── nebula particle field (round, glowing, dense) ─────────────────────────
  const NEB = 3200;
  const nebGeo = new THREE.BufferGeometry();
  const nebPos = new Float32Array(NEB * 3);
  for (let i = 0; i < NEB; i++) {
    const r = 16 + Math.random() * 70;
    const th = Math.random() * Math.PI * 2;
    const ph = Math.acos(2 * Math.random() - 1);
    nebPos[i * 3] = r * Math.sin(ph) * Math.cos(th);
    nebPos[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th);
    nebPos[i * 3 + 2] = r * Math.cos(ph) - 30;
  }
  nebGeo.setAttribute("position", new THREE.BufferAttribute(nebPos, 3));
  const nebMat = new THREE.PointsMaterial({
    color: CYAN_SOFT, size: 0.55, map: dotTex, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
  });
  const nebula = new THREE.Points(nebGeo, nebMat);
  world.add(nebula);

  // ── reactor core: layered wireframe + bright sprites ──────────────────────
  const core = new THREE.Mesh(
    new THREE.IcosahedronGeometry(2.4, 1),
    new THREE.MeshBasicMaterial({ color: AMBER, wireframe: true, transparent: true,
      opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }));
  world.add(core);
  const coreMat = core.material as THREE.MeshBasicMaterial;

  const coreInner = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.2, 0),
    new THREE.MeshBasicMaterial({ color: 0xffffff, wireframe: true, transparent: true,
      opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }));
  world.add(coreInner);
  const coreInnerMat = coreInner.material as THREE.MeshBasicMaterial;

  // three stacked flares give the core a soft volumetric glow
  const flares: THREE.Sprite[] = [];
  for (const s of [9, 16, 26]) {
    const m = new THREE.SpriteMaterial({ map: glowTex, color: AMBER, transparent: true,
      opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false });
    const sp = new THREE.Sprite(m);
    sp.scale.set(s, s, 1);
    world.add(sp);
    flares.push(sp);
  }

  // ── energy shell: round particles swirling on a sphere around the core ────
  const SHELL = 900;
  const shellGeo = new THREE.BufferGeometry();
  const shellPos = new Float32Array(SHELL * 3);
  const shellBase = new Float32Array(SHELL * 3);
  for (let i = 0; i < SHELL; i++) {
    const th = Math.random() * Math.PI * 2;
    const ph = Math.acos(2 * Math.random() - 1);
    const x = Math.sin(ph) * Math.cos(th), y = Math.sin(ph) * Math.sin(th), z = Math.cos(ph);
    shellBase[i * 3] = x; shellBase[i * 3 + 1] = y; shellBase[i * 3 + 2] = z;
    shellPos[i * 3] = x; shellPos[i * 3 + 1] = y; shellPos[i * 3 + 2] = z;
  }
  shellGeo.setAttribute("position", new THREE.BufferAttribute(shellPos, 3));
  const shellMat = new THREE.PointsMaterial({
    color: CYAN, size: 0.5, map: dotTex, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
  });
  const shell = new THREE.Points(shellGeo, shellMat);
  world.add(shell);

  // ── three gimbal rings (the classic JARVIS gimbal) ────────────────────────
  type Ring = { mesh: THREE.Mesh; mat: THREE.MeshBasicMaterial;
                axis: THREE.Vector3; speed: number };
  const rings: Ring[] = [];
  const ringRadii = [7.5, 10, 12.8];
  const ringAxes = [
    new THREE.Vector3(1, 0.4, 0).normalize(),
    new THREE.Vector3(0.3, 1, 0.2).normalize(),
    new THREE.Vector3(0.1, 0.4, 1).normalize(),
  ];
  for (let i = 0; i < 3; i++) {
    const geo = new THREE.TorusGeometry(ringRadii[i], 0.06, 14, 220);
    const mat = new THREE.MeshBasicMaterial({
      color: i === 1 ? CYAN : CYAN_SOFT, transparent: true, opacity: 0,
      blending: THREE.AdditiveBlending, depthWrite: false });
    const mesh = new THREE.Mesh(geo, mat);
    const ticks = makeRingTicks(ringRadii[i], i === 1 ? 60 : 30);
    (ticks.material as THREE.LineBasicMaterial).opacity = 0;
    mesh.add(ticks);
    world.add(mesh);
    rings.push({ mesh, mat, axis: ringAxes[i], speed: 0.25 + i * 0.18 });
  }

  // ── scanning disc: radial spokes that sweep ───────────────────────────────
  const discN = 110;
  const discGeo = new THREE.BufferGeometry();
  const discPos = new Float32Array(discN * 2 * 3);
  for (let i = 0; i < discN; i++) {
    const a = (i / discN) * Math.PI * 2;
    const r0 = 3, r1 = 13.5;
    discPos[i * 6] = Math.cos(a) * r0; discPos[i * 6 + 1] = Math.sin(a) * r0; discPos[i * 6 + 2] = 0;
    discPos[i * 6 + 3] = Math.cos(a) * r1; discPos[i * 6 + 4] = Math.sin(a) * r1; discPos[i * 6 + 5] = 0;
  }
  discGeo.setAttribute("position", new THREE.BufferAttribute(discPos, 3));
  const discMat = new THREE.LineBasicMaterial({
    color: CYAN, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false });
  const disc = new THREE.LineSegments(discGeo, discMat);
  world.add(disc);

  // ── convergence particles: implode to feed the core, then explode ─────────
  const CONV = 1500;
  const convGeo = new THREE.BufferGeometry();
  const convPos = new Float32Array(CONV * 3);
  const convHome = new Float32Array(CONV * 3);
  const convDir = new Float32Array(CONV * 3); // explosion direction (unit)
  for (let i = 0; i < CONV; i++) {
    const r = 14 + Math.random() * 18;
    const th = Math.random() * Math.PI * 2;
    const ph = Math.acos(2 * Math.random() - 1);
    const x = r * Math.sin(ph) * Math.cos(th);
    const y = r * Math.sin(ph) * Math.sin(th);
    const z = r * Math.cos(ph);
    convHome[i * 3] = x; convHome[i * 3 + 1] = y; convHome[i * 3 + 2] = z;
    convPos[i * 3] = x; convPos[i * 3 + 1] = y; convPos[i * 3 + 2] = z;
    const inv = 1 / Math.hypot(x, y, z);
    convDir[i * 3] = x * inv; convDir[i * 3 + 1] = y * inv; convDir[i * 3 + 2] = z * inv;
  }
  convGeo.setAttribute("position", new THREE.BufferAttribute(convPos, 3));
  const convMat = new THREE.PointsMaterial({
    color: CYAN, size: 0.6, map: dotTex, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true });
  const conv = new THREE.Points(convGeo, convMat);
  world.add(conv);

  // ── staggered shock-waves ─────────────────────────────────────────────────
  type Wave = { mesh: THREE.Mesh; mat: THREE.MeshBasicMaterial; off: number; reach: number };
  const waves: Wave[] = [];
  const waveOffsets = [0, 0.35, 0.8]; // relative to the climax (cueBurst)
  const waveReach = [70, 55, 90];
  for (let i = 0; i < waveOffsets.length; i++) {
    const mat = new THREE.MeshBasicMaterial({ color: i === 2 ? CYAN_SOFT : CYAN,
      transparent: true, opacity: 0, side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending, depthWrite: false });
    const mesh = new THREE.Mesh(new THREE.RingGeometry(1, 1.18, 120), mat);
    world.add(mesh);
    waves.push({ mesh, mat, off: waveOffsets[i], reach: waveReach[i] });
  }

  // ── climax flash ──────────────────────────────────────────────────────────
  const flashMat = new THREE.SpriteMaterial({ map: glowTex, color: 0xffffff,
    transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false });
  const flash = new THREE.Sprite(flashMat);
  flash.scale.set(140, 140, 1);
  world.add(flash);

  // ── DOM / SVG HUD overlay (no captions) ───────────────────────────────────
  const hud = buildHud();
  root.appendChild(hud.el);

  // ── per-frame timeline ────────────────────────────────────────────────────
  function frame(t: number) {
    const prog = clamp01(t / (cueBurst + 1.5));
    const f = fading ? 0 : 1;

    // nebula: fade in, gentle parallax drift
    nebMat.opacity = lerp(0, 0.6, eramp(t, 0.5, 4)) * f;
    nebula.rotation.y = t * 0.012;
    nebula.rotation.x = Math.sin(t * 0.05) * 0.05;

    // reactor ignition: amber spark grows, then cools toward cyan
    const ignite = eramp(t, 1.2, 4.5);
    const cool = eramp(t, 7, 12);
    const coreCol = new THREE.Color(AMBER).lerp(new THREE.Color(CYAN), cool);
    const pulse = 1 + Math.sin(t * 3.2) * 0.05 + Math.sin(t * 7.1) * 0.025;
    coreMat.color.copy(coreCol);
    coreMat.opacity = lerp(0, 0.95, ignite) * f;
    core.scale.setScalar(lerp(0.2, 1, ignite) * pulse);
    core.rotation.x = t * 0.5; core.rotation.y = t * 0.7;
    coreInnerMat.opacity = lerp(0, 0.9, ignite) * f;
    coreInner.scale.setScalar(lerp(0.1, 1, ignite) * (2 - pulse));
    coreInner.rotation.x = -t * 0.9; coreInner.rotation.z = t * 0.6;
    flares.forEach((sp, i) => {
      (sp.material as THREE.SpriteMaterial).color.copy(coreCol);
      (sp.material as THREE.SpriteMaterial).opacity = lerp(0, [0.55, 0.35, 0.22][i], ignite) * pulse * f;
    });

    // energy shell breathes around the core
    const sp = shellGeo.attributes.position.array as Float32Array;
    const rad = 3.4 + Math.sin(t * 1.6) * 0.25;
    const sw = t * 0.6;
    for (let i = 0; i < SHELL; i++) {
      const bx = shellBase[i * 3], by = shellBase[i * 3 + 1], bz = shellBase[i * 3 + 2];
      // swirl around Y for a living plasma feel
      const cx = Math.cos(sw), sx = Math.sin(sw);
      sp[i * 3] = (bx * cx - bz * sx) * rad;
      sp[i * 3 + 1] = by * rad;
      sp[i * 3 + 2] = (bx * sx + bz * cx) * rad;
    }
    shellGeo.attributes.position.needsUpdate = true;
    shellMat.color.copy(coreCol);
    shellMat.opacity = lerp(0, 0.8, eramp(t, 3, 6)) * (1 - eramp(t, cueBurst - 0.5, cueBurst + 1)) * f;

    // rings assemble, spin, then tighten into the burst
    rings.forEach((rg, i) => {
      const a = eramp(t, 2.4 + i * 0.5, 6 + i * 0.5);
      let op = lerp(0, i === 1 ? 0.9 : 0.65, a);
      rg.mesh.quaternion.setFromAxisAngle(rg.axis, t * rg.speed + i);
      const tighten = eramp(t, cueBurst - 3, cueBurst + 0.3);
      rg.mesh.scale.setScalar(lerp(1, 0.1, tighten));
      op *= (1 - tighten) * f;
      rg.mat.opacity = op;
      const ticks = rg.mesh.children[0] as THREE.LineSegments;
      (ticks.material as THREE.LineBasicMaterial).opacity = op * 0.85;
    });

    // scanning disc
    discMat.opacity = eramp(t, 5.5, 8) * (1 - eramp(t, cueBurst - 4, cueBurst - 1.5)) * 0.45 * f;
    disc.rotation.z = -t * 0.9;
    disc.rotation.x = 1.15 + Math.sin(t * 0.4) * 0.1;

    // convergence particles: implode toward the core, then explode at the burst
    const implode = eramp(t, cueBurst - 6, cueBurst);
    const explode = ramp(t, cueBurst, cueBurst + 3);
    const cp = convGeo.attributes.position.array as Float32Array;
    const expDist = smooth(explode) * 80;
    for (let i = 0; i < CONV; i++) {
      const k = lerp(1, 0.04, implode);
      cp[i * 3] = convHome[i * 3] * k + convDir[i * 3] * expDist;
      cp[i * 3 + 1] = convHome[i * 3 + 1] * k + convDir[i * 3 + 1] * expDist;
      cp[i * 3 + 2] = convHome[i * 3 + 2] * k + convDir[i * 3 + 2] * expDist;
    }
    convGeo.attributes.position.needsUpdate = true;
    conv.rotation.y = t * 0.3; conv.rotation.x = t * 0.15;
    convMat.opacity = (lerp(0, 0.85, eramp(t, 11, 14)) * (1 - ramp(t, cueBurst + 1.5, cueBurst + 3.5))) * f;

    // shock-waves
    for (const w of waves) {
      const at = cueBurst + w.off;
      const p = ramp(t, at, at + 2.0);
      if (p > 0 && p < 1) {
        const s = lerp(1, w.reach, smooth(p));
        w.mesh.scale.set(s, s, s);
        w.mat.opacity = (1 - p) * 0.85 * f;
      } else {
        w.mat.opacity = 0;
      }
    }

    // climax flash — a quick (softened) white bloom right at the burst
    const fl = Math.max(0, 1 - Math.abs(t - (cueBurst + 0.12)) / 0.55);
    flashMat.opacity = fl * fl * 0.5 * f;
    flash.scale.setScalar(lerp(70, 150, fl));

    // cinematic camera: drift, push-in, a short shake at the burst
    const push = eramp(t, cueBurst - 3, cueBurst + 2);
    const shake = Math.max(0, 1 - Math.abs(t - (cueBurst + 0.2)) / 0.6);
    camera.position.z = lerp(42, 28, push) + Math.sin(t * 0.25) * 1.2;
    camera.position.x = Math.sin(t * 0.18) * 1.6 + (Math.random() - 0.5) * shake * 1.4;
    camera.position.y = Math.cos(t * 0.21) * 1.0 + (Math.random() - 0.5) * shake * 1.4;
    camera.lookAt(0, 0, 0);
    world.rotation.z = Math.sin(t * 0.08) * 0.04;

    // bloom swells through the build-up and peaks at the burst
    if (bloom) bloom.strength = 0.85 + eramp(t, cueBurst - 7, cueBurst) * 0.7 + fl * 0.45;

    // hand off to the orb: bloom out, everything dims (aligned to the handoff beat)
    if (t > cueHandoff) {
      const out = ramp(t, cueHandoff, cueHandoff + 3.5);
      world.scale.setScalar(lerp(1, 1.5, out));
      scene.traverse((o) => {
        const m = (o as THREE.Mesh).material as THREE.Material | undefined;
        if (m && "opacity" in m) (m as THREE.Material & { opacity: number }).opacity *= (1 - out);
      });
    }

    hud.update(t, prog, cueBurst, cueHandoff);
    if (composer) composer.render();
    else if (renderer) renderer.render(scene, camera);
  }

  function loop() {
    if (!running) return;
    frame(now());
    requestAnimationFrame(loop);
  }

  function onResize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    if (renderer) renderer.setSize(window.innerWidth, window.innerHeight);
    if (composer) composer.setSize(window.innerWidth, window.innerHeight);
  }
  window.addEventListener("resize", onResize);

  return {
    begin() {
      if (running) return;
      running = true;
      audioWall = performance.now();
      hud.el.classList.add("live");
      requestAnimationFrame(loop);
    },
    setTime(t: number) { audioTime = t; audioWall = performance.now(); },
    setCue(burst: number, handoff?: number) {
      if (burst > 6 && burst < 40) cueBurst = burst;
      cueHandoff = handoff && handoff > cueBurst ? handoff : cueBurst + 3.5;
    },
    fadeOut() { fading = true; },
    dispose() {
      running = false;
      window.removeEventListener("resize", onResize);
      if (composer) composer.dispose();
      if (renderer) renderer.dispose();
      canvas.remove();
      hud.el.remove();
    },
  };
}

// ── HUD overlay (SVG + DOM), updated each frame — no captions ────────────────
function buildHud() {
  const SVGNS = "http://www.w3.org/2000/svg";
  const el = document.createElement("div");
  el.className = "boot-hud";

  for (const c of ["tl", "tr", "bl", "br"]) {
    const b = document.createElement("div");
    b.className = `boot-bracket ${c}`;
    el.appendChild(b);
  }

  // cinematic finishing: letterbox bars + film grain (vignette is on .boot-hud)
  for (const edge of ["top", "bottom"]) {
    const bar = document.createElement("div");
    bar.className = `boot-bar ${edge}`;
    el.appendChild(bar);
  }
  const grain = document.createElement("div");
  grain.className = "boot-grain";
  el.appendChild(grain);

  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", "0 0 400 400");
  svg.setAttribute("class", "boot-reactor");
  const cx = 200, cy = 200;
  const mk = (tag: string, attrs: Record<string, string | number>) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const k in attrs) n.setAttribute(k, String(attrs[k]));
    return n;
  };
  svg.appendChild(mk("circle", { cx, cy, r: 150, fill: "none", stroke: "rgba(90,200,255,0.14)", "stroke-width": 1 }));
  svg.appendChild(mk("circle", { cx, cy, r: 120, fill: "none", stroke: "rgba(90,200,255,0.10)", "stroke-width": 1, "stroke-dasharray": "2 6" }));
  const seg = mk("g", { class: "boot-seg" });
  for (let i = 0; i < 36; i++) {
    const a = (i / 36) * Math.PI * 2;
    const r1 = 132, r2 = i % 3 === 0 ? 146 : 140;
    seg.appendChild(mk("line", {
      x1: cx + Math.cos(a) * r1, y1: cy + Math.sin(a) * r1,
      x2: cx + Math.cos(a) * r2, y2: cy + Math.sin(a) * r2,
      stroke: "rgba(120,210,255,0.5)", "stroke-width": i % 3 === 0 ? 2 : 1 }));
  }
  svg.appendChild(seg);
  const seg2 = mk("g", { class: "boot-seg2" });
  for (let i = 0; i < 60; i++) {
    const a = (i / 60) * Math.PI * 2;
    seg2.appendChild(mk("line", {
      x1: cx + Math.cos(a) * 104, y1: cy + Math.sin(a) * 104,
      x2: cx + Math.cos(a) * 110, y2: cy + Math.sin(a) * 110,
      stroke: "rgba(120,210,255,0.35)", "stroke-width": 1 }));
  }
  svg.appendChild(seg2);
  const R = 165, C = 2 * Math.PI * R;
  const defs = mk("defs", {});
  const grad = mk("linearGradient", { id: "bootgrad", x1: "0", y1: "0", x2: "1", y2: "1" });
  grad.appendChild(mk("stop", { offset: "0", "stop-color": "#5ac8ff" }));
  grad.appendChild(mk("stop", { offset: "1", "stop-color": "#ffb24a" }));
  defs.appendChild(grad);
  svg.appendChild(defs);
  svg.appendChild(mk("circle", { cx, cy, r: R, fill: "none", stroke: "rgba(90,200,255,0.10)", "stroke-width": 3 }));
  const arc = mk("circle", {
    cx, cy, r: R, fill: "none", stroke: "url(#bootgrad)", "stroke-width": 3,
    "stroke-linecap": "round", "stroke-dasharray": C,
    "stroke-dashoffset": C, transform: `rotate(-90 ${cx} ${cy})` }) as SVGCircleElement;
  svg.appendChild(arc);
  el.appendChild(svg);

  const scan = document.createElement("div");
  scan.className = "boot-scan";
  el.appendChild(scan);

  const colL = document.createElement("div");
  colL.className = "boot-col left";
  const colR = document.createElement("div");
  colR.className = "boot-col right";
  el.appendChild(colL);
  el.appendChild(colR);

  const rail = document.createElement("div");
  rail.className = "boot-rail";
  rail.innerHTML =
    `<span class="boot-rail-title">INITIATING SYSTEM</span>` +
    `<span class="boot-bar"><i></i></span>` +
    `<span class="boot-tokens">` +
    `<b data-at="0.30">CHECKSUM</b><b data-at="0.55">MEMORY</b>` +
    `<b data-at="0.78">NEURAL NET</b><b data-at="0.95">CORE ONLINE</b>` +
    `</span>`;
  el.appendChild(rail);
  const barFill = rail.querySelector(".boot-bar i") as HTMLElement;
  const tokens = Array.from(rail.querySelectorAll<HTMLElement>(".boot-tokens b"));

  const LABELS = ["DIAGNOSTIC", "SUBSYSTEM", "MEM SECTOR", "I/O BUS", "VISION",
    "AUDIO DSP", "NAV CORE", "POWER CELL", "UPLINK", "HEURISTIC", "KERNEL", "SENSOR"];
  const hex = () => Math.floor(Math.random() * 0xffff).toString(16).padStart(4, "0").toUpperCase();
  function pushLine(col: HTMLElement) {
    const line = document.createElement("div");
    line.className = "boot-line";
    const ok = Math.random() > 0.08;
    line.innerHTML = `<span>${LABELS[(Math.random() * LABELS.length) | 0]} 0x${hex()}</span>` +
      `<em class="${ok ? "ok" : "wait"}">${ok ? "OK" : "···"}</em>`;
    col.appendChild(line);
    while (col.childElementCount > 9) col.firstChild!.remove();
  }

  let lastLine = 0;

  function update(t: number, prog: number, cueBurst: number, cueHandoff: number) {
    arc.setAttribute("stroke-dashoffset", String(C * (1 - prog)));
    barFill.style.width = (prog * 100).toFixed(1) + "%";
    for (const tk of tokens) {
      const at = parseFloat(tk.dataset.at || "1");
      tk.classList.toggle("on", prog >= at);
    }
    const diagActive = t > 4 && t < cueBurst - 1;
    el.classList.toggle("diag", diagActive);
    if (diagActive && t - lastLine > 0.1) {
      lastLine = t;
      pushLine(Math.random() > 0.5 ? colL : colR);
    }
    if (t > cueHandoff - 0.3) el.classList.add("out");
  }

  return { el, update };
}

// ── procedural textures / geometry helpers ───────────────────────────────────
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
  grd.addColorStop(0.25, "rgba(255,225,170,0.7)");
  grd.addColorStop(1, "rgba(255,210,140,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.needsUpdate = true;
  return tex;
}

function makeRingTicks(radius: number, count: number): THREE.LineSegments {
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(count * 2 * 3);
  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2;
    const r0 = radius - 0.4, r1 = radius + 0.4;
    pos[i * 6] = Math.cos(a) * r0; pos[i * 6 + 1] = Math.sin(a) * r0; pos[i * 6 + 2] = 0;
    pos[i * 6 + 3] = Math.cos(a) * r1; pos[i * 6 + 4] = Math.sin(a) * r1; pos[i * 6 + 5] = 0;
  }
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.LineBasicMaterial({
    color: CYAN, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false });
  return new THREE.LineSegments(geo, mat);
}
