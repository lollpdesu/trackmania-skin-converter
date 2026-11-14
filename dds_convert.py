#!/usr/bin/env python3
"""
dds_convert.py - a small, dependency-free DDS -> PNG / TGA converter.

Only the Python standard library is used. The main image of each DDS
(mip level 0, first cube face / array slice) is decoded to 8-bit RGBA and
written without any lossy step: PNG is zlib-compressed, TGA is uncompressed.

Supported DDS formats
  Block compressed : BC1/DXT1, BC2/DXT2/DXT3, BC3/DXT4/DXT5,
                     BC4/ATI1 (unsigned + signed), BC5/ATI2 (unsigned + signed),
                     BC7
  Uncompressed     : legacy RGB / RGBA / luminance / alpha bitmask layouts with
                     8/16/24/32 bits per pixel and <= 8 bits per channel, plus the
                     common DX10 formats R8G8B8A8, B8G8R8A8, B8G8R8X8, R8, R8G8,
                     A8, B5G6R5, B5G5R5A1, B4G4R4A4
  Not supported    : BC6H and 10/16-bit or float formats - they cannot be stored
                     in an 8-bit PNG/TGA without losing precision.

Output layout
  Images with constant opaque alpha are written as RGB, and images where
  R == G == B are written as grayscale (same pixels, smaller files).
  Use --rgba to always write full 32-bit RGBA instead.

Examples
  python dds_convert.py texture.dds                  -> texture.png
  python dds_convert.py -f tga a.dds b.dds           -> a.tga, b.tga
  python dds_convert.py -f both -o out textures\\     -> whole folder (recursive)
"""

import argparse
import os
import struct
import sys
import zlib
from array import array
from concurrent.futures import ProcessPoolExecutor


class DDSError(Exception):
    pass


# --------------------------------------------------------------------------
# Block decoding helpers
# --------------------------------------------------------------------------

def _rows(px):
    """16 RGBA pixels (flat list of 64 ints) -> 4 rows of 16 bytes."""
    return (bytes(px[0:16]), bytes(px[16:32]), bytes(px[32:48]), bytes(px[48:64]))


def _rgb565(c):
    r = c >> 11
    g = (c >> 5) & 63
    b = c & 31
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


