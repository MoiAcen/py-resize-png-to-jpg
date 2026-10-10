"""JPG 重壓效率研究工具（獨立執行，輸入檔案唯讀，不會被修改）

回答一個問題：「JPG 重新壓縮為什麼只省 30%，不像 PNG 轉 JPG 能省 90%？」
對一批 JPG（或單一一張）實測，沿用 resize.py 的編碼與分流函式，所以量到的
就是正式流程實際會產生的結果，而不是另一套估算。

用法：
    python jpeg_efficiency.py <輸入> [--samples 30] [--pick random|largest]
                                      [--qualities 70,80,85,90,92,95] [--out 資料夾]

<輸入> 可以是：單一 .jpg / 資料夾 / .zip / .rar / .7z 壓縮包（rar 與 7z 需要 7-Zip）

產出（預設在 jpeg_efficiency_out/）：
    jpeg_efficiency.html   圖表報告（雙擊用瀏覽器開，內嵌 SVG，不需要網路）
    jpeg_efficiency.csv    每張圖的原始數字（Excel 可直接開，方便自己再分析）
並在終端機印出摘要，可以直接貼給我一起看喵！

PSNR 比的是「重壓後」對「來源 JPG 解碼結果」——量的是這次重壓多損失了多少，
不是相對於最原始的畫作。
"""
import argparse
import base64
import csv
import html
import io
import math
import os
import random
import re
import shutil
import subprocess
import statistics
import sys
import tempfile
import webbrowser
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from config import (
    BASE_DIR, JPEG_EXTENSIONS, JPEG_LARGE_KB, JPEG_TARGET_QUALITY,
    JPEG_FLAT_QUALITY, JPEG_FLAT_COLOR_RATIO, JPEG_TARGET_SUBSAMPLING,
    MIN_ARCHIVE_SAVING_RATIO, MAX_WORKERS, TEMP_WORK_DIR,
)
from common_utils import clean_str, list_7z_entries, SEVEN_ZIP_PATH
from jpeg_inspector import inspect_jpeg, classify_jpeg
from quality_test import psnr
import resize   # 沿用正式流程的編碼，量到的就是 resize.py 實際會產生的結果

DEFAULT_QUALITIES = (70, 75, 80, 85, 88, 90, 92, 95)
THUMB_EDGE = 180
PNG_TYPICAL_SAVING = 0.90      # 使用者觀察到的 PNG→JPG 常見縮減，圖上畫成參考線
MAX_FAINT_LINES = 60           # 逐檔細線最多畫幾條，免得 SVG 肥到打不開
TABLE_ROWS = 200
SEVEN_ZIP_SUFFIXES = ('.rar', '.7z')   # 這兩種交給 7-Zip；.zip 用 Python 內建的 zipfile
MAX_CMD_CHARS = 16000                  # 一次 7z 指令的成員名稱總長上限，Windows 命令列約 32k 字元


# ================= 取樣 =================
def _scratch_dir():
    """暫存解壓用的資料夾：優先用 TEMP_WORK_DIR（與收藏庫分開，分攤寫入損耗）。"""
    if TEMP_WORK_DIR:
        try:
            base = Path(TEMP_WORK_DIR)
            base.mkdir(parents=True, exist_ok=True)
            return str(base)
        except Exception:
            pass
    return None


def _pick(items, count, how, seed):
    """items: [(size, ...)]，依大小挑出 count 個。largest = 最大的；random = 固定種子隨機。"""
    if count <= 0 or len(items) <= count:
        return list(items)
    if how == 'largest':
        return sorted(items, key=lambda it: it[0], reverse=True)[:count]
    return random.Random(seed).sample(items, count)


def _chunks_by_chars(items, limit=MAX_CMD_CHARS):
    """把名稱依總字元數分批，每批不超過 limit，順序不變。單一名稱再長也自成一批。"""
    batch, size = [], 0
    for it in items:
        if batch and size + len(it) + 1 > limit:
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += len(it) + 1
    if batch:
        yield batch


def _member_disk_path(workdir, member):
    """7-Zip 解出來的路徑：保留壓縮包內的資料夾結構，分隔符不分 Windows / Unix 一律處理。"""
    return Path(workdir).joinpath(*[p for p in re.split(r'[\\/]', member) if p])


def _samples_from_7z(p, count, how, seed, min_kb, workdir):
    """rar / 7z：先列清單挑出 JPG，再只把被挑中的成員解出來，原檔不動。"""
    exe = SEVEN_ZIP_PATH
    if not exe:
        print(f"❌ 讀取 {p.suffix} 需要 7-Zip，但找不到。請安裝 7-Zip，"
              "或在 config.py 的 POSSIBLE_7Z_PATHS 補上它的路徑。")
        return []
    listing = list_7z_entries(p, exe)
    if listing is None:
        print(f"❌ 7-Zip 打不開這個壓縮包（損毀、加密或格式不支援）：{p.name}")
        return []

    min_bytes = max(0, min_kb) * 1024
    members = [(size, name) for name, size in listing['entries']
               if name.lower().endswith(JPEG_EXTENSIONS) and size >= min_bytes]
    if not members:
        print(f"❌ 壓縮包裡沒有 >= {min_kb} KB 的 JPG：{p.name}")
        return []

    chosen = _pick(members, count, how, seed)
    total_mb = sum(size for size, _n in chosen) / 1024 / 1024
    print(f"📦 {p.name}：共 {len(members)} 張符合條件的 JPG，抽 {len(chosen)} 張"
          f"（約 {total_mb:.0f} MB）解出來測試...")
    if listing['solid']:
        print("   ⚠️ 這是 solid 壓縮包：要抽出指定成員，7-Zip 得把前面的內容也解碼過一遍，"
              "大的包會比較慢，請耐心等候。")

    names = [name for _s, name in chosen]
    for batch in _chunks_by_chars(names):
        try:
            # `--` 之後一律當檔名：成員名稱若以 - 開頭才不會被誤認成參數。
            # stdin 接 DEVNULL：遇到加密檔時 7z 會等密碼輸入，不接上它整支程式就卡住。
            subprocess.run([exe, 'x', str(p), f'-o{workdir}', '-y', '--', *batch],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=3600)
        except Exception as e:
            print(f"   ⚠️ 解壓其中一批時出錯：{e}")

    out, missing = [], 0
    for name in names:
        disk = _member_disk_path(workdir, name)
        if disk.is_file():
            out.append((name, disk))
        else:
            missing += 1
    if missing:
        print(f"   ⚠️ 有 {missing} 張沒能解出來，已略過")
    return out


