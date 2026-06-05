/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type OrbState } from "./orb";
import { createMarion } from "./marion";
import { createMarionStream, type MarionStream } from "./did_stream";
import { createBuildHud } from "./build_hud";
import { createWeather } from "./weather";
import { createBoot } from "./boot";
import { createCornerHud } from "./hud";
import { createSecurityHud } from "./security_hud";
import { createLearningHud } from "./learning_hud";
import { createSurveillance } from "./surveillance";
import { createAudioPlayer } from "./voice";
import { createAudioCapture } from "./audio_capture";
import { createSocket } from "./ws";
import { captureCameraFrame } from "./camera";
import { openSettings, checkFirstTimeSetup } from "./settings";
import "./style.css";


// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking";
let currentState: State = "idle";
let isMuted = false;
let bootActive = true; // during the startup boot video — suppress greeting + mic
let currentLang = "fr"; // active language (drives boot audio + recognition)
let currentLook = "default"; // weather-dependent outfit: "default" | "rain" | "sun"
const LOOK_IMG: Record<string, string> = {
  default: "/marion-cutout.png", rain: "/marion-rain.png", sun: "/marion-sun.png",
};
let awaitingBriefing = false; // mic stays off until the post-boot briefing finishes

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => {
    errorEl.style.opacity = "0";
  }, 5000);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "listening...",
    thinking: "thinking...",
    speaking: "",
  };
  statusEl.textContent = labels[state];
}

// ---------------------------------------------------------------------------
// Init components
// ---------------------------------------------------------------------------

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);
// Marion — the FR/TR personas get a real face instead of the orb. The face
// reacts to the same TTS audio as the orb (wired below). Hidden by default
// (English starts on the orb); setLanguage() flips between face and orb.
const marion = createMarion("/marion-cutout.png");
// Languages that show Marion's face rather than the orb.
const FACE_LANGS = new Set(["fr", "tr"]);
const cornerHud = createCornerHud(); // persistent 4-corner monitoring panels
const securityHud = createSecurityHud(); // mid-left live WatchGuard threat panel
const learningHud = createLearningHud(); // mid-right live Self-Evolution panel
const buildHud = createBuildHud(); // middle-right progress panel for background builds

