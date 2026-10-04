/*
 * CANary firmware for the NOYITO 10-channel USB ADC module
 * (STM32F103x6, low density: 32 KB flash, 10 KB RAM; CH340 on USART1).
 *
 * Version 2: one input, anti-alias filtered, at 503.9 Hz. The ADC converts
 * ADC_INPUT continuously into a DMA ring (42,328 conversions/s, no gaps);
 * filter.h sums each 21 and runs a 64-tap FIR on those, keeping every 4th
 * (design_fir.py, which writes fir_taps.h). The output passes 0..180 Hz and
 * rejects what would alias into 0..200 Hz by >= 74 dB, so the rail's injector
 * pulsation can be studied rather than averaged away. Version 1 (git history)
 * was a 100 Hz boxcar that let 50 Hz through at -4 dB.
 *
 * Every output line is the stock text format plus a sequence number:
 *
 *     CH0:2114\t7342\r\n        <- counts (12-bit, 4 digits), tab, seq mod 10000
 *     \r\n                      <- one blank line ends every frame
 *
 * and about once a second a frame carries one more line first:
 *
 *     FW:2 503905 15864\r\n     <- version, output rate (mHz), group delay (us)
 *
 * so the collector can stamp each sample by its sequence number instead of
 * its USB arrival, and say how late the filter makes it. Collectors that
 * predate version 2 still read the counts: their pattern stops at the tab.
 *
 * The rate comes from the ADC clock, which is the internal 8 MHz RC through
 * the PLL (64 MHz): nominal, and ~1 % off at worst. The collector measures
 * the true rate against the Pi's clock. A watchdog resets the chip if the
 * loop ever stops for ~1 s.
 *
 * Bare metal, no vendor headers or libc. Build and flash: README.md.
 */
#include <stdint.h>

#include "filter.h"

#define FIRMWARE_VERSION 2u
/* The input to read: the module's CHn is ADC_INn (PA0..PA7, PB0, PB1).
 * One input only: continuous conversion into the DMA ring keeps the rate
 * exact, and reading several would need scan mode. */
#define ADC_INPUT 0u
/* Frames between FW lines: about one a second. */
#define FW_EVERY 504u

#define SYSCLK_HZ 64000000u
#define BAUD 115200u

/* Wire budget: 17 bytes a frame ("CH0:2114\t7342\r\n\r\n") at ~504 Hz is
 * 8.6 kB/s of the link's 11.5 kB/s; refuse a build that would not fit. */
#define FRAME_BYTES 17u
_Static_assert((uint64_t)FRAME_BYTES * OUTPUT_MHZ / 1000u < (BAUD / 10u) * 85u / 100u,
               "output rate too high for the serial link");

#define REG(addr) (*(volatile uint32_t *)(addr))

/* RCC */
#define RCC_CR REG(0x40021000u)
#define RCC_CFGR REG(0x40021004u)
#define RCC_AHBENR REG(0x40021014u)
#define RCC_APB2ENR REG(0x40021018u)
#define RCC_CR_PLLON (1u << 24)
#define RCC_CR_PLLRDY (1u << 25)
#define RCC_CFGR_SW_PLL 0x2u
#define RCC_CFGR_SWS_MASK (0x3u << 2)
#define RCC_CFGR_SWS_PLL (0x2u << 2)
#define RCC_CFGR_PPRE1_DIV2 (0x4u << 8)
#define RCC_CFGR_ADCPRE_DIV6 (0x2u << 14) /* 64 / 6 = 10.67 MHz, under the 14 MHz limit */
#define RCC_CFGR_PLLMUL16 (0xEu << 18)    /* PLLSRC = 0: HSI / 2 = 4 MHz, x16 = 64 MHz */
#define RCC_AHBENR_DMA1EN (1u << 0)
#define RCC_APB2ENR_AFIOEN (1u << 0)
#define RCC_APB2ENR_IOPAEN (1u << 2)
#define RCC_APB2ENR_IOPBEN (1u << 3)
#define RCC_APB2ENR_ADC1EN (1u << 9)
#define RCC_APB2ENR_USART1EN (1u << 14)

/* Flash: two wait states above 48 MHz */
#define FLASH_ACR REG(0x40022000u)
#define FLASH_ACR_LATENCY_2 0x2u
#define FLASH_ACR_PRFTBE (1u << 4)

/* GPIO */
#define GPIOA_CRL REG(0x40010800u)
#define GPIOA_CRH REG(0x40010804u)
#define GPIOB_CRL REG(0x40010C00u)

