/*
 * CANary-PWR rev 1.0 supervisor: bring-up firmware.
 *
 * Observes and reports; switches nothing. It reads the INA226 (Pi 5 V volts
 * and amps) and the LTC3350 (input, bank, cells, currents, temperature) on
 * the private I2C bus, the MCU's own ADC (VIN_SENSE, Pi 3V3) and the status
 * pins, and serves all of it to the Pi as the register map in regs.h. The
 * green LED is a heartbeat; the red LED means a private-bus device is not
 * answering.
 *
 * What it never does: drive PB0 (PI_5V_OFF, high = the Pi's 5 V off) or PA8
 * (~PWR_FAIL to the Pi), write the LTC3350, or start the watchdog. Those pins
 * are read as plain inputs, so their pull resistors set them exactly as when
 * the MCU is held in reset: the default-on rule of the PWR spec (§P7). The
 * supervisor's state machine is later work.
 */
#include <stddef.h>
#include <stdint.h>

#include "board.h"
#include "regs.h"
#include "stm32g031xx.h"

#ifndef BUILD_ID
#define BUILD_ID 0u
#endif

/* Private bus (I2C2 on PA11/PA12, 4.7k to +3V3_AON). */
#define INA226_ADDR    0x40u
#define LTC3350_ADDR   0x09u
#define INA226_MFG_ID  0x5449u
/* Continuous shunt and bus, 332 us each, no averaging: about 1.5 bus readings
 * per millisecond, fast enough for the minimum tracker to see a dip. */
#define INA226_CONFIG  0x4097u

/* I2CCLK = PCLK = 16 MHz. RM0444's 16 MHz examples: 100 kHz for the private
 * bus; the 400 kHz value's setup and hold delays for the target, which uses
 * only those (the Pi sets the clock). */
#define I2C_TIMING_100K 0x30420F13u
#define I2C_TIMING_400K 0x10320309u

#define VREFINT_CAL_ADDR 0x1FFF75AAu

struct pwr_regs {
    char magic[4];
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t board_major;
    uint8_t board_minor;
    uint32_t build_id;
    uint32_t uptime_ms;
    uint32_t optr;
    uint32_t reset_flags;
    uint16_t inputs;
    uint16_t button_presses;
    uint16_t devices;
    uint16_t bus_errors;
    uint16_t ina_bus;
    int16_t ina_shunt;
    uint16_t ina_bus_min;
    uint16_t ina_bus_max;
    int16_t ina_shunt_max;
    uint16_t ina_id;
    uint32_t ina_samples;
    uint16_t adc_vin;
    uint16_t adc_pi3v3;
    uint16_t adc_vrefint;
    uint16_t vrefint_cal;
    uint16_t adc_vin_min;
    uint16_t adc_vin_max;
    uint16_t loop_hz;
    uint16_t reserved;
    uint16_t ltc[PWR_LTC_COUNT];
};

