<script lang="ts">
  import { app } from "../lib/store.svelte";
  import { api } from "../lib/api";
  import { run } from "../lib/toast.svelte";
  import { ago, px } from "../lib/format";
</script>

<section class="panel">
  <header>
    Open orders <span class="tag">{app.orders.length}</span>
    <span class="spacer"></span>
    <button class="small" disabled={app.orders.length === 0} onclick={() => run(() => api.cancelAll(), (r) => `cancelled ${r.cancelled}`)}>Cancel all</button>
  </header>
  <div class="body">
    {#if app.orders.length === 0}
      <div class="empty">No open orders.</div>
    {:else}
      <table>
        <thead><tr><th>Market</th><th class="num">Price</th><th class="num">Filled</th><th class="num">Age</th><th></th></tr></thead>
        <tbody>
          {#each app.orders as o (o.id)}
            <tr>
              <td class="q">
                <div class="clamp" title={o.question}>{o.question}</div>
                <span class={o.side === "BUY" ? "pos" : "neg"}>{o.side} {o.outcome}</span> <span class="muted">· {o.strategy}</span>
              </td>
              <td class="num">{px(o.price)}</td>
              <td class="num">{o.filled.toFixed(0)}/{o.size.toFixed(0)}</td>
              <td class="num">{ago(o.created_ts)}</td>
              <td><button class="small" onclick={() => run(() => api.cancelOrder(o.id), () => "cancelled")}>Cancel</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</section>