// Live gate camera (DoorBird) — a small panel bottom-left, PLUS a centre-screen
// "spotlight" that surges the live view to the middle of the screen when someone
// rings (Hollywood zoom). The MJPEG stream is proxied by the backend.
const gateCam = (function gateCam() {
  const C = "76, 168, 232";
  const VIDEO = "/api/doorbird/video";
  const style = document.createElement("style");
  style.textContent = `
    #gate-cam { position: fixed; bottom: 22px; left: 250px; z-index: 4; width: 250px;
      border: 1px solid rgba(${C},0.35); border-radius: 8px; overflow: hidden;
      background: #05070c; box-shadow: 0 0 18px rgba(${C},0.15); pointer-events: none;
      font-family: ui-monospace, Menlo, monospace; }
    #gate-cam .gc-head { font-size: 9px; letter-spacing: 1.5px; color: rgba(${C},0.9);
      padding: 3px 8px; text-transform: uppercase; display: flex; align-items: center; gap: 6px;
      border-bottom: 1px solid rgba(${C},0.2); }
    #gate-cam .gc-dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e;
      box-shadow: 0 0 6px #22c55e; animation: gcblink 2s ease-in-out infinite; }
    @keyframes gcblink { 50% { opacity: .3; } }
    #gate-cam img { display: block; width: 100%; height: 92px; object-fit: cover; background:#05070c; }

    /* ── Centre-screen spotlight (on ring) ───────────────────────────────── */
    #gate-spot { position: fixed; left: 50%; top: 62%; z-index: 60;
      width: min(880px, 90vw); pointer-events: auto; cursor: pointer;
      transform: translate(-50%,-50%) scale(0.82) rotateX(8deg);
      opacity: 0; visibility: hidden;
      transition: opacity .45s ease, transform .7s cubic-bezier(.16,.9,.2,1.25), visibility .45s;
      border: 1px solid rgba(${C},0.65); border-radius: 14px; overflow: hidden;
      background: #04070d; font-family: ui-monospace, Menlo, monospace;
      box-shadow: 0 0 70px rgba(${C},0.5), inset 0 0 0 1px rgba(${C},0.25); }
    #gate-spot.show { opacity: 1; visibility: visible;
      transform: translate(-50%,-50%) scale(1) rotateX(0deg); }
    #gate-spot .gs-head { display: flex; align-items: center; gap: 9px;
      font-size: 12px; letter-spacing: 3px; text-transform: uppercase;
      color: #ff5b5b; padding: 9px 14px; border-bottom: 1px solid rgba(${C},0.22);
      background: linear-gradient(90deg, rgba(255,60,60,0.12), transparent); }
    #gate-spot .gs-dot { width: 9px; height: 9px; border-radius: 50%; background: #ff3b3b;
      box-shadow: 0 0 10px #ff3b3b; animation: gcblink 0.9s ease-in-out infinite; }
    #gate-spot .gs-head .grow { flex: 1; }
    #gate-spot .gs-hint { font-size: 8px; letter-spacing: 1.5px; color: rgba(${C},0.55); }
    #gate-spot img { display: block; width: 100%; height: min(560px, 66vh);
      object-fit: cover; background: #05070c; }
    /* sweeping scanline + corner brackets for the futuristic feel */
    #gate-spot .gs-scan { position: absolute; left: 0; right: 0; top: 38px; height: 2px;
      background: linear-gradient(90deg, transparent, rgba(${C},0.7), transparent);
      animation: gs-scan 2.4s linear infinite; pointer-events: none; }
    @keyframes gs-scan { 0% { top: 38px; } 100% { top: 100%; } }
    #gate-spot .gb { position: absolute; width: 18px; height: 18px; border: 2px solid rgba(${C},0.8); pointer-events: none; }
    #gate-spot .gb.tl { top: 6px; left: 6px; border-right: 0; border-bottom: 0; }
    #gate-spot .gb.tr { top: 6px; right: 6px; border-left: 0; border-bottom: 0; }
    #gate-spot .gb.bl { bottom: 6px; left: 6px; border-right: 0; border-top: 0; }
    #gate-spot .gb.br { bottom: 6px; right: 6px; border-left: 0; border-top: 0; }
  `;
  document.head.appendChild(style);

  // small persistent panel
  const box = document.createElement("div");
  box.id = "gate-cam";
  box.innerHTML = `<div class="gc-head"><span class="gc-dot"></span>PORTAIL · LIVE</div>`;
  const img = document.createElement("img");
  img.alt = "Portail";
  img.src = VIDEO;
  img.onerror = () => { box.style.display = "none"; };
  box.appendChild(img);
  document.body.appendChild(box);

  // centre-screen spotlight (hidden until a ring)
  const spot = document.createElement("div");
  spot.id = "gate-spot";
  spot.innerHTML = `
    <div class="gs-head"><span class="gs-dot"></span><span>Portail · Sonnerie</span>
      <span class="grow"></span><span class="gs-hint">cliquer pour fermer</span></div>
    <div class="gs-scan"></div>
    <span class="gb tl"></span><span class="gb tr"></span><span class="gb bl"></span><span class="gb br"></span>`;
  const spotImg = document.createElement("img");
  spotImg.alt = "Portail (live)";
  spot.appendChild(spotImg);
  document.body.appendChild(spot);

  let hideTimer = 0;
  function close() {
    window.clearTimeout(hideTimer);
    spot.classList.remove("show");
    spotImg.src = "";          // stop the spotlight stream
    box.style.display = "";    // bring the small live panel back
    img.src = VIDEO;
  }
  function ringAlert() {
    console.log("[gate] ring → camera spotlight");
    window.clearTimeout(hideTimer);
    box.style.display = "none"; // hide the small panel entirely (no "shrink")
    img.src = "";               // free the single DoorBird stream for the spotlight
    spotImg.src = VIDEO;
    spot.classList.add("show");
    hideTimer = window.setTimeout(close, 22000);  // auto-dismiss after ~22s
  }
  spot.addEventListener("click", close);

  return { ringAlert };
})();

