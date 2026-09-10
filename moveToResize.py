"""階段二：互動式搬移工具

讀取排行榜快取（或現場關鍵字精算），讓使用者挑選要處理的標籤，
把達標的壓縮包搬到瘦身工作區，並可接續啟動轉檔腳本喵！
"""
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from config import (
    SOURCE_DIR, TARGET_DIR, LOG_FILE, TARGETS_CACHE, HASH_CACHE_FILE,
    RESIZE_SCRIPT_NAME, MIN_ARCHIVE_SIZE_MB, ESTIMATED_REDUCTION_RATE,
    ESTIMATED_JPG_REDUCTION_RATE, JPEG_PROBLEM_RATIO, ARCHIVE_EXTENSIONS,
)
from common_utils import (
    clean_str, format_mb_or_gb, extract_all_tags, load_processed_files,
    save_clean_file, load_targets_cache, load_hash_cache, save_hash_cache,
    get_archive_image_stats, stats_png_ratio, stats_large_jpg_ratio,
    is_slim_target, get_safe_destination,
)


def list_archives(src_path):
    """列出來源資料夾下所有支援的壓縮檔。"""
    return [
        f for f in src_path.rglob("*")
        if f.is_file() and f.suffix.lower() in ARCHIVE_EXTENSIONS
    ]


def clear_all_caches():
    """一鍵徹底清除所有舊快取檔（黑名單、Hash 快取、排行榜）。"""
    cleared = []
    for cache_path in (LOG_FILE, HASH_CACHE_FILE, TARGETS_CACHE):
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
                cleared.append(cache_path.name)
            except Exception:
                pass

    if cleared:
        print(f"🧹 已成功徹底清除快取檔: {', '.join(cleared)}！下一次掃描將進行 100% 全新精算喵！")
    else:
        print("💡 目前沒有任何快取檔可清除喵！")


