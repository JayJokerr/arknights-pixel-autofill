"""Windows automation backend shared by the Qt application."""

from arknights_pixel_autofill import (
    AutoPainter,
    list_selectable_windows,
    require_admin_before_startup,
    resource_path,
)

__all__ = [
    "AutoPainter",
    "list_selectable_windows",
    "require_admin_before_startup",
    "resource_path",
]
