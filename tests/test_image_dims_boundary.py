"""ctxwin._image_dims reads a JPEG's size even when the SOF marker's last
dimension byte is the very last byte of the peeked buffer.

image_tokens() decodes only the first _B64_PEEK base64 characters of a payload
(98304 bytes). A JPEG whose APP segments (EXIF, ICC) push its SOF to exactly
that boundary used to fall back to IMAGE_TOKENS_DEFAULT: the marker loop was
guarded by ``i + 9 < len(raw)`` while reading the dimensions needs only
``len(raw) >= i + 9``. A fill byte (0xFF) before a marker, which the JPEG spec
allows, also used to be read as the marker itself.

Hermetic: synthetic byte strings only, no file, no network.
"""
import base64
import struct

import pytest

import ctxwin


W, H = 800, 600                       # 800 * 600 / 750 = 640 tokens
EXPECT_TOKENS = 640


def _sof(marker=0xC0, w=W, h=H):
    """A SOF segment cut right after its 4 dimension bytes (9 bytes)."""
    return bytes([0xFF, marker]) + struct.pack(">HBHH", 17, 8, h, w)


def _sof_rest():
    """The rest of a 3-component SOF0 segment after the dimension bytes."""
    return bytes([3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])


def _app(n_total, marker=0xE1):
    """An APPn segment that is n_total bytes long, marker included."""
    seg = n_total - 2
    assert 2 <= seg <= 0xFFFF
    return bytes([0xFF, marker]) + struct.pack(">H", seg) + b"\x00" * (seg - 2)


# --------------------------------------------------------------------------- #
# The bug: the last dimension byte is the last byte of the buffer
# --------------------------------------------------------------------------- #

def test_sof_ending_exactly_at_the_buffer_end_is_read():
    raw = b"\xff\xd8" + _sof()
    assert len(raw) == 2 + 9
    assert ctxwin._image_dims(raw) == (W, H)


def test_sof_after_an_app_segment_ending_at_the_buffer_end_is_read():
    raw = b"\xff\xd8" + _app(18, 0xE0) + _sof()
    assert ctxwin._image_dims(raw) == (W, H)


@pytest.mark.parametrize("marker", [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                    0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF])
def test_every_sof_kind_is_read_at_the_buffer_end(marker):
    raw = b"\xff\xd8" + _app(40) + _sof(marker, w=1920, h=1080)
    assert ctxwin._image_dims(raw) == (1920, 1080)


def _jpeg_with_sof_at_peek_end():
    """A JPEG whose SOF dimension bytes end exactly at the peeked byte count."""
    peek_bytes = ctxwin._B64_PEEK // 4 * 3          # 98304
    filler = peek_bytes - 2 - 9                      # SOI, then the SOF up to dims
    first = 0xFFFF + 2                               # one APP1 at its max size
    head = b"\xff\xd8" + _app(first, 0xE1) + _app(filler - first, 0xE2) + _sof()
    assert len(head) == peek_bytes
    return head + _sof_rest() + b"\xff\xd9"


def test_image_tokens_reads_a_jpeg_whose_sof_ends_at_the_peek_boundary():
    raw = _jpeg_with_sof_at_peek_end()
    b64 = base64.b64encode(raw).decode()
    assert len(b64) > ctxwin._B64_PEEK               # the peek really truncates
    assert ctxwin.image_tokens(b64=b64) == EXPECT_TOKENS
    url = "data:image/jpeg;base64," + b64
    assert ctxwin.image_tokens(url=url) == EXPECT_TOKENS
    assert EXPECT_TOKENS != ctxwin.IMAGE_TOKENS_DEFAULT


def test_every_prefix_of_a_header_reads_none_until_the_dims_then_the_dims():
    full = b"\xff\xd8" + _app(20, 0xE0) + _sof() + _sof_rest() + b"\xff\xd9"
    dims_end = 2 + 20 + 9
    for n in range(len(full) + 1):
        got = ctxwin._image_dims(full[:n])            # never raises
        if n < dims_end:
            assert got is None, n
        else:
            assert got == (W, H), n


# --------------------------------------------------------------------------- #
# A fill byte before a marker (JPEG spec B.1.1.2)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fills", [1, 2, 5])
def test_fill_bytes_before_the_sof_marker_are_skipped(fills):
    raw = (b"\xff\xd8" + _app(30, 0xE0) + b"\xff" * fills + _sof()
           + _sof_rest() + b"\xff\xd9")
    assert ctxwin._image_dims(raw) == (W, H)


def test_fill_bytes_before_an_app_marker_are_skipped():
    raw = b"\xff\xd8" + b"\xff" + _app(30, 0xE0) + _sof()
    assert ctxwin._image_dims(raw) == (W, H)


# --------------------------------------------------------------------------- #
# Unchanged behaviour (regression guards; these pass with or without the fix)
# --------------------------------------------------------------------------- #

def test_a_normal_jpeg_header_still_works():
    raw = (b"\xff\xd8" + _app(18, 0xE0) + _app(200, 0xE1) + _sof()
           + _sof_rest() + b"\xff\xc4" + struct.pack(">H", 4) + b"\x00\x00"
           + b"\xff\xd9")
    assert ctxwin._image_dims(raw) == (W, H)
    b64 = base64.b64encode(raw).decode()
    assert ctxwin.image_tokens(b64=b64) == EXPECT_TOKENS


@pytest.mark.parametrize("cut", [1, 2, 3, 4, 8])
def test_a_truncated_sof_returns_none_without_raising(cut):
    raw = b"\xff\xd8" + _app(30, 0xE0) + _sof()
    assert ctxwin._image_dims(raw[:-cut]) is None
    b64 = base64.b64encode(raw[:-cut]).decode()
    assert ctxwin.image_tokens(b64=b64) == ctxwin.IMAGE_TOKENS_DEFAULT


@pytest.mark.parametrize("raw", [
    b"\xff\xd8",
    b"\xff\xd8\xff",
    b"\xff\xd8\xff\xc0",
    b"\xff\xd8" + b"\xff" * 40,
    b"\xff\xd8\xff\xe0\x00\x00" + b"\x00" * 20,      # a zero segment length
    b"\xff\xd8\xff\xe0\x00\x01" + b"\x00" * 20,      # a bad segment length
    b"\xff\xd8\xff\xe0\xff\xff",                      # a jump past the end
])
def test_odd_or_short_jpeg_bytes_never_raise(raw):
    assert ctxwin._image_dims(raw) is None


def _png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", w, h))


def _gif(w, h):
    return b"GIF89a" + struct.pack("<HH", w, h)


def _webp_vp8x(w, h):
    return (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 4
            + b"\x00" * 4 + (w - 1).to_bytes(3, "little")
            + (h - 1).to_bytes(3, "little"))


def _webp_vp8(w, h):
    return (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 " + b"\x00" * 4
            + b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", w, h))


@pytest.mark.parametrize("make", [_png, _gif, _webp_vp8x, _webp_vp8])
def test_other_formats_are_read_at_their_exact_header_length(make):
    raw = make(640, 480)
    assert ctxwin._image_dims(raw) == (640, 480)
    assert ctxwin._image_dims(raw[:-1]) is None
