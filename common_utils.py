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

from config import (
    LOG_FILE, TARGETS_CACHE, HASH_CACHE_FILE,
    MIN_SINGLE_PNG_KB, POSSIBLE_7Z_PATHS, IGNORED_TAG_KEYS,
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


def extract_all_tags(filename):
    """從檔名的 []【】()（） 括號中抓出標籤，並濾掉雜訊關鍵字。"""
    raw_tags = re.findall(r'[\[【\(\（]\s*([^\]】\)\）]+?)\s*[\]】\)\）]', filename)
    clean_tags = []
    for t in raw_tags:
        t_str = t.strip()
        if t_str and t_str.lower() not in IGNORED_TAG_KEYS:
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


# ================= 7-Zip 定位 =================
def find_7z():
    """依 POSSIBLE_7Z_PATHS 逐一尋找 7z 執行檔，找不到回傳 None。"""
    for p in POSSIBLE_7Z_PATHS:
        if os.path.exists(p) or shutil.which(p):
            return p
    return None


SEVEN_ZIP_PATH = find_7z()


# ================= PNG 佔比精算（核心）=================
def get_png_ratio_with_cache(file_path, hash_cache, min_single_kb=MIN_SINGLE_PNG_KB):
    """只讀壓縮檔的檔頭清單，計算內部 PNG 容量佔比。

    zip 直接用 Python zipfile 讀，其餘格式呼叫 7-Zip 列出清單解析。
    命中快取則直接回傳，不重複掃描。

    回傳: (png 佔比, 是否成功, 是否為本次全新解析)
    """
    hash_key = get_file_hash_key(file_path)

    if hash_key and hash_key in hash_cache:
        cached_info = hash_cache[hash_key]
        return cached_info['ratio'], cached_info['success'], False

    ext = file_path.suffix.lower()
    total_bytes = 0
    png_bytes = 0
    min_bytes = min_single_kb * 1024
    is_success = False

    if ext == '.zip':
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                for item in zf.infolist():
                    if item.is_dir():
                        continue
                    total_bytes += item.file_size
                    if item.filename.lower().endswith('.png') and item.file_size >= min_bytes:
                        png_bytes += item.file_size
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

                file_size = int(size_str)
                total_bytes += file_size
                if line_str.lower().endswith('.png') and file_size >= min_bytes:
                    png_bytes += file_size
            is_success = True
        except Exception:
            is_success = False

    ratio = (png_bytes / total_bytes) if (total_bytes > 0 and is_success) else 0.0

    if hash_key and is_success:
        hash_cache[hash_key] = {
            'filename': file_path.name,
            'ratio': ratio,
            'success': is_success,
        }

    return ratio, is_success, True


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
