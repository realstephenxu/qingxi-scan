"""Local, non-destructive scan storage and export. No network services."""
import errno
import io
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'qingxi-scan'
SANE_DIR = APP_DIR / 'sane'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sane_env():
    env = dict(os.environ)
    # Only load the already verified M1005 backend. Other installed backends
    # can block discovery for tens of seconds or expose the same device twice.
    env['SANE_CONFIG_DIR'] = str(SANE_DIR)
    env['LC_ALL'] = 'C'
    return env


@dataclass
class Page:
    id: str
    source: str
    dpi: int = 300
    operations: list = field(default_factory=list)

    def image(self):
        with Image.open(self.source) as source:
            image = source.convert('RGB' if source.mode == 'RGB' else 'L')
        for operation in self.operations:
            if operation['type'] == 'rotate':
                image = image.transpose(Image.ROTATE_270 if operation['clockwise'] else Image.ROTATE_90)
            elif operation['type'] == 'crop':
                box = tuple(operation['box'])
                if not (0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height):
                    raise ValueError('裁剪范围超出了页面边界')
                image = image.crop(box)
        return image


class Session:
    def __init__(self, root=DATA_DIR, fresh=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.root / 'current.json'
        if not fresh and current.exists():
            pointer = json.loads(current.read_text(encoding='utf-8'))
            workspace = pointer['workspace']
            if not re.fullmatch(r'[0-9a-f]{32}', workspace):
                raise ValueError('草稿索引无效')
            self.directory = self.root / 'workspaces' / workspace
            data = json.loads((self.directory / 'draft.json').read_text(encoding='utf-8'))
            self.pages = [Page(**page) for page in data['pages']]
            for page in self.pages:
                if Path(page.source).resolve().parent != self.directory.resolve():
                    raise ValueError('草稿页面路径无效')
                if not Path(page.source).is_file():
                    raise ValueError('草稿缺少页面原图：' + Path(page.source).name)
            self.settings = data.get('settings', {})
            self.revision = data.get('revision', 0)
            self.exported_revision = data.get('exported_revision', -1)
            self.last_export = data.get('last_export', '')
        else:
            self.directory = self.root / 'workspaces' / uuid.uuid4().hex
            self.directory.mkdir(parents=True, mode=0o700)
            self.pages, self.settings = [], {}
            self.revision, self.exported_revision, self.last_export = 0, -1, ''
            self.save()
            atomic_json(current, {'workspace': self.directory.name})

    @property
    def dirty(self):
        return bool(self.pages) and self.revision != self.exported_revision

    def save(self, changed=False):
        if changed:
            self.revision += 1
        atomic_json(self.directory / 'draft.json', {
            'pages': [asdict(page) for page in self.pages],
            'settings': self.settings, 'revision': self.revision,
            'exported_revision': self.exported_revision, 'last_export': self.last_export,
        })


def parse_devices(output):
    return re.findall(r"device `([^']+)' is a (.+)", output)


def parse_capabilities(output):
    resolutions = re.search(r'--resolution ([0-9|]+)dpi', output)
    modes = re.search(r'--mode ([A-Za-z|]+)', output)
    width = re.search(r'-x 0\.\.([\d.]+)mm', output)
    height = re.search(r'-y 0\.\.([\d.]+)mm', output)
    if not all((resolutions, modes, width, height)):
        raise ValueError('无法读取 M1005 的扫描参数，请检查驱动或关闭其他扫描程序。')
    return {'resolutions': [int(v) for v in resolutions[1].split('|')],
            'modes': modes[1].split('|'), 'width': float(width[1]), 'height': float(height[1])}


def scan_arguments(device, dpi, mode, size, caps):
    if not device or dpi not in caps['resolutions'] or mode not in caps['modes']:
        raise ValueError('扫描设备或参数无效，请重新检测设备。')
    width, height = size if size else (caps['width'], caps['height'])
    if not (0 < width <= caps['width'] and 0 < height <= caps['height']):
        raise ValueError('纸张尺寸超出扫描玻璃范围。')
    return ['scanimage', '--device-name=' + device, '--format=pnm', '--progress',
            '--resolution=' + str(dpi), '--mode=' + mode,
            '-l', '0', '-t', '0', '-x', str(width), '-y', str(height)]


def finish_scan(raw, directory, dpi):
    target = Path(directory) / (uuid.uuid4().hex + '.png')
    temporary = target.with_suffix('.png.part')
    try:
        with Image.open(str(raw)) as image:
            image.load()  # Reject truncated scans, including cancelled acquisition.
            if image.width < 10 or image.height < 10:
                raise ValueError('扫描数据不完整，请重新扫描。')
            image.save(str(temporary), 'PNG', dpi=(dpi, dpi))
        os.chmod(str(temporary), 0o600)
        os.replace(str(temporary), str(target))
        return Page(uuid.uuid4().hex, str(target), dpi)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_name(name):
    name = name.strip()
    if not name or name in ('.', '..') or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        raise ValueError('请填写文件名；文件名不能含有 / \\ : * ? " < > |。')
    if len(name) > 100:
        raise ValueError('文件名请控制在 100 个字符以内。')
    return name


def output_target(directory, name, kind):
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise ValueError('保存文件夹不存在，请点击“选择文件夹”。')
    name = validate_name(name)
    if kind == 'pdf':
        if name.lower().endswith('.pdf'):
            name = name[:-4]
        validate_name(name)
        return directory / (name + '.pdf')
    return directory / name


def publish_new(temporary, target):
    """Publish complete output without replacing any existing user file."""
    try:
        os.link(str(temporary), str(target))
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV):
            raise
        # Some removable filesystems do not support hard links.
        with open(str(target), 'xb') as destination:
            try:
                with open(str(temporary), 'rb') as source:
                    shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            except BaseException:
                os.unlink(str(target))
                raise
    os.unlink(str(temporary))


