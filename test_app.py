"""Functional checks use generated pages and a fake scanimage, never the glass."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image, ImageDraw
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core import (Page, Session, export_pages, finish_scan, output_target,
                  layout_preview, layout_sheets, parse_capabilities, scan_arguments)
from app import STYLE, Window

CAPS_TEXT = '''Options specific to device:
    --resolution 75|100|150|200|300|600|1200dpi [75]
    -x 0..220mm [220]
    -y 0..330mm [330]
    --mode Gray|Color [Color]
'''

FAKE = '''#!/usr/bin/python3
import os,sys,time
from PIL import Image,ImageDraw
if '-L' in sys.argv:
 print("device `hpljm1005:libusb:001:009' is a Hewlett-Packard LaserJet M1005 multi-function peripheral")
elif '--help' in sys.argv:
 print(%r)
else:
 behavior=os.environ.get('QINGXI_TEST_BEHAVIOR','ok')
 if behavior=='slow':
  sys.stderr.write('Progress: 5.0%%\\n');sys.stderr.flush();time.sleep(20)
 if behavior=='fail':
  sys.stdout.write('P6\\n600 850\\n255\\npartial');sys.exit(1)
 mode='L' if '--mode=Gray' in sys.argv else 'RGB'
 image=Image.new(mode,(600,850),'white');draw=ImageDraw.Draw(image)
 draw.rectangle((50,60,550,780),outline='black');draw.text((90,100),'SCAN TEST - PAGE',fill='black')
 image.save(sys.stdout.buffer,format='PPM')
 sys.stderr.write('Progress: 100.0%%\\n')
''' % CAPS_TEXT


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qingxi-test-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.png'
        image = Image.new('RGB', (100, 200), 'white')
        ImageDraw.Draw(image).rectangle((0, 0, 49, 49), fill='red')
        image.save(str(self.source))
        self.page = Page('sample', str(self.source), 100)

    def tearDown(self):
        self.temp.cleanup()

    def test_non_destructive_edit_and_undo(self):
        self.page.operations.append({'type': 'rotate', 'clockwise': True})
        image = self.page.image()
        self.assertEqual(image.size, (200, 100))
        self.assertEqual(image.getpixel((199, 0)), (255, 0, 0))
        self.page.operations.append({'type': 'crop', 'box': [150, 0, 200, 50]})
        self.assertEqual(self.page.image().size, (50, 50))
        self.page.operations.pop()
        self.assertEqual(self.page.image().size, (200, 100))
        with Image.open(str(self.source)) as image:
            self.assertEqual(image.size, (100, 200))

    def test_round_trip_draft_and_new_workspace(self):
        session = Session(self.root / 'data')
        target = session.directory / 'page.png'
        target.write_bytes(self.source.read_bytes())
        session.pages.append(Page('sample', str(target), 300))
        session.settings = {'name': '中文材料'}
        session.save(True)
        restored = Session(self.root / 'data')
        self.assertEqual(restored.pages[0].image().size, (100, 200))
        self.assertTrue(restored.dirty)
        self.assertEqual(restored.settings['name'], '中文材料')
        fresh = Session(self.root / 'data', fresh=True)
        self.assertEqual(fresh.pages, [])
        self.assertTrue(target.exists())

    def test_export_order_size_and_no_overwrite(self):
        second = Page('second', str(self.source), 200, [{'type': 'rotate', 'clockwise': True}])
        pdf = self.root / '中文扫描.pdf'
        export_pages([self.page, second], pdf, 'pdf')
        result = subprocess.check_output(['pdfinfo', str(pdf)], env=dict(os.environ, LC_ALL='C')).decode()
        self.assertRegex(result, r'Pages:\s+2')
        self.assertRegex(result, r'Page size:\s+72 x 144 pts')
        original = pdf.read_bytes()
        with self.assertRaises(FileExistsError):
            export_pages([second], pdf, 'pdf')
        self.assertEqual(pdf.read_bytes(), original)
        for kind in ('png', 'jpg'):
            folder = self.root / ('图片_' + kind)
            export_pages([second, self.page], folder, kind)
            with Image.open(str(folder / ('001.' + kind))) as image:
                self.assertEqual(image.size, (200, 100))
            with Image.open(str(folder / ('002.' + kind))) as image:
                self.assertEqual(image.size, (100, 200))
            with self.assertRaises(FileExistsError):
                export_pages([second], folder, kind)

    def test_failed_export_leaves_no_target(self):
        missing = Page('missing', str(self.root / 'absent.png'), 300)
        for kind in ('pdf', 'jpg', 'png'):
            target = self.root / ('failure.' + kind)
            with self.assertRaises(FileNotFoundError):
                export_pages([self.page, missing], target, kind)
            self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.qingxi-*')))

    def test_scan_arguments_and_truncated_input(self):
        caps = parse_capabilities(CAPS_TEXT)
        args = scan_arguments('hpljm1005:libusb:001:009', 300, 'Gray', (210, 297), caps)
        self.assertIn('--resolution=300', args)
        self.assertIn('--mode=Gray', args)
        with self.assertRaises(ValueError):
            scan_arguments('device', 123, 'Gray', None, caps)
        raw = self.root / 'partial.pnm'
        raw.write_bytes(b'P6\n600 850\n255\npartial')
        with self.assertRaises(OSError):
            finish_scan(raw, self.root, 300)
        self.assertFalse(list(self.root.glob('*.part')))

    def test_save_path_validation(self):
        self.assertEqual(output_target(self.root, '中文.pdf', 'pdf').name, '中文.pdf')
        for name in ('../bad', '', '.', 'a/b', 'a\\b'):
            with self.assertRaises(ValueError):
                output_target(self.root, name, 'pdf')

    def colored_pair(self):
        pages = []
        for index, color in enumerate(('blue', 'red')):
            source = self.root / ('face{}.png'.format(index))
            Image.new('RGB', (856, 540), color).save(str(source))
            pages.append(Page('face{}'.format(index), str(source), 254))
        return pages

    def test_card_pdf_has_two_faces_on_one_a4_and_matches_preview(self):
        pages = self.colored_pair()
        sheets = layout_sheets(pages, 'card')
        size, rectangles = sheets[0]
        self.assertEqual(len(sheets), 1)
        self.assertEqual(len(rectangles), 2)
        self.assertAlmostEqual(size[0] * 25.4 / 72, 210)
        self.assertAlmostEqual(size[1] * 25.4 / 72, 297)
        self.assertGreater(rectangles[0][2], rectangles[1][2])
        self.assertAlmostEqual(rectangles[0][3] * 25.4 / 72, 85.6)
        self.assertAlmostEqual(rectangles[0][4] * 25.4 / 72, 54)
        target = self.root / '正反面.pdf'
        export_pages(pages, target, 'pdf', layout='card')
        info = subprocess.check_output(['pdfinfo', str(target)], env=dict(os.environ, LC_ALL='C')).decode()
        self.assertRegex(info, r'Pages:\s+1')
        self.assertRegex(info, r'Page size:.*A4')
        rendered = self.root / 'rendered'
        subprocess.run(['pdftoppm', '-r', '110', '-png', '-singlefile', str(target), str(rendered)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        preview = layout_preview(pages, 'card', dpi=110)
        with Image.open(str(rendered) + '.png') as actual:
            for index, x, y, w, h in rectangles:
                point = (round((x + w / 2) * 110 / 72), round((size[1] - y - h / 2) * 110 / 72))
                expected = preview.getpixel(point)
                self.assertEqual(expected, (0, 0, 255) if index == 0 else (255, 0, 0))
                self.assertTrue(all(abs(a - b) < 5 for a, b in zip(expected, actual.getpixel(point))))

    def test_card_requires_both_faces_and_pdf(self):
        pair = self.colored_pair()
        for pages in (pair[:1], pair + pair[:1]):
            target = self.root / 'invalid.pdf'
            with self.assertRaises(ValueError):
                export_pages(pages, target, 'pdf', layout='card')
            self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            export_pages(pair, self.root / 'images', 'png', layout='card')

    def test_household_pairs_and_odd_last_page(self):
        pair = self.colored_pair()
        pages = pair + pair + pair[:1]
        sheets = layout_sheets(pages, 'two_up')
        self.assertEqual([[rect[0] for rect in sheet[1]] for sheet in sheets], [[0, 1], [2, 3], [4]])
        for (width, height), rectangles in sheets:
            for index, x, y, w, h in rectangles:
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + w, width)
                self.assertLessEqual(y + h, height)
                self.assertAlmostEqual(w / h, 856 / 540)
        export_pages(pages, self.root / '户口本.pdf', 'pdf', layout='two_up')
        info = subprocess.check_output(['pdfinfo', str(self.root / '户口本.pdf')], env=dict(os.environ, LC_ALL='C')).decode()
        self.assertRegex(info, r'Pages:\s+3')
        # Large A4 originals shrink without distortion and stay in their slots.
        large = Page('large', str(pair[0].source), 60)
        sheets = layout_sheets([large, large], 'two_up')
        self.assertLess(sheets[0][1][0][3], 856 * 72 / 60)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(STYLE)

    def pump(self, predicate, seconds=6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(20)
        self.fail('Timed out: ' + self.window.status.text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qingxi-gui-')
        self.root = Path(self.temp.name)
        binary = self.root / 'bin'
        binary.mkdir()
        fake = binary / 'scanimage'
        fake.write_text(FAKE, encoding='utf-8')
        fake.chmod(0o700)
        self.path = os.environ['PATH']
        os.environ['PATH'] = str(binary) + ':' + self.path
        os.environ.pop('QINGXI_TEST_BEHAVIOR', None)
        self.window = Window(Session(self.root / 'data'))
        self.window.show()
        self.pump(lambda: self.window.device and not self.window.detecting)

    def tearDown(self):
        if self.window.busy:
            self.window.cancel()
            self.pump(lambda: not self.window.busy)
        self.window.close()
        self.app.processEvents()
        os.environ['PATH'] = self.path
        os.environ.pop('QINGXI_TEST_BEHAVIOR', None)
        self.temp.cleanup()

    def scan(self):
        self.window.scan_btn.click()
        self.pump(lambda: not self.window.busy)

    def test_scan_edit_save_recover(self):
        window = self.window
        self.scan()
        self.assertEqual(len(window.session.pages), 1)
        window.profile.setCurrentIndex(1)
        self.scan()
        self.assertEqual(len(window.session.pages), 2)
        self.assertEqual(window.session.pages[0].image().mode, 'L')
        self.assertEqual(window.session.pages[1].image().mode, 'RGB')
        second_id = window.current_page().id
        window.rotate(True)
        self.assertEqual(window.current_page().image().size, (850, 600))
        window.crop_btn.click()
        bounds = window.view.mapFromScene(window.view.picture.boundingRect()).boundingRect()
        start = bounds.topLeft() + (bounds.center() - bounds.topLeft()) / 3
        end = bounds.bottomRight() - (bounds.bottomRight() - bounds.center()) / 3
        QTest.mousePress(window.view.viewport(), Qt.LeftButton, pos=start)
        QTest.mouseMove(window.view.viewport(), end)
        QTest.mouseRelease(window.view.viewport(), Qt.LeftButton, pos=end)
        self.assertEqual(len(window.current_page().operations), 2)
        self.assertLess(window.current_page().image().width, 850)
        window.undo()
        window.move(-1)
        self.assertEqual(window.session.pages[0].id, second_id)
        window.remove()
        self.assertEqual(len(window.session.pages), 1)
        window.restore_btn.click()
        self.assertEqual(window.session.pages[0].id, second_id)
        window.directory.setText(str(self.root))
        window.name.setText('功能验证')
        window.export_btn.click()
        self.pump(lambda: not window.busy)
        self.assertTrue((self.root / '功能验证.pdf').exists())
        self.assertFalse(window.session.dirty)
        # The file is not overwritten by a second click.
        window.export_btn.click()
        self.assertIn('已存在', window.status.text())
        recovered = Session(self.root / 'data')
        self.assertEqual([page.id for page in recovered.pages], [page.id for page in window.session.pages])
        self.assertFalse(recovered.dirty)
        window.grab().save('/tmp/qingxi-populated.png')

    def test_cancel_and_failure_preserve_existing_pages(self):
        self.scan()
        original = self.window.session.pages[0].id
        os.environ['QINGXI_TEST_BEHAVIOR'] = 'slow'
        self.window.scan_btn.click()
        QTest.qWait(200)
        self.window.cancel_btn.click()
        self.pump(lambda: not self.window.busy)
        self.assertEqual([page.id for page in self.window.session.pages], [original])
        self.assertIn('已取消', self.window.status.text())
        os.environ['QINGXI_TEST_BEHAVIOR'] = 'fail'
        self.scan()
        self.assertEqual([page.id for page in self.window.session.pages], [original])
        self.assertIn('扫描失败', self.window.status.text())
        self.assertFalse(list(self.window.session.directory.glob('*.part')))

    def test_thumbnail_drop_changes_export_order(self):
        self.scan()
        self.scan()
        window = self.window
        before = [page.id for page in window.session.pages]
        window.pages_list.setCurrentRow(0)
        position = window.pages_list.visualItemRect(window.pages_list.item(1)).center()

        class Drop:
            def source(self):
                return window.pages_list

            def pos(self):
                return position

            def acceptProposedAction(self):
                pass

        window.pages_list.dropEvent(Drop())
        self.assertEqual([page.id for page in window.session.pages], list(reversed(before)))
        recovered = Session(self.root / 'data')
        self.assertEqual([page.id for page in recovered.pages], list(reversed(before)))

    def test_card_guided_scan_preview_export_and_draft(self):
        window = self.window
        window.layout_combo.setCurrentIndex(1)
        self.assertEqual(window.scan_btn.text(), '扫描正面')
        self.assertEqual(window.paper.currentData(), (100, 70))
        self.assertEqual(window.mode.currentData(), 'Color')
        self.assertFalse(window.export_btn.isEnabled())
        self.scan()
        window.crop([50, 60, 550, 375])
        self.assertEqual(window.scan_btn.text(), '扫描反面')
        self.assertFalse(window.composite_btn.isEnabled())
        self.scan()
        window.crop([50, 60, 550, 375])
        self.assertEqual(window.pages_list.item(0).text(), '正面')
        self.assertEqual(window.pages_list.item(1).text(), '反面')
        self.assertFalse(window.scan_btn.isEnabled())
        window.composite_btn.click()
        self.assertIn('合页预览', window.page_info.text())
        self.assertFalse(window.crop_btn.isEnabled())
        self.assertFalse(window.left_btn.isEnabled())
        self.assertIn('1 页 PDF', window.count.text())
        window.grab().save('/tmp/qingxi-card-mode.png')
        window.directory.setText(str(self.root))
        window.name.setText('证件测试')
        window.export_btn.click()
        self.pump(lambda: not window.busy)
        info = subprocess.check_output(['pdfinfo', str(self.root / '证件测试.pdf')], env=dict(os.environ, LC_ALL='C')).decode()
        self.assertRegex(info, r'Pages:\s+1')
        recovered = Session(self.root / 'data')
        self.assertEqual(recovered.settings['layout'], 'card')
        self.assertFalse(recovered.dirty)
        window.layout_combo.setCurrentIndex(0)
        self.assertTrue(window.session.dirty)
        self.assertFalse(window.composite_btn.isChecked())
        self.assertTrue(window.crop_btn.isEnabled())
        self.assertTrue(window.format.model().item(1).isEnabled())

    def test_rescanning_one_face_preserves_other_and_failed_original(self):
        window = self.window
        window.layout_combo.setCurrentIndex(1)
        self.scan()
        self.scan()
        window.pages_list.setCurrentRow(0)
        original = [page.id for page in window.session.pages]
        os.environ['QINGXI_TEST_BEHAVIOR'] = 'slow'
        window.rescan_btn.click()
        QTest.qWait(100)
        window.cancel_btn.click()
        self.pump(lambda: not window.busy)
        self.assertEqual([page.id for page in window.session.pages], original)
        os.environ['QINGXI_TEST_BEHAVIOR'] = 'fail'
        window.rescan_btn.click()
        self.pump(lambda: not window.busy)
        self.assertEqual([page.id for page in window.session.pages], original)
        os.environ.pop('QINGXI_TEST_BEHAVIOR')
        window.rescan_btn.click()
        self.pump(lambda: not window.busy)
        self.assertEqual(len(window.session.pages), 2)
        self.assertNotEqual(window.session.pages[0].id, original[0])
        self.assertEqual(window.session.pages[1].id, original[1])

    def test_household_odd_page_preview_and_legacy_draft_mode(self):
        window = self.window
        window.layout_combo.setCurrentIndex(2)
        for _ in range(3):
            self.scan()
        window.composite_btn.click()
        self.assertIn('第 2 页 PDF', window.page_info.text())
        self.assertIn('2 页 PDF', window.count.text())
        window.new_document()
        self.assertEqual(len(window.session.pages), 0)
        self.assertEqual(window.layout_combo.currentData(), 'two_up')
        self.assertFalse(window.composite_btn.isChecked())
        # An older draft without a layout field must reopen as a normal document.
        window.session.settings.pop('layout', None)
        window.load_settings()
        self.assertEqual(window.layout_combo.currentData(), 'single')


if __name__ == '__main__':
    unittest.main(verbosity=2)
