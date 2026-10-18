<script lang="ts">
  import { app } from "../lib/store.svelte";
  import { api } from "../lib/api";
  import { run } from "../lib/toast.svelte";

  const FIELDS: [string, string][] = [
    ["max_new_markets_per_day", "New markets / day"],
    ["max_daily_loss_usd", "Daily loss limit $"],
    ["max_drawdown_pct", "Max drawdown %"],
    ["max_order_usd", "Max order $"],
    ["max_position_usd_per_market", "Max per market $"],
    ["max_exposure_per_event_usd", "Max per event $"],
    ["max_gross_exposure_usd", "Max gross exposure $"],
    ["max_open_orders", "Max open orders"],
    ["kelly_fraction", "Kelly fraction"],
  ];

  let edits: Record<string, string> = $state({});
  const limits = $derived(app.control?.limits);

  async function save() {
    const changes: Record<string, number> = {};
    for (const [k, v] of Object.entries(edits)) {
      if (v !== "" && Number(v) !== Number(limits?.[k])) changes[k] = Number(v);
    }
    if (Object.keys(changes).length === 0) return;
    const r = await run(() => api.setLimits(changes), (res) => `updated ${Object.keys(res.applied).join(", ")}`);
    if (r) edits = {};
  }
</script>

<details class="panel">
  <summary>Risk limits & strategies</summary>
  <div class="grid">
    {#each FIELDS as [key, label] (key)}
      <label>
        <span class="muted">{label}</span>
        <input type="number" step="any" min="0" placeholder={String(limits?.[key] ?? "")} bind:value={edits[key]} />
      </label>
    {/each}
  </div>
  <div class="row">
    <button class="primary" onclick={save} disabled={Object.values(edits).every((v) => v === "" || v == null)}>Save limits</button>
    <span class="muted">Validated against hard ceilings; every change is audited.</span>
  </div>
  <div class="strategies">
    {#each Object.entries(app.strategies) as [name, s] (name)}
      <label class="strat">
        <input type="checkbox" checked={app.control?.strategy_auto[name] ?? s.auto}
          onchange={(e) => run(() => api.setStrategyAuto(name, (e.target as HTMLInputElement).checked), () => `${name} updated`)} />
        auto-trade <b>{name}</b>{#if s.needs_judgment}<span class="tag">LLM</span>{/if}
      </label>
    {/each}
  </div>
</details>

<style>
  details { padding: 0 12px 12px; }
  summary { padding: 10px 0; font-weight: 600; cursor: pointer; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  label { display: flex; flex-direction: column; gap: 3px; }
  .row { display: flex; align-items: center; gap: 12px; margin: 12px 0; }
  .strategies { display: flex; flex-wrap: wrap; gap: 6px 18px; }
  .strat { flex-direction: row; align-items: center; gap: 6px; }
</style>
