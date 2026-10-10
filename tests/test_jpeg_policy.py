"""JPG 重壓的設定不變量：目標品質怎麼調，壓完的檔案都不能被下一輪再挑中。

JPEG_TARGET_QUALITY、JPEG_SKIP_BELOW_QUALITY、JPEG_RECOMPRESS_MIN_QUALITY 三者互相咬合。
目標品質 85 剛好等於「跳過低品質」的門檻，落在邊界上，所以不只驗現在的設定，
也掃過一整排目標值——任何一個值若造成「壓完又被判定需要重壓」，就會是無限迴圈。
"""
import shutil
import sys
from pathlib import Path

from _harness import Checker, Sandbox, noisy_image
import config


def main():
    c = Checker('JPG 重壓設定的不變量')
    with Sandbox() as sb:
        import jpeg_inspector as ji
        import resize

        orig_target = (resize.JPEG_TARGET_QUALITY, ji.JPEG_TARGET_QUALITY)

        def set_target(q):
            resize.JPEG_TARGET_QUALITY = q
            ji.JPEG_TARGET_QUALITY = q

        def one_pass(path):
            """跟 process_jpegs_auto 同樣的判斷與動作；回傳 (走哪條, 這輪有沒有改檔)。"""
            verdict = ji.classify_jpeg(ji.inspect_jpeg(path))
            if verdict['action'] == 'skip':
                return None, False
            route = 'B' if 'recompress' in verdict['action'] else 'A'
            changed, _kind = resize.jpeg_fix_worker((str(path), route, config.JPEG_TARGET_SUBSAMPLING))
            return route, changed

        photo = noisy_image(seed=3, w=480, h=360)

        c.section('設定本身要自洽')
        c.check(1 <= config.JPEG_FLAT_QUALITY <= config.JPEG_TARGET_QUALITY <= 100,
                f'平塗目標 Q{config.JPEG_FLAT_QUALITY} 不高於寫實目標 Q{config.JPEG_TARGET_QUALITY}')
        c.check(config.JPEG_TARGET_QUALITY == 85, '出廠的寫實目標是 Q85（依 30 張插畫的實測決定）')

        c.section('目前的設定：Q95 來源重壓一次之後，下一輪不再動它')
        set_target(config.JPEG_TARGET_QUALITY)
        src = sb.root / 'q95.jpg'
        photo.save(src, 'JPEG', quality=95, subsampling=2)
        size0 = src.stat().st_size
        route, changed = one_pass(src)
        rep = ji.inspect_jpeg(src)
        c.check(route == 'B' and changed, 'Q95 baseline 走 B 重壓並且確實變小')
        c.check(src.stat().st_size < size0, f'檔案變小了（{size0} → {src.stat().st_size} 位元組）')
        c.check(rep['est_quality'] == config.JPEG_TARGET_QUALITY,
                f"壓完的估算品質正好是目標 Q{config.JPEG_TARGET_QUALITY}（實際 Q{rep['est_quality']}）")
        c.check(rep['progressive'] and rep['subsampling'] == '4:2:0', '輸出是 progressive 4:2:0')
        after = src.read_bytes()
        for i in (2, 3):
            route, changed = one_pass(src)
            c.check(route is None and not changed, f'第 {i} 輪：判定為跳過，沒有再動')
        c.check(src.read_bytes() == after, '連跑三輪，檔案位元組只被改過一次（沒有累積重壓）')

        c.section('Q99 4:4:4 來源也一樣：一次到位、之後不再動')
        src2 = sb.root / 'q99.jpg'
        photo.save(src2, 'JPEG', quality=99, subsampling=0)
        route, changed = one_pass(src2)
        c.check(route == 'B' and changed, 'Q99 4:4:4 走 B 重壓')
        after2 = src2.read_bytes()
        route, changed = one_pass(src2)
        c.check(route is None and src2.read_bytes() == after2, '第二輪不再動')

        c.section('以前處理成 Q92 的檔案：改成 Q85 之後不會被再壓一次')
        old = sb.root / 'processed_q92.jpg'
        photo.save(old, 'JPEG', quality=92, subsampling=2, progressive=True, optimize=True)
        before_old = old.read_bytes()
        route, changed = one_pass(old)
        c.check(route is None and not changed and old.read_bytes() == before_old,
                '舊版流程產出的 Q92 progressive 檔：判定為跳過，位元組不變')

        c.section('掃過一整排目標品質：沒有任何一個值會造成「壓完又被挑中」')
        bad = []
        for target in (60, 70, 75, 80, 84, 85, 86, 88, 90, 92, 94, 95, 96):
            set_target(target)
            f = sb.root / f't{target}.jpg'
            photo.save(f, 'JPEG', quality=95, subsampling=2)
            one_pass(f)
            snapshot = f.read_bytes()
            route2, changed2 = one_pass(f)
            if route2 is not None or changed2 or f.read_bytes() != snapshot:
                bad.append((target, route2, changed2))
        c.check(not bad, f'13 個目標值都一次到位、第二輪跳過（有問題的: {bad}）')

        c.section('標記簽章：改目標品質會讓舊的「效率不足」標記自動失效')
        import common_utils as cu
        set_target(92)
        cu_target = cu.JPEG_TARGET_QUALITY
        cu.JPEG_TARGET_QUALITY = 92
        sig_old = cu.settings_signature()
        cu.JPEG_TARGET_QUALITY = 85
        sig_new = cu.settings_signature()
        cu.JPEG_TARGET_QUALITY = cu_target
        c.check(sig_old != sig_new and 'tq92' in sig_old and 'tq85' in sig_new,
                '簽章含目標品質，改了就不同，之前被標記放棄的包會重新評估')

        resize.JPEG_TARGET_QUALITY, ji.JPEG_TARGET_QUALITY = orig_target

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