def collect_samples(input_path, count, how, seed, min_kb, workdir):
    """回傳 [(顯示名稱, 磁碟路徑), ...]。zip 只解出被挑中的成員，原檔不動。"""
    p = Path(input_path)
    if not p.exists():
        print(f"❌ 找不到輸入：{input_path}")
        return []
    min_bytes = max(0, min_kb) * 1024

    if p.is_file() and p.suffix.lower() == '.zip':
        try:
            with zipfile.ZipFile(p, 'r') as zf:
                members = [(it.file_size, it) for it in zf.infolist()
                           if not it.is_dir()
                           and it.filename.lower().endswith(JPEG_EXTENSIONS)
                           and it.file_size >= min_bytes]
                if not members:
                    print(f"❌ 壓縮包裡沒有 >= {min_kb} KB 的 JPG：{p.name}")
                    return []
                chosen = _pick(members, count, how, seed)
                out = []
                for idx, (_size, it) in enumerate(chosen):
                    dst = Path(workdir) / f"{idx:04d}_{Path(it.filename).name}"
                    with zf.open(it, 'r') as src, open(dst, 'wb') as fh:
                        shutil.copyfileobj(src, fh)
                    out.append((it.filename, dst))
                return out
        except Exception as e:
            print(f"❌ 讀取壓縮包失敗：{e}")
            return []

    if p.is_file() and p.suffix.lower() in SEVEN_ZIP_SUFFIXES:
        return _samples_from_7z(p, count, how, seed, min_kb, workdir)

    if p.is_file():
        if p.suffix.lower() not in JPEG_EXTENSIONS:
            print(f"❌ 不是 JPG：{p.name}")
            return []
        return [(p.name, p)]       # 指名要看的單一檔案，不套用大小門檻

    files = [(f.stat().st_size, f) for f in p.rglob('*')
             if f.is_file() and f.suffix.lower() in JPEG_EXTENSIONS
             and f.stat().st_size >= min_bytes]
    if not files:
        print(f"❌ 資料夾裡沒有 >= {min_kb} KB 的 JPG：{p}")
        return []
    return [(f.name, f) for _s, f in _pick(files, count, how, seed)]


# ================= 單張圖的實測（在子行程裡跑）=================
def _thumb_b64(im):
    t = im.copy()
    t.thumbnail((THUMB_EDGE, THUMB_EDGE))
    buf = io.BytesIO()
    t.save(buf, 'JPEG', quality=70)
    return base64.b64encode(buf.getvalue()).decode('ascii')


def pipeline_route(action):
    """與 process_jpegs_auto 相同的分流：含 recompress → B，其餘需修正者 → A（降尺寸目前不做）。"""
    if action == 'skip':
        return None
    return 'B' if 'recompress' in action else 'A'


def analyze_one(args):
    """對單張 JPG 跑完整測試，回傳可序列化的 dict（失敗時帶 error）。"""
    path_str, display, qualities, with_psnr = args
    row = {'name': display, 'error': None, 'results': {}}
    try:
        path = Path(path_str)
        raw = path.read_bytes()
        size = len(raw)
        report = inspect_jpeg(path) or {}
        verdict = classify_jpeg(report)
        route = pipeline_route(verdict['action'])

        with Image.open(path) as opened:
            im = resize.to_rgb(opened)
            im.load()
        w, h = im.size
        q_auto, ratio, kind = resize.pick_quality_for(im)

        row.update({
            'size': size, 'width': w, 'height': h,
            'est_quality': report.get('est_quality'),
            'subsampling': report.get('subsampling'),
            'progressive': bool(report.get('progressive')),
            'bpp': size * 8.0 / (w * h),
            'action': verdict['action'], 'route': route,
            'reasons': '；'.join(verdict['reasons']),
            'color_ratio': ratio, 'kind': kind, 'q_auto': q_auto,
            'thumb': _thumb_b64(im),
        })

        # A：mozjpeg 無損最佳化，並驗證像素真的完全相同
        lossless = resize._optimize_lossless(raw)
        row['lossless_size'] = len(lossless) if lossless else None
        row['lossless_identical'] = None
        if lossless:
            from PIL import ImageChops
            with Image.open(io.BytesIO(lossless)) as lo:
                row['lossless_identical'] = ImageChops.difference(
                    im, resize.to_rgb(lo)).getbbox() is None

        # B：各目標品質重壓（含流程實際會挑的那個品質）
        for q in sorted(set(qualities) | {q_auto}):
            out = resize._encode(im, q, JPEG_TARGET_SUBSAMPLING)
            entry = {'size': len(out), 'psnr': None}
            if with_psnr:
                with Image.open(io.BytesIO(out)) as v:
                    entry['psnr'] = psnr(im, v.convert('RGB'))
            row['results'][q] = entry

        row['pipeline_size'] = pipeline_size_after(row)
        return row
    except Exception as e:      # 壞檔不能拖垮整批
        row['error'] = f"{type(e).__name__}: {e}"
        return row


def pipeline_size_after(row):
    """正式流程對這張圖的結果：只有變小才替換，所以不會比原檔大。"""
    size = row['size']
    route = row.get('route')
    if route == 'B':
        new = row['results'].get(row['q_auto'], {}).get('size')
    elif route == 'A':
        new = row.get('lossless_size')
    else:
        new = None
    return new if (new is not None and new < size) else size


# ================= 彙總（純函式，方便測試）=================
def _median(values):
    return statistics.median(values) if values else None


def effective(row, q):
    """重壓到 q 之後實際會存的大小：變大就保留原檔。"""
    return min(row['size'], row['results'][q]['size'])


def summarize(rows, qualities):
    ok = [r for r in rows if not r.get('error')]
    summary = {'n': len(ok), 'errors': len(rows) - len(ok), 'by_q': {}, 'total': 0}
    if not ok:
        return summary

    summary['total'] = sum(r['size'] for r in ok)
    for q in qualities:
        have = [r for r in ok if q in r['results']]
        if not have:
            continue
        orig = sum(r['size'] for r in have)
        after = sum(effective(r, q) for r in have)
        savings = [1 - r['results'][q]['size'] / r['size'] for r in have]
        psnrs = [r['results'][q]['psnr'] for r in have if r['results'][q]['psnr'] is not None]
        summary['by_q'][q] = {
            'total_savings': 1 - after / orig,
            'median_savings': _median(savings),
            'min_savings': min(savings), 'max_savings': max(savings),
            'median_psnr': _median(psnrs),
            'min_psnr': min(psnrs) if psnrs else None,
            'not_smaller': sum(1 for s in savings if s <= 0),
        }

    after = sum(r['pipeline_size'] for r in ok)
    summary['pipeline_after'] = after
    summary['pipeline_savings'] = 1 - after / summary['total']
    summary['routes'] = {k: sum(1 for r in ok if r['route'] == k) for k in ('A', 'B')}
    summary['routes']['skip'] = sum(1 for r in ok if r['route'] is None)
    summary['kinds'] = {k: sum(1 for r in ok if r['kind'] == k) for k in ('flat', 'photo')}
    lossless = [r for r in ok if r.get('lossless_size')]
    if lossless:
        summary['lossless_savings'] = 1 - (sum(min(r['size'], r['lossless_size']) for r in lossless)
                                           / sum(r['size'] for r in lossless))
        summary['lossless_all_identical'] = all(r['lossless_identical'] for r in lossless)
    return summary


