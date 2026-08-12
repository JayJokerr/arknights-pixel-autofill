import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QSignalSpy, QTest
    from PySide6.QtWidgets import QApplication
    from arknights_pixel_qt import MainWindow, PixelCanvas
except ImportError:
    QApplication = None
    MainWindow = None
    PixelCanvas = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class QtInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_canvas_emits_live_change_before_release(self):
        canvas = PixelCanvas()
        canvas.resize(480, 480)
        canvas.set_selected(4)
        canvas.show()
        self.app.processEvents()
        board, _guide = canvas._geometry()
        first = QPoint(board.left() + 5, board.top() + 5)
        second = QPoint(board.left() + board.width() // 24 + 5, board.top() + 5)
        changing = QSignalSpy(canvas.matrixChanging)
        finished = QSignalSpy(canvas.matrixEdited)

        QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, first)
        self.assertEqual(1, changing.count())
        self.assertEqual(0, finished.count())
        QTest.mouseMove(canvas, second)
        self.assertGreaterEqual(changing.count(), 2)
        self.assertEqual(0, finished.count())
        QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, second)
        self.assertEqual(1, finished.count())
        canvas.close()

    def test_slider_reprocess_waits_until_handle_release(self):
        window = MainWindow()
        window.source_image = mock.sentinel.image
        slider = window.resample_slider.slider
        with mock.patch.object(window, "reprocess") as reprocess:
            slider.setSliderDown(True)
            slider.setValue(slider.value() - 1)
            window._flush_reprocess()
            reprocess.assert_not_called()
            slider.setSliderDown(False)
            reprocess.assert_called_once()
        window.close()

    def test_window_icon_is_configured(self):
        window = MainWindow()
        self.assertFalse(window.windowIcon().isNull())
        window.close()


if __name__ == "__main__":
    unittest.main()
