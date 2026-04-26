"""Corporate Theme for GridBear Admin UI.

Clean professional SaaS aesthetic with indigo/slate palette.
Opaque surfaces, sharp borders, subtle shadows. No glassmorphism.
"""

from pathlib import Path
from typing import Any

from core.interfaces.theme import BaseTheme

PLUGIN_DIR = Path(__file__).resolve().parent


class CorporateTheme(BaseTheme):
    name = "theme-corporate"

    def get_css_variables(self) -> dict[str, dict[str, str]]:
        return {
            "light": {
                "--canvas": "#f8fafc",
                "--elevated": "#ffffff",
                "--sidebar-bg": "#0f172a",
                "--header-bg": "#ffffff",
                "--card-bg": "#ffffff",
                "--separator": "#e2e8f0",
                "--label-0": "#0f172a",
                "--label-1": "#1e293b",
                "--label-2": "#64748b",
                "--label-3": "#94a3b8",
                "--fill-1": "#f8fafc",
                "--fill-2": "#f1f5f9",
                "--fill-3": "#e2e8f0",
                "--fill-4": "#cbd5e1",
                "--nav-active-bg": "rgba(79,70,229,0.10)",
                "--nav-hover-bg": "rgba(255,255,255,0.06)",
                "--nav-active-icon": "#4f46e5",
                "--tint-accent": "#4f46e5",
                "--tint-green": "#16a34a",
                "--tint-orange": "#d97706",
                "--tint-red": "#dc2626",
                "--tint-purple": "#9333ea",
                "--tint-cyan": "#0891b2",
                "--input-bg": "#ffffff",
                "--input-border": "#e2e8f0",
                "--input-focus-border": "#4f46e5",
                "--input-focus-ring": "rgba(79,70,229,0.20)",
                "--btn-hover": "#4338ca",
                "--mesh-1": "transparent",
                "--mesh-2": "transparent",
                "--mesh-3": "transparent",
                "--dot-on": "#16a34a",
                "--dot-on-shadow": "rgba(22,163,74,0.3)",
                "--dot-warn": "#d97706",
                "--dot-warn-shadow": "rgba(217,119,6,0.3)",
                "--dot-off": "#cbd5e1",
                "--dot-err": "#dc2626",
                "--dot-err-shadow": "rgba(220,38,38,0.3)",
                "--overlay-bg": "rgba(0,0,0,0.30)",
                "--table-hover": "#f8fafc",
                "--pill-green-bg": "rgba(22,163,74,0.10)",
                "--pill-orange-bg": "rgba(217,119,6,0.10)",
                "--icon-accent-bg": "rgba(79,70,229,0.10)",
                "--icon-green-bg": "rgba(22,163,74,0.10)",
                "--icon-orange-bg": "rgba(217,119,6,0.10)",
                "--icon-purple-bg": "rgba(147,51,234,0.10)",
                "--icon-cyan-bg": "rgba(8,145,178,0.10)",
                "--avatar-from": "#4f46e5",
                "--avatar-to": "#9333ea",
                "--version-color": "#94a3b8",
                "--shield-color": "rgba(79,70,229,0.35)",
            },
            "dark": {
                "--canvas": "#0f172a",
                "--elevated": "#1e293b",
                "--sidebar-bg": "#020617",
                "--header-bg": "#1e293b",
                "--card-bg": "#1e293b",
                "--separator": "#334155",
                "--label-0": "#f8fafc",
                "--label-1": "#e2e8f0",
                "--label-2": "#94a3b8",
                "--label-3": "#64748b",
                "--fill-1": "#1e293b",
                "--fill-2": "#334155",
                "--fill-3": "#475569",
                "--fill-4": "#64748b",
                "--nav-active-bg": "rgba(99,102,241,0.15)",
                "--nav-hover-bg": "rgba(255,255,255,0.05)",
                "--nav-active-icon": "#818cf8",
                "--tint-accent": "#818cf8",
                "--tint-green": "#4ade80",
                "--tint-orange": "#fbbf24",
                "--tint-red": "#f87171",
                "--tint-purple": "#c084fc",
                "--tint-cyan": "#22d3ee",
                "--input-bg": "#1e293b",
                "--input-border": "#334155",
                "--input-focus-border": "#818cf8",
                "--input-focus-ring": "rgba(129,140,248,0.25)",
                "--btn-hover": "#6366f1",
                "--mesh-1": "transparent",
                "--mesh-2": "transparent",
                "--mesh-3": "transparent",
                "--dot-on": "#4ade80",
                "--dot-on-shadow": "rgba(74,222,128,0.3)",
                "--dot-warn": "#fbbf24",
                "--dot-warn-shadow": "rgba(251,191,36,0.3)",
                "--dot-off": "#475569",
                "--dot-err": "#f87171",
                "--dot-err-shadow": "rgba(248,113,113,0.3)",
                "--overlay-bg": "rgba(0,0,0,0.55)",
                "--table-hover": "rgba(255,255,255,0.03)",
                "--pill-green-bg": "rgba(74,222,128,0.15)",
                "--pill-orange-bg": "rgba(251,191,36,0.15)",
                "--icon-accent-bg": "rgba(129,140,248,0.15)",
                "--icon-green-bg": "rgba(74,222,128,0.15)",
                "--icon-orange-bg": "rgba(251,191,36,0.15)",
                "--icon-purple-bg": "rgba(192,132,252,0.15)",
                "--icon-cyan-bg": "rgba(34,211,238,0.15)",
                "--avatar-from": "#818cf8",
                "--avatar-to": "#c084fc",
                "--version-color": "#64748b",
                "--shield-color": "rgba(129,140,248,0.35)",
            },
        }

    def get_tailwind_config(self) -> dict[str, Any]:
        return {
            "borderRadius": {
                "ios": "8px",
                "ios-lg": "10px",
                "ios-xl": "12px",
            },
            "colors": {
                "nordic": {
                    "50": "#eef2ff",
                    "100": "#e0e7ff",
                    "200": "#c7d2fe",
                    "300": "#a5b4fc",
                    "400": "#818cf8",
                    "500": "#6366f1",
                    "600": "#4f46e5",
                    "700": "#4338ca",
                    "800": "#3730a3",
                    "900": "#312e81",
                    "950": "#1e1b4b",
                },
                "ember": {
                    "400": "#fbbf24",
                    "500": "#f59e0b",
                    "600": "#d97706",
                },
                "frost": {
                    "400": "#38bdf8",
                    "500": "#0ea5e9",
                    "600": "#0284c7",
                },
            },
            "fontFamily": {
                "sans": ["Inter", "-apple-system", "system-ui", "sans-serif"],
                "mono": ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
            },
        }

    def get_custom_css(self) -> str:
        return """\
/* ==============================================
   Corporate Theme — global overrides
   Opaque surfaces, subtle shadows, no glassmorphism
   ============================================== */

/* Body: solid background, no mesh gradients */
body {
    background-color: var(--canvas) !important;
    color: var(--label-1) !important;
    background-image: none !important;
    transition: background-color 0.2s ease, color 0.2s ease;
}

/* Sidebar: always dark, opaque */
aside > div,
aside .flex.flex-col.h-full {
    background: var(--sidebar-bg) !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
    border-color: var(--separator) !important;
}

/* Top header bar: opaque with bottom border */
header.sticky {
    background: var(--header-bg) !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
    border-color: var(--separator) !important;
    box-shadow: 0 1px 2px 0 rgb(0 0 0 / 0.05);
}

/* Cards: opaque with thin border and subtle shadow */
div.rounded-2xl {
    background: var(--card-bg) !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
    border-color: var(--separator) !important;
    box-shadow: 0 1px 2px 0 rgb(0 0 0 / 0.05);
    transition: background 0.2s ease;
}

/* Card inner headers */
div.rounded-2xl > div.border-b {
    border-color: var(--separator) !important;
}

/* Card text overrides */
div.rounded-2xl h3 {
    color: var(--label-0) !important;
}
div.rounded-2xl p {
    color: var(--label-2);
}

/* Navigation active item — indigo left bar */
.menu-active {
    background: var(--nav-active-bg) !important;
    border-left-color: var(--tint-accent) !important;
    color: var(--tint-accent) !important;
}

/* Navigation hover */
nav a:hover {
    background: var(--nav-hover-bg) !important;
}

/* Sidebar section headers */
nav h3 {
    color: var(--label-3) !important;
}

/* Sidebar nav links default color — light for always-dark sidebar */
nav a:not(.menu-active) {
    color: rgba(148, 163, 184, 0.9) !important;
}

/* Sidebar borders */
aside .border-b,
aside .border-t {
    border-color: rgba(255,255,255,0.06) !important;
}

/* Footer */
footer {
    border-color: var(--separator) !important;
}

/* Status dot */
.status-online {
    background-color: var(--dot-on) !important;
    box-shadow: 0 0 6px var(--dot-on-shadow);
}

/* Hide background blobs */
.fixed.inset-0.pointer-events-none {
    opacity: 0 !important;
}

/* Top bar action buttons */
header button.rounded-xl,
header a.rounded-xl {
    background: var(--fill-2) !important;
    color: var(--label-2) !important;
    border: 1px solid var(--separator);
}
header button.rounded-xl:hover,
header a.rounded-xl:hover {
    background: var(--fill-3) !important;
}
header button.rounded-xl i,
header a.rounded-xl i {
    color: var(--label-2) !important;
}

/* Page title */
main h1 {
    color: var(--label-0) !important;
}

/* Gradient border — use indigo */
.gradient-border::before {
    background: linear-gradient(\
135deg, var(--tint-accent), var(--tint-purple), var(--tint-accent)\
) !important;
}

/* Brand text (sidebar) */
aside .text-xl,
aside .text-lg {
    color: #f8fafc !important;
}

/* Version badge (sidebar) */
aside .font-mono {
    color: rgba(148, 163, 184, 0.5) !important;
}

/* User menu in sidebar */
aside .border-t p.text-sm {
    color: #e2e8f0 !important;
}
aside .border-t p.text-xs {
    color: #64748b !important;
}

/* Footer text */
footer span, footer div {
    color: var(--label-2) !important;
}
footer .font-semibold {
    color: var(--label-1) !important;
}

/* Scrollbar */
::-webkit-scrollbar-thumb {
    background: var(--fill-3) !important;
}
::-webkit-scrollbar-thumb:hover {
    background: var(--fill-4) !important;
}

/* Nordic/accent color overrides in plugin templates */
.text-nordic-600, .dark\\:text-nordic-400,
.text-nordic-500 {
    color: var(--tint-accent) !important;
}
.bg-nordic-500 {
    background-color: var(--tint-accent) !important;
}
.from-nordic-500 {
    --tw-gradient-from: var(--tint-accent) !important;
}

/* Input fields */
.input-field {
    background: var(--input-bg);
    border: 1px solid var(--input-border);
    color: var(--label-0);
    transition: all 0.15s ease;
}
.input-field::placeholder { color: var(--label-3); }
.input-field:focus {
    outline: none;
    border-color: var(--input-focus-border);
    box-shadow: 0 0 0 3px var(--input-focus-ring);
}

/* Accent button */
.btn-primary {
    background: var(--tint-accent);
    transition: all 0.15s ease;
}
.btn-primary:hover { background: var(--btn-hover); }
.btn-primary:active { filter: brightness(0.92); }

/* Fade-in animations (minimal) */
@keyframes fadeUp {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
}
.fade-up { animation: fadeUp 0.3s ease-out forwards; }
.fade-d1 { animation-delay: 0.03s; opacity: 0; }
.fade-d2 { animation-delay: 0.08s; opacity: 0; }
.fade-d3 { animation-delay: 0.12s; opacity: 0; }
"""

    def get_static_dir(self) -> Path | None:
        static = PLUGIN_DIR / "static"
        return static if static.is_dir() else None

    def get_templates_dir(self) -> Path | None:
        templates = PLUGIN_DIR / "templates"
        return templates if templates.is_dir() else None

    def get_template_overrides(self) -> dict[str, str]:
        return {
            "auth/login.html": "auth/login.html",
            "dashboard.html": "dashboard.html",
        }

    def get_metadata(self) -> dict[str, str]:
        return {
            "display_name": "Corporate",
            "description": (
                "Clean professional SaaS dashboard with indigo/slate palette"
            ),
            "author": "GridBear",
            "preview_image": "preview.png",
            "accent_color": "#4f46e5",
            "font_imports": [
                "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap"
            ],
        }
