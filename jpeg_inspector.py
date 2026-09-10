"""JPEG 體檢器

只讀 JPEG 的檔頭 marker（不解碼像素），快速判斷一張 JPG 的：
解析度、baseline/progressive、色度抽樣、估算原始品質，
並據此分級「該不該修、怎麼修」。供 analysis / resize 共用喵！
"""
from config import (
    JPEG_MAX_LONG_EDGE, JPEG_RECOMPRESS_MIN_QUALITY, JPEG_SKIP_BELOW_QUALITY,
    JPEG_ENABLE_DOWNSCALE,
)

# JPEG 規格書 Annex K.1 標準亮度量化表（自然順序 8x8）
STD_LUMA_QT = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]

# zigzag 位置 -> 自然順序索引（DQT 內容是以 zigzag 順序儲存）
ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]

# SOF marker（開始一個 Frame）；C2 = progressive，其餘常見為 baseline/extended
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# 沒有長度欄位的獨立 marker（SOI/EOI/RSTn/TEM）
_STANDALONE = {0xD8, 0xD9, 0x01} | set(range(0xD0, 0xD8))


def estimate_quality(qt_zigzag):
    """由亮度量化表（zigzag 順序）反推 libjpeg 品質值，回傳 1-100 或 None。"""
    if not qt_zigzag or len(qt_zigzag) < 64:
        return None
    ratios = []
    for k in range(64):
        std = STD_LUMA_QT[ZIGZAG[k]]
        q = qt_zigzag[k]
        if std > 0 and q > 0:
            ratios.append(q * 100.0 / std)
    if not ratios:
        return None

    ratios.sort()
    scale = ratios[len(ratios) // 2]  # 取中位數，抗離群值

    if scale <= 0:
        return 100
    quality = (200 - scale) / 2.0 if scale < 100 else 5000.0 / scale
    return max(1, min(100, round(quality)))


def _subsampling_label(comp_sampling):
    """由各分量取樣因子推斷色度抽樣格式字串。"""
    if not comp_sampling:
        return 'unknown'
    y_h, y_v = comp_sampling[0]
    mapping = {(1, 1): '4:4:4', (2, 1): '4:2:2', (2, 2): '4:2:0', (1, 2): '4:4:0'}
    return mapping.get((y_h, y_v), f'{y_h}x{y_v}')


def inspect_jpeg_stream(fp):
    """解析一個 binary stream 的 JPEG marker，回傳體檢 dict（失敗回 None）。"""
    if fp.read(2) != b'\xff\xd8':   # 必須以 SOI 開頭
        return None

    width = height = 0
    is_progressive = False
    comp_sampling = []
    luma_qt = None

    while True:
        b = fp.read(1)
        if not b:
            break
        if b != b'\xff':
            continue
        # 跳過連續的 0xFF 填充位元組
        marker = fp.read(1)
        while marker == b'\xff':
            marker = fp.read(1)
        if not marker:
            break
        m = marker[0]

        if m == 0xDA or m == 0xD9:   # SOS（進入壓縮資料）或 EOI → 停止
            break
        if m in _STANDALONE:
            continue

        length_bytes = fp.read(2)
        if len(length_bytes) < 2:
            break
        seg_len = (length_bytes[0] << 8) | length_bytes[1]
        payload = fp.read(seg_len - 2)

        if m in _SOF_MARKERS:
            is_progressive = (m == 0xC2)
            if len(payload) >= 6:
                height = (payload[1] << 8) | payload[2]
                width = (payload[3] << 8) | payload[4]
                ncomp = payload[5]
                for i in range(ncomp):
                    off = 6 + i * 3
                    if off + 2 < len(payload):
                        sampling = payload[off + 1]
                        comp_sampling.append((sampling >> 4, sampling & 0x0F))
        elif m == 0xDB:   # DQT
            idx = 0
            while idx < len(payload):
                pq_tq = payload[idx]
                idx += 1
                precision = pq_tq >> 4   # 0 = 8-bit, 1 = 16-bit
                tq = pq_tq & 0x0F
                count = 128 if precision else 64
                table = payload[idx:idx + count]
                idx += count
                if tq == 0 and precision == 0 and luma_qt is None:
                    luma_qt = list(table)

    if width == 0 or height == 0:
        return None

    return {
        'width': width,
        'height': height,
        'long_edge': max(width, height),
        'progressive': is_progressive,
        'subsampling': _subsampling_label(comp_sampling),
        'est_quality': estimate_quality(luma_qt),
    }


def inspect_jpeg(path):
    """讀取一個 JPG 檔案並回傳體檢 dict（失敗回 None）。"""
    try:
        with open(path, 'rb') as f:
            return inspect_jpeg_stream(f)
    except Exception:
        return None


def classify_jpeg(report):
    """依體檢報告與 config 門檻，判斷是否需要修正、建議動作與理由。

    回傳: {'needs_fix': bool, 'action': str, 'reasons': [str]}
      action: 'skip' / 'lossless' / 'downscale' / 'recompress' / 'downscale+recompress'
    """
    if not report:
        return {'needs_fix': False, 'action': 'skip', 'reasons': ['無法解析']}

    reasons = []
    q = report.get('est_quality')
    oversized = report['long_edge'] > JPEG_MAX_LONG_EDGE
    overquality = q is not None and q >= JPEG_RECOMPRESS_MIN_QUALITY
    low_quality = q is not None and q < JPEG_SKIP_BELOW_QUALITY
    full_chroma = report.get('subsampling') == '4:4:4'
    baseline = not report.get('progressive')

    # 低品質原檔：再壓純虧畫質，除非解析度過大才只做縮圖
    if low_quality and not oversized:
        return {'needs_fix': False, 'action': 'skip',
                'reasons': [f'原始品質偏低(~Q{q})，保護畫質不重壓']}

    if oversized:
        reasons.append(f'解析度過大({report["width"]}x{report["height"]})')
    if overquality:
        reasons.append(f'品質過高(~Q{q})')
    if full_chroma:
        reasons.append('色度未抽樣(4:4:4)')
    if baseline:
        reasons.append('baseline(可轉progressive)')

    # 縮解析度關閉時，「解析度過大」只保留在 reasons 當資訊，不參與動作判定；
    # 否則已最佳化但尺寸本來就大的圖會每輪都被判定需修正、重複空跑。
    scale_fix = oversized and JPEG_ENABLE_DOWNSCALE

    if scale_fix and (overquality or full_chroma):
        action = 'downscale+recompress'
    elif scale_fix:
        action = 'downscale'
    elif overquality or full_chroma:
        action = 'recompress'
    elif baseline:
        action = 'lossless'   # 只有 baseline 沒佔到，走無損最佳化
    else:
        action = 'skip'

    return {'needs_fix': action != 'skip', 'action': action, 'reasons': reasons}
