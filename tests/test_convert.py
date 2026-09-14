"""轉檔安全性：PNG 轉出的 JPG 不得蓋掉包內同名的既有 JPG。"""
import hashlib
import io
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor

from _harness import Checker, Sandbox, capture, make_zip, noisy_image
import config


def main():
    c = Checker('轉檔安全性')
    with Sandbox() as sb:
        config.MIN_ARCHIVE_SAVING_RATIO = 0
        import resize
        sb.sync(resize)
        pool = ProcessPoolExecutor(max_workers=4)

        c.section('包內同時有 a.png 與 a.jpg')
        arc = sb.work / 'collide.zip'
        make_zip(arc, [('a.png', 'png', 1), ('a.jpg', 'jpg', 99),
                       ('b.png', 'png', 2), ('sub/a.png', 'png', 3)])
        with zipfile.ZipFile(arc) as zf:
            before_jpg = zf.read('a.jpg')
            n_in = len(zf.namelist())

        out, _ = capture(resize.slim_single_archive, str(arc), pool, str(sb.tmp), {})
        c.check('數量不吻合' not in out, '數量驗收通過（沒有檔案憑空消失）')
        c.check('🔀 有 1 張' in out, 'log 回報撞名改名')
        with zipfile.ZipFile(arc) as zf:
            names = sorted(zf.namelist())
            c.check(len(names) == n_in, f'產出檔數與輸入相同 ({len(names)} == {n_in})')
            c.check('a.jpg' in names and 'a.png.jpg' in names,
                    f'既有的 a.jpg 與轉出的 a.png.jpg 都在: {names}')
            c.check('a.png' not in names, '原 a.png 已轉走')
            c.check('sub/a.jpg' in names, '不同資料夾的同名 PNG 正常用 .jpg')

            from PIL import Image, ImageChops, ImageStat
            kept = Image.open(io.BytesIO(zf.read('a.jpg'))).convert('RGB')
            conv = Image.open(io.BytesIO(zf.read('a.png.jpg'))).convert('RGB')
            orig = Image.open(io.BytesIO(before_jpg)).convert('RGB')

            def mad(a, b):
                return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
            # 測試圖是純噪訊(4:2:0 重壓的最壞情況)，絕對差本來就大，所以比相對距離
            c.check(mad(kept, orig) < mad(kept, conv),
                    f'存活的 a.jpg 是原本那張的重壓版，不是被 PNG 蓋掉 '
                    f'(離原檔 {mad(kept, orig):.1f} < 離PNG {mad(kept, conv):.1f})')

        c.section('30 組撞名併發處理')
        arc2 = sb.work / 'many.zip'
        members = []
        for i in range(30):
            members.append((f'p{i}.png', 'png', 100 + i))
            members.append((f'p{i}.jpg', 'jpg', 500 + i))
        make_zip(arc2, members)
        out, _ = capture(resize.slim_single_archive, str(arc2), pool, str(sb.tmp), {})
        c.check('數量不吻合' not in out, '數量驗收通過')
        c.check('🔀 有 30 張' in out, '30 組撞名全部被認出')
        with zipfile.ZipFile(arc2) as zf:
            names = zf.namelist()
            c.check(len(names) == 60, f'產出 {len(names)} 檔 = 輸入 60 檔')
            digests = {hashlib.md5(zf.read(n)).hexdigest() for n in names}
            c.check(len(digests) == 60, '每個檔案內容都不同（併發沒有互相覆蓋）')
            c.check(all(f'p{i}.png.jpg' in names and f'p{i}.jpg' in names for i in range(30)),
                    '30 組都是 pN.jpg + pN.png.jpg 成對存在')

        c.section('轉檔失敗不得留下殘骸')
        arc3 = sb.work / 'bad.zip'
        make_zip(arc3, [('ok.png', 'png', 7), ('broken.png', 'text', 'x' * 5000)])
        out, _ = capture(resize.slim_single_archive, str(arc3), pool, str(sb.tmp), {})
        c.check('數量不吻合' not in out, '數量驗收通過（沒有留下佔位檔）')
        with zipfile.ZipFile(arc3) as zf:
            c.check(sorted(zf.namelist()) == ['broken.png', 'ok.jpg'],
                    f'壞檔原樣保留、好檔正常轉出: {sorted(zf.namelist())}')
        pool.shutdown()

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
