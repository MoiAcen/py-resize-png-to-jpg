"""選單直接呼叫 resize.py：處理、查看「效率不足」標記、重評，以及搬完之後的詢問。"""
import os
import subprocess
import sys
from pathlib import Path

from _harness import Checker, Sandbox, capture, feed_input, make_png_zip, REPO_ROOT
import config


class FakeRun:
    """記下 subprocess.run 實際被叫了什麼，不真的啟動 resize.py。"""

    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))

        class Result:
            pass
        r = Result()
        r.returncode = self.returncode
        return r


def main():
    c = Checker('選單直接執行 Resize')
    with Sandbox() as sb:
        import common_utils as cu
        import moveToResize as mv
        import resize
        sb.sync(cu, mv, resize)

        real_run = subprocess.run
        fake = FakeRun()
        subprocess.run = fake
        try:
            make_png_zip(sb.work / '[a] 1.zip', seed=1)
            make_png_zip(sb.work / '[b] 2.zip', seed=2)
            (sb.work / 'x_temp_processing.zip').write_bytes(b'half written')   # 不算待處理
            flagged = sb.work / '[flagged] f.zip'
            make_png_zip(flagged, seed=3)
            cu.save_lowgain_flags({cu.flag_key(flagged): {
                'name': flagged.name, 'ratio': 3.2, 'sig': cu.settings_signature()}})

            c.section('選單上有這個選項')
            out, _ = capture(mv.print_menu, [])
            c.check('[X] 直接執行 Resize' in out, '主選單列出 [X]')
            c.check('效率不足' in out.split('[X]')[1].split('\n')[0], '選項說明提到可以查看/重評效率不足標記')

            c.section('[Enter]：直接處理')
            feed_input(mv, [''])
            out, ok = capture(mv.run_resize_menu)
            c.check('3 個壓縮包待處理' in out, '待處理數正確，且不把 *_temp_processing.zip 算進去')
            c.check('1 個已標記為「效率不足」' in out, '顯示已標記的筆數')
            c.check(ok and len(fake.calls) == 1, '啟動了 resize.py 一次')
            cmd = fake.calls[-1][0]
            c.check(cmd[1] == str(REPO_ROOT / 'resize.py') and cmd[2:] == [],
                    f'沒有帶額外參數: {cmd[2:]}')

            c.section('[L]：只查看標記，不處理')
            fake.calls.clear()
            feed_input(mv, ['L'])
            capture(mv.run_resize_menu)
            c.check(len(fake.calls) == 1 and fake.calls[0][0][2:] == ['--list-flags'],
                    f'帶的是 --list-flags: {fake.calls[0][0][2:] if fake.calls else None}')

            c.section('[R]：忽略標記重新評估')
            fake.calls.clear()
            feed_input(mv, ['R'])
            capture(mv.run_resize_menu)
            c.check(len(fake.calls) == 1 and fake.calls[0][0][2:] == ['--recheck'],
                    f'帶的是 --recheck: {fake.calls[0][0][2:] if fake.calls else None}')

            c.section('[Q] 與無效輸入：什麼都不做')
            fake.calls.clear()
            feed_input(mv, ['Q'])
            out, ok = capture(mv.run_resize_menu)
            c.check(not ok and not fake.calls and '已取消' in out, '[Q] 取消，沒有啟動任何東西')
            feed_input(mv, ['zzz'])
            out, ok = capture(mv.run_resize_menu)
            c.check(not ok and not fake.calls and '輸入無效' in out, '亂打的輸入被擋下來')

            c.section('腳本路徑不依賴目前的工作目錄')
            fake.calls.clear()
            old_cwd = os.getcwd()
            os.chdir(sb.root)                       # 從完全不相干的資料夾執行
            try:
                feed_input(mv, [''])
                capture(mv.run_resize_menu)
            finally:
                os.chdir(old_cwd)
            c.check(fake.calls and fake.calls[0][0][1] == str(REPO_ROOT / 'resize.py'),
                    '從別的資料夾執行，仍然找得到 resize.py')
            c.check(fake.calls and fake.calls[0][1].get('cwd') == str(REPO_ROOT),
                    '子行程的工作目錄固定在腳本所在的資料夾')

            c.section('失敗要讓使用者看得到')
            fake.returncode = 3
            fake.calls.clear()
            feed_input(mv, [''])
            out, ok = capture(mv.run_resize_menu)
            c.check(not ok and '回傳代碼 3' in out, '非 0 的結束代碼會顯示出來')
            fake.returncode = 0

            c.section('搬完之後的詢問也走同一個啟動函式')
            fake.calls.clear()
            feed_input(mv, ['y'])
            capture(mv.trigger_resize_prompt)
            c.check(len(fake.calls) == 1 and fake.calls[0][0][2:] == [], '回答 y 會啟動 resize.py')
            fake.calls.clear()
            feed_input(mv, ['n'])
            capture(mv.trigger_resize_prompt)
            c.check(not fake.calls, '回答 n 不啟動')

            c.section('工作區是空的：不空跑')
            for f in sb.work.glob('*.zip'):
                f.unlink()
            fake.calls.clear()
            for answer in ('', 'R'):
                feed_input(mv, [answer])
                out, ok = capture(mv.run_resize_menu)
                c.check(not ok and not fake.calls and '目前沒有壓縮包' in out,
                        f'輸入 {answer!r} 時，空工作區不啟動、並提示先搬檔案進來')
            feed_input(mv, ['L'])
            capture(mv.run_resize_menu)
            c.check(len(fake.calls) == 1, '但 [L] 查看標記不需要工作區有東西，照樣可以用')
        finally:
            subprocess.run = real_run

        c.section('標記清單本身：查詢輸出')
        out, _ = capture(resize.show_flags, {
            'k1': {'name': 'big one.zip', 'ratio': 7.5},
            'k2': {'name': 'tiny.zip', 'ratio': 1.2}})
        c.check('2 個壓縮包被標記為效率不足' in out, '顯示總數')
        c.check(out.index('tiny.zip') < out.index('big one.zip'), '依省得最少的排在前面')
        c.check('只省   1.2%' in out and '只省   7.5%' in out, '每個都顯示當時只省幾 %')
        out, _ = capture(resize.show_flags, {})
        c.check('沒有任何『效率不足』標記' in out, '沒有標記時有說明')

        c.section('resize.py 的提示行告訴使用者怎麼查')
        cu.save_lowgain_flags({'zz': {'name': 'old.zip', 'ratio': 2.0, 'sig': cu.settings_signature()}})
        make_png_zip(sb.work / '[c] 3.zip', seed=9)
        sys.argv = ['resize.py']
        out, _ = capture(resize.main)
        c.check('已標記效率不足: 1 筆' in out and '--list-flags 可查看清單' in out,
                '那行提示現在帶有查詢方式（以前只提到 --recheck）')

    c.section('真的跑一次 resize.py --list-flags（不假裝）')
    proc = subprocess.run([sys.executable, str(REPO_ROOT / 'resize.py'), '--list-flags'],
                          capture_output=True, text=True, errors='replace')
    c.check(proc.returncode == 0, f'結束代碼 0（實際 {proc.returncode}）')
    c.check('標記' in proc.stdout, '有輸出標記相關訊息')

    return c.finish()


if __name__ == '__main__':
    sys.exit(main())