#define AT(field, reg) _Static_assert(offsetof(struct pwr_regs, field) == (reg), #field " offset")
AT(magic, PWR_REG_MAGIC);
AT(fw_major, PWR_REG_FW_MAJOR);
AT(fw_minor, PWR_REG_FW_MINOR);
AT(board_major, PWR_REG_BOARD_MAJOR);
AT(board_minor, PWR_REG_BOARD_MINOR);
AT(build_id, PWR_REG_BUILD_ID);
AT(uptime_ms, PWR_REG_UPTIME_MS);
AT(optr, PWR_REG_OPTR);
AT(reset_flags, PWR_REG_RESET_FLAGS);
AT(inputs, PWR_REG_INPUTS);
AT(button_presses, PWR_REG_BUTTON_PRESSES);
AT(devices, PWR_REG_DEVICES);
AT(bus_errors, PWR_REG_BUS_ERRORS);
AT(ina_bus, PWR_REG_INA_BUS);
AT(ina_shunt, PWR_REG_INA_SHUNT);
AT(ina_bus_min, PWR_REG_INA_BUS_MIN);
AT(ina_bus_max, PWR_REG_INA_BUS_MAX);
AT(ina_shunt_max, PWR_REG_INA_SHUNT_MAX);
AT(ina_id, PWR_REG_INA_ID);
AT(ina_samples, PWR_REG_INA_SAMPLES);
AT(adc_vin, PWR_REG_ADC_VIN);
AT(adc_pi3v3, PWR_REG_ADC_PI3V3);
AT(adc_vrefint, PWR_REG_ADC_VREFINT);
AT(vrefint_cal, PWR_REG_VREFINT_CAL);
AT(adc_vin_min, PWR_REG_ADC_VIN_MIN);
AT(adc_vin_max, PWR_REG_ADC_VIN_MAX);
AT(loop_hz, PWR_REG_LOOP_HZ);
AT(reserved, PWR_REG_RESERVED);
AT(ltc, PWR_REG_LTC);
_Static_assert(sizeof(struct pwr_regs) == PWR_REG_SIZE, "register map size");

static const uint8_t ltc_regs[PWR_LTC_COUNT] = PWR_LTC_REGS;

static volatile uint32_t ms;

/* Written by the main loop, copied by the I2C interrupt when a read starts.
 * Every field is naturally aligned, so no single store can tear. */
static struct pwr_regs live;

/* I2C target state, owned by the interrupt. A write's bytes after the pointer
 * go to cmd_buf; on STOP the interrupt hands them to the main loop through
 * cmd_ready, and drops new writes until the main loop has taken them. */
static uint8_t snapshot[PWR_REG_SIZE];
static volatile uint8_t reg_ptr;
static volatile uint8_t rx_count;
static volatile uint8_t write_reg;
static volatile uint8_t cmd_ready;
static volatile uint8_t cmd_reg;
static volatile uint8_t cmd_len;
static volatile uint8_t cmd_buf[8];

#define barrier() __asm volatile("" ::: "memory")

void SysTick_Handler(void)
{
    ms++;
}

static void delay_us(uint32_t us)
{
    for (volatile uint32_t i = 0; i < us * 4u; i++) {
    }
}

/* ---- GPIO ------------------------------------------------------------- */

enum { MODE_INPUT = 0, MODE_OUTPUT = 1, MODE_AF = 2, MODE_ANALOG = 3 };
enum { PULL_NONE = 0, PULL_UP = 1 };

static void pin_mode(GPIO_TypeDef *port, unsigned pin, unsigned mode)
{
    port->MODER = (port->MODER & ~(3u << (pin * 2u))) | (mode << (pin * 2u));
}

static void pin_pull(GPIO_TypeDef *port, unsigned pin, unsigned pull)
{
    port->PUPDR = (port->PUPDR & ~(3u << (pin * 2u))) | (pull << (pin * 2u));
}

static void pin_input(GPIO_TypeDef *port, unsigned pin, unsigned pull)
{
    pin_pull(port, pin, pull);
    pin_mode(port, pin, MODE_INPUT);
}

static void pin_i2c(GPIO_TypeDef *port, unsigned pin)
{
    port->OTYPER |= 1u << pin;
    pin_pull(port, pin, PULL_NONE);
    port->AFR[pin >> 3] = (port->AFR[pin >> 3] & ~(0xFu << ((pin & 7u) * 4u))) |
                          (6u << ((pin & 7u) * 4u)); /* AF6: I2C1 on PB6/PB7, I2C2 on PA11/PA12 */
    pin_mode(port, pin, MODE_AF);
}

