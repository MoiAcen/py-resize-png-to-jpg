"""集中設定檔

所有可調參數、路徑與常數統一放這裡，需要調整行為時只改這一個檔案就好喵！
（本工具鏈鎖定 Windows 平台使用。）
"""
import os
from pathlib import Path

# 腳本所在的絕對路徑；快取檔一律以此為基準，與當前工作目錄無關
BASE_DIR = Path(__file__).resolve().parent

# ================= 資料夾路徑 =================
SOURCE_DIR = r"U:\CG"                 # 來源資料夾
TARGET_DIR = r"U:\resize"             # 瘦身工作區
FAILED_DIR = r"U:\resize_failed"      # 解壓失敗的壓縮包搬到這裡待查
# 處理完的壓縮包搬到這裡，讓工作區只留下還沒處理完的東西。預設搬回來源收藏庫。
# 「處理成功」「已最佳化無需處理」「效率不足放棄」三種都算完成，走同一條規則。
# 設成 None 或 "" 就維持原本行為（產出留在 TARGET_DIR 原地）。
# 同名時沿用「自動加 (1)(2) 序號、永不覆蓋」的規則。
DONE_DIR   = r"U:\CG"                  # 處理完成的壓縮包搬回這裡(收藏庫)

# ================= 快取 / 紀錄檔（絕對路徑）=================
LOG_FILE        = BASE_DIR / "analysis.bin"            # 無 PNG 黑名單
TARGETS_CACHE   = BASE_DIR / "analysis_targets.json"   # 排行榜快取
HASH_CACHE_FILE = BASE_DIR / "file_hash_cache.bin"     # Binary Hash 快取
LOWGAIN_FLAG_FILE = BASE_DIR / "lowgain_skip.json"     # 「省太少而放棄」的標記，避免每次重跑空轉
# 手動標籤對照表（檔名 → 額外標籤）。給檔名裡找不到標籤的包用：
# 在搬移工具裡用關鍵字搜尋後，可以把那個關鍵字掛到搜到的檔案上。
# 只記在這個檔案裡，不會更動任何檔名；清快取也不會刪它（這是人工輸入的資料）。
MANUAL_TAG_FILE = BASE_DIR / "manual_tags.json"

# ================= 腳本名稱（階段串接用）=================
MOVE_SCRIPT_NAME   = "moveToResize.py"
RESIZE_SCRIPT_NAME = "resize.py"

# ================= 掃描 / 分析門檻 =================
SHOW_TOP_N               = 20     # 主選單/報告直接列出的排行榜筆數
# 寫進排行榜快取的筆數。選單只列 SHOW_TOP_N 筆，但「顯示全排名」可以翻到這麼多名，
# 所以這個值要比 SHOW_TOP_N 大才有東西可翻。
SAVE_RANK_N              = 200
RANK_PAGE_SIZE           = 20     # 「顯示全排名」每頁幾筆
EXTRA_ANALYZE_DEFAULT    = 500    # 選單「追加解析」預設要補幾筆（直接 Enter 用這個值）
MAX_TARGET_FILES         = 5000   # 分析目標上限包數
# 每輪至少要「新解析」幾個包。抓滿上限之後若本輪新解析的數量還不到這個額度，
# 就繼續往下讀更多沒掃過的檔案，補滿再進排名——這樣快取才會一輪一輪把收藏庫吃完，
# 而不是每次都只重複啃前面那幾個資料夾。沒有更多檔案可讀就直接進排名。
# 設 0 表示不啟用（抓滿上限就立刻停止讀新檔）。
EXTRA_SCAN_QUOTA         = 500
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

# ================= JPG 體檢 / 瘦身門檻 =================
JPEG_EXTENSIONS = ('.jpg', '.jpeg')

# --- 分析階段（analysis.py）：靠檔案大小快速篩「問題包」候選 ---
JPEG_LARGE_KB      = 1024   # 單張 JPG 超過此大小 (KB) 視為「可能過大」候選（排除縮圖/雜圖）
JPEG_PROBLEM_RATIO = 0.30   # 過大 JPG 佔壓縮包內容比例達此值 → 視為值得瘦身的目標
# 抽樣幾張最大的 JPG 來判斷「這包是否已經處理過」。抽到的每張都不需修正 →
# 視為已最佳化，排行榜不再列入（避免處理完的包每次掃描又冒出來）。
JPEG_OPTIMIZED_SAMPLE_COUNT = 3
ESTIMATED_JPG_REDUCTION_RATE = 0.20   # 預估過大 JPG 重整後可省下的體積比例（保守估算，僅供排序）

# --- resize 階段：逐檔讀 marker 判斷該不該修正 ---
JPEG_MAX_LONG_EDGE          = 4000  # 長邊超過此像素 → 標記解析度過大
# 是否允許縮解析度。預設關閉：關閉時「解析度過大」只當資訊提示、不觸發修正，
# 避免「已最佳化但尺寸本來就大」的圖每輪被重複丟去處理。
JPEG_ENABLE_DOWNSCALE       = False
JPEG_RECOMPRESS_MIN_QUALITY = 95    # 估算品質 >= 此值才建議重壓
JPEG_SKIP_BELOW_QUALITY     = 85    # 估算品質 < 此值 → 跳過以保護畫質

