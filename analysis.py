"""階段一：實體硬碟空間推演分析器

掃描來源資料夾下的壓縮包，只讀檔頭精算內部 PNG 佔比，依標籤彙總後
產出「硬碟釋放空間推演排行榜」，並寫入排行榜快取供搬移工具使用喵！
"""
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from config import (
    SOURCE_DIR, LOG_FILE, TARGETS_CACHE, HASH_CACHE_FILE, MOVE_SCRIPT_NAME,
    SHOW_TOP_N, MAX_TARGET_FILES, MIN_ARCHIVE_SIZE_MB,
    TARGET_PNG_RATIO, ESTIMATED_REDUCTION_RATE, ARCHIVE_EXTENSIONS,
)
from common_utils import (
    clean_str, format_mb_or_gb, extract_all_tags,
    load_processed_files, save_clean_file,
    load_hash_cache, save_hash_cache, get_png_ratio_with_cache,
)


def trigger_move_script():
    """分析完成後，詢問是否接著啟動搬移腳本。"""
    move_script = Path(MOVE_SCRIPT_NAME)

    print("\n" + "=" * 65)
    if not move_script.exists():
        print(f"💡 提醒：未在當前目錄找到搬移腳本 [{MOVE_SCRIPT_NAME}] 喵！")
        print("=" * 65)
        return

    user_input = input(
        f"❓ 分析完成！是否要立即啟動搬移工具 [{move_script.name}]？(Y/n): "
    ).strip().lower()
    if user_input != 'n':
        print(f"\n🚀 正在自動啟動 {move_script.name} ...\n" + "=" * 65 + "\n")
        try:
            subprocess.run([sys.executable, str(move_script)])
        except Exception as e:
            print(f"❌ 啟動搬移腳本失敗: {e} 喵！")
    else:
        print("💡 已跳過自動搬移，後續可隨時手動執行 python moveToResize.py 喵！")
    print("=" * 65)


