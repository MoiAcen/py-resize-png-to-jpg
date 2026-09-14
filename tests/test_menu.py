"""搬移工具選單：排行榜列出筆數、全排名翻頁、追加解析、關鍵字標籤、切割規則精靈。"""
import re
import sys

from _harness import Checker, Sandbox, capture, feed_input, make_png_zip, read_json
import config


def main():
    c = Checker('搬移工具選單')
    with Sandbox() as sb:
        config.SHOW_TOP_N = 3
        config.SAVE_RANK_N = 10
        config.RANK_PAGE_SIZE = 2
        config.EXTRA_ANALYZE_DEFAULT = 500
        import analysis
        import common_utils as cu
        import moveToResize as mv
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu, mv)

        for i in range(8):
            make_png_zip(sb.src / f'[t{i:02d}] pack.zip', seed=i)

        c.section('追加解析：指定筆數、跑完不會自己再開一次搬移工具')
        feed_input(mv, ['3'])
        out, ok = capture(mv.run_extra_analysis)
        c.check(ok, '追加解析回報成功')
        c.check(out.count('🔍新解析') == 3, f"只解析 3 筆（實際 {out.count('🔍新解析')}）")
        c.check('搬移工具' not in out.split('排行榜')[-1], '沒有遞迴啟動搬移工具')
        ranked = read_json(config.TARGETS_CACHE)
        c.check(len(ranked) == 3, f'排行榜累積 3 個標籤: {len(ranked)}')

        feed_input(mv, [''])          # Enter 用預設值
        out, _ = capture(mv.run_extra_analysis)
        c.check(f'追加解析 {config.EXTRA_ANALYZE_DEFAULT}' in out, 'Enter 使用預設筆數')
        ranked = read_json(config.TARGETS_CACHE)
        c.check(len(ranked) == 8, '剩下的包被補完，八個標籤全部上榜')

        feed_input(mv, ['abc'])
        out, ok = capture(mv.run_extra_analysis)
        c.check(not ok and '正整數' in out, '輸入非數字會被擋下來')

        c.section('選單只列前 SHOW_TOP_N 名，快取存滿 SAVE_RANK_N 名')
        out, _ = capture(mv.print_menu, ranked)
        c.check(f'共 {len(ranked)} 名，以下列出前 3 名' in out, '標題標示總名次與列出筆數')
        c.check(out.count('] ── PNG:') == 3, '只列 3 行')
        for label in ('[R] 顯示全排名', '[E] 追加解析', '[S] 無標籤檔案快速分類'):
            c.check(label in out, f'選單上有 {label}')

        c.section('顯示全排名：翻頁')
        feed_input(mv, ['', '', 'P', '4', 'Q'])
        out, _ = capture(mv.show_full_ranking, ranked)
        c.check('第 1/4 頁' in out and '第 2/4 頁' in out, 'Enter 往下翻頁')
        c.check('第 3/4 頁' in out, '翻到第 3 頁')
        c.check(out.count('第 2/4 頁') == 2, '[P] 回上一頁')
        c.check('第 4/4 頁' in out, '[數字] 直接跳頁')
        c.check('第 1~2 名' in out and '第 7~8 名' in out, '每頁標示名次區間')

        feed_input(mv, ['4', ''])
        out, _ = capture(mv.show_full_ranking, ranked)
        c.check('第 4/4 頁' in out, '最後一頁按 Enter 正常結束，不會卡住')

        out, _ = capture(mv.show_full_ranking, [])
        c.check('先跑一次 analysis.py' in out, '沒有排行榜快取時有提示')

        c.section('名次可以直接輸入（快取有 SAVE_RANK_N 筆）')
        out, sel = capture(mv.resolve_selection, '7', ranked, sb.src, set(), {})
        c.check(sel is not None and sel[0] == [ranked[6]['tag']], '輸入 7 選到第 7 名的標籤')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
