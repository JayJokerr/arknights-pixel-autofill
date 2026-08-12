import unittest
from pathlib import Path

from PIL import Image

import arknights_pixel_autofill as app_module


class ResolutionRegressionTests(unittest.TestCase):
    def test_1280_render_rejects_large_false_outer_frame(self):
        sample = Path(__file__).resolve().parents[1] / ".demo-output" / "mumu-focused-render.png"
        if not sample.exists():
            self.skipTest("local live regression capture is unavailable")
        image = Image.open(sample).convert("RGB")
        grid, palette, _score = app_module.AutoPainter._recognize_surface_image(image)

        expected = (295, 119, 856, 680)
        self.assertLess(max(abs(a - b) for a, b in zip(grid.bounds, expected)), 4)
        self.assertEqual(4, len(palette.columns))
        self.assertEqual(6, len(palette.rows))
        centers = [
            (
                (grid.x_lines[col] + grid.x_lines[col + 1]) / 2,
                (grid.y_lines[row] + grid.y_lines[row + 1]) / 2,
            )
            for row in range(24)
            for col in range(24)
        ]
        left, top, right, bottom = grid.bounds
        self.assertTrue(all(left < x < right and top < y < bottom for x, y in centers))
        self.assertGreater(min(x - left for x, _y in centers), 9)
        self.assertGreater(min(right - x for x, _y in centers), 9)


if __name__ == "__main__":
    unittest.main()
