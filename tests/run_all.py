"""跑完所有測試。

    python tests/run_all.py            # 全部
    python tests/run_all.py tags menu  # 只跑名字含這些關鍵字的

每個測試都在自己的暫存資料夾裡跑，不會碰到 config 裡指到的真實收藏庫喵！
"""
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main(argv):
    keywords = [a.lower() for a in argv[1:]]
    files = sorted(p for p in TESTS_DIR.glob('test_*.py'))
    if keywords:
        files = [p for p in files if any(k in p.stem.lower() for k in keywords)]
    if not files:
        print('❌ 沒有符合的測試檔喵！')
        return 1

    print('=' * 70)
    print(f'🧪 準備執行 {len(files)} 組測試')
    print('=' * 70)

    results = []
    for path in files:
        start = time.time()
        proc = subprocess.run([sys.executable, str(path)], cwd=str(TESTS_DIR),
                              capture_output=True, text=True, errors='replace')
        elapsed = time.time() - start
        ok = proc.returncode == 0
        results.append((path.stem, ok, elapsed, proc))
        print(f"{'✅' if ok else '❌'} {path.stem:28} {elapsed:6.1f}s")
        if not ok:
            print('-' * 70)
            print(proc.stdout.strip()[-3000:])
            if proc.stderr.strip():
                print('--- stderr ---')
                print(proc.stderr.strip()[-2000:])
            print('-' * 70)

    failed = [name for name, ok, _, _ in results if not ok]
    total_time = sum(t for _, _, t, _ in results)
    print('=' * 70)
    if failed:
        print(f'❌ {len(failed)}/{len(results)} 組未通過: {", ".join(failed)}  ({total_time:.1f}s)')
        return 1
    print(f'✅ {len(results)} 組全部通過 ({total_time:.1f}s) 喵！(ฅ\'ω\'ฅ)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
