<script lang="ts">
  import { api } from "../lib/api";

  let { onPick }: { onPick: (marketId: string) => void } = $props();

  let q = $state("");
  let results = $state<Awaited<ReturnType<typeof api.searchMarkets>>>([]);
  let timer: ReturnType<typeof setTimeout> | undefined;

  function onInput() {
    clearTimeout(timer);
    if (q.trim().length < 2) {
      results = [];
      return;
    }
    timer = setTimeout(async () => {
      try {
        results = await api.searchMarkets(q.trim());
      } catch {
        results = [];
      }
    }, 200);
  }

  function pick(id: string) {
    q = "";
    results = [];
    onPick(id);
  }
</script>

<div class="search">
  <input type="search" placeholder="Find a market to trade…" bind:value={q} oninput={onInput} aria-label="Search markets" />
  {#if results.length}
    <ul>
      {#each results as r (r.id)}
        <li>
          <button onclick={() => pick(r.id)}>
            {r.question}
            <span class="muted">${(r.volume_24h / 1000).toFixed(0)}k 24h{r.live ? " · live" : ""}</span>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .search { position: relative; }
  input { width: min(320px, 50vw); }
  ul {
    position: absolute;
    right: 0;
    top: 110%;
    z-index: 20;
    list-style: none;
    margin: 0;
    padding: 4px;
    width: min(520px, 90vw);
    max-height: 50vh;
    overflow: auto;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgb(0 0 0 / 0.25);
  }
  li button {
    width: 100%;
    text-align: left;
    border: none;
    padding: 6px 8px;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-weight: 400;
  }
  li button:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
</style>
