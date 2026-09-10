"""階段三：實際轉檔瘦身

對工作區內每個壓縮包解壓 → 將內部 PNG 轉為 JPG → 重新打包成 ZIP，
以「檔案數量吻合」為驗收條件，通過才替換原檔並繼承修改時間喵！
（多核並行；使用短路徑暫存區規避 Windows 260 字元路徑限制。）
"""
import argparse
import io
import json
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
    TARGET_DIR, FAILED_DIR, DONE_DIR, QUALITY, AUTO_DELETE_ORIGINAL, OVERWRITE_EXISTING_ZIP,
    MAX_WORKERS, ARCHIVE_EXTENSIONS, JPEG_EXTENSIONS, JPEG_FIX_MODE,
    JPEG_TARGET_QUALITY, JPEG_TARGET_SUBSAMPLING,
    JPEG_FLAT_QUALITY, JPEG_FLAT_COLOR_RATIO, JPEG_CONTENT_SAMPLE_SIZE,
    MIN_ARCHIVE_SAVING_RATIO, SKIP_ALREADY_DONE_ARCHIVES,
)
from common_utils import (
    clean_str, format_mb_or_gb, SEVEN_ZIP_PATH, get_safe_destination, get_file_hash_key,
    zip_has_work, settings_signature, load_lowgain_flags, save_lowgain_flags, flag_key,
)
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


def _list_7z_members(archive_path):
    """用 7z l 列出成員名稱；回傳 (是否可開啟, 成員名稱 list)。"""
    if not SEVEN_ZIP_PATH:
        return False, []
    try:
        res = subprocess.run(
            [SEVEN_ZIP_PATH, "l", "-slt", str(archive_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='ignore',
        )
        if res.returncode != 0:
            return False, []
        members = []
        is_dir = False
        current = None
        for line in res.stdout.splitlines():
            line = line.rstrip()
            if line.startswith('Path = '):
                current = line[7:]
                is_dir = False
            elif line.startswith('Attributes = '):
                is_dir = 'D' in line[13:].upper().split()[0] if line[13:].strip() else False
            elif line == '' and current:
                if not is_dir:
                    members.append(current)
                current = None
        if current and not is_dir:
            members.append(current)
        # 第一筆 Path 是壓縮檔自身，去掉
        if members and os.path.basename(members[0]) == os.path.basename(str(archive_path)):
            members = members[1:]
        return True, members
    except Exception:
        return False, []


def _extract_zip_per_member(archive_path, extract_dst):
    """逐成員解壓 zip，精準取得失敗清單。

    回傳 (status, failed_members, err_msg)
    """
    try:
        zf = zipfile.ZipFile(archive_path, 'r')
    except Exception as e:
        return 'unopenable', [], f"zipfile 無法開啟壓縮檔: {type(e).__name__}: {e}"

    failed = []
    try:
        with zf:
            for item in zf.infolist():
                if item.is_dir():
                    continue
                try:
                    zf.extract(item, extract_dst)
                except Exception as e:
                    failed.append((item.filename, f"{type(e).__name__}: {e}"))
    except Exception as e:
        return 'unopenable', [], f"zipfile 讀取成員清單失敗: {type(e).__name__}: {e}"

    if failed:
        # 失敗數量已寫在說明檔標頭，這裡不重複
        return 'partial', failed, ""
    return 'ok', [], ""


def extract_archive_to_dir(archive_path, extract_dst):
    """解壓壓縮包，並區分「整包打不開」與「部分內容物失敗」。

    回傳 (status, failed_members, err_msg)
      status: 'ok'         全部成功
              'partial'    整包可開，但有內容物解壓失敗（failed_members 有明細）
              'unopenable' 整個壓縮檔打不開（沒有內容物明細可言）
              'no_tool'    缺少 7-Zip 而無法處理此格式（檔案本身可能完好）
    """
    ext = os.path.splitext(archive_path)[1].lower()
    err_msg = ""

    # 策略 1: 7-Zip（速度快、格式支援廣）
    if SEVEN_ZIP_PATH:
        try:
            cmd = [SEVEN_ZIP_PATH, "x", str(archive_path), f"-o{extract_dst}", "-y"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, errors='ignore')
            if res.returncode == 0:
                return 'ok', [], ""
            err_msg = f"7-Zip 解壓失敗 (Code {res.returncode}): {res.stderr.strip() or res.stdout.strip()}"
        except Exception as e:
            err_msg = f"呼叫 7-Zip 異常: {e}"

    # 策略 2: zip 交給 Python zipfile 逐成員處理，可取得精準失敗清單
    if ext == '.zip':
        status, failed, zip_err = _extract_zip_per_member(archive_path, extract_dst)
        combined = " | ".join(x for x in (err_msg, zip_err) if x)
        return status, failed, combined

    # 非 zip 格式只能靠 7-Zip；工具不存在時無法斷定檔案好壞，不可當成壞檔
    if not SEVEN_ZIP_PATH:
        return 'no_tool', [], f"未安裝 7-Zip，無法處理 {ext} 格式"

    # 非 zip 格式：用 7z 是否列得出成員，判斷是「整包打不開」還是「部分失敗」
    openable, members = _list_7z_members(archive_path)
    if not openable:
        return 'unopenable', [], err_msg or "壓縮檔無法開啟或格式不受支援"

    # 可列出成員 → 屬部分失敗；用「清單 vs 實際落地」比對缺漏的成員
    landed = set()
    for root, _, files in os.walk(extract_dst):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), extract_dst)
            landed.add(rel.replace('\\', '/'))
    failed = [(m, "未成功解出（7-Zip 回報錯誤）")
              for m in members if m.replace('\\', '/') not in landed]
    if not failed:
        failed = [("(無法逐一辨識)", "7-Zip 回報錯誤，但所有成員皆已落地，內容可能毀損")]
    return 'partial', failed, err_msg


