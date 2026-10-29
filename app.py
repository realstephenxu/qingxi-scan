#!/usr/bin/python3
"""清晰扫描 — a native, offline M1005 scanning workspace for UOS."""
import copy
import datetime
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from PyQt5.QtCore import (Qt, QLockFile, QProcess, QRect, QSize, QThread, QTimer,
                          QUrl, pyqtSignal)
from PyQt5.QtGui import QDesktopServices, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog,
    QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QRubberBand, QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget)

from core import (APP_DIR, DATA_DIR, Session, export_pages, finish_scan,
                  layout_preview, output_target, parse_capabilities, parse_devices, sane_env,
                  scan_arguments)


STYLE = '''
QWidget { font-family: "Noto Sans CJK SC"; font-size: 13px; color: #23334b; }
QMainWindow, QWidget#root { background: #f3f5f8; }
QFrame#header, QFrame#savebar { background: #ffffff; border: none; }
QFrame#card, QFrame#pagesCard { background: #ffffff; border: 1px solid #e0e6ed; border-radius: 12px; }
QLabel#brand { font-size: 22px; font-weight: bold; color: #152c40; }
QLabel#section { font-size: 15px; font-weight: bold; }
QLabel#muted { color: #708095; font-size: 12px; }
QLabel#eyebrow { color: #268675; font-size: 12px; font-weight: bold; }
QLabel#emptyTitle { font-size: 24px; font-weight: bold; color: #233d53; }
QLabel#emptyIcon { font-size: 54px; color: #2c8d7d; }
QLabel#status { padding: 10px 14px; border-radius: 7px; background: #e7f3ef; color: #246655; }
QPushButton { background: #ffffff; border: 1px solid #dce3eb; border-radius: 7px; padding: 8px 12px; }
QPushButton:hover { background: #edf5f3; border-color: #95beb4; }
QPushButton:pressed { background: #dcece6; }
QPushButton:disabled { color: #a5afbb; border-color: #e6ebef; background: #f7f8fa; }
QPushButton#primary { background: #167968; color: white; font-weight: bold; border: 1px solid #167968; padding: 12px 16px; }
QPushButton#primary:hover { background: #0f6658; }
QPushButton#primary:disabled { background: #adcbc3; border-color: #adcbc3; color: #fff; }
QPushButton#danger { color: #b04b40; }
QPushButton:checked { background: #dcefe8; border-color: #167968; color: #166653; }
QComboBox, QLineEdit { background: #fff; border: 1px solid #d6dfe8; border-radius: 6px; min-height: 24px; padding: 6px 9px; selection-background-color: #167968; }
QComboBox:focus, QLineEdit:focus { border: 1px solid #167968; }
QComboBox:disabled, QLineEdit:disabled { color: #9aa6b5; background: #f5f7f9; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: url("__CHEVRON_URL__"); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: white; color: #23334b; selection-background-color: #dcefe8; selection-color: #164f43; padding: 4px; }
QListWidget { background: #fff; border: none; outline: none; }
QListWidget::item { margin: 4px; padding: 7px; border: 2px solid transparent; border-radius: 8px; }
QListWidget::item:selected { background: #eaf5f0; border-color: #328a76; color: #195c4a; }
QListWidget::item:hover:!selected { background: #f2f5f8; }
QGraphicsView { background: #e9edf2; border: 1px solid #dfe5ec; border-radius: 10px; }
QScrollArea { border: none; background: transparent; }
QProgressBar { border: none; background: #dfe9e4; border-radius: 3px; height: 6px; text-align: center; }
QProgressBar::chunk { background: #268675; border-radius: 3px; }
QSplitter::handle { background: transparent; width: 12px; }
QToolTip { background: #23334b; color: white; border: none; padding: 7px; }
'''.replace('__CHEVRON_URL__', (APP_DIR / 'chevron.svg').as_posix())


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback, name=None):
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


def pixmap(image, maximum=None):
    image = image.copy()
    if maximum:
        image.thumbnail(maximum)
    image = image.convert('RGB')
    data = image.tobytes('raw', 'RGB')
    qt_image = QImage(data, image.width, image.height, image.width * 3, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qt_image)


