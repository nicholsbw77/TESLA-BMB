"""
Register map for the bq76PL536-A as exposed via the Tesla BMS slave board's
UART wrapper.

Source: collin80/TeslaBMS config.h + bq76PL536-A datasheet + the Hackaday
Model S BMS hacking project notes.
"""

# Device addressing
BROADCAST_ADDR = 0x3F      # all modules respond
UNADDRESSED_ADDR = 0x00    # any module that has not yet been assigned an address

# Read/write framing bit. The first byte of a frame is (addr << 1) | rw_bit
# where rw_bit = 1 for write, 0 for read.
RW_WRITE = 0x01
RW_READ = 0x00

# --- Register addresses ---------------------------------------------------
REG_DEV_STATUS       = 0x00
REG_GPAI             = 0x01   # General-purpose ADC input (module/pack voltage). Reading
                              # 18 bytes from here in burst mode returns:
                              # 2 bytes GPAI + 2 bytes * 6 cells + 2 bytes * 2 temps.
REG_VCELL1           = 0x03
REG_VCELL2           = 0x05
REG_VCELL3           = 0x07
REG_VCELL4           = 0x09
REG_VCELL5           = 0x0B
REG_VCELL6           = 0x0D
REG_TEMPERATURE1     = 0x0F
REG_TEMPERATURE2     = 0x11
REG_ALERT_STATUS     = 0x20
REG_FAULT_STATUS     = 0x21
REG_COV_FAULT        = 0x22   # cell over-voltage fault, one bit per cell
REG_CUV_FAULT        = 0x23   # cell under-voltage fault
REG_ADC_CTRL         = 0x30
REG_IO_CTRL          = 0x31
REG_BAL_CTRL         = 0x32
REG_BAL_TIME         = 0x33
REG_ADC_CONVERT      = 0x34
REG_ADDR_CTRL        = 0x3B
REG_RESET            = 0x3C

# --- Magic values ---------------------------------------------------------
RESET_MAGIC          = 0xA5   # write to REG_RESET to reset
ADC_CTRL_ALL         = 0x3D   # 0b00111101: ADC auto, both temps, pack, 6 cells
IO_CTRL_TEMP_VSS     = 0x03   # 0b00000011: enable temperature measurement VSS pins
ADC_CONVERT_START    = 0x01   # write to REG_ADC_CONVERT to start a conversion

# Address-assignment uses the high bit set as "I am now addressed":
ADDR_ASSIGNED_BIT    = 0x80

# --- Conversion constants -------------------------------------------------
# Voltage scale factors — calibrated against a Fluke DMM reading 21.75V
# while the tool reported 21.606V module / 21.721V cell sum.
# Correction: module * 1.006665, cell * 1.001335
CELL_V_SCALE   = 0.000382002   # V per ADC count (14-bit, 6.25V full scale)
MODULE_V_SCALE = 0.002048169   # V per ADC count for pack voltage

# Thermistor conversion — Beta equation
# Tesla BMB uses 10K NTC thermistors (B=4365K from DIY EV community research)
# The bq76PL536 temperature circuit: Vcc -> R_ref(33046Ω) -> TS_pin -> NTC -> GND
# ADC raw value = (NTC / (R_ref + NTC)) * 16383
# Verified: raw~3740 = 25°C, raw~2650 = ~34°C (plausible bench reading)
TEMP_R_REF  = 33046.0   # reference resistor in voltage divider (Ω)
TEMP_R0     = 10000.0   # NTC nominal resistance at 25°C (Ω)
TEMP_T0     = 298.15    # nominal temperature in Kelvin (25°C)
TEMP_B      = 4365.0    # Beta coefficient for Tesla NTC thermistor
