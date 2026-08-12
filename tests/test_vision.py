import unittest

from PIL import Image, ImageDraw

from arknights_pixel.palette import N, PALETTE
from arknights_pixel.vision import (
    detect_canvas_grid_dynamic,
    detect_official_share_grid,
    detect_palette_layout,
)


def make_game_scene(scale=1.0, first_palette_row=0):
    width, height = round(1280 * scale), round(720 * scale)
    image = Image.new("RGB", (width, height), (241, 243, 242))
    draw = ImageDraw.Draw(image)
    left, top = round(328 * scale), round(143 * scale)
    side = round(504 * scale)
    draw.rectangle((left, top, left + side, top + side), fill=(252, 252, 251),
                   outline=(72, 72, 72), width=max(1, round(2 * scale)))
    for index in range(1, N):
        value = round(left + side * index / N)
        draw.line((value, top, value, top + side), fill=(221, 223, 222), width=1)
        value = round(top + side * index / N)
        draw.line((left, value, left + side, value), fill=(221, 223, 222), width=1)

    columns = [round(value * scale) for value in (954, 1018, 1082, 1146)]
    rows = [round((292 + index * 64) * scale) for index in range(6)]
    swatch = round(54 * scale)
    draw.rectangle((round(912 * scale), round(120 * scale), round(1192 * scale),
                    round(666 * scale)), fill=(44, 44, 44))
    for visible_row, y in enumerate(rows):
        global_row = first_palette_row + visible_row
        for column, x in enumerate(columns):
            color = PALETTE[global_row * 4 + column]
            draw.rectangle((x - swatch // 2, y - swatch // 2,
                            x + swatch // 2, y + swatch // 2), fill=color)
    return image, (left, top, left + side, top + side)


class VisionTests(unittest.TestCase):
    def test_dynamic_canvas_uses_exact_equal_subdivision(self):
        for scale in (0.9, 0.95, 1.0, 1.25):
            image, truth = make_game_scene(scale)
            expected = tuple(value + round(delta * scale)
                             for value, delta in zip(truth, (-25, -20, 25, 20)))
            layout = detect_canvas_grid_dynamic(image, expected)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(layout.bounds, truth)), 3)
            x_pitch = (layout.x_lines[-1] - layout.x_lines[0]) / N
            y_pitch = (layout.y_lines[-1] - layout.y_lines[0]) / N
            self.assertTrue(all(abs((b - a) - x_pitch) < 1e-6
                                for a, b in zip(layout.x_lines, layout.x_lines[1:])))
            self.assertTrue(all(abs((b - a) - y_pitch) < 1e-6
                                for a, b in zip(layout.y_lines, layout.y_lines[1:])))

    def test_palette_phase_is_detected_at_top_and_bottom(self):
        for first in (0, 4):
            image, _truth = make_game_scene(1.0, first_palette_row=first)
            layout = detect_palette_layout(image)
            self.assertEqual(first, layout.first_global_row)
            self.assertGreater(layout.confidence, 0.8)

    def test_official_share_survives_resize_crop_and_frame_hue_change(self):
        image = Image.new("RGB", (900, 1400), (246, 246, 243))
        draw = ImageDraw.Draw(image)
        frame = (70, 180, 830, 940)
        draw.rectangle(frame, fill=(72, 184, 198))
        inner = (76, 186, 824, 934)
        expected = []
        for row in range(N):
            expected_row = []
            for col in range(N):
                index = (row * 3 + col // 3) % len(PALETTE)
                expected_row.append(index)
                x0 = round(inner[0] + col * (inner[2] - inner[0]) / N)
                x1 = round(inner[0] + (col + 1) * (inner[2] - inner[0]) / N)
                y0 = round(inner[1] + row * (inner[3] - inner[1]) / N)
                y1 = round(inner[1] + (row + 1) * (inner[3] - inner[1]) / N)
                draw.rectangle((x0, y0, x1, y1), fill=PALETTE[index])
            expected.append(expected_row)
        transformed = image.crop((20, 75, 875, 1180)).resize((555, 717), Image.Resampling.LANCZOS)
        actual, _bounds, confidence = detect_official_share_grid(transformed)
        matches = sum(a == b for a_row, b_row in zip(actual, expected)
                      for a, b in zip(a_row, b_row))
        self.assertGreaterEqual(matches, 570)
        self.assertGreater(confidence, 0.8)


if __name__ == "__main__":
    unittest.main()
