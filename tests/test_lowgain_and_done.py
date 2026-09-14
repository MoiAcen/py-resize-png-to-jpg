"""效率不足標記（不再來回空轉）與完成目錄的三條路徑。"""
import os
import sys
from concurrent.futures import ProcessPoolExecutor

from _harness import Checker, Sandbox, capture, make_zip, make_png_zip, read_json
import config


def main():
    c = Checker('放棄標記與完成目錄')

    # ---------- 被放棄的包不再來回空轉 ----------
    for collide in (False, True):
        with Sandbox() as sb:
            config.DONE_DIR = str(sb.src)          # 完成區指回收藏庫，與實際設定一致
            config.MIN_ARCHIVE_SAVING_RATIO = 0.99  # 逼出「效率不足」分支
            config.JPEG_LARGE_KB = 1
            import analysis
            import common_utils as cu
            import moveToResize as mv
            import resize
            analysis.trigger_move_script = lambda: None
            sb.sync(analysis, cu, mv, resize)
            pool = ProcessPoolExecutor(max_workers=2)

            name = '[testtag] pingpong.zip'
            c.section(f"效率不足 → 搬到完成區（{'完成區已有同名檔' if collide else '無同名碰撞'}）")
            make_zip(sb.work / name, [(f'p{i}.jpg', 'jpg', i) for i in range(3)])
            if collide:
                (sb.src / name).write_bytes(b'someone else already lives here')

            flags = cu.load_lowgain_flags()
            out, _ = capture(resize.slim_single_archive,
                             str(sb.work / name), pool, str(sb.tmp), flags)
            cu.save_lowgain_flags(flags)
            c.check('效率不足' in out, 'resize 判定效率不足並放棄替換')
            c.check(not (sb.work / name).exists(), '工作區已清空')
            expect = '[testtag] pingpong (1).zip' if collide else name
            done_file = sb.src / expect
            c.check(done_file.exists(), f'落到完成區，檔名為 {expect}')
            saved = read_json(config.LOWGAIN_FLAG_FILE)
            c.check(cu.flag_key(done_file) in saved,
                    '標記的 key 對得上完成區裡那個實體檔案（改名也對得上）')

            out, _ = capture(analysis.main, auto_next=False)
            c.check('試過但省太少' in out, 'analysis 認出並略過被標記的包')
            ranking = read_json(config.TARGETS_CACHE) if config.TARGETS_CACHE.exists() else []
            counted = sum(r['total_files'] for r in ranking
                          if 'testtag' in (r.get('tag') or '').lower())
            c.check(counted == (1 if collide else 0),
                    f'被標記的包沒有被計入標籤統計（計數 {counted}）')

            capture(mv.move_files_for_pattern, sb.src, sb.work,
                    set(), cu.load_hash_cache(), None)
            c.check(not (sb.work / expect).exists(), 'moveToResize 沒有把它搬回工作區')
            c.check(done_file.exists(), '檔案安穩留在完成區')

            import shutil
            shutil.copy2(done_file, sb.work / expect)
            out, _ = capture(resize.slim_single_archive, str(sb.work / expect),
                             pool, str(sb.tmp), flags, recheck=True)
            c.check('已標記為效率不足，跳過' not in out, '--recheck 能繞過標記重新評估')
            pool.shutdown()

    # ---------- 完成目錄 ----------
    with Sandbox() as sb:
        config.DONE_DIR = str(sb.src)
        config.MIN_ARCHIVE_SAVING_RATIO = 0.05
        config.JPEG_LARGE_KB = 1
        import resize
        sb.sync(resize)
        pool = ProcessPoolExecutor(max_workers=2)

        cases = [
            ('[t] good.zip', [('a0.png', 'png', 0), ('a1.png', 'png', 1)], '成功處理'),
            ('[t] opt.zip', None, '已最佳化'),
            ('[t] nowork.zip', [('readme.txt', 'text', 'no images' * 500)], '無可瘦身內容'),
        ]
        for name, members, label in cases:
            c.section(f'完成目錄：{label}')
            path = sb.work / name
            if members is None:
                from PIL import Image
                import tempfile, zipfile
                from pathlib import Path
                from _harness import noisy_image
                with tempfile.TemporaryDirectory() as td:
                    td = Path(td)
                    for i in range(3):
                        noisy_image(seed=i).save(td / f'b{i}.jpg', 'JPEG', quality=92,
                                                 subsampling=2, progressive=True, optimize=True)
                    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for f in sorted(td.iterdir()):
                            zf.write(f, f.name)
            else:
                make_zip(path, members)
            os.utime(path, (1_000_000_000, 1_000_000_000))

            out, _ = capture(resize.slim_single_archive, str(path), pool, str(sb.tmp), {})
            c.check(not path.exists(), '工作區已清空')
            landed = sb.src / name
            c.check(landed.exists(), f'已搬到完成區 {name}')
            if landed.exists():
                c.check(int(landed.stat().st_mtime) == 1_000_000_000, '修改時間與原檔一致')
            c.check('已搬至完成區' in out, 'log 有說搬到哪裡')

        c.section('完成區就是原地 / 關閉完成區')
        import importlib
        config.DONE_DIR = str(sb.work)
        importlib.reload(resize)
        p = make_png_zip(sb.work / '[t] inplace.zip', seed=5, count=2)
        out, _ = capture(resize.slim_single_archive, str(p), pool, str(sb.tmp), {})
        c.check(p.exists() and '已搬至完成區' not in out, '完成區就是原地時不做多餘搬移')

        config.DONE_DIR = ''
        importlib.reload(resize)
        p = make_png_zip(sb.work / '[t] off.zip', seed=6, count=2)
        capture(resize.slim_single_archive, str(p), pool, str(sb.tmp), {})
        c.check(p.exists() and not (sb.src / '[t] off.zip').exists(), '關閉時產出留在原地')
        pool.shutdown()

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
