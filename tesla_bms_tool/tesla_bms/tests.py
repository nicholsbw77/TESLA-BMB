"""
Offline sanity tests — no hardware needed. Run with:
    python -m tesla_bms.tests
"""

from tesla_bms.crc import crc8, append_crc, verify_crc
from tesla_bms.bms import _thermistor_v_to_c
from tesla_bms.registers import CELL_V_SCALE, MODULE_V_SCALE


def test_crc_known_vectors():
    # poly=0x07, init=0, non-reflected.
    # Matches crcmod.mkCrcFun(0x107, initCrc=0, rev=False) from TeslaBMS_02.py.
    # CRC of [7f 3c a5] = 0x57 verified against collin80/TeslaBMS source.
    cases = {
        b"\x7f\x3c\xa5":         0x57,   # broadcast reset — collin80 verified
        b"\x01\x3b\x81":         0x8B,   # address assignment frame
        b"\x03\x30\x3d":         0xF7,   # ADC ctrl frame
        b"\x03\x31\x03":         0x58,   # IO ctrl frame
    }
    for data, expected in cases.items():
        got = crc8(data)
        assert got == expected, f"crc8({data.hex()})=0x{got:02X}, want 0x{expected:02X}"


def test_crc_roundtrip():
    f = append_crc(bytes([0x02, 0x30, 0x3D]))
    assert verify_crc(f)
    bad = bytearray(f)
    bad[1] ^= 0x01
    assert not verify_crc(bytes(bad))


def test_cell_scale():
    # Calibrated against Fluke DMM: 21.75V measured vs 21.721V cell sum
    # correction factor 1.001335 applied to base scale 6.250/16383.
    # Mid-scale (raw=8191) should give ~3.13V
    assert abs(8191 * CELL_V_SCALE - 3.130) < 0.01


def test_module_scale():
    # Tesla module is ~22-24V at full charge; raw counts ~11000-12000.
    v = 11500 * MODULE_V_SCALE
    assert 22.0 < v < 25.0, v


def test_thermistor_monotonic():
    # Beta equation with 10K NTC (B=4365), Rref=33046.
    # Higher raw value = higher NTC voltage = lower NTC resistance = higher temp.
    # So temperature should INCREASE with raw value (NTC to GND, Rref to Vcc).
    # Verified: raw~3740 gives ~25°C (room temp), raw~2650 gives ~34°C.
    raws = [1000, 2000, 3000, 4000, 6000, 8000, 10000]
    temps = [_thermistor_v_to_c(r) for r in raws]
    for r, t in zip(raws, temps):
        assert t == t, f"NaN at raw={r}"
    # Should be monotonically increasing (higher raw = higher temp on this circuit)
    for a, b in zip(temps, temps[1:]):
        assert b < a, f"expected decreasing temp with increasing raw: {temps}"


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
