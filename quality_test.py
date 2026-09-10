"""畫質驗證工具（獨立執行，不會修改任何輸入檔案）

用你自己收藏裡的真實檔案，實測不同 JPEG_TARGET_QUALITY 的畫質與體積影響，
再決定 config 要設多少。平塗的賽璐璐風和高頻紋理的寫實風反應差很多，
所以請各挑一包來跑、分開判斷喵！

用法：
    python quality_test.py <輸入> [--qualities 80,85,88,92] [--samples 3] [--out 資料夾]

<輸入> 可以是：.zip 壓縮包 / 資料夾 / 單一 .jpg

產出（預設在 quality_test_out/）：
    summary.txt              數據總表
    <名稱>_q80.jpg 等        各品質的完整重壓圖，可直接丟進 NeeView 全螢幕比對
    <名稱>_crop.png          最劣區域的 100% 原尺寸橫向對照（刻意看最糟的地方）
    <名稱>_diff_q80.png 等   差異放大圖，顯示資訊損失集中在哪
"""
import argparse
import math
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

from config import (
    JPEG_TARGET_SUBSAMPLING, JPEG_FLAT_COLOR_RATIO,
    JPEG_FLAT_QUALITY, JPEG_TARGET_QUALITY,
)
from common_utils import clean_str
from resize import _recompress_bytes, pick_quality_for   # 直接重用生產路徑，確保結果一致

JPEG_SUFFIXES = ('.jpg', '.jpeg')


def human(n_bytes):
    """位元組轉可讀字串。"""
    mb = n_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    if mb >= 1:
        return f"{mb:.2f} MB"
    return f"{n_bytes / 1024:.0f} KB"


def psnr(img_a, img_b):
    """兩張 RGB 圖的 PSNR（dB）；完全相同回傳 inf 以 99 表示。"""
    rms = ImageStat.Stat(ImageChops.difference(img_a, img_b)).rms
    mse = sum(v * v for v in rms) / len(rms)
    if mse <= 0:
        return 99.0
    return 20 * math.log10(255.0 / math.sqrt(mse))


