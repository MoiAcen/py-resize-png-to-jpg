"""階段三：實際轉檔瘦身

對工作區內每個壓縮包解壓 → 將內部 PNG 轉為 JPG → 重新打包成 ZIP，
以「檔案數量吻合」為驗收條件，通過才替換原檔並繼承修改時間喵！
（多核並行；使用短路徑暫存區規避 Windows 260 字元路徑限制。）
"""
import io
import os
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from config import (
    TARGET_DIR, QUALITY, AUTO_DELETE_ORIGINAL, OVERWRITE_EXISTING_ZIP,
    MAX_WORKERS, ARCHIVE_EXTENSIONS, JPEG_EXTENSIONS, JPEG_FIX_MODE,
    JPEG_TARGET_QUALITY, JPEG_TARGET_SUBSAMPLING,
)
from common_utils import clean_str, format_mb_or_gb, SEVEN_ZIP_PATH
from jpeg_inspector import inspect_jpeg, classify_jpeg

# mozjpeg 無損最佳化（pip 套件，內建 mozjpeg，免外部 exe）；未安裝則 A 略過
try:
    import mozjpeg_lossless_optimization as _mlo
except Exception:
    _mlo = None


def get_safe_output_path(target_zip_path, current_archive_path=None):
    """決定最終輸出路徑：允許覆蓋自身，否則遇同名檔自動加序號。"""
    target_abs = os.path.abspath(target_zip_path)
    current_abs = os.path.abspath(current_archive_path) if current_archive_path else None

    if OVERWRITE_EXISTING_ZIP or target_abs == current_abs:
        return target_zip_path

    if not os.path.exists(target_zip_path):
        return target_zip_path

    parent = os.path.dirname(target_zip_path)
    filename, ext = os.path.splitext(os.path.basename(target_zip_path))

    counter = 1
    new_path = os.path.join(parent, f"{filename} ({counter}){ext}")
    while os.path.exists(new_path):
        if current_abs and os.path.abspath(new_path) == current_abs:
            return new_path
        counter += 1
        new_path = os.path.join(parent, f"{filename} ({counter}){ext}")

    return new_path


def safe_remove(file_path, retries=3, delay=1):
    """帶重試的刪除：先解除唯讀，遇 PermissionError 稍候再試。"""
    for i in range(retries):
        try:
            os.chmod(file_path, stat.S_IWRITE)
            os.remove(file_path)
            return True
        except PermissionError:
            if i < retries - 1:
                time.sleep(delay)
            else:
                raise
    return False


def extract_archive_to_dir(archive_path, extract_dst):
    """雙重解壓：先試 7-Zip，失敗再退回 Python zipfile，並回傳錯誤詳情。"""
    err_msg = ""

    # 策略 1: 使用 7-Zip 解壓
    if SEVEN_ZIP_PATH:
        try:
            cmd = [SEVEN_ZIP_PATH, "x", str(archive_path), f"-o{extract_dst}", "-y"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='ignore')
            if res.returncode == 0:
                return True, ""
            err_msg = f"7-Zip 解壓失敗 (Code {res.returncode}): {res.stderr.strip() or res.stdout.strip()}"
        except Exception as e:
            err_msg = f"呼叫 7-Zip 異常: {e}"

    # 策略 2: 備用 Python zipfile
    ext = os.path.splitext(archive_path)[1].lower()
    if ext == '.zip':
        try:
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(extract_dst)
            return True, ""
        except Exception as e:
            err_msg += f" | zipfile 解壓失敗: {e}"

    return False, err_msg