def smart_analyze_keyword_on_the_fly(kw, src_path, processed_files, hash_cache):
    """現場針對關鍵字即時精算，回傳使用者選定要搬移的標籤清單。"""
    kw_clean = kw.strip().lower()

    print(f"\n🔍 正在全域對 [{SOURCE_DIR}] 進行即時檔頭精算（關鍵字: 「{kw}」）...")

    all_files = list_archives(src_path)

    tag_stats = defaultdict(lambda: {
        'display_name': '',
        'sample_file': '',
        'qualified_count': 0,
        'archive_total_mb': 0.0,
        'est_png_disk_mb': 0.0,
        'est_jpg_disk_mb': 0.0,
        'est_saved_disk_mb': 0.0,
    })

    matched_files_count = 0

    for file_path in all_files:
        if (file_path.name.lower() in processed_files
                or file_path.name.lower() == LOG_FILE.name.lower()):
            continue

        tags = extract_all_tags(file_path.name)
        matched_tags = [t for t in tags if kw_clean in t.lower()]

        # 標籤沒命中，但關鍵字出現在檔名裡，也視為命中
        if not matched_tags and kw_clean in file_path.name.lower():
            matched_tags = [kw.strip()]

        if not matched_tags:
            continue

        archive_mb = file_path.stat().st_size / (1024 * 1024)
        if archive_mb < MIN_ARCHIVE_SIZE_MB:
            continue

        stats_info, _ = get_archive_image_stats(file_path, hash_cache)
        is_success = stats_info['success']
        png_ratio = stats_png_ratio(stats_info)
        large_jpg_ratio = stats_large_jpg_ratio(stats_info)

        # 只有「PNG 與過大 JPG 都沒有」才列黑名單
        if is_success and png_ratio == 0 and stats_info['large_jpg_bytes'] == 0:
            save_clean_file(file_path.name)
            continue

        target = is_slim_target(stats_info)
        matched_files_count += 1

        for raw_tag in matched_tags:
            tag_key = raw_tag.lower()
            stats = tag_stats[tag_key]
            if not stats['display_name']:
                stats['display_name'] = raw_tag
            if not stats['sample_file']:
                stats['sample_file'] = file_path.name

            png_disk_mb = archive_mb * png_ratio
            jpg_disk_mb = archive_mb * large_jpg_ratio if large_jpg_ratio >= JPEG_PROBLEM_RATIO else 0.0
            saved_disk_mb = (png_disk_mb * ESTIMATED_REDUCTION_RATE
                             + jpg_disk_mb * ESTIMATED_JPG_REDUCTION_RATE)

            stats['archive_total_mb'] += archive_mb
            stats['est_png_disk_mb'] += png_disk_mb
            stats['est_jpg_disk_mb'] += jpg_disk_mb
            stats['est_saved_disk_mb'] += saved_disk_mb

            if target:
                stats['qualified_count'] += 1

    save_hash_cache(hash_cache)

    if not tag_stats:
        print(f"❌ 現場沒有找到任何包含關鍵字「{kw}」且符合瘦身條件的壓縮包喵！")
        return None

    results = sorted(tag_stats.values(), key=lambda x: x['est_saved_disk_mb'], reverse=True)

    print("-" * 75)
    print(f"📊 實時分析完畢！找到 {len(results)} 個相關標籤 (共 {matched_files_count} 個目標壓縮包)：")
    print("-" * 75)

    for idx, r in enumerate(results, start=1):
        safe_tag = clean_str(r['display_name'])
        total_mb_str = format_mb_or_gb(r['archive_total_mb'])
        png_mb_str = format_mb_or_gb(r['est_png_disk_mb'])
        jpg_mb_str = format_mb_or_gb(r['est_jpg_disk_mb'])
        save_mb_str = format_mb_or_gb(r['est_saved_disk_mb'])
        print(
            f" [{idx}] [{safe_tag}] ── 達標包: {r['qualified_count']} 個 | "
            f"PNG: {png_mb_str} | 過大JPG: {jpg_mb_str} | "
            f"預估可空出: ~{save_mb_str} (檔案總重 {total_mb_str})"
        )
        if r['sample_file']:
            print(f"        └─ 📄 {clean_str(r['sample_file'])}")

    print("-" * 75)
    print(" [A] 搬移上述分析到的【全部符合標籤】")
    print(" [1~N] 選擇上述單一標籤")
    print("-" * 75)

    sub_choice = input("👉 請選擇要執行的選項: ").strip().upper()
    if sub_choice == 'A':
        return [r['display_name'] for r in results if r['qualified_count'] > 0]
    if sub_choice.isdigit():
        s_idx = int(sub_choice)
        if 1 <= s_idx <= len(results):
            return [results[s_idx - 1]['display_name']]

    return None


def remove_tags_from_targets_cache(moved_tags):
    """搬移完成後，只從排行榜快取移除已處理的標籤，其餘保留（全部清空才刪檔）。"""
    if not moved_tags or not os.path.exists(TARGETS_CACHE):
        return
    targets = load_targets_cache()
    if not targets:
        return

    moved_lower = {t.lower() for t in moved_tags}
    remaining = [t for t in targets if t.get('tag', '').lower() not in moved_lower]
    removed = len(targets) - len(remaining)
    if removed == 0:
        return

    try:
        if remaining:
            # 重新編號 rank，讓選單維持連續
            for new_rank, item in enumerate(remaining, start=1):
                item['rank'] = new_rank
            with open(TARGETS_CACHE, 'w', encoding='utf-8') as f:
                json.dump(remaining, f, ensure_ascii=False, indent=2)
        else:
            os.remove(TARGETS_CACHE)
        print(f"🧾 已從排行榜快取移除 {removed} 個已處理標籤，其餘 {len(remaining)} 個保留喵！")
    except Exception as e:
        print(f"⚠️ 更新排行榜快取失敗: {e}")


