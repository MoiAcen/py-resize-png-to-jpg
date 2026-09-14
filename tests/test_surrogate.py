"""Windows 檔名可能帶落單 surrogate：各種快取都要寫得進去、讀得回來、還對得上原檔。"""
import json
import re
import sys

from _harness import Checker, Sandbox, capture, feed_input, make_png_zip
import config

# 使用者實際遇到的是 '\udef3'。Linux 上只有 surrogateescape 範圍(U+DC80~DCFF)的
# surrogate 能真的當檔名建出來，所以實體檔案用 '\udcf3'，
# 使用者那顆則做純字串的來回測試——兩者走的是同一條 surrogatepass 路徑。
FS_SURROGATE = '\udcf3'
USER_SURROGATE = '\udef3'


def main():
    c = Checker('含 surrogate 的檔名')
    with Sandbox() as sb:
        import analysis
        import common_utils as cu
        import moveToResize as mv
        analysis.trigger_move_script = lambda: None
        sb.sync(analysis, cu, mv)

        bad_name = f'[Vortalis{FS_SURROGATE}] Acheron{FS_SURROGATE} Set 1.zip'
        names = [bad_name, '[Normal] 正常的包.zip']
        for i, n in enumerate(names):
            make_png_zip(sb.src / n, seed=i)

        c.section('排行榜快取要寫得進去')
        out, _ = capture(analysis.main, auto_next=False)
        c.check('快取寫入失敗' not in out, '沒有出現「快取寫入失敗」')
        c.check(config.TARGETS_CACHE.exists(), '排行榜快取檔確實產生了')

        c.section('讀回來的標籤要與原始檔名位元一致')
        targets = cu.load_targets_cache(config.TARGETS_CACHE)
        tags = {t['tag'] for t in targets}
        c.check(len(targets) == 2, f'兩個包都上榜: {len(targets)}')
        c.check(f'Vortalis{FS_SURROGATE}' in tags, 'surrogate 原樣讀回，沒被吃掉也沒變成 ?')

        c.section('用這個標籤還要搬得動')
        feed_input(mv, ['n'])
        tag = f'Vortalis{FS_SURROGATE}'
        regex = re.compile('(' + r'[\(【\[\（]\s*' + re.escape(tag) + r'\s*[\)】\]\）]' + ')',
                           re.IGNORECASE)
        capture(mv.move_files_for_pattern, sb.src, sb.work,
                cu.load_processed_files(), cu.load_hash_cache(), regex, None, [tag])
        c.check((sb.work / bad_name).exists(), '含 surrogate 的包成功搬到工作區')
        c.check((sb.src / names[1]).exists(), '另一個包留在原地')

        c.section('其他快取檔也要能來回')
        cu.save_clean_file(bad_name, config.LOG_FILE)
        c.check(bad_name.lower() in cu.load_processed_files(config.LOG_FILE),
                '黑名單存回來的檔名與原檔一致（errors=ignore 時會被默默吃掉）')

        c.check(cu.add_manual_tags([bad_name], 'vortalis') == 1, '手動標籤寫得進對照表')
        cu.load_manual_tags(force=True)
        c.check(cu.extract_all_tags(bad_name) == ['vortalis'], '重讀後對照得回來')

        cu.save_lowgain_flags({'k': {'name': bad_name, 'ratio': 1.0,
                                     'sig': cu.settings_signature()}})
        c.check(cu.load_lowgain_flags().get('k', {}).get('name') == bad_name,
                '效率不足標記存得進去也讀得回來')

        cu.save_tag_rule({'separator': '-', 'field': 1, 'accepted': [f'A{FS_SURROGATE}']})
        cu.load_tag_rule(force=True)
        c.check(cu.load_tag_rule()['accepted'] == [f'A{FS_SURROGATE}'], '切割規則也能來回')

        c.section(f'回報裡那顆 {USER_SURROGATE!r} 直接來回')
        probe = sb.root / 'probe.json'
        payload = [{'tag': f'Vortalis{USER_SURROGATE}', 'sample': f'[V{USER_SURROGATE}] x.zip'}]
        try:
            with open(probe, 'w', **cu.TEXT_IO) as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            wrote = True
        except Exception as e:
            wrote = False
            print(f'    寫入失敗: {e}')
        c.check(wrote, '含 \\udef3 的資料寫得進 JSON（修好前就是這裡整批失敗）')
        if wrote:
            c.check(cu.load_targets_cache(probe) == payload, '讀回來與原始資料完全相同')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