static void gpio_init(void)
{
    RCC->IOPENR |= RCC_IOPENR_GPIOAEN | RCC_IOPENR_GPIOBEN | RCC_IOPENR_GPIOCEN;

    GPIOA->BRR = (1u << 6) | (1u << 7);
    pin_mode(GPIOA, 6, MODE_OUTPUT); /* LED_STATUS, green */
    pin_mode(GPIOA, 7, MODE_OUTPUT); /* LED_BANK, red */

    pin_input(GPIOA, 5, PULL_UP);  /* BUTTON */
    pin_input(GPIOB, 8, PULL_UP);  /* LTC_PFO, open drain */
    pin_input(GPIOB, 9, PULL_UP);  /* LTC_SMBALERT, open drain */
    pin_input(GPIOC, 14, PULL_UP); /* BUCK_PG, open drain */
    pin_input(GPIOB, 5, PULL_UP);  /* CAN_FAULT_N, open drain */
    pin_input(GPIOB, 4, PULL_UP);  /* CAN_RXD */

    /* Read, never driven: the pull resistors on the board decide these. */
    pin_input(GPIOA, 15, PULL_NONE); /* CAN_EN */
    pin_input(GPIOB, 3, PULL_NONE);  /* CAN_STB_N */
    pin_input(GPIOB, 0, PULL_NONE);  /* PI_5V_OFF: 100k pull-down keeps the buck on */
    pin_input(GPIOA, 8, PULL_NONE);  /* ~PWR_FAIL: 10k pull-up to the Pi's 3V3 */
}

/* The two pins whose output would act on the Pi stay inputs, whatever any
 * other code here does to MODER. */
static void keep_power_pins_passive(void)
{
    if ((GPIOB->MODER & (3u << 0)) != (MODE_INPUT << 0)) {
        pin_input(GPIOB, 0, PULL_NONE);
    }
    if ((GPIOA->MODER & (3u << 16)) != (MODE_INPUT << 16)) {
        pin_input(GPIOA, 8, PULL_NONE);
    }
}

static uint16_t read_inputs(uint8_t button_pressed)
{
    uint32_t a = GPIOA->IDR, b = GPIOB->IDR, c = GPIOC->IDR;
    uint16_t in = 0;
    in |= (uint16_t)(button_pressed ? 1u : 0u) << PWR_IN_BUTTON;
    in |= (uint16_t)((a >> 14) & 1u) << PWR_IN_BOOT0;
    in |= (uint16_t)((b >> 8) & 1u) << PWR_IN_LTC_PFO;
    in |= (uint16_t)((b >> 9) & 1u) << PWR_IN_LTC_SMBALERT;
    in |= (uint16_t)((c >> 14) & 1u) << PWR_IN_BUCK_PG;
    in |= (uint16_t)((b >> 5) & 1u) << PWR_IN_CAN_FAULT_N;
    in |= (uint16_t)((b >> 4) & 1u) << PWR_IN_CAN_RXD;
    in |= (uint16_t)((a >> 15) & 1u) << PWR_IN_CAN_EN;
    in |= (uint16_t)((b >> 3) & 1u) << PWR_IN_CAN_STB_N;
    in |= (uint16_t)((b >> 0) & 1u) << PWR_IN_PI_5V_OFF;
    in |= (uint16_t)((a >> 8) & 1u) << PWR_IN_PWR_FAIL_N;
    return in;
}

/* ---- Clock ------------------------------------------------------------ */

/* HSI16 straight through, no PLL. The bootloader's Go command arrives with
 * the core on a 24 MHz PLL and one flash wait state, so set every field. */
static void clock_init(void)
{
    RCC->CR |= RCC_CR_HSION;
    while ((RCC->CR & RCC_CR_HSIRDY) == 0u) {
    }
    RCC->CR &= ~RCC_CR_HSIDIV;
    RCC->CFGR &= ~(RCC_CFGR_SW | RCC_CFGR_HPRE | RCC_CFGR_PPRE);
    while ((RCC->CFGR & RCC_CFGR_SWS) != 0u) {
    }
    RCC->CR &= ~RCC_CR_PLLON;
    FLASH->ACR = (FLASH->ACR & ~FLASH_ACR_LATENCY) | FLASH_ACR_ICEN | FLASH_ACR_PRFTEN;
    SysTick_Config(16000u);
}

