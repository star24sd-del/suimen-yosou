#!/usr/bin/env python3
"""LZH(LHA)形式の解凍。標準ライブラリだけで動く(外部ライブラリ・外部コマンド不要)。

対応: -lh0-(無圧縮), -lh4-, -lh5-, -lh6-, -lh7-。ヘッダはレベル0/1/2。
使い方:
  from lzh import read_lzh
  for name, data in read_lzh("k261030.lzh"):
      text = data.decode("cp932")
"""
from __future__ import annotations

import struct

_THRESHOLD = 3
_NC = 510   # 文字 + 一致長の記号数
_NT = 19
_CBIT = 9
_TBIT = 5
_DIC_BITS = {"-lh4-": 12, "-lh5-": 13, "-lh6-": 15, "-lh7-": 16}


class LzhError(Exception):
    pass


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class _Bits:
    def __init__(self, data: bytes):
        self.d, self.i, self.acc, self.n = data, 0, 0, 0

    def peek(self, k: int) -> int:
        while self.n < k:
            b = self.d[self.i] if self.i < len(self.d) else 0
            self.i += 1
            self.acc = (self.acc << 8) | b
            self.n += 8
        return (self.acc >> (self.n - k)) & ((1 << k) - 1)

    def skip(self, k: int) -> None:
        self.n -= k
        self.acc &= (1 << self.n) - 1

    def get(self, k: int) -> int:
        if k == 0:
            return 0
        v = self.peek(k)
        self.skip(k)
        return v


def _make_table(lens: list[int]) -> list:
    """符号長から、16ビット先読みで記号を引く表を作る。"""
    count = [0] * 18
    for l in lens:
        count[l] += 1
    start = [0] * 19
    for i in range(1, 17):
        start[i + 1] = start[i] + (count[i] << (16 - i))
    table = [None] * 65536
    nxt = start[:]
    for sym, l in enumerate(lens):
        if l == 0:
            continue
        s = nxt[l]
        e = s + (1 << (16 - l))
        if e > 65536:
            raise LzhError("符号表が不正です")
        table[s:e] = [sym] * (e - s)
        nxt[l] = e
    return table


def _read_pt_len(br: _Bits, nn: int, nbit: int, i_special: int):
    n = br.get(nbit)
    if n == 0:
        c = br.get(nbit)
        return [0] * nn, [c] * 65536
    lens = [0] * nn
    i = 0
    while i < n:
        c = br.peek(3)
        if c == 7:
            mask = 1 << 12
            b = br.peek(16)
            while b & mask:
                mask >>= 1
                c += 1
        br.skip(3 if c < 7 else c - 3)
        lens[i] = c
        i += 1
        if i == i_special:
            c = br.get(2)
            for _ in range(c):
                lens[i] = 0
                i += 1
    return lens, _make_table(lens)


def _decode_lh(data: bytes, orig_size: int, dic_bits: int) -> bytes:
    npt = dic_bits + 1
    pbit = 4 if dic_bits <= 14 else 5
    br = _Bits(data)
    dic = 1 << dic_bits
    out = bytearray(b" " * dic)  # 辞書の初期値は空白
    target = dic + orig_size
    blocksize = 0
    c_len = c_tab = p_len = p_tab = None
    pt_len = pt_tab = None
    while len(out) < target:
        if blocksize == 0:
            blocksize = br.get(16)
            pt_len, pt_tab = _read_pt_len(br, _NT, _TBIT, 3)
            n = br.get(_CBIT)
            if n == 0:
                c = br.get(_CBIT)
                c_len, c_tab = [0] * _NC, [c] * 65536
            else:
                c_len = [0] * _NC
                i = 0
                while i < n:
                    c = pt_tab[br.peek(16)]
                    if c is None:
                        raise LzhError("データが壊れています")
                    br.skip(pt_len[c])
                    if c <= 2:
                        if c == 0:
                            c = 1
                        elif c == 1:
                            c = br.get(4) + 3
                        else:
                            c = br.get(_CBIT) + 20
                        i += c
                    else:
                        c_len[i] = c - 2
                        i += 1
                c_tab = _make_table(c_len)
            p_len, p_tab = _read_pt_len(br, npt, pbit, -1)
        blocksize -= 1
        sym = c_tab[br.peek(16)]
        if sym is None:
            raise LzhError("データが壊れています")
        br.skip(c_len[sym])
        if sym < 256:
            out.append(sym)
        else:
            length = sym - 253
            j = p_tab[br.peek(16)]
            if j is None:
                raise LzhError("データが壊れています")
            br.skip(p_len[j])
            if j != 0:
                j = (1 << (j - 1)) + br.get(j - 1)
            dist = j + 1
            for _ in range(length):
                out.append(out[-dist])
    return bytes(out[dic:dic + orig_size])


def read_lzh(path: str):
    """LZHファイルの中身を (ファイル名, 中身のバイト列) で順に返す。CRCも確認する。"""
    with open(path, "rb") as f:
        buf = f.read()
    pos = 0
    while pos < len(buf) and buf[pos] != 0:
        hsize = buf[pos]
        method = buf[pos + 2:pos + 7].decode("ascii", "replace")
        packed, orig = struct.unpack_from("<II", buf, pos + 7)
        level = buf[pos + 20]
        if level != 0:
            raise LzhError(f"ヘッダのレベル{level}には未対応です")
        namelen = buf[pos + 21]
        name = buf[pos + 22:pos + 22 + namelen].decode("cp932", "replace")
        crc = struct.unpack_from("<H", buf, pos + 22 + namelen)[0]
        body = pos + 2 + hsize
        raw = buf[body:body + packed]
        if method == "-lh0-":
            data = raw
        elif method in _DIC_BITS:
            data = _decode_lh(raw, orig, _DIC_BITS[method])
        else:
            raise LzhError(f"圧縮方式{method}には未対応です")
        if crc16(data) != crc:
            raise LzhError(f"{name}: CRCが一致しません(データ破損の可能性)")
        yield name, data
        pos = body + packed


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        for n, d in read_lzh(p):
            out = n.lower()
            open(out, "wb").write(d)
            print(f"{p}: {n} -> {out} ({len(d)} bytes, CRC OK)")