def whatif_flat_threshold(rows, thresholds, flat_q, photo_q):
    """如果「平塗/寫實」的顏色佔比門檻改成 t，整批會省多少。

    與正式流程一致：低於門檻走 flat_q，否則走 photo_q，且變大就保留原檔。
    只包含實際走重壓（B）的檔案——A 與跳過的檔案不受這個門檻影響。
    """
    ok = [r for r in rows if not r.get('error') and r['route'] == 'B'
          and flat_q in r['results'] and photo_q in r['results']]
    out = []
    if not ok:
        return out
    orig = sum(r['size'] for r in ok)
    for t in thresholds:
        flat = [r for r in ok if r['color_ratio'] < t]
        after = sum(effective(r, flat_q) if r['color_ratio'] < t else effective(r, photo_q)
                    for r in ok)
        out.append({'threshold': t, 'flat': len(flat), 'photo': len(ok) - len(flat),
                    'savings': 1 - after / orig})
    return out


# ================= SVG 圖表（零相依）=================
class Axes:
    """最小的座標軸：負責資料座標 → SVG 座標、刻度與格線。"""

    def __init__(self, xr, yr, w=640, h=340, ml=58, mr=18, mt=14, mb=46):
        self.x0, self.x1 = xr
        self.y0, self.y1 = yr
        self.w, self.h, self.ml, self.mr, self.mt, self.mb = w, h, ml, mr, mt, mb

    def px(self, x):
        span = (self.x1 - self.x0) or 1
        return self.ml + (x - self.x0) / span * (self.w - self.ml - self.mr)

    def py(self, y):
        span = (self.y1 - self.y0) or 1
        return self.h - self.mb - (y - self.y0) / span * (self.h - self.mt - self.mb)

    def clip(self, cid):
        """資料線的裁切區：超出座標框的部分不畫，免得線條跑到刻度與標題上。"""
        return (f'<defs><clipPath id="{cid}"><rect x="{self.ml}" y="{self.mt}" '
                f'width="{self.w - self.ml - self.mr}" height="{self.h - self.mt - self.mb}"/>'
                f'</clipPath></defs>')

    def frame(self, xticks, yticks, xfmt, yfmt, xlabel, ylabel):
        g = []
        for yv in yticks:
            y = self.py(yv)
            g.append(f'<line class="grid" x1="{self.ml}" x2="{self.w - self.mr}" y1="{y:.1f}" y2="{y:.1f}"/>')
            g.append(f'<text class="tick" x="{self.ml - 6}" y="{y + 4:.1f}" text-anchor="end">{yfmt(yv)}</text>')
        for xv in xticks:
            x = self.px(xv)
            g.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{self.mt}" y2="{self.h - self.mb}"/>')
            g.append(f'<text class="tick" x="{x:.1f}" y="{self.h - self.mb + 16}" text-anchor="middle">{xfmt(xv)}</text>')
        g.append(f'<rect class="plot" x="{self.ml}" y="{self.mt}" '
                 f'width="{self.w - self.ml - self.mr}" height="{self.h - self.mt - self.mb}"/>')
        g.append(f'<text class="axis" x="{(self.ml + self.w - self.mr) / 2:.0f}" y="{self.h - 6}" '
                 f'text-anchor="middle">{html.escape(xlabel)}</text>')
        g.append(f'<text class="axis" transform="rotate(-90)" x="{-(self.mt + self.h - self.mb) / 2:.0f}" '
                 f'y="14" text-anchor="middle">{html.escape(ylabel)}</text>')
        return ''.join(g)


def _nice_ticks(lo, hi, target=6):
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    start = math.ceil(lo / step) * step
    ticks, v = [], start
    while v <= hi + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _svg(inner, aria, w=640, h=340):
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(aria)}" '
            f'xmlns="http://www.w3.org/2000/svg">{inner}</svg>')


def _poly(ax, pts, cls):
    path = ' '.join(f'{ax.px(x):.1f},{ax.py(y):.1f}' for x, y in pts)
    return f'<polyline class="{cls}" points="{path}"/>'


def _text(x, y, s, cls='note', anchor='start'):
    return f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}">{html.escape(s)}</text>'


def chart_savings_vs_quality(rows, summary, qualities):
    """圖1：重壓到各品質，能省多少。粗線是整批合計，細線是每張圖。"""
    ok = [r for r in rows if not r.get('error')]
    qs = sorted(summary['by_q'])
    if not qs:
        return ''
    # 有些檔案重壓後反而變大（縮減為負），座標範圍要包含它們，否則線會畫到框外
    lowest = min(1 - r['results'][q]['size'] / r['size'] for r in ok for q in qs if q in r['results'])
    ymin = min(-0.05, math.floor(lowest * 10) / 10)
    ax = Axes((min(qs) - 2, max(qs) + 2), (ymin, 1.0))
    inner = [ax.clip('clip1'),
             ax.frame(_nice_ticks(min(qs), max(qs), 6), _nice_ticks(ymin, 1.0, 6),
                      lambda v: f'Q{v:.0f}', lambda v: f'{v * 100:.0f}%',
                      '重壓的目標品質', '縮減比例（越高越省，負數 = 變大）')]
    ref = ax.py(PNG_TYPICAL_SAVING)
    inner.append(f'<line class="ref" x1="{ax.ml}" x2="{ax.w - ax.mr}" y1="{ref:.1f}" y2="{ref:.1f}"/>')
    inner.append(_text(ax.ml + 6, ref - 5, f'PNG 轉 JPG 常見約 {PNG_TYPICAL_SAVING * 100:.0f}%'))
    for q, cls, label in ((JPEG_TARGET_QUALITY, 'mark-photo', '寫實目標'),
                          (JPEG_FLAT_QUALITY, 'mark-flat', '平塗目標')):
        if ax.x0 <= q <= ax.x1:
            x = ax.px(q)
            inner.append(f'<line class="{cls}" x1="{x:.1f}" x2="{x:.1f}" y1="{ax.mt}" y2="{ax.h - ax.mb}"/>')
            inner.append(_text(x + 4, ax.mt + 12, f'{label} Q{q}'))
    sample = ok if len(ok) <= MAX_FAINT_LINES else random.Random(1).sample(ok, MAX_FAINT_LINES)
    inner.append('<g clip-path="url(#clip1)">')
    for r in sample:
        pts = [(q, 1 - r['results'][q]['size'] / r['size']) for q in qs if q in r['results']]
        inner.append(_poly(ax, pts, 'faint'))
    inner.append(_poly(ax, [(q, summary['by_q'][q]['median_savings']) for q in qs], 'median'))
    inner.append(_poly(ax, [(q, summary['by_q'][q]['total_savings']) for q in qs], 'total'))
    inner.append('</g>')
    for q in qs:
        inner.append(f'<circle class="dot-total" cx="{ax.px(q):.1f}" cy="{ax.py(summary["by_q"][q]["total_savings"]):.1f}" r="3.5">'
                     f'<title>Q{q}：整批合計省 {summary["by_q"][q]["total_savings"] * 100:.1f}%</title></circle>')
    lx, ly = ax.w - ax.mr - 150, ax.h - ax.mb - 62
    inner.append(f'<rect class="legend-bg" x="{lx - 8}" y="{ly}" width="154" height="58" rx="4"/>')
    for i, (cls, label) in enumerate((('total', '整批合計'), ('median', '單張中位數'), ('faint', '每張圖'))):
        y = ly + 14 + i * 16
        inner.append(f'<line class="{cls}" x1="{lx}" x2="{lx + 22}" y1="{y}" y2="{y}"/>')
        inner.append(_text(lx + 28, y + 4, label))
    return _svg(''.join(inner), '各目標品質的縮減比例')


