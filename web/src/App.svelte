<script lang="ts">
  import StatusBar from "./components/StatusBar.svelte";
  import Opportunities from "./components/Opportunities.svelte";
  import Positions from "./components/Positions.svelte";
  import Orders from "./components/Orders.svelte";
  import Activity from "./components/Activity.svelte";
  import RiskPanel from "./components/RiskPanel.svelte";
  import TradeTicket from "./components/TradeTicket.svelte";
  import { app } from "./lib/store.svelte";
  import { toasts } from "./lib/toast.svelte";

  let ticket: TradeTicket;
  const openTicket = (marketId: string, assetId?: string) => ticket.open(marketId, assetId);
</script>

<StatusBar />

{#if app.control?.state === "HALTED"}
  <div class="banner halted">HALTED — {app.control.reason}. Nothing trades until you resume.</div>
{:else if app.stale}
  <div class="banner stale">{app.connected ? "Stream stale — waiting for data…" : "Disconnected from engine — reconnecting…"}</div>
{/if}

<main>
  <div class="opps"><Opportunities onTrade={openTicket} /></div>
  <div class="side">
    <Positions onTrade={openTicket} />
    <Orders />
  </div>
  <div class="activity"><Activity /></div>
  <div class="risk"><RiskPanel /></div>
</main>

<TradeTicket bind:this={ticket} />

<div class="toasts" aria-live="polite">
  {#each toasts as t (t.id)}<div class="toast {t.level}">{t.text}</div>{/each}
</div>

<style>
  main {
    display: grid;
    grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
    grid-template-rows: minmax(320px, 58vh) minmax(180px, 26vh) auto;
    grid-template-areas: "opps side" "activity side" "risk risk";
    gap: 12px;
    padding: 12px;
  }
  .opps { grid-area: opps; display: grid; min-height: 0; }
  .side { grid-area: side; display: grid; grid-template-rows: 1fr 1fr; gap: 12px; min-height: 0; }
  .activity { grid-area: activity; display: grid; min-height: 0; }
  .risk { grid-area: risk; }
  @media (max-width: 1000px) {
    main { grid-template-columns: 1fr; grid-template-rows: auto; grid-template-areas: "opps" "side" "activity" "risk"; }
    .opps, .side, .activity { max-height: 70vh; }
  }
  .banner { padding: 6px 14px; font-weight: 600; }
  .banner.halted { background: var(--neg); color: white; }
  .banner.stale { background: color-mix(in srgb, var(--warn) 25%, transparent); }
  .toasts { position: fixed; right: 16px; bottom: 16px; display: flex; flex-direction: column; gap: 8px; z-index: 50; }
  .toast { background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--pos); padding: 8px 12px; border-radius: 8px; max-width: 420px; }
  .toast.err { border-left-color: var(--neg); }
</style>
