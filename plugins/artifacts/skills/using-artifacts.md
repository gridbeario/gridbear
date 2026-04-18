# Using artifacts

When the user would benefit from a visual representation of data beyond chat
text (charts, tables with many rows, comparison matrices, interactive
viewers), create an artifact using the `artifacts__create_artifact` tool.

- Keep HTML self-contained. Inline CSS and JS.
- External libraries: only from esm.sh, unpkg.com, cdn.jsdelivr.net.
- Embed data directly as JSON literals in <script> tags. No runtime fetch.
- Size budget: keep under 500KB for responsiveness.
- Use the returned URL in your reply — the user will click to view.
- If the user wants to keep the artifact beyond 30 days, pass pin=true.

Good candidates: dashboards, sales reports, timeline visualizations,
filterable tables.
Bad candidates: one-line answers, simple markdown that renders in chat.
