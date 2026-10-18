<script lang="ts">
  import { app } from "../lib/store.svelte";
  import { px } from "../lib/format";
  import type { FeedEvent } from "../lib/types";

  function line(e: FeedEvent): string {
    switch (e.kind) {
      case "fill":
        return `FILL ${e.side} ${Number(e.size).toFixed(1)} ${e.outcome} @ ${px(Number(e.price))} · ${e.strategy} · ${String(e.question).slice(0, 80)}`;
      case "order":
        return `${e.status} ${e.side} ${Number(e.size).toFixed(0)} @ ${px(Number(e.price))} · ${e.strategy}${e.reason ? ` · ${e.reason}` : ""}`;
      case "state":
        return `STATE → ${e.state} (${e.actor}): ${e.reason}`;
      case "alert":
        return `${String(e.level).toUpperCase()}: ${e.title}${e.message ? ` — ${e.message}` : ""}`;
      case "reject":
        return `risk reject [${e.code}] ${e.strategy}: ${e.message}`;
      case "report":
        return `P&L report (${e.period})`;
      default:
        return JSON.stringify(e);
    }
  }
  const cls = (e: FeedEvent) =>
    e.kind === "fill" ? "pos" : e.kind === "alert" && e.level !== "info" ? "neg" : e.kind === "state" ? "warn" : "muted";
</script>

<section class="panel">
  <header>Activity <span class="spacer"></span><span class="muted">{app.events.length}</span></header>
  <div class="body log">
    {#if app.events.length === 0}
      <div class="empty">Fills, alerts and state changes appear here in real time.</div>
    {/if}
    {#each app.events as e, i (e.ts + ":" + i)}
      <div class="row {cls(e)}">
        <span class="t">{new Date(e.ts * 1000).toLocaleTimeString()}</span>{line(e)}
      </div>
    {/each}
  </div>
</section>

<style>
  .log { font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; padding: 6px 10px; }
  .row { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .t { color: var(--muted); margin-right: 8px; }
  .warn { color: var(--warn); }
</style>
