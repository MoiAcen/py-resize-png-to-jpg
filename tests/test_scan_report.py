"""開頭報的「尚待解析」必須等於這輪真的會去解析的數量。

回歸自實跑回報：開頭說 32816 筆尚待解析、追加解析 5000 筆，結果只解析了 13 筆。
差額是體積低於門檻、在黑名單、已標記放棄的檔案——它們在解析前就被跳過，
所以永遠沒有快取，卻一直被算成「待解析」。
"""
import sys

from _harness import Checker, Sandbox, capture, make_png_zip
import config


def main():
    c = Checker('掃描報告的數字')
    with Sandbox(MIN_ARCHIVE_SIZE_MB=1) as sb:       # 1 MB 門檻，方便造出「太小」的包
        import common_utils as cu
        import analysis
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu)

        # 太小：每個約 0.3 MB，低於 1 MB 門檻
        for i in range(10):
            make_png_zip(sb.src / f'[small{i:02d}] s.zip', seed=i, count=1)
        # 夠大：每個約 1.4 MB
        for i in range(3):
            make_png_zip(sb.src / f'[big{i}] b.zip', seed=100 + i, count=5)
        # 夠大但在黑名單
        for i in range(2):
            name = f'[black{i}] k.zip'
            make_png_zip(sb.src / name, seed=200 + i, count=5)
            cu.save_clean_file(name, config.LOG_FILE)
        # 夠大但已標記放棄
        flagged = sb.src / '[flag] f.zip'
        make_png_zip(flagged, seed=300, count=5)
        cu.save_lowgain_flags({cu.flag_key(flagged): {
            'name': flagged.name, 'ratio': 1.0, 'sig': cu.settings_signature()}})

        config.EXTRA_SCAN_QUOTA = analysis.EXTRA_SCAN_QUOTA = 0
        config.MAX_TARGET_FILES = analysis.MAX_TARGET_FILES = 5000

        c.section('開頭：待解析只算這輪真的會解析的')
        out, _ = capture(analysis.main, auto_next=False, scan_limit=5000)
        c.check('0 筆直接套用快取，3 筆尚待解析' in out,
                '16 個包裡只有 3 個符合條件，開頭就該報 3 筆待解析而不是 13 或 16')

        c.section('把不會解析的原因講清楚，數字要能加得起來')
        c.check('另有 13 筆這輪不會解析' in out, '10 太小 + 2 黑名單 + 1 標記放棄 = 13 筆不解析')
        c.check('低於 1 MB 門檻 10 個' in out, '說明有 10 個是太小')
        c.check('黑名單 2 個' in out, '說明有 2 個在黑名單')
        c.check('已標記放棄 1 個' in out, '說明有 1 個已標記放棄')

        c.section('實際解析的數量要等於開頭報的待解析數')
        c.check(out.count('🔍新解析') == 3, f"實際新解析 {out.count('🔍新解析')} 筆，等於開頭報的 3 筆")
        c.check('少於指定的 5000 筆' in out and '沒有更多沒掃過的檔案' in out,
                '追加數量大於可解析數時，說明是真的沒有更多可解析的了')

        c.section('第二輪：沒有東西可解析，不再有永遠待解析的幽靈')
        out, _ = capture(analysis.main, auto_next=False, scan_limit=5000)
        c.check('3 筆直接套用快取，0 筆尚待解析' in out, '三個合格的包都在快取，待解析歸零')
        c.check(out.count('🔍新解析') == 0, '沒有重複解析')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