// Weather effects: rain/sun screen FX + Marion's outfit follows the weather
// (umbrella photo when raining, bikini when sunny). The talking-video portrait
// updates too (restart the stream so D-ID uses the new look).
const weather = createWeather((look) => {
  currentLook = look;
  marion.setImage(LOOK_IMG[look] || LOOK_IMG.default);
  // Re-create the talking stream with the new outfit. Also restart when it's
  // still CONNECTING (`starting`) — at boot the stream pre-connects with
  // "default" before the weather is known, so without this the live video would
  // stay on the default look even once the weather resolves to rain/sun.
  if (marionStream.active || marionStream.starting) marionStream.restart();
});
// NB: weather.start() is called *after* marionStream is declared below — with a
// preview hash (#rain/#clear) it fires onLook synchronously, which touches
// marionStream; calling it here would hit a temporal-dead-zone ReferenceError.

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);
// Re-assert the active language on every (re)connection so a backend restart /
// dropped socket never leaves the server on unreliable language auto-detect
// (which made Marion answer in English).
socket.onOpen(() => socket.send({ type: "set_lang", lang: currentLang }));

// Surveillance (webcam face watch) — toggled by the backend on the voice
// command "active/désactive la surveillance".
const surveillance = createSurveillance((m) => socket.send(m));

const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());
marion.setAnalyser(audioPlayer.getAnalyser());

// Marion live avatar (D-ID WebRTC). Started when the user switches to FR/TR,
// stopped on English. The incoming video track is rendered in Marion's frame;
// speaking start/end events drive the listening/speaking state machine.
const marionStream: MarionStream = createMarionStream({
  socket,
  onTrack: (stream) => marion.showLiveStream(stream, isMuted),
  onSpeakingStart: () => { transition("speaking"); armSpeakSafety(20000); },
  onSpeakingEnd: () => { clearSpeakSafety(); finishSpeaking(); },
  onClosed: () => {
    marion.hideLiveStream();
    // D-ID idles a stream out after a few quiet minutes. If we're still on a
    // face persona, transparently reopen it so the next reply lip-syncs.
    if (FACE_LANGS.has(currentLang)) setTimeout(() => marionStream.start(), 3000);
  },
  getLook: () => currentLook,  // weather-appropriate portrait for the talking video
});

weather.start();  // now safe: marionStream exists, so onLook won't hit a TDZ

// Safety net for the live-stream "speaking" state: D-ID's data-channel "done"
// event is not 100% reliable, and if it's missed we'd stay in "speaking" with
// the mic paused forever ("she stopped listening"). Arm a fallback that forces
// us back to idle so the mic always resumes.
let speakSafety: ReturnType<typeof setTimeout> | undefined;
function armSpeakSafety(ms: number) {
  if (speakSafety) clearTimeout(speakSafety);
  speakSafety = setTimeout(() => { speakSafety = undefined; finishSpeaking(); }, ms);
}
function clearSpeakSafety() {
  if (speakSafety) { clearTimeout(speakSafety); speakSafety = undefined; }
}
// Called when Marion finishes speaking over the live stream (no base64 audio, so
// audioPlayer.onFinished never fires). Mirrors that handler: after the post-boot
// briefing, START the mic; otherwise just return to idle.
function finishSpeaking() {
  if (awaitingBriefing) {
    awaitingBriefing = false;
    voiceInput.start();
    transition("listening");
  } else {
    transition("idle");
  }
}

function transition(newState: State) {
  if (newState === currentState) return;
  if (newState !== "speaking") clearSpeakSafety();
  currentState = newState;
  orb.setState(newState as OrbState);
  marion.setState(newState as OrbState);
  cornerHud.setState(newState as OrbState);
  updateStatus(newState);

  switch (newState) {
    case "idle":
      if (!isMuted) voiceInput.resume();
      break;
    case "listening":
      if (!isMuted) voiceInput.resume();
      break;
    case "thinking":
      voiceInput.pause();
      break;
    case "speaking":
      voiceInput.pause();
      break;
  }
}

// ---------------------------------------------------------------------------
// Voice input — mic capture + VAD; server transcribes (Whisper) & auto-detects
// the language, so we just stream raw audio of each utterance.
// ---------------------------------------------------------------------------

