"""Pure responsive-layout calculations, kept independent from Tk/Win32."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ResponsiveMetrics:
    density: str
    layout_scale: float
    body_padding: int
    panel_gap: int
    sidebar_width: int
    palette_width: int
    font_size: int
    section_font_size: int
    muted_font_size: int
    button_padding: tuple[int, int]
    compact_button_padding: tuple[int, int]
    hero_height: int
    legal_height: int


def calculate_ui_scale(work_w, work_h, dpi=96, base_size=(1180, 760)):
    """Return a physical UI scale that respects both DPI and work-area fit."""
    base_w, base_h = base_size
    dpi_scale = max(0.75, min(2.5, float(dpi) / 96.0))
    resolution_scale = min(float(work_w) / 1920.0, float(work_h) / 1040.0)
    desired_scale = max(dpi_scale, min(2.0, max(0.85, resolution_scale)))
    return min(
        desired_scale,
        max(0.68, (float(work_w) - 24) / base_w),
        max(0.68, (float(work_h) - 24) / base_h),
    )


def responsive_metrics(window_w, window_h, ui_scale=1.0):
    """Choose one coherent density from the *current* usable window size.

    Using logical pixels makes the breakpoints stable at 96/120/144/192 DPI;
    widths, fonts and button padding then change as one unit so controls do not
    disappear while their text remains oversized.
    """
    scale = max(0.1, float(ui_scale))
    logical_w = float(window_w) / scale
    logical_h = float(window_h) / scale
    # Scale continuously with the current window instead of jumping between
    # three sets of fixed dimensions.  The density still decides which
    # secondary hints may be hidden, while every visible control grows and
    # shrinks proportionally with the complete UI.
    layout_scale = round(max(0.78, min(1.30, min(
        logical_w / 1180.0,
        logical_h / 760.0,
    ))), 2)
    if logical_w < 940 or logical_h < 650:
        density = "tight"
    elif logical_w < 1240 or logical_h < 880:
        density = "compact"
    else:
        density = "regular"

    def scaled(value, minimum=1):
        return max(minimum, round(value * layout_scale))

    return ResponsiveMetrics(
        density=density,
        layout_scale=layout_scale,
        body_padding=scaled(14, 8),
        panel_gap=scaled(12, 7),
        sidebar_width=scaled(270, 218),
        palette_width=scaled(214, 174),
        font_size=scaled(10, 8),
        section_font_size=scaled(11, 9),
        muted_font_size=scaled(9, 7),
        button_padding=(scaled(12, 6), scaled(9, 4)),
        compact_button_padding=(scaled(8, 5), scaled(6, 3)),
        hero_height=scaled(90, 74),
        legal_height=scaled(48, 32),
    )
