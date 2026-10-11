"""PNG 是不是從 JPEG 解碼來的？（獨立執行，輸入檔案唯讀）

原理：JPEG 解碼出來的圖，用「同一個品質、同一個色度抽樣、同一條 8x8 格線」重新編碼，
幾乎不會再損失任何東西（PSNR 會異常地高）；乾淨的 PNG 沒有這種現象。
所以掃過各品質，看哪一個品質的 PSNR 特別突出，就能反推來源的 JPEG 品質。

偵測到之後的用法：用「偵測到的品質」轉，而不是固定 Q80——不會二次傷害畫質，
檔案又只比 Q80 大一點（對比之下，直接排除就是整張 PNG 原封不動留著）。

限制（請務必知道）：
  * 圖被縮放過就偵測不到（格線整個打散了）；被裁切過只要位移還在 8x8 內可以找回來。
  * 只認 libjpeg 標準量化表（Pillow / 大多數工具）；Photoshop 等自訂量化表可能偵測不到。
  * 偵測不到 ≠ 乾淨：只代表沒有「可驗證的」JPEG 痕跡。
  * 位移不是 (0,0) 時，整張轉檔會對不齊格線，偵測到的品質就沒有「不傷畫質」的保證。
  * 品質只看亮度，很可靠；4:2:0 / 4:4:4 是推測的，色彩邊緣銳利的圖會傾向判成 4:4:4（保守：檔案較大，但不傷畫質）。

用法：
    python jpeg_origin.py <輸入> [--samples 20] [--pick random|largest] [--out 資料夾]
<輸入>：單一 .png / 資料夾 / .zip / .rar / .7z（資料夾與壓縮包只取 PNG）
"""
import argparse
import csv
import io
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageStat

from config import BASE_DIR, QUALITY
from common_utils import clean_str
from quality_test import psnr, worst_tile_psnr, human
import jpeg_efficiency as je
import resize

PROBE = 256                       # 偵測用的裁切邊長（8 的倍數，才不會自己破壞格線）
SCAN_Q = range(50, 99)            # 掃描的品質範圍
COARSE_Q = (70, 75, 80, 85, 90, 95)   # 找位移時先用少數幾個品質粗掃
SUBSAMPLINGS = (2, 0)             # 4:2:0 與 4:4:4
FOUND_GAP_DB = 4.0                # 最高點比相鄰品質高出這麼多 dB 才算偵測到
FOUND_MIN_PSNR = 50.0             # 而且絕對值要夠高（同品質重壓幾乎無損）
FEATURELESS_PSNR = 80.0           # 高到這個程度代表圖幾乎沒有紋理（純漸層），任何品質都無損，曲線沒有資訊


def _enc(im, q, sub):
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=q, subsampling=sub)
    buf.seek(0)
    with Image.open(buf) as v:
        return v.convert(im.mode)


