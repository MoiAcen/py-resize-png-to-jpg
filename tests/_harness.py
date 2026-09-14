"""測試共用骨架

每個測試都在自己的暫存資料夾裡跑，config 的路徑全部改指到那裡，
所以不會碰到你真正的收藏庫，也不會留下任何快取檔喵！
"""
import contextlib
import io
import json
import random
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# config 必須在其他模組之前被改寫：它們是用 from config import X 把值複製走的
import config  # noqa: E402

# 這些路徑一律改指到暫存區，測試才不會動到真實資料
_REDIRECTED_PATHS = (
    'LOG_FILE', 'TARGETS_CACHE', 'HASH_CACHE_FILE',
    'LOWGAIN_FLAG_FILE', 'MANUAL_TAG_FILE', 'TAG_RULE_FILE',
)


class Sandbox:
    """一次性的測試環境：來源庫、工作區、失敗區與各式快取檔。"""

    def __init__(self, **overrides):
        self.root = Path(tempfile.mkdtemp(prefix='pyresize_test_'))
        self.src = self.root / 'CG'
        self.work = self.root / 'resize'
        self.failed = self.root / 'failed'
        self.tmp = self.root / 'tmp'
        for d in (self.src, self.work, self.failed, self.tmp):
            d.mkdir(parents=True, exist_ok=True)

        config.SOURCE_DIR = str(self.src)
        config.TARGET_DIR = str(self.work)
        config.FAILED_DIR = str(self.failed)
        config.DONE_DIR = ''
        config.MIN_ARCHIVE_SIZE_MB = 0
        for name in _REDIRECTED_PATHS:
            setattr(config, name, self.root / f'{name.lower()}.dat')
        for key, value in overrides.items():
            setattr(config, key, value)

    def sync(self, *modules):
        """把改過的 config 值同步到已經 import 的模組（它們複製走的是舊值）。"""
        names = _REDIRECTED_PATHS + (
            'SOURCE_DIR', 'TARGET_DIR', 'FAILED_DIR', 'DONE_DIR',
            'MIN_ARCHIVE_SIZE_MB', 'MAX_TARGET_FILES', 'EXTRA_SCAN_QUOTA',
            'SHOW_TOP_N', 'SAVE_RANK_N', 'RANK_PAGE_SIZE', 'RULE_TAG_SHOW_N',
            'EXTRA_ANALYZE_DEFAULT', 'MIN_ARCHIVE_SAVING_RATIO',
            'IGNORED_TAG_KEYS', 'JPEG_LARGE_KB',
        )
        for module in modules:
            for name in names:
                if hasattr(module, name):
                    setattr(module, name, getattr(config, name))

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def noisy_image(seed=0, w=400, h=300):
    """造一張高頻雜訊圖：壓縮不掉，最能逼出各種邊界行為。"""
    from PIL import Image
    random.seed(seed)
    im = Image.new('RGB', (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            v = (x * 5 + y * 11 + random.randint(0, 120)) % 256
            px[x, y] = (v, (v * 3) % 256, (v * 7) % 256)
    return im


def make_zip(path, members):
    """members: [(壓縮包內檔名, 'png'|'jpg'|'text', seed 或內容)]"""
    path = Path(path)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        staged = []
        for arcname, kind, payload in members:
            dst = td / Path(arcname).name
            if kind == 'png':
                noisy_image(seed=payload).save(dst, 'PNG')
            elif kind == 'jpg':
                noisy_image(seed=payload).save(dst, 'JPEG', quality=99, subsampling=0)
            else:
                dst.write_text(str(payload), encoding='utf-8')
            staged.append((dst, arcname))
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for dst, arcname in staged:
                zf.write(dst, arcname)
    return path


def make_png_zip(path, seed=0, count=1):
    """最常用的情境：一包 PNG。"""
    return make_zip(path, [(f'p{i}.png', 'png', seed + i) for i in range(count)])


def capture(fn, *args, **kwargs):
    """收走 stdout，回傳 (輸出字串, 回傳值)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return buf.getvalue(), result


def feed_input(module, answers):
    """把模組裡的 input() 換成固定答案序列，用來跑互動式流程。"""
    it = iter(answers)
    module.input = lambda *a, **k: next(it)


def read_json(path):
    with open(path, 'r', encoding='utf-8', errors='surrogatepass') as f:
        return json.load(f)


class Checker:
    """收集檢查結果，最後一次回報。"""

    def __init__(self, title):
        self.title = title
        self.failures = []
        self.passed = 0
        print(f"\n===== {title} =====")

    def section(self, text):
        print(f"\n--- {text} ---")

    def check(self, condition, message):
        if condition:
            self.passed += 1
            print(f"  ✅ {message}")
        else:
            self.failures.append(message)
            print(f"  ❌ {message}")
        return bool(condition)

    def finish(self):
        total = self.passed + len(self.failures)
        if self.failures:
            print(f"\n❌ {self.title}: {len(self.failures)}/{total} 項未通過")
            for f in self.failures:
                print(f"   - {f}")
            return 1
        print(f"\n✅ {self.title}: {total} 項全部通過")
        return 0