def move_to_failed_dir(archive_path):
    """把解壓失敗的壓縮包搬到 FAILED_DIR 保留待查（同名自動加序號）。

    回傳實際搬移後的路徑（失敗時回傳 None），讓說明檔能以相同檔名配對。
    """
    try:
        failed_base = Path(FAILED_DIR)
        failed_base.mkdir(parents=True, exist_ok=True)
        dst_path = get_safe_destination(failed_base / Path(archive_path).name)
        shutil.move(str(archive_path), str(dst_path))
        print(f"   ├─ 📦 已搬至失敗區: {clean_str(str(dst_path))}")
        return dst_path
    except Exception as e:
        print(f"   └─ ⚠️ 搬移到失敗區 [{FAILED_DIR}] 時出錯: {e}")
        return None


def move_to_done_dir(final_path):
    """把處理完成的壓縮包搬到 DONE_DIR，讓工作區只留下還沒處理的東西。

    同名時沿用 get_safe_destination（加序號、永不覆蓋）。
    搬移後補回修改時間，維持「時間與原檔一致」的承諾。
    搬移失敗不視為錯誤——檔案已經處理好了，留在原地即可。
    """
    if not DONE_DIR:
        return final_path
    try:
        src = Path(final_path)
        done_base = Path(DONE_DIR)
        if done_base.resolve() == src.parent.resolve():
            return final_path          # 已經在完成區，不用搬

        done_base.mkdir(parents=True, exist_ok=True)
        st = src.stat()
        dst = get_safe_destination(done_base / src.name)
        shutil.move(str(src), str(dst))
        try:
            os.utime(dst, (st.st_atime, st.st_mtime))
        except Exception:
            pass
        renamed = '' if dst.name == src.name else f"（同名已存在，改名為 {clean_str(dst.name)}）"
        print(f"  📁 已搬至完成區: {clean_str(str(dst))}{renamed}")
        return str(dst)
    except Exception as e:
        print(f"  ⚠️ 搬到完成區 [{DONE_DIR}] 失敗，檔案保留原地: {e}")
        return final_path


def write_failed_report(dst_path, failed_members, err_msg):
    """在失敗區寫一份與壓縮包同名的 .txt，記錄哪些內容物解壓失敗。"""
    report_path = Path(str(dst_path) + '.txt')
    try:
        lines = [
            "解壓失敗內容物清單",
            "=" * 60,
            f"壓縮檔  : {clean_str(dst_path.name)}",
            f"處理時間: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"失敗數量: {len(failed_members)} 個內容物",
            "=" * 60,
            "",
        ]
        for name, reason in failed_members:
            lines.append(f"[失敗] {clean_str(name)}")
            lines.append(f"       原因: {clean_str(reason)}")
            lines.append("")
        if err_msg:
            lines += ["-" * 60, "原始錯誤詳情:", clean_str(err_msg), ""]

        report_path.write_text("\n".join(lines), encoding='utf-8', errors='replace')
        print(f"   └─ 📝 已寫出失敗清單: {clean_str(report_path.name)}")
        return True
    except Exception as e:
        print(f"   └─ ⚠️ 寫出失敗清單時出錯: {e}")
        return False


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


