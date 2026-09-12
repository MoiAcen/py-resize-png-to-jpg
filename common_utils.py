"""共用工具函式

檔名清洗、標籤解析、各式快取讀寫，以及「只讀檔頭精算 PNG 佔比」等
三個階段腳本共用的邏輯都集中在這裡。設定值一律從 config 匯入喵！
"""
import hashlib
import json
import os
import pickle
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from config import (
    LOG_FILE, TARGETS_CACHE, HASH_CACHE_FILE,
    MIN_SINGLE_PNG_KB, POSSIBLE_7Z_PATHS, IGNORED_TAG_KEYS,
    MAX_NUMERIC_NOISE_DIGITS,
    JPEG_EXTENSIONS, JPEG_LARGE_KB, TARGET_PNG_RATIO, JPEG_PROBLEM_RATIO,
    JPEG_OPTIMIZED_SAMPLE_COUNT, LOWGAIN_FLAG_FILE,
    QUALITY, JPEG_TARGET_QUALITY, JPEG_FLAT_QUALITY, JPEG_FLAT_COLOR_RATIO,
    JPEG_TARGET_SUBSAMPLING, MIN_ARCHIVE_SAVING_RATIO,
)


# ================= 字串 / 格式化 =================
def clean_str(s):
    """把 Windows 檔名裡的 surrogate 亂碼字元換成 '?'，避免 print 崩潰。"""
    if not isinstance(s, str):
        return s
    return ''.join(c if not (0xD800 <= ord(c) <= 0xDFFF) else '?' for c in s)


def format_mb_or_gb(mb_value):
    """數值 >= 1024 MB 時自動改用 GB 顯示。"""
    if mb_value >= 1024:
        return f"{mb_value / 1024:.2f} GB"
    return f"{mb_value:.1f} MB"


# 先把忽略清單正規化成小寫，讓比對大小寫無關
# （config 裡不論寫 'Uncensored' 還 'uncensored'、'AI生成' 都能命中）
_IGNORED_TAG_KEYS_LOWER = {k.lower() for k in IGNORED_TAG_KEYS}


def is_numeric_noise_tag(tag):
    """判斷標籤是不是「純數字的短組合」(1、2、…、1234)。

    這種多半是集數或序號，拿來排行榜彙總會把不相干的包湊成一堆。
    作品 ID 那種長數字位數夠長，不會被判成雜訊。
    刻意只給排行榜用：使用者若真的拿數字當關鍵字搜尋，還是要找得到。
    """
    return tag.isdigit() and len(tag) <= MAX_NUMERIC_NOISE_DIGITS


def extract_all_tags(filename):
    """從檔名的 []【】()（） 括號中抓出標籤，並濾掉雜訊關鍵字（大小寫無關）。"""
    raw_tags = re.findall(r'[\[【\(\（]\s*([^\]】\)\）]+?)\s*[\]】\)\）]', filename)
    clean_tags = []
    for t in raw_tags:
        t_str = t.strip()
        if t_str and t_str.lower() not in _IGNORED_TAG_KEYS_LOWER:
            clean_tags.append(t_str)
    return clean_tags


# ================= Binary Hash 快取 =================
def get_file_hash_key(file_path):
    """以「檔名 + 大小 + 修改時間」產生唯一 key，用來辨識檔案是否變動。"""
    try:
        stat = file_path.stat()
        raw_key = f"{file_path.name}_{stat.st_size}_{stat.st_mtime}"
        return hashlib.md5(raw_key.encode('utf-8', errors='ignore')).hexdigest()
    except Exception:
        return None


def load_hash_cache():
    """讀取 pickle 格式的 PNG 佔比快取；失敗則回傳空 dict。"""
    if os.path.exists(HASH_CACHE_FILE):
        try:
            with open(HASH_CACHE_FILE, 'rb') as f:
                return pickle.load(f)
        except Exception:
            pass
    return {}