def move_files_for_pattern(src_path, dst_path, processed_files, hash_cache, search_regex,
                           target_kw=None, selected_tags=None):
    """依標籤正則（或全域）搬移達標的壓縮包到目標資料夾。"""
    all_files = list_archives(src_path)

    moved_count, skipped_count, total_moved_mb = 0, 0, 0

    for file_path in all_files:
        if (file_path.name.lower() == LOG_FILE.name.lower()
                or file_path.name.lower() in processed_files):
            skipped_count += 1
            continue

        archive_mb = file_path.stat().st_size / (1024 * 1024)
        if archive_mb < MIN_ARCHIVE_SIZE_MB:
            skipped_count += 1
            continue

        # 標籤比對（search_regex 為 None 代表全域搬移，不做標籤過濾）
        if search_regex:
            tags = extract_all_tags(file_path.name)
            matched = any(search_regex.search(f"[{t}]") for t in tags)
            if not matched and target_kw and target_kw.lower() in file_path.name.lower():
                matched = True
            if not matched:
                skipped_count += 1
                continue

        stats_info, _ = get_archive_image_stats(file_path, hash_cache)

        if is_slim_target(stats_info):
            safe_dst_path = get_safe_destination(dst_path / file_path.name)
            safe_name = clean_str(file_path.name)
            try:
                shutil.move(str(file_path), str(safe_dst_path))
                moved_count += 1
                total_moved_mb += archive_mb
                print(f"🚚 成功搬移: {safe_name} ({format_mb_or_gb(archive_mb)})")
            except Exception as e:
                print(f"❌ 搬移失敗 [{safe_name}]: {e}\n")
        else:
            skipped_count += 1

    save_hash_cache(hash_cache)

    print("\n" + "=" * 65)
    print(f"🎉 搬移完成！成功搬移 {moved_count} 個檔案 (已移交總體積: {format_mb_or_gb(total_moved_mb)}) 喵！")
    print("=" * 65)

    if moved_count > 0:
        remove_tags_from_targets_cache(selected_tags)
        trigger_resize_prompt()


def trigger_resize_prompt():
    """搬移完成後，詢問是否接著啟動轉檔腳本。"""
    target_script = Path(RESIZE_SCRIPT_NAME)

    print("\n" + "=" * 65)
    if not target_script.exists():
        print(f"💡 提醒：未在當前目錄找到瘦身腳本 [{RESIZE_SCRIPT_NAME}] 喵！")
        print("=" * 65)
        return

    user_input = input(
        f"❓ 搬移已完成！是否要立即啟動瘦身腳本 [{target_script.name}] 進行轉檔？(y/N): "
    ).strip().lower()
    if user_input == 'y':
        print(f"\n🚀 正在啟動 {target_script.name} ...\n" + "=" * 65 + "\n")
        try:
            subprocess.run([sys.executable, str(target_script)])
        except Exception as e:
            print(f"❌ 執行瘦身腳本失敗: {e} 喵！")
    else:
        print("💡 已跳過自動瘦身，後續可自行手動執行 resize.py 喵！")
    print("=" * 65)


def print_menu(targets):
    """印出主選單（含排行榜快取內容，若有）。"""
    if targets:
        print("📋 【從 analysis.py 載入的排行榜選單】：")
        print("-" * 75)
        for t in targets:
            tag_name = clean_str(t['tag'])
            png_str = format_mb_or_gb(t['png_mb'])
            jpg_str = format_mb_or_gb(t.get('jpg_mb', 0))
            save_str = format_mb_or_gb(t['est_saved_mb'])
            print(f" [{t['rank']}] [{tag_name}] ── PNG: {png_str} | 過大JPG: {jpg_str} | 預估可省 ~{save_str}")
            if t.get('sample'):
                print(f"        └─ 📄 {clean_str(t['sample'])}")
    else:
        print("💡 提醒：目前無排行榜快取 (不影響搜尋，可直接輸入 [F] 進行即時精算) 喵！")

    print("-" * 75)
    if targets:
        print(" [A] 搬移上述 Top 排行榜【全部標籤】")
    print(" [F] 關鍵字搜尋 (發動即時精算/Hash快取)")
    print(" [0] 全域掃描所有符合條件的檔案")
    print(" [M] 手動輸入標籤進行精準比對")
    print(" [C] 徹底清除所有舊快取檔 (包含黑名單與舊 Hash 快取)")
    print(" [Q] 離開")
    print("-" * 75)