const voiceInput = createAudioCapture(
  (pcm: ArrayBuffer) => {
    // Cancel any current JARVIS response before sending new input
    audioPlayer.stop();
    socket.sendBinary(pcm);
    transition("thinking");
  },
  (msg: string) => {
    showError(msg);
  }
);

// ---------------------------------------------------------------------------
// Audio playback finished
// ---------------------------------------------------------------------------

audioPlayer.onFinished(() => {
  // After the post-boot briefing finishes speaking, NOW start the mic — keeping
  // it off during the briefing so JARVIS never transcribes its own voice.
  if (awaitingBriefing) {
    awaitingBriefing = false;
    voiceInput.start();
    transition("listening");
    return;
  }
  transition("idle");
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    if (bootActive) return; // ignore the backend greeting while the boot video plays
    const audioData = msg.data as string;
    console.log("[audio] received", audioData ? `${audioData.length} chars` : "EMPTY", "state:", currentState);
    if (audioData) {
      if (currentState !== "speaking") {
        transition("speaking");
      }
      audioPlayer.enqueue(audioData);
    } else {
      // TTS failed — no audio but still need to return to idle
      console.warn("[audio] no data received, returning to idle");
      transition("idle");
    }
    // Log text for debugging
    if (msg.text) console.log("[JARVIS]", msg.text);
  } else if (type === "avatar_stream_speak") {
    // Marion is about to speak over the live WebRTC stream. Pause the mic and
    // show speaking state now; the stream's "done" event returns us to idle.
    if (bootActive) return;
    if (msg.text) console.log("[JARVIS]", msg.text);
    audioPlayer.stop();
    if (currentState !== "speaking") transition("speaking");
    // Resume the mic exactly when she finishes: D-ID gives us the audio duration,
    // so we don't depend on its flaky "done" event. (+buffer for setup latency.)
    const durSec = typeof msg.duration === "number"
      ? msg.duration
      : ((msg.text as string | undefined)?.length ?? 80) * 0.09 + 4;
    armSpeakSafety(Math.round((durSec + 3) * 1000));
  } else if (type === "avatar_video") {
    // Phase 2 — a D-ID lip-sync video of Marion (FR/TR). The mp4 carries her
    // voice, so we play the video instead of the base64 audio and drive the
    // return-to-idle off the video's end rather than audioPlayer.onFinished.
    if (bootActive) return;
    const url = msg.url as string;
    if (msg.text) console.log("[JARVIS]", msg.text);
    if (url) {
      audioPlayer.stop(); // no overlapping Fish audio
      if (currentState !== "speaking") transition("speaking");
      marion.playVideo(url, isMuted).then(() => transition("idle"));
    } else {
      transition("idle");
    }
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking" && currentState !== "thinking") {
      transition("thinking");
    } else if (state === "working") {
      // Task spawned — show thinking with a different label
      transition("thinking");
      statusEl.textContent = "working...";
    } else if (state === "idle") {
      transition("idle");
    }
  } else if (type === "text") {
    // Text fallback when TTS fails
    console.log("[JARVIS]", msg.text);
  } else if (type === "capture_camera") {
    // Server wants a single webcam frame. Capture one, release the camera,
    // and send it back tagged with the same request_id.
    const requestId = msg.request_id as string;
    console.log("[camera] capture requested", requestId);
    captureCameraFrame()
      .then((data) => {
        socket.send({ type: "camera_frame", request_id: requestId, data });
        if (!data) showError("Camera unavailable or blocked.");
      })
      .catch((e) => {
        console.error("[camera] error", e);
        socket.send({ type: "camera_frame", request_id: requestId, data: null });
      });
  } else if (type === "task_spawned") {
    const label = ((msg.prompt as string) || "Construction").split("\n")[0].trim().slice(0, 42);
    buildHud.spawned(msg.task_id as string, label);
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
  } else if (type === "task_complete") {
    buildHud.completed(msg.task_id as string, msg.status as string, msg.summary as string | undefined);
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
  } else if (type === "play_music") {
    // Voice command ("lance la musique" / "go to hell") — restart the music, loud.
    replayBootMusic();
  } else if (type === "stop_music") {
    // Voice command ("coupe la musique") — fade the music out, then pause it.
    fadeMusicTo(0, 800);
    setTimeout(() => { try { bootMusic.pause(); } catch {} }, 900);
  } else if (type === "gate_ring") {
    // Someone rang the gate — surge the live camera to centre-screen.
    gateCam.ringAlert();
  } else if (type === "watch_mode") {
    // Backend toggled surveillance (voice command).
    if (msg.on) surveillance.start(); else surveillance.stop();
  }
});

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

