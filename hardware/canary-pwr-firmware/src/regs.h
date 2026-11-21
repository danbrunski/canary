/*
 * CANary-PWR supervisor: the I2C register map the Pi reads.
 *
 * The one copy of this contract. The firmware's struct pwr_regs is checked
 * against these offsets at compile time, and pwr_tool.py parses this file at
 * run time, so neither side can drift from it. Rules for editing it:
 *
 *   - every PWR_REG_* line's comment starts with the field's type:
 *     char4, u8, u16, s16, u32, or u16[PWR_LTC_COUNT];
 *   - fields are naturally aligned (a u16 at an even offset, a u32 at a
 *     multiple of 4), so the main loop's single stores never tear a field
 *     the I2C interrupt is copying;
 *   - everything is little-endian, and every value is raw: the scales live
 *     in pwr_tool.py next to the datasheet figure each comes from.
 *
 * Bus: the Pi's I2C1 (GPIO2/3) through the TCA9511A, the same pins the ST
 * system bootloader answers on at 0x56. A read returns a snapshot taken when
 * the read starts. Write the register pointer first, then read. Reads past
 * PWR_REG_SIZE return 0xFF.
 */
#ifndef PWR_REGS_H
#define PWR_REGS_H

#define PWR_I2C_ADDR           0x2E

#define PWR_REG_MAGIC          0x00 /* char4 "CPWR" */
#define PWR_REG_FW_MAJOR       0x04 /* u8 */
#define PWR_REG_FW_MINOR       0x05 /* u8 */
#define PWR_REG_BOARD_MAJOR    0x06 /* u8 board revision, 1 for rev 1.0 */
#define PWR_REG_BOARD_MINOR    0x07 /* u8 */
#define PWR_REG_BUILD_ID       0x08 /* u32 git short hash the image was built from, 0 if unknown */
#define PWR_REG_UPTIME_MS      0x0C /* u32 milliseconds since reset */
#define PWR_REG_OPTR           0x10 /* u32 FLASH_OPTR as loaded (RDP, nBOOT_SEL, ...) */
#define PWR_REG_RESET_FLAGS    0x14 /* u32 RCC_CSR at boot (reset cause), cleared after copying */
#define PWR_REG_INPUTS         0x18 /* u16 PWR_IN_* bits, pin levels now */
#define PWR_REG_BUTTON_PRESSES 0x1A /* u16 debounced presses since the trackers were cleared */
#define PWR_REG_DEVICES        0x1C /* u16 PWR_DEV_* bits: the device's last transfer succeeded */
#define PWR_REG_BUS_ERRORS     0x1E /* u16 failed private-bus transfers since boot, saturating */
#define PWR_REG_INA_BUS        0x20 /* u16 INA226 bus voltage, Pi 5 V, 1.25 mV/LSB */
#define PWR_REG_INA_SHUNT      0x22 /* s16 INA226 shunt voltage, 2.5 uV/LSB across R28 */
#define PWR_REG_INA_BUS_MIN    0x24 /* u16 lowest bus reading since the trackers were cleared */
#define PWR_REG_INA_BUS_MAX    0x26 /* u16 highest bus reading */
#define PWR_REG_INA_SHUNT_MAX  0x28 /* s16 highest shunt reading (peak Pi current) */
#define PWR_REG_INA_ID         0x2A /* u16 INA226 manufacturer ID, 0x5449 once it has answered */
#define PWR_REG_INA_SAMPLES    0x2C /* u32 bus readings behind the trackers */
#define PWR_REG_ADC_VIN        0x30 /* u16 PA0 VIN_SENSE, 12-bit, mean of PWR_ADC_MEAN conversions */
#define PWR_REG_ADC_PI3V3      0x32 /* u16 PA4 PI_3V3_SENSE, same */
#define PWR_REG_ADC_VREFINT    0x34 /* u16 internal reference, same; gives VDDA */
#define PWR_REG_VREFINT_CAL    0x36 /* u16 factory VREFINT reading at VDDA = 3.0 V */
#define PWR_REG_ADC_VIN_MIN    0x38 /* u16 lowest single VIN conversion since the trackers were cleared */
#define PWR_REG_ADC_VIN_MAX    0x3A /* u16 highest single VIN conversion */
#define PWR_REG_LOOP_HZ        0x3C /* u16 main-loop passes in the last second */
#define PWR_REG_RESERVED       0x3E /* u16 */
#define PWR_REG_LTC            0x40 /* u16[PWR_LTC_COUNT] LTC3350 registers, in PWR_LTC_REGS order */
#define PWR_REG_SIZE           0x68

#define PWR_ADC_MEAN           16

/* The LTC3350 registers mirrored at PWR_REG_LTC: vcapfb_dac, vshunt, ctl_reg,
 * then num_caps through meas_dtemp. Read-only here: the bring-up firmware
 * never writes the LTC3350. */
#define PWR_LTC_COUNT          20
#define PWR_LTC_REGS           { 0x05, 0x06, 0x17, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, \
                                 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2A }

/* Writes: [PWR_REG_COMMAND, command, key...] in one transaction. */
#define PWR_REG_COMMAND        0x70
#define PWR_CMD_CLEAR_TRACKERS 0xC1 /* no key */
#define PWR_CMD_BOOTLOADER     0xB7 /* key "LDR": reset into the ST system bootloader at 0x56 */
#define PWR_CMD_BOOTLOADER_KEY "LDR"

/* PWR_REG_INPUTS: bit numbers. 1 = the pin reads high, except BUTTON (1 =
 * pressed). The firmware drives none of the last four: it reads them so the
 * Pi can see their state, and their pull resistors decide it. */
#define PWR_IN_BUTTON          0  /* PA5, SW1 to GND, internal pull-up */
#define PWR_IN_BOOT0           1  /* PA14 SWCLK/BOOT0, high while the Pi holds GPIO27 */
#define PWR_IN_LTC_PFO         2  /* PB8, low = LTC3350 input below PFI */
#define PWR_IN_LTC_SMBALERT    3  /* PB9, low = LTC3350 alert */
#define PWR_IN_BUCK_PG         4  /* PC14, LM61460 PGOOD */
#define PWR_IN_CAN_FAULT_N     5  /* PB5, TCAN1043 nFAULT */
#define PWR_IN_CAN_RXD         6  /* PB4, TCAN1043 RXD, low = wake or bus traffic */
#define PWR_IN_CAN_EN          7  /* PA15, not driven */
#define PWR_IN_CAN_STB_N       8  /* PB3, not driven */
#define PWR_IN_PI_5V_OFF       9  /* PB0, not driven: high would switch the Pi's 5 V off */
#define PWR_IN_PWR_FAIL_N      10 /* PA8 ~PWR_FAIL, not driven: high = power OK to the Pi */

/* PWR_REG_DEVICES: bit numbers. */
#define PWR_DEV_INA226         0
#define PWR_DEV_LTC3350        1

#endif /* PWR_REGS_H */
