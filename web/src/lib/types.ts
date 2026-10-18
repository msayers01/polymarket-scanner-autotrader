// Shapes mirror pmtrader.app.Engine.snapshot() and the WebSocket delta sections.

export type TradingState = "RUNNING" | "PAUSED" | "HALTED";

export interface Limits {
  max_new_markets_per_day: number;
  max_daily_loss_usd: number;
  max_drawdown_pct: number;
  max_order_usd: number;
  max_position_usd_per_market: number;
  max_exposure_per_event_usd: number;
  max_gross_exposure_usd: number;
  max_open_orders: number;
  max_orders_per_minute: number;
  kelly_fraction: number;
  [k: string]: unknown;
}

export interface Control {
  state: TradingState;
  reason: string;
  since: number;
  mode: "scanner" | "paper" | "live";
  limits: Limits;
  strategy_auto: Record<string, boolean>;
}

export interface Position {
  asset_id: string;
  market_id: string;
  question: string;
  outcome: string;
  strategy: string;
  qty: number;
  avg_cost: number;
  mark: number;
  value: number;
  unrealized: number;
  realized: number;
  opened_ts: number;
}

export interface Portfolio {
  ts: number;
  equity: number;
  cash: number;
  realized: number;
  unrealized: number;
  fees: number;
  gross_exposure: number;
  day_pnl: number | null;
  high_water: number;
  markets_today: number;
  positions: Position[];
  by_strategy: Record<string, number>;
}

export interface Leg {
  asset_id: string;
  side: "BUY" | "SELL";
  price: number;
  outcome: string;
  market_id: string;
}

export interface Opportunity {
  key: string;
  strategy: string;
  market_id: string;
  question: string;
  legs: Leg[];
  fair: number | null;
  edge: number;
  size: number;
  ev_usd: number;
  score: number;
  days: number | null;
  needs_judgment: boolean;
  note: string;
  category: string;
  llm: { enter: boolean; c: number; r: string; p: string } | null;
}

export interface OpenOrder {
  id: string;
  strategy: string;
  market_id: string;
  question: string;
  outcome: string;
  side: "BUY" | "SELL";
  price: number;
  size: number;
  filled: number;
  status: string;
  type: string;
  created_ts: number;
}

export interface Health {
  feed_connected: boolean;
  feed_age_s: number | null;
  subscribed: number;
  books: number;
  universe: number;
  scan_ms: number;
  llm: { mode: string; calls: number; tokens: number; usd: number };
  uptime_s: number;
  risk_rejects: Record<string, number>;
}

export interface FeedEvent {
  kind: "fill" | "order" | "state" | "alert" | "reject" | "report";
  ts: number;
  [k: string]: unknown;
}

export interface Snapshot {
  control: Control;
  portfolio: Portfolio;
  opportunities: Opportunity[];
  orders: OpenOrder[];
  health: Health;
  strategies: Record<string, { enabled: boolean; auto: boolean; needs_judgment: boolean }>;
}
