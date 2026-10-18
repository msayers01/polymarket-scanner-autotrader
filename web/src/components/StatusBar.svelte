<script lang="ts">
  import { app } from "../lib/store.svelte";
  import { api } from "../lib/api";
  import { run } from "../lib/toast.svelte";
  import { signedUsd, usd, pnlClass } from "../lib/format";

  let killDialog: HTMLDialogElement;
  let resumeDialog: HTMLDialogElement;
  let flattenOnKill = $state(false);
  let resumeText = $state("");

  const c = $derived(app.control);
  const p = $derived(app.portfolio);
  const h = $derived(app.health);
  const exposurePct = $derived(c && p ? p.gross_exposure / c.limits.max_gross_exposure_usd : 0);

  async function kill() {
    killDialog.close();
    await run(() => api.kill(flattenOnKill), (r) => r.result);
  }
  async function resume() {
    if (c?.state === "HALTED") {
      resumeText = "";
      resumeDialog.showModal();
      return;
    }
    await run(() => api.resume(), (r) => r.result);
  }
  async function confirmResume() {
    resumeDialog.close();
    await run(() => api.resume(resumeText), (r) => r.result);
  }
</script>

<header class="bar">
  <div class="brand">pmtrader</div>
  {#if c}
    <span class="mode {c.mode}">{c.mode.toUpperCase()}</span>
    <span class="state {c.state}" title={c.reason}>{c.state}</span>
  {/if}

  <div class="metric"><span class="muted">Equity</span><b>{usd(p?.equity)}</b></div>
  <div class="metric"><span class="muted">Today</span><b class={pnlClass(p?.day_pnl)}>{signedUsd(p?.day_pnl)}</b></div>
  <div class="metric">
    <span class="muted">Exposure</span>
    <b class:neg={exposurePct > 0.9}>{usd(p?.gross_exposure, 0)}</b>
    <span class="muted">/ {usd(c?.limits.max_gross_exposure_usd, 0)}</span>
  </div>
  <div class="metric">
    <span class="muted">Markets today</span>
    <b>{p?.markets_today ?? 0}</b><span class="muted">/ {c?.limits.max_new_markets_per_day ?? "—"}</span>
  </div>

  <div class="spacer"></div>

  <div class="health" title="feed: {h?.subscribed ?? 0} books · scan {h?.scan_ms ?? 0}ms · LLM {h?.llm.calls ?? 0} calls">
    <span class="dot" class:ok={app.connected && !app.stale && h?.feed_connected}></span>
    {#if !app.connected}offline{:else if app.stale}stale{:else}{h?.subscribed ?? 0} books · {h?.scan_ms ?? 0}ms{/if}
  </div>

  {#if c?.state === "RUNNING"}
    <button onclick={() => run(() => api.pause(), (r) => r.result)}>Pause</button>
  {:else}
    <button class="primary" onclick={resume} disabled={!c || c.mode === "scanner"}>Resume</button>
  {/if}
  <button class="danger" onclick={() => killDialog.showModal()} disabled={!c}>KILL</button>
</header>

<dialog bind:this={killDialog}>
  <h3>Kill switch</h3>
  <p>Halts all trading and cancels every open order. The system stays halted across restarts until you resume it.</p>
  <label><input type="checkbox" bind:checked={flattenOnKill} /> Also flatten all positions (sell at bid, slippage-bounded)</label>
  <div class="actions">
    <button onclick={() => killDialog.close()}>Cancel</button>
    <button class="danger" onclick={kill}>KILL</button>
  </div>
</dialog>

<dialog bind:this={resumeDialog}>
  <h3>Resume from HALTED</h3>
  <p class="muted">Reason: {c?.reason}</p>
  <p>Type <b>RESUME</b> to confirm.</p>
  <input bind:value={resumeText} placeholder="RESUME" />
  <div class="actions">
    <button onclick={() => resumeDialog.close()}>Cancel</button>
    <button class="primary" disabled={resumeText.trim().toUpperCase() !== "RESUME"} onclick={confirmResume}>Resume</button>
  </div>
</dialog>

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 8px 14px;
    background: var(--panel);
    border-bottom: 1px solid var(--line);
    flex-wrap: wrap;
    position: sticky;
    top: 0;
    z-index: 5;
  }
  .brand { font-weight: 800; letter-spacing: 0.3px; }
  .spacer { flex: 1; }
  .metric { display: flex; gap: 6px; align-items: baseline; }
  .metric b { font-size: 15px; }
  .mode, .state {
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--line);
  }
  .mode.live { background: var(--kill); color: white; border-color: var(--kill); }
  .state.RUNNING { color: var(--pos); border-color: var(--pos); }
  .state.PAUSED { color: var(--warn); border-color: var(--warn); }
  .state.HALTED { background: var(--neg); color: white; border-color: var(--neg); }
  .health { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--neg); }
  .dot.ok { background: var(--pos); }
  .danger { padding: 6px 18px; }
</style>