/* ---- I2C1: the Pi's target ------------------------------------------- */

static void pi_bus_init(void)
{
    RCC->APBRSTR1 |= RCC_APBRSTR1_I2C1RST;
    RCC->APBRSTR1 &= ~RCC_APBRSTR1_I2C1RST;
    RCC->APBENR1 |= RCC_APBENR1_I2C1EN;
    pin_i2c(GPIOB, 6);
    pin_i2c(GPIOB, 7);
    I2C1->TIMINGR = I2C_TIMING_400K;
    I2C1->OAR1 = 0;
    I2C1->OAR1 = I2C_OAR1_OA1EN | ((uint32_t)PWR_I2C_ADDR << 1);
    I2C1->CR1 = I2C_CR1_ADDRIE | I2C_CR1_RXIE | I2C_CR1_TXIE | I2C_CR1_STOPIE | I2C_CR1_NACKIE |
                I2C_CR1_ERRIE | I2C_CR1_PE;
    NVIC_SetPriority(I2C1_IRQn, 0);
    NVIC_EnableIRQ(I2C1_IRQn);
}

void I2C1_IRQHandler(void)
{
    uint32_t isr = I2C1->ISR;

    if (isr & I2C_ISR_ADDR) {
        if (isr & I2C_ISR_DIR) {
            /* The Pi reads: freeze a copy, so a multi-byte read is coherent. */
            const uint8_t *src = (const uint8_t *)&live;
            for (unsigned i = 0; i < PWR_REG_SIZE; i++) {
                snapshot[i] = src[i];
            }
            I2C1->ISR = I2C_ISR_TXE; /* drop a byte left over from the last read */
        } else {
            rx_count = 0;
        }
        I2C1->ICR = I2C_ICR_ADDRCF;
    }
    if (isr & I2C_ISR_RXNE) {
        uint8_t byte = (uint8_t)I2C1->RXDR;
        if (rx_count == 0u) {
            reg_ptr = byte;
            write_reg = byte;
        } else if (!cmd_ready && rx_count - 1u < sizeof cmd_buf) {
            cmd_buf[rx_count - 1u] = byte;
        }
        if (rx_count < 255u) {
            rx_count++;
        }
    }
    if (isr & I2C_ISR_TXIS) {
        uint8_t p = reg_ptr;
        I2C1->TXDR = p < PWR_REG_SIZE ? snapshot[p] : 0xFFu;
        if (p < 255u) {
            reg_ptr = (uint8_t)(p + 1u);
        }
    }
    if (isr & I2C_ISR_NACKF) {
        I2C1->ICR = I2C_ICR_NACKCF;
    }
    if (isr & I2C_ISR_STOPF) {
        I2C1->ICR = I2C_ICR_STOPCF;
        if (rx_count > 1u && !cmd_ready) {
            cmd_reg = write_reg;
            cmd_len = (uint8_t)(rx_count - 1u > sizeof cmd_buf ? sizeof cmd_buf : rx_count - 1u);
            cmd_ready = 1;
        }
        rx_count = 0;
    }
    if (isr & (I2C_ISR_BERR | I2C_ISR_ARLO | I2C_ISR_OVR)) {
        I2C1->ICR = I2C_ICR_BERRCF | I2C_ICR_ARLOCF | I2C_ICR_OVRCF;
    }
}

/* ---- I2C2: the private bus -------------------------------------------- */