// ── Boot sequence: cinematic real-time HUD (boot.ts) driven by the boot audio ──
// Two tracks: bootMusic loops forever (the 28s music bed; it drives the HUD
// clock and never stops — it just fades to a faint ambient level once the orb
// takes over), and bootAudio plays the one-shot voice line over the top.
const bootOverlay = document.getElementById("boot-overlay")!;
const bootHint = document.getElementById("boot-hint")!;
const bootMusic = document.getElementById("boot-music") as HTMLAudioElement;
const bootAudio = document.getElementById("boot-audio") as HTMLAudioElement;
const boot = createBoot(bootOverlay);
let bootStarted = false;
let bootFaded = false;

// Climax cue — the HUD burst is aligned to a musical hit detected offline
// (tools/boot_build/analyze_beats.py → boot_cue.json). Defaults keep the boot
// working even if the cue file is missing.
const CUE = { burst: 19, handoff: 22.5, end: 28 };
fetch("/boot_cue.json")
  .then((r) => (r.ok ? r.json() : null))
  .then((c) => {
    if (c && typeof c.burst === "number") {
      CUE.burst = c.burst;
      CUE.handoff = c.handoff ?? c.burst + 3.3;
      CUE.end = c.end ?? c.burst + 8.5;
      boot.setCue(CUE.burst, CUE.handoff);
    }
  })
  .catch(() => {});

// Music levels: present during the boot, faint (audible but unobtrusive) once
// the orb is live. The music keeps looping underneath at the ambient level.
const BOOT_MUSIC_VOL = 0.9;
const AMBIENT_MUSIC_VOL = 0.08;

// Smoothly ramp the looping music volume (never pauses it).
let musicFadeRaf = 0;
function fadeMusicTo(target: number, ms: number) {
  cancelAnimationFrame(musicFadeRaf);
  const start = bootMusic.volume;
  const t0 = performance.now();
  const step = () => {
    const k = Math.min(1, (performance.now() - t0) / ms);
    bootMusic.volume = start + (target - start) * k;
    if (k < 1) musicFadeRaf = requestAnimationFrame(step);
  };
  musicFadeRaf = requestAnimationFrame(step);
}

// The boot track is time-of-day aware: a morning coffee line, an evening wine
// line (voice-over only — no on-screen captions). Morning runs 05:00–17:59.
function timeOfDay(): "morning" | "evening" {
  const h = new Date().getHours();
  return h >= 5 && h < 18 ? "morning" : "evening";
}

bootMusic.addEventListener("timeupdate", () => {
  if (!bootActive) return;
  boot.setTime(bootMusic.currentTime);
  // Near the end of the first pass, bloom the HUD out so the orb shows through
  // and drop the music to the faint ambient level — without ever stopping it.
  if (!bootFaded && bootMusic.currentTime >= CUE.handoff) {
    bootFaded = true;
    // Crossfade, not a cut: ignite the orb so it blooms in underneath while the
    // boot's particles dissolve and the overlay eases away — and drop the music
    // to the faint ambient level.
    orb.ignite();
    cornerHud.reveal();
  securityHud.reveal();
  learningHud.reveal();
    bootOverlay.classList.add("done");
    fadeMusicTo(AMBIENT_MUSIC_VOL, 2500);
  }
});