def save_hash_cache(cache_data):
    """把 PNG 佔比快取寫回磁碟（失敗時靜默略過）。"""
    try:
        with open(HASH_CACHE_FILE, 'wb') as f:
            pickle.dump(cache_data, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass


# ================= 無 PNG 黑名單 =================
def load_processed_files(log_file=LOG_FILE):
    """讀取「確認無 PNG」黑名單，回傳小寫檔名集合。"""
    processed = set()
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line_clean = line.strip().lower()
                    if line_clean:
                        processed.add(line_clean)
        except Exception:
            pass
    return processed


def save_clean_file(filename, log_file=LOG_FILE):
    """把一個確認無 PNG 的檔名追加到黑名單。"""
    try:
        with open(log_file, 'a', encoding='utf-8', errors='ignore') as f:
            f.write(f"{filename}\n")
    except Exception:
        pass


# ================= 排行榜快取 =================
def load_targets_cache(cache_file=TARGETS_CACHE):
    """讀取 analysis.py 產出的排行榜 JSON 快取。"""
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 快取讀取失敗: {e}")
    return []


# ================= 「效率不足」標記 =================
def settings_signature():
    """把會影響「能省多少」的設定編成簽章。

    任一設定改變 → 舊標記自動失效重評，不必手動清，
    免得又出現「調了門檻卻沒生效」的困惑。
    """
    return (f"q{QUALITY}|tq{JPEG_TARGET_QUALITY}|fq{JPEG_FLAT_QUALITY}"
            f"|fr{JPEG_FLAT_COLOR_RATIO}|sub{JPEG_TARGET_SUBSAMPLING}"
            f"|min{MIN_ARCHIVE_SAVING_RATIO}")


def load_lowgain_flags():
    """讀取「省太少而放棄」的標記；設定簽章不符的項目直接丟棄。"""
    if not os.path.exists(LOWGAIN_FLAG_FILE):
        return {}
    try:
        with open(LOWGAIN_FLAG_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return {}

    sig = settings_signature()
    kept = {k: v for k, v in raw.items() if v.get('sig') == sig}
    dropped = len(raw) - len(kept)
    if dropped:
        print(f"♻️ 設定已變更，{dropped} 筆舊標記失效，這些壓縮包將重新評估喵！")
    return kept


def save_lowgain_flags(flags):
    """把標記寫回磁碟（失敗時靜默略過，不影響主流程）。"""
    try:
        with open(LOWGAIN_FLAG_FILE, 'w', encoding='utf-8') as f:
            json.dump(flags, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 標記寫入失敗: {e}")


def flag_key(archive_path):
    """以「檔名+大小+時間」當標記 key：檔案一旦變動，標記自然失效。"""
    return get_file_hash_key(Path(archive_path))



# ================= 7-Zip 定位 =================
def find_7z():
    """依 POSSIBLE_7Z_PATHS 逐一尋找 7z 執行檔，找不到回傳 None。"""
    for p in POSSIBLE_7Z_PATHS:
        if os.path.exists(p) or shutil.which(p):
            return p
    return None


SEVEN_ZIP_PATH = find_7z()


# ================= 壓縮包影像帳（核心）=================
# 快取簽章：把「格式版本 + 會影響統計結果的門檻」一起編進 key。
# 任一門檻改變（例如調整 JPEG_LARGE_KB / MIN_SINGLE_PNG_KB）時，舊快取自動
# 失效重算，不需手動清快取。
_STATS_CACHE_VERSION = f"4|png{MIN_SINGLE_PNG_KB}|jpg{JPEG_LARGE_KB}|s{JPEG_OPTIMIZED_SAMPLE_COUNT}"


def _sample_zip_jpgs_optimized(zf, jpg_entries):
    """抽樣 zip 內最大的幾張 JPG，判斷是否『已經最佳化、再處理也省不了』。

    只讀每張圖的檔頭（不解出整張圖），交給 jpeg_inspector 判定。
    回傳 True 代表抽樣到的每一張都不需要修正。
    無法判定（讀不到、沒有可抽樣的圖）時回傳 False，採保守作法。
    """
    if not jpg_entries:
        return False

    # 延遲匯入避免模組循環相依（jpeg_inspector 只依賴 config）
    from jpeg_inspector import inspect_jpeg_stream, classify_jpeg

    picked = sorted(jpg_entries, key=lambda it: it.file_size, reverse=True)
    picked = picked[:max(1, JPEG_OPTIMIZED_SAMPLE_COUNT)]

    checked = 0
    for item in picked:
        try:
            with zf.open(item, 'r') as fp:
                report = inspect_jpeg_stream(fp)
        except Exception:
            return False
        if not report:
            return False
        if classify_jpeg(report)['needs_fix']:
            return False
        checked += 1

    return checked > 0


def zip_has_work(file_path):
    """不解壓，只讀檔頭快速判斷這個 zip 還有沒有可瘦身的內容。

    回傳 True=有事可做 / False=已經處理過 / None=無法判斷（呼叫端應保守繼續）。
    解壓一個 250MB 的包要 2.6 秒，這裡只要約 2ms，差三個數量級。
    """
    if file_path.suffix.lower() != '.zip':
        return None
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            jpg_entries = []
            for item in zf.infolist():
                if item.is_dir():
                    continue
                name = item.filename.lower()
                if name.endswith('.png'):
                    return True          # 有 PNG 就一定有得轉
                if name.endswith(JPEG_EXTENSIONS):
                    jpg_entries.append(item)

            if not jpg_entries:
                return False             # 既沒 PNG 也沒 JPG
            return not _sample_zip_jpgs_optimized(zf, jpg_entries)
    except Exception:
        return None


def get_archive_image_stats(file_path, hash_cache):
    """只讀壓縮檔的檔頭清單，統計 PNG / JPG 容量帳。

    zip 直接用 Python zipfile 讀，其餘格式呼叫 7-Zip 列出清單解析。
    命中快取（且版本相符）則直接回傳，不重複掃描。

    回傳: (stats: dict, 是否為本次全新解析)
      stats 內含 success / total_bytes / png_bytes / jpg_bytes / large_jpg_bytes
      - png_bytes：只計單張 >= MIN_SINGLE_PNG_KB 的 PNG
      - large_jpg_bytes：只計單張 >= JPEG_LARGE_KB 的 JPG（過大候選）
    """
    hash_key = get_file_hash_key(file_path)
    if hash_key and hash_key in hash_cache:
        cached = hash_cache[hash_key]
        if cached.get('v') == _STATS_CACHE_VERSION:
            return cached, False

    ext = file_path.suffix.lower()
    min_png_bytes = MIN_SINGLE_PNG_KB * 1024
    large_jpg_bytes_th = JPEG_LARGE_KB * 1024
    acc = {'total': 0, 'png': 0, 'jpg': 0, 'large_jpg': 0}
    is_success = False

    def account(name_lower, size):
        acc['total'] += size
        if name_lower.endswith('.png'):
            if size >= min_png_bytes:
                acc['png'] += size
        elif name_lower.endswith(JPEG_EXTENSIONS):
            acc['jpg'] += size
            if size >= large_jpg_bytes_th:
                acc['large_jpg'] += size

    jpg_optimized = False
    if ext == '.zip':
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                jpg_entries = []
                for item in zf.infolist():
                    if item.is_dir():
                        continue
                    name_lower = item.filename.lower()
                    account(name_lower, item.file_size)
                    if (name_lower.endswith(JPEG_EXTENSIONS)
                            and item.file_size >= large_jpg_bytes_th):
                        jpg_entries.append(item)
                # 抽樣判斷這包的 JPG 是否已最佳化（resize 產物一律是 zip）
                jpg_optimized = _sample_zip_jpgs_optimized(zf, jpg_entries)
            is_success = True
        except Exception:
            is_success = False
    elif SEVEN_ZIP_PATH:
        try:
            cmd = [SEVEN_ZIP_PATH, "l", str(file_path)]
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors='ignore', timeout=10,
            )

            line_pattern = re.compile(
                r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+([D\.A-Z\?\*]{5})\s+(\d+)',
                re.IGNORECASE,
            )

            for line in res.stdout.splitlines():
                line_str = line.strip()
                match = line_pattern.match(line_str)
                if not match:
                    continue

                attr, size_str = match.group(1), match.group(2)
                if 'D' in attr.upper():   # 跳過資料夾項目
                    continue
                # 7-Zip 清單的檔名在行尾，直接用整行小寫比對副檔名
                account(line_str.lower(), int(size_str))
            is_success = True
        except Exception:
            is_success = False

    stats = {
        'v': _STATS_CACHE_VERSION,
        'filename': file_path.name,
        'success': is_success,
        'total_bytes': acc['total'],
        'png_bytes': acc['png'],
        'jpg_bytes': acc['jpg'],
        'large_jpg_bytes': acc['large_jpg'],
        'jpg_optimized': jpg_optimized,
    }
    if hash_key and is_success:
        hash_cache[hash_key] = stats

    return stats, True


def stats_png_ratio(stats):
    """由 stats 算 PNG 容量佔比。"""
    total = stats['total_bytes']
    return (stats['png_bytes'] / total) if (total > 0 and stats['success']) else 0.0


def stats_large_jpg_ratio(stats):
    """由 stats 算『過大 JPG』容量佔比（問題包指標）。"""
    total = stats['total_bytes']
    return (stats['large_jpg_bytes'] / total) if (total > 0 and stats['success']) else 0.0


def is_slim_target(stats):
    """統一的『值得瘦身』判準：達標 PNG 佔比，或過大 JPG 佔比達門檻。

    analysis / moveToResize / 關鍵字搜尋共用同一判準，避免 JPG 包在中途被濾掉。
    若抽樣顯示這包的 JPG 已最佳化，且沒有可轉的 PNG，則視為已處理完畢、不再列入。
    """
    if not stats['success']:
        return False

    has_png = stats_png_ratio(stats) >= TARGET_PNG_RATIO
    if has_png:
        return True

    # 沒有可轉的 PNG，且 JPG 抽樣判定已最佳化 → 再跑也省不了，直接排除
    if stats.get('jpg_optimized'):
        return False

    return stats_large_jpg_ratio(stats) >= JPEG_PROBLEM_RATIO


def get_png_ratio_with_cache(file_path, hash_cache, min_single_kb=MIN_SINGLE_PNG_KB):
    """相容包裝：沿用舊介面回傳 (png 佔比, 是否成功, 是否為本次全新解析)。"""
    stats, is_new = get_archive_image_stats(file_path, hash_cache)
    return stats_png_ratio(stats), stats['success'], is_new


# ================= 檔名防撞 =================
def get_safe_destination(target_path):
    """目標路徑已存在時，自動補上 (1)(2)... 序號避免覆蓋。"""
    if not target_path.exists():
        return target_path
    parent, suffix = target_path.parent, target_path.suffix
    counter = 1
    new_path = parent / f"{target_path.stem} ({counter}){suffix}"
    while new_path.exists():
        counter += 1
        new_path = parent / f"{target_path.stem} ({counter}){suffix}"
    return new_path
