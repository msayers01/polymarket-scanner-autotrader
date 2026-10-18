<script lang="ts">
  import { app } from "../lib/store.svelte";
  import { api } from "../lib/api";
  import { run } from "../lib/toast.svelte";
  import { pnlClass, px, signedUsd, usd } from "../lib/format";

  let { onTrade }: { onTrade: (marketId: string, assetId?: string) => void } = $props();
  const positions = $derived(app.portfolio?.positions ?? []);
</script>

<section class="panel">
  <header>
    Positions <span class="tag">{positions.length}</span>
    <span class="spacer"></span>
    <span class="muted">uPnL</span>
    <b class={pnlClass(app.portfolio?.unrealized)}>{signedUsd(app.portfolio?.unrealized)}</b>
  </header>
  <div class="body">
    {#if positions.length === 0}
      <div class="empty">No open positions.</div>
    {:else}
      <table>
        <thead>
          <tr><th>Market</th><th class="num">Qty</th><th class="num">Avg → Mark</th><th class="num">uPnL</th><th></th></tr>
        </thead>
        <tbody>
          {#each positions as p (p.asset_id)}
            <tr>
              <td class="q"><div class="clamp" title={p.question}>{p.question}</div><b>{p.outcome}</b> <span class="muted">· {p.strategy}</span></td>
              <td class="num">{p.qty.toFixed(1)}</td>
              <td class="num">{px(p.avg_cost)} → {px(p.mark)}<div class="muted">{usd(p.value)}</div></td>
              <td class="num {pnlClass(p.unrealized)}">{signedUsd(p.unrealized)}</td>
              <td>
                <div class="acts">
                  <button class="small" onclick={() => onTrade(p.market_id, p.asset_id)}>Ticket</button>
                  <button class="small" onclick={() => run(() => api.closePosition(p.asset_id), (r) => (r.ok ? "close sent" : `${r.code}: ${r.message}`))}>Close</button>
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</section>
