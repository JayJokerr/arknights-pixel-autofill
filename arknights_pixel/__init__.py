"""Core image and vision modules for Arknights Pixel Autofill."""

from .palette import N, PALETTE
from .layout import calculate_ui_scale, responsive_metrics

__all__ = ["N", "PALETTE", "calculate_ui_scale", "responsive_metrics"]