def color_ratio(im, size=JPEG_CONTENT_SAMPLE_SIZE):
    """相異顏色數 / 總像素，用來分辨平塗與寫實內容。

    以 NEAREST 取樣成小圖再數顏色——NEAREST 不會內插出新顏色，
    所以平塗的「顏色少」這個特性得以保留。實測真實照片 36~74%、
    平塗類 0~9%，中間空隙很大。約 5ms/張，相對轉檔成本可忽略。
    """
    w, h = im.size
    side = min(w, h)
    box = ((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)
    small = im.resize((size, size), Image.NEAREST, box=box)
    colors = small.getcolors(size * size) or []
    return len(colors) / float(size * size)


def pick_quality_for(im):
    """依內容挑目標品質，回傳 (品質, 顏色數佔比, 類型標籤)。"""
    ratio = color_ratio(im)
    if ratio < JPEG_FLAT_COLOR_RATIO:
        return JPEG_FLAT_QUALITY, ratio, 'flat'
    return JPEG_TARGET_QUALITY, ratio, 'photo'


def _encode(im, quality, subsampling):
    """以指定品質/抽樣編碼，再接一次無損擠壓，回傳較小的那份。"""
    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=quality, subsampling=subsampling,
            optimize=True, progressive=True)
    out = buf.getvalue()
    squeezed = _optimize_lossless(out)
    return squeezed if (squeezed and len(squeezed) < len(out)) else out


def _recompress_bytes(img_path, quality, subsampling):
    """B：以指定品質重壓，回傳新位元組。"""
    with Image.open(img_path) as im:
        if im.mode != 'RGB':
            im = im.convert('RGB')
        return _encode(im, quality, subsampling)


def _recompress_auto(img_path, subsampling):
    """B：依內容自動挑品質後重壓，回傳 (位元組, 品質, 類型)。"""
    with Image.open(img_path) as im:
        if im.mode != 'RGB':
            im = im.convert('RGB')
        quality, _ratio, kind = pick_quality_for(im)
        return _encode(im, quality, subsampling), quality, kind


def jpeg_fix_worker(args):
    """Worker：依路由對單張 JPG 做 A/B 處理，僅在變小時替換並繼承時間。

    回傳 (是否替換, 內容類型)；類型只有走 B 時才有值，用來回報分流統計。
    """
    img_path_str, route, subsampling = args
    img_path = Path(img_path_str)
    kind = None
    try:
        orig_stat = img_path.stat()
        if route == 'B':
            new_bytes, _q, kind = _recompress_auto(img_path, subsampling)
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
            return True, kind
        return False, kind
    except Exception:
        return False, kind


def process_jpegs_auto(jpg_files, pool):
    """auto 模式：逐檔體檢分流到 A(無損)/B(重壓)，並行處理，回傳實際替換張數。"""
    if not jpg_files:
        return 0

    tasks = []
    diag = Counter()
    qdist = Counter()        # 會被處理的來源品質分佈
    qskip = Counter()        # 被跳過的品質分佈（看得出有沒有「還能再壓」的漏網之魚）
    chroma444 = 0            # 其中有幾張是 4:4:4
    first = None             # 第一張要處理的圖，當作具體範例
    for jf in jpg_files:
        report = inspect_jpeg(jf)
        result = classify_jpeg(report)
        action = result['action']
        if not result['needs_fix'] or action == 'skip':
            diag['skip'] += 1
            if report:
                qskip[report.get('est_quality')] += 1
            continue
        # downscale 目前刻意不做 → 非重壓類一律走 A 無損
        route = 'B' if 'recompress' in action else 'A'
        diag[route] += 1
        if report:
            qdist[report.get('est_quality')] += 1
            if report.get('subsampling') == '4:4:4':
                chroma444 += 1
            if first is None:
                first = (jf.name, report, route)
        tasks.append((str(jf), route, JPEG_TARGET_SUBSAMPLING))

    if not tasks:
        return 0

    futures = [pool.submit(jpeg_fix_worker, t) for t in tasks]
    fixed = 0
    for f in as_completed(futures):
        changed, kind = f.result()
        if changed:
            fixed += 1
        if kind:
            diag[kind] += 1

    split = ''
    if diag['flat'] or diag['photo']:
        split = (f" (B 依內容分流: 平塗Q{JPEG_FLAT_QUALITY}×{diag['flat']} / "
                 f"寫實Q{JPEG_TARGET_QUALITY}×{diag['photo']})")
    print(
        f"  🖼️ JPG 修正: A無損×{diag['A']} / B重壓×{diag['B']} / 跳過×{diag['skip']} "
        f"→ 實際變小並替換 {fixed} 張{split}"
    )

    if first:
        name, rep, route = first
        prog = 'progressive' if rep.get('progressive') else 'baseline'
        print(f"     ├─ 📷 首張 [{clean_str(name)}]: {rep['width']}x{rep['height']}, "
              f"Q{rep.get('est_quality')}, {rep.get('subsampling')}, {prog} → 走 {route}")
    if qdist:
        top = ', '.join(f"Q{q}×{n}" for q, n in qdist.most_common(5))
        extra = f"（其中 4:4:4 共 {chroma444} 張）" if chroma444 else ''
        connector = '├─' if qskip else '└─'
        print(f"     {connector} 📊 處理的來源品質: {top}{extra}")
    if qskip:
        top = ', '.join(f"Q{q}×{n}" for q, n in qskip.most_common(5))
        print(f"     └─ 💤 跳過的品質分佈: {top}")
    return fixed