def chart_tradeoff(rows, summary, qualities):
    """圖2：代價。橫軸省多少、縱軸畫質（PSNR）。曲線越快掉下去，代表再省就開始傷畫質。"""
    ok = [r for r in rows if not r.get('error')]
    qs = [q for q in sorted(summary['by_q']) if summary['by_q'][q]['median_psnr'] is not None]
    if not qs:
        return ''
    allp = [r['results'][q]['psnr'] for r in ok for q in qs if q in r['results']
            and r['results'][q]['psnr'] is not None]
    lo = math.floor(min(allp) / 2) * 2 - 2
    hi = math.ceil(max(allp) / 2) * 2 + 2
    lowest = min(1 - r['results'][q]['size'] / r['size'] for r in ok for q in qs if q in r['results'])
    xmin = min(0, math.floor(lowest * 10) / 10)       # 變大的檔案縮減為負，範圍要包含
    ax = Axes((xmin, 1.0), (lo, hi))
    inner = [ax.clip('clip2'),
             ax.frame(_nice_ticks(xmin, 1.0, 6), _nice_ticks(lo, hi, 6),
                      lambda v: f'{v * 100:.0f}%', lambda v: f'{v:.0f}',
                      '縮減比例（越右越省，負數 = 變大）', 'PSNR dB（越高畫質越接近來源）')]
    sample = ok if len(ok) <= MAX_FAINT_LINES else random.Random(1).sample(ok, MAX_FAINT_LINES)
    inner.append('<g clip-path="url(#clip2)">')
    for r in sample:
        pts = [(1 - r['results'][q]['size'] / r['size'], r['results'][q]['psnr'])
               for q in qs if q in r['results'] and r['results'][q]['psnr'] is not None]
        inner.append(_poly(ax, pts, 'faint'))
    med = [(summary['by_q'][q]['median_savings'], summary['by_q'][q]['median_psnr']) for q in qs]
    inner.append(_poly(ax, med, 'median'))
    inner.append('</g>')
    for i, (q, (x, y)) in enumerate(zip(qs, med)):
        cls = ('dot-photo' if q == JPEG_TARGET_QUALITY
               else 'dot-flat' if q == JPEG_FLAT_QUALITY else 'dot-total')
        inner.append(f'<circle class="{cls}" cx="{ax.px(x):.1f}" cy="{ax.py(y):.1f}" r="4">'
                     f'<title>Q{q}：中位數省 {x * 100:.1f}%，PSNR {y:.1f} dB</title></circle>')
        dy = -8 if i % 2 == 0 else 17        # 上下錯開，品質接近時標籤才不會疊在一起
        inner.append(_text(ax.px(x), ax.py(y) + dy, f'Q{q}', anchor='middle'))
    inner.append(_text(ax.ml + 6, ax.mt + 14, '點 = 各品質的單張中位數；兩點之間越陡，多省這一段要付出的畫質越多'))
    return _svg(''.join(inner), '縮減比例與畫質的取捨')


def chart_source_quality(rows):
    """圖3：來源品質 vs 正式流程實際省多少。點越大代表檔案越大。"""
    ok = [r for r in rows if not r.get('error') and r.get('est_quality') is not None]
    if not ok:
        return ''
    qlo = max(60, min(r['est_quality'] for r in ok) - 3)
    qhi = min(101, max(r['est_quality'] for r in ok) + 3)
    ax = Axes((qlo, qhi), (-0.05, 1.0))
    inner = [ax.frame(_nice_ticks(qlo, qhi, 6), [0, .2, .4, .6, .8, 1.0],
                      lambda v: f'Q{v:.0f}', lambda v: f'{v * 100:.0f}%',
                      '來源 JPG 的估算品質', '流程實際縮減（0% = 沒動或沒變小）')]
    biggest = max(r['size'] for r in ok)
    for i, r in enumerate(ok):
        jitter = ((i * 37) % 11 - 5) * 0.12
        x = ax.px(r['est_quality'] + jitter)
        y = ax.py(1 - r['pipeline_size'] / r['size'])
        rad = 3 + 6 * math.sqrt(r['size'] / biggest)
        cls = {'A': 'pt-a', 'B': 'pt-b', None: 'pt-skip'}[r['route']]
        inner.append(
            f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}"><title>'
            f'{html.escape(clean_str(r["name"]))}\nQ{r["est_quality"]} {r["subsampling"]} '
            f'{"progressive" if r["progressive"] else "baseline"}\n'
            f'走 {r["route"] or "跳過"}，省 {(1 - r["pipeline_size"] / r["size"]) * 100:.1f}%</title></circle>')
    lx, ly = ax.ml + 10, ax.mt + 14
    for i, (cls, label) in enumerate((('pt-b', 'B 重壓'), ('pt-a', 'A 無損'), ('pt-skip', '跳過'))):
        inner.append(f'<circle class="{cls}" cx="{lx + i * 80}" cy="{ly - 4}" r="5"/>')
        inner.append(_text(lx + i * 80 + 9, ly, label))
    return _svg(''.join(inner), '來源品質與流程實際縮減')


