export const usd = (x: number | null | undefined, digits = 2): string =>
  x == null ? "—" : `${x < 0 ? "-" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const signedUsd = (x: number | null | undefined): string =>
  x == null ? "—" : `${x >= 0 ? "+" : "-"}$${Math.abs(x).toFixed(2)}`;

export const px = (x: number | null | undefined): string => (x == null ? "—" : x.toFixed(3));

export const cents = (x: number): string => `${(x * 100).toFixed(1)}¢`;

export const pct = (x: number, digits = 0): string => `${(x * 100).toFixed(digits)}%`;

export const pnlClass = (x: number | null | undefined): string => (x == null || x === 0 ? "" : x > 0 ? "pos" : "neg");

export const ago = (tsSeconds: number): string => {
  const s = Math.max(0, Date.now() / 1000 - tsSeconds);
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(0)}m`;
  return `${(s / 3600).toFixed(1)}h`;
};

export const days = (d: number | null): string =>
  d == null ? "—" : d < 1 ? `${(d * 24).toFixed(1)}h` : `${d.toFixed(1)}d`;
