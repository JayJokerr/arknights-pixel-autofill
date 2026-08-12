import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from arknights_pixel_qt import MainWindow
except ImportError:  # Local source-only environments may not have Qt yet.
    QApplication = None
    MainWindow = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class QtResponsiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_common_window_sizes_keep_primary_controls_visible_and_aligned(self):
        window = MainWindow()
        for width, height in ((920, 660), (1180, 760), (1500, 960)):
            window.resize(width, height)
            window.show()
            self.app.processEvents()
            self.assertEqual(window.open_button.width(), window.crop_button.width())
            self.assertTrue(window.start_button.isVisible())
            self.assertTrue(window.window_combo.isVisible())
            self.assertGreaterEqual(window.canvas.width(), 300)
            self.assertGreaterEqual(window.palette.width(), 150)
            hero = window.hero.geometry()
            logo = window.logo.geometry()
            self.assertLessEqual(hero.width() - (logo.x() + logo.width()), 28)
        window.close()


if __name__ == "__main__":
    unittest.main()
