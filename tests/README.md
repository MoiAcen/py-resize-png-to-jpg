# 測試

```bash
python tests/run_all.py              # 全部
python tests/run_all.py tags menu    # 只跑檔名含關鍵字的那幾組
python tests/test_tags.py            # 單獨跑一組
```

只需要 `requirements.txt` 裡已有的相依（Pillow、mozjpeg-lossless-optimization），
不需要 pytest。每組測試都是獨立的腳本，通過回傳 0、失敗回傳 1。

## 安全性

每組測試都在 `tempfile.mkdtemp()` 開出來的暫存資料夾裡跑，
`_harness.Sandbox` 會把 `config` 裡所有路徑（來源庫、工作區、失敗區、
各式快取與對照表）改指到那裡，結束時整個刪掉。
**不會讀寫你 config 裡設定的真實收藏庫**，也不會留下任何快取檔。

## 各組涵蓋什麼

| 檔案 | 涵蓋範圍 |
|---|---|
| `test_tags.py` | 標籤解析：雜訊關鍵字與樣式、斜線拆成多標籤、重複去除、手動標籤取代、切割規則 |
| `test_analysis.py` | 快取/待解析計數、解析額度上限、逐檔 log 只印新解析、無標籤包仍被分析、排行榜不被掃描順序截斷、追加解析 |
| `test_menu.py` | 選單列出筆數、全排名翻頁（上下頁/跳頁/結束）、追加解析輸入驗證、名次直接選取 |
| `test_tagging_flows.py` | `[T]` 關鍵字標籤與 `[S]` 切割規則精靈的完整流程，含「不得更動檔名」與「不觸發重新解析」 |
| `test_convert.py` | PNG 轉檔不得蓋掉同名 JPG、30 組撞名併發、轉檔失敗不留殘骸 |
| `test_failures.py` | 壞檔搬移與說明檔、缺 7-Zip 不誤搬、WebP（含動圖）與非圖片內容位元組不變 |
| `test_lowgain_and_done.py` | 效率不足標記不再來回空轉（含同名碰撞）、完成目錄三條路徑與 mtime 保留 |
| `test_surrogate.py` | 檔名含落單 surrogate 時，所有快取檔仍能寫入、讀回並對得上原檔 |

## 寫新測試

用 `_harness` 裡的東西：`Sandbox`（暫存環境）、`make_zip` / `make_png_zip`（造測試包）、
`capture`（收 stdout）、`feed_input`（餵互動式輸入）、`Checker`（收集檢查結果）。
照著現有的檔案寫一個 `main()` 回傳 `checker.finish()` 即可，`run_all.py` 會自動撿到。
