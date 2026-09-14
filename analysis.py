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
    SHOW_TOP_N, SAVE_RANK_N, MAX_TARGET_FILES, EXTRA_SCAN_QUOTA, MIN_ARCHIVE_SIZE_MB,
    TARGET_PNG_RATIO, ESTIMATED_REDUCTION_RATE, ARCHIVE_EXTENSIONS,
    JPEG_PROBLEM_RATIO, ESTIMATED_JPG_REDUCTION_RATE,
)
from common_utils import (
    clean_str, format_mb_or_gb, extract_all_tags,
    load_processed_files, save_clean_file,
    load_hash_cache, save_hash_cache,
    get_archive_image_stats, has_cached_stats,
    stats_png_ratio, stats_large_jpg_ratio,
    load_lowgain_flags, flag_key, TEXT_IO,
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


def main(scan_limit=None, auto_next=True):
    """跑一輪分析並產出排行榜。

    scan_limit：這輪最多新解析幾筆（選單的「追加解析」用）。None 表示照
                MAX_TARGET_FILES + EXTRA_SCAN_QUOTA 的預設規則跑。
    auto_next ：跑完是否詢問接續啟動搬移工具。從搬移工具裡呼叫時要關掉，
                否則會把自己再開一次。
    """
    src_path = Path(SOURCE_DIR).resolve()
    if not src_path.exists():
        print(f"❌ 錯誤：來源目錄 [{SOURCE_DIR}] 不存在喵！")
        return

    processed_files = load_processed_files()
    hash_cache = load_hash_cache()
    lowgain_flags = load_lowgain_flags()

    print("========================================")
    print("⚡ 實體硬碟空間推演分析器 (Binary 二進制快取加速版) 啟動喵！")
    print(f"📂 快取檔: [{HASH_CACHE_FILE}] | 已載入 {len(hash_cache)} 筆 Binary 記憶體快取")
    print(f"🎯 鎖定目標: 抓滿 {MAX_TARGET_FILES} 個『內部 PNG 佔比 >= {int(TARGET_PNG_RATIO * 100)}%』的爆發包")
    if EXTRA_SCAN_QUOTA and scan_limit is None:
        print(f"🧮 每輪最多新解析 {EXTRA_SCAN_QUOTA} 個包；掃到就先出排行榜，下一輪繼續往下")
    print("========================================\n")

    tag_stats = defaultdict(lambda: {
        'display_name': '',
        'sample_file': '',        # 代表性檔名，讓數字型標籤看得出是什麼
        'total_files': 0,
        'qualified_files': 0,
        'est_png_disk_mb': 0.0,
        'est_saved_disk_mb': 0.0,
        'archive_total_mb': 0.0,
        'jpg_problem_files': 0,      # 內含過大 JPG 的包數
        'jpg_problem_disk_mb': 0.0,  # 過大 JPG 的實體總量
    })

    all_files = [
        f for f in src_path.rglob("*")
        if f.is_file() and f.suffix.lower() in ARCHIVE_EXTENSIONS
    ]

    cached_ready = sum(1 for f in all_files if has_cached_stats(f, hash_cache))
    pending_total = len(all_files) - cached_ready
    print(f"📦 掃描範圍 {len(all_files)} 個壓縮包："
          f"{cached_ready} 筆直接套用快取，{pending_total} 筆尚待解析")
    if scan_limit is not None:
        print(f"➕ 追加解析模式：本輪最多新解析 {scan_limit} 筆")
    print()

    target_found_count = 0
    scanned_total_count = 0
    new_analyzed_count = 0
    jpg_problem_found_count = 0
    already_optimized_count = 0
    lowgain_skipped_count = 0
    untagged_count = 0            # 有解析、但檔名裡找不到可用標籤，無法歸類
    cached_extra_count = 0        # 掃描上限之後，靠快取既有資料補進統計的包數
    uncached_skipped_count = 0    # 掃描上限之後，快取沒有資料只好略過的包數

    # 掃到上限就停止「開新檔案」，但不整個中斷：快取裡已經有結果的包是零成本的，
    # 繼續納入統計，排行榜才不會被掃描順序截斷（前面幾個資料夾吃掉全部名額）。
    # 另外，抓滿上限時若本輪新解析的數量還不到 EXTRA_SCAN_QUOTA，就繼續往下讀，
    # 讓每一輪都確實推進快取的覆蓋率；檔案讀完了自然就結束迴圈進排名。
    cache_only_mode = False

    def scan_budget_exhausted():
        """還能不能再開新檔案。

        兩個條件任一成立就停：本輪新解析的筆數到頂，或潛力包已經抓滿。
        （這裡必須是「或」——寫成「且」的話，因為 MAX_TARGET_FILES 實際上
        很難抓滿，筆數上限等於永遠不會生效，整個收藏庫會被一次掃完。）
        """
        limit = scan_limit if scan_limit is not None else EXTRA_SCAN_QUOTA
        if limit and new_analyzed_count >= limit:
            return True
        # 追加解析只認筆數；預設模式才另外看「潛力包抓夠了沒」
        return scan_limit is None and target_found_count >= MAX_TARGET_FILES

    for file_path in all_files:
        if not cache_only_mode and scan_budget_exhausted():
            cache_only_mode = True
            if scan_limit is not None:
                print(f"\n✅ 已追加解析 {new_analyzed_count} 筆！"
                      f"接下來不再開新檔案，快取裡已知的包仍會納入排行榜喵！")
            elif EXTRA_SCAN_QUOTA and new_analyzed_count >= EXTRA_SCAN_QUOTA:
                print(f"\n✅ 本輪已新解析 {new_analyzed_count} 個包（達到上限 {EXTRA_SCAN_QUOTA}）！"
                      f"接下來不再開新檔案，快取裡已知的包仍會納入排行榜；"
                      f"再跑一次就會從沒掃過的繼續往下喵！")
            else:
                print(f"\n🎉 已抓滿 {MAX_TARGET_FILES} 個高潛力爆發包！"
                      f"接下來不再開新檔案，但快取裡已知的包仍會納入排行榜喵！")

        # 跳過黑名單與紀錄檔本身（以小寫檔名比對）
        if (file_path.name.lower() in processed_files
                or file_path.name.lower() == LOG_FILE.name.lower()):
            continue

        # 試過但省太少的包：不列入統計、也不列入排行榜，免得反覆被搬來搬去
        if flag_key(file_path) in lowgain_flags:
            lowgain_skipped_count += 1
            continue

        # 注意順序：先讓它被分析，最後才談標籤。
        # 「沒有可用標籤」只代表無法歸類到排行榜，不代表這個包不該被看一眼——
        # 提早 continue 會讓它連開都不開，還會永遠卡在「尚待解析」的數字裡。
        tags = extract_all_tags(file_path.name)

        archive_mb = file_path.stat().st_size / (1024 * 1024)
        if archive_mb < MIN_ARCHIVE_SIZE_MB:
            continue

        scanned_total_count += 1

        stats_info, is_new_scan = get_archive_image_stats(
            file_path, hash_cache, cache_only=cache_only_mode)
        if stats_info is None:
            # 只吃快取的階段遇到沒掃過的包：這輪先不算，下次分析就會補上
            uncached_skipped_count += 1
            continue
        if is_new_scan:
            new_analyzed_count += 1
        elif cache_only_mode:
            cached_extra_count += 1

        is_success = stats_info['success']
        png_ratio = stats_png_ratio(stats_info)
        large_jpg_ratio = stats_large_jpg_ratio(stats_info)
        jpg_optimized = stats_info.get('jpg_optimized', False)
        is_jpg_problem = large_jpg_ratio >= JPEG_PROBLEM_RATIO and not jpg_optimized

        # 只有「PNG 與過大 JPG 都沒有」才列入黑名單，避免 JPG 包被永久隱藏
        if is_success and png_ratio == 0 and stats_info['large_jpg_bytes'] == 0:
            save_clean_file(file_path.name)
            continue

        # 抽樣顯示 JPG 已最佳化、又沒有可轉的 PNG → 再跑也省不了，不列入排行榜
        if is_success and png_ratio == 0 and jpg_optimized:
            already_optimized_count += 1
            continue

        # 到這裡已經解析完、也寫進快取了；沒有可用標籤就只是進不了排行榜。
        # 不 continue——下面的逐檔訊息照印，讓「有沒有被看過」看得見；
        # 標籤為空時歸類迴圈本來就不會跑，不必特別擋。
        if not tags:
            untagged_count += 1

        for raw_tag_name in tags:
            tag_key = raw_tag_name.lower()
            stats = tag_stats[tag_key]
            if not stats['display_name']:
                stats['display_name'] = raw_tag_name
            if not stats['sample_file']:
                stats['sample_file'] = file_path.name

            stats['total_files'] += 1
            stats['archive_total_mb'] += archive_mb

            if png_ratio > 0:
                png_disk_mb = archive_mb * png_ratio
                saved_disk_mb = png_disk_mb * ESTIMATED_REDUCTION_RATE

                stats['est_png_disk_mb'] += png_disk_mb
                stats['est_saved_disk_mb'] += saved_disk_mb

                if png_ratio >= TARGET_PNG_RATIO:
                    stats['qualified_files'] += 1

            # 過大 JPG 的預估節省也計入排序，讓 JPG 包能上榜、進搬移選單
            if is_jpg_problem:
                jpg_disk_mb = archive_mb * large_jpg_ratio
                stats['jpg_problem_files'] += 1
                stats['jpg_problem_disk_mb'] += jpg_disk_mb
                stats['est_saved_disk_mb'] += jpg_disk_mb * ESTIMATED_JPG_REDUCTION_RATE

        # 逐檔訊息只印「這輪真的去解析過」的包。套用快取的不再刷一次，
        # 否則幾千筆舊資料會把本輪實際做了什麼事整個蓋掉。
        display_tag = clean_str(tags[0]) if tags else '無標籤'

        if png_ratio >= TARGET_PNG_RATIO:
            target_found_count += 1
            if is_new_scan:
                print(
                    f"🔥 [🔍新解析 第 {new_analyzed_count} 筆] "
                    f"[{display_tag}] -> {clean_str(file_path.name)} "
                    f"(PNG 佔比 {int(png_ratio * 100)}% | {format_mb_or_gb(archive_mb)})"
                )
        elif is_jpg_problem:
            jpg_problem_found_count += 1
            if is_new_scan:
                print(
                    f"🖼️ [🔍新解析 疑似過大JPG包] "
                    f"[{display_tag}] -> {clean_str(file_path.name)} "
                    f"(過大JPG佔比 {int(large_jpg_ratio * 100)}% | {format_mb_or_gb(archive_mb)})"
                )

    # 儲存極速 Binary 快取
    save_hash_cache(hash_cache)

    if not tag_stats:
        print("\n❌ 沒有找到任何符合條件的壓縮檔喵！")
        # 全部都被略過時也要說清楚原因，不然看起來像是掃描壞掉了
        if uncached_skipped_count:
            print(f"⏳ 其中 {uncached_skipped_count} 個是快取還沒有資料、本輪掃描額度又用完而未列入的包")
        if lowgain_skipped_count:
            print(f"🏷️ 其中 {lowgain_skipped_count} 個是『試過但省太少』的標記包"
                  f"（resize.py --recheck 可重評）")
        if already_optimized_count:
            print(f"✅ 其中 {already_optimized_count} 個是『內容已最佳化、再跑也省不了』的包")
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
    if scan_limit is not None and new_analyzed_count < scan_limit:
        print(f"📖 本輪追加解析 {new_analyzed_count} 筆（少於指定的 {scan_limit} 筆）"
              f"——已經沒有更多沒掃過的檔案了，直接進排名")
    elif scan_limit is None and EXTRA_SCAN_QUOTA and new_analyzed_count < EXTRA_SCAN_QUOTA:
        print(f"📖 本輪新解析 {new_analyzed_count} 個包（未達上限 {EXTRA_SCAN_QUOTA}）"
              f"——已經沒有更多沒掃過的檔案了，直接進排名")
    if untagged_count:
        print(f"🏷️ 另有 {untagged_count} 個包檔名裡沒有可用標籤：已解析並存進快取，"
              f"但無法歸類到排行榜（可用 [0] 全域搬移或 [F] 關鍵字搜尋處理）")
    if cached_extra_count:
        print(f"⚡ 掃描上限之後，另有 {cached_extra_count} 個包直接採用快取既有資料納入統計（零額外 I/O）")
    if uncached_skipped_count:
        if scan_limit is not None:
            print(f"⏳ 另有 {uncached_skipped_count} 個包快取裡還沒有資料，本輪未列入"
                  f"（再選一次『追加解析』就會繼續往下補）")
        else:
            print(f"⏳ 另有 {uncached_skipped_count} 個包快取裡還沒有資料、本輪解析額度已用完，"
                  f"暫未列入（再跑一次就會繼續往下解析，想一次做多一點可調高 EXTRA_SCAN_QUOTA）")
    if lowgain_skipped_count:
        print(f"🏷️ 已略過 {lowgain_skipped_count} 個『試過但省太少』的壓縮包（resize.py --recheck 可重評）")
    if already_optimized_count:
        print(f"✅ 已略過 {already_optimized_count} 個『內容已最佳化、再跑也省不了』的壓縮包")
    if jpg_problem_found_count:
        print(f"🖼️ 另偵測到 {jpg_problem_found_count} 個『疑似過大 JPG』包（PNG 未達標但 JPG 過大，值得 resize 逐檔體檢）")
    print("=" * 65)

    targets_cache_data = []

    for rank, (tag_key, data) in enumerate(sorted_tags[:SAVE_RANK_N], start=1):
        # 快取存滿 SAVE_RANK_N 筆給「顯示全排名」翻頁用；報告本身只列前 SHOW_TOP_N 名
        show_this = rank <= SHOW_TOP_N
        safe_tag = clean_str(data['display_name'])
        png_disk_mb = data['est_png_disk_mb']
        est_saved_mb = data['est_saved_disk_mb']
        archive_mb = data['archive_total_mb']

        has_jpg = data['jpg_problem_files'] > 0
        png_connector = '├─' if has_jpg else '└─'

        if show_this:
            print(f"\n🏆 Rank {rank}: [{safe_tag}]")
            if data['sample_file']:
                print(f"    ├─ 📄 代表檔案: {clean_str(data['sample_file'])}")
            print(f"    ├─ 💡 處理後預估能幫硬碟『直接空出』: ~{format_mb_or_gb(est_saved_mb)}")
            print(f"    ├─ 📦 標籤旗下壓縮檔實體總重: {format_mb_or_gb(archive_mb)} ({data['total_files']} 個檔案)")
            print(f"    {png_connector} 🔍 其中約有 {format_mb_or_gb(png_disk_mb)} 是由 PNG 構成的內容")
            if has_jpg:
                print(
                    f"    └─ 🖼️ 另有 {data['jpg_problem_files']} 個包含過大 JPG "
                    f"(約 {format_mb_or_gb(data['jpg_problem_disk_mb'])}，可交由 resize 逐檔體檢)"
                )

        targets_cache_data.append({
            'rank': rank,
            'tag': data['display_name'],
            'sample': data['sample_file'],
            'png_mb': png_disk_mb,
            'jpg_mb': data['jpg_problem_disk_mb'],
            'est_saved_mb': est_saved_mb,
            'total_files': data['total_files'],
            'archive_total_mb': archive_mb,
        })

    try:
        with open(TARGETS_CACHE, 'w', **TEXT_IO) as f:
            json.dump(targets_cache_data, f, ensure_ascii=False, indent=2)
        print("\n" + "=" * 65)
        print(f"💾 已將最新清單快取更新至: [{TARGETS_CACHE}]（共 {len(targets_cache_data)} 名，"
              f"選單可用『顯示全排名』翻閱）與 Binary 快取 [{HASH_CACHE_FILE}]！")
    except Exception as e:
        print(f"\n❌ 快取寫入失敗: {e}")

    print("=" * 65 + " 喵！(ฅ'ω'ฅ)")

    if auto_next:
        trigger_move_script()


if __name__ == '__main__':
    main()
