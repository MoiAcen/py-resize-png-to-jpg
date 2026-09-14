"""失敗處理與內容保護：壞檔搬移、說明檔、缺 7-Zip、非目標內容不得被動到。"""
import hashlib
import io
import os
import sys
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from _harness import Checker, Sandbox, capture, make_zip, noisy_image
import config


def main():
    c = Checker('失敗處理與內容保護')
    with Sandbox() as sb:
        config.MIN_ARCHIVE_SAVING_RATIO = 0
        import common_utils as cu
        import resize
        sb.sync(resize)
        pool = ProcessPoolExecutor(max_workers=2)
        print(f"  (本機 7-Zip: {cu.find_7z()})")

        c.section('整個壓縮包打不開')
        broken = sb.work / 'broken.zip'
        broken.write_bytes(b'PK\x03\x04' + os.urandom(4000))
        capture(resize.slim_single_archive, str(broken), pool, str(sb.tmp), {})
        c.check(not broken.exists() and (sb.failed / 'broken.zip').exists(), '已搬到失敗區')
        c.check(not (sb.failed / 'broken.zip.txt').exists(), '整包打不開 → 不寫說明檔')

        c.section('部分內容物損毀')
        part = sb.work / 'partial.zip'
        make_zip(part, [('ok0.png', 'png', 0), ('ok1.png', 'png', 1), ('corrupt.png', 'png', 9)])
        raw = bytearray(part.read_bytes())
        with zipfile.ZipFile(part) as zf:
            info = zf.getinfo('corrupt.png')
            start = info.header_offset + 30 + len(info.filename) + len(info.extra) + 200
        raw[start:start + 400] = os.urandom(400)      # 只毀成員資料，中央目錄保持完整
        part.write_bytes(bytes(raw))
        capture(resize.slim_single_archive, str(part), pool, str(sb.tmp), {})
        c.check((sb.failed / 'partial.zip').exists(), '已搬到失敗區')
        txt = sb.failed / 'partial.zip.txt'
        c.check(txt.exists(), '有寫同名說明檔')
        if txt.exists():
            body = txt.read_text(encoding='utf-8')
            c.check('corrupt.png' in body, '說明檔列出壞掉的成員')
            c.check('ok0.png' not in body, '沒有把好的成員也列進去')

        c.section('沒裝 7-Zip 時的 rar/7z')
        rar = sb.work / 'need7z.rar'
        rar.write_bytes(b'Rar!\x1a\x07\x00' + os.urandom(3000))
        capture(resize.slim_single_archive, str(rar), pool, str(sb.tmp), {})
        if cu.find_7z():
            print('  (本機有 7-Zip，跳過此項)')
        else:
            c.check(rar.exists(), 'rar 留在原地（沒裝工具不算檔案壞掉）')
            c.check(not (sb.failed / 'need7z.rar').exists(), '沒有被誤搬到失敗區')

        c.section('WebP / 非圖片內容必須原封不動')
        keep = sb.work / 'mixed.zip'
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            noisy_image(seed=3).save(td / 'still.webp', 'WEBP', quality=90)
            frames = [noisy_image(seed=k, w=200, h=150) for k in range(4)]
            frames[0].save(td / 'anim.webp', 'WEBP', save_all=True,
                           append_images=frames[1:], duration=120, loop=0)
            (td / 'note.txt').write_text('keep me' * 300, encoding='utf-8')
            noisy_image(seed=5).save(td / 'big.png', 'PNG')
            with zipfile.ZipFile(keep, 'w', zipfile.ZIP_DEFLATED) as zf:
                for x in sorted(td.iterdir()):
                    zf.write(x, x.name)
        with zipfile.ZipFile(keep) as zf:
            before = {n: hashlib.md5(zf.read(n)).hexdigest() for n in zf.namelist()}

        capture(resize.slim_single_archive, str(keep), pool, str(sb.tmp), {})
        c.check(keep.exists(), '產出壓縮包存在')
        with zipfile.ZipFile(keep) as zf:
            names = zf.namelist()
            for n in ('still.webp', 'anim.webp', 'note.txt'):
                c.check(n in names and hashlib.md5(zf.read(n)).hexdigest() == before[n],
                        f'{n} 仍在包內且位元組完全未變')
            from PIL import Image
            with Image.open(io.BytesIO(zf.read('anim.webp'))) as im:
                c.check(getattr(im, 'n_frames', 1) == 4, '動態 WebP 的 4 個影格都還在')
            c.check(any(n.endswith('.jpg') for n in names), 'PNG 已依規則轉成 JPG')
        pool.shutdown()

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