class DeviceWorker(QThread):
    result = pyqtSignal(object)
    failure = pyqtSignal(str)

    def run(self):
        try:
            result = subprocess.run(['scanimage', '-L'], env=sane_env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            devices = parse_devices(result.stdout.decode('utf-8', 'replace'))
            if not devices:
                raise ValueError('没有找到 M1005。请检查电源和 USB 连接，再点击“重新检测”。')
            device, description = devices[0]
            result = subprocess.run(['scanimage', '-d', device, '--help'], env=sane_env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if result.returncode:
                raise ValueError('无法打开扫描仪。请关闭其他扫描软件，再重新检测。\n' + result.stderr.decode('utf-8', 'replace')[-400:])
            self.result.emit((device, description, parse_capabilities(result.stdout.decode('utf-8', 'replace'))))
        except Exception as error:
            self.failure.emit(str(error))


class Work(QThread):
    result = pyqtSignal(object)
    failure = pyqtSignal(str)
    progress = pyqtSignal(int)

    def __init__(self, function):
        super().__init__()
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function(self.progress.emit))
        except Exception as error:
            self.failure.emit(str(error))


class Preview(QGraphicsView):
    cropped = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(280, 240)
        self.band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self.cropping, self.fitting, self.origin, self.picture = False, True, None, None

    def set_image(self, image):
        self.scene().clear()
        self.picture = self.scene().addPixmap(pixmap(image))
        self.scene().setSceneRect(self.picture.boundingRect())
        self.fit()

    def fit(self):
        self.fitting = True
        if self.picture:
            self.fitInView(self.picture, Qt.KeepAspectRatio)

    def zoom(self, factor):
        if self.picture and .02 < self.transform().m11() * factor < 8:
            self.fitting = False
            self.scale(factor, factor)

    def crop_mode(self, active):
        self.cropping = active
        self.origin = None
        self.band.hide()
        self.setDragMode(QGraphicsView.NoDrag if active else QGraphicsView.ScrollHandDrag)
        self.viewport().setCursor(Qt.CrossCursor if active else Qt.OpenHandCursor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitting:
            self.fit()

    def mousePressEvent(self, event):
        if self.cropping and event.button() == Qt.LeftButton:
            self.origin = event.pos()
            self.band.setGeometry(QRect(self.origin, QSize()))
            self.band.show()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.cropping and self.origin is not None:
            self.band.setGeometry(QRect(self.origin, event.pos()).normalized())
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.cropping and self.origin is not None:
            selection = QRect(self.origin, event.pos()).normalized()
            self.band.hide()
            self.origin = None
            if selection.width() > 8 and selection.height() > 8 and self.picture:
                rect = self.mapToScene(selection).boundingRect().intersected(self.picture.boundingRect())
                box = [round(rect.left()), round(rect.top()), round(rect.right()), round(rect.bottom())]
                if box[2] - box[0] > 10 and box[3] - box[1] > 10:
                    self.cropped.emit(box)
        else:
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        else:
            super().wheelEvent(event)


class PageList(QListWidget):
    reordered = pyqtSignal()

    def dropEvent(self, event):
        # IconMode's native drop moves coordinates rather than document rows.
        # Keep thumbnail order and exported page order under one owner.
        if event.source() != self:
            event.ignore()
            return
        source = self.currentRow()
        target = self.indexAt(event.pos()).row()
        if target < 0:
            target = self.count() - 1
        if source >= 0 and source != target:
            self.blockSignals(True)
            item = self.takeItem(source)
            self.insertItem(target, item)
            self.setCurrentItem(item)
            self.blockSignals(False)
            self.reordered.emit()
        event.acceptProposedAction()


class Window(QMainWindow):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self.device, self.caps = None, None
        self.busy, self.detecting, self.cancelled = False, False, False
        self.process, self.worker, self.detector = None, None, None
        self.removed, self.rebuilding = [], False
        self.setWindowTitle('清晰扫描')
        self.setWindowIcon(QIcon(str(APP_DIR / 'icon.svg')))
        self.resize(1320, 900)
        self.setMinimumSize(1080, 760)
        self.build()
        self.load_settings()
        self.rebuild_pages()
        self.autosave = QTimer(self)
        self.autosave.setSingleShot(True)
        self.autosave.timeout.connect(self.remember)
        for field in (self.name, self.directory):
            field.textChanged.connect(lambda: self.autosave.start(400))
            field.textChanged.connect(self.update_target)
        for field in (self.mode, self.dpi, self.paper, self.format):
            field.currentIndexChanged.connect(lambda: self.autosave.start(400))
        self.format.currentIndexChanged.connect(self.update_target)
        self.layout_combo.currentIndexChanged.connect(self.layout_changed)
        self.configure_layout()
        self.scan_timeout = QTimer(self)
        self.scan_timeout.setSingleShot(True)
        self.scan_timeout.timeout.connect(self.timed_out)
        self.update_target()
        QTimer.singleShot(150, self.detect)

    def build(self):
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName('header')
        row = QHBoxLayout(header)
        row.setContentsMargins(26, 17, 26, 17)
        logo = label('')
        logo.setPixmap(QIcon(str(APP_DIR / 'icon.svg')).pixmap(42, 42))
        row.addWidget(logo)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(label('清晰扫描', 'brand'))
        brand.addWidget(label('扫描 · 整理 · 保存', 'muted'))
        row.addLayout(brand)
        row.addSpacing(24)
        row.addWidget(label('扫描模式', 'muted'))
        self.layout_combo = QComboBox()
        self.layout_combo.addItem('普通文档 · 逐页保存', 'single')
        self.layout_combo.addItem('身份证 · 正反面同页', 'card')
        self.layout_combo.addItem('户口本 · 两页合排', 'two_up')
        self.layout_combo.setMinimumWidth(205)
        row.addWidget(self.layout_combo)
        row.addStretch()
        row.addWidget(label('本地处理  /  草稿自动保留', 'muted'))
        self.history_btn = button('打开历史草稿', self.history)
        self.new_btn = button('新建文档', self.new_document)
        row.addWidget(self.history_btn)
        row.addWidget(self.new_btn)
        layout.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(22, 20, 22, 16)
        body.setSpacing(18)
        self.settings_card = QFrame()
        self.settings_card.setObjectName('card')
        self.settings_card.setFixedWidth(250)
        settings = QVBoxLayout(self.settings_card)
        settings.setContentsMargins(18, 20, 18, 18)
        settings.setSpacing(10)
        settings.addWidget(label('01  扫描设置', 'section'))
        settings.addSpacing(8)
        self.device_label = label('正在检测扫描仪…', None, True)
        self.device_label.setMinimumHeight(40)
        settings.addWidget(self.device_label)
        self.detect_btn = button('重新检测设备', self.detect)
        settings.addWidget(self.detect_btn)
        settings.addSpacing(8)
        settings.addWidget(label('快捷设置', 'muted'))
        self.profile = QComboBox()
        self.profile.addItems(['普通文件（灰度）', '彩色材料（彩色）', '高清细节（600 dpi）', '自定义设置'])
        self.profile.currentIndexChanged.connect(self.apply_profile)
        settings.addWidget(self.profile)
        settings.addWidget(label('色彩模式', 'muted'))
        self.mode = QComboBox()
        self.mode.addItem('灰度 · 文字材料', 'Gray')
        self.mode.addItem('彩色 · 照片 / 证件', 'Color')
        settings.addWidget(self.mode)
        settings.addWidget(label('清晰度', 'muted'))
        self.dpi = QComboBox()
        for value, text in [(150, '150 dpi · 快速'), (300, '300 dpi · 常用'), (600, '600 dpi · 精细')]:
            self.dpi.addItem(text, value)
        self.dpi.setCurrentIndex(1)
        settings.addWidget(self.dpi)
        settings.addWidget(label('扫描范围', 'muted'))
        self.paper = QComboBox()
        self.paper.addItem('A4  ·  210 × 297 mm', (210, 297))
        self.paper.addItem('A5  ·  148 × 210 mm', (148, 210))
        self.paper.addItem('整块玻璃 · 扫描后可裁剪', None)
        self.paper.addItem('小证件 · 100 × 70 mm', (100, 70))
        settings.addWidget(self.paper)
        self.mode.currentIndexChanged.connect(self.custom_profile)
        self.dpi.currentIndexChanged.connect(self.custom_profile)
        self.paper_hint = label('原稿对齐玻璃上的定位角。\n较小的证件可先扫整页，再框选裁剪。', 'muted', True)
        settings.addWidget(self.paper_hint)
        settings.addStretch()
        self.scan_btn = button('扫描第 1 页', self.scan, 'primary')
        self.scan_btn.setEnabled(False)
        self.scan_btn.setMinimumHeight(48)
        self.scan_btn.setShortcut('Ctrl+Return')
        self.scan_btn.setToolTip('扫描并追加到当前文档（Ctrl+Enter）')
        settings.addWidget(self.scan_btn)
        self.scan_hint = label('每点一次，追加一页', 'muted', True)
        settings.addWidget(self.scan_hint)
        body.addWidget(self.settings_card)

        workspace = QVBoxLayout()
        workspace.setSpacing(12)
        title_row = QHBoxLayout()
        title_row.addWidget(label('02  页面整理', 'section'))
        self.composite_btn = button('查看合页效果', self.toggle_composite)
        self.composite_btn.setCheckable(True)
        title_row.addWidget(self.composite_btn)
        title_row.addStretch()
        self.count = label('尚未扫描', 'muted')
        title_row.addWidget(self.count)
        workspace.addLayout(title_row)
        self.toolbar = QWidget()
        tools = QHBoxLayout(self.toolbar)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self.left_btn = button('↶ 左转', lambda: self.rotate(False))
        self.right_btn = button('↷ 右转', lambda: self.rotate(True))
        tools.addWidget(self.left_btn)
        tools.addWidget(self.right_btn)
        self.crop_btn = button('框选裁剪', self.toggle_crop)
        self.crop_btn.setCheckable(True)
        tools.addWidget(self.crop_btn)
        self.undo_btn = button('撤销编辑', self.undo)
        tools.addWidget(self.undo_btn)
        tools.addStretch()
        tools.addWidget(button('−', lambda: self.view.zoom(1 / 1.25)))
        tools.addWidget(button('适合窗口', lambda: self.view.fit()))
        tools.addWidget(button('＋', lambda: self.view.zoom(1.25)))
        workspace.addWidget(self.toolbar)
        splitter = QSplitter(Qt.Horizontal)
        self.stack = QStackedWidget()
        empty = QFrame()
        empty.setObjectName('card')
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(28, 30, 28, 30)
        empty_layout.addStretch()
        for text, name in [('▤', 'emptyIcon'), ('从第一张纸开始', 'emptyTitle'),
                           ('放好原稿，点击左侧“扫描第 1 页”', None),
                           ('扫描后在这里查看大图，右侧整理页序。', 'muted')]:
            item = label(text, name)
            item.setAlignment(Qt.AlignCenter)
            empty_layout.addWidget(item)
            empty_layout.addSpacing(8)
        empty_layout.addStretch()
        self.stack.addWidget(empty)
        self.view = Preview()
        self.view.cropped.connect(self.crop)
        self.stack.addWidget(self.view)
        splitter.addWidget(self.stack)
        self.pages_card = QFrame()
        self.pages_card.setObjectName('pagesCard')
        page_layout = QVBoxLayout(self.pages_card)
        page_layout.setContentsMargins(8, 12, 8, 10)
        self.page_title = label('页面  0', 'section')
        page_layout.addWidget(self.page_title)
        page_layout.addWidget(label('拖动缩略图调整顺序', 'muted'))
        self.pages_list = PageList()
        self.pages_list.setIconSize(QSize(108, 140))
        self.pages_list.setViewMode(QListWidget.IconMode)
        self.pages_list.setMovement(QListWidget.Snap)
        self.pages_list.setFlow(QListWidget.TopToBottom)
        self.pages_list.setWrapping(False)
        self.pages_list.setResizeMode(QListWidget.Adjust)
        self.pages_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.pages_list.setDefaultDropAction(Qt.MoveAction)
        self.pages_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.pages_list.currentItemChanged.connect(self.select_page)
        self.pages_list.reordered.connect(self.reorder)
        page_layout.addWidget(self.pages_list, 1)
        self.page_actions = QWidget()
        action_layout = QHBoxLayout(self.page_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(4)
        action_layout.addWidget(button('上移', lambda: self.move(-1)))
        action_layout.addWidget(button('下移', lambda: self.move(1)))
        page_layout.addWidget(self.page_actions)
        self.rescan_btn = button('重扫当前面', lambda: self.scan(True))
        self.rescan_btn.setToolTip('新扫描成功后替换当前面；取消或失败时保留原图。')
        page_layout.addWidget(self.rescan_btn)
        self.remove_btn = button('移除当前页', self.remove, 'danger')
        page_layout.addWidget(self.remove_btn)
        self.restore_btn = button('撤销移除', self.restore)
        self.restore_btn.hide()
        page_layout.addWidget(self.restore_btn)
        self.pages_card.setMinimumWidth(174)
        self.pages_card.setMaximumWidth(200)
        splitter.addWidget(self.pages_card)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([680, 184])
        workspace.addWidget(splitter, 1)
        self.page_info = label('预览区支持 Ctrl + 滚轮缩放', 'muted')
        workspace.addWidget(self.page_info)
        self.status = label('就绪。先扫描，再整理页面，最后保存文件。', 'status', True)
        workspace.addWidget(self.status)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        progress_row.addWidget(self.progress, 1)
        self.cancel_btn = button('取消扫描', self.cancel)
        self.cancel_btn.hide()
        progress_row.addWidget(self.cancel_btn)
        workspace.addLayout(progress_row)
        body.addLayout(workspace, 1)
        layout.addLayout(body, 1)

        savebar = QFrame()
        savebar.setObjectName('savebar')
        saving = QVBoxLayout(savebar)
        saving.setContentsMargins(24, 16, 24, 16)
        saving.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label('03  保存文档', 'section'))
        self.save_state = label('还没有页面', 'muted')
        top.addWidget(self.save_state)
        top.addStretch()
        self.open_btn = button('打开保存位置', self.open_output)
        top.addWidget(self.open_btn)
        saving.addLayout(top)
        self.save_fields = QWidget()
        fields = QHBoxLayout(self.save_fields)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(10)
        self.name = QLineEdit()
        self.name.setPlaceholderText('文件名，例如：会议材料')
        self.name.setMinimumWidth(180)
        self.format = QComboBox()
        self.format.addItem('PDF · 所有页面合为一份', 'pdf')
        self.format.addItem('JPG · 每页一张，存入新文件夹', 'jpg')
        self.format.addItem('PNG · 每页一张，存入新文件夹', 'png')
        self.format.setMinimumWidth(260)
        self.directory = QLineEdit()
        self.directory.setPlaceholderText('保存到的文件夹')
        self.directory.setMinimumWidth(180)
        fields.addWidget(self.name, 3)
        fields.addWidget(self.format, 3)
        fields.addWidget(self.directory, 4)
        fields.addWidget(button('选择文件夹', self.choose_directory))
        self.export_btn = button('保存 PDF', self.export, 'primary')
        self.export_btn.setShortcut('Ctrl+S')
        self.export_btn.setMinimumWidth(145)
        fields.addWidget(self.export_btn)
        saving.addWidget(self.save_fields)
        self.target_label = label('', 'muted', True)
        saving.addWidget(self.target_label)
        layout.addWidget(savebar)

    def default_name(self):
        return '扫描_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    def load_settings(self):
        settings = self.session.settings
        self.name.setText(settings.get('name', self.default_name()))
        self.directory.setText(settings.get('directory', str(Path.home() / 'Documents')))
        for field, key in [(self.format, 'format'), (self.mode, 'mode'), (self.dpi, 'dpi'), (self.paper, 'paper'), (self.layout_combo, 'layout')]:
            default = 'single' if key == 'layout' else field.currentData()
            value = settings.get(key, default)
            for index in range(field.count()):
                candidate = field.itemData(index)
                if candidate == value or (isinstance(candidate, tuple) and list(candidate) == value):
                    field.blockSignals(True)
                    field.setCurrentIndex(index)
                    field.blockSignals(False)
                    break
        self.custom_profile()
        self.configure_layout()

    def remember(self, changed=False):
        self.session.settings = {'name': self.name.text(), 'directory': self.directory.text(),
            'format': self.format.currentData(), 'mode': self.mode.currentData(),
            'dpi': self.dpi.currentData(), 'paper': self.paper.currentData(),
            'layout': self.layout_combo.currentData()}
        try:
            self.session.save(changed)
        except Exception as error:
            self.message('草稿保存失败，请及时导出文件：' + str(error), True)
        self.update_state()

    def message(self, text, error=False):
        self.status.setText(text)
        self.status.setStyleSheet('background: #fcece7; color: #a33e2b;' if error else '')

    def update_state(self):
        count = len(self.session.pages)
        layout = self.layout_combo.currentData()
        self.page_title.setText('页面  {}'.format(count))
        output_count = count if layout == 'single' else (count + 1) // 2
        self.count.setText(('{} 张原图 → {} 页 PDF'.format(count, output_count) if layout != 'single' else '共 {} 页'.format(count)) if count else '尚未扫描')
        if layout == 'card':
            self.scan_btn.setText(['扫描正面', '扫描反面', '正反面已齐'][min(count, 2)])
        else:
            self.scan_btn.setText('扫描下一页 · 第 {} 页'.format(count + 1) if count else '扫描第 1 页')
        ready = bool(self.device) and not self.busy and not self.detecting
        self.scan_btn.setEnabled(ready and (layout != 'card' or count < 2))
        selected = bool(self.current_page())
        self.toolbar.setEnabled(selected and not self.busy)
        for edit in (self.left_btn, self.right_btn, self.crop_btn, self.undo_btn):
            edit.setEnabled(selected and not self.busy and not self.composite_btn.isChecked())
        self.composite_btn.setEnabled(not self.busy and count > 0 and (layout != 'card' or count == 2))
        self.rescan_btn.setEnabled(selected and ready)
        self.page_actions.setEnabled(selected and not self.busy)
        self.remove_btn.setEnabled(selected and not self.busy)
        self.restore_btn.setEnabled(not self.busy)
        self.undo_btn.setEnabled(selected and bool(self.current_page().operations) and not self.busy and not self.composite_btn.isChecked())
        self.export_btn.setEnabled(count > 0 and not self.busy and (layout != 'card' or count == 2))
        self.open_btn.setEnabled(bool(self.session.last_export))
        self.save_state.setText('有更新待保存' if self.session.dirty else ('已保存 · 可继续扫描' if count else '还没有页面'))
        if layout == 'card' and count != 2:
            self.save_state.setText('请扫描正面和反面' if count < 2 else '仅需 2 张，请移除多余页或新建文档')
        self.stack.setCurrentIndex(1 if selected else 0)
        self.setWindowTitle(('● ' if self.session.dirty else '') + self.name.text() + ' — 清晰扫描')

    def update_target(self):
        kind = self.format.currentData()
        layout = self.layout_combo.currentData()
        self.export_btn.setText(('保存证件 PDF' if layout == 'card' else '保存合页 PDF' if layout == 'two_up' else '保存 PDF') if kind == 'pdf' else '保存图片')
        try:
            target = output_target(self.directory.text(), self.name.text(), kind)
            description = '将保存为：' if kind == 'pdf' else '将新建文件夹：'
            self.target_label.setText(description + str(target))
            self.target_label.setToolTip(str(target))
        except ValueError as error:
            self.target_label.setText(str(error))

    def configure_layout(self):
        layout = self.layout_combo.currentData()
        self.composite_btn.setVisible(layout != 'single')
        self.rescan_btn.setVisible(layout == 'card')
        self.pages_list.setIconSize(QSize(108, 68) if layout == 'card' else QSize(108, 140))
        self.format.setItemText(0, {'single': 'PDF · 所有页面合为一份', 'card': 'PDF · 正反面合为一页 A4', 'two_up': 'PDF · 每两页合为一页 A4'}[layout])
        for index in (1, 2):
            self.format.model().item(index).setEnabled(layout == 'single')
        if layout != 'single':
            self.format.setCurrentIndex(0)
            self.scan_hint.setText('扫完后分别裁剪，再查看合页效果')
            self.paper_hint.setText('原稿放在玻璃定位角。\n扫后裁掉空白；合页保持扫描尺寸，放不下时等比缩小。')
        else:
            self.scan_hint.setText('每点一次，追加一页')
            self.paper_hint.setText('原稿对齐玻璃上的定位角。\n较小的证件可先扫整页，再框选裁剪。')
            self.composite_btn.setChecked(False)
        self.composite_btn.setText('返回原图编辑' if self.composite_btn.isChecked() else '查看合页效果')
        self.update_target()

    def layout_changed(self):
        layout = self.layout_combo.currentData()
        self.composite_btn.setChecked(False)
        self.configure_layout()
        if not self.session.pages:
            self.paper.setCurrentIndex(3 if layout == 'card' else 0)
            if layout != 'single':
                self.profile.setCurrentIndex(1)
                self.apply_profile(1)
        selected = self.current_page()
        self.remember(True)
        self.rebuild_pages(selected.id if selected else None)
        if layout == 'card':
            self.message('先扫正面，再翻面扫描。分别裁掉空白后点“查看合页效果”，将上下排在同一张 A4 上。')
        elif layout == 'two_up':
            self.message('按顺序扫描户口本各页，分别裁掉空白。每两页上下合排为一张 A4，奇数末页单独占一张。')
        else:
            self.message('已切换为普通文档，每张扫描图分别保存为一页。')

    def toggle_composite(self):
        self.configure_layout()
        self.select_page()
        self.message('正在预览合页后的 A4 PDF。返回原图可分别裁剪和旋转，右侧可调整上下顺序。' if self.composite_btn.isChecked() else '已返回原图编辑，可分别裁剪、旋转，再查看合页效果。')

    def page_label(self, index):
        if self.layout_combo.currentData() == 'card':
            return ['正面', '反面'][index] if index < 2 else '多余页 {}'.format(index + 1)
        return '第 {} 页'.format(index + 1)

    def apply_profile(self, index):
        if index == 3:
            return
        mode, dpi = [('Gray', 300), ('Color', 300), ('Color', 600)][index]
        for field, value in [(self.mode, mode), (self.dpi, dpi)]:
            field.blockSignals(True)
            field.setCurrentIndex(field.findData(value))
            field.blockSignals(False)
        if hasattr(self, 'autosave'):
            self.remember()

    def custom_profile(self):
        pair = (self.mode.currentData(), self.dpi.currentData())
        profiles = [('Gray', 300), ('Color', 300), ('Color', 600)]
        index = profiles.index(pair) if pair in profiles else 3
        self.profile.blockSignals(True)
        self.profile.setCurrentIndex(index)
        self.profile.blockSignals(False)

    def detect(self):
        if self.busy or self.detecting:
            return
        self.detecting = True
        self.device = None
        self.device_label.setText('正在检测扫描仪…')
        self.detect_btn.setEnabled(False)
        self.update_state()
        self.detector = DeviceWorker()
        self.detector.result.connect(self.detected)
        self.detector.failure.connect(self.detection_failed)
        self.detector.finished.connect(self.detection_finished)
        self.detector.start()

    def detected(self, result):
        self.device, description, self.caps = result
        self.device_label.setText('●  HP LaserJet M1005\nUSB 已连接')
        self.device_label.setToolTip(description + '\n' + self.device)
        self.message('扫描仪已连接。放好原稿后，点击左侧扫描按钮。')

    def detection_failed(self, error):
        self.device_label.setText('扫描仪尚未连接')
        self.message(error, True)

    def detection_finished(self):
        self.detecting = False
        self.detect_btn.setEnabled(True)
        self.update_state()

    def current_page(self):
        item = self.pages_list.currentItem()
        if not item:
            return None
        return next((page for page in self.session.pages if page.id == item.data(Qt.UserRole)), None)

    def rebuild_pages(self, selected=None):
        self.rebuilding = True
        self.pages_list.blockSignals(True)
        self.pages_list.clear()
        for index, page in enumerate(self.session.pages):
            item = QListWidgetItem(self.page_label(index))
            item.setData(Qt.UserRole, page.id)
            item.setTextAlignment(Qt.AlignCenter)
            item.setSizeHint(QSize(136, 110 if self.layout_combo.currentData() == 'card' else 176))
            try:
                item.setIcon(QIcon(pixmap(page.image(), (108, 140))))
            except Exception as error:
                item.setToolTip('原图读取失败：' + str(error))
            self.pages_list.addItem(item)
            if page.id == selected:
                self.pages_list.setCurrentItem(item)
        if self.pages_list.currentRow() < 0 and self.pages_list.count():
            self.pages_list.setCurrentRow(self.pages_list.count() - 1)
        self.pages_list.blockSignals(False)
        self.rebuilding = False
        self.select_page()
        self.update_state()

    def select_page(self, *args):
        self.crop_btn.setChecked(False)
        self.view.crop_mode(False)
        page = self.current_page()
        if page:
            try:
                layout = self.layout_combo.currentData()
                if layout == 'card' and len(self.session.pages) != 2:
                    self.composite_btn.setChecked(False)
                composite = self.composite_btn.isChecked() and layout != 'single'
                row = self.pages_list.currentRow()
                image = layout_preview(self.session.pages, layout, row // 2) if composite else page.image()
                self.view.set_image(image)
                if composite:
                    self.page_info.setText('A4 合页预览 · 第 {} 页 PDF · {} · 返回原图可分别裁剪和旋转'.format(row // 2 + 1, '正面在上，反面在下' if layout == 'card' else '按右侧页序，上下合排'))
                else:
                    self.page_info.setText('{}  ·  {} × {} 像素  ·  {} dpi  ·  Ctrl + 滚轮缩放'.format(self.page_label(row), image.width, image.height, page.dpi))
            except Exception as error:
                self.message('页面读取失败：' + str(error), True)
        else:
            self.page_info.setText('预览区支持 Ctrl + 滚轮缩放')
        self.composite_btn.setText('返回原图编辑' if self.composite_btn.isChecked() else '查看合页效果')
        self.update_state()

    def reorder(self, *args):
        if self.rebuilding:
            return
        lookup = {page.id: page for page in self.session.pages}
        self.session.pages = [lookup[self.pages_list.item(i).data(Qt.UserRole)] for i in range(self.pages_list.count())]
        for index in range(self.pages_list.count()):
            self.pages_list.item(index).setText(self.page_label(index))
        self.remember(True)
        self.select_page()

    def move(self, delta):
        index = self.pages_list.currentRow()
        other = index + delta
        if index >= 0 and 0 <= other < len(self.session.pages):
            page = self.session.pages[index]
            self.session.pages[index], self.session.pages[other] = self.session.pages[other], page
            self.remember(True)
            self.rebuild_pages(page.id)

    def rotate(self, clockwise):
        page = self.current_page()
        if page:
            page.operations.append({'type': 'rotate', 'clockwise': clockwise})
            self.remember(True)
            self.rebuild_pages(page.id)

    def toggle_crop(self):
        self.view.crop_mode(self.crop_btn.isChecked())
        self.message('在预览上拖出要保留的范围，松开即可裁剪。可用“撤销编辑”恢复。' if self.crop_btn.isChecked() else '已退出裁剪。')

    def crop(self, box):
        page = self.current_page()
        if page and not self.busy:
            page.operations.append({'type': 'crop', 'box': box})
            self.remember(True)
            self.rebuild_pages(page.id)
            self.message('已裁剪当前页。原始扫描仍保留，可点击“撤销编辑”恢复。')

    def undo(self):
        page = self.current_page()
        if page and page.operations:
            page.operations.pop()
            self.remember(True)
            self.rebuild_pages(page.id)
            self.message('已撤销当前页的上一次编辑。')

    def remove(self):
        page = self.current_page()
        if page:
            index = self.session.pages.index(page)
            self.removed.append((index, page))
            self.session.pages.pop(index)
            self.restore_btn.show()
            self.remember(True)
            self.rebuild_pages()
            self.message('已从文档中移除该页。需要恢复时点击“撤销移除”。')

    def restore(self):
        if self.removed:
            index, page = self.removed.pop()
            self.session.pages.insert(min(index, len(self.session.pages)), page)
            self.restore_btn.setVisible(bool(self.removed))
            self.remember(True)
            self.rebuild_pages(page.id)

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.settings_card, self.pages_list, self.save_fields, self.new_btn, self.history_btn, self.layout_combo):
            widget.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.update_state()

    def scan(self, replace=False):
        if self.busy or not self.device or self.detecting:
            return
        card = self.layout_combo.currentData() == 'card'
        selected = self.current_page()
        if replace and not selected:
            return
        if card and len(self.session.pages) >= 2 and not replace:
            self.message('正反面已齐。需要重新扫描时，选中一面后点击“重扫当前面”。')
            return
        self.scan_target_id = selected.id if replace else None
        self.composite_btn.setChecked(False)
        self.select_page()
        try:
            arguments = scan_arguments(self.device, self.dpi.currentData(), self.mode.currentData(), self.paper.currentData(), self.caps)
        except ValueError as error:
            self.message(str(error), True)
            return
        self.remember()
        self.scan_dpi = self.dpi.currentData()
        self.raw = self.session.directory / (uuid.uuid4().hex + '.pnm.part')
        self.cancelled, self.stderr = False, ''
        self.set_busy(True)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText('取消扫描')
        self.cancel_btn.show()
        self.progress.setRange(0, 0)
        target_index = self.session.pages.index(selected) if replace else len(self.session.pages)
        self.message('正在扫描{}… {}'.format(self.page_label(target_index), '成功后替换当前面，取消或失败时保留原图。' if replace else '完成后会追加到右侧，已有页面保留。'))
        self.process = QProcess(self)
        from PyQt5.QtCore import QProcessEnvironment
        environment = QProcessEnvironment()
        for key, value in sane_env().items():
            environment.insert(key, value)
        self.process.setProcessEnvironment(environment)
        self.process.setStandardOutputFile(str(self.raw))
        self.process.readyReadStandardError.connect(self.scan_progress)
        self.process.finished.connect(self.scan_finished)
        self.process.errorOccurred.connect(self.scan_error)
        self.process.start(arguments[0], arguments[1:])
        self.scan_timeout.start(300000)

    def scan_progress(self):
        text = bytes(self.process.readAllStandardError()).decode('utf-8', 'replace')
        self.stderr = (self.stderr + text)[-6000:]
        matches = re.findall(r'Progress:\s*([\d.]+)%', self.stderr)
        if matches:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(99, int(float(matches[-1]))))

    def scan_error(self, error):
        if error == QProcess.FailedToStart:
            self.scan_timeout.stop()
            self.cancel_btn.hide()
            self.set_busy(False)
            self.message('扫描程序无法启动，请检查 scanimage 是否可用。', True)

    def cancel(self):
        if self.process and self.process.state() != QProcess.NotRunning:
            self.cancelled = True
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText('正在取消…')
            process = self.process
            kill_timer = QTimer(process)
            kill_timer.setSingleShot(True)
            kill_timer.timeout.connect(process.kill)
            process.finished.connect(kill_timer.stop)
            kill_timer.start(1800)
            process.terminate()

    def timed_out(self):
        self.cancel()
        self.message('扫描超过 5 分钟，正在停止。请检查扫描仪连接。', True)

    def scan_finished(self, code, status):
        self.scan_timeout.stop()
        self.scan_progress()
        self.cancel_btn.hide()
        if self.cancelled or code != 0 or status != QProcess.NormalExit:
            if self.raw.exists():
                self.raw.unlink()
            self.set_busy(False)
            if self.cancelled:
                self.message('扫描已取消，已有页面保留。')
            else:
                detail = re.sub(r'Progress:\s*[\d.]+%\s*', '', self.stderr).strip()[-500:]
                self.message('扫描失败。请关闭其他扫描软件，检查 USB 连接后重试。\n' + detail, True)
            return
        self.progress.setRange(0, 0)
        self.message('扫描完成，正在生成预览并保留草稿…')
        raw, directory, dpi = self.raw, self.session.directory, self.scan_dpi
        self.start_work(lambda progress: finish_scan(raw, directory, dpi), self.scanned)

    def start_work(self, function, success):
        self.worker = Work(function)
        self.worker.result.connect(success)
        self.worker.failure.connect(self.work_failed)
        self.worker.progress.connect(self.export_progress)
        self.worker.finished.connect(self.work_finished)
        self.worker.start()

    def scanned(self, page):
        replacement = next((i for i, old in enumerate(self.session.pages) if old.id == self.scan_target_id), None)
        if replacement is None:
            self.session.pages.append(page)
        else:
            self.session.pages[replacement] = page
        self.remember(True)
        self.rebuild_pages(page.id)
        if self.raw.exists():
            self.raw.unlink()
        if self.layout_combo.currentData() == 'card':
            self.message('正面已加入。可框选裁掉空白，再翻面点击“扫描反面”。' if len(self.session.pages) == 1 else '正反面已齐。分别裁掉空白并转正后，点“查看合页效果”，再保存为一页 A4 PDF。')
        else:
            self.message('第 {} 页已加入。换好下一张后可继续扫描，或在下方保存整份文档。'.format(len(self.session.pages)))

    def work_failed(self, error):
        self.message('操作未完成：' + error + '\n扫描页和原有文件已保留。', True)

    def work_finished(self):
        self.set_busy(False)

    def choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, '选择扫描文件的保存位置', self.directory.text())
        if directory:
            self.directory.setText(directory)

    def export(self):
        if self.busy or not self.session.pages:
            return
        self.autosave.stop()
        self.remember()
        kind = self.format.currentData()
        layout = self.layout_combo.currentData()
        try:
            if layout == 'card' and len(self.session.pages) != 2:
                raise ValueError('请先准备正面、反面各一张，共 2 张原图。')
            target = output_target(self.directory.text(), self.name.text(), kind)
            if target.exists():
                raise ValueError('这个名称已存在，请修改文件名。已有文件会保留。')
        except Exception as error:
            self.message(str(error), True)
            self.name.setFocus()
            return
        self.set_busy(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.message('正在保存 {} 页…'.format(len(self.session.pages)))
        pages = copy.deepcopy(self.session.pages)
        self.start_work(lambda progress: export_pages(pages, target, kind, progress, layout), self.exported)

    def export_progress(self, value):
        self.progress.setRange(0, 100)
        self.progress.setValue(value)

    def exported(self, path):
        self.session.last_export = path
        self.session.exported_revision = self.session.revision
        self.remember()
        size = Path(path).stat().st_size if Path(path).is_file() else sum(item.stat().st_size for item in Path(path).iterdir())
        count = len(self.session.pages)
        if self.layout_combo.currentData() != 'single':
            count = (count + 1) // 2
        self.message('已保存 {} 页 · {:.1f} MB\n{}'.format(count, size / 1024 / 1024, path))

    def open_output(self):
        path = Path(self.session.last_export)
        folder = path if path.is_dir() else path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def new_document(self):
        if self.busy or self.detecting:
            return
        self.autosave.stop()
        self.remember()
        settings = dict(self.session.settings)
        self.session = Session(self.session.root, fresh=True)
        settings['name'] = self.default_name()
        self.session.settings = settings
        self.session.save()
        self.removed = []
        self.restore_btn.hide()
        self.composite_btn.setChecked(False)
        self.load_settings()
        self.rebuild_pages()
        self.message('已新建文档。上一份草稿保留在“打开历史草稿”中。')

    def history(self):
        if self.busy or self.detecting:
            return
        from PyQt5.QtWidgets import QDialog, QDialogButtonBox
        self.autosave.stop()
        self.remember()
        dialog = QDialog(self)
        dialog.setWindowTitle('打开历史草稿')
        dialog.resize(620, 380)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label('双击一份草稿，继续整理和保存。', 'muted'))
        entries = QListWidget()
        layout.addWidget(entries)
        for manifest in sorted((self.session.root / 'workspaces').glob('*/draft.json'), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                data = json.loads(manifest.read_text(encoding='utf-8'))
                if not data['pages'] or manifest.parent == self.session.directory:
                    continue
                stamp = datetime.datetime.fromtimestamp(manifest.stat().st_mtime).strftime('%m-%d %H:%M')
                item = QListWidgetItem('{}  ·  {} 页  ·  {}'.format(data.get('settings', {}).get('name', '未命名'), len(data['pages']), stamp))
                item.setData(Qt.UserRole, manifest.parent.name)
                entries.addItem(item)
            except (ValueError, OSError, KeyError):
                continue
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Open).setText('打开草稿')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        entries.itemDoubleClicked.connect(dialog.accept)
        layout.addWidget(buttons)
        if dialog.exec_() and entries.currentItem():
            from core import atomic_json
            previous = self.session.directory.name
            try:
                atomic_json(self.session.root / 'current.json', {'workspace': entries.currentItem().data(Qt.UserRole)})
                restored = Session(self.session.root)
            except Exception as error:
                atomic_json(self.session.root / 'current.json', {'workspace': previous})
                self.message('无法打开草稿：' + str(error), True)
                return
            self.session = restored
            self.removed = []
            self.restore_btn.hide()
            self.composite_btn.setChecked(False)
            self.load_settings()
            self.rebuild_pages()
            self.message('草稿已打开，可以继续扫描、编辑或保存。')

    def closeEvent(self, event):
        if self.busy or self.detecting or (self.worker and self.worker.isRunning()):
            self.message('请等当前操作结束；扫描时可以先点击“取消扫描”。', True)
            event.ignore()
            return
        self.autosave.stop()
        self.remember()
        event.accept()


def main():
    os.umask(0o077)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setApplicationName('清晰扫描')
    app.setDesktopFileName('qingxi-scan')
    app.setStyle('Fusion')
    app.setFont(QFont('Noto Sans CJK SC', 10))
    app.setStyleSheet(STYLE)
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = QLockFile(str(DATA_DIR / 'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, '清晰扫描已经打开', '请切换到已经打开的“清晰扫描”窗口。')
        return 0
    try:
        session = Session()
    except Exception as error:
        QMessageBox.critical(None, '草稿读取失败', '原草稿已保留。请联系维护人员检查：\n' + str(error))
        return 1
    window = Window(session)
    window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