def resolve_selection(user_choice, targets, src_path, processed_files, hash_cache):
    """把使用者選項解析成 (selected_tags, target_kw)；回傳 None 代表本輪不執行搬移。"""
    if user_choice == 'A':
        if not targets:
            print("⚠️ 目前沒有排行榜快取，請改用 [F] 關鍵字搜尋喵！")
            return None
        selected_tags = [t['tag'] for t in targets]
        print(f"\n🚀 已選擇搬移 Top {len(selected_tags)} 全榜標籤喵！")
        return selected_tags, None

    if user_choice == 'F':
        kw = input("\n👉 請輸入要搜尋精算的標籤關鍵字 (例如: Kurohime): ").strip()
        if not kw:
            print("⚠️ 未輸入關鍵字喵！")
            return None
        res_tags = smart_analyze_keyword_on_the_fly(kw, src_path, processed_files, hash_cache)
        if not res_tags:
            return None
        print(f"\n🚀 已精準鎖定選擇 {len(res_tags)} 個標籤進行搬移喵！")
        return res_tags, kw

    if user_choice == '0':
        print("\n🚀 已選擇全域檢查搬移喵！")
        return [], None

    if user_choice == 'M':
        raw_in = input("👉 請輸入要比對的標籤文字: ").strip()
        if not raw_in:
            print("⚠️ 未輸入標籤喵！")
            return None
        return [raw_in], None

    if user_choice.isdigit():
        if not targets:
            print("❌ 當前無排行榜選單，請選擇 [F] 輸入關鍵字進行搜尋喵！")
            return None
        idx = int(user_choice)
        if not (1 <= idx <= len(targets)):
            print("❌ 無效的數字選項喵！")
            return None
        selected_tags = [targets[idx - 1]['tag']]
        print(f"\n🚀 已選擇標籤: [{selected_tags[0]}] 喵！")
        return selected_tags, None

    print("❌ 輸入無效喵！")
    return None


def main():
    src_path = Path(SOURCE_DIR).resolve()
    dst_path = Path(TARGET_DIR).resolve()

    if not src_path.exists():
        print(f"❌ 錯誤：來源目錄 [{SOURCE_DIR}] 不存在喵！")
        return

    dst_path.mkdir(parents=True, exist_ok=True)
    processed_files = load_processed_files()
    hash_cache = load_hash_cache()

    print("========================================")
    print("🐱 互動式搬移工具 - 檔名 Hash 快取雙引擎版 (moveToResize.py) 喵！")
    print("========================================\n")

    # 主選單迴圈：每次動作結束後回到選單，直到選 [Q] 離開
    while True:
        targets = load_targets_cache()   # 每輪重讀，反映搬移後已移除的標籤
        print_menu(targets)

        user_choice = input("👉 請選擇要執行的選項: ").strip().upper()

        if user_choice == 'Q':
            print("👋 掰掰喵！(ฅ'ω'ฅ)")
            break

        if user_choice == 'C':
            clear_all_caches()
            print()
            continue

        selection = resolve_selection(user_choice, targets, src_path, processed_files, hash_cache)
        if selection is None:
            print()
            continue

        selected_tags, target_kw = selection

        if selected_tags:
            escaped_patterns = [
                r'[\(【\[\（]\s*' + re.escape(t) + r'\s*[\)】\]\）]'
                for t in selected_tags
            ]
            search_regex = re.compile(f"({'|'.join(escaped_patterns)})", re.IGNORECASE)
        else:
            search_regex = None

        move_files_for_pattern(src_path, dst_path, processed_files, hash_cache,
                               search_regex, target_kw, selected_tags)
        print()   # 回到選單前空一行


if __name__ == '__main__':
    main()