function endBoot() {
  if (!bootActive) return;
  bootActive = false;
  // Stop the one-shot voice, but leave the music looping — just drop it to the
  // faint ambient level so it stays present under the orb without disturbing.
  try { bootAudio.pause(); } catch {}
  fadeMusicTo(AMBIENT_MUSIC_VOL, 2500);
  if (!bootFaded) { bootFaded = true; orb.ignite(); } // ignite if we never crossfaded
  cornerHud.reveal();
  securityHud.reveal();
  learningHud.reveal();
  boot.fadeOut();
  bootOverlay.classList.add("done");
  setTimeout(() => { boot.dispose(); bootOverlay.style.display = "none"; }, 1500);
  // Deliver the briefing with the mic OFF so JARVIS can't hear (and transcribe)
  // its own voice. The mic starts only when the briefing finishes (onFinished).
  awaitingBriefing = true;
  transition("thinking");
  setTimeout(() => socket.send({ type: "briefing" }), 600);
  // Safety net for the rare case the briefing produces NO audio at all. Kept
  // well beyond any real briefing length (the briefing can run ~60s) so it never
  // fires mid-briefing and cuts the end off — onFinished is the normal trigger.
  setTimeout(() => {
    if (awaitingBriefing) {
      awaitingBriefing = false;
      voiceInput.start();
      transition("listening");
    }
  }, 180000);
}

// Route the boot music + voice into the orb's analyser so the orb visibly
// pulses with the music (during the boot and the faint ambient loop) and with
// JARVIS's voice. The orb already reads this analyser (it's the TTS player's).
// MediaElementSource can be created only once per element, so guard it.
let bootAudioRouted = false;
function routeBootAudioToOrb() {
  if (bootAudioRouted) return;
  try {
    const an = audioPlayer.getAnalyser();
    const ctx = an.context as AudioContext;
    ctx.createMediaElementSource(bootMusic).connect(an);
    ctx.createMediaElementSource(bootAudio).connect(an);
    bootAudioRouted = true;
  } catch (e) {
    console.warn("[boot] could not route audio to the orb analyser", e);
  }
}

// "Let's go to hell" — replay the music from the top, loud, with a quick fade-in
// (no loop: it plays through once, then stays finished until asked again).
const REPLAY_MUSIC_VOL = 0.85;
function replayBootMusic() {
  if (!bootMusic.src) bootMusic.src = `/boot_music.mp3`;
  routeBootAudioToOrb();
  bootMusic.loop = false;
  bootMusic.currentTime = 0;
  bootMusic.muted = isMuted;
  bootMusic.volume = 0.0;
  bootMusic.play().then(() => fadeMusicTo(REPLAY_MUSIC_VOL, 1200)).catch(() => {});
}

function startBoot() {
  if (bootStarted) return;
  bootStarted = true;
  bootHint.style.display = "none";
  // Two tracks, started together by this user gesture:
  //  • bootMusic — the looping music bed; it drives the HUD clock and keeps
  //    playing forever (faded to ambient at the handoff).
  //  • bootAudio — the one-shot, time-of-day welcome line (morning coffee /
  //    evening wine), louder than the music so it sits clearly on top.
  const tod = timeOfDay();
  bootMusic.src = `/boot_music.mp3`;
  // No loop: once the track finishes it stays finished and is NOT reloaded —
  // until the user says the trigger phrase (server → {type:"play_music"}).
  bootMusic.loop = false;
  bootMusic.currentTime = 0;
  bootMusic.volume = BOOT_MUSIC_VOL;
  bootMusic.muted = isMuted;
  bootAudio.src = `/boot_voice_${currentLang}_${tod}.mp3`;
  bootAudio.currentTime = 0;
  bootAudio.volume = 1.0;
  routeBootAudioToOrb(); // so the orb pulses with the music + voice
  boot.begin();
  bootMusic.play().catch(() => endBoot());
  bootAudio.play().catch(() => {});
  // The music loops (never "ends"), so hand off on a timer once the first pass
  // is done — the HUD has bloomed into the orb by then. Tied to the climax cue.
  setTimeout(endBoot, CUE.end * 1000);
  // Prefetch the briefing NOW (during the ~28s boot) so it's ready instantly
  // when the boot ends — no second wait.
  socket.send({ type: "set_lang", lang: currentLang });
  socket.send({ type: "briefing_prefetch" });
  // Pre-connect Marion's live stream during the boot if she's the active
  // persona, so the ~2-3s WebRTC handshake is hidden behind the boot screen
  // and she's already online (warmed up) the instant the boot ends.
  if (FACE_LANGS.has(currentLang)) marionStream.start();
}