def convert_single_image_worker(args):
    """Worker：將單張 PNG 轉為 JPG。只有轉出更小才保留並刪除原 PNG。"""
    img_path_str, quality = args
    img_path = Path(img_path_str)

    if img_path.suffix.lower() != '.png':
        return False

    try:
        orig_stat = img_path.stat()
        orig_size = orig_stat.st_size
        orig_atime = orig_stat.st_atime
        orig_mtime = orig_stat.st_mtime

        with Image.open(img_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')

            jpg_path = img_path.with_suffix('.jpg')
            img.save(jpg_path, format='JPEG', quality=quality, optimize=True)

        if jpg_path.stat().st_size < orig_size:
            try:
                os.utime(jpg_path, (orig_atime, orig_mtime))
            except Exception:
                pass
            img_path.unlink()
            return True

        # 轉出反而更大：放棄 JPG，保留原 PNG
        jpg_path.unlink()
        return False
    except Exception:
        return False


def report_jpeg_health(all_extracted_files):
    """對解壓後的 JPG 逐檔讀 marker 體檢，印出『需不需要修正』彙總（不改檔）。"""
    jpg_files = [f for f in all_extracted_files if f.suffix.lower() in JPEG_EXTENSIONS]
    if not jpg_files:
        return

    buckets = defaultdict(lambda: {'count': 0, 'reasons': Counter()})
    need_fix = 0
    for jf in jpg_files:
        result = classify_jpeg(inspect_jpeg(jf))
        bucket = buckets[result['action']]
        bucket['count'] += 1
        for reason in result['reasons']:
            bucket['reasons'][reason.split('(')[0]] += 1   # 去掉括號內數字做彙總
        if result['needs_fix']:
            need_fix += 1

    print(f"  🖼️ JPG 體檢: 共 {len(jpg_files)} 張，建議修正 {need_fix} 張 (模式: {JPEG_FIX_MODE})")
    for action, info in sorted(buckets.items(), key=lambda kv: -kv[1]['count']):
        if action == 'skip':
            continue
        reason_str = ', '.join(f'{k}×{v}' for k, v in info['reasons'].most_common())
        print(f"     └─ [{action}] {info['count']} 張  ({reason_str})")


def _optimize_lossless(jpeg_bytes):
    """A：mozjpeg 無損最佳化（零像素變化）；套件未安裝回 None。"""
    if _mlo is None:
        return None
    try:
        return _mlo.optimize(jpeg_bytes)
    except Exception:
        return None


def _recompress_bytes(img_path, quality, subsampling):
    """B：以目標品質/抽樣重新編碼，再接一次無損擠壓，回傳新位元組。"""
    with Image.open(img_path) as im:
        if im.mode != 'RGB':
            im = im.convert('RGB')
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=quality, subsampling=subsampling,
                optimize=True, progressive=True)
    out = buf.getvalue()
    squeezed = _optimize_lossless(out)
    return squeezed if (squeezed and len(squeezed) < len(out)) else out


def jpeg_fix_worker(args):
    """Worker：依路由對單張 JPG 做 A/B 處理，僅在變小時替換並繼承時間。"""
    img_path_str, route, quality, subsampling = args
    img_path = Path(img_path_str)
    try:
        orig_stat = img_path.stat()
        if route == 'B':
            new_bytes = _recompress_bytes(img_path, quality, subsampling)
        else:   # A 無損
            with open(img_path, 'rb') as f:
                new_bytes = _optimize_lossless(f.read())

        if new_bytes and len(new_bytes) < orig_stat.st_size:
            with open(img_path, 'wb') as f:
                f.write(new_bytes)
            try:
                os.utime(img_path, (orig_stat.st_atime, orig_stat.st_mtime))
            except Exception:
                pass
            return True
        return False
    except Exception:
        return False