/* USART1: PA9 TX, PA10 RX */
#define USART1_SR REG(0x40013800u)
#define USART1_DR REG(0x40013804u)
#define USART1_BRR REG(0x40013808u)
#define USART1_CR1 REG(0x4001380Cu)
#define USART_SR_TXE (1u << 7)
#define USART_CR1_UE (1u << 13)
#define USART_CR1_TE (1u << 3)

/* ADC1 */
#define ADC1_CR2 REG(0x40012408u)
#define ADC1_SMPR2 REG(0x40012410u)
#define ADC1_SQR1 REG(0x4001242Cu)
#define ADC1_SQR3 REG(0x40012434u)
#define ADC1_DR_ADDR 0x4001244Cu
#define ADC_CR2_ADON (1u << 0)
#define ADC_CR2_CONT (1u << 1)
#define ADC_CR2_CAL (1u << 2)
#define ADC_CR2_RSTCAL (1u << 3)
#define ADC_CR2_DMA (1u << 8)
#define ADC_CR2_EXTSEL_SWSTART (0x7u << 17)
#define ADC_CR2_EXTTRIG (1u << 20)
#define ADC_CR2_SWSTART (1u << 22)

/* DMA1 channel 1 (ADC1's request line) */
#define DMA1_CCR1 REG(0x40020008u)
#define DMA1_CNDTR1 REG(0x4002000Cu)
#define DMA1_CPAR1 REG(0x40020010u)
#define DMA1_CMAR1 REG(0x40020014u)
#define DMA_CCR_EN (1u << 0)
#define DMA_CCR_CIRC (1u << 5)
#define DMA_CCR_MINC (1u << 7)
#define DMA_CCR_PSIZE_16 (1u << 8)
#define DMA_CCR_MSIZE_16 (1u << 10)
#define DMA_CCR_PL_HIGH (2u << 12)

/* Independent watchdog, clocked by the ~40 kHz LSI */
#define IWDG_KR REG(0x40003000u)
#define IWDG_PR REG(0x40003004u)
#define IWDG_RLR REG(0x40003008u)

static void watchdog_init(void) {
    IWDG_KR = 0xCCCCu; /* start */
    IWDG_KR = 0x5555u; /* unlock PR/RLR */
    IWDG_PR = 4u;      /* /64: 625 Hz */
    IWDG_RLR = 625u;   /* ~1 s */
    IWDG_KR = 0xAAAAu; /* reload */
}

static void delay_cycles(uint32_t n) {
    while (n--) {
        __asm__ volatile("nop");
    }
}

static void clock_init(void) {
    FLASH_ACR = FLASH_ACR_PRFTBE | FLASH_ACR_LATENCY_2;
    RCC_CFGR = RCC_CFGR_PLLMUL16 | RCC_CFGR_ADCPRE_DIV6 | RCC_CFGR_PPRE1_DIV2;
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) {
    }
    RCC_CFGR |= RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL) {
    }
}

static void gpio_init(void) {
    RCC_APB2ENR |= RCC_APB2ENR_AFIOEN | RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPBEN |
                   RCC_APB2ENR_ADC1EN | RCC_APB2ENR_USART1EN;
    /* Analog mode (CNF=00, MODE=00) for the input. */
#if ADC_INPUT < 8u
    GPIOA_CRL &= ~(0xFu << (ADC_INPUT * 4u));
#else
    GPIOB_CRL &= ~(0xFu << ((ADC_INPUT - 8u) * 4u));
#endif
    /* PA9: alternate-function push-pull, 50 MHz (0xB). PA10 stays a floating input. */
    GPIOA_CRH = (GPIOA_CRH & ~(0xFu << 4)) | (0xBu << 4);
}

static void usart_init(void) {
    USART1_BRR = (SYSCLK_HZ + BAUD / 2u) / BAUD; /* 556: 115108 baud, 0.08 % slow */
    USART1_CR1 = USART_CR1_UE | USART_CR1_TE;
}

/* The DMA ring: 512 conversions is 12 ms of slack for the main loop, which
 * needs well under 1 ms to drain it. */
#define RING 512u
static volatile uint16_t ring[RING];

