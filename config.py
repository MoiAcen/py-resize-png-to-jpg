"""集中設定檔

所有可調參數、路徑與常數統一放這裡，需要調整行為時只改這一個檔案就好喵！
（本工具鏈鎖定 Windows 平台使用。）
"""
import os
from pathlib import Path

# 腳本所在的絕對路徑；快取檔一律以此為基準，與當前工作目錄無關
BASE_DIR = Path(__file__).resolve().parent

# ================= 資料夾路徑 =================
SOURCE_DIR = r"U:\CG"        # 來源資料夾
TARGET_DIR = r"U:\resize"    # 瘦身工作區

# ================= 快取 / 紀錄檔（絕對路徑）=================
LOG_FILE        = BASE_DIR / "analysis.bin"            # 無 PNG 黑名單
TARGETS_CACHE   = BASE_DIR / "analysis_targets.json"   # 排行榜快取
HASH_CACHE_FILE = BASE_DIR / "file_hash_cache.bin"     # Binary Hash 快取

# ================= 腳本名稱（階段串接用）=================
MOVE_SCRIPT_NAME   = "moveToResize.py"
RESIZE_SCRIPT_NAME = "resize.py"

# ================= 掃描 / 分析門檻 =================
SHOW_TOP_N               = 20     # 排行榜顯示數量
MAX_TARGET_FILES         = 5000   # 分析目標上限包數
MIN_ARCHIVE_SIZE_MB      = 50     # 壓縮包體積門檻 (MB)，小於此值直接略過
TARGET_PNG_RATIO         = 0.30   # 內部 PNG 容量佔比門檻（>= 才算達標）
MIN_SINGLE_PNG_KB        = 100    # 忽略小於此大小的碎圖 (KB)
ESTIMATED_REDUCTION_RATE = 0.80   # 預估 PNG 轉 JPG 可省下的體積比例

# ================= 轉檔設定 (resize.py) =================
QUALITY                = 80     # PNG 轉 JPG 品質 (0-100)
AUTO_DELETE_ORIGINAL   = True   # 數量驗收吻合後，刪除原始檔
OVERWRITE_EXISTING_ZIP = False  # 遇到其他同名 ZIP：False = 自動加 (1) 序號

# 轉檔併發數：扣掉 4 個核心留給其他服務（至少保留 1 個 Worker）
MAX_WORKERS = max(1, (os.cpu_count() or 8) - 4)

# ================= 共用常數 =================
# 支援掃描 / 處理的壓縮格式（副檔名皆為小寫）
ARCHIVE_EXTENSIONS = ('.zip', '.rar', '.7z')

# 7-Zip 可能的安裝路徑（依序嘗試，最後退回 PATH 上的 "7z"）
POSSIBLE_7Z_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "7z",
]

# 解析檔名標籤時要忽略的雜訊關鍵字（一律小寫比對）
IGNORED_TAG_KEYS = {'ai generated', 'fanbox', 'patreon', 'pixiv', 'unifans'}
