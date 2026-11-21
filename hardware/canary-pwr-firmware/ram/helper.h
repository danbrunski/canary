/*
 * The RAM helper: a few hundred bytes the ST bootloader loads into SRAM and
 * starts with its Go command (stm32flash -w ... -g ...). It is how the Pi
 * changes the option bytes and tests the reflash lines without an SWD probe.
 *
 * pwr_tool.py parses this file for the addresses, the status layout and the
 * step codes, so the two cannot drift. Status words are u32, little-endian,
 * at HELPER_STATUS + 4 * HS_<name>. SRAM survives the resets in between
 * (NRST and the option-byte reload), and the bootloader uses only the first
 * 4 KB (AN2606 §47), so the Pi reads the status back through it afterwards.
 */
#ifndef PWR_RAM_HELPER_H
#define PWR_RAM_HELPER_H

#define HELPER_LOAD          0x20001000 /* image start: [initial SP, entry] */
#define HELPER_STATUS        0x20001E00
#define HELPER_STATUS_WORDS  20
#define HELPER_MAGIC         0x31485750 /* "PWH1" */

#define HELPER_MODE_BOOT_SEL 1 /* clear nBOOT_SEL, nothing else, then reload the option bytes */
#define HELPER_MODE_PINS     2 /* sample BOOT0 (PA14) and the button (PA5) until reset */

#define HS_MAGIC        0
#define HS_MODE         1
#define HS_STEP         2
#define HS_OPTR_BEFORE  3
#define HS_OPTR_STAGED  4  /* FLASH_OPTR read straight after writing it (see helper.c) */
#define HS_FLASH_SR     5
#define HS_FLASH_CR     6
#define HS_SECR         7
#define HS_COUNT        8  /* sampling passes, or spins after stopping */
#define HS_PA14_HIGH    9
#define HS_PA14_LOW     10
#define HS_PA14_EDGES   11
#define HS_PA5_LOW      12
#define HS_IDR_A        13
#define HS_IDR_B        14
#define HS_IDR_C        15
#define HS_FLASH_SR_BEFORE 16 /* FLASH_SR and FLASH_CR as the bootloader left them */
#define HS_FLASH_CR_BEFORE 17
#define HS_OPTR_IDLE    18 /* FLASH_OPTR after BSY1 and CFGBSY clear, before OPTSTRT */
#define HS_SPARE        19

#define HELPER_STEP_STARTED        0x01
#define HELPER_STEP_ALREADY_CLEAR  0x02 /* nBOOT_SEL was already 0: nothing written */
#define HELPER_STEP_LAUNCHING      0x03 /* programmed; OBL_LAUNCH set (the reset follows) */
#define HELPER_STEP_SAMPLING       0x04
#define HELPER_STEP_FAIL_RDP       0x81 /* RDP is not 0xAA: refused before writing */
#define HELPER_STEP_FAIL_BOOT_LOCK 0x82
#define HELPER_STEP_FAIL_PLAN      0x83 /* the new OPTR would change more than nBOOT_SEL */
#define HELPER_STEP_FAIL_BUSY      0x84
#define HELPER_STEP_FAIL_LOCK      0x85
#define HELPER_STEP_FAIL_OPTLOCK   0x86
#define HELPER_STEP_FAIL_SR        0x88 /* a flash error flag after OPTSTRT */

#endif /* PWR_RAM_HELPER_H */
