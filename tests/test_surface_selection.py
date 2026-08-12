import unittest
from unittest import mock

from PIL import Image

from arknights_pixel import automation as app_module


class SurfaceSelectionTests(unittest.TestCase):
    def test_pixels_choose_valid_editor_not_best_aspect_ratio(self):
        painter = app_module.AutoPainter(mock.Mock())
        candidates = [
            {"hwnd": 10, "origin_x": 0, "origin_y": 0, "width": 1280, "height": 720, "overlap": 1.0},
            {"hwnd": 11, "origin_x": 0, "origin_y": 40, "width": 1200, "height": 800, "overlap": .95},
        ]
        valid_grid = mock.Mock(confidence=.98, x_lines=(0, 600), y_lines=(0, 600))
        valid_palette = mock.Mock(
            confidence=.91, bounds=(900, 100, 1180, 700),
            columns=(920, 980, 1040, 1100), rows=(100, 200, 300, 400, 500, 600),
            first_global_row=0,
        )

        def recognize(image):
            # The non-16:9 candidate is the one whose pixels contain both
            # structures; dimensions alone must never select the first item.
            # Candidate probes are capped to a 1280px long edge.
            if image.size in ((1200, 800),):
                return valid_grid, valid_palette, 3.5
            raise RuntimeError("没有编辑器结构")

        with (
            mock.patch.object(app_module, "list_visual_surface_candidates", return_value=candidates),
            mock.patch.object(app_module.ImageGrab, "grab", side_effect=lambda bbox, all_screens: Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]))),
            mock.patch.object(painter, "_recognize_surface_image", side_effect=recognize),
        ):
            selected, grid, palette = painter._select_visual_surface(99)

        self.assertEqual(11, selected["hwnd"])
        self.assertEqual(.98, grid.confidence)
        self.assertEqual(.91, palette.confidence)
        self.assertEqual((0.0, 600.0), grid.x_lines)

    def test_no_visual_match_fails_closed_instead_of_guessing(self):
        painter = app_module.AutoPainter(mock.Mock())
        candidates = [
            {"hwnd": 10, "origin_x": 0, "origin_y": 0, "width": 1280, "height": 720, "overlap": 1.0},
        ]
        with (
            mock.patch.object(app_module, "list_visual_surface_candidates", return_value=candidates),
            mock.patch.object(app_module.ImageGrab, "grab", return_value=Image.new("RGB", (1280, 720))),
            mock.patch.object(painter, "_recognize_surface_image", side_effect=RuntimeError("未识别")),
        ):
            with self.assertRaisesRegex(RuntimeError, "同时识别"):
                painter._select_visual_surface(99)

    def test_duplicate_screen_rectangles_are_recognized_once_plus_refinement(self):
        painter = app_module.AutoPainter(mock.Mock())
        candidates = [
            {"hwnd": 10, "origin_x": 50, "origin_y": 80, "width": 1920, "height": 1080, "overlap": 1.0},
            {"hwnd": 11, "origin_x": 50, "origin_y": 80, "width": 1920, "height": 1080, "overlap": 1.0},
        ]
        grid = mock.Mock(confidence=.98, x_lines=(0, 800), y_lines=(0, 800))
        palette = mock.Mock(
            confidence=.90, bounds=(1200, 100, 1800, 1000),
            columns=(1250, 1350, 1450, 1550), rows=(100, 200, 300, 400, 500, 600),
            first_global_row=0,
        )
        with (
            mock.patch.object(app_module, "list_visual_surface_candidates", return_value=candidates),
            mock.patch.object(app_module.ImageGrab, "grab", return_value=Image.new("RGB", (1920, 1080))) as grab,
            mock.patch.object(painter, "_recognize_surface_image", return_value=(grid, palette, 3.5)) as recognize,
        ):
            selected, _grid, _palette = painter._select_visual_surface(99)

        self.assertEqual(10, selected["hwnd"])
        self.assertEqual(1, grab.call_count)
        self.assertEqual(1, recognize.call_count)

    def test_cell_order_uses_serpentine_rows(self):
        points = [(0, 1), (4, 0), (1, 0), (3, 1), (2, 2)]
        self.assertEqual(
            [(1, 0), (4, 0), (3, 1), (0, 1), (2, 2)],
            app_module.AutoPainter._optimize_cell_order(points),
        )


if __name__ == "__main__":
    unittest.main()