static void priv_bus_init(void)
{
    RCC->APBRSTR1 |= RCC_APBRSTR1_I2C2RST;
    RCC->APBRSTR1 &= ~RCC_APBRSTR1_I2C2RST;
    RCC->APBENR1 |= RCC_APBENR1_I2C2EN;

    /* A reset mid-transfer can leave a target holding SDA low: clock it
     * free, then send a STOP, before handing the pins to the peripheral. */
    GPIOA->OTYPER |= (1u << 11) | (1u << 12);
    GPIOA->BSRR = (1u << 11) | (1u << 12);
    pin_pull(GPIOA, 11, PULL_NONE);
    pin_pull(GPIOA, 12, PULL_NONE);
    pin_mode(GPIOA, 11, MODE_OUTPUT);
    pin_mode(GPIOA, 12, MODE_OUTPUT);
    for (unsigned i = 0; i < 9u && (GPIOA->IDR & (1u << 12)) == 0u; i++) {
        GPIOA->BRR = 1u << 11;
        delay_us(5);
        GPIOA->BSRR = 1u << 11;
        delay_us(5);
    }
    GPIOA->BRR = 1u << 12;
    delay_us(5);
    GPIOA->BSRR = 1u << 12;
    delay_us(5);

    pin_i2c(GPIOA, 11);
    pin_i2c(GPIOA, 12);
    I2C2->TIMINGR = I2C_TIMING_100K;
    I2C2->CR1 = I2C_CR1_PE;
}

static void priv_reset_peripheral(void)
{
    I2C2->CR1 &= ~I2C_CR1_PE;
    while (I2C2->CR1 & I2C_CR1_PE) {
    }
    delay_us(2);
    I2C2->CR1 |= I2C_CR1_PE;
}

enum { XFER_OK = 0, XFER_NACK = -1, XFER_BUS = -2, XFER_TIMEOUT = -3 };

static int priv_wait(uint32_t flag, uint32_t t0)
{
    for (;;) {
        uint32_t isr = I2C2->ISR;
        if (isr & flag) {
            return XFER_OK;
        }
        if (isr & I2C_ISR_NACKF) {
            return XFER_NACK;
        }
        if (isr & (I2C_ISR_BERR | I2C_ISR_ARLO)) {
            return XFER_BUS;
        }
        if ((uint32_t)(ms - t0) > 3u) {
            return XFER_TIMEOUT;
        }
    }
}

/* Write wn bytes then (repeated start) read rn bytes. */
static int priv_xfer(uint8_t addr, const uint8_t *w, uint32_t wn, uint8_t *r, uint32_t rn)
{
    uint32_t t0 = ms;
    int rc = XFER_OK;

    while (I2C2->ISR & I2C_ISR_BUSY) {
        if ((uint32_t)(ms - t0) > 3u) {
            rc = XFER_TIMEOUT;
            goto fail;
        }
    }
    I2C2->ICR = I2C_ICR_STOPCF | I2C_ICR_NACKCF | I2C_ICR_BERRCF | I2C_ICR_ARLOCF;
    if (wn) {
        I2C2->CR2 = ((uint32_t)addr << 1) | (wn << I2C_CR2_NBYTES_Pos) |
                    (rn ? 0u : I2C_CR2_AUTOEND) | I2C_CR2_START;
        for (uint32_t i = 0; i < wn; i++) {
            if ((rc = priv_wait(I2C_ISR_TXIS, t0)) != XFER_OK) {
                goto fail;
            }
            I2C2->TXDR = w[i];
        }
        if ((rc = priv_wait(rn ? I2C_ISR_TC : I2C_ISR_STOPF, t0)) != XFER_OK) {
            goto fail;
        }
    }
    if (rn) {
        I2C2->CR2 = ((uint32_t)addr << 1) | I2C_CR2_RD_WRN | (rn << I2C_CR2_NBYTES_Pos) |
                    I2C_CR2_AUTOEND | I2C_CR2_START;
        for (uint32_t i = 0; i < rn; i++) {
            if ((rc = priv_wait(I2C_ISR_RXNE, t0)) != XFER_OK) {
                goto fail;
            }
            r[i] = (uint8_t)I2C2->RXDR;
        }
        if ((rc = priv_wait(I2C_ISR_STOPF, t0)) != XFER_OK) {
            goto fail;
        }
    }
    I2C2->ICR = I2C_ICR_STOPCF;
    return XFER_OK;

fail:
    if (rc == XFER_NACK) {
        /* The controller sends STOP on its own after a NACK. */
        (void)priv_wait(I2C_ISR_STOPF, ms);
        I2C2->ICR = I2C_ICR_NACKCF | I2C_ICR_STOPCF;
    } else {
        priv_reset_peripheral();
    }
    if (live.bus_errors < 0xFFFFu) {
        live.bus_errors++;
    }
    return rc;
}