// Boot needs a user gesture (audio autoplay). Start it on the first click.
document.addEventListener("click", startBoot);

// Resume AudioContext on ANY user interaction (browser autoplay policy)
function ensureAudioContext() {
  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  if (ctx.state === "suspended") {
    ctx.resume().then(() => console.log("[audio] context resumed"));
  }
}
document.addEventListener("click", ensureAudioContext);
document.addEventListener("touchstart", ensureAudioContext);
document.addEventListener("keydown", ensureAudioContext, { once: true });

// Try to resume audio context on load
ensureAudioContext();

// ---------------------------------------------------------------------------
// UI Controls
// ---------------------------------------------------------------------------

const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;

// Avatar button — opens the interactive avatar app (separate project on :5174)
// in a new tab, without leaving the orb.
const btnAvatar = document.getElementById("btn-avatar");
btnAvatar?.addEventListener("click", (e) => {
  e.stopPropagation();
  window.open(`${location.protocol}//${location.hostname}:5174`, "_blank");
});
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;

// Language toggle — forces Whisper recognition + JARVIS replies to a language.
const langButtons = Array.from(document.querySelectorAll<HTMLButtonElement>(".lang-btn"));
function setLanguage(lang: string) {
  currentLang = lang;
  for (const b of langButtons) b.classList.toggle("active", b.dataset.lang === lang);
  socket.send({ type: "set_lang", lang });
  // FR/TR personas show Marion's face; English stays on the orb. Hide the orb
  // canvas when the face is up so only one visualization is ever visible.
  const showFace = FACE_LANGS.has(lang);
  marion.setVisible(showFace);
  // Keep the orb rendering; .marion-mode shrinks it into a core behind Marion.
  document.body.classList.toggle("marion-mode", showFace);
  canvas.style.visibility = "visible";
  // In Marion mode, dim the orb's bright core while she speaks so it doesn't
  // wash her out (she's screen-blended in front of it).
  orb.setDimOnSpeak(showFace);
  // Open/close the live D-ID stream to match the persona.
  if (showFace) marionStream.start();
  else marionStream.stop();
  console.log("[lang] set to", lang);
}
for (const b of langButtons) {
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    setLanguage(b.dataset.lang || "en");
  });
}
// Tell the server the default (French — Marion) once connected.
setTimeout(() => setLanguage("fr"), 1500);

btnMute.addEventListener("click", (e) => {
  e.stopPropagation();
  isMuted = !isMuted;
  btnMute.classList.toggle("muted", isMuted);
  bootMusic.muted = isMuted; // also silence the looping ambient music
  marion.setMuted(isMuted);  // and Marion's live video audio
  if (isMuted) {
    voiceInput.pause();
    transition("idle");
  } else {
    voiceInput.resume();
    transition("listening");
  }
});

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});

document.addEventListener("click", () => {
  menuDropdown.style.display = "none";
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  statusEl.textContent = "restarting...";
  try {
    await fetch("/api/restart", { method: "POST" });
    // Wait a few seconds then reload
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  // Activate work mode on the WebSocket session (JARVIS becomes Claude Code's voice)
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
});

// Settings button
const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

// First-time setup detection — check after a short delay for server readiness
setTimeout(() => {
  checkFirstTimeSetup();
}, 2000);

// ── FX preview: open .../#rain, #storm, #clear or #clouds to jump STRAIGHT to the
//    live UI (Marion + the weather effect), skipping the ~28s cinematic boot. ──
if (["rain", "storm", "clear", "clouds"].includes(decodeURIComponent(location.hash.replace("#", "")).trim())) {
  bootStarted = true;        // block the real boot from a stray click
  bootActive = false;
  bootOverlay.style.display = "none";
  orb.ignite();
  cornerHud.reveal();
  securityHud.reveal();
  learningHud.reveal();
  setLanguage("fr");         // show Marion (with her weather accessory)
  // weather.start() (above) reads the hash and stages the effect.
}