def chart_color_ratio(rows):
    """圖4：顏色數佔比的分布，與平塗/寫實門檻的相對位置。"""
    ok = [r for r in rows if not r.get('error')]
    if not ok:
        return ''
    top = max(0.8, math.ceil(max(r['color_ratio'] for r in ok) * 20) / 20)
    bins = 32
    width = top / bins
    counts = [0] * bins
    for r in ok:
        counts[min(bins - 1, int(r['color_ratio'] / width))] += 1
    ymax = max(counts) + 1
    ax = Axes((0, top), (0, ymax))
    inner = [ax.frame(_nice_ticks(0, top, 8), _nice_ticks(0, ymax, 5),
                      lambda v: f'{v * 100:.0f}%', lambda v: f'{v:.0f}',
                      '顏色數佔比（相異顏色數 / 取樣像素數）', '圖片張數')]
    for i, c in enumerate(counts):
        if not c:
            continue
        x0, x1 = ax.px(i * width), ax.px((i + 1) * width)
        cls = 'bar-flat' if (i + 0.5) * width < JPEG_FLAT_COLOR_RATIO else 'bar-photo'
        inner.append(f'<rect class="{cls}" x="{x0 + 1:.1f}" y="{ax.py(c):.1f}" width="{x1 - x0 - 2:.1f}" '
                     f'height="{ax.py(0) - ax.py(c):.1f}"><title>{i * width * 100:.1f}%～'
                     f'{(i + 1) * width * 100:.1f}%：{c} 張</title></rect>')
    x = ax.px(JPEG_FLAT_COLOR_RATIO)
    inner.append(f'<line class="mark-thr" x1="{x:.1f}" x2="{x:.1f}" y1="{ax.mt}" y2="{ax.h - ax.mb}"/>')
    flat = sum(1 for r in ok if r['color_ratio'] < JPEG_FLAT_COLOR_RATIO)
    inner.append(_text(x - 6, ax.mt + 14, f'← 平塗 Q{JPEG_FLAT_QUALITY}：{flat} 張', anchor='end'))
    inner.append(_text(x + 6, ax.mt + 14,
                       f'門檻 {JPEG_FLAT_COLOR_RATIO * 100:.0f}% → 寫實 Q{JPEG_TARGET_QUALITY}：{len(ok) - flat} 張'))
    return _svg(''.join(inner), '顏色數佔比分布')


def chart_bpp(rows, q):
    """圖5：來源每像素用掉多少位元 vs 重壓到 q 能省多少。已經很精簡的圖沒什麼好擠。"""
    ok = [r for r in rows if not r.get('error') and q in r['results']]
    if not ok:
        return ''
    xhi = math.ceil(max(r['bpp'] for r in ok) + 0.5)
    ax = Axes((0, xhi), (-0.05, 1.0))
    inner = [ax.frame(_nice_ticks(0, xhi, 7), [0, .2, .4, .6, .8, 1.0],
                      lambda v: f'{v:g}', lambda v: f'{v * 100:.0f}%',
                      '來源檔案的 bits / pixel（越大代表來源越「肥」）', f'重壓到 Q{q} 的縮減')]
    for r in ok:
        cls = 'pt-flat' if r['kind'] == 'flat' else 'pt-b'
        s = 1 - r['results'][q]['size'] / r['size']
        inner.append(f'<circle class="{cls}" cx="{ax.px(r["bpp"]):.1f}" cy="{ax.py(s):.1f}" r="4.5"><title>'
                     f'{html.escape(clean_str(r["name"]))}\n{r["bpp"]:.2f} bpp → 省 {s * 100:.1f}%</title></circle>')
    inner.append(f'<circle class="pt-b" cx="{ax.ml + 12}" cy="{ax.mt + 12}" r="5"/>')
    inner.append(_text(ax.ml + 21, ax.mt + 16, '判為寫實'))
    inner.append(f'<circle class="pt-flat" cx="{ax.ml + 100}" cy="{ax.mt + 12}" r="5"/>')
    inner.append(_text(ax.ml + 109, ax.mt + 16, '判為平塗'))
    return _svg(''.join(inner), '來源位元率與縮減比例')