static void device_seen(unsigned bit, int ok)
{
    if (ok) {
        live.devices |= (uint16_t)(1u << bit);
    } else {
        live.devices &= (uint16_t)~(1u << bit);
    }
}

/* INA226: big-endian 16-bit registers. */
static int ina_read(uint8_t reg, uint16_t *value)
{
    uint8_t b[2];
    int rc = priv_xfer(INA226_ADDR, &reg, 1, b, 2);
    if (rc == XFER_OK) {
        *value = (uint16_t)((b[0] << 8) | b[1]);
    }
    return rc;
}

static int ina_write(uint8_t reg, uint16_t value)
{
    uint8_t b[3] = {reg, (uint8_t)(value >> 8), (uint8_t)value};
    return priv_xfer(INA226_ADDR, b, 3, NULL, 0);
}

/* LTC3350: SMBus words, low byte first. */
static int ltc_read(uint8_t reg, uint16_t *value)
{
    uint8_t b[2];
    int rc = priv_xfer(LTC3350_ADDR, &reg, 1, b, 2);
    if (rc == XFER_OK) {
        *value = (uint16_t)(b[0] | (b[1] << 8));
    }
    return rc;
}

static uint8_t ina_ready;
static uint8_t ina_read_shunt;
static uint32_t ina_retry_at;

static void ina_poll(uint32_t now)
{
    uint16_t v;
    if (!ina_ready) {
        if ((int32_t)(now - ina_retry_at) < 0) {
            return;
        }
        ina_retry_at = now + 1000u;
        int ok = ina_read(0xFE, &v) == XFER_OK && v == INA226_MFG_ID &&
                 ina_write(0x00, INA226_CONFIG) == XFER_OK;
        device_seen(PWR_DEV_INA226, ok);
        if (ok) {
            live.ina_id = v;
            ina_ready = 1;
        }
        return;
    }
    if (ina_read_shunt) {
        int ok = ina_read(0x01, &v) == XFER_OK;
        if (ok) {
            int16_t shunt = (int16_t)v;
            live.ina_shunt = shunt;
            if (shunt > live.ina_shunt_max) {
                live.ina_shunt_max = shunt;
            }
        }
        device_seen(PWR_DEV_INA226, ok);
        ina_ready = (uint8_t)ok;
    } else {
        int ok = ina_read(0x02, &v) == XFER_OK;
        if (ok) {
            live.ina_bus = v;
            if (v < live.ina_bus_min) {
                live.ina_bus_min = v;
            }
            if (v > live.ina_bus_max) {
                live.ina_bus_max = v;
            }
            if (live.ina_samples < 0xFFFFFFFFu) {
                live.ina_samples++;
            }
        }
        device_seen(PWR_DEV_INA226, ok);
        ina_ready = (uint8_t)ok;
    }
    ina_read_shunt ^= 1u;
}

static unsigned ltc_index;

static void ltc_poll_one(void)
{
    uint16_t v;
    int ok = ltc_read(ltc_regs[ltc_index], &v) == XFER_OK;
    if (ok) {
        live.ltc[ltc_index] = v;
    }
    device_seen(PWR_DEV_LTC3350, ok);
    ltc_index = (ltc_index + 1u) % PWR_LTC_COUNT;
}

/* ---- ADC ---------------------------------------------------------------- */