def pick_probe_box(im, edge=PROBE):
    """挑一塊「有紋理」的區域：全平坦的區塊不管怎麼壓都幾乎無損，會造成誤判。

    起點與邊長都取 16 的倍數：4:2:0 的色度格線是 16x16，這樣位移搜尋才有一致的基準。
    """
    w, h = im.size
    edge = min(edge, ((w - 16) // 16) * 16, ((h - 16) // 16) * 16)
    if edge < 64:
        return None
    best, best_box = -1.0, None
    gray = im.convert('L')
    for fy in (0.15, 0.5, 0.85):
        for fx in (0.15, 0.5, 0.85):
            x = (int((w - edge - 16) * fx) // 16) * 16
            y = (int((h - edge - 16) * fy) // 16) * 16
            sd = ImageStat.Stat(gray.crop((x, y, x + edge, y + edge))).stddev[0]
            if sd > best:
                best, best_box = sd, (x, y, edge)
    return best_box


def _curve(crop, sub, qs):
    return {q: psnr(crop, _enc(crop, q, sub)) for q in qs}


def _peak(res):
    """回傳 (最佳品質, 該品質 PSNR, 落差 dB)；落差 = 相對相鄰品質平均。"""
    best = max(res, key=res.get)
    nb = [res[x] for x in (best - 2, best - 1, best + 1, best + 2) if x in res]
    gap = res[best] - (sum(nb) / len(nb) if nb else res[best])
    return best, res[best], gap


def _luma_grid_offset(im, x0, y0, edge):
    """先只看亮度：在 8x8 的 64 種位移裡，找重壓 PSNR 最高的那一個（與色度抽樣無關）。"""
    gray = im.convert('L')
    top_off, top_val = (0, 0), -1.0
    for oy in range(8):
        for ox in range(8):
            crop = gray.crop((x0 + ox, y0 + oy, x0 + ox + edge, y0 + oy + edge))
            val = max(psnr(crop, _enc(crop, q, 0)) for q in COARSE_Q)
            if val > top_val:
                top_off, top_val = (ox, oy), val
    return top_off


def detect_jpeg_origin(im):
    """回傳 dict：found, quality, subsampling(2=4:2:0 / 0=4:4:4 / 找不到時 None), offset(x, y), peak_psnr, gap, reason。

    品質只看亮度（灰階）：亮度的量化表與色度抽樣無關，而且不受色度上取樣的誤差干擾——
    顏色邊緣銳利的圖（4:2:0 重壓本來就不是冪等的）用整張 RGB 去比會漏掉。
    抽樣方式與色度格線位移，確定「有 JPEG 痕跡」之後才用 RGB 去分辨。
    offset 是 JPEG 的格線相對圖左上角的位移；(0, 0) = 完全對齊。
    """
    box = pick_probe_box(im)
    if box is None:
        return {'found': False, 'reason': '圖太小，無法偵測', 'quality': None, 'subsampling': None,
                'offset': (0, 0), 'peak_psnr': 0.0, 'gap': 0.0}
    x0, y0, edge = box
    gx, gy = _luma_grid_offset(im, x0, y0, edge)
    gray = im.convert('L').crop((x0 + gx, y0 + gy, x0 + gx + edge, y0 + gy + edge))
    q, peak, gap = _peak(_curve(gray, 0, SCAN_Q))
    det = {'quality': q, 'subsampling': None, 'offset': (gx, gy), 'peak_psnr': peak, 'gap': gap}
    edge_q = q <= SCAN_Q.start or q >= SCAN_Q.stop - 1      # 峰值頂在掃描範圍邊緣 = 單調曲線，不是尖峰
    if peak >= FEATURELESS_PSNR:
        det.update(found=False, reason='圖幾乎沒有紋理（平滑漸層），無法判斷')
    elif edge_q:
        det.update(found=False, reason='沒有尖峰（曲線單調）')
    elif gap >= FOUND_GAP_DB and peak >= FOUND_MIN_PSNR:
        det.update(found=True, reason='')
    else:
        det.update(found=False, reason='沒有可驗證的 JPEG 痕跡')
    if not det['found']:
        return det

    # 確定是 JPEG 之後：用 RGB 分辨 4:4:4 / 4:2:0，以及 4:2:0 的色度格線位移（16x16，亮度位移的 mod 16 有四種可能）
    best = None
    for sub in SUBSAMPLINGS:
        cands = [(gx, gy)] if sub == 0 else [(gx + dx, gy + dy) for dy in (0, 8) for dx in (0, 8)]
        for ox, oy in cands:
            crop = im.crop((x0 + ox, y0 + oy, x0 + ox + edge, y0 + oy + edge))
            val = psnr(crop, _enc(crop, q, sub))
            if best is None or val > best[0]:
                best = (val, sub, (ox % 16, oy % 16) if sub == 2 else (ox, oy))
    det.update(subsampling=best[1], offset=best[2])
    return det


# ================= 偵測到之後：用偵測到的品質轉，會怎樣 =================
def _convert(im, q, sub):
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=q, subsampling=sub, optimize=True)
    return buf.getvalue()


def analyze_one(args):
    """單張 PNG：偵測 + 用「偵測到的品質」與「目前固定品質」各轉一次的比較。"""
    path_str, display = args
    row = {'name': display, 'error': None}
    try:
        path = Path(path_str)
        size = path.stat().st_size
        with Image.open(path) as opened:
            im = resize.to_rgb(opened)
            im.load()
        row.update({'size': size, 'width': im.size[0], 'height': im.size[1]})
        det = detect_jpeg_origin(im)
        row.update({k: det.get(k) for k in ('found', 'quality', 'subsampling', 'offset', 'peak_psnr', 'gap', 'reason')})
        aligned = bool(det.get('found')) and tuple(det['offset']) == (0, 0)
        row['aligned'] = aligned

        def measure(q, sub):
            data = _convert(im, q, sub)
            with Image.open(io.BytesIO(data)) as v:
                back = v.convert('RGB')
            return {'size': len(data), 'psnr': psnr(im, back), 'tile': worst_tile_psnr(im, back)[0]}
        row['now'] = measure(QUALITY, 2)                      # 目前流程：固定 QUALITY、Pillow 預設 4:2:0
        row['detected'] = measure(det['quality'], det['subsampling']) if aligned else None
        return row
    except Exception as e:
        row['error'] = f"{type(e).__name__}: {e}"
        return row


def print_report(rows):
    ok = [r for r in rows if not r.get('error')]
    print('\n' + '=' * 78)
    print(f"📊 PNG {len(ok)} 張" + (f"（{len(rows) - len(ok)} 張讀取失敗）" if len(rows) != len(ok) else ''))
    print('=' * 78)
    for r in ok:
        print(f"\n■ {clean_str(r['name'])}   {r['width']}x{r['height']}  {human(r['size'])}")
        if not r['found']:
            print(f"   偵測: 沒找到（{r['reason']}；最高點 Q{r['quality']}，PSNR {r['peak_psnr']:.1f} dB，落差 {r['gap']:.1f} dB）")
            n = r['now']
            print(f"   目前流程 Q{QUALITY}: {human(n['size'])}（省 {(1 - n['size'] / r['size']) * 100:.1f}%），"
                  f"對 PNG 整張 {n['psnr']:.1f} dB／最差區塊 {n['tile']:.1f} dB")
            continue
        sub = '4:2:0' if r['subsampling'] == 2 else '4:4:4'
        ox, oy = r['offset']
        grid = '格線對齊' if r['aligned'] else f"⚠ 格線位移 ({ox},{oy})，整張轉檔會對不齊，沒有「不傷畫質」的保證"
        print(f"   偵測: ✔ 來源是 JPEG，約 Q{r['quality']} {sub}（PSNR {r['peak_psnr']:.1f} dB，落差 {r['gap']:.1f} dB），{grid}")
        n, d = r['now'], r['detected']
        print(f"   {'做法':<22}{'大小':>10}{'省':>8}{'整張':>10}{'最差區塊':>10}")
        print(f"   {'目前流程 Q' + str(QUALITY):<22}{human(n['size']):>10}{(1 - n['size'] / r['size']) * 100:>7.1f}%"
              f"{n['psnr']:>8.1f}dB{n['tile']:>8.1f}dB")
        if d:
            print(f"   {'用偵測到的 Q' + str(r['quality']) + ' ' + sub:<22}{human(d['size']):>10}{(1 - d['size'] / r['size']) * 100:>7.1f}%"
                  f"{d['psnr']:>8.1f}dB{d['tile']:>8.1f}dB")
    found = [r for r in ok if r['found']]
    aligned = [r for r in found if r['aligned']]
    print('\n' + '-' * 78)
    print(f"偵測到 JPEG 來源: {len(found)} / {len(ok)} 張（其中格線對齊、可無損式重壓: {len(aligned)} 張）")
    if aligned:
        d_total = sum(r['detected']['size'] for r in aligned)
        n_total = sum(r['now']['size'] for r in aligned)
        o_total = sum(r['size'] for r in aligned)
        print(f"這 {len(aligned)} 張: 目前流程 {human(n_total)}（省 {(1 - n_total / o_total) * 100:.1f}%）"
              f" vs 用偵測品質 {human(d_total)}（省 {(1 - d_total / o_total) * 100:.1f}%）")
    print("沒偵測到的不代表乾淨：縮放過、非標準量化表、Q≥98 的圖都偵測不到。")
    print("品質值很可靠；4:2:0 / 4:4:4 是推測的，色彩邊緣銳利的圖會傾向判成 4:4:4（保守：檔案較大但不傷畫質）。")


def write_csv(path, rows):
    head = ['name', 'size_bytes', 'width', 'height', 'found', 'quality', 'subsampling', 'offset_x', 'offset_y',
            'aligned', 'peak_psnr', 'gap_db', 'reason', 'now_bytes', 'now_psnr', 'now_worst_tile_psnr',
            'detected_bytes', 'detected_psnr', 'detected_worst_tile_psnr', 'error']
    with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for r in rows:
            if r.get('error'):
                line = [''] * len(head)
                line[0] = clean_str(r['name'])
                line[head.index('error')] = r['error']
                w.writerow(line)
                continue
            n, d = r['now'], r['detected']
            w.writerow([clean_str(r['name']), r['size'], r['width'], r['height'], int(r['found']),
                        r['quality'] if r['found'] else '', ('4:2:0' if r['subsampling'] == 2 else '4:4:4') if r['found'] else '',
                        r['offset'][0] if r['found'] else '', r['offset'][1] if r['found'] else '', int(r['aligned']),
                        f"{r['peak_psnr']:.2f}", f"{r['gap']:.2f}", r['reason'],
                        n['size'], f"{n['psnr']:.2f}", f"{n['tile']:.2f}",
                        d['size'] if d else '', f"{d['psnr']:.2f}" if d else '', f"{d['tile']:.2f}" if d else '', ''])


def main(argv=None):
    parser = argparse.ArgumentParser(description='偵測 PNG 是不是從 JPEG 解碼來的，並比較用偵測品質轉檔的結果（輸入唯讀）')
    parser.add_argument('input', help='單一 .png / 資料夾 / .zip / .rar / .7z（只取 PNG）')
    parser.add_argument('--samples', type=int, default=20, help='抽測幾張（預設 20）')
    parser.add_argument('--pick', choices=('random', 'largest'), default='random')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--min-kb', type=int, default=0, help='只取不小於這個大小的 PNG（預設 0）')
    parser.add_argument('--workers', type=int, default=4, help='平行行程數')
    parser.add_argument('--out', default=str(BASE_DIR / 'jpeg_origin_out'), help='輸出資料夾（CSV）')
    args = parser.parse_args(argv)

    from concurrent.futures import ProcessPoolExecutor, as_completed
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print('🔬 PNG 的 JPEG 來源偵測（輸入檔案唯讀，不會被修改）')
    with tempfile.TemporaryDirectory(dir=je._scratch_dir()) as tmp:
        samples = je.collect_samples(args.input, args.samples, args.pick, args.seed, args.min_kb, tmp, je.PNG_EXTENSIONS)
        if not samples:
            return 1
        print(f'抽測 {len(samples)} 張，用 {args.workers} 個行程平行處理...')
        rows = []
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(analyze_one, (str(p), n)) for n, p in samples]
            for i, fut in enumerate(as_completed(futures), start=1):
                r = fut.result()
                rows.append(r)
                state = f"❌ {r['error']}" if r['error'] else ('JPEG 來源 Q' + str(r['quality']) if r['found'] else '未偵測到')
                print(f"  [{i}/{len(samples)}] {clean_str(r['name'])}  {state}")
    order = {n: i for i, (n, _p) in enumerate(samples)}
    rows.sort(key=lambda r: order.get(r['name'], 0))
    if not [r for r in rows if not r.get('error')]:
        print('❌ 沒有任何一張讀取成功')
        return 1
    print_report(rows)
    csv_path = out_dir / 'jpeg_origin.csv'
    write_csv(csv_path, rows)
    print(f"\n📑 原始數字: {csv_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
