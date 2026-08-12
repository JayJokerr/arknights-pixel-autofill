import unittest

from arknights_pixel.layout import calculate_ui_scale, responsive_metrics


class ResponsiveLayoutTests(unittest.TestCase):
    def test_ui_scale_fits_common_work_areas_and_dpi(self):
        cases = (
            (1920, 1040, 96),
            (2560, 1400, 120),
            (3840, 2120, 192),
            (1366, 728, 96),
        )
        for width, height, dpi in cases:
            scale = calculate_ui_scale(width, height, dpi)
            self.assertLessEqual(round(1180 * scale), width)
            self.assertLessEqual(round(760 * scale), height)
            self.assertGreaterEqual(scale, 0.68)

    def test_density_changes_fonts_buttons_and_panels_together(self):
        regular = responsive_metrics(1400, 950)
        compact = responsive_metrics(1200, 840)
        tight = responsive_metrics(820, 600)
        self.assertEqual(("regular", "compact", "tight"),
                         (regular.density, compact.density, tight.density))
        self.assertGreater(regular.font_size, compact.font_size)
        self.assertGreater(compact.font_size, tight.font_size)
        self.assertGreater(regular.sidebar_width, compact.sidebar_width)
        self.assertGreater(compact.sidebar_width, tight.sidebar_width)
        self.assertGreater(regular.layout_scale, compact.layout_scale)
        self.assertGreater(compact.layout_scale, tight.layout_scale)


if __name__ == "__main__":
    unittest.main()