static void adc_init(void)
{
    RCC->APBRSTR2 |= RCC_APBRSTR2_ADCRST;
    RCC->APBRSTR2 &= ~RCC_APBRSTR2_ADCRST;
    RCC->APBENR2 |= RCC_APBENR2_ADCEN;

    ADC1->CFGR2 = ADC_CFGR2_CKMODE_0; /* PCLK / 2 = 8 MHz */
    ADC1->CR = ADC_CR_ADVREGEN;
    delay_us(50);                     /* tADCVREG_STUP is 20 us */
    ADC1->CR |= ADC_CR_ADCAL;
    while (ADC1->CR & ADC_CR_ADCAL) {
    }
    delay_us(5);
    ADC1->CFGR1 = 0;   /* 12-bit, right-aligned, single sequence, ascending */
    ADC1->SMPR = 7u;   /* 160.5 cycles (20 us): VIN_SENSE is a 130k source, VREFINT needs 4 us */
    ADC1_COMMON->CCR |= ADC_CCR_VREFEN;
    ADC1->ISR = ADC_ISR_ADRDY;
    ADC1->CR |= ADC_CR_ADEN;
    while ((ADC1->ISR & ADC_ISR_ADRDY) == 0u) {
    }
    ADC1->ISR = ADC_ISR_CCRDY;
    ADC1->CHSELR = ADC_CHSELR_CHSEL0 | ADC_CHSELR_CHSEL4 | ADC_CHSELR_CHSEL13;
    while ((ADC1->ISR & ADC_ISR_CCRDY) == 0u) {
    }
    live.vrefint_cal = *(const volatile uint16_t *)VREFINT_CAL_ADDR;
}

/* One sequence: PA0 (VIN_SENSE), PA4 (PI_3V3_SENSE), VREFINT. */
static int adc_sample(uint16_t out[3])
{
    ADC1->ISR = ADC_ISR_EOC | ADC_ISR_EOS | ADC_ISR_OVR;
    ADC1->CR |= ADC_CR_ADSTART;
    for (unsigned i = 0; i < 3u; i++) {
        uint32_t t0 = ms;
        while ((ADC1->ISR & ADC_ISR_EOC) == 0u) {
            if ((uint32_t)(ms - t0) > 2u) {
                return 0;
            }
        }
        out[i] = (uint16_t)ADC1->DR;
    }
    return 1;
}

static uint32_t adc_sum[3];
static unsigned adc_count;

static void adc_poll(void)
{
    uint16_t s[3];
    if (!adc_sample(s)) {
        return;
    }
    if (s[0] < live.adc_vin_min) {
        live.adc_vin_min = s[0];
    }
    if (s[0] > live.adc_vin_max) {
        live.adc_vin_max = s[0];
    }
    for (unsigned i = 0; i < 3u; i++) {
        adc_sum[i] += s[i];
    }
    if (++adc_count == PWR_ADC_MEAN) {
        live.adc_vin = (uint16_t)(adc_sum[0] / PWR_ADC_MEAN);
        live.adc_pi3v3 = (uint16_t)(adc_sum[1] / PWR_ADC_MEAN);
        live.adc_vrefint = (uint16_t)(adc_sum[2] / PWR_ADC_MEAN);
        adc_sum[0] = adc_sum[1] = adc_sum[2] = 0;
        adc_count = 0;
    }
}

/* ---- Commands from the Pi --------------------------------------------- */

static void trackers_clear(void)
{
    live.ina_bus_min = 0xFFFFu;
    live.ina_bus_max = 0;
    live.ina_shunt_max = INT16_MIN;
    live.ina_samples = 0;
    live.adc_vin_min = 0xFFFFu;
    live.adc_vin_max = 0;
    live.button_presses = 0;
}

static void enter_bootloader(void)
{
    /* Let go of the Pi's bus first, so it sees 0x2E vanish before 0x56
     * appears. startup.c makes the jump after the reset. */
    I2C1->CR1 = 0;
    NVIC_DisableIRQ(I2C1_IRQn);
    pwr_boot_request = PWR_BOOT_REQUEST_MAGIC;
    __DSB();
    NVIC_SystemReset();
}