def slim_single_archive(archive_path, pool, temp_work_base, flags=None, recheck=False):
    """處理單一壓縮包：解壓 → 轉檔 → 重打包 → 驗收替換。

    flags 為「省太少而放棄」的標記表；已標記者直接跳過，不做任何解壓，
    除非 recheck=True 要求重新評估。
    """
    safe_name = clean_str(os.path.basename(archive_path))
    orig_stat = os.stat(archive_path)
    orig_total_bytes = orig_stat.st_size

    key = flag_key(archive_path) if flags is not None else None
    if key and not recheck and key in flags:
        prev = flags[key].get('ratio')
        note = f"（上次只省 {prev:.1f}%）" if isinstance(prev, (int, float)) else ""
        print(f"⏭️ 已標記為效率不足{note}，跳過 [{safe_name}]；"
              f"想重評請加 --recheck 喵！")
        return

    # 解壓前先抽樣判斷：整包都處理過就直接跳過，省掉整包解壓的 I/O
    # （250MB 的包解壓要 2.6 秒，這裡只要約 2ms）
    if SKIP_ALREADY_DONE_ARCHIVES and not recheck:
        if zip_has_work(Path(archive_path)) is False:
            print(f"⏭️ 抽樣判定整包已處理過，免解壓直接跳過 [{safe_name}] 喵！")
            move_to_done_dir(archive_path)
            return

    # 使用短路徑暫存區，徹底解決 260 字元長度限制
    with tempfile.TemporaryDirectory(dir=temp_work_base) as temp_dir:
        temp_dir_path = Path(temp_dir)

        # 1. 高速解壓
        start_ex = time.time()
        status, failed_members, err_reason = extract_archive_to_dir(archive_path, temp_dir)
        if status != 'ok':
            # 工具缺失是環境問題而非檔案問題：保留原地，不搬進失敗區
            if status == 'no_tool':
                print(f"⏭️ 略過：{err_reason}，檔案保留原地不搬移 [{safe_name}] 喵！")
                return
            if status == 'unopenable':
                print(f"❌ 整個壓縮檔打不開或格式不受支援 [{safe_name}] 喵！")
            else:
                print(f"❌ 有 {len(failed_members)} 個內容物解壓失敗 [{safe_name}] 喵！")
            if err_reason:
                print(f"   ├─ 🔍 錯誤詳情: {err_reason}")

            dst_path = move_to_failed_dir(archive_path)
            # 只有「整包可開、部分內容物壞掉」才寫說明檔；整包打不開就沒有明細可寫
            if dst_path and status == 'partial':
                write_failed_report(dst_path, failed_members, err_reason)
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
            move_to_done_dir(archive_path)
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

        # 省太少就整包放棄：重壓的畫質代價已經付了，換不到空間就不值得替換
        if MIN_ARCHIVE_SAVING_RATIO > 0 and savings_ratio < MIN_ARCHIVE_SAVING_RATIO * 100:
            print(f"  ⏭️ 效率不足：只省下 {savings_ratio:.1f}%"
                  f"（低於門檻 {MIN_ARCHIVE_SAVING_RATIO * 100:.0f}%），放棄替換、保留原檔喵！")
            if os.path.exists(temp_output_zip):
                safe_remove(temp_output_zip)
            # 先搬再標記：標記的 key 是「檔名+大小+時間」，搬到完成區若因同名
            # 被加了序號，就得改用搬移後的檔案重算，否則下一輪認不出來，
            # 這包又會被分析器挑中、搬回來重跑一次。
            done_path = move_to_done_dir(archive_path) or archive_path
            if key is not None and flags is not None:
                final_key = flag_key(done_path) or key
                flags[final_key] = {'name': os.path.basename(done_path),
                                    'ratio': round(savings_ratio, 1),
                                    'sig': settings_signature()}
                print("     └─ 🏷️ 已標記，之後不再重複嘗試（--recheck 可重評）")
            return

        # 這次成功處理了，若先前被標記過就把標記解除
        if key is not None and flags is not None:
            flags.pop(key, None)

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

        move_to_done_dir(final_zip_path)