# ================= 報告 =================
CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--grid:#d1d9e0;--panel:#f6f8fa;--line:#d1d9e0;
--photo:#cf222e;--flat:#1a7f37;--a:#0969da;--skip:#8c959f;--accent:#8250df}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--grid:#30363d;
--panel:#161b22;--line:#30363d;--photo:#ff7b72;--flat:#56d364;--a:#58a6ff;--skip:#6e7681;--accent:#bc8cff}}
*{box-sizing:border-box}body{margin:0;padding:20px 16px 48px;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft JhengHei",sans-serif}
main{max-width:1100px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 4px}
.sub{color:var(--muted);margin:0 0 18px}.cap{color:var(--muted);margin:0 0 8px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:20px}.card span{color:var(--muted);font-size:12px}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px}
svg{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.plot{fill:none;stroke:var(--grid)}
.tick{fill:var(--muted);font-size:11px}.axis{fill:var(--muted);font-size:12px}.note{fill:var(--fg);font-size:11px}
.faint{fill:none;stroke:var(--muted);stroke-width:1;opacity:.35}
.median{fill:none;stroke:var(--accent);stroke-width:2.2}.total{fill:none;stroke:var(--fg);stroke-width:2.8}
.ref{stroke:var(--skip);stroke-width:1.2;stroke-dasharray:5 4}
.legend-bg{fill:var(--panel);stroke:var(--line);opacity:.92}
.mark-photo{stroke:var(--photo);stroke-dasharray:4 3;opacity:.8}.mark-flat{stroke:var(--flat);stroke-dasharray:4 3;opacity:.8}
.mark-thr{stroke:var(--fg);stroke-width:1.6;stroke-dasharray:6 4}
.dot-total{fill:var(--fg)}.dot-photo{fill:var(--photo)}.dot-flat{fill:var(--flat)}
.pt-a{fill:var(--a);opacity:.7}.pt-b{fill:var(--photo);opacity:.65}.pt-skip{fill:var(--skip);opacity:.7}.pt-flat{fill:var(--flat);opacity:.7}
.bar-flat{fill:var(--flat);opacity:.85}.bar-photo{fill:var(--photo);opacity:.8}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--panel)}td:first-child,th:first-child{text-align:left;white-space:normal;max-width:340px;word-break:break-all}
.scroll{overflow-x:auto;max-height:520px;border:1px solid var(--line);border-radius:8px}
.hl td{background:color-mix(in srgb,var(--accent) 14%,transparent);font-weight:600}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.thumb{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px;font-size:11.5px;overflow:hidden}
.thumb img{width:100%;height:120px;object-fit:contain;background:#0000000d;border-radius:4px}
.thumb.flat{border-color:var(--flat)}.thumb.photo{border-color:var(--photo)}
.thumb .n{display:block;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag-flat{color:var(--flat);font-weight:600}.tag-photo{color:var(--photo);font-weight:600}
"""


def _pct(v, digits=1):
    return '—' if v is None else f'{v * 100:.{digits}f}%'


def _mb(n):
    return f'{n / 1024 / 1024:.2f} MB' if n >= 1024 * 1024 else f'{n / 1024:.0f} KB'


def render_html(rows, summary, qualities, meta):
    ok = [r for r in rows if not r.get('error')]
    bq = summary['by_q']
    target_q = JPEG_TARGET_QUALITY if JPEG_TARGET_QUALITY in bq else None
    parts = [f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>JPG 重壓效率研究</title><style>{CSS}</style></head><body><main>']
    parts.append('<h1>JPG 重壓效率研究</h1>')
    parts.append(f'<p class="sub">輸入：{html.escape(clean_str(meta["input"]))} ｜ 樣本 {summary["n"]} 張'
                 f'（{meta["pick"]}，總計 {_mb(summary["total"])}）｜ 色度抽樣 {JPEG_TARGET_SUBSAMPLING}（4:2:0）'
                 f'｜ 全部沿用 resize.py 的編碼流程</p>')

    cards = [(_pct(summary['pipeline_savings']), '目前設定下，流程實際會省'),
             (f"A {summary['routes']['A']} / B {summary['routes']['B']} / 跳過 {summary['routes']['skip']}",
              '流程分流（無損 / 重壓 / 不動）'),
             (f"平塗 {summary['kinds']['flat']} / 寫實 {summary['kinds']['photo']}",
              f'內容判定（門檻 {JPEG_FLAT_COLOR_RATIO * 100:.0f}%）')]
    if 'lossless_savings' in summary:
        same = '像素逐一比對相同 ✔' if summary['lossless_all_identical'] else '⚠ 有像素不同！'
        cards.append((_pct(summary['lossless_savings']), f'只做無損最佳化能省（{same}）'))
    if summary['errors']:
        cards.append((str(summary['errors']), '讀取失敗（見 CSV）'))
    parts.append('<div class="cards">' + ''.join(
        f'<div class="card"><b>{html.escape(v)}</b><span>{html.escape(k)}</span></div>' for v, k in cards) + '</div>')

    gate = MIN_ARCHIVE_SAVING_RATIO
    if gate:
        verdict = '會' if summary['pipeline_savings'] < gate else '不會'
        parts.append(f'<p class="cap">參考：整包驗收門檻 {gate * 100:.0f}%——若這批樣本就是一整包，'
                     f'目前設定省 {_pct(summary["pipeline_savings"])}，<b>{verdict}</b>被判定效率不足。</p>')

    parts.append('<h2>① 重壓到不同品質，能省多少？</h2>')
    parts.append('<p class="cap">粗黑線是整批合計，紫線是單張中位數，灰細線是每張圖。虛線是你觀察到的 PNG 轉 JPG 常見縮減——'
                 '先看看 JPG 重壓這條線離它有多遠。</p>')
    parts.append(f'<div class="chart">{chart_savings_vs_quality(rows, summary, qualities)}</div>')

    parts.append('<h2>② 再省下去，要付出多少畫質？</h2>')
    parts.append('<p class="cap">橫軸越右越省、縱軸越高越接近來源。曲線平緩的地方是划算的，開始陡降的地方就是拐點。</p>')
    parts.append(f'<div class="chart">{chart_tradeoff(rows, summary, qualities)}</div>')

    parts.append('<h2>③ 來源品質決定了能擠多少</h2>')
    parts.append('<p class="cap">每個點是一張圖。落在 0% 的是流程沒動或重壓後沒變小而保留原檔的。</p>')
    parts.append(f'<div class="chart">{chart_source_quality(rows)}</div>')

    parts.append('<h2>④ 平塗／寫實分流有沒有作用？</h2>')
    parts.append(f'<p class="cap">門檻左邊的圖走平塗 Q{JPEG_FLAT_QUALITY}，右邊走寫實 Q{JPEG_TARGET_QUALITY}。'
                 '如果幾乎全部堆在門檻右邊，分流等於沒作用——下面的縮圖可以對照看看判定合不合理。</p>')
    parts.append(f'<div class="chart">{chart_color_ratio(rows)}</div>')

    if target_q:
        parts.append(f'<h2>⑤ 來源位元率 vs 能省多少（重壓到 Q{target_q}）</h2>')
        parts.append('<p class="cap">橫軸是來源每個像素用掉幾個位元。已經很精簡（左邊）的圖沒什麼好擠；很肥（右邊）的才有空間。</p>')
        parts.append(f'<div class="chart">{chart_bpp(rows, target_q)}</div>')

    parts.append('<h2>各品質的整批數字</h2>')
    parts.append('<div class="scroll"><table><tr><th>目標品質</th><th>整批縮減</th><th>單張中位數</th>'
                 '<th>範圍</th><th>PSNR 中位數</th><th>PSNR 最低</th><th>沒變小的張數</th></tr>')
    for q in sorted(bq):
        b = bq[q]
        cls = ' class="hl"' if q == JPEG_TARGET_QUALITY else ''
        psnr_med = '—' if b['median_psnr'] is None else f"{b['median_psnr']:.1f} dB"
        psnr_min = '—' if b['min_psnr'] is None else f"{b['min_psnr']:.1f} dB"
        parts.append(f'<tr{cls}><td>Q{q}{" ← 寫實目標" if q == JPEG_TARGET_QUALITY else ""}'
                     f'{" ← 平塗目標" if q == JPEG_FLAT_QUALITY else ""}</td>'
                     f'<td>{_pct(b["total_savings"])}</td><td>{_pct(b["median_savings"])}</td>'
                     f'<td>{_pct(b["min_savings"], 0)} ~ {_pct(b["max_savings"], 0)}</td>'
                     f'<td>{psnr_med}</td><td>{psnr_min}</td><td>{b["not_smaller"]}</td></tr>')
    parts.append('</table></div>')

    flat_q, photo_q = JPEG_FLAT_QUALITY, JPEG_TARGET_QUALITY
    thresholds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60]
    whatif = whatif_flat_threshold(rows, thresholds, flat_q, photo_q)
    if whatif:
        parts.append(f'<h2>如果把平塗判定門檻（JPEG_FLAT_COLOR_RATIO）調高？</h2>')
        parts.append(f'<p class="cap">只算實際走重壓的 {sum(1 for r in ok if r["route"] == "B")} 張；低於門檻走 Q{flat_q}，其餘走 Q{photo_q}。'
                     '門檻拉高會讓更多圖走較低品質——省得更多，但請對照上面的縮圖確認畫質看不看得出來。</p>')
        parts.append('<div class="scroll"><table><tr><th>門檻</th><th>判為平塗</th><th>判為寫實</th><th>整批縮減</th></tr>')
        for w in whatif:
            cls = ' class="hl"' if abs(w['threshold'] - JPEG_FLAT_COLOR_RATIO) < 1e-9 else ''
            parts.append(f'<tr{cls}><td>{w["threshold"] * 100:.0f}%{" ← 目前" if cls else ""}</td>'
                         f'<td>{w["flat"]}</td><td>{w["photo"]}</td><td>{_pct(w["savings"])}</td></tr>')
        parts.append('</table></div>')

    gallery = sorted(ok, key=lambda r: r['color_ratio'])[:60]
    if gallery:
        parts.append('<h2>縮圖對照：顏色佔比由低到高</h2>')
        parts.append('<p class="cap">綠框＝判為平塗，紅框＝判為寫實。看看判成「寫實」的圖，你自己會不會也這樣認為？</p><div class="gallery">')
        for r in gallery:
            kind = 'flat' if r['kind'] == 'flat' else 'photo'
            sav = (1 - r['results'][target_q]['size'] / r['size']) if target_q and target_q in r['results'] else None
            parts.append(
                f'<div class="thumb {kind}"><img alt="" src="data:image/jpeg;base64,{r["thumb"]}">'
                f'<span class="n" title="{html.escape(clean_str(r["name"]))}">{html.escape(clean_str(r["name"]))}</span>'
                f'<span class="tag-{kind}">{"平塗" if kind == "flat" else "寫實"}</span> {r["color_ratio"] * 100:.1f}%'
                f'{"｜Q" + str(target_q) + " 省 " + _pct(sav, 0) if sav is not None else ""}</div>')
        parts.append('</div>')

    shown = sorted(ok, key=lambda r: r['size'], reverse=True)[:TABLE_ROWS]
    parts.append(f'<h2>逐張數字（最大的 {len(shown)} 張，完整資料在 CSV）</h2>')
    heads = ['檔名', '大小', '尺寸', '來源 Q', '抽樣', '編碼', 'bpp', '顏色佔比', '流程', '流程省']
    qshow = [q for q in (80, 85, 90, 92, 95) if q in bq]
    heads += [f'Q{q} 省' for q in qshow]
    parts.append('<div class="scroll"><table><tr>' + ''.join(f'<th>{h}</th>' for h in heads) + '</tr>')
    for r in shown:
        cells = [html.escape(clean_str(r['name'])), _mb(r['size']), f"{r['width']}×{r['height']}",
                 f"Q{r['est_quality']}" if r['est_quality'] is not None else '?', r['subsampling'] or '?',
                 'prog' if r['progressive'] else 'base', f"{r['bpp']:.2f}", f"{r['color_ratio'] * 100:.1f}% {r['kind']}",
                 r['route'] or '跳過', _pct(1 - r['pipeline_size'] / r['size'])]
        cells += [_pct(1 - r['results'][q]['size'] / r['size']) if q in r['results'] else '—' for q in qshow]
        parts.append('<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>')
    parts.append('</table></div>')
    parts.append('</main></body></html>')
    return ''.join(parts)


def write_csv(path, rows, qualities):
    qs = sorted({q for r in rows for q in r.get('results', {})} | set(qualities))
    head = ['name', 'size_bytes', 'width', 'height', 'est_quality', 'subsampling', 'progressive', 'bpp',
            'action', 'route', 'reasons', 'color_ratio', 'kind', 'auto_quality',
            'lossless_bytes', 'lossless_identical', 'pipeline_bytes', 'pipeline_savings', 'error']
    for q in qs:
        head += [f'q{q}_bytes', f'q{q}_savings', f'q{q}_psnr']
    with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for r in rows:
            if r.get('error'):
                # 錯誤訊息要放在 error 欄底下。error 欄後面還接著各品質的欄位，
                # 所以不能把它塞在整列的最後一格。
                line = [''] * len(head)
                line[0] = clean_str(r['name'])
                line[head.index('error')] = r['error']
                w.writerow(line)
                continue
            line = [clean_str(r['name']), r['size'], r['width'], r['height'], r['est_quality'],
                    r['subsampling'], int(r['progressive']), f"{r['bpp']:.4f}", r['action'], r['route'] or 'skip',
                    r['reasons'], f"{r['color_ratio']:.4f}", r['kind'], r['q_auto'],
                    r['lossless_size'] if r['lossless_size'] else '', '' if r['lossless_identical'] is None
                    else int(r['lossless_identical']), r['pipeline_size'],
                    f"{1 - r['pipeline_size'] / r['size']:.4f}", '']
            for q in qs:
                e = r['results'].get(q)
                line += ([e['size'], f"{1 - e['size'] / r['size']:.4f}",
                          '' if e['psnr'] is None else f"{e['psnr']:.2f}"] if e else ['', '', ''])
            w.writerow(line)


# ================= 終端機摘要 =================
def print_summary(rows, summary, qualities, html_path, csv_path):
    ok = [r for r in rows if not r.get('error')]
    print('\n' + '=' * 70)
    print(f"📊 樣本 {summary['n']} 張（總計 {_mb(summary['total'])}）"
          + (f"，{summary['errors']} 張讀取失敗" if summary['errors'] else ''))
    print('=' * 70)

    if len(ok) <= 5:
        for r in ok:
            print(f"\n■ {clean_str(r['name'])}")
            print(f"   {r['width']}x{r['height']}  {_mb(r['size'])}  Q{r['est_quality']}  {r['subsampling']}  "
                  f"{'progressive' if r['progressive'] else 'baseline'}  {r['bpp']:.2f} bpp")
            print(f"   內容判定: 顏色佔比 {r['color_ratio'] * 100:.2f}%（門檻 {JPEG_FLAT_COLOR_RATIO * 100:.0f}%）"
                  f" → {'平塗' if r['kind'] == 'flat' else '寫實'}，自動選 Q{r['q_auto']}")
            print(f"   流程判斷: {r['action']} → 走 {r['route'] or '跳過'}"
                  + (f"（{r['reasons']}）" if r['reasons'] else ''))
            if r['lossless_size']:
                tag = '像素相同 ✔' if r['lossless_identical'] else '⚠ 像素不同'
                print(f"   A 無損:    {_mb(r['lossless_size']):>10}  省 {_pct(1 - r['lossless_size'] / r['size']):>6}  （{tag}）")
            print(f"   {'目標品質':<10}{'大小':>11}{'縮減':>9}{'PSNR':>11}")
            for q in sorted(r['results']):
                e = r['results'][q]
                mark = '  ← 流程實際用這個' if q == r['q_auto'] and r['route'] == 'B' else ''
                ps = '—' if e['psnr'] is None else f"{e['psnr']:.2f} dB"
                print(f"   Q{q:<9}{_mb(e['size']):>11}{_pct(1 - e['size'] / r['size']):>9}{ps:>11}{mark}")
            print(f"   ➜ 流程實際結果: {_mb(r['size'])} → {_mb(r['pipeline_size'])}  "
                  f"省 {_pct(1 - r['pipeline_size'] / r['size'])}")
    else:
        print(f"{'目標品質':<10}{'整批縮減':>10}{'中位數':>9}{'PSNR中位':>11}{'PSNR最低':>11}{'沒變小':>8}")
        for q in sorted(summary['by_q']):
            b = summary['by_q'][q]
            ps = '—' if b['median_psnr'] is None else f"{b['median_psnr']:.1f} dB"
            pm = '—' if b['min_psnr'] is None else f"{b['min_psnr']:.1f} dB"
            mark = '  ← 寫實目標' if q == JPEG_TARGET_QUALITY else ('  ← 平塗目標' if q == JPEG_FLAT_QUALITY else '')
            print(f"Q{q:<9}{_pct(b['total_savings']):>10}{_pct(b['median_savings']):>9}{ps:>11}{pm:>11}{b['not_smaller']:>8}{mark}")
        qdist = {}
        for r in ok:
            key = (r['est_quality'], r['subsampling'])
            qdist[key] = qdist.get(key, 0) + 1
        top = sorted(qdist.items(), key=lambda kv: -kv[1])[:5]
        print('\n來源品質分布: ' + '、'.join(f"Q{k[0]} {k[1]}×{n}" for k, n in top))
        print(f"內容判定: 平塗 {summary['kinds']['flat']} / 寫實 {summary['kinds']['photo']}"
              f"（顏色佔比中位數 {_median([r['color_ratio'] for r in ok]) * 100:.1f}%，門檻 {JPEG_FLAT_COLOR_RATIO * 100:.0f}%）")
        print(f"流程分流: A 無損 {summary['routes']['A']} / B 重壓 {summary['routes']['B']} / 跳過 {summary['routes']['skip']}")
        if 'lossless_savings' in summary:
            tag = '像素逐一比對相同 ✔' if summary['lossless_all_identical'] else '⚠ 有像素不同'
            print(f"只做無損最佳化: 省 {_pct(summary['lossless_savings'])}（{tag}）")
        print(f"\n➜ 流程實際結果: {_mb(summary['total'])} → {_mb(summary['pipeline_after'])}  "
              f"省 {_pct(summary['pipeline_savings'])}")

    print(f"\n📄 圖表報告: {html_path}\n📑 原始數字: {csv_path}")


# ================= 主程式 =================
def main(argv=None):
    parser = argparse.ArgumentParser(
        description='JPG 重壓效率研究：實測各品質能省多少、畫質代價、分流是否有作用（輸入唯讀）')
    parser.add_argument('input', help='單一 .jpg / 資料夾 / .zip / .rar / .7z')
    parser.add_argument('--samples', type=int, default=30, help='抽測幾張（預設 30）')
    parser.add_argument('--pick', choices=('random', 'largest'), default='random',
                        help='怎麼挑：random 隨機（預設，較有代表性）/ largest 挑最大的')
    parser.add_argument('--seed', type=int, default=1, help='隨機種子，固定它才能重現同一批樣本')
    parser.add_argument('--min-kb', type=int, default=JPEG_LARGE_KB,
                        help=f'只取不小於這個大小的 JPG（預設 {JPEG_LARGE_KB}，與 analysis 判斷「過大」相同；0 = 不限）')
    parser.add_argument('--qualities', default=','.join(map(str, DEFAULT_QUALITIES)),
                        help='要掃描的目標品質，逗號分隔')
    parser.add_argument('--no-psnr', action='store_true', help='不算 PSNR（快很多，但沒有畫質代價那張圖）')
    parser.add_argument('--workers', type=int, default=min(MAX_WORKERS, 6), help='平行行程數')
    parser.add_argument('--out', default=str(BASE_DIR / 'jpeg_efficiency_out'), help='輸出資料夾')
    parser.add_argument('--open', action='store_true', help='跑完自動用瀏覽器開啟報告')
    args = parser.parse_args(argv)

    try:
        qualities = sorted({int(x) for x in args.qualities.split(',') if x.strip()} |
                           {JPEG_FLAT_QUALITY, JPEG_TARGET_QUALITY})
    except ValueError:
        print('❌ --qualities 格式錯誤，範例：--qualities 70,80,90,95')
        return 1
    if not all(1 <= q <= 100 for q in qualities):
        print('❌ 品質必須在 1~100 之間')
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print('=' * 70)
    print('🔬 JPG 重壓效率研究（輸入檔案唯讀，不會被修改）')
    print(f'   掃描品質: {", ".join("Q" + str(q) for q in qualities)}')
    print(f'   編碼方式: 沿用 resize.py（色度抽樣 {JPEG_TARGET_SUBSAMPLING}、progressive、mozjpeg 無損擠壓）')
    print('=' * 70)

    with tempfile.TemporaryDirectory(dir=_scratch_dir()) as tmp:
        samples = collect_samples(args.input, args.samples, args.pick, args.seed, args.min_kb, tmp)
        if not samples:
            return 1
        print(f'\n抽測 {len(samples)} 張（{args.pick}），用 {args.workers} 個行程平行處理...\n')

        jobs = [(str(path), name, qualities, not args.no_psnr) for name, path in samples]
        rows = []
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(analyze_one, j) for j in jobs]
            for i, fut in enumerate(as_completed(futures), start=1):
                r = fut.result()
                rows.append(r)
                state = f"❌ {r['error']}" if r['error'] else f"Q{r['est_quality']} {_mb(r['size'])}"
                print(f"  [{i}/{len(jobs)}] {clean_str(r['name'])}  {state}")

    order = {name: i for i, (name, _p) in enumerate(samples)}
    rows.sort(key=lambda r: order.get(r['name'], 0))
    summary = summarize(rows, qualities)
    if not summary['n']:
        print('❌ 沒有任何一張讀取成功')
        return 1

    meta = {'input': args.input, 'pick': f'{"隨機" if args.pick == "random" else "最大"}抽樣'
            if len(samples) > 1 else '單一檔案'}
    html_path = out_dir / 'jpeg_efficiency.html'
    csv_path = out_dir / 'jpeg_efficiency.csv'
    html_path.write_text(render_html(rows, summary, qualities, meta), encoding='utf-8')
    write_csv(csv_path, rows, qualities)
    print_summary(rows, summary, qualities, html_path, csv_path)

    if args.open:
        try:
            os.startfile(str(html_path)) if hasattr(os, 'startfile') else webbrowser.open(html_path.as_uri())
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
