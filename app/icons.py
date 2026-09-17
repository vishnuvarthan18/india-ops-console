"""Inline SVG icons.

Inline rather than an icon font or a sprite sheet for the same reason the rest
of the console avoids dependencies: it must render with no outbound network,
and an icon that fails to load in an ops tool is worse than no icon.

Each is 16x16, stroke-based, and inherits currentColor so it picks up whatever
the surrounding text colour is — including the nav's active state.
"""

from markupsafe import Markup

_WRAP = ('<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
         'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
         'aria-hidden="true" focusable="false">{}</svg>')

_PATHS = {
    "home":   '<path d="M2 6.5 8 2l6 4.5V13a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z"/><path d="M6 14V9h4v5"/>',
    "bell":   '<path d="M8 2a4 4 0 0 0-4 4c0 3-1 4-1 4h10s-1-1-1-4a4 4 0 0 0-4-4z"/><path d="M6.8 12.5a1.5 1.5 0 0 0 2.4 0"/>',
    "server": '<rect x="2" y="2.5" width="12" height="4.5" rx="1"/><rect x="2" y="9" width="12" height="4.5" rx="1"/><path d="M4.5 4.75h.01M4.5 11.25h.01"/>',
    "layers": '<path d="M8 1.8 14.5 5 8 8.2 1.5 5z"/><path d="m2.4 8 5.6 2.8L13.6 8"/><path d="m2.4 11 5.6 2.8L13.6 11"/>',
    "table":  '<rect x="2" y="2.5" width="12" height="11" rx="1.2"/><path d="M2 6h12M6.2 6v7.5"/>',
    "map":    '<path d="m1.8 4 4-1.7 4.4 1.7 4-1.7v10l-4 1.7-4.4-1.7L1.8 14z"/><path d="M5.8 2.3v10M10.2 4v10"/>',
    "key":    '<circle cx="5.5" cy="6.5" r="3"/><path d="m7.8 8.4 5 5M11 12l1.4-1.4M12.6 13.6 14 12.2"/>',
    "check":  '<path d="M2 8.4 5.4 12 14 3.6"/>',
    "book":   '<path d="M2.5 3.2c1.8-.7 3.7-.7 5.5.3 1.8-1 3.7-1 5.5-.3v9.3c-1.8-.7-3.7-.7-5.5.3-1.8-1-3.7-1-5.5-.3z"/><path d="M8 3.5v9.3"/>',
    "search": '<circle cx="7" cy="7" r="4.3"/><path d="m10.2 10.2 3.3 3.3"/>',
    "exit":   '<path d="M6 14H3.5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1H6"/><path d="M10.5 11 13.5 8l-3-3M13.5 8H6"/>',
    "menu":   '<path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/>',
    "theme":  '<circle cx="8" cy="8" r="4"/><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3.3 3.3l1 1M11.7 11.7l1 1M12.7 3.3l-1 1M4.3 11.7l-1 1"/>',
    "play":   '<path d="M5 3.4v9.2l7.5-4.6z"/>',
    "alert":  '<path d="M8 2.6 14.4 13H1.6z"/><path d="M8 6.4v3M8 11.2h.01"/>',
    "ok":     '<circle cx="8" cy="8" r="6"/><path d="m5.4 8.2 1.8 1.8 3.4-3.6"/>',
    "clock":  '<circle cx="8" cy="8" r="6"/><path d="M8 4.6V8l2.3 1.6"/>',
    "back":   '<path d="M7 3.5 2.5 8 7 12.5M2.5 8h11"/>',
    "down":   '<path d="M8 2.5v8M4.6 7.4 8 10.8l3.4-3.4M3 13.5h10"/>',
    "empty":  '<rect x="2" y="3.5" width="12" height="9" rx="1.2"/><path d="M5 7.5h6M5 10h3.5"/>',
    "shield": '<path d="M8 1.8 13 3.6v4c0 3.2-2.1 5.6-5 6.6-2.9-1-5-3.4-5-6.6v-4z"/><path d="m5.8 7.8 1.6 1.6 3-3.2"/>',
    "link":   '<path d="M6.8 9.2a2.6 2.6 0 0 0 3.7 0l2.1-2.1a2.6 2.6 0 0 0-3.7-3.7l-1.2 1.2"/><path d="M9.2 6.8a2.6 2.6 0 0 0-3.7 0L3.4 8.9a2.6 2.6 0 0 0 3.7 3.7l1.2-1.2"/>',
}


def icon(name: str) -> Markup:
    return Markup(_WRAP.format(_PATHS.get(name, _PATHS["empty"])))