def process_jpegs_auto(jpg_files, pool):
    """auto 模式：逐檔體檢分流到 A(無損)/B(重壓)，並行處理，回傳實際替換張數。"""
    if not jpg_files:
        return 0

    tasks = []
    diag = Counter()
    for jf in jpg_files:
        result = classify_jpeg(inspect_jpeg(jf))
        action = result['action']
        if not result['needs_fix'] or action == 'skip':
            diag['skip'] += 1
            continue
        # downscale 目前刻意不做 → 非重壓類一律走 A 無損
        route = 'B' if 'recompress' in action else 'A'
        diag[route] += 1
        tasks.append((str(jf), route, JPEG_TARGET_QUALITY, JPEG_TARGET_SUBSAMPLING))

    if not tasks:
        return 0

    futures = [pool.submit(jpeg_fix_worker, t) for t in tasks]
    fixed = sum(1 for f in as_completed(futures) if f.result())
    print(
        f"  🖼️ JPG 修正: A無損×{diag['A']} / B重壓×{diag['B']} / 跳過×{diag['skip']} "
        f"→ 實際變小並替換 {fixed} 張"
    )
    return fixed


def slim_single_archive(archive_path, pool, temp_work_base):
    """處理單一壓縮包：解壓 → 轉檔 → 重打包 → 驗收替換。"""
    safe_name = clean_str(os.path.basename(archive_path))
    orig_stat = os.stat(archive_path)
    orig_total_bytes = orig_stat.st_size

    # 使用短路徑暫存區，徹底解決 260 字元長度限制
    with tempfile.TemporaryDirectory(dir=temp_work_base) as temp_dir:
        temp_dir_path = Path(temp_dir)

        # 1. 高速解壓
        start_ex = time.time()
        success, err_reason = extract_archive_to_dir(archive_path, temp_dir)
        if not success:
            print(f"❌ 解壓失敗或格式不受支援 [{safe_name}] 喵！")
            if err_reason:
                print(f"   └─ 🔍 錯誤詳情: {err_reason}")
            return

        all_extracted_files = [f for f in temp_dir_path.glob("**/*") if f.is_file()]
        png_files = [f for f in all_extracted_files if f.suffix.lower() == '.png']
        jpg_files = [f for f in all_extracted_files if f.suffix.lower() in JPEG_EXTENSIONS]

        input_item_count = len(all_extracted_files)

        # JPG 逐檔體檢 / 修正
        jpg_fixed = 0
        if JPEG_FIX_MODE == 'report':
            report_jpeg_health(all_extracted_files)
        elif JPEG_FIX_MODE == 'auto':
            jpg_fixed = process_jpegs_auto(jpg_files, pool)

        if not png_files and jpg_fixed == 0:
            print(f"⏭️ 跳過：無可瘦身內容（PNG 或可修正 JPG）[{safe_name}] 喵！")
            return

        # 2. 派工器分派 PNG 轉檔任務（若有）
        converted_cnt = 0
        if png_files:
            tasks = [(str(png_path), QUALITY) for png_path in png_files]
            futures = [pool.submit(convert_single_image_worker, task) for task in tasks]
            converted_cnt = sum(1 for f in as_completed(futures) if f.result())

        # 3. 重新打包成 ZIP
        base_filename, _ = os.path.splitext(archive_path)
        ideal_zip_path = f"{base_filename}.zip"
        final_zip_path = get_safe_output_path(ideal_zip_path, current_archive_path=archive_path)
        temp_output_zip = f"{base_filename}_temp_processing.zip"

        final_files_to_pack = [f for f in temp_dir_path.glob("**/*") if f.is_file()]
        output_item_count = len(final_files_to_pack)

        with zipfile.ZipFile(temp_output_zip, 'w', zipfile.ZIP_DEFLATED) as zip_out:
            for file_p in final_files_to_pack:
                arcname = str(file_p.relative_to(temp_dir_path))
                zinfo = zipfile.ZipInfo.from_file(file_p, arcname=arcname)
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                with open(file_p, 'rb') as f_in:
                    zip_out.writestr(zinfo, f_in.read())

        new_total_bytes = os.path.getsize(temp_output_zip)
        savings_ratio = (1 - new_total_bytes / orig_total_bytes) * 100 if orig_total_bytes > 0 else 0
        total_time = time.time() - start_ex

        print(
            f"  📊 核對: 原有 {input_item_count} 檔 ──> 產出 {output_item_count} 檔 "
            f"(處理 {converted_cnt} 張 PNG + 修正 {jpg_fixed} 張 JPG，總耗時 {total_time:.1f} 秒)"
        )

        # 4. 驗收、時間繼承與替換
        if input_item_count != output_item_count:
            print(f"  ❌ 警告：數量不吻合 ({input_item_count} != {output_item_count})！保護原檔 {safe_name}")
            if os.path.exists(temp_output_zip):
                safe_remove(temp_output_zip)
            return

        print("  ✅ 驗收通過：數量吻合喵！")
        print(
            f"  🚀 瘦身成效: {format_mb_or_gb(orig_total_bytes / (1024 * 1024))} -> "
            f"{format_mb_or_gb(new_total_bytes / (1024 * 1024))} (砍掉 -{savings_ratio:.1f}%)"
        )

        if os.path.exists(final_zip_path):
            safe_remove(final_zip_path)

        os.rename(temp_output_zip, final_zip_path)

        try:
            os.utime(final_zip_path, (orig_stat.st_atime, orig_stat.st_mtime))
            print("  🕒 已將【壓縮包外層】與【內部所有圖片】的修改時間還原與原檔完全一致喵！")
        except Exception as e:
            print(f"  ⚠️ 修改時間寫回失敗: {e}")

        print(f"  🏷️ 已產出最終檔: {clean_str(os.path.basename(final_zip_path))}")

        if AUTO_DELETE_ORIGINAL and os.path.abspath(archive_path) != os.path.abspath(final_zip_path):
            if os.path.exists(archive_path):
                safe_remove(archive_path)
                print(f"  🗑️ 已安全清理原始檔: {safe_name}")