def _color_palette(blk, o, four_color):
    """Palette of a BC1-style colour block as 4 RGBA tuples."""
    c0 = blk[o] | blk[o + 1] << 8
    c1 = blk[o + 2] | blk[o + 3] << 8
    r0, g0, b0 = _rgb565(c0)
    r1, g1, b1 = _rgb565(c1)
    if four_color or c0 > c1:
        return ((r0, g0, b0, 255), (r1, g1, b1, 255),
                ((2 * r0 + r1 + 1) // 3, (2 * g0 + g1 + 1) // 3, (2 * b0 + b1 + 1) // 3, 255),
                ((r0 + 2 * r1 + 1) // 3, (g0 + 2 * g1 + 1) // 3, (b0 + 2 * b1 + 1) // 3, 255))
    return ((r0, g0, b0, 255), (r1, g1, b1, 255),
            ((r0 + r1 + 1) // 2, (g0 + g1 + 1) // 2, (b0 + b1 + 1) // 2, 255),
            (0, 0, 0, 0))


def _bc4_values(blk, o, signed):
    """Decode one 8-byte BC4 block at offset o into 16 values (0..255)."""
    a0 = blk[o]
    a1 = blk[o + 1]
    if signed:
        a0 = max(a0 - 256 if a0 > 127 else a0, -127)
        a1 = max(a1 - 256 if a1 > 127 else a1, -127)
        lo, hi = -127, 127
    else:
        lo, hi = 0, 255
    if a0 > a1:
        pal = [a0, a1] + [((7 - i) * a0 + i * a1 + 3) // 7 for i in range(1, 7)]
    else:
        pal = [a0, a1] + [((5 - i) * a0 + i * a1 + 2) // 5 for i in range(1, 5)] + [lo, hi]
    if signed:  # map -127..127 onto 0..255
        pal = [((v + 127) * 255 + 127) // 254 for v in pal]
    bits = int.from_bytes(blk[o + 2:o + 8], 'little')
    return [pal[(bits >> (3 * i)) & 7] for i in range(16)]


def _bc1(blk):
    pal = [bytes(p) for p in _color_palette(blk, 0, False)]
    return tuple(pal[b & 3] + pal[b >> 2 & 3] + pal[b >> 4 & 3] + pal[b >> 6] for b in blk[4:8])


def _bc2(blk):
    pal = _color_palette(blk, 8, True)
    px = []
    for r in range(4):
        a = blk[2 * r] | blk[2 * r + 1] << 8
        b = blk[12 + r]
        for c in range(4):
            p = pal[b >> (2 * c) & 3]
            px += (p[0], p[1], p[2], (a >> (4 * c) & 15) * 17)
    return _rows(px)


def _bc3(blk):
    alpha = _bc4_values(blk, 0, False)
    pal = _color_palette(blk, 8, True)
    px = []
    for r in range(4):
        b = blk[12 + r]
        for c in range(4):
            p = pal[b >> (2 * c) & 3]
            px += (p[0], p[1], p[2], alpha[4 * r + c])
    return _rows(px)


def _bc4(blk, signed=False):
    px = []
    for v in _bc4_values(blk, 0, signed):
        px += (v, v, v, 255)
    return _rows(px)


def _bc4s(blk):
    return _bc4(blk, True)


def _bc5(blk, signed=False):
    red = _bc4_values(blk, 0, signed)
    green = _bc4_values(blk, 8, signed)
    blue = 128 if signed else 0  # the unused channel reads as 0.0
    px = []
    for i in range(16):
        px += (red[i], green[i], blue, 255)
    return _rows(px)


def _bc5s(blk):
    return _bc5(blk, True)


# --------------------------------------------------------------------------
# BC7
# --------------------------------------------------------------------------

# mode: subsets, partition bits, rotation bits, index-selection bits,
#       colour bits, alpha bits, per-endpoint p-bits, shared p-bits,
#       index bits, secondary index bits
_BC7_MODES = (
    (3, 4, 0, 0, 4, 0, 1, 0, 3, 0),
    (2, 6, 0, 0, 6, 0, 0, 1, 3, 0),
    (3, 6, 0, 0, 5, 0, 0, 0, 2, 0),
    (2, 6, 0, 0, 7, 0, 1, 0, 2, 0),
    (1, 0, 2, 1, 5, 6, 0, 0, 2, 3),
    (1, 0, 2, 0, 7, 8, 0, 0, 2, 2),
    (1, 0, 0, 0, 7, 7, 1, 0, 4, 0),
    (2, 6, 0, 0, 5, 5, 1, 0, 2, 0),
)

_BC7_WEIGHTS = {
    2: (0, 21, 43, 64),
    3: (0, 9, 18, 27, 37, 46, 55, 64),
    4: (0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64),
}

# 2-subset partitions: bit i = subset of pixel i
_BC7_P2 = (
    0xCCCC, 0x8888, 0xEEEE, 0xECC8, 0xC880, 0xFEEC, 0xFEC8, 0xEC80,
    0xC800, 0xFFEC, 0xFE80, 0xE800, 0xFFE8, 0xFF00, 0xFFF0, 0xF000,
    0xF710, 0x008E, 0x7100, 0x08CE, 0x008C, 0x7310, 0x3100, 0x8CCE,
    0x088C, 0x3110, 0x6666, 0x366C, 0x17E8, 0x0FF0, 0x718E, 0x399C,
    0xAAAA, 0xF0F0, 0x5A5A, 0x33CC, 0x3C3C, 0x55AA, 0x9696, 0xA55A,
    0x73CE, 0x13C8, 0x324C, 0x3BDC, 0x6996, 0xC33C, 0x9966, 0x0660,
    0x0272, 0x04E4, 0x4E40, 0x2720, 0xC936, 0x936C, 0x39C6, 0x639C,
    0x9336, 0x9CC6, 0x817E, 0xE718, 0xCCF0, 0x0FCC, 0x7744, 0xEE22,
)

# 3-subset partitions: bits 2i..2i+1 = subset of pixel i
_BC7_P3 = (
    0xAA685050, 0x6A5A5040, 0x5A5A4200, 0x5450A0A8, 0xA5A50000, 0xA0A05050, 0x5555A0A0, 0x5A5A5050,
    0xAA550000, 0xAA555500, 0xAAAA5500, 0x90909090, 0x94949494, 0xA4A4A4A4, 0xA9A59450, 0x2A0A4250,
    0xA5945040, 0x0A425054, 0xA5A5A500, 0x55A0A0A0, 0xA8A85454, 0x6A6A4040, 0xA4A45000, 0x1A1A0500,
    0x0050A4A4, 0xAAA59090, 0x14696914, 0x69691400, 0xA08585A0, 0xAA821414, 0x50A4A450, 0x6A5A0200,
    0xA9A58000, 0x5090A0A8, 0xA8A09050, 0x24242424, 0x00AA5500, 0x24924924, 0x24499224, 0x50A50A50,
    0x500AA550, 0xAAAA4444, 0x66660000, 0xA5A0A5A0, 0x50A050A0, 0x69286928, 0x44AAAA44, 0x66666600,
    0xAA444444, 0x54A854A8, 0x95809580, 0x96969600, 0xA85454A8, 0x80959580, 0xAA141414, 0x96960000,
    0xAAAA1414, 0xA05050A0, 0xA0A5A5A0, 0x96000000, 0x40804080, 0xA9A8A9A8, 0xAAAAAA44, 0x2A4A5254,
)

_BC7_ANCHOR2 = (
    15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15,
    15, 2, 8, 2, 2, 8, 8, 15, 2, 8, 2, 2, 8, 8, 2, 2,
    15, 15, 6, 8, 2, 8, 15, 15, 2, 8, 2, 2, 2, 15, 15, 6,
    6, 2, 6, 8, 15, 15, 2, 2, 15, 15, 15, 15, 15, 2, 2, 15,
)

_BC7_ANCHOR3A = (
    3, 3, 15, 15, 8, 3, 15, 15, 8, 8, 6, 6, 6, 5, 3, 3,
    3, 3, 8, 15, 3, 3, 6, 10, 5, 8, 8, 6, 8, 5, 15, 15,
    8, 15, 3, 5, 6, 10, 8, 15, 15, 3, 15, 5, 15, 15, 15, 15,
    3, 15, 5, 5, 5, 8, 5, 10, 5, 10, 8, 13, 15, 12, 3, 3,
)

_BC7_ANCHOR3B = (
    15, 8, 8, 3, 15, 15, 3, 8, 15, 15, 15, 15, 15, 15, 15, 8,
    15, 8, 15, 3, 15, 8, 15, 8, 3, 15, 6, 10, 15, 15, 10, 8,
    15, 3, 15, 10, 10, 8, 9, 10, 6, 15, 8, 15, 3, 6, 6, 8,
    15, 3, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 3, 15, 15, 8,
)

_ZERO_ROWS = (bytes(16),) * 4


def _bc7(blk):
    first = blk[0]
    if first == 0:  # reserved mode 8: transparent black
        return _ZERO_ROWS
    mode = (first & -first).bit_length() - 1
    ns, pbits, rbits, ibits, cb, ab, epb, spb, ib, ib2 = _BC7_MODES[mode]
    v = int.from_bytes(blk, 'little') >> (mode + 1)

    part = v & ((1 << pbits) - 1)
    v >>= pbits
    rot = v & ((1 << rbits) - 1)
    v >>= rbits
    isb = v & ((1 << ibits) - 1)
    v >>= ibits

    ne = ns * 2
    ep = [[0, 0, 0, 255] for _ in range(ne)]
    m = (1 << cb) - 1
    for c in range(3):
        for e in range(ne):
            ep[e][c] = v & m
            v >>= cb
    if ab:
        m = (1 << ab) - 1
        for e in range(ne):
            ep[e][3] = v & m
            v >>= ab
    nch = 4 if ab else 3
    if epb:
        for e in range(ne):
            p = v & 1
            v >>= 1
            for c in range(nch):
                ep[e][c] = ep[e][c] << 1 | p
    elif spb:
        for s in range(ns):
            p = v & 1
            v >>= 1
            for e in (2 * s, 2 * s + 1):
                for c in range(3):
                    ep[e][c] = ep[e][c] << 1 | p
    cprec = cb + (1 if (epb or spb) else 0)
    aprec = ab + (1 if epb else 0)
    for e in ep:
        for c in range(3):
            x = e[c]
            e[c] = (x << (8 - cprec)) | (x >> (2 * cprec - 8))
        if ab:
            x = e[3]
            e[3] = (x << (8 - aprec)) | (x >> (2 * aprec - 8))

    if ns == 1:
        subset = (0,) * 16
        anchors = (0,)
    elif ns == 2:
        pm = _BC7_P2[part]
        subset = [(pm >> i) & 1 for i in range(16)]
        anchors = (0, _BC7_ANCHOR2[part])
    else:
        pm = _BC7_P3[part]
        subset = [(pm >> (2 * i)) & 3 for i in range(16)]
        anchors = (0, _BC7_ANCHOR3A[part], _BC7_ANCHOR3B[part])

    idx1 = [0] * 16
    for i in range(16):
        n = ib - 1 if i in anchors else ib
        idx1[i] = v & ((1 << n) - 1)
        v >>= n
    if ib2:
        idx2 = [0] * 16
        for i in range(16):
            n = ib2 - 1 if i == 0 else ib2
            idx2[i] = v & ((1 << n) - 1)
            v >>= n
        if isb:
            cidx, cw, aidx, aw = idx2, _BC7_WEIGHTS[ib2], idx1, _BC7_WEIGHTS[ib]
        else:
            cidx, cw, aidx, aw = idx1, _BC7_WEIGHTS[ib], idx2, _BC7_WEIGHTS[ib2]
    else:
        cidx = aidx = idx1
        cw = aw = _BC7_WEIGHTS[ib]

    px = []
    for i in range(16):
        s = subset[i]
        e0 = ep[2 * s]
        e1 = ep[2 * s + 1]
        w = cw[cidx[i]]
        iw = 64 - w
        r = (iw * e0[0] + w * e1[0] + 32) >> 6
        g = (iw * e0[1] + w * e1[1] + 32) >> 6
        b = (iw * e0[2] + w * e1[2] + 32) >> 6
        w = aw[aidx[i]]
        a = ((64 - w) * e0[3] + w * e1[3] + 32) >> 6
        if rot == 1:
            r, a = a, r
        elif rot == 2:
            g, a = a, g
        elif rot == 3:
            b, a = a, b
        px += (r, g, b, a)
    return _rows(px)


# --------------------------------------------------------------------------
# Surface decoders
# --------------------------------------------------------------------------

def decode_blocks(data, offset, w, h, block_size, decode_block):
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    if len(data) - offset < bw * bh * block_size:
        raise DDSError('file is truncated')
    pw, ph = bw * 4, bh * 4
    stride = pw * 4
    out = bytearray(stride * ph)
    cache = {}
    pos = offset
    for by in range(bh):
        row_base = by * 4 * stride
        for bx in range(bw):
            blk = data[pos:pos + block_size]
            pos += block_size
            rows = cache.get(blk)
            if rows is None:
                rows = decode_block(blk)
                if len(cache) < 100000:
                    cache[blk] = rows
            o = row_base + bx * 16
            out[o:o + 16] = rows[0]
            o += stride
            out[o:o + 16] = rows[1]
            o += stride
            out[o:o + 16] = rows[2]
            o += stride
            out[o:o + 16] = rows[3]
    if pw == w and ph == h:
        return out
    cropped = bytearray(w * h * 4)  # dimensions not a multiple of 4
    ws = w * 4
    for y in range(h):
        cropped[y * ws:(y + 1) * ws] = out[y * stride:y * stride + ws]
    return cropped


def _scale(x, mx):
    return x if mx == 255 else (x * 255 + mx // 2) // mx


def decode_masked(data, offset, w, h, bpp, masks, gray, fill):
    if bpp not in (8, 16, 24, 32):
        raise DDSError(f'unsupported bit depth {bpp}')
    bpx = bpp // 8
    n = w * h
    raw = data[offset:offset + n * bpx]
    if len(raw) < n * bpx:
        raise DDSError('file is truncated')
    if gray:
        masks = (masks[0], masks[0], masks[0], masks[3])

    chans = []
    for m in masks:
        if m == 0:
            chans.append(None)
            continue
        if m >> bpp:
            raise DDSError('channel mask exceeds pixel size')
        sh = (m & -m).bit_length() - 1
        mx = m >> sh
        if mx & (mx + 1):
            raise DDSError('non-contiguous channel mask')
        if mx > 255:
            raise DDSError('channel has more than 8 bits - an 8-bit PNG/TGA would lose precision')
        chans.append((m, sh, mx))
    defaults = (fill, fill, fill, 255)
    out = bytearray(n * 4)

    # Fast path: every channel is a whole byte -> plain byte shuffling.
    if all(ch is None or (ch[2] == 255 and ch[1] % 8 == 0) for ch in chans):
        for c, ch in enumerate(chans):
            if ch is None:
                out[c::4] = bytes((defaults[c],)) * n
            else:
                out[c::4] = raw[ch[1] // 8::bpx]
        return out

    if bpp <= 16:  # lookup table over every possible pixel value
        lut = [bytes(defaults[c] if ch is None else _scale((v & ch[0]) >> ch[1], ch[2])
                     for c, ch in enumerate(chans)) for v in range(1 << bpp)]
        if bpp == 8:
            vals = raw
        else:
            vals = array('H', raw)
            if sys.byteorder == 'big':
                vals.byteswap()
        return bytearray(b''.join(map(lut.__getitem__, vals)))

    for i in range(n):  # rare: odd 24/32-bit layouts
        v = int.from_bytes(raw[i * bpx:(i + 1) * bpx], 'little')
        for c, ch in enumerate(chans):
            out[4 * i + c] = defaults[c] if ch is None else _scale((v & ch[0]) >> ch[1], ch[2])
    return out


# --------------------------------------------------------------------------
# DDS container
# --------------------------------------------------------------------------

DDPF_ALPHAPIXELS = 0x1
DDPF_ALPHA = 0x2
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_LUMINANCE = 0x20000

_BC1 = ('block', 'BC1', 8, _bc1)
_BC2 = ('block', 'BC2', 16, _bc2)
_BC3 = ('block', 'BC3', 16, _bc3)
_BC4 = ('block', 'BC4', 8, _bc4)
_BC4S = ('block', 'BC4 signed', 8, _bc4s)
_BC5 = ('block', 'BC5', 16, _bc5)
_BC5S = ('block', 'BC5 signed', 16, _bc5s)
_BC7 = ('block', 'BC7', 16, _bc7)


def _masked(name, bpp, r, g, b, a, gray=False, fill=0):
    return ('masked', name, bpp, (r, g, b, a), gray, fill)


_RGBA8 = _masked('R8G8B8A8', 32, 0xFF, 0xFF00, 0xFF0000, 0xFF000000)
_BGRA8 = _masked('B8G8R8A8', 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000)
_BGRX8 = _masked('B8G8R8X8', 32, 0xFF0000, 0xFF00, 0xFF, 0)
_R8 = _masked('R8', 8, 0xFF, 0, 0, 0, gray=True)
_RG8 = _masked('R8G8', 16, 0xFF, 0xFF00, 0, 0)

FOURCC_FORMATS = {
    b'DXT1': _BC1, b'DXT2': _BC2, b'DXT3': _BC2, b'DXT4': _BC3, b'DXT5': _BC3,
    b'ATI1': _BC4, b'BC4U': _BC4, b'BC4S': _BC4S,
    b'ATI2': _BC5, b'BC5U': _BC5, b'BC5S': _BC5S,
}

DXGI_FORMATS = {
    27: _RGBA8, 28: _RGBA8, 29: _RGBA8, 30: _RGBA8,
    87: _BGRA8, 90: _BGRA8, 91: _BGRA8,
    88: _BGRX8, 92: _BGRX8, 93: _BGRX8,
    60: _R8, 61: _R8, 62: _R8,
    48: _RG8, 49: _RG8, 50: _RG8,
    65: _masked('A8', 8, 0, 0, 0, 0xFF, fill=255),
    85: _masked('B5G6R5', 16, 0xF800, 0x07E0, 0x001F, 0),
    86: _masked('B5G5R5A1', 16, 0x7C00, 0x03E0, 0x001F, 0x8000),
    115: _masked('B4G4R4A4', 16, 0x0F00, 0x00F0, 0x000F, 0xF000),
    70: _BC1, 71: _BC1, 72: _BC1,
    73: _BC2, 74: _BC2, 75: _BC2,
    76: _BC3, 77: _BC3, 78: _BC3,
    79: _BC4, 80: _BC4, 81: _BC4S,
    82: _BC5, 83: _BC5, 84: _BC5S,
    97: _BC7, 98: _BC7, 99: _BC7,
}


def _fourcc_text(fourcc):
    if all(32 <= ch < 127 for ch in fourcc):
        return repr(fourcc.decode('ascii'))
    return str(struct.unpack('<I', fourcc)[0])


def decode_dds(data):
    """Return (width, height, rgba bytearray, format name) for DDS file bytes."""
    if len(data) < 128 or data[:4] != b'DDS ':
        raise DDSError('not a DDS file')
    height, width = struct.unpack_from('<2I', data, 12)
    pf_flags, fourcc, bpp, rm, gm, bm, am = struct.unpack_from('<I4s5I', data, 80)
    if width == 0 or height == 0:
        raise DDSError('image has zero size')
    offset = 128

    if pf_flags & DDPF_FOURCC:
        if fourcc == b'DX10':
            if len(data) < 148:
                raise DDSError('truncated DX10 header')
            dxgi = struct.unpack_from('<I', data, 128)[0]
            offset = 148
            spec = DXGI_FORMATS.get(dxgi)
            if spec is None:
                if dxgi in (94, 95, 96):
                    raise DDSError('BC6H is an HDR float format and cannot be stored losslessly in PNG/TGA')
                raise DDSError(f'unsupported DXGI format {dxgi} (10/16-bit, float or exotic format)')
        else:
            spec = FOURCC_FORMATS.get(fourcc)
            if spec is None:
                raise DDSError(f'unsupported FourCC {_fourcc_text(fourcc)}')
    elif pf_flags & (DDPF_RGB | DDPF_LUMINANCE | DDPF_ALPHA):
        if not pf_flags & (DDPF_ALPHAPIXELS | DDPF_ALPHA):
            am = 0
        alpha_only = not pf_flags & (DDPF_RGB | DDPF_LUMINANCE)
        if alpha_only:
            rm = gm = bm = 0
        spec = _masked(f'uncompressed {bpp}-bit', bpp, rm, gm, bm, am,
                       gray=bool(pf_flags & DDPF_LUMINANCE), fill=255 if alpha_only else 0)
    else:
        raise DDSError('unsupported pixel format')

    if spec[0] == 'block':
        _, name, block_size, fn = spec
        rgba = decode_blocks(data, offset, width, height, block_size, fn)
    else:
        _, name, bpp, masks, gray, fill = spec
        rgba = decode_masked(data, offset, width, height, bpp, masks, gray, fill)
    return width, height, rgba, name


def load_dds(path):
    with open(path, 'rb') as f:
        return decode_dds(f.read())


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

def _pack(rgba, n, order):
    if order == (0, 1, 2, 3):
        return rgba
    k = len(order)
    out = bytearray(n * k)
    for i, c in enumerate(order):
        out[i::k] = rgba[c::4]
    return out


def analyze(rgba, n):
    """(has_alpha, is_gray) for an RGBA buffer."""
    alpha = rgba[3::4]
    has_alpha = alpha.count(255) != n
    r = rgba[0::4]
    is_gray = r == rgba[1::4] and r == rgba[2::4]
    return has_alpha, is_gray


def write_png(path, w, h, rgba, has_alpha, is_gray):
    n = w * h
    if is_gray:
        order, ctype = ((0, 3), 4) if has_alpha else ((0,), 0)
    else:
        order, ctype = ((0, 1, 2, 3), 6) if has_alpha else ((0, 1, 2), 2)
    pix = _pack(rgba, n, order)
    stride = w * len(order)
    raw = bytearray((stride + 1) * h)  # filter byte 0 (None) per row
    for y in range(h):
        o = y * (stride + 1) + 1
        raw[o:o + stride] = pix[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload +
                struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))

    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, ctype, 0, 0, 0)))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 6)))
        f.write(chunk(b'IEND', b''))


def write_tga(path, w, h, rgba, has_alpha, is_gray):
    if w > 65535 or h > 65535:
        raise DDSError('image too large for TGA (max 65535 px)')
    n = w * h
    if is_gray and not has_alpha:
        order, itype = (0,), 3
    else:
        order, itype = ((2, 1, 0, 3) if has_alpha else (2, 1, 0)), 2
    pix = _pack(rgba, n, order)
    k = len(order)
    desc = 8 if k == 4 else 0  # alpha bits; origin bottom-left (most compatible)
    stride = w * k
    body = bytearray(len(pix))
    for y in range(h):
        src = (h - 1 - y) * stride
        body[y * stride:(y + 1) * stride] = pix[src:src + stride]
    with open(path, 'wb') as f:
        f.write(struct.pack('<BBBHHBHHHHBB', 0, 0, itype, 0, 0, 0, 0, 0, w, h, k * 8, desc))
        f.write(body)
        f.write(struct.pack('<II', 0, 0) + b'TRUEVISION-XFILE.\x00')


WRITERS = {'png': write_png, 'tga': write_tga}


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _convert_one(job):
    path, rel, formats, outdir, force_rgba = job
    try:
        w, h, rgba, fmt = load_dds(path)
        has_alpha, is_gray = (True, False) if force_rgba else analyze(rgba, w * h)
        dest = os.path.normpath(os.path.join(outdir, rel) if outdir else os.path.dirname(os.path.abspath(path)))
        os.makedirs(dest, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        written = []
        for ext in formats:
            out_path = os.path.join(dest, base + '.' + ext)
            WRITERS[ext](out_path, w, h, rgba, has_alpha, is_gray)
            written.append(out_path)
        return True, f'OK    {path}  [{fmt}, {w}x{h}] -> ' + ', '.join(written)
    except (DDSError, OSError, struct.error) as e:
        return False, f'FAIL  {path}: {e}'


def _gather(inputs):
    found = []
    for p in inputs:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for name in sorted(files):
                    if name.lower().endswith('.dds'):
                        found.append((os.path.join(root, name), os.path.relpath(root, p)))
        elif os.path.isfile(p):
            found.append((p, '.'))
        else:
            print(f'SKIP  {p}: not found')
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description='Lossless DDS -> PNG / TGA converter (pure Python).')
    ap.add_argument('inputs', nargs='+', help='.dds files and/or folders (searched recursively)')
    ap.add_argument('-f', '--format', choices=('png', 'tga', 'both'), default='png',
                    help='output format (default: png)')
    ap.add_argument('-o', '--outdir', help='output folder (default: next to each input file)')
    ap.add_argument('--rgba', action='store_true',
                    help='always write 32-bit RGBA instead of RGB/grayscale when possible')
    ap.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 1,
                    help='files converted in parallel (default: number of CPUs)')
    args = ap.parse_args(argv)

    formats = ('png', 'tga') if args.format == 'both' else (args.format,)
    jobs = [(p, rel, formats, args.outdir, args.rgba) for p, rel in _gather(args.inputs)]
    if not jobs:
        print('No .dds files found.')
        return 1

    if len(jobs) > 1 and args.jobs > 1:
        with ProcessPoolExecutor(max_workers=min(args.jobs, len(jobs))) as ex:
            results = list(ex.map(_convert_one, jobs))
    else:
        results = [_convert_one(j) for j in jobs]
    for _, msg in results:
        print(msg)
    failed = sum(1 for ok, _ in results if not ok)
    print(f'\n{len(results) - failed} converted, {failed} failed.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