def layout_sheets(pages, layout='single'):
    """Return PDF points and placements, shared by preview and PDF export.

    Rectangles use a bottom-left origin. Pair layouts never stretch or enlarge
    a scan; its DPI preserves physical size unless a slot requires shrinking.
    """
    if layout not in ('single', 'card', 'two_up'):
        raise ValueError('不支持的合页方式。')
    if layout == 'card' and len(pages) != 2:
        raise ValueError('身份证合页需要正面、反面各一张，共 2 张原图。')
    dimensions = []
    for page in pages:
        image = page.image()
        dimensions.append((image.width * 72 / page.dpi, image.height * 72 / page.dpi))
    if layout == 'single':
        return [(size, [(i, 0, 0, *size)]) for i, size in enumerate(dimensions)]
    mm = 72 / 25.4
    width, height = 210 * mm, 297 * mm
    if layout == 'card':
        sizes = []
        for w, h in dimensions:
            scale = min(1, 180 * mm / w, 120 * mm / h)
            sizes.append((w * scale, h * scale))
        gap = 16 * mm
        cursor = (height + sum(h for w, h in sizes) + gap) / 2
        placements = []
        for index, (w, h) in enumerate(sizes):
            cursor -= h
            placements.append((index, (width - w) / 2, cursor, w, h))
            cursor -= gap
        return [((width, height), placements)]
    margin, gap = 15 * mm, 12 * mm
    slot_w, slot_h = width - 2 * margin, (height - 2 * margin - gap) / 2
    sheets = []
    for start in range(0, len(pages), 2):
        placements = []
        for index in range(start, min(start + 2, len(pages))):
            w, h = dimensions[index]
            scale = min(1, slot_w / w, slot_h / h)
            w, h = w * scale, h * scale
            top = height - margin - (index - start) * (slot_h + gap)
            placements.append((index, (width - w) / 2, top - (slot_h + h) / 2, w, h))
        sheets.append(((width, height), placements))
    return sheets


def layout_preview(pages, layout, sheet_index=0, dpi=110):
    sheets = layout_sheets(pages, layout)
    size, placements = sheets[sheet_index]
    factor = dpi / 72
    result = Image.new('RGB', tuple(round(v * factor) for v in size), 'white')
    for index, x, y, w, h in placements:
        image = pages[index].image().convert('RGB')
        image = image.resize((max(1, round(w * factor)), max(1, round(h * factor))), Image.LANCZOS)
        result.paste(image, (round(x * factor), round((size[1] - y - h) * factor)))
    return result


def export_pages(pages, target, kind, progress=lambda value: None, layout='single'):
    if not pages:
        raise ValueError('请先扫描至少一页。')
    if layout != 'single' and kind != 'pdf':
        raise ValueError('证件合页请保存为 PDF。')
    sheets = layout_sheets(pages, layout) if kind == 'pdf' else None
    target = Path(target)
    if target.exists():
        raise FileExistsError('目标名称已存在。请更换名称，已有文件不会被覆盖。')
    if kind == 'pdf':
        fd, temporary = tempfile.mkstemp(prefix='.qingxi-', suffix='.pdf', dir=str(target.parent))
        os.close(fd)
        try:
            pdf = canvas.Canvas(temporary, pageCompression=1)
            pdf.setTitle(target.stem)
            pdf.setCreator('清晰扫描')
            for sheet_index, (size, placements) in enumerate(sheets):
                pdf.setPageSize(size)
                for index, x, y, width, height in placements:
                    image = pages[index].image()
                    buffer = io.BytesIO()
                    image.save(buffer, 'JPEG', quality=95, subsampling=0)
                    buffer.seek(0)
                    pdf.drawImage(ImageReader(buffer), x, y, width=width, height=height)
                pdf.showPage()
                progress(round((sheet_index + 1) * 100 / len(sheets)))
            pdf.save()
            publish_new(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    elif kind in ('jpg', 'png'):
        staging = Path(tempfile.mkdtemp(prefix='.qingxi-', dir=str(target.parent)))
        created = False
        try:
            for index, page in enumerate(pages):
                image = page.image()
                path = staging / ('{:03d}.{}'.format(index + 1, kind))
                options = {'quality': 95, 'subsampling': 0} if kind == 'jpg' else {}
                image.save(str(path), dpi=(page.dpi, page.dpi), **options)
                os.chmod(str(path), 0o600)
                progress(round((index + 1) * 100 / len(pages)))
            # Reserve a brand-new folder. Never merge into an existing folder.
            target.mkdir(mode=0o700)
            created = True
            for path in staging.iterdir():
                os.rename(str(path), str(target / path.name))
        except BaseException:
            if created:
                shutil.rmtree(str(target))
            raise
        finally:
            shutil.rmtree(str(staging))
    else:
        raise ValueError('不支持的保存格式')
    return str(target)
