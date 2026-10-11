"""jpeg_origin.py：偵測 PNG 是不是從 JPEG 解碼來的。

重點：偵測得到（含格線位移）、乾淨/縮放/平滑漸層不能誤判、輸入唯讀、
「用偵測品質轉」確實比固定 Q80 更接近原圖。
"""
import csv
import hashlib
import io
import random
import sys
from pathlib import Path

from _harness import Checker, Sandbox, capture
import config


def md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def main():
    c = Checker('PNG 的 JPEG 來源偵測')
    with Sandbox() as sb:
        import jpeg_origin as jo
        from PIL import Image, ImageDraw, ImageFilter

        def art_like(seed, w=512, h=512):
            """有色塊、輪廓與漸層背景的插畫風合成圖（純雜訊與純平坦都不適合拿來測偵測）。"""
            r = random.Random(seed)
            im = Image.linear_gradient('L').resize((w, h)).convert('RGB')
            d = ImageDraw.Draw(im)
            for _ in range(40):
                x, y, rad = r.randint(0, w), r.randint(0, h), r.randint(15, 90)
                d.ellipse([x - rad, y - rad, x + rad, y + rad],
                          fill=(r.randint(0, 255), r.randint(0, 255), r.randint(0, 255)), outline=(20, 20, 20), width=2)
            for _ in range(60):
                pts = [(r.randint(0, w), r.randint(0, h)) for _ in range(3)]
                d.polygon(pts, fill=(r.randint(0, 255), r.randint(0, 255), r.randint(0, 255)))
            return im.filter(ImageFilter.GaussianBlur(0.8))

        def via_jpeg(im, q, sub=2):
            b = io.BytesIO()
            im.save(b, 'JPEG', quality=q, subsampling=sub)
            return Image.open(io.BytesIO(b.getvalue())).convert('RGB')

        base = art_like(2)

        c.section('偵測：有 JPEG 史的要抓到，品質要對')
        for q in (75, 85, 92, 95):
            r = jo.detect_jpeg_origin(via_jpeg(base, q))
            c.check(r['found'] and r['quality'] == q and r['offset'] == (0, 0),
                    f"Q{q} 4:2:0 來源：偵測到 Q{r['quality']}，格線對齊（落差 {r['gap']:.1f} dB）")
        r = jo.detect_jpeg_origin(via_jpeg(base, 85, 0))
        c.check(r['found'] and r['quality'] == 85 and r['subsampling'] == 0, '4:4:4 來源被認出是 4:4:4')

        c.section('偵測：格線被位移（裁切過）也找得回來')
        for (cx, cy) in ((3, 5), (11, 2)):
            shifted = via_jpeg(base, 85, 0).crop((cx, cy, 509, 507))
            r = jo.detect_jpeg_origin(shifted)
            want = ((8 - cx % 8) % 8, (8 - cy % 8) % 8)
            c.check(r['found'] and r['quality'] == 85 and r['offset'] == want,
                    f"裁掉左 {cx} 上 {cy}：位移 {r['offset']}，預期 {want}")

        c.section('偵測：不能誤判')
        c.check(not jo.detect_jpeg_origin(base)['found'], '乾淨的 PNG 不會被判成 JPEG 來源')
        c.check(not jo.detect_jpeg_origin(art_like(3))['found'], '另一張乾淨圖也不會')
        r = jo.detect_jpeg_origin(via_jpeg(base, 85).resize((460, 460), Image.LANCZOS))
        c.check(not r['found'], '縮放過的圖偵測不到（格線被打散；這是已知限制，不是 bug）')
        grad = Image.linear_gradient('L').resize((512, 512)).convert('RGB')
        r = jo.detect_jpeg_origin(grad)
        c.check(not r['found'] and '紋理' in r['reason'], f"純平滑漸層不判（任何品質都無損）：{r['reason']}")
        r = jo.detect_jpeg_origin(Image.new('RGB', (40, 40), (9, 9, 9)))
        c.check(not r['found'] and '太小' in r['reason'], '太小的圖說明原因而不是出錯')
        flat = Image.new('RGB', (512, 512), (240, 240, 240))
        c.check(not jo.detect_jpeg_origin(flat)['found'], '整張純色不誤判')

        c.section('用偵測到的品質轉：比固定 Q80 更接近原圖')
        src = via_jpeg(base, 85, 0)
        now = jo._convert(src, config.QUALITY, 2)
        det = jo._convert(src, 85, 0)
        from quality_test import psnr
        with Image.open(io.BytesIO(now)) as a, Image.open(io.BytesIO(det)) as b:
            p_now, p_det = psnr(src, a.convert('RGB')), psnr(src, b.convert('RGB'))
        c.check(p_det > p_now + 10, f'用偵測品質 {p_det:.1f} dB vs 固定 Q{config.QUALITY} {p_now:.1f} dB（幾乎無二次損失）')

        c.section('命令列：輸入唯讀、報告與 CSV、壞檔不拖垮')
        d = sb.root / 'origin_set'
        d.mkdir()
        via_jpeg(base, 85, 0).save(d / 'aligned.png')
        via_jpeg(base, 90, 0).crop((3, 5, 509, 507)).save(d / 'shifted.png')
        base.save(d / 'clean.png')
        via_jpeg(base, 85).resize((460, 460), Image.LANCZOS).save(d / 'resized.png')
        (d / 'broken.png').write_bytes(b'\x89PNG\r\n\x1a\n' + bytes(range(200)))
        before = {p.name: md5(p) for p in d.iterdir()}
        out_dir = sb.root / 'origin_out'
        out, rc = capture(jo.main, [str(d), '--workers', '2', '--out', str(out_dir)])
        c.check(rc == 0, f'結束代碼 0（實際 {rc}）')
        c.check({p.name: md5(p) for p in d.iterdir()} == before, '輸入資料夾內容與數量都沒變（唯讀）')
        c.check('broken.png' in out and '❌' in out, '壞檔有被回報、沒有讓整批中斷')
        rows = {r['name']: r for r in csv.DictReader(open(out_dir / 'jpeg_origin.csv', encoding='utf-8-sig'))}
        c.check(set(rows) == {'aligned.png', 'shifted.png', 'clean.png', 'resized.png', 'broken.png'}, f'CSV 五筆: {sorted(rows)}')
        c.check(rows['aligned.png']['found'] == '1' and rows['aligned.png']['quality'] == '85'
                and rows['aligned.png']['aligned'] == '1', '對齊的那張：偵測到 Q85、格線對齊')
        c.check(rows['shifted.png']['found'] == '1' and rows['shifted.png']['quality'] == '90'
                and rows['shifted.png']['aligned'] == '0' and rows['shifted.png']['detected_bytes'] == '',
                '位移的那張：偵測到 Q90，但不算對齊、不給「偵測品質轉檔」的數字')
        c.check(rows['clean.png']['found'] == '0' and rows['resized.png']['found'] == '0', '乾淨與縮放過的都沒偵測到')
        c.check(rows['broken.png']['error'] != '' and rows['aligned.png']['error'] == '', '錯誤訊息只在壞檔那一列')
        expect = len(jo._convert(Image.open(d / 'aligned.png').convert('RGB'), 85, int(rows['aligned.png']['subsampling'] == '4:2:0') * 2))
        c.check(int(rows['aligned.png']['detected_bytes']) == expect, 'CSV 的偵測品質轉檔大小與獨立重算一致')
        c.check('JPEG 來源: 2 / 4' in out and '偵測到 JPEG 來源' in out, '摘要：4 張可讀的裡面偵測到 2 張')
        c.check('不代表乾淨' in out, '報告有提醒「沒偵測到不代表乾淨」')

        out, rc = capture(jo.main, [str(d / 'aligned.png'), '--out', str(sb.root / 'o_single')])
        c.check(rc == 0 and 'Q85' in out, '單一 PNG 可以直接指定')
        out, rc = capture(jo.main, [str(sb.root / 'nope'), '--out', str(sb.root / 'o_none')])
        c.check(rc == 1, '輸入不存在時結束代碼為 1')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