static void handle_command(void)
{
    if (!cmd_ready) {
        return;
    }
    uint8_t buf[sizeof cmd_buf];
    uint8_t len = cmd_len;
    for (unsigned i = 0; i < len; i++) {
        buf[i] = cmd_buf[i];
    }
    uint8_t reg = cmd_reg;
    cmd_ready = 0;

    if (reg != PWR_REG_COMMAND || len == 0u) {
        return;
    }
    if (buf[0] == PWR_CMD_CLEAR_TRACKERS) {
        trackers_clear();
    } else if (buf[0] == PWR_CMD_BOOTLOADER && len == 4u) {
        static const char key[] = PWR_CMD_BOOTLOADER_KEY;
        if (buf[1] == (uint8_t)key[0] && buf[2] == (uint8_t)key[1] && buf[3] == (uint8_t)key[2]) {
            enter_bootloader();
        }
    }
}

/* ---- Main ---------------------------------------------------------------- */

int main(void)
{
    clock_init();

    live.reset_flags = RCC->CSR;
    RCC->CSR |= RCC_CSR_RMVF;

    /* The option-byte loader set PROGEMPTY when it found the flash blank; it
     * stays set until the next OBL, and with it a pin reset with BOOT0 low
     * still lands in the bootloader (AN2606 pattern 11). Clear it so a reset
     * comes back here. */
    if (FLASH->ACR & FLASH_ACR_PROGEMPTY) {
        FLASH->ACR &= ~FLASH_ACR_PROGEMPTY;
    }

    live.magic[0] = 'C';
    live.magic[1] = 'P';
    live.magic[2] = 'W';
    live.magic[3] = 'R';
    live.fw_major = PWR_FW_MAJOR;
    live.fw_minor = PWR_FW_MINOR;
    live.board_major = PWR_BOARD_MAJOR;
    live.board_minor = PWR_BOARD_MINOR;
    live.build_id = BUILD_ID;
    live.optr = FLASH->OPTR;
    trackers_clear();

    gpio_init();
    adc_init();
    priv_bus_init();
    pi_bus_init();

    uint32_t last_ltc = 0, last_10ms = 0, last_second = 0, passes = 0;
    uint8_t button_stable = 0, button_count = 0;

    for (;;) {
        uint32_t now = ms;
        keep_power_pins_passive();

        ina_poll(now);
        if (now - last_ltc >= 5u) {
            last_ltc = now;
            ltc_poll_one();
        }
        if (now - last_10ms >= 10u) {
            last_10ms = now;
            adc_poll();
            uint8_t pressed = (GPIOA->IDR & (1u << 5)) == 0u;
            if (pressed != button_stable) {
                if (++button_count >= 3u) {
                    button_stable = pressed;
                    button_count = 0;
                    if (pressed && live.button_presses < 0xFFFFu) {
                        live.button_presses++;
                    }
                }
            } else {
                button_count = 0;
            }
        }
        live.inputs = read_inputs(button_stable);

        passes++;
        if (now - last_second >= 1000u) {
            last_second = now;
            live.loop_hz = (uint16_t)(passes > 0xFFFFu ? 0xFFFFu : passes);
            passes = 0;
        }
        live.uptime_ms = now;

        /* LEDs: both on for the first second (lamp test); then green blinks
         * 100 ms a second, or stays on while the button is held, and red is
         * on while the INA226 or the LTC3350 is not answering. */
        uint32_t all = (1u << PWR_DEV_INA226) | (1u << PWR_DEV_LTC3350);
        int lamp_test = now < 1000u;
        int green = lamp_test || button_stable || (now % 1000u) < 100u;
        int red = lamp_test || (live.devices & all) != all;
        GPIOA->BSRR = (green ? (1u << 6) : (1u << (6 + 16))) | (red ? (1u << 7) : (1u << (7 + 16)));

        barrier();
        handle_command();
    }
}
