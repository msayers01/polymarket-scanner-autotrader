<script lang="ts">
  import { api } from "../lib/api";
  import { run } from "../lib/toast.svelte";
  import { px, usd } from "../lib/format";

  type Market = Awaited<ReturnType<typeof api.market>>;

  let dialog: HTMLDialogElement;
  let market = $state<Market | null>(null);
  let assetId = $state("");
  let side: "BUY" | "SELL" = $state("BUY");
  let price = $state(0.5);
  let sizeUsd = $state(10);
  let orderType: "GTC" | "FAK" = $state("GTC");
  let postOnly = $state(true);
  let busy = $state(false);

  const book = $derived(market && assetId ? market.books[assetId] : null);
  const shares = $derived(price > 0 ? Math.floor((sizeUsd / price) * 100) / 100 : 0);

  export async function open(marketId: string, asset?: string) {
    market = null;
    dialog.showModal();
    const m = await run(() => api.market(marketId));
    if (!m) return dialog.close();
    market = m;
    assetId = asset ?? m.outcomes[0].asset_id;
    side = "BUY";
    pickPrice();
  }

  function pickPrice() {
    if (!book) return;
    const best = side === "BUY" ? (postOnly ? book.bids[0]?.[0] : book.asks[0]?.[0]) : (postOnly ? book.asks[0]?.[0] : book.bids[0]?.[0]);
    if (best != null) price = best;
  }

  async function submit() {
    if (!market) return;
    busy = true;
    const m = market;
    const r = await run(
      () => api.placeOrder({ market_id: m.id, asset_id: assetId, side, price, size: shares, order_type: orderType, post_only: orderType === "GTC" && postOnly }),
      (res) => (res.ok ? `order ${res.status}` : `rejected: ${res.code} — ${res.message}`),
    );
    busy = false;
    if (r?.ok) dialog.close();
  }
</script>

<dialog bind:this={dialog}>
  {#if !market}
    <p class="muted">Loading market…</p>
  {:else}
    <h3>Trade ticket</h3>
    <p>{market.question} <a href={market.url} target="_blank" rel="noreferrer">↗</a></p>

    <div class="row">
      {#each market.outcomes as o (o.asset_id)}
        <button class:primary={assetId === o.asset_id} onclick={() => { assetId = o.asset_id; pickPrice(); }}>{o.name}</button>
      {/each}
      <span class="spacer"></span>
      <button class:primary={side === "BUY"} onclick={() => { side = "BUY"; pickPrice(); }}>Buy</button>
      <button class:primary={side === "SELL"} onclick={() => { side = "SELL"; pickPrice(); }}>Sell</button>
    </div>

    {#if book}
      <div class="book">
        <div><span class="muted">bids</span>{#each book.bids.slice(0, 4) as [p, s] (p)}<div class="pos">{px(p)} × {s.toFixed(0)}</div>{/each}</div>
        <div><span class="muted">asks</span>{#each book.asks.slice(0, 4) as [p, s] (p)}<div class="neg">{px(p)} × {s.toFixed(0)}</div>{/each}</div>
      </div>
    {/if}

    <div class="grid">
      <label>Limit price<input type="number" min={market.tick} max={1 - market.tick} step={market.tick} bind:value={price} /></label>
      <label>Amount ($)<input type="number" min="1" step="1" bind:value={sizeUsd} /></label>
      <label>Type
        <select bind:value={orderType}><option value="GTC">Limit (GTC)</option><option value="FAK">Immediate (FAK)</option></select>
      </label>
      {#if orderType === "GTC"}<label class="inline"><input type="checkbox" bind:checked={postOnly} /> post-only (maker)</label>{/if}
    </div>
    <p class="muted">≈ {shares} shares · notional {usd(shares * price)} · min size {market.min_size}. Sells are reduce-only. Risk checks apply.</p>
  {/if}
  <div class="actions">
    <button onclick={() => dialog.close()}>Cancel</button>
    <button class="primary" disabled={!market || busy || shares < (market?.min_size ?? 0)} onclick={submit}>
      {side} {shares || ""} @ {px(price)}
    </button>
  </div>
</dialog>

<style>
  .row { display: flex; gap: 6px; align-items: center; margin: 8px 0; }
  .spacer { flex: 1; }
  .book { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; font: 12px ui-monospace, monospace; margin: 8px 0; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  label { display: flex; flex-direction: column; gap: 3px; }
  label.inline { flex-direction: row; align-items: center; gap: 6px; }
  a { color: var(--accent); }
</style>
