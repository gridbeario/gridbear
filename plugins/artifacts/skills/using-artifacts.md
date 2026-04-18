# Using artifacts

When the user would benefit from a visual representation of data beyond chat
text (charts, tables with many rows, comparison matrices, interactive
viewers), create an artifact using the `artifacts__create_artifact` tool.

## Sharing model

The URL returned by the tool includes a random share token (`?s=<token>`) so
the link works on any channel — webchat, WhatsApp, Telegram, Discord, SMS,
email. Just include the URL in your reply.

The admin can revoke or regenerate the share token at any time from the
artifacts admin page. The artifact itself remains in storage (owner/admin can
still view it via the portal) but the shareable link stops working.

You don't need to manage this — just create the artifact and return the URL.

## Hard constraints (CSP-enforced)

The artifact is served inside an iframe with a strict Content-Security-Policy:

- **`connect-src 'none'`** — no `fetch()`, no `XMLHttpRequest`, no WebSocket at
  runtime. All data MUST be embedded in the HTML as a JSON literal inside a
  `<script>` tag.
- **`script-src`** allows only: `esm.sh`, `unpkg.com`, `cdn.jsdelivr.net` (plus
  inline). Any other origin is blocked.
- **`img-src`** allows `data:` URIs and `https:` (images from anywhere work).

## Recommended pattern

Use ESM modules from **`esm.sh`**. This avoids UMD-global timing bugs and is
the most reliable path through the CSP:

```html
<!doctype html>
<html>
<head><meta charset="utf-8"><title>...</title></head>
<body>
  <canvas id="chart"></canvas>
  <script id="data" type="application/json">
    {"labels": [...], "values": [...]}
  </script>
  <script type="module">
    import Chart from "https://esm.sh/chart.js@4";
    const data = JSON.parse(document.getElementById("data").textContent);
    new Chart(document.getElementById("chart"), { type: "bar", data: ... });
  </script>
</body>
</html>
```

## Library picks

- Charts: `chart.js`, `echarts`, `plotly.js-dist-min` — via `esm.sh`
- Tables (sort/filter): `@tanstack/table-core` or plain DOM — via `esm.sh`
- Styling: inline CSS, or Tailwind Play CDN from `cdn.jsdelivr.net`
- Icons: inline SVG or data-URI (never `<link>` to external icon font)

## Other rules

- Keep HTML self-contained. Inline CSS and JS beyond imports.
- Size budget: under 500 KB total.
- Pass `pin=true` if the artifact should outlive the 30-day default TTL.

## Bad patterns (will break in production)

- `fetch("https://api.example.com/data")` — blocked by `connect-src 'none'`
- `<script src="https://cdnjs.cloudflare.com/...">` — cloudflare not whitelisted
- Loading data from a query parameter via JS fetch — same block
- Relying on UMD globals set by unpkg `<script src>` without checking load order

Good candidates: dashboards, sales reports, timeline visualizations,
filterable tables.
Bad candidates: one-line answers, simple markdown that renders in chat.
