import unittest

from PIL import Image, ImageDraw, ImageFont

import arknights_pixel_autofill as app


class ImageProcessingTests(unittest.TestCase):
    def test_resample_strength_endpoints(self):
        source = Image.new("RGB", (48, 48), "white")
        draw = ImageDraw.Draw(source)
        draw.rectangle((5, 5, 25, 42), fill=(215, 70, 30))
        draw.ellipse((18, 8, 45, 35), fill=(35, 65, 145))

        nearest = app.resize_to_24(
            source,
            "crop",
            "Nearest（像素画）",
            resample_strength=1.0,
        )
        zero_strength = app.resize_to_24(
            source,
            "crop",
            "Lanczos（原版）",
            resample_strength=0.0,
        )
        full_lanczos = app.resize_to_24(
            source,
            "crop",
            "Lanczos（原版）",
            resample_strength=1.0,
        )
        self.assertEqual(nearest.tobytes(), zero_strength.tobytes())
        self.assertNotEqual(nearest.tobytes(), full_lanczos.tobytes())

    def test_default_match_strength_preserves_selected_algorithm(self):
        samples = [(203, 179, 146), (40, 72, 130), (204, 45, 48), (235, 232, 222)]
        for color in samples:
            rgb_expected = min(
                range(len(app.PALETTE)),
                key=lambda index: app.color_distance(color, app.PALETTE[index]),
            )
            value = app.rgb_to_oklab(color)
            lab_expected = min(
                range(len(app.PALETTE)),
                key=lambda index: sum(
                    (value[channel] - app.OKLAB_PALETTE[index][channel]) ** 2
                    for channel in range(3)
                ),
            )
            self.assertEqual(
                rgb_expected,
                app.nearest_palette_index(color, perceptual=False, match_strength=1.0),
            )
            self.assertEqual(
                lab_expected,
                app.nearest_palette_index(color, perceptual=True, match_strength=1.0),
            )

    def test_official_share_grid_recovers_cells_under_uid_overlay(self):
        width, height = 1000, 1680
        image = Image.new("RGB", (width, height), (53, 216, 230))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((25, 32, 975, 1630), radius=44, fill=(245, 246, 244))
        outer = (85, 220, 915, 1050)
        draw.rounded_rectangle(outer, radius=24, fill=(36, 204, 221))
        inner = (91, 226, 909, 1044)

        source_indices = []
        for row in range(app.N):
            matrix_row = []
            for col in range(app.N):
                index = (row // 4 * 5 + col // 5) % len(app.PALETTE)
                matrix_row.append(index)
                x0 = round(inner[0] + col * (inner[2] - inner[0]) / app.N)
                x1 = round(inner[0] + (col + 1) * (inner[2] - inner[0]) / app.N)
                y0 = round(inner[1] + row * (inner[3] - inner[1]) / app.N)
                y1 = round(inner[1] + (row + 1) * (inner[3] - inner[1]) / app.N)
                draw.rectangle((x0, y0, x1, y1), fill=app.PALETTE[index])
                draw.line((x0, y0, x1, y0), fill=(235, 235, 225), width=1)
                draw.line((x0, y0, x0, y1), fill=(235, 235, 225), width=1)
            source_indices.append(matrix_row)

        # Repeated official-style text crosses cell centers but leaves corner
        # evidence from the underlying flat fill.
        watermark_font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 24)
        for y in (330, 610, 890):
            for x in (155, 430, 705):
                draw.multiline_text((x, y), "//UID\n531122502", font=watermark_font,
                          spacing=0, fill=(248, 248, 248), stroke_width=1,
                          stroke_fill=(120, 120, 120))

        recovered, bounds, confidence = app.detect_official_share_grid(image)
        self.assertGreater(confidence, 0.95)
        self.assertLess(abs((bounds[2] - bounds[0]) - (bounds[3] - bounds[1])), 3)
        self.assertEqual(source_indices, recovered)

    def test_matrix_to_image_is_exact_24_square(self):
        matrix = [[(x + y) % len(app.PALETTE) for x in range(app.N)] for y in range(app.N)]
        image = app.matrix_to_image(matrix)
        self.assertEqual((24, 24), image.size)
        self.assertEqual(app.PALETTE[matrix[7][9]], image.getpixel((9, 7)))


if __name__ == "__main__":
    unittest.main()