static void adc_dma_init(void) {
    RCC_AHBENR |= RCC_AHBENR_DMA1EN;
    DMA1_CPAR1 = ADC1_DR_ADDR;
    DMA1_CMAR1 = (uint32_t)(uintptr_t)ring;
    DMA1_CNDTR1 = RING;
    DMA1_CCR1 = DMA_CCR_CIRC | DMA_CCR_MINC | DMA_CCR_PSIZE_16 | DMA_CCR_MSIZE_16 |
                DMA_CCR_PL_HIGH | DMA_CCR_EN;

    ADC1_SMPR2 = 0x3FFFFFFFu; /* 239.5 cycles on IN0..IN9: 252 ADC clocks a conversion */
    ADC1_SQR1 = 0;            /* one conversion per sequence */
    ADC1_SQR3 = ADC_INPUT;
    ADC1_CR2 = ADC_CR2_ADON;
    delay_cycles(1000); /* t_STAB, 1 us */
    ADC1_CR2 |= ADC_CR2_RSTCAL;
    while (ADC1_CR2 & ADC_CR2_RSTCAL) {
    }
    ADC1_CR2 |= ADC_CR2_CAL;
    while (ADC1_CR2 & ADC_CR2_CAL) {
    }
    ADC1_CR2 = ADC_CR2_ADON | ADC_CR2_CONT | ADC_CR2_DMA | ADC_CR2_EXTTRIG |
               ADC_CR2_EXTSEL_SWSTART;
    ADC1_CR2 |= ADC_CR2_SWSTART;
}

/* Transmit ring, drained one byte at a time so the loop never waits on the
 * UART. */
#define TX_SIZE 256u
static uint8_t tx_buf[TX_SIZE];
static uint32_t tx_head;
static uint32_t tx_tail;

static void tx_put(uint8_t c) {
    uint32_t next = (tx_head + 1u) % TX_SIZE;
    if (next == tx_tail) {
        return; /* full: drop rather than block (the wire budget above prevents it) */
    }
    tx_buf[tx_head] = c;
    tx_head = next;
}

static void tx_pump(void) {
    if (tx_tail != tx_head && (USART1_SR & USART_SR_TXE)) {
        USART1_DR = tx_buf[tx_tail];
        tx_tail = (tx_tail + 1u) % TX_SIZE;
    }
}

static void tx_uint(uint32_t v) {
    char digits[10];
    unsigned n = 0;
    do {
        digits[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n) {
        tx_put((uint8_t)digits[--n]);
    }
}

static void tx_padded4(uint32_t v) {
    if (v < 1000u) tx_put('0');
    if (v < 100u) tx_put('0');
    if (v < 10u) tx_put('0');
    tx_uint(v);
}

static void emit_frame(uint32_t counts, uint32_t seq) {
    if (seq % FW_EVERY == 0u) {
        tx_put('F');
        tx_put('W');
        tx_put(':');
        tx_uint(FIRMWARE_VERSION);
        tx_put(' ');
        tx_uint(OUTPUT_MHZ);
        tx_put(' ');
        tx_uint(GROUP_DELAY_US);
        tx_put('\r');
        tx_put('\n');
    }
    tx_put('C');
    tx_put('H');
    tx_uint(ADC_INPUT);
    tx_put(':');
    tx_padded4(counts); /* zero-padded to four digits, as the stock firmware prints them */
    tx_put('\t');
    tx_uint(seq % 10000u);
    tx_put('\r');
    tx_put('\n');
    tx_put('\r');
    tx_put('\n');
}

int main(void) {
    clock_init();
    gpio_init();
    usart_init();
    adc_dma_init();
    watchdog_init();

    static filter_t filter;
    uint32_t seq = 0;
    uint32_t tail = 0; /* next ring slot to read */

    for (;;) {
        IWDG_KR = 0xAAAAu;
        tx_pump();
        /* CNDTR counts down from RING as the DMA writes, reloading at 0. */
        uint32_t head = (RING - DMA1_CNDTR1) % RING;
        while (tail != head) {
            uint32_t counts;
            if (filter_push(&filter, ring[tail] & 0xFFFu, &counts)) {
                emit_frame(counts, seq++);
            }
            tail = (tail + 1u) % RING;
            tx_pump();
        }
    }
}

/* ---- startup ---- */

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;

void Reset_Handler(void) {
    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) {
        *dst++ = *src++;
    }
    for (uint32_t *dst = &_sbss; dst < &_ebss;) {
        *dst++ = 0;
    }
    main();
    for (;;) {
    }
}

void Default_Handler(void) {
    for (;;) {
    }
}

/* Cortex-M3 core vectors; no peripheral interrupt is enabled. */
__attribute__((section(".isr_vector"), used)) static void (*const vectors[16])(void) = {
    (void (*)(void))(&_estack),
    Reset_Handler,
    Default_Handler, /* NMI */
    Default_Handler, /* HardFault */
    Default_Handler, /* MemManage */
    Default_Handler, /* BusFault */
    Default_Handler, /* UsageFault */
    0, 0, 0, 0,
    Default_Handler, /* SVCall */
    Default_Handler, /* DebugMon */
    0,
    Default_Handler, /* PendSV */
    Default_Handler, /* SysTick: never enabled */
};
