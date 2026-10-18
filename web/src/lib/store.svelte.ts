// Live application state fed by the engine's WebSocket (snapshot + coalesced deltas).
import type { Control, FeedEvent, Health, OpenOrder, Opportunity, Portfolio, Snapshot } from "./types";
import { getToken } from "./api";

interface AppState {
  connected: boolean;
  stale: boolean;
  seq: number;
  lastMsg: number;
  control: Control | null;
  portfolio: Portfolio | null;
  opportunities: Opportunity[];
  orders: OpenOrder[];
  health: Health | null;
  strategies: Snapshot["strategies"];
  events: FeedEvent[];
}

export const app: AppState = $state({
  connected: false,
  stale: true,
  seq: 0,
  lastMsg: 0,
  control: null,
  portfolio: null,
  opportunities: [],
  orders: [],
  health: null,
  strategies: {},
  events: [],
});

const MAX_EVENTS = 200;
let ws: WebSocket | null = null;
let backoff = 500;

function applySnapshot(s: Snapshot & { events?: FeedEvent[] }): void {
  if (s.events) app.events = [...s.events].reverse().slice(0, MAX_EVENTS);
  app.control = s.control;
  app.portfolio = s.portfolio;
  app.opportunities = s.opportunities;
  app.orders = s.orders;
  app.health = s.health;
  app.strategies = s.strategies;
}

function applyDelta(data: Partial<Snapshot>): void {
  if (data.control) app.control = data.control;
  if (data.portfolio) app.portfolio = data.portfolio;
  if (data.opportunities) app.opportunities = data.opportunities;
  if (data.orders) app.orders = data.orders;
  if (data.health) app.health = data.health;
}

function onMessage(raw: string): void {
  const msg = JSON.parse(raw);
  app.lastMsg = Date.now();
  app.stale = false;
  if (msg.type === "snapshot") {
    app.seq = msg.seq;
    applySnapshot(msg.data);
    return;
  }
  if (msg.seq !== app.seq + 1) {
    // missed frames: ask for a fresh snapshot rather than render inconsistent state
    app.seq = msg.seq;
    ws?.send(JSON.stringify({ type: "resync" }));
    return;
  }
  app.seq = msg.seq;
  if (msg.type === "delta") {
    applyDelta(msg.data);
    if (msg.events?.length) {
      app.events = [...msg.events.reverse(), ...app.events].slice(0, MAX_EVENTS);
    }
  }
}

export function connect(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const token = getToken();
  const url = `${proto}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  ws = new WebSocket(url);
  ws.onopen = () => {
    app.connected = true;
    backoff = 500;
  };
  ws.onmessage = (e) => onMessage(e.data);
  ws.onclose = () => {
    app.connected = false;
    app.stale = true;
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10_000);
  };
  ws.onerror = () => ws?.close();
}

// Heartbeats arrive at least every second; flag the UI if the stream goes quiet.
setInterval(() => {
  if (app.connected && Date.now() - app.lastMsg > 3000) app.stale = true;
}, 1000);
