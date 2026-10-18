// Transient command feedback (bottom-right).
export const toasts: { id: number; text: string; level: "ok" | "err" }[] = $state([]);
let next = 1;

export function toast(text: string, level: "ok" | "err" = "ok"): void {
  const id = next++;
  toasts.push({ id, text, level });
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id);
    if (i >= 0) toasts.splice(i, 1);
  }, level === "err" ? 7000 : 3500);
}

export async function run<T>(fn: () => Promise<T>, ok?: (r: T) => string): Promise<T | undefined> {
  try {
    const r = await fn();
    if (ok) toast(ok(r));
    return r;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return undefined;
  }
}
