/**
 * Marion live avatar — D-ID WebRTC streaming client.
 *
 * Opens a real-time talking-head stream from D-ID and exposes the incoming
 * MediaStream + speaking events. The D-ID API key never touches the browser:
 * every signaling call is relayed through the JARVIS backend (`/api/did/stream/*`),
 * and the backend (not us) pushes the audio for Marion to speak. We just:
 *   1. ask the backend to create a stream (gets the SDP offer + ICE servers),
 *   2. complete the WebRTC handshake (answer + trickle ICE, both via the backend),
 *   3. surface the live video track and the "speaking started/done" events.
 *
 * The backend learns our stream id/session via the existing WebSocket
 * (`did_stream_ready`) so it can make Marion talk on each FR/TR reply.
 */

interface StreamOpts {
  socket: { send: (m: Record<string, unknown>) => void };
  onTrack: (stream: MediaStream) => void;   // attach to a <video>
  onSpeakingStart?: () => void;
  onSpeakingEnd?: () => void;
  onReady?: () => void;
  onClosed?: () => void;
  getLook?: () => string;                   // weather-dependent portrait
}

export interface MarionStream {
  start(): Promise<void>;
  stop(): Promise<void>;
  restart(): Promise<void>;
  readonly active: boolean;
  /** True while the WebRTC handshake is in flight (not yet `active`). */
  readonly starting: boolean;
}

async function api(path: string, body?: unknown): Promise<any> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

export function createMarionStream(opts: StreamOpts): MarionStream {
  let pc: RTCPeerConnection | null = null;
  let streamId: string | null = null;
  let sessionId: string | null = null;
  let active = false;
  let starting = false;
  // A look (weather outfit) change requested while we're still connecting: the
  // stream is created with the look read at start() time, so we can't swap it
  // mid-handshake. Remember the request and restart once connected.
  let pendingRestart = false;

  // Close the D-ID stream when the page is hidden/reloaded/closed. Without this
  // every page reload abandons an OPEN stream on D-ID's side; on the deid-lite
  // plan those orphans pile up until "Max user sessions reached" — after which
  // no new stream can be created (502) and Marion can't lip-sync. sendBeacon is
  // the only request that reliably survives an unload.
  window.addEventListener("pagehide", () => {
    if (streamId && sessionId) {
      const blob = new Blob(
        [JSON.stringify({ stream_id: streamId, session_id: sessionId })],
        { type: "application/json" },
      );
      navigator.sendBeacon("/api/did/stream/close", blob);
    }
  });

  function handleDataChannelMessage(raw: string) {
    // D-ID emits plain strings like "stream/started", "stream/done", "stream/ready".
    const msg = String(raw);
    if (msg.includes("started")) opts.onSpeakingStart?.();
    else if (msg.includes("done")) opts.onSpeakingEnd?.();
  }

  async function start() {
    if (active || starting) return;
    starting = true;
    try {
      const look = opts.getLook ? opts.getLook() : "default";
      const s = await api("/api/did/stream/new?look=" + encodeURIComponent(look));
      if (!s || !s.id || !s.offer) throw new Error("no stream/offer");
      streamId = s.id;
      sessionId = s.session_id;

      pc = new RTCPeerConnection({ iceServers: s.ice_servers });

      // Accumulate inbound tracks. D-ID sends a video + audio track; some
      // browsers populate event.streams, others only event.track — handle both.
      const inbound = new MediaStream();
      pc.ontrack = (e) => {
        console.log("[marion-stream] ontrack", e.track.kind, "streams:", e.streams.length);
        if (e.streams && e.streams[0]) {
          opts.onTrack(e.streams[0]);
        } else {
          inbound.addTrack(e.track);
          opts.onTrack(inbound);
        }
      };
      pc.onicecandidate = (e) => {
        if (e.candidate && streamId && sessionId) {
          api("/api/did/stream/ice", {
            stream_id: streamId,
            session_id: sessionId,
            candidate: e.candidate.candidate,
            sdpMid: e.candidate.sdpMid,
            sdpMLineIndex: e.candidate.sdpMLineIndex,
          }).catch(() => {});
        }
      };
      pc.ondatachannel = (e) => {
        e.channel.onmessage = (m) => handleDataChannelMessage(m.data);
      };
      pc.onconnectionstatechange = () => {
        const st = pc?.connectionState;
        if (st === "connected" && !active) {
          active = true;
          // Tell the backend which stream to speak into.
          opts.socket.send({ type: "did_stream_ready", stream_id: streamId, session_id: sessionId });
          opts.onReady?.();
        } else if (st === "failed" || st === "closed" || st === "disconnected") {
          void teardown();
        }
      };

      await pc.setRemoteDescription(s.offer);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      await api("/api/did/stream/sdp", { stream_id: streamId, session_id: sessionId, answer });
    } catch (err) {
      console.error("[marion-stream] start failed", err);
      await teardown();
    } finally {
      starting = false;
      // A look change came in mid-handshake — reconnect now with the new look.
      if (pendingRestart) { pendingRestart = false; void restart(); }
    }
  }

  async function teardown() {
    const sid = streamId, sess = sessionId;
    active = false;
    streamId = null;
    sessionId = null;
    try { pc?.close(); } catch {}
    pc = null;
    opts.socket.send({ type: "did_stream_closed" });
    opts.onClosed?.();
    if (sid && sess) await api("/api/did/stream/close", { stream_id: sid, session_id: sess }).catch(() => {});
  }

  async function restart() {
    // Mid-handshake we can't swap the look; defer until start() settles.
    if (starting) { pendingRestart = true; return; }
    await teardown();
    await start();
  }

  return {
    start,
    stop: teardown,
    restart,
    get starting() { return starting; },
    get active() { return active; },
  };
}
