"""兩條歸類流程：[T] 關鍵字標籤、[S] 切割規則精靈。兩者都不得更動檔名。"""
import re
import sys

from _harness import Checker, Sandbox, capture, feed_input, make_png_zip, read_json
import config


def main():
    c = Checker('歸類流程')

    # ---------- [T] 關鍵字標籤 ----------
    with Sandbox() as sb:
        import analysis
        import common_utils as cu
        import moveToResize as mv
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu, mv)
        cu.load_manual_tags(force=True)
        cu.load_tag_rule(force=True)

        names = ['[tako123] aa.zip', '[tako] aa.zip', 'tako aaa.zip', '[Kurohime] 別人的包.zip']
        for i, n in enumerate(names):
            make_png_zip(sb.src / n, seed=i)

        c.section('[T] 關鍵字標籤：標記前')
        capture(analysis.main, auto_next=False)
        before = {r['tag'] for r in read_json(config.TARGETS_CACHE)}
        c.check('tako123' in before and 'tako' in before, 'tako123 與 tako 是兩個不同的排行項目')
        c.check(not any(r['sample'] == 'tako aaa.zip' for r in read_json(config.TARGETS_CACHE)),
                '沒有括號的 tako aaa.zip 進不了排行榜')

        c.section('[T] 標記後：收攏成同一個排序標籤')
        feed_input(mv, ['T'])
        out, ret = capture(mv.smart_analyze_keyword_on_the_fly,
                           'tako', sb.src, cu.load_processed_files(), cu.load_hash_cache())
        c.check('已把標籤「tako」掛到 3 個檔案上' in out, '三個 tako 檔都被掛上標籤')
        c.check('檔名一個字都沒改' in out, '有說明沒有動檔名')
        c.check(ret is None, '標記後回到選單，不執行搬移')
        c.check(sorted(p.name for p in sb.src.glob('*.zip')) == sorted(names),
                '硬碟上四個檔名一個都沒動')

        capture(analysis.main, auto_next=False)
        by_tag = {r['tag']: r['total_files'] for r in read_json(config.TARGETS_CACHE)}
        c.check(by_tag.get('tako') == 3, f'tako 底下有 3 個檔案: {by_tag.get("tako")}')
        c.check('tako123' not in by_tag, 'tako123 不再是獨立項目（已被手動標籤取代）')
        c.check(by_tag.get('Kurohime') == 1, '沒被標記的包完全不受影響')

        out, _ = capture(analysis.main, auto_next=False)
        c.check('4 筆直接套用快取，0 筆尚待解析' in out,
                '掛標籤不會讓任何包被重新解析（沒動檔名，快取 key 沒變）')

        c.section('[T] 標記後仍能用同一個標籤搬移')
        feed_input(mv, ['n'])
        regex = re.compile('(' + r'[\(【\[\（]\s*' + re.escape('tako') + r'\s*[\)】\]\）]' + ')',
                           re.IGNORECASE)
        capture(mv.move_files_for_pattern, sb.src, sb.work,
                cu.load_processed_files(), cu.load_hash_cache(), regex, None, ['tako'])
        c.check(sorted(p.name for p in sb.work.glob('*.zip')) == sorted(names[:3]),
                '三個 tako 檔都搬到工作區')
        c.check([p.name for p in sb.src.glob('*.zip')] == ['[Kurohime] 別人的包.zip'],
                '其他包留在原地')

    # ---------- [S] 切割規則精靈 ----------
    with Sandbox() as sb:
        import analysis
        import common_utils as cu
        import moveToResize as mv
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu, mv)
        cu.load_manual_tags(force=True)
        cu.load_tag_rule(force=True)

        names = ['ABC-BBB-CC.zip', 'ABC-DDD.zip', 'XYZ-001.zip',
                 '2024-01-05 某活動.zip', '[Kurohime] 有標籤.zip', '沒有分隔符.zip']
        for i, n in enumerate(names):
            make_png_zip(sb.src / n, seed=i)

        c.section('[S] 切割規則：預覽是臨時的，取消就什麼都不改')
        feed_input(mv, ['', '', 'Q'])
        out, ok = capture(mv.rule_tag_wizard, sb.src)
        c.check('[ABC]' in out and '2 個檔案' in out, 'ABC 兩個檔案被分成一組')
        c.check('[XYZ]' in out and '[2024]' in out, 'XYZ 與 2024 也各自成組（先列出來給人看）')
        c.check('[Kurohime]' not in out, '本來就有標籤的包不列入')
        c.check('沒有分隔符' not in out.split('[A] 全部採用')[0],
                '切不出東西的不列入（檔名裡要有分隔符）')
        c.check(not ok and '已取消' in out, '[Q] 取消不做任何變更')
        c.check(not config.TAG_RULE_FILE.exists(), '取消後連規則檔都沒寫出來')

        c.section('[S] 只採用選到的那組')
        feed_input(mv, ['', '', '1'])
        out, ok = capture(mv.rule_tag_wizard, sb.src)
        c.check(ok and '已採用 1 個標籤，涵蓋 2 個檔案' in out, '只採用第 1 組')
        cu.load_tag_rule(force=True)
        rule = cu.load_tag_rule()
        c.check(rule['accepted'] == ['ABC'], f"規則只記下 ABC: {rule['accepted']}")
        c.check(rule['separator'] == '-' and rule['field'] == 1, '分隔符與段數一併存下來')
        c.check(sorted(p.name for p in sb.src.glob('*.zip')) == sorted(names),
                '檔名一個字都沒改')

        c.section('採用過的自動歸位，沒採用的維持無標籤')
        c.check(cu.extract_all_tags('ABC-BBB-CC.zip') == ['ABC'], '既有的 ABC 檔自動歸位')
        c.check(cu.extract_all_tags('ABC-NEW-999.zip') == ['ABC'], '新進來的 ABC 檔也自動歸位')
        c.check(cu.extract_all_tags('2024-01-05 某活動.zip') == [], '沒採用的 2024 維持無標籤')

        capture(analysis.main, auto_next=False)
        by_tag = {r['tag']: r['total_files'] for r in read_json(config.TARGETS_CACHE)}
        c.check(by_tag.get('ABC') == 2, f'排行榜出現 ABC（2 個檔案）: {by_tag.get("ABC")}')
        c.check('2024' not in by_tag and 'XYZ' not in by_tag, '沒採用的不會跑到排行榜上')

        c.section('組別太多時只列前 N 組')
        config.RULE_TAG_SHOW_N = mv.RULE_TAG_SHOW_N = 2
        for i in range(4):
            make_png_zip(sb.src / f'G{i}-part-{i}.zip', seed=50 + i)
        feed_input(mv, ['', '', 'Q'])
        out, _ = capture(mv.rule_tag_wizard, sb.src)
        listing = out.split('[A] 採用上列')[0]
        c.check(listing.count('個檔案 |') == 2, f'只列 2 組（實際 {listing.count("個檔案 |")}）')
        c.check('其餘' in out and '組這次不列' in out, '有說明還有幾組沒列出來')
        c.check('採用之後再進來一次' in out, '有告訴使用者沒列到的會遞補')
        c.check('[A] 採用上列 2 組' in out, '[A] 的文字與實際列出的組數一致')

        feed_input(mv, ['', '', '3'])
        out, ok = capture(mv.rule_tag_wizard, sb.src)
        c.check(not ok and '無效的編號' in out, '超過列出範圍的編號會被擋下來')
        config.RULE_TAG_SHOW_N = mv.RULE_TAG_SHOW_N = 30
        for i in range(4):
            (sb.src / f'G{i}-part-{i}.zip').unlink()

        c.section('第二次進來：分隔符記住了，已採用的不重複加')
        feed_input(mv, ['', '', 'A'])
        out, ok = capture(mv.rule_tag_wizard, sb.src)
        c.check('[ABC]' not in out.split('[A] 全部採用')[0],
                'ABC 已經有標籤，不再出現在無標籤清單裡')
        c.check(ok and 'XYZ' in out, '剩下的組別仍可採用')
        cu.load_tag_rule(force=True)
        c.check(set(cu.load_tag_rule()['accepted']) == {'ABC', 'XYZ', '2024'},
                f"全部採用後清單累加: {cu.load_tag_rule()['accepted']}")

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
