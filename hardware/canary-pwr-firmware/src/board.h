/* CANary-PWR rev 1.0 supervisor firmware: what startup.c and main.c share. */
#ifndef PWR_BOARD_H
#define PWR_BOARD_H

#include <stdint.h>

#define PWR_FW_MAJOR    0
#define PWR_FW_MINOR    1
#define PWR_BOARD_MAJOR 1
#define PWR_BOARD_MINOR 0

/* Set by main.c's enter_bootloader() just before a software reset; startup.c
 * sees it before touching RAM and jumps into the ST system bootloader. Lives
 * in .noinit so the reset leaves it alone. */
#define PWR_BOOT_REQUEST_MAGIC 0xB0071D57u
extern volatile uint32_t pwr_boot_request;

/* ST system memory (AN2606 §47: 8 KB at 0x1FFF0000). */
#define PWR_SYSTEM_MEMORY 0x1FFF0000u

#endif /* PWR_BOARD_H */
