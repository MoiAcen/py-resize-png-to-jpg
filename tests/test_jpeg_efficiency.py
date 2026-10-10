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
        c.check(s['routes'] == {'A': 1, 'B': 2, 'skip': 1}, f"流程分流計數正確: {s['routes']}")
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
        out, got = capture(je.collect_samples, str(lib / 'ignored.png'), 1, 'random', 1, 0, work)
        c.check(got == [] and '不是 JPG' in out, '指到非 JPG 檔時說明並回傳空清單')

        # ---------- 9. 參數檢查 ----------
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