def collect_samples(input_path, count, workdir):
    """依輸入型態取出要測試的 JPG，回傳 [(顯示名稱, 磁碟路徑), ...]。

    zip 會解到暫存區（不動原檔）；資料夾與單檔直接唯讀使用。
    一律取檔案最大的幾張——大檔細節最多，最容易暴露壓縮問題。
    """
    p = Path(input_path)
    if not p.exists():
        print(f"❌ 找不到輸入：{input_path}")
        return []

    if p.is_file() and p.suffix.lower() == '.zip':
        try:
            with zipfile.ZipFile(p, 'r') as zf:
                members = [it for it in zf.infolist()
                           if not it.is_dir() and it.filename.lower().endswith(JPEG_SUFFIXES)]
                if not members:
                    print(f"❌ 壓縮包裡沒有 JPG：{p.name}")
                    return []
                members.sort(key=lambda it: it.file_size, reverse=True)
                picked = []
                for it in members[:count]:
                    dst = workdir / Path(it.filename).name
                    with zf.open(it, 'r') as src, open(dst, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    picked.append((it.filename, dst))
                return picked
        except Exception as e:
            print(f"❌ 讀取壓縮包失敗：{e}")
            return []

    if p.is_file():
        if p.suffix.lower() not in JPEG_SUFFIXES:
            print(f"❌ 不是 JPG：{p.name}")
            return []
        return [(p.name, p)]

    files = [f for f in p.rglob('*') if f.is_file() and f.suffix.lower() in JPEG_SUFFIXES]
    if not files:
        print(f"❌ 資料夾裡沒有 JPG：{p}")
        return []
    files.sort(key=lambda f: f.stat().st_size, reverse=True)
    return [(f.name, f) for f in files[:count]]


def find_worst_block(orig, worst_variant, block=320):
    """找出兩張圖差異最大的區塊，回傳裁切框 (left, top, right, bottom)。

    刻意挑最糟的地方看，而不是看平均——平均值會把問題稀釋掉。
    """
    diff = ImageChops.difference(orig, worst_variant).convert('L')
    w, h = diff.size
    block = min(block, w, h)
    best, best_box = -1.0, (0, 0, block, block)

    for top in range(0, max(1, h - block + 1), block):
        for left in range(0, max(1, w - block + 1), block):
            box = (left, top, min(left + block, w), min(top + block, h))
            score = ImageStat.Stat(diff.crop(box)).mean[0]
            if score > best:
                best, best_box = score, box
    return best_box


def make_crop_comparison(orig, variants, box, out_path):
    """把原圖與各品質版本在同一區塊的 100% 裁切橫向拼成一張對照圖。"""
    label_h = 18
    crops = [('原檔', orig.crop(box))]
    crops += [(f'Q{q}', img.crop(box)) for q, img, _ in variants]

    cw, ch = crops[0][1].size
    canvas = Image.new('RGB', (cw * len(crops), ch + label_h), (24, 24, 28))
    draw = ImageDraw.Draw(canvas)

    sizes = ['—'] + [human(n) for _, _, n in variants]
    for i, ((name, crop), size_txt) in enumerate(zip(crops, sizes)):
        canvas.paste(crop, (i * cw, label_h))
        draw.text((i * cw + 4, 4), f"{name}  {size_txt}", fill=(240, 240, 240))
        if i:
            draw.line([(i * cw, 0), (i * cw, ch + label_h)], fill=(90, 90, 100))

    canvas.save(out_path)


def make_diff_map(orig, variant, out_path, amplify=12):
    """輸出差異放大圖：損失越大的地方越亮。"""
    diff = ImageChops.difference(orig, variant).convert('L')
    diff = diff.point(lambda v: min(255, v * amplify))
    diff.save(out_path)


def run_one(display_name, jpg_path, qualities, out_dir, report):
    """對單一張圖跑完整測試，並把結果寫進 report。"""
    safe = clean_str(display_name)
    stem = Path(display_name).stem.replace('/', '_').replace('\\', '_')[:60]
    orig_bytes = jpg_path.stat().st_size

    try:
        with Image.open(jpg_path) as im:
            orig = im.convert('RGB')
    except Exception as e:
        print(f"  ❌ 無法開啟 [{safe}]：{e}")
        return

    auto_q, ratio, kind = pick_quality_for(orig)
    kind_txt = '平塗(賽璐璐類)' if kind == 'flat' else '寫實(照片類)'
    info = (f"   原檔 {orig.size[0]}x{orig.size[1]}  {human(orig_bytes)}\n"
            f"   內容判定: 顏色數佔比 {ratio * 100:.2f}% "
            f"(門檻 {JPEG_FLAT_COLOR_RATIO * 100:.0f}%) → {kind_txt}，自動選用 Q{auto_q}")
    print(f"\n■ {safe}")
    print(info)
    report.append(f"\n■ {safe}")
    report.append(info)

    header = f"   {'品質':<8}{'大小':>12}{'縮減':>9}{'PSNR':>10}"
    print(header)
    print('   ' + '-' * 39)
    report.append(header)

    variants = []
    for q in qualities:
        try:
            data = _recompress_bytes(jpg_path, q, JPEG_TARGET_SUBSAMPLING)
        except Exception as e:
            print(f"   Q{q}: 重壓失敗 {e}")
            continue

        out_file = out_dir / f"{stem}_q{q}.jpg"
        out_file.write_bytes(data)

        with Image.open(out_file) as vim:
            variant = vim.convert('RGB')

        val = psnr(orig, variant)
        line = (f"   Q{q:<7}{human(len(data)):>12}"
                f"{(1 - len(data) / orig_bytes) * 100:>8.1f}%{val:>9.2f} dB")
        print(line)
        report.append(line)
        variants.append((q, variant, len(data)))

    if not variants:
        return

    # 以品質最低的版本找最劣區域——那是最容易看出問題的地方
    worst_q, worst_img, _ = min(variants, key=lambda v: v[0])
    box = find_worst_block(orig, worst_img)
    crop_path = out_dir / f"{stem}_crop.png"
    make_crop_comparison(orig, variants, box, crop_path)
    print(f"   📐 最劣區域對照（100% 原尺寸，取自 Q{worst_q} 差異最大處 {box[0]},{box[1]}）")
    print(f"      → {crop_path.name}")

    for q, variant, _ in variants:
        make_diff_map(orig, variant, out_dir / f"{stem}_diff_q{q}.png")
    print(f"   🔍 差異放大圖：{stem}_diff_q*.png（越亮 = 損失越多）")


def main():
    parser = argparse.ArgumentParser(
        description='用真實檔案實測不同 JPEG 品質的畫質與體積影響（不會修改輸入檔）')
    parser.add_argument('input', help='.zip 壓縮包 / 資料夾 / 單一 .jpg')
    parser.add_argument('--qualities', default='80,85,88,92',
                        help='要測試的品質，逗號分隔（預設 80,85,88,92）')
    parser.add_argument('--samples', type=int, default=3,
                        help='抽測幾張（取最大的幾張，預設 3）')
    parser.add_argument('--out', default='quality_test_out', help='輸出資料夾')
    args = parser.parse_args()

    try:
        qualities = sorted({int(x) for x in args.qualities.split(',') if x.strip()})
    except ValueError:
        print('❌ --qualities 格式錯誤，範例：--qualities 80,85,92')
        return 1
    if not qualities:
        print('❌ 至少要指定一個品質')
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print('=' * 60)
    print('🔬 JPEG 畫質驗證（輸入檔案唯讀，不會被修改喵！）')
    print(f'   測試品質: {", ".join("Q" + str(q) for q in qualities)}')
    print(f'   色度抽樣: {JPEG_TARGET_SUBSAMPLING} (與 resize.py 實際使用一致)')
    print(f'   輸出到  : {out_dir.resolve()}')
    print('=' * 60)

    report = []
    with tempfile.TemporaryDirectory() as tmp:
        samples = collect_samples(args.input, args.samples, Path(tmp))
        if not samples:
            return 1
        print(f'\n抽測 {len(samples)} 張（取檔案最大的幾張，細節最多、最容易看出問題）')
        for display_name, jpg_path in samples:
            run_one(display_name, jpg_path, qualities, out_dir, report)

    (out_dir / 'summary.txt').write_text('\n'.join(report), encoding='utf-8')

    print('\n' + '=' * 60)
    print('✅ 完成！建議這樣判斷：')
    print('   1. 先看 *_crop.png —— 那是最糟的區域，這裡能接受就整張都能接受')
    print('   2. 再把 *_q80.jpg 等丟進 NeeView 全螢幕看，那才是你平常的觀看情境')
    print('   3. 平塗風格通常降到 Q80 也看不出來；寫實紋理請看仔細一點')
    print('   4. 上面每張都有「顏色數佔比」——那是自動分流用的指標。')
    print(f'      若判定跟你的認知不符，調 config.py 的 JPEG_FLAT_COLOR_RATIO'
          f'（目前 {JPEG_FLAT_COLOR_RATIO * 100:.0f}%）')
    print(f'   5. 品質本身則調 JPEG_FLAT_QUALITY(平塗，目前 Q{JPEG_FLAT_QUALITY}) '
          f'與 JPEG_TARGET_QUALITY(寫實，目前 Q{JPEG_TARGET_QUALITY})')
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