def main():
    target_path = Path(TARGET_DIR).resolve()
    if not target_path.exists():
        print(f"❌ 錯誤：目標目錄 [{TARGET_DIR}] 不存在喵！")
        return

    all_files = [
        f for f in target_path.glob("*")
        if f.is_file() and f.suffix.lower() in ARCHIVE_EXTENSIONS
        and not f.name.endswith('_temp_processing.zip')
    ]

    if not all_files:
        print(f"❌ [{TARGET_DIR}] 下沒有找到任何壓縮檔喵！")
        return

    # 建立短路徑暫存資料夾
    temp_work_base = target_path / "_temp_work"
    temp_work_base.mkdir(parents=True, exist_ok=True)

    total_cpus = os.cpu_count() or 8
    print("========================================")
    print(f"🐱 找到 {len(all_files)} 個壓縮包，啟動【短路徑破解 + 詳細除錯版】[resize.py]...")
    print(f"🚀 總核心數: {total_cpus} | 系統保留: 4 核心 | 轉檔 WorkPool: {MAX_WORKERS} Workers")
    if JPEG_FIX_MODE == 'auto':
        mlo_state = '可用' if _mlo else '未安裝（A 無損將略過，B 僅用 PIL 重壓）'
        print(f"🖼️ JPG 修正模式: auto | mozjpeg 無損套件: {mlo_state}")
    elif JPEG_FIX_MODE == 'report':
        print("🖼️ JPG 修正模式: report（只體檢、不改檔）")
    print("========================================\n")

    try:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for index, file_path in enumerate(all_files, start=1):
                safe_name = clean_str(file_path.name)
                print(f"▶ [{index}/{len(all_files)}] 正在處理: {safe_name}")
                slim_single_archive(str(file_path), pool, temp_work_base)
                print("-" * 50)
    finally:
        # 任務結束後順手清理短路徑暫存資料夾
        if temp_work_base.exists():
            shutil.rmtree(temp_work_base, ignore_errors=True)

    print("\n========================================")
    print("🎉 U:\\resize 下的所有壓縮包已完成極速轉檔喵！(ฅ'ω'ฅ)")
    print("========================================")


if __name__ == '__main__':
    main()
