/*
 * RAM helper, built twice: HELPER_MODE=1 clears nBOOT_SEL, HELPER_MODE=2
 * samples pins. See helper.h.
 *
 * The option-byte sequence is ST's own (stm32g0xx_hal_flash.c and
 * stm32g0xx_hal_flash_ex.c, HAL v1.4.7: HAL_FLASH_Unlock, HAL_FLASH_OB_Unlock,
 * FLASH_OB_OptrConfig, HAL_FLASHEx_OBProgram, HAL_FLASH_OB_Launch), with two
 * refusals in front of it: readout protection must be level 0 (0xAA) before
 * and after, and the new FLASH_OPTR may differ from the old in nBOOT_SEL
 * only. 0xCC in the RDP byte would be level 2, a chip locked for good.
 *
 * FLASH_OPTR reads back the value loaded at the last option-byte load, not
 * the value just written: HAL_FLASHEx_OBGetConfig warns that a read "does not
 * reflect any change that would have been programmed" since, and on this
 * board (2026-11-20) a read straight after the write returned the old word.
 * So the write cannot be checked before OPTSTRT; the helper records what the
 * register reads, and pwr_tool.py checks the option bytes after the reload.
 */
#include <stdint.h>

#include "helper.h"
#include "stm32g031xx.h"

#ifndef HELPER_MODE
#error "build with -DHELPER_MODE=HELPER_MODE_BOOT_SEL or HELPER_MODE_PINS"
#endif

#define FLASH_KEY1    0x45670123u
#define FLASH_KEY2    0xCDEF89ABu
#define FLASH_OPTKEY1 0x08192A3Bu
#define FLASH_OPTKEY2 0x4C5D6E7Fu
#define FLASH_SR_ERRORS                                                                    \
    (FLASH_SR_OPERR | FLASH_SR_PROGERR | FLASH_SR_WRPERR | FLASH_SR_PGAERR | FLASH_SR_SIZERR | \
     FLASH_SR_PGSERR | FLASH_SR_MISERR | FLASH_SR_FASTERR | FLASH_SR_RDERR | FLASH_SR_OPTVERR)

#define S ((volatile uint32_t *)HELPER_STATUS)

extern uint32_t _estack;
void helper_main(void);

/* What the bootloader's Go command reads: the stack pointer, then the entry. */
__attribute__((section(".vectors"), used)) const uintptr_t helper_vectors[2] = {
    (uintptr_t)&_estack,
    (uintptr_t)helper_main,
};

__attribute__((noreturn)) static void stop(uint32_t step)
{
    S[HS_STEP] = step;
    for (;;) {
        S[HS_COUNT]++;
    }
}

#if HELPER_MODE == HELPER_MODE_BOOT_SEL

static int flash_idle(void)
{
    /* The bootloader runs the core at 24 MHz; option programming takes tens
     * of milliseconds. This allows well over a second. */
    for (uint32_t i = 0; i < 8000000u; i++) {
        if ((FLASH->SR & (FLASH_SR_BSY1 | FLASH_SR_CFGBSY)) == 0u) {
            return 1;
        }
    }
    return 0;
}

