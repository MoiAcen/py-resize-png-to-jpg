"""jpeg_efficiency.py：JPG 重壓效率研究工具。

重點：輸入唯讀、量到的就是 resize.py 的編碼結果、彙總的數學對、圖表不會畫出框外。
"""
import hashlib
import io
import re
import sys
import zipfile
from pathlib import Path

from _harness import Checker, Sandbox, capture, noisy_image
import config


def md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def save_jpg(im, path, q, sub, progressive=False):
    im.save(path, 'JPEG', quality=q, subsampling=sub, progressive=progressive)
    return Path(path)


def make_row(name, size, route, q_auto=92, ratio=0.5, results=None, lossless=None, quality=95):
    """手工造一筆分析結果，讓彙總的數學可以用手算的數字驗證。"""
    results = results or {}
    row = {'name': name, 'error': None, 'size': size, 'width': 100, 'height': 100,
           'est_quality': quality, 'subsampling': '4:2:0', 'progressive': False, 'bpp': 1.0,
           'action': 'recompress' if route == 'B' else ('lossless' if route == 'A' else 'skip'),
           'route': route, 'reasons': '', 'color_ratio': ratio,
           'kind': 'flat' if ratio < config.JPEG_FLAT_COLOR_RATIO else 'photo',
           'q_auto': q_auto, 'thumb': '', 'lossless_size': lossless,
           'lossless_identical': True if lossless else None,
           'results': {q: {'size': s, 'psnr': 40.0 + (95 - q) * 0.3} for q, s in results.items()}}
    return row


