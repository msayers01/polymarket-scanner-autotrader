<script lang="ts">
  import { app } from "../lib/store.svelte";
  import { api } from "../lib/api";
  import { run } from "../lib/toast.svelte";
  import { cents, days, pct, px, usd } from "../lib/format";
  import type { Opportunity } from "../lib/types";
  import MarketSearch from "./MarketSearch.svelte";

  let { onTrade }: { onTrade: (marketId: string, assetId?: string) => void } = $props();

  let filter = $state("");
  let confirmDialog: HTMLDialogElement;
  let pending = $state<Opportunity | null>(null);

  const rows = $derived(
    app.opportunities.filter(
      (o) => !filter || o.strategy === filter || o.question.toLowerCase().includes(filter.toLowerCase()),
    ),
  );
  const strategies = $derived([...new Set(app.opportunities.map((o) => o.strategy))]);
  const canTrade = $derived(app.control?.state === "RUNNING" && app.control?.mode !== "scanner");

  function ask(o: Opportunity) {
    pending = o;
    confirmDialog.showModal();
  }
  async function execute() {
    confirmDialog.close();
    if (!pending) return;
    const key = pending.key;
    await run(() => api.executeOpportunity(key), (r) => (r.ok ? "sent" : `rejected: ${r.code} ${r.message}`));
  }
</script>

<section class="panel">
  <header>
    Opportunities <span class="tag">{app.opportunities.length}</span>
    <span class="spacer"></span>
    <MarketSearch onPick={(id) => onTrade(id)} />
    <select bind:value={filter} aria-label="Filter by strategy">
      <option value="">all strategies</option>
      {#each strategies as s (s)}<option value={s}>{s}</option>{/each}
    </select>
  </header>
  <div class="body">
    {#if rows.length === 0}
      <div class="empty">No opportunities above threshold. The scanner re-evaluates every second.</div>
    {:else}
      <table>
        <thead>
          <tr>
            <th>Market</th><th>Strategy</th><th>Legs</th><th class="num">Edge</th><th class="num">Fair</th>
            <th class="num">Size</th><th class="num">EV</th><th class="num">APR</th><th class="num">Closes</th>
            <th>LLM</th><th></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as o (o.key)}
            <tr>
              <td class="q">{o.question}<div class="muted small">{o.note}</div></td>
              <td><span class="tag">{o.strategy}</span></td>
              <td>
                {#each o.legs as l (l.asset_id)}
                  <div>{l.side} <b>{l.outcome}</b> @ {px(l.price)}</div>
                {/each}
              </td>
              <td class="num pos">{cents(o.edge)}</td>
              <td class="num">{px(o.fair)}</td>
              <td class="num">{o.size.toFixed(0)}</td>
              <td class="num">{usd(o.ev_usd)}</td>
              <td class="num">{pct(o.score)}</td>
              <td class="num">{days(o.days)}</td>
              <td>
                {#if o.llm}<span class={o.llm.enter ? "pos" : "neg"} title={o.llm.r}>{o.llm.enter ? "enter" : "skip"} {o.llm.c.toFixed(2)}</span>
                {:else if o.needs_judgment}<span class="muted">pending</span>{:else}<span class="muted">n/a</span>{/if}
              </td>
              <td class="actions">
                <button class="small" onclick={() => onTrade(o.market_id, o.legs[0]?.asset_id)}>Ticket</button>
                <button class="small primary" disabled={!canTrade} onclick={() => ask(o)}>Execute</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</section>

<dialog bind:this={confirmDialog}>
  {#if pending}
    <h3>Execute {pending.strategy}?</h3>
    <p>{pending.question}</p>
    <ul>
      {#each pending.legs as l (l.asset_id)}<li>{l.side} {l.outcome} up to {px(l.price)}</li>{/each}
    </ul>
    <p class="muted">Sized by the strategy (≤ its max per trade) and checked by the risk engine.</p>
  {/if}
  <div class="actions">
    <button onclick={() => confirmDialog.close()}>Cancel</button>
    <button class="primary" onclick={execute}>Execute</button>
  </div>
</dialog>

<style>
  .small { font-size: 11px; }
  .actions { display: flex; gap: 4px; }
</style>
