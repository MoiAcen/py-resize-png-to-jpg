"""標籤解析規則：雜訊過濾、斜線拆解、手動標籤、切割規則。"""
import sys
import tempfile
from pathlib import Path

from _harness import Checker, Sandbox
import config


def main():
    c = Checker('標籤解析規則')
    with Sandbox() as sb:
        config.IGNORED_TAG_KEYS = set(config.IGNORED_TAG_KEYS) | {'ABC'}
        import common_utils as cu
        cu.TAG_RULE_FILE = config.TAG_RULE_FILE
        cu.MANUAL_TAG_FILE = config.MANUAL_TAG_FILE
        cu._IGNORED_TAG_KEYS_LOWER = {k.lower() for k in config.IGNORED_TAG_KEYS}
        cu.load_manual_tags(force=True)
        cu.load_tag_rule(force=True)

        c.section('雜訊關鍵字與樣式')
        noise = ['Patreon', 'Uncensored', 'Decensored', 'Extra', 'V',
                 'Part 1', 'Part 2', 'Part.03', 'Part-10', 'PART  7',
                 '1', '23', '100', '1234', '2020', '9999', '１２３']
        keep = ['Kurohime', '倉崎楓子', '45088166', '12345', '10000',
                'Parts', 'Part', 'Particle2', 'Extra Kurohime', '3D', 'A1']
        c.check(all(cu.is_noise_tag(t) for t in noise),
                f'雜訊判定正確: {[t for t in noise if not cu.is_noise_tag(t)] or "全中"}')
        c.check(not any(cu.is_noise_tag(t) for t in keep),
                f'不該誤傷的都留著: {[t for t in keep if cu.is_noise_tag(t)] or "全留"}')

        c.section('一個括號可能裝多個標籤（斜線拆解）')
        c.check(cu.extract_all_tags('[ABC][XYZ] aaa.zip') == ['XYZ'],
                '[ABC][XYZ] → 只留 XYZ（被排除的不連累整個檔案）')
        c.check(cu.extract_all_tags('[Kurohime ⧸ Uncensored] x.zip') == ['Kurohime'],
                '[Kurohime ⧸ Uncensored] → 拆開後只留 Kurohime')
        c.check(cu.extract_all_tags('[Decensored ⧸ Uncensored] x.zip') == [],
                '[Decensored ⧸ Uncensored] → 兩段都是雜訊，全濾掉')
        c.check(cu.extract_all_tags('[Kurohime] x [kurohime] y.zip') == ['Kurohime'],
                '同一檔名重複的標籤只算一次')
        c.check(cu.extract_all_tags('[45088166][Kurohime] 倉崎楓子 [AI Generated].zip')
                == ['45088166', 'Kurohime'], '長數字作品 ID 與作者名都保留')

        c.section('標籤全被排除 → 空清單（由 analysis 決定怎麼處理）')
        for name in ['[ABC] bbb.zip', '[Part 2] ccc.zip', '沒有括號.zip']:
            c.check(cu.extract_all_tags(name) == [], f'{name} → []')

        c.section('手動標籤：取代檔名推出來的標籤')
        names = ['[tako123] aa.zip', '[tako] aa.zip', 'tako aaa.zip']
        c.check(cu.add_manual_tags(names, 'tako') == 3, '三個檔案都掛上標籤')
        c.check(all(cu.extract_all_tags(n) == ['tako'] for n in names),
                '三個都收攏成同一個 tako（[tako123] 不再自成一格）')
        c.check(cu.extract_all_tags('[Kurohime] 別人的包.zip') == ['Kurohime'],
                '沒被標記的包不受影響')
        c.check(cu.add_manual_tags(names, 'tako') == 0, '重複標記是 no-op')

        c.section('切割規則：只認採用過的標籤')
        c.check(cu.rule_tag_from_name('ABC-BBB-CC.zip', '-', 1) == 'ABC', '切出第一段 ABC')
        c.check(cu.rule_tag_from_name('ABC-BBB-CC.zip', '-', 2) == 'BBB', '可以指定第二段')
        c.check(cu.rule_tag_from_name('沒有分隔符.zip', '-', 1) == '',
                '檔名裡沒有分隔符就不套用（否則每個檔案都自成一個標籤）')
        c.check(cu.rule_tag_from_name('ABC-BBB.zip', '-', 9) == '', '段數不夠就回空字串')

        cu.save_tag_rule({'separator': '-', 'field': 1, 'accepted': ['ABC']})
        cu.load_tag_rule(force=True)
        c.check(cu.extract_all_tags('ABC-BBB-CC.zip') == ['ABC'], '採用過的標籤會自動歸位')
        c.check(cu.extract_all_tags('ABC-DDD.zip') == ['ABC'], '新檔案切出相同標籤也自動歸位')
        c.check(cu.extract_all_tags('2024-01-05 某活動.zip') == [],
                '沒採用過的（日期那種）維持無標籤')
        c.check(cu.extract_all_tags('[Kurohime] x.zip') == ['Kurohime'],
                '本來就有標籤的不會被規則覆蓋')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