def main():
    c = Checker('JPG 重壓效率研究工具')
    with Sandbox() as sb:
        import jpeg_efficiency as je
        import resize
        from jpeg_inspector import inspect_jpeg, classify_jpeg
        from PIL import Image, ImageDraw

        # ---------- 準備一個小型的樣本庫 ----------
        lib = sb.root / 'library'
        lib.mkdir()
        photo = noisy_image(seed=1, w=480, h=360)
        flat = Image.new('RGB', (480, 360), (250, 240, 230))
        d = ImageDraw.Draw(flat)
        for i in range(8):
            d.ellipse([20 + i * 50, 40 + i * 20, 120 + i * 50, 140 + i * 20],
                      fill=(30 * i % 255, 200 - 20 * i, 90 + 15 * i), outline=(10, 10, 10), width=3)
        p_q95 = save_jpg(photo, lib / 'photo_q95.jpg', 95, 2)
        p_q99 = save_jpg(photo, lib / 'photo_q99_444.jpg', 99, 0)
        p_prog = save_jpg(photo, lib / 'photo_q90_prog.jpg', 90, 2, progressive=True)
        f_q95 = save_jpg(flat, lib / 'flat_q95.jpg', 95, 2)
        (lib / 'broken.jpg').write_bytes(b'\xff\xd8\xff\xe0' + bytes(range(256)) * 4)   # 壞檔
        photo.save(lib / 'ignored.png', 'PNG')                                          # 不是 JPG
        before = {p.name: md5(p) for p in lib.iterdir()}

        # ---------- 1. 完整跑一次 ----------
        c.section('完整跑一次：輸出檔案、壞檔不拖垮整批、輸入唯讀')
        out_dir = sb.root / 'out'
        out, rc = capture(je.main, [str(lib), '--min-kb', '0', '--samples', '50',
                                    '--workers', '2', '--qualities', '80,92', '--out', str(out_dir)])
        c.check(rc == 0, f'結束代碼 0（實際 {rc}）')
        html_path, csv_path = out_dir / 'jpeg_efficiency.html', out_dir / 'jpeg_efficiency.csv'
        c.check(html_path.exists() and csv_path.exists(), 'HTML 報告與 CSV 都產生了')
        c.check({p.name: md5(p) for p in lib.iterdir()} == before, '輸入資料夾裡每個檔案的內容與數量都沒變（唯讀）')
        c.check('ignored.png' in before and 'ignored.png' not in out, '非 JPG 不會被拿來測')
        c.check('broken.jpg' in out and '❌' in out, '壞檔有被回報出來，而且沒有讓整批中斷')

        import csv
        rows = {r['name']: r for r in csv.DictReader(open(csv_path, encoding='utf-8-sig'))}
        c.check(set(rows) == {'photo_q95.jpg', 'photo_q99_444.jpg', 'photo_q90_prog.jpg',
                              'flat_q95.jpg', 'broken.jpg'}, f'CSV 有五筆（含壞檔）: {sorted(rows)}')
        c.check(rows['broken.jpg']['error'] != '' and rows['photo_q95.jpg']['error'] == '',
                '壞檔的 error 欄有值，好檔沒有')
        c.check('Error' in rows['broken.jpg']['error'], f"error 欄裡是真正的錯誤訊息: {rows['broken.jpg']['error'][:40]!r}")
        c.check(rows['broken.jpg']['q80_bytes'] == '' and rows['broken.jpg']['size_bytes'] == '',
                '壞檔的其他欄位是空的（錯誤訊息沒有跑到別的欄去）')

        # ---------- 2. 量到的就是 resize.py 的編碼結果 ----------
        c.section('量到的數字 = resize.py 實際會產生的結果（不是另一套估算）')
        im = Image.open(p_q95).convert('RGB')
        c.check(int(rows['photo_q95.jpg']['q92_bytes']) == len(resize._encode(im, 92, 2)),
                'Q92 的位元組數與 resize._encode 逐位元組一致')
        c.check(int(rows['photo_q95.jpg']['q80_bytes']) == len(resize._encode(im, 80, 2)),
                'Q80 的位元組數也一致')
        c.check(int(rows['photo_q95.jpg']['lossless_bytes']) == len(resize._optimize_lossless(p_q95.read_bytes())),
                '無損最佳化的位元組數與 resize._optimize_lossless 一致')
        c.check(rows['photo_q95.jpg']['lossless_identical'] == '1', '無損結果經逐像素比對確實相同')
        for name, path in (('photo_q95.jpg', p_q95), ('photo_q99_444.jpg', p_q99),
                           ('photo_q90_prog.jpg', p_prog), ('flat_q95.jpg', f_q95)):
            want = je.pipeline_route(classify_jpeg(inspect_jpeg(path))['action']) or 'skip'
            c.check(rows[name]['route'] == want, f'{name}: 流程分流 = {want}（與 classify_jpeg 一致）')
        c.check(rows['photo_q90_prog.jpg']['route'] == 'skip', 'Q90 progressive 被正確判為跳過（不是需修正的圖）')
        c.check(rows['photo_q90_prog.jpg']['pipeline_savings'] == '0.0000', '跳過的圖流程省 0%')
        c.check(rows['flat_q95.jpg']['kind'] == 'flat' and rows['photo_q95.jpg']['kind'] == 'photo',
                '平塗圖判為 flat、雜訊照片判為 photo')

        # ---------- 3. 彙總的數學 ----------
        c.section('彙總的數學（手算的數字當標準答案）')
        r1 = make_row('a', 1000, 'B', results={80: 400, 92: 700})
        r2 = make_row('b', 1000, 'B', results={80: 300, 92: 1200})    # Q92 反而變大
        r3 = make_row('c', 500, 'A', lossless=450)
        r4 = make_row('d', 500, None)
        for r in (r1, r2, r3, r4):
            r['pipeline_size'] = je.pipeline_size_after(r)
        s = je.summarize([r1, r2, r3, r4], [80, 92])
        c.check(s['by_q'][80]['total_savings'] == 1 - (400 + 300) / 2000, 'Q80 整批縮減 = 65%')
        c.check(s['by_q'][92]['total_savings'] == 1 - (700 + 1000) / 2000,
                '變大的那張在整批合計裡保留原檔（與流程「變小才替換」一致）')
        c.check(s['by_q'][92]['not_smaller'] == 1, '「沒變小」的張數 = 1')
        c.check(s['by_q'][92]['min_savings'] == 1 - 1200 / 1000, '單張範圍的最小值保留負數，不被截成 0')
        c.check(s['routes'] == {'A': 1, 'B': 2, 'P': 0, 'skip': 1}, f"流程分流計數正確: {s['routes']}")
        c.check((r1['pipeline_size'], r2['pipeline_size'], r3['pipeline_size'], r4['pipeline_size'])
                == (700, 1000, 450, 500), 'B 取 q_auto 的結果、變大者保留原檔、A 取無損、跳過不動')

        # ---------- 4. 如果改門檻 ----------
        c.section('如果改平塗判定門檻（只算走 B 的檔案）')
        w = je.whatif_flat_threshold(
            [make_row('p', 1000, 'B', ratio=0.05, results={80: 400, 92: 700}),
             make_row('q', 1000, 'B', ratio=0.30, results={80: 500, 92: 800}),
             make_row('x', 1000, 'A', ratio=0.05, lossless=900)],     # A 不受門檻影響
            [0.10, 0.40], 80, 92)
        c.check([x['flat'] for x in w] == [1, 2], f'門檻 10% 時 1 張平塗，40% 時 2 張: {[x["flat"] for x in w]}')
        c.check(abs(w[0]['savings'] - (1 - (400 + 800) / 2000)) < 1e-9, '門檻 10%：p 走 Q80、q 走 Q92')
        c.check(abs(w[1]['savings'] - (1 - (400 + 500) / 2000)) < 1e-9, '門檻 40%：兩張都走 Q80')
        c.check(je.whatif_flat_threshold([make_row('a', 1, 'A', lossless=1)], [0.1], 80, 92) == [],
                '沒有任何走 B 的檔案時回空，不當機')

        # ---------- 5. 報告內容 ----------
        c.section('HTML 報告：圖都在、數字對得上、沒有壞掉的值')
        page = html_path.read_text(encoding='utf-8')
        c.check(page.count('<svg') == 5, f'五張圖都畫出來了（實際 {page.count("<svg")} 張）')
        ids = re.findall(r'<clipPath id="(\w+)"', page)
        c.check(len(ids) == len(set(ids)) == 2, f'裁切區的 id 不重複: {ids}')
        c.check('NaN' not in page and 'None' not in page and 'nan' not in page.lower().replace('nano', ''),
                '頁面裡沒有 NaN / None 這類壞掉的值')
        c.check('data:image/jpeg;base64,' in page, '有縮圖')
        c.check(je.summarize([], [80])['n'] == 0, '空輸入的彙總不會當機')
        c.check('JPG 重壓效率研究' in page and '平塗' in page and '寫實' in page, '標題與內容判定都在')

        # ---------- 6. 圖表範圍：重壓後變大的檔案不能畫到框外 ----------
        c.section('座標範圍包含負值（重壓後反而變大的檔案）')
        grow = [make_row('g', 1000, 'B', results={80: 400, 92: 1300, 95: 1500})]
        grow[0]['pipeline_size'] = 1000
        sm = je.summarize(grow, [80, 92, 95])
        svg1 = je.chart_savings_vs_quality(grow, sm, [80, 92, 95])
        svg2 = je.chart_tradeoff(grow, sm, [80, 92, 95])
        c.check(re.search(r'>-\d+%<', svg1) is not None, '圖①的縱軸有負的刻度（−50% 之類）')
        c.check(re.search(r'>-\d+%<', svg2) is not None, '圖②的橫軸有負的刻度')
        c.check('clip-path="url(#clip1)"' in svg1 and 'clip-path="url(#clip2)"' in svg2,
                '資料線有裁切，萬一超出也不會跑到刻度上')

        # ---------- 7. --no-psnr ----------
        c.section('--no-psnr：快速模式')
        out2_dir = sb.root / 'out2'
        out, rc = capture(je.main, [str(p_q95), '--workers', '1', '--qualities', '85,92',
                                    '--no-psnr', '--out', str(out2_dir)])
        c.check(rc == 0, '單一檔案、不算 PSNR 也能跑完')
        page2 = (out2_dir / 'jpeg_efficiency.html').read_text(encoding='utf-8')
        c.check(page2.count('<svg') == 4, f'沒有 PSNR 就少畫「畫質代價」那張圖（{page2.count("<svg")} 張）')
        c.check('流程實際結果' in out and 'photo_q95.jpg' in out, '單一檔案時終端機會印出逐項明細')

        # ---------- 8. 取樣 ----------
        c.section('取樣：zip / 資料夾 / 單檔、largest / random / 門檻')
        zpath = sb.root / 'pack.zip'
        with zipfile.ZipFile(zpath, 'w') as zf:
            for p in (p_q95, p_q99, p_prog, f_q95):
                zf.write(p, f'sub/{p.name}')
            zf.writestr('readme.txt', 'x')
        zbefore = md5(zpath)
        work = sb.root / 'zwork'
        work.mkdir()
        got = je.collect_samples(str(zpath), 2, 'largest', 1, 0, work)
        names = sorted(Path(n).name for n, _ in got)
        c.check(names == sorted(['photo_q99_444.jpg', 'photo_q95.jpg']), f'largest 挑到最大的兩張: {names}')
        c.check(len(list(work.iterdir())) == 2, '只解出被挑中的成員，沒有把整包解開')
        c.check(md5(zpath) == zbefore, 'zip 原檔沒被動到')
        a = [n for n, _ in je.collect_samples(str(zpath), 2, 'random', 7, 0, work)]
        b = [n for n, _ in je.collect_samples(str(zpath), 2, 'random', 7, 0, work)]
        c.check(a == b, '同一個隨機種子，抽到同一批（結果可重現）')
        c.check(len(je.collect_samples(str(lib), 100, 'random', 1, 0, work)) == 5,
                '資料夾：5 個 .jpg（含壞檔），png 不算')
        out, got = capture(je.collect_samples, str(lib), 100, 'random', 1, 10_000, work)
        c.check(got == [] and '沒有 >= 10000 KB' in out, '--min-kb 把小檔案濾掉，並說明原因')
        c.check(len(je.collect_samples(str(p_q95), 1, 'random', 1, 10_000, work)) == 1,
                '指名的單一檔案不套用大小門檻')
        out, got = capture(je.collect_samples, str(sb.root / 'nope'), 1, 'random', 1, 0, work)
        c.check(got == [] and '找不到輸入' in out, '輸入不存在時說明並回傳空清單')
        (lib / 'notes.txt').write_text('x')
        out, got = capture(je.collect_samples, str(lib / 'notes.txt'), 1, 'random', 1, 0, work)
        c.check(got == [] and '不是 JPG / PNG' in out, '指到不是圖片的檔案時說明並回傳空清單')
        (lib / 'notes.txt').unlink()

        # ---------- 9. rar / 7z（透過 7-Zip）----------
        import common_utils as cu
        import subprocess
        from _rar import make_rar5
        c.section('rar / 7z：用 7-Zip 列清單、只解出被挑中的 JPG')
        if not cu.SEVEN_ZIP_PATH:
            print('  (本機沒有 7-Zip，跳過這一組)')
        else:
            payloads = {
                'sub/photo_big.jpg': p_q99.read_bytes(),
                'photo_small.jpg': f_q95.read_bytes(),
                '-dash name.jpg': p_q95.read_bytes(),             # 開頭是 - ，不加 -- 會被當成參數
                '角色 [Patreon] 01.jpg': p_prog.read_bytes(),      # 中文 + 方括號 + 空白
                'note.txt': b'not an image' * 20,                  # 不是 JPG
            }
            rar = make_rar5(sb.root / 'pack.rar',
                            [('emptydir/', b'')] + list(payloads.items()))      # 含資料夾項目
            rar_before = md5(rar)

            listing = cu.list_7z_entries(rar)
            c.check(listing is not None and not listing['solid'], '7-Zip 讀得懂這個 rar（Rar5，非 solid）')
            names = {n for n, _s in listing['entries']}
            c.check(names == set(payloads), f'清單只含檔案、不含資料夾項目: {sorted(names)}')
            c.check(dict(listing['entries']) == {k: len(v) for k, v in payloads.items()},
                    '每個成員的大小與原始內容一致')

            work2 = sb.root / 'rwork'
            work2.mkdir()
            got = je.collect_samples(str(rar), 100, 'random', 1, 0, work2)
            c.check(sorted(n for n, _ in got) == sorted(k for k in payloads if k.endswith('.jpg')),
                    '四張 JPG 都挑到，note.txt 不算')
            same = all(md5(path) == hashlib.md5(payloads[n]).hexdigest() for n, path in got)
            c.check(same, '解出來的每個 JPG 與原始位元組完全相同（含中文、方括號、開頭是 - 的檔名）')
            c.check(any('/' in n for n, _ in got) and (work2 / 'sub').is_dir(),
                    '壓縮包內的子資料夾結構有保留')
            c.check(md5(rar) == rar_before, 'rar 原檔沒被動到')

            work3 = sb.root / 'rwork2'
            work3.mkdir()
            got = je.collect_samples(str(rar), 2, 'largest', 1, 0, work3)
            c.check(sorted(n for n, _ in got) == sorted(['sub/photo_big.jpg', '-dash name.jpg']),
                    f'largest 挑到最大的 2 張: {[n for n, _ in got]}')
            extracted = [f for f in work3.rglob('*') if f.is_file()]
            c.check(len(extracted) == 2, f'只解出被挑中的 2 張，不是整包（解出 {len(extracted)} 個）')

            out, got = capture(je.collect_samples, str(rar), 100, 'random', 1, 10_000, sb.root / 'rwork3')
            c.check(got == [] and '沒有 >= 10000 KB' in out, '--min-kb 對 rar 也有效')

            out, rc = capture(je.main, [str(rar), '--min-kb', '0', '--samples', '10', '--workers', '2',
                                        '--qualities', '80,92', '--out', str(sb.root / 'rout')])
            c.check(rc == 0, '整個流程從 rar 一路跑到出報告')
            rrows = {r['name']: r for r in csv.DictReader(
                open(sb.root / 'rout' / 'jpeg_efficiency.csv', encoding='utf-8-sig'))}
            c.check(set(rrows) == {'sub/photo_big.jpg', 'photo_small.jpg', '-dash name.jpg',
                                   '角色 [Patreon] 01.jpg'}, f'CSV 的檔名保留壓縮包內的路徑: {sorted(rrows)}')
            c.check(all(r['error'] == '' for r in rrows.values()), '四張都分析成功')
            c.check(md5(rar) == rar_before, '跑完整個流程後 rar 仍然沒被動到')

            # .7z 也走同一條路徑；7z 預設就是 solid，要提示會比較慢
            z7 = sb.root / 'pack.7z'
            subprocess.run([cu.SEVEN_ZIP_PATH, 'a', str(z7), str(p_q95), str(f_q95)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out, got = capture(je.collect_samples, str(z7), 10, 'random', 1, 0, sb.root / 'zwork7')
            c.check(len(got) == 2 and all(path.read_bytes() == (p_q95 if n == p_q95.name else f_q95).read_bytes()
                                          for n, path in got), '.7z 也能挑、能解，位元組一致')
            c.check('solid' in out, 'solid 壓縮包會提示解指定成員比較慢')

            bad = sb.root / 'garbage.rar'
            bad.write_bytes(b'this is not a rar' * 50)
            out, got = capture(je.collect_samples, str(bad), 5, 'random', 1, 0, sb.root / 'bw')
            c.check(got == [] and '打不開' in out, '損毀的 rar：說明原因並回傳空清單，不當機')

        c.section('沒有 7-Zip 時：說清楚怎麼辦')
        # 用一個確實存在的檔案：不存在的話會先被「找不到輸入」攔下，測不到 7-Zip 缺失的分支
        stub_rar = sb.root / 'stub.rar'
        stub_rar.write_bytes(b'placeholder')
        saved = je.SEVEN_ZIP_PATH
        je.SEVEN_ZIP_PATH = None
        try:
            out, got = capture(je.collect_samples, str(stub_rar), 5, 'random', 1, 0, sb.root / 'nw')
        finally:
            je.SEVEN_ZIP_PATH = saved
        c.check(got == [] and '需要 7-Zip' in out and 'POSSIBLE_7Z_PATHS' in out,
                '找不到 7-Zip：說明原因，並指出要在哪裡設定路徑')

        c.section('分批與路徑小工具（純函式）')
        items = [f'file_{i:03d}.jpg' for i in range(50)]
        batches = list(je._chunks_by_chars(items, limit=100))
        c.check([x for b in batches for x in b] == items, '分批後順序不變、一個不漏、一個不重')
        c.check(all(sum(len(x) + 1 for x in b) <= 100 for b in batches) and len(batches) > 1,
                '每批的總長度都不超過上限，且確實分成多批')
        c.check(list(je._chunks_by_chars(['x' * 500], limit=100)) == [['x' * 500]],
                '單一名稱超過上限時自成一批，不會卡住')
        c.check(list(je._chunks_by_chars([], limit=100)) == [], '空清單不會出問題')
        wd = sb.root / 'wd'
        c.check(je._member_disk_path(wd, 'a/b/c.jpg') == wd / 'a' / 'b' / 'c.jpg', 'Unix 分隔符')
        c.check(je._member_disk_path(wd, 'a\\b\\c.jpg') == wd / 'a' / 'b' / 'c.jpg',
                'Windows 分隔符也能正確拆開')

        # ---------- 9b. PNG 轉 JPG 模式 ----------
        c.section('PNG 模式：量的是正式轉檔的結果，對 PNG 原圖比對')
        from quality_test import worst_tile_psnr, psnr as qpsnr
        import quality_test as qt
        import zipfile as zf_mod
        pdir = sb.root / 'pngs'
        pdir.mkdir()
        photo.save(pdir / 'photo.png', 'PNG')
        flat.save(pdir / 'flat.png', 'PNG')
        Image.new('RGB', (40, 40), (0, 0, 0)).save(pdir / 'tiny_black.png', 'PNG')   # 極小，轉 JPG 不會更小
        noisy_image(seed=9, w=24, h=24).save(pdir / 'tiny_noise.png', 'PNG')
        pbefore = {p.name: md5(p) for p in pdir.iterdir()}
        pout = sb.root / 'pout'
        out, rc = capture(je.main, [str(pdir), '--type', 'png', '--samples', '20', '--workers', '2',
                                    '--qualities', '70,80,95', '--out', str(pout)])
        c.check(rc == 0, f'--type png 跑得完（代碼 {rc}）')
        c.check({p.name: md5(p) for p in pdir.iterdir()} == pbefore, 'PNG 輸入唯讀：內容與數量都沒變')
        prow = {r['name']: r for r in csv.DictReader(open(pout / 'jpeg_efficiency.csv', encoding='utf-8-sig'))}
        c.check(set(prow) == {'photo.png', 'flat.png', 'tiny_black.png', 'tiny_noise.png'}, f'四張 PNG 都在 CSV: {sorted(prow)}')
        c.check(all(r['route'] == 'P' and r['action'] == 'png_convert' for r in prow.values()), '分流欄標成 P（PNG 轉檔）')
        c.check('q80_worst_tile_psnr' in next(iter(prow.values())), 'CSV 有「最差區塊 PSNR」欄')

        # 與正式轉檔逐位元組相同：直接呼叫 worker 轉一份副本，再與工具量到的大小比
        wcopy = sb.root / 'wcopy'
        wcopy.mkdir()
        photo.save(wcopy / 'photo.png', 'PNG')
        ok_conv, _ = resize.convert_single_image_worker((str(wcopy / 'photo.png'), config.QUALITY))
        real = (wcopy / 'photo.jpg').read_bytes()
        c.check(ok_conv and int(prow['photo.png'][f'q{config.QUALITY}_bytes']) == len(real),
                f'工具量到的 Q{config.QUALITY} 大小 = 正式轉檔實際產出的大小（{len(real)} 位元組）')
        with Image.open(pdir / 'photo.png') as ref_im, Image.open(io.BytesIO(real)) as got_im:
            want_psnr = qpsnr(ref_im.convert('RGB'), got_im.convert('RGB'))
        c.check(abs(float(prow['photo.png'][f'q{config.QUALITY}_psnr']) - want_psnr) < 0.01,
                'PSNR 是對 PNG 原圖算的（與獨立重算一致）')
        c.check(float(prow['photo.png']['q95_psnr']) > float(prow['photo.png']['q70_psnr']), '品質越高 PSNR 越高')
        c.check(float(prow['photo.png']['q80_worst_tile_psnr']) <= float(prow['photo.png']['q80_psnr']) + 1e-6,
                '最差區塊 PSNR 不會比整張平均還好')
        c.check(all(int(r['pipeline_bytes']) <= int(r['size_bytes']) for r in prow.values()),
                '流程結果不會比 PNG 原檔大（轉出更大就保留原檔）')
        c.check(any(int(r['pipeline_bytes']) == int(r['size_bytes']) for r in prow.values()),
                '至少一張「轉成 JPG 反而沒變小」的圖被正確算成保留原檔')
        page = (pout / 'jpeg_efficiency.html').read_text(encoding='utf-8')
        c.check('PNG 轉 JPG 畫質研究' in page and '來源品質決定' not in page and '平塗／寫實分流' not in page,
                'HTML 報告是 PNG 版：沒有與 PNG 無關的「來源品質／分流」章節')
        c.check('最差區塊' in page, 'HTML 表格有最差區塊欄')
        c.check(re.search(r'<svg', page) is not None, 'HTML 圖表有畫出來')

        # 預設只取 JPG；--type png 才取 PNG；壓縮包同理
        both = sb.root / 'both'
        both.mkdir()
        photo.save(both / 'p.png', 'PNG')
        save_jpg(photo, both / 'j.jpg', 95, 2)
        got = je.collect_samples(str(both), 9, 'largest', 1, 0, work)
        c.check([n for n, _ in got] == ['j.jpg'], '資料夾預設只取 JPG')
        got = je.collect_samples(str(both), 9, 'largest', 1, 0, work, je.PNG_EXTENSIONS)
        c.check([n for n, _ in got] == ['p.png'], '資料夾 --type png 只取 PNG')
        pz = sb.root / 'pngs.zip'
        with zf_mod.ZipFile(pz, 'w') as z:
            z.write(pdir / 'photo.png', 'a/photo.png')
            z.write(pdir / 'flat.png', 'a/flat.png')
        out, got = capture(je.collect_samples, str(pz), 5, 'largest', 1, 0, work)
        c.check(got == [] and '沒有' in out and 'JPG' in out, 'zip 預設找不到 JPG 時有說明')
        got = je.collect_samples(str(pz), 5, 'largest', 1, 0, work, je.PNG_EXTENSIONS)
        c.check(sorted(n for n, _ in got) == ['a/flat.png', 'a/photo.png'], 'zip --type png 取出 PNG')
        out, got = capture(je.collect_samples, str(pdir / 'photo.png'), 1, 'random', 1, 10_000, work)
        c.check(len(got) == 1, '指名單一 PNG 直接接受（不套用大小門檻）')
        out, rc = capture(je.main, [str(pdir / 'photo.png'), '--out', str(sb.root / 'o_single')])
        c.check(rc == 0 and 'PNG 轉 JPG' in out and '最差區塊' in out, '單一 PNG 不加 --type 也會走 PNG 模式')

        # ---------- 最差區塊 PSNR ----------
        c.section('最差區塊 PSNR：抓得到局部損傷，整張平均抓不到')
        base = noisy_image(seed=3, w=256, h=256)
        same = base.copy()
        c.check(worst_tile_psnr(base, same)[0] == 99.0, '完全相同 → 99 dB')
        dmg = base.copy()
        ImageDraw.Draw(dmg).rectangle([128, 64, 191, 127], fill=(255, 255, 255))   # 剛好是一個 64px 區塊
        wt, tx, ty = worst_tile_psnr(base, dmg)
        c.check((tx, ty) == (128, 64), f'損傷位置抓對：({tx},{ty})')
        c.check(wt < qpsnr(base, dmg) - 5, f'最差區塊 {wt:.1f} dB 遠低於整張 {qpsnr(base, dmg):.1f} dB')
        small = Image.new('RGB', (30, 20), (10, 10, 10))
        c.check(worst_tile_psnr(small, small.copy())[0] == 99.0, '圖比區塊還小也不會出錯')

        # quality_test.py 的 PNG 支援
        c.section('quality_test.py 支援 PNG')
        c.check(len(qt.collect_samples(str(pdir / 'photo.png'), 1, work)) == 1, '單一 PNG 可以收')
        got = qt.collect_samples(str(pdir), 9, work)
        c.check(sorted(n for n, _ in got) == ['flat.png', 'photo.png', 'tiny_black.png', 'tiny_noise.png'],
                '資料夾裡的 PNG 會被收進來')
        got = qt.collect_samples(str(pz), 9, work)
        c.check(len(got) == 2, 'zip 裡的 PNG 會被收進來')
        qout = sb.root / 'qout'
        qout.mkdir()
        rep = []
        out, _ = capture(qt.run_one, 'photo.png', pdir / 'photo.png', [80, 95], qout, rep)
        c.check('PNG 轉 JPG' in out and '最差區塊' in out and '目前流程用這個' in out, 'PNG 的說明、最差區塊欄、目前品質標記都有')
        c.check((qout / 'photo_q80.jpg').read_bytes() == real, 'quality_test 產出的 Q80 與正式轉檔逐位元組相同')
        c.check((qout / 'photo_crop.png').exists() and (qout / 'photo_diff_q80.png').exists(), '最劣區域對照與差異圖都產生了')

        # ---------- 10. 參數檢查 ----------
        c.section('參數檢查')
        out, rc = capture(je.main, [str(p_q95), '--qualities', 'abc', '--out', str(sb.root / 'o3')])
        c.check(rc == 1 and '格式錯誤' in out, '品質格式錯誤會擋下來')
        out, rc = capture(je.main, [str(p_q95), '--qualities', '0,200', '--out', str(sb.root / 'o4')])
        c.check(rc == 1 and '1~100' in out, '品質超出 1~100 會擋下來')
        out, rc = capture(je.main, [str(sb.root / 'nope'), '--out', str(sb.root / 'o5')])
        c.check(rc == 1, '輸入不存在時結束代碼為 1')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
