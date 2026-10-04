/*
 * CANary firmware for the NOYITO 10-channel USB ADC module
 * (STM32F103x6, low density: 32 KB flash, 10 KB RAM; CH340 on USART1).
 *
 * Replaces the stock firmware's fixed 2 Hz frame of all ten inputs with the
 * configured inputs only, each averaged over the whole output period, at
 * OUTPUT_HZ. The text format is the stock one minus the volts column, so
 * src/collection/usb_adc_collector.py reads both without a change:
 *
 *     CH0:2114\r\n
 *     \r\n                      <- one blank line ends every frame
 *
 * Values are 12-bit counts (0..4095), the mean of every conversion in the
 * period (a boxcar: ~420 conversions per 10 ms, shared by the inputs),
 * rounded. A watchdog resets the chip if the loop ever stops for ~1 s. Clock is the internal 8 MHz RC oscillator through the PLL to
 * 64 MHz, so nothing depends on which crystal the board carries; the Pi
 * stamps lines on arrival, so the RC's ~1 % rate error does not reach the
 * data.
 *
 * Bare metal, no vendor headers or libc. Build and flash: README.md.
 */
#include <stdint.h>

#ifndef OUTPUT_HZ
#define OUTPUT_HZ 100u
#endif

/* ADC inputs to report, in order. The module's CHn is ADC_INn (PA0..PA7, PB0, PB1). */
static const uint8_t CHANNELS[] = {0};
#define N_CHANNELS (sizeof(CHANNELS) / sizeof(CHANNELS[0]))

#define SYSCLK_HZ 64000000u
#define BAUD 115200u

#define REG(addr) (*(volatile uint32_t *)(addr))

/* RCC */
#define RCC_CR REG(0x40021000u)
#define RCC_CFGR REG(0x40021004u)
#define RCC_APB2ENR REG(0x40021018u)
#define RCC_CR_PLLON (1u << 24)
#define RCC_CR_PLLRDY (1u << 25)
#define RCC_CFGR_SW_PLL 0x2u
#define RCC_CFGR_SWS_MASK (0x3u << 2)
#define RCC_CFGR_SWS_PLL (0x2u << 2)
#define RCC_CFGR_PPRE1_DIV2 (0x4u << 8)
#define RCC_CFGR_ADCPRE_DIV6 (0x2u << 14) /* 64 / 6 = 10.67 MHz, under the 14 MHz limit */
#define RCC_CFGR_PLLMUL16 (0xEu << 18)    /* PLLSRC = 0: HSI / 2 = 4 MHz, x16 = 64 MHz */
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
#define ADC1_SR REG(0x40012400u)
#define ADC1_CR2 REG(0x40012408u)
#define ADC1_SMPR2 REG(0x40012410u)
#define ADC1_SQR1 REG(0x4001242Cu)
#define ADC1_SQR3 REG(0x40012434u)
#define ADC1_DR REG(0x4001244Cu)
#define ADC_SR_EOC (1u << 1)
#define ADC_CR2_ADON (1u << 0)
#define ADC_CR2_CAL (1u << 2)
#define ADC_CR2_RSTCAL (1u << 3)
#define ADC_CR2_EXTSEL_SWSTART (0x7u << 17)
#define ADC_CR2_EXTTRIG (1u << 20)
#define ADC_CR2_SWSTART (1u << 22)

/* Independent watchdog, clocked by the ~40 kHz LSI */
#define IWDG_KR REG(0x40003000u)
#define IWDG_PR REG(0x40003004u)
#define IWDG_RLR REG(0x40003008u)

/* SysTick */
#define SYST_CSR REG(0xE000E010u)
#define SYST_RVR REG(0xE000E014u)
#define SYST_CVR REG(0xE000E018u)
#define SYST_CSR_ENABLE (1u << 0)
#define SYST_CSR_CLKSOURCE (1u << 2)
#define SYST_CSR_COUNTFLAG (1u << 16)

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
    /* Analog mode (CNF=00, MODE=00) for every reported input. */
    for (unsigned i = 0; i < N_CHANNELS; i++) {
        uint8_t ch = CHANNELS[i];
        if (ch < 8) {
            GPIOA_CRL &= ~(0xFu << (ch * 4u));
        } else {
            GPIOB_CRL &= ~(0xFu << ((ch - 8u) * 4u));
        }
    }
    /* PA9: alternate-function push-pull, 50 MHz (0xB). PA10 stays a floating input. */
    GPIOA_CRH = (GPIOA_CRH & ~(0xFu << 4)) | (0xBu << 4);
}