def main():
    src_path = Path(SOURCE_DIR).resolve()
    if not src_path.exists():
        print(f"❌ 錯誤：來源目錄 [{SOURCE_DIR}] 不存在喵！")
        return

    processed_files = load_processed_files()
    hash_cache = load_hash_cache()

    print("========================================")
    print("⚡ 實體硬碟空間推演分析器 (Binary 二進制快取加速版) 啟動喵！")
    print(f"📂 快取檔: [{HASH_CACHE_FILE}] | 已載入 {len(hash_cache)} 筆 Binary 記憶體快取")
    print(f"🎯 鎖定目標: 抓滿 {MAX_TARGET_FILES} 個『內部 PNG 佔比 >= {int(TARGET_PNG_RATIO * 100)}%』的爆發包")
    print("========================================\n")

    tag_stats = defaultdict(lambda: {
        'display_name': '',
        'total_files': 0,
        'qualified_files': 0,
        'est_png_disk_mb': 0.0,
        'est_saved_disk_mb': 0.0,
        'archive_total_mb': 0.0,
    })

    all_files = [
        f for f in src_path.rglob("*")
        if f.is_file() and f.suffix.lower() in ARCHIVE_EXTENSIONS
    ]

    target_found_count = 0
    scanned_total_count = 0
    new_analyzed_count = 0

    for file_path in all_files:
        if target_found_count >= MAX_TARGET_FILES:
            print(f"\n🎉 成功抓滿 {MAX_TARGET_FILES} 個高潛力爆發包！自動暫停並產出報告喵！")
            break

        # 跳過黑名單與紀錄檔本身（以小寫檔名比對）
        if (file_path.name.lower() in processed_files
                or file_path.name.lower() == LOG_FILE.name.lower()):
            continue

        tags = extract_all_tags(file_path.name)
        if not tags:
            continue

        archive_mb = file_path.stat().st_size / (1024 * 1024)
        if archive_mb < MIN_ARCHIVE_SIZE_MB:
            continue

        scanned_total_count += 1

        png_ratio, is_success, is_new_scan = get_png_ratio_with_cache(file_path, hash_cache)
        if is_new_scan:
            new_analyzed_count += 1

        # 確認無 PNG 的包，記入黑名單後跳過
        if is_success and png_ratio == 0:
            save_clean_file(file_path.name)
            continue

        for raw_tag_name in tags:
            tag_key = raw_tag_name.lower()
            stats = tag_stats[tag_key]
            if not stats['display_name']:
                stats['display_name'] = raw_tag_name

            stats['total_files'] += 1
            stats['archive_total_mb'] += archive_mb

            if png_ratio > 0:
                png_disk_mb = archive_mb * png_ratio
                saved_disk_mb = png_disk_mb * ESTIMATED_REDUCTION_RATE

                stats['est_png_disk_mb'] += png_disk_mb
                stats['est_saved_disk_mb'] += saved_disk_mb

                if png_ratio >= TARGET_PNG_RATIO:
                    stats['qualified_files'] += 1

        if png_ratio >= TARGET_PNG_RATIO:
            target_found_count += 1
            safe_tag = clean_str(tags[0])
            safe_name = clean_str(file_path.name)
            cache_tag = "⚡Binary快取" if not is_new_scan else "🔍新解析"
            print(
                f"🔥 [{cache_tag} 第 {target_found_count}/{MAX_TARGET_FILES} 個潛力包] "
                f"[{safe_tag}] -> {safe_name} "
                f"(PNG 佔比 {int(png_ratio * 100)}% | {format_mb_or_gb(archive_mb)})"
            )

    # 儲存極速 Binary 快取
    save_hash_cache(hash_cache)

    if not tag_stats:
        print("\n❌ 沒有找到任何符合條件的壓縮檔喵！")
        return

    sorted_tags = sorted(
        tag_stats.items(),
        key=lambda item: (
            item[1]['est_saved_disk_mb'],
            item[1]['qualified_files'],
            item[1]['archive_total_mb'],
        ),
        reverse=True,
    )

    print("\n" + "=" * 65)
    print(f"📊 【硬碟釋放空間推演排行榜 Top {SHOW_TOP_N}】 (本次全新解析了 {new_analyzed_count} 個檔案)")
    print("=" * 65)

    targets_cache_data = []

    for rank, (tag_key, data) in enumerate(sorted_tags[:SHOW_TOP_N], start=1):
        safe_tag = clean_str(data['display_name'])
        png_disk_mb = data['est_png_disk_mb']
        est_saved_mb = data['est_saved_disk_mb']
        archive_mb = data['archive_total_mb']

        print(f"\n🏆 Rank {rank}: [{safe_tag}]")
        print(f"    ├─ 💡 處理後預估能幫硬碟『直接空出』: ~{format_mb_or_gb(est_saved_mb)}")
        print(f"    ├─ 📦 標籤旗下壓縮檔實體總重: {format_mb_or_gb(archive_mb)} ({data['total_files']} 個檔案)")
        print(f"    └─ 🔍 其中約有 {format_mb_or_gb(png_disk_mb)} 是由 PNG 構成的內容")

        targets_cache_data.append({
            'rank': rank,
            'tag': data['display_name'],
            'png_mb': png_disk_mb,
            'est_saved_mb': est_saved_mb,
            'total_files': data['total_files'],
            'archive_total_mb': archive_mb,
        })

    try:
        with open(TARGETS_CACHE, 'w', encoding='utf-8') as f:
            json.dump(targets_cache_data, f, ensure_ascii=False, indent=2)
        print("\n" + "=" * 65)
        print(f"💾 已將最新清單快取更新至: [{TARGETS_CACHE}] 與 Binary 快取 [{HASH_CACHE_FILE}]！")
    except Exception as e:
        print(f"\n❌ 快取寫入失敗: {e}")

    print("=" * 65 + " 喵！(ฅ'ω'ฅ)")

    trigger_move_script()


if __name__ == '__main__':
    main()
