"""
CRC8 used by the Tesla BMS slave board UART protocol.

Polynomial: 0x07 (x^8 + x^2 + x + 1), non-reflected, init=0.
This matches crcmod.mkCrcFun(0x107, initCrc=0, rev=False) as used in
the original working TeslaBMS_02.py by Jarrod Tuma / Hackaday project.

Verified: CRC of [0x7f, 0x3c, 0xa5] = 0x57, which matches the expected
broadcast reset reply CRC from the collin80/TeslaBMS source.
"""

_CRC8_TABLE = []
for _byte in range(256):
    _crc = _byte
    for _ in range(8):
        if _crc & 0x80:
            _crc = ((_crc << 1) ^ 0x07) & 0xFF
        else:
            _crc = (_crc << 1) & 0xFF
    _CRC8_TABLE.append(_crc)


def crc8(data: bytes) -> int:
    """Compute CRC8 over the given bytes (poly=0x07, init=0, non-reflected)."""
    crc = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc & 0xFF


def append_crc(data: bytes) -> bytes:
    return data + bytes([crc8(data)])


def verify_crc(frame: bytes) -> bool:
    """Verify the last byte of `frame` is the CRC8 of everything before it."""
    if len(frame) < 2:
        return False
    return crc8(frame[:-1]) == frame[-1]
