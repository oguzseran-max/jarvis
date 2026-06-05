/**
 * WebSocket client for JARVIS server communication.
 */

export type MessageHandler = (msg: Record<string, unknown>) => void;

export interface JarvisSocket {
  send(data: Record<string, unknown>): void;
  sendBinary(data: ArrayBuffer): void;
  onMessage(handler: MessageHandler): void;
  /** Called on every (re)connection — used to re-assert the forced language so
   *  the backend never falls back to unreliable auto-detect after a reconnect. */
  onOpen(handler: () => void): void;
  close(): void;
  isConnected(): boolean;
}

export function createSocket(url: string): JarvisSocket {
  let ws: WebSocket | null = null;
  let handlers: MessageHandler[] = [];
  let openHandlers: (() => void)[] = [];
  let reconnectDelay = 1000;
  let closed = false;
  let connected = false;

  function connect() {
    if (closed) return;

    ws = new WebSocket(url);

    ws.onopen = () => {
      connected = true;
      reconnectDelay = 1000;
      console.log("[ws] connected");
      for (const h of openHandlers) {
        try { h(); } catch {}
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        for (const h of handlers) h(msg);
      } catch {
        console.warn("[ws] bad message", event.data);
      }
    };

    ws.onclose = () => {
      connected = false;
      if (!closed) {
        console.log(`[ws] reconnecting in ${reconnectDelay}ms`);
        setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
      }
    };

    ws.onerror = (err) => {
      console.error("[ws] error", err);
      ws?.close();
    };
  }

  connect();

  return {
    send(data) {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
      }
    },
    sendBinary(data) {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(data);
      }
    },
    onMessage(handler) {
      handlers.push(handler);
    },
    onOpen(handler) {
      openHandlers.push(handler);
      if (connected) handler();
    },
    close() {
      closed = true;
      ws?.close();
    },
    isConnected() {
      return connected;
    },
  };
}
