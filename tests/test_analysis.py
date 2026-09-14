"""分析流程：快取/待解析計數、解析額度上限、無標籤包仍要被分析、排行榜不被掃描順序截斷。"""
import sys

from _harness import Checker, Sandbox, capture, make_png_zip, read_json
import config


def ranked_tags():
    return {r['tag'] for r in read_json(config.TARGETS_CACHE)}


def main():
    c = Checker('分析流程')
    with Sandbox() as sb:
        config.IGNORED_TAG_KEYS = set(config.IGNORED_TAG_KEYS) | {'ABC'}
        import analysis
        import common_utils as cu
        cu._IGNORED_TAG_KEYS_LOWER = {k.lower() for k in config.IGNORED_TAG_KEYS}
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu)

        tagged = ['[aaa] 1.zip', '[bbb] 2.zip', '[ccc] 3.zip', '[ddd] 4.zip', '[eee] 5.zip']
        untagged = ['[ABC] u1.zip', '[Part 2] u2.zip', '沒有括號.zip']
        for i, n in enumerate(tagged + untagged):
            make_png_zip(sb.src / n, seed=i)

        def run(**kw):
            config.MAX_TARGET_FILES = analysis.MAX_TARGET_FILES = kw.pop('cap', 5000)
            config.EXTRA_SCAN_QUOTA = analysis.EXTRA_SCAN_QUOTA = kw.pop('quota', 0)
            return capture(analysis.main, auto_next=False, **kw)[0]

        c.section('解析額度是「上限」，不是可有可無的下限')
        out = run(quota=3)
        c.check(out.count('🔍新解析') == 3,
                f"額度 3 → 本輪只解析 3 個（實際 {out.count('🔍新解析')}）")
        c.check('達到上限 3' in out, '有說明是達到解析上限而停')
        c.check('再跑一次就會從沒掃過的繼續往下' in out, '有告訴使用者下一步')

        c.section('開工前先報數，載入舊資料不再逐筆刷 log')
        out = run(quota=3)
        c.check('3 筆直接套用快取，5 筆尚待解析' in out, '快取命中與待解析筆數每輪重算')
        c.check(out.count('🔍新解析') == 3, '只印這輪真的開檔解析的包')
        c.check('⚡Binary快取' not in out, '沒有為快取命中刷逐檔訊息')

        c.section('一輪一輪把收藏庫吃完')
        out = run(quota=3)
        c.check('6 筆直接套用快取，2 筆尚待解析' in out, '第三輪接著往下解析')
        out = run(quota=3)
        c.check('未達上限 3' in out and '沒有更多沒掃過的檔案' in out,
                '掃完了就說掃完了，不是硬湊額度')
        c.check('8 筆直接套用快取，0 筆尚待解析' in out, '八個包全部解析完畢')

        c.section('沒有可用標籤的包也要被分析')
        cache = cu.load_hash_cache()
        for n in untagged:
            c.check(cu.has_cached_stats(sb.src / n, cache), f'{n} 確實被開檔解析過')
        c.check('3 個包檔名裡沒有可用標籤' in out, '摘要回報無標籤的包數')

        for f in (config.HASH_CACHE_FILE, config.LOG_FILE):
            f.unlink(missing_ok=True)
        fresh = run(quota=0)
        c.check(fresh.count('[無標籤]') == 3,
                f"重新解析時，三個無標籤包都看得到逐檔訊息（實際 {fresh.count('[無標籤]')}）")
        c.check(ranked_tags() == {'aaa', 'bbb', 'ccc', 'ddd', 'eee'},
                f'排行榜只有真標籤，沒有硬湊的檔名標籤: {sorted(ranked_tags())}')

        c.section('額度 0 不限制；MAX_TARGET_FILES 仍然有效')
        for f in (config.HASH_CACHE_FILE, config.LOG_FILE, config.TARGETS_CACHE):
            f.unlink(missing_ok=True)
        out = run(quota=0)
        c.check(out.count('🔍新解析') == 8, f"額度 0 → 一輪掃完全部（實際 {out.count('🔍新解析')}）")

        for f in (config.HASH_CACHE_FILE, config.LOG_FILE, config.TARGETS_CACHE):
            f.unlink(missing_ok=True)
        out = run(cap=1, quota=0)
        c.check(out.count('🔍新解析') == 1, '抓滿 1 個潛力包就停止讀新檔')
        c.check('已抓滿 1 個高潛力爆發包' in out, '有說明是抓滿潛力包而停')

        c.section('排行榜不該被掃描順序截斷')
        c.check(len(ranked_tags()) >= 1, '掃描上限之後，快取已知的包仍然納入統計')
        out = run(cap=1, quota=0)
        c.check(len(ranked_tags()) >= 1, '再跑一次仍然看得到已知的包（不是被 break 砍掉）')

        c.section('追加解析：指定筆數優先於額度')
        for f in (config.HASH_CACHE_FILE, config.LOG_FILE, config.TARGETS_CACHE):
            f.unlink(missing_ok=True)
        out = run(quota=3, scan_limit=4)
        c.check(out.count('🔍新解析') == 4,
                f"追加 4 筆就是 4 筆，不受額度 3 影響（實際 {out.count('🔍新解析')}）")
        c.check('追加解析模式：本輪最多新解析 4 筆' in out, '有說明本輪上限')
        out = run(quota=3, scan_limit=100)
        c.check('少於指定的 100 筆' in out and '沒有更多沒掃過的檔案' in out,
                '追加筆數超過剩餘檔案時，說明是掃完了而不是中途放棄')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
