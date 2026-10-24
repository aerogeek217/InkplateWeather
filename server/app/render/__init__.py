"""DashboardContext → HTML (Jinja2). The browser and quantizer live in sibling modules."""
from __future__ import annotations

from functools import lru_cache

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .. import icons
from ..context import DashboardContext
from ..paths import TEMPLATES_DIR

# Pages loaded into the renderer's Chromium use this origin; browser.py serves /assets/* from disk under it.
RENDER_ORIGIN = "http://inkplate.render"


@lru_cache(maxsize=1)
def environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"], default_for_string=True, default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals.update(
        icon_svg=icons.svg,
        icon_inner=icons.inner,
        resolve_icon=icons.resolve,
        wind_arrow=icons.wind_arrow,
        wind_names=icons.WIND_NAMES,
        precip_markers=icons.PRECIP_MARKERS,
        moon_small=icons.MOON_SMALL,
        callout_icons=icons.CALLOUT,
    )
    return env


def render_html(ctx: DashboardContext, asset_base: str = "/assets/") -> str:
    """Full page for the dashboard. `asset_base` is the URL prefix of server/assets (fonts)."""
    return environment().get_template("dashboard.html.j2").render(ctx=ctx, asset_base=asset_base)