# 修正模式：
#   'report' = 只判斷並列出報告、不改檔（預設，最安全）
#   'auto'   = 依體檢結果自動分流修正，沿用「變小才替換」驗收
#              A 無損：mozjpeg 無損最佳化（零畫質損失）
#              B 重壓：PIL 以目標品質/抽樣重編 → 再接 A 無損擠壓
#   'off'    = 完全不做 JPG 體檢
# （A/B 皆用 pip 套件 mozjpeg-lossless-optimization，免裝外部 exe）
JPEG_FIX_MODE           = 'auto'
JPEG_TARGET_QUALITY     = 92   # B（重壓）對「寫實/照片類」內容的目標品質
JPEG_TARGET_SUBSAMPLING = 2    # B 重壓的色度抽樣：2 = 4:2:0（省空間、肉眼幾乎無感）

# --- 依內容分流：平塗（賽璐璐風）可以壓得更兇，寫實紋理則保守 ---
# 判斷指標是「顏色數佔比」：把圖以 NEAREST 取樣成小圖後，相異顏色數 / 總像素。
# 實測分離度很大——真實照片 36~74%，平塗類 0~9%，中間有約 27 個百分點的空隙。
# 低於門檻視為平塗 → 用 JPEG_FLAT_QUALITY；否則用 JPEG_TARGET_QUALITY。
# 門檻預設偏保守（偏向判成寫實），寧可少省一點也不要壓壞細節多的圖。
# 想校準成自己收藏的實際分佈，用 quality_test.py 看每張圖的實測值。
JPEG_FLAT_QUALITY       = 80    # 平塗內容的目標品質；設成與 JPEG_TARGET_QUALITY 相同即等於關閉分流
JPEG_FLAT_COLOR_RATIO   = 0.15  # 顏色數佔比低於此值 → 判定為平塗
JPEG_CONTENT_SAMPLE_SIZE = 160  # 判定用的取樣邊長（越大越準也越慢；160 約 5ms/張）

# --- 整包驗收：省太少就整包放棄，保留原檔 ---
# 重壓一定有畫質代價，若整包只省下個位數百分比，等於付出代價卻換不到空間，
# 不如原封不動。設 0 表示不啟用這道檢查。
MIN_ARCHIVE_SAVING_RATIO = 0.10   # 整包縮減低於 10% → 放棄替換

# 解壓前先抽樣判斷整包是否已處理過，是就直接跳過，省下整包解壓的 I/O。
# 判斷方式與 analysis 相同（抽樣最大的幾張 JPG 看還需不需要修正）。
# 關掉的話每個已處理過的包仍會被完整解壓一次才發現沒事做。
SKIP_ALREADY_DONE_ARCHIVES = True

# ================= 共用常數 =================
# 支援掃描 / 處理的壓縮格式（副檔名皆為小寫）
ARCHIVE_EXTENSIONS = ('.zip', '.rar', '.7z')

# 7-Zip 可能的安裝路徑（依序嘗試，最後退回 PATH 上的 "7z"）
POSSIBLE_7Z_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "7z",
]

# 解析檔名標籤時要忽略的雜訊關鍵字（比對時大小寫無關，可直接照原樣寫）
# 全域生效：這裡列到的字串一律不會被當成標籤。
IGNORED_TAG_KEYS = {
    'ai generated', 'fanbox', 'patreon', 'pixiv', 'unifans',
    'Uncensored', 'Decensored', 'Extra', 'AI生成', '同人CG集',
    'V',
}

# 整串標籤符合這些樣式就當雜訊（大小寫無關，自動錨定頭尾）。
# 數字的部分沒辦法逐一列舉，所以這些用樣式比對而不是列在 IGNORED_TAG_KEYS：
#   part[\s._-]*\d+  → [Part 2]、[Part.03]、[Part-10] 這種分卷序號
#                      （Parts、Particle2 不會誤中，因為樣式錨定頭尾）
#   \d{1,4}          → [1]、[23]、[1234] 這種短號，是集數/序號而不是標籤；
#                      [45088166] 這類作品 ID 位數夠長，不會被這條打到
IGNORED_TAG_PATTERNS = (
    r'part[\s._-]*\d+',
    r'\d{1,4}',
)

# 一個括號裡用斜線隔開時是「多個標籤」而不是一個複合名稱，要拆開各自判斷：
#   [Decensored ⧸ Uncensored] → Decensored、Uncensored 兩個標籤（兩個都是雜訊，都丟掉）
#   [Kurohime ⧸ Uncensored]   → Kurohime 留下、Uncensored 丟掉
# Windows 檔名不能放 '/'，常被代換成 '⧸'，所以這幾種都收進來。
# 刻意不拆空白——作者名本來就可能有空格，拆了會把名字打散。
TAG_SEPARATORS = r'[/⧸∕／\\＼|｜]'