static void run(void)
{
    uint32_t optr = FLASH->OPTR;
    S[HS_OPTR_BEFORE] = optr;
    S[HS_FLASH_SR_BEFORE] = FLASH->SR;
    S[HS_FLASH_CR_BEFORE] = FLASH->CR;
    S[HS_SECR] = FLASH->SECR;

    if ((optr & FLASH_OPTR_RDP_Msk) != 0xAAu) {
        stop(HELPER_STEP_FAIL_RDP);
    }
    if (FLASH->SECR & FLASH_SECR_BOOT_LOCK) {
        stop(HELPER_STEP_FAIL_BOOT_LOCK);
    }
    if ((optr & FLASH_OPTR_nBOOT_SEL) == 0u) {
        stop(HELPER_STEP_ALREADY_CLEAR);
    }
    uint32_t want = optr & ~FLASH_OPTR_nBOOT_SEL;
    if ((want ^ optr) != FLASH_OPTR_nBOOT_SEL || (want & FLASH_OPTR_RDP_Msk) != 0xAAu) {
        stop(HELPER_STEP_FAIL_PLAN);
    }

    if (!flash_idle()) {
        stop(HELPER_STEP_FAIL_BUSY);
    }
    FLASH->SR = FLASH_SR_ERRORS | FLASH_SR_EOP;
    if (FLASH->CR & FLASH_CR_LOCK) {
        FLASH->KEYR = FLASH_KEY1;
        FLASH->KEYR = FLASH_KEY2;
    }
    if (FLASH->CR & FLASH_CR_LOCK) {
        stop(HELPER_STEP_FAIL_LOCK);
    }
    if (FLASH->CR & FLASH_CR_OPTLOCK) {
        FLASH->OPTKEYR = FLASH_OPTKEY1;
        FLASH->OPTKEYR = FLASH_OPTKEY2;
    }
    if (FLASH->CR & FLASH_CR_OPTLOCK) {
        stop(HELPER_STEP_FAIL_OPTLOCK);
    }

    FLASH->OPTR = want;
    S[HS_OPTR_STAGED] = FLASH->OPTR;
    if (!flash_idle()) {
        stop(HELPER_STEP_FAIL_BUSY);
    }
    S[HS_OPTR_IDLE] = FLASH->OPTR;
    FLASH->CR |= FLASH_CR_OPTSTRT;
    if (!flash_idle()) {
        stop(HELPER_STEP_FAIL_BUSY);
    }
    S[HS_FLASH_SR] = FLASH->SR;
    S[HS_FLASH_CR] = FLASH->CR;
    if (FLASH->SR & FLASH_SR_ERRORS) {
        stop(HELPER_STEP_FAIL_SR);
    }

    /* Option bytes load on power-up or OBL_LAUNCH, not on a pin reset, so
     * this reset is what makes the change take effect. With the flash still
     * blank, or BOOT0 held high, the MCU comes back in the bootloader. */
    S[HS_STEP] = HELPER_STEP_LAUNCHING;
    FLASH->CR |= FLASH_CR_OBL_LAUNCH;
    stop(HELPER_STEP_LAUNCHING);
}

#elif HELPER_MODE == HELPER_MODE_PINS

static void run(void)
{
    RCC->IOPENR |= RCC_IOPENR_GPIOAEN | RCC_IOPENR_GPIOBEN | RCC_IOPENR_GPIOCEN;
    /* PA5 (button) as an input with its pull-up, as the firmware uses it.
     * PA14 stays SWCLK with its pull-down; its input path reads either way.
     * Nothing is driven. */
    GPIOA->PUPDR = (GPIOA->PUPDR & ~(3u << 10)) | (1u << 10);
    GPIOA->MODER &= ~(3u << 10);

    uint32_t last = (GPIOA->IDR >> 14) & 1u;
    S[HS_STEP] = HELPER_STEP_SAMPLING;
    for (;;) {
        uint32_t a = GPIOA->IDR;
        uint32_t boot0 = (a >> 14) & 1u;
        S[HS_COUNT]++;
        S[boot0 ? HS_PA14_HIGH : HS_PA14_LOW]++;
        if (boot0 != last) {
            S[HS_PA14_EDGES]++;
            last = boot0;
        }
        if ((a & (1u << 5)) == 0u) {
            S[HS_PA5_LOW]++;
        }
        S[HS_IDR_A] = a;
        S[HS_IDR_B] = GPIOB->IDR;
        S[HS_IDR_C] = GPIOC->IDR;
    }
}

#else
#error "unknown HELPER_MODE"
#endif

void helper_main(void)
{
    __disable_irq();
    for (unsigned i = 0; i < HELPER_STATUS_WORDS; i++) {
        S[i] = 0;
    }
    S[HS_MAGIC] = HELPER_MAGIC;
    S[HS_MODE] = HELPER_MODE;
    S[HS_STEP] = HELPER_STEP_STARTED;
    run();
    stop(S[HS_STEP]);
}
