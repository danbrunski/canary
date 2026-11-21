/*
 * Reset entry, vector table and the two library calls GCC may emit.
 *
 * Reset_Handler runs from three places: a power-on or pin reset, the ST
 * bootloader's Go command (which leaves system memory mapped at 0 and the
 * core on its 24 MHz PLL, so VTOR is set here and main.c sets the clock), and
 * the software reset enter_bootloader() makes, which this file turns into a
 * jump to the bootloader before any RAM is initialised.
 */
#include <stddef.h>
#include <stdint.h>

#include "board.h"
#include "stm32g031xx.h"

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;

int main(void);
void Reset_Handler(void);
void Default_Handler(void);
void HardFault_Handler(void);
void SysTick_Handler(void);
void I2C1_IRQHandler(void);

__attribute__((section(".noinit"))) volatile uint32_t pwr_boot_request;

typedef void (*vector_t)(void);

/* Cortex-M0+ core vectors, then the STM32G031's 32 interrupt lines. Every
 * line the firmware does not use goes to Default_Handler, never to 0. */
__attribute__((section(".isr_vector"), used)) const vector_t vector_table[16 + 32] = {
    [0] = (vector_t)(uintptr_t)&_estack,
    [1] = Reset_Handler,
    [2] = Default_Handler,   /* NMI */
    [3] = HardFault_Handler,
    [11] = Default_Handler,  /* SVCall */
    [14] = Default_Handler,  /* PendSV */
    [15] = SysTick_Handler,
    [16 ... 16 + I2C1_IRQn - 1] = Default_Handler,
    [16 + I2C1_IRQn] = I2C1_IRQHandler,
    [16 + I2C1_IRQn + 1 ... 16 + 31] = Default_Handler,
};

/* Right after the vector table (offset 0xC0): pwr_tool.py refuses to flash
 * an image without it, such as a RAM helper or another board's firmware. */
__attribute__((section(".fw_tag"), used)) const char pwr_fw_tag[16] = "CANary-PWR fw";

static void jump_to_system_bootloader(void)
{
    const volatile uint32_t *system = (const volatile uint32_t *)PWR_SYSTEM_MEMORY;
    RCC->APBENR2 |= RCC_APBENR2_SYSCFGEN;
    SYSCFG->CFGR1 = (SYSCFG->CFGR1 & ~SYSCFG_CFGR1_MEM_MODE) | SYSCFG_CFGR1_MEM_MODE_0;
    SCB->VTOR = PWR_SYSTEM_MEMORY;
    __set_MSP(system[0]);
    __DSB();
    __ISB();
    ((void (*)(void))system[1])();
}

void Reset_Handler(void)
{
    /* Only a software reset asked for by enter_bootloader() goes to the
     * bootloader: power-on leaves .noinit random, so the flag is not enough
     * on its own. main() clears RCC_CSR after reading it. */
    if (pwr_boot_request == PWR_BOOT_REQUEST_MAGIC && (RCC->CSR & RCC_CSR_SFTRSTF) != 0u) {
        pwr_boot_request = 0;
        jump_to_system_bootloader();
    }
    pwr_boot_request = 0;

    SCB->VTOR = (uint32_t)(uintptr_t)vector_table;

    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) {
        *dst++ = *src++;
    }
    for (uint32_t *dst = &_sbss; dst < &_ebss;) {
        *dst++ = 0;
    }
    __enable_irq();
    (void)main();
    for (;;) {
    }
}

void Default_Handler(void)
{
    for (;;) {
    }
}

/* A fault leaves every power pin as it was (inputs, so the pull-downs keep
 * the Pi's 5 V on) and blinks the red LED fast until a reset or a reflash. */
void HardFault_Handler(void)
{
    RCC->IOPENR |= RCC_IOPENR_GPIOAEN;
    GPIOA->MODER = (GPIOA->MODER & ~(3u << (7u * 2u))) | (1u << (7u * 2u));
    for (;;) {
        GPIOA->ODR ^= 1u << 7;
        for (volatile uint32_t i = 0; i < 200000u; i++) {
        }
    }
}

/* GCC emits these for struct copies and initialisers even when freestanding;
 * -fno-tree-loop-distribute-patterns keeps it from turning them into calls
 * to themselves. */
void *memcpy(void *dst, const void *src, size_t n)
{
    uint8_t *d = dst;
    const uint8_t *s = src;
    while (n--) {
        *d++ = *s++;
    }
    return dst;
}

void *memset(void *dst, int value, size_t n)
{
    uint8_t *d = dst;
    while (n--) {
        *d++ = (uint8_t)value;
    }
    return dst;
}