def show_flags(flags):
    """列出目前被標記為效率不足的壓縮包。"""
    if not flags:
        print("💡 目前沒有任何『效率不足』標記喵！")
        return
    print(f"🏷️ 目前有 {len(flags)} 個壓縮包被標記為效率不足：")
    print("-" * 65)
    for v in sorted(flags.values(), key=lambda x: x.get('ratio', 0)):
        print(f"  只省 {v.get('ratio', 0):>5.1f}%   {clean_str(v.get('name', '?'))}")
    print("-" * 65)
    print("💡 加 --recheck 可忽略標記重新評估；--clear-flags 可清空標記")


def main():
    parser = argparse.ArgumentParser(description='壓縮包瘦身工具')
    parser.add_argument('--recheck', action='store_true',
                        help='忽略「效率不足」標記，重新評估所有壓縮包')
    parser.add_argument('--list-flags', action='store_true',
                        help='列出目前被標記為效率不足的壓縮包後結束')
    parser.add_argument('--clear-flags', action='store_true',
                        help='清空所有「效率不足」標記後結束')
    args = parser.parse_args()

    flags = load_lowgain_flags()

    if args.list_flags:
        show_flags(flags)
        return
    if args.clear_flags:
        n = len(flags)
        save_lowgain_flags({})
        print(f"🧹 已清空 {n} 筆『效率不足』標記，下次執行會全部重新評估喵！")
        return

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
    if not SEVEN_ZIP_PATH:
        non_zip = sum(1 for f in all_files if f.suffix.lower() != '.zip')
        print("⚠️ 未找到 7-Zip：.rar / .7z 無法處理，將『原地略過、不搬移』")
        if non_zip:
            print(f"   └─ 本次有 {non_zip} 個非 zip 壓縮包會被略過，裝好 7-Zip 後再跑即可")
    if JPEG_FIX_MODE == 'auto':
        mlo_state = '可用' if _mlo else '未安裝（A 無損將略過，B 僅用 PIL 重壓）'
        print(f"🖼️ JPG 修正模式: auto | mozjpeg 無損套件: {mlo_state}")
    elif JPEG_FIX_MODE == 'report':
        print("🖼️ JPG 修正模式: report（只體檢、不改檔）")
    if args.recheck:
        print(f"♻️ --recheck：忽略 {len(flags)} 筆效率不足標記，全部重新評估")
    elif flags:
        print(f"🏷️ 已標記效率不足: {len(flags)} 筆（會直接跳過；--recheck 可重評）")
    print("========================================\n")

    try:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for index, file_path in enumerate(all_files, start=1):
                safe_name = clean_str(file_path.name)
                print(f"▶ [{index}/{len(all_files)}] 正在處理: {safe_name}")
                slim_single_archive(str(file_path), pool, temp_work_base,
                                    flags=flags, recheck=args.recheck)
                print("-" * 50)
    finally:
        # 任務結束後順手清理短路徑暫存資料夾
        if temp_work_base.exists():
            shutil.rmtree(temp_work_base, ignore_errors=True)
        save_lowgain_flags(flags)

    print("\n========================================")
    print("🎉 U:\\resize 下的所有壓縮包已完成極速轉檔喵！(ฅ'ω'ฅ)")
    if flags:
        print(f"🏷️ 目前累計 {len(flags)} 筆效率不足標記（--list-flags 可查看）")
    print("========================================")


if __name__ == '__main__':
    main()
