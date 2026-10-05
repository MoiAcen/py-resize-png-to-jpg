"""暫存工作區可以指到別的磁碟，指錯了也要能自己退回去。"""
import sys
from pathlib import Path

from _harness import Checker, Sandbox, capture, make_png_zip
import config


def run_resize(resize, record):
    """跑一輪 resize.main()，並記下實際用到的暫存工作區。"""
    original = resize.slim_single_archive

    def spy(archive_path, pool, temp_work_base, **kw):
        record.append(Path(temp_work_base))
        return original(archive_path, pool, temp_work_base, **kw)

    resize.slim_single_archive = spy
    try:
        sys.argv = ['resize.py']
        return capture(resize.main)[0]
    finally:
        resize.slim_single_archive = original


def main():
    c = Checker('暫存工作區位置')

    c.section('預設：開在 TARGET_DIR 底下（舊行為）')
    with Sandbox() as sb:
        import resize
        sb.sync(resize)
        make_png_zip(sb.work / '[t] a.zip', seed=1, count=2)
        used = []
        run_resize(resize, used)
        c.check(len(used) == 1, '有處理到壓縮包')
        c.check(used[0] == sb.work / '_temp_work',
                f'暫存區在工作區底下: {used[0]}')
        c.check(not (sb.work / '_temp_work').exists(), '跑完有清掉暫存資料夾')

    c.section('指定到另一個位置（模擬另一顆 SSD）')
    with Sandbox() as sb:
        other = sb.root / 'other_ssd'
        config.TEMP_WORK_DIR = str(other)
        import resize
        sb.sync(resize)
        make_png_zip(sb.work / '[t] a.zip', seed=2, count=2)
        used = []
        out = run_resize(resize, used)
        c.check(used and used[0] == other / '_temp_work',
                f'暫存區開在指定的位置: {used[0] if used else None}')
        c.check('暫存工作區' in out and str(other) in out, 'log 有說暫存區在哪裡')
        c.check(not (sb.work / '_temp_work').exists(), '沒有在工作區底下留下暫存資料夾')
        c.check(not (other / '_temp_work').exists(), '跑完有清掉指定位置下的暫存資料夾')
        c.check(other.exists(), '指定的根目錄本身保留（那是使用者指定的位置）')
        c.check(list(sb.src.glob('*.zip')) or list(sb.work.glob('*.zip')), '壓縮包仍然產出')

    c.section('內容正確性不受暫存區位置影響')
    with Sandbox() as sb:
        config.TEMP_WORK_DIR = str(sb.root / 'elsewhere')
        import resize
        sb.sync(resize)
        arc = sb.work / '[t] mixed.zip'
        from _harness import make_zip
        make_zip(arc, [('a.png', 'png', 3), ('note.txt', 'text', 'keep' * 200)])
        before = arc.stat().st_size
        used = []
        out = run_resize(resize, used)
        c.check('數量不吻合' not in out, '數量驗收通過')
        import zipfile
        with zipfile.ZipFile(arc) as zf:
            names = sorted(zf.namelist())
        c.check(names == ['a.jpg', 'note.txt'], f'產出內容正確: {names}')
        c.check(arc.stat().st_size <= before, '壓縮包沒有變大')

    c.section('指定的位置不能用 → 退回 TARGET_DIR，不中斷')
    with Sandbox() as sb:
        # 用一個「同名檔案佔位」的路徑：mkdir 一定失敗
        blocker = sb.root / 'blocked'
        blocker.write_text('not a directory', encoding='utf-8')
        config.TEMP_WORK_DIR = str(blocker)
        import resize
        sb.sync(resize)
        make_png_zip(sb.work / '[t] a.zip', seed=4, count=2)
        used = []
        out = run_resize(resize, used)
        c.check('無法使用' in out, '有警告指定的位置不能用')
        c.check(used and used[0] == sb.work / '_temp_work',
                f'退回工作區底下: {used[0] if used else None}')
        c.check('數量不吻合' not in out, '退回之後照樣完成轉檔')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