static void usart_init(void) {
    USART1_BRR = (SYSCLK_HZ + BAUD / 2u) / BAUD; /* 556: 115108 baud, 0.08 % slow */
    USART1_CR1 = USART_CR1_UE | USART_CR1_TE;
}

static void adc_init(void) {
    ADC1_SMPR2 = 0x3FFFFFFFu; /* 239.5 cycles on IN0..IN9: 23.6 us per conversion */
    ADC1_SQR1 = 0;            /* one conversion per sequence */
    ADC1_CR2 = ADC_CR2_ADON;
    delay_cycles(1000); /* t_STAB, 1 us */
    ADC1_CR2 |= ADC_CR2_RSTCAL;
    while (ADC1_CR2 & ADC_CR2_RSTCAL) {
    }
    ADC1_CR2 |= ADC_CR2_CAL;
    while (ADC1_CR2 & ADC_CR2_CAL) {
    }
    ADC1_CR2 = ADC_CR2_ADON | ADC_CR2_EXTTRIG | ADC_CR2_EXTSEL_SWSTART;
}

/* Transmit ring, drained one byte at a time between conversions so the
 * averaging window never stalls on the UART. */
#define TX_SIZE 256u
static uint8_t tx_buf[TX_SIZE];
static volatile uint32_t tx_head;
static uint32_t tx_tail;

static void tx_put(uint8_t c) {
    uint32_t next = (tx_head + 1u) % TX_SIZE;
    if (next == tx_tail) {
        return; /* full: drop rather than block (cannot happen at 115200 baud) */
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

static void emit_frame(const uint32_t *sum, const uint32_t *count) {
    for (unsigned i = 0; i < N_CHANNELS; i++) {
        uint32_t mean = count[i] ? (sum[i] + count[i] / 2u) / count[i] : 0u;
        tx_put('C');
        tx_put('H');
        tx_uint(CHANNELS[i]);
        tx_put(':');
        /* Zero-padded to four digits, as the stock firmware prints them. */
        if (mean < 1000u) tx_put('0');
        if (mean < 100u) tx_put('0');
        if (mean < 10u) tx_put('0');
        tx_uint(mean);
        tx_put('\r');
        tx_put('\n');
    }
    tx_put('\r');
    tx_put('\n');
}

int main(void) {
    clock_init();
    gpio_init();
    usart_init();
    adc_init();
    watchdog_init();

    SYST_RVR = SYSCLK_HZ / OUTPUT_HZ - 1u;
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_ENABLE;

    uint32_t sum[N_CHANNELS] = {0};
    uint32_t count[N_CHANNELS] = {0};
    unsigned slot = 0;

    ADC1_SQR3 = CHANNELS[0];
    ADC1_CR2 |= ADC_CR2_SWSTART;
    for (;;) {
        IWDG_KR = 0xAAAAu;
        tx_pump();
        if (ADC1_SR & ADC_SR_EOC) {
            sum[slot] += ADC1_DR & 0xFFFu; /* reading DR clears EOC */
            count[slot]++;
            slot = (slot + 1u) % N_CHANNELS;
            ADC1_SQR3 = CHANNELS[slot];
            ADC1_CR2 |= ADC_CR2_SWSTART;
        }
        if (SYST_CSR & SYST_CSR_COUNTFLAG) { /* reading clears it */
            emit_frame(sum, count);
            for (unsigned i = 0; i < N_CHANNELS; i++) {
                sum[i] = 0;
                count[i] = 0;
            }
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
    Default_Handler, /* SysTick: polled, never enabled as an interrupt */
};
