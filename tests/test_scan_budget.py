"""掃描額度只能被「這輪實際做的工」消耗，快取命中不佔額度。

回歸自實跑回報：載入 31735 筆快取後，一輪連一個新檔案都沒開就宣告「已抓滿」，
29395 筆待解析的包因此永遠停在原地。
"""
import sys

from _harness import Checker, Sandbox, capture, make_png_zip
import config


def main():
    c = Checker('掃描額度')
    with Sandbox() as sb:
        import analysis
        import common_utils as cu
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu)

        # 全部都是高 PNG 佔比的「潛力包」，才測得出額度被什麼消耗掉
        for i in range(12):
            make_png_zip(sb.src / f'[t{i:02d}] pack.zip', seed=i)

        def run(cap, quota, **kw):
            config.MAX_TARGET_FILES = analysis.MAX_TARGET_FILES = cap
            config.EXTRA_SCAN_QUOTA = analysis.EXTRA_SCAN_QUOTA = quota
            return capture(analysis.main, auto_next=False, **kw)[0]

        c.section('先把一部分包餵進快取')
        out = run(cap=5000, quota=4)
        c.check(out.count('🔍新解析') == 4, f"第一輪解析 4 個（實際 {out.count('🔍新解析')}）")

        c.section('快取命中不得佔用潛力包額度')
        # 上限設 2：快取裡已有 4 個達標包，舊版會在讀到第 2 個快取時就喊停
        out = run(cap=2, quota=10)
        c.check('4 筆直接套用快取，8 筆尚待解析' in out, '快取與待解析筆數正確')
        c.check(out.count('🔍新解析') == 2,
                f"仍然有開新檔案，且停在解析出 2 個潛力包（實際 {out.count('🔍新解析')}）")
        c.check('本輪已解析出 2 個高潛力爆發包' in out, '訊息說的是「本輪解析出」而不是「抓滿」')

        c.section('快取再多也不會把新解析卡死')
        out = run(cap=3, quota=10)
        c.check(out.count('🔍新解析') == 3,
                f"快取已有 6 個達標包，本輪照樣解析 3 個新的（實際 {out.count('🔍新解析')}）")

        c.section('一輪一輪把收藏庫吃完，不會停在原地')
        seen = 6
        rounds = 0
        while True:
            out = run(cap=5000, quota=3)
            done = out.count('🔍新解析')
            rounds += 1
            seen += done
            if done == 0 or rounds > 10:
                break
        c.check('0 筆尚待解析' in out, f'最終全部解析完畢（共 {rounds} 輪）')
        c.check(rounds <= 4, f'沒有無限空轉，{rounds} 輪內收斂')

        c.section('額度仍然是上限')
        for f in (config.HASH_CACHE_FILE, config.LOG_FILE, config.TARGETS_CACHE):
            f.unlink(missing_ok=True)
        out = run(cap=5000, quota=5)
        c.check(out.count('🔍新解析') == 5, f"額度 5 → 只解析 5 個（實際 {out.count('🔍新解析')}）")

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
