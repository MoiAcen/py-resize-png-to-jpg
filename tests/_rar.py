"""測試用：手工組一個「只儲存、不壓縮」的 RAR5 壓縮包。

RAR 是專利格式，沒有授權可以在測試裡寫入；但 7-Zip 讀得懂，所以照 RAR 5.0
公開的檔案格式規格手工組出最小的合法檔案：簽章 → 主標頭 → 各檔案標頭與資料 → 結尾標頭。
只用來驗證「透過 7-Zip 讀 rar」這條路徑，內容一律不壓縮。
"""
import struct
import zlib

SIGNATURE = b'Rar!\x1a\x07\x01\x00'


def _vint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _block(htype, hflags, body=b'', data_size=None):
    """標頭 = CRC32 + 標頭大小 + (型別 旗標 [資料大小] 內容)。CRC 涵蓋「標頭大小」到標頭結尾。"""
    inner = _vint(htype) + _vint(hflags | (0x02 if data_size is not None else 0))
    if data_size is not None:
        inner += _vint(data_size)
    inner += body
    sized = _vint(len(inner)) + inner
    return struct.pack('<I', zlib.crc32(sized) & 0xFFFFFFFF) + sized


def _file_entry(name, payload):
    nm = name.encode('utf-8')
    body = (_vint(0x04)                                  # 檔案旗標：有資料 CRC32
            + _vint(len(payload))                        # 解壓後大小
            + _vint(0x20)                                # 屬性
            + struct.pack('<I', zlib.crc32(payload) & 0xFFFFFFFF)
            + _vint(0)                                   # 壓縮資訊：版本 0、方法 0 = 儲存
            + _vint(1)                                   # 主機作業系統：1 = Unix
            + _vint(len(nm)) + nm)
    return _block(2, 0, body, data_size=len(payload)) + payload


def _dir_entry(name):
    nm = name.encode('utf-8')
    body = (_vint(0x01)                                  # 檔案旗標：這是資料夾
            + _vint(0) + _vint(0x10)                     # 大小 0、屬性
            + _vint(0) + _vint(1)                        # 壓縮資訊、主機作業系統
            + _vint(len(nm)) + nm)
    return _block(2, 0, body)


def make_rar5(path, entries):
    """entries: [(成員路徑, bytes)]；成員路徑以 '/' 結尾視為資料夾（內容忽略）。"""
    out = SIGNATURE + _block(1, 0, _vint(0))             # 主標頭，封存旗標 0
    for name, payload in entries:
        out += _dir_entry(name.rstrip('/')) if name.endswith('/') else _file_entry(name, payload)
    out += _block(5, 0, _vint(0))                        # 結尾標頭
    with open(path, 'wb') as fh:
        fh.write(out)
    return path
