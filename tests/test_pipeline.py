"""端對端：analysis → moveToResize → resize 三階段串起來跑一次。"""
import re
import sys
import zipfile
from pathlib import Path

from _harness import Checker, Sandbox, capture, feed_input, make_zip, make_png_zip, read_json
import config


def main():
    c = Checker('三階段端對端')
    with Sandbox() as sb:
        config.DONE_DIR = str(sb.src)             # 處理完搬回收藏庫，與實際設定一致
        config.MIN_ARCHIVE_SAVING_RATIO = 0.10
        config.JPEG_LARGE_KB = 1
        import analysis
        import common_utils as cu
        import moveToResize as mv
        import resize
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu, mv, resize)

        c.section('準備一個有代表性的收藏庫')
        make_png_zip(sb.src / '[Kurohime] PNG 大包.zip', seed=1, count=4)
        make_zip(sb.src / '[Kurohime] 混合包.zip',
                 [('a.png', 'png', 5), ('b.jpg', 'jpg', 6), ('note.txt', 'text', 'keep' * 200)])
        make_png_zip(sb.src / '[Other] 別人的.zip', seed=9, count=2)
        make_zip(sb.src / 'ABC-無標籤-01.zip', [('u.png', 'png', 11)])
        c.check(len(list(sb.src.glob('*.zip'))) == 4, '四個壓縮包就位')

        c.section('階段一：分析並產出排行榜')
        out, _ = capture(analysis.main, auto_next=False)
        c.check('快取寫入失敗' not in out, '排行榜快取正常寫出')
        ranked = read_json(config.TARGETS_CACHE)
        tags = {r['tag']: r['total_files'] for r in ranked}
        c.check(tags.get('Kurohime') == 2, f'Kurohime 底下兩個包: {tags.get("Kurohime")}')
        c.check('1 個包檔名裡沒有可用標籤' in out, '無標籤的包有被分析並回報')

        c.section('階段一半：用切割規則把無標籤的包歸位')
        feed_input(mv, ['-', '1', 'A'])
        out, ok = capture(mv.rule_tag_wizard, sb.src)
        c.check(ok and 'ABC' in out, '[S] 把 ABC-無標籤-01.zip 歸到 ABC')
        capture(analysis.main, auto_next=False)
        tags = {r['tag'] for r in read_json(config.TARGETS_CACHE)}
        c.check('ABC' in tags, f'重新分析後 ABC 上了排行榜: {sorted(tags)}')

        c.section('階段二：把 Kurohime 搬到工作區')
        feed_input(mv, ['n'])
        regex = re.compile('(' + r'[\(【\[\（]\s*Kurohime\s*[\)】\]\）]' + ')', re.IGNORECASE)
        capture(mv.move_files_for_pattern, sb.src, sb.work,
                cu.load_processed_files(), cu.load_hash_cache(), regex, None, ['Kurohime'])
        moved = sorted(p.name for p in sb.work.glob('*.zip'))
        c.check(moved == ['[Kurohime] PNG 大包.zip', '[Kurohime] 混合包.zip'],
                f'兩個 Kurohime 包進了工作區: {moved}')
        c.check(len(list(sb.src.glob('*.zip'))) == 2, '其他包留在收藏庫')

        c.section('階段三：轉檔瘦身')
        before = {p.name: p.stat().st_size for p in sb.work.glob('*.zip')}
        mtimes = {p.name: int(p.stat().st_mtime) for p in sb.work.glob('*.zip')}
        sys.argv = ['resize.py']
        out, _ = capture(resize.main)
        c.check('數量不吻合' not in out, '沒有任何包卡在數量驗收')
        c.check(not list(sb.work.glob('*.zip')), '工作區已清空（產出都搬去完成區了）')

        done = {p.name: p for p in sb.src.glob('*.zip')}
        c.check(len(done) == 4, f'四個包全部回到收藏庫: {sorted(done)}')
        for name, old_size in before.items():
            if name in done:
                c.check(done[name].stat().st_size <= old_size,
                        f'{name} 沒有變大（{old_size} → {done[name].stat().st_size}）')
                c.check(int(done[name].stat().st_mtime) == mtimes[name],
                        f'{name} 修改時間與原檔一致')

        c.section('產出內容檢查')
        with zipfile.ZipFile(done['[Kurohime] 混合包.zip']) as zf:
            names = sorted(zf.namelist())
            c.check('note.txt' in names, '非圖片檔仍在包內')
            c.check(any(n.endswith('.jpg') for n in names), 'PNG 已轉成 JPG')
            c.check(not any(n.endswith('.png') for n in names), '包內已無 PNG')

        c.section('再跑一次分析：處理過的包不該又被挑出來')
        out, _ = capture(analysis.main, auto_next=False)
        c.check('快取寫入失敗' not in out, '第二輪分析正常完成')
        c.check('已最佳化' in out or 'Rank' in out, '第二輪有正常產出報告')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
