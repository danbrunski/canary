# CANary USB ADC firmware. Needs gcc-arm-none-eabi (installed on the Pi).
#   make                     -> canary-usb-adc.bin (v2: 503.9 Hz, filter in fir_taps.h)
#   make flash               -> stm32flash over the module's own USB port (BOOT0 = 3V3 first)

PORT ?= /dev/serial/by-path/platform-xhci-hcd.1-usb-0:2:1.0-port0

CC = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
CFLAGS = -mcpu=cortex-m3 -mthumb -Os -std=c11 -Wall -Wextra -Werror \
         -ffreestanding -fno-common -ffunction-sections
LDFLAGS = -nostdlib -nostartfiles -T stm32f103x6.ld -Wl,--gc-sections

canary-usb-adc.bin: canary-usb-adc.elf
	$(OBJCOPY) -O binary $< $@
	arm-none-eabi-size $<

canary-usb-adc.elf: main.c filter.h fir_taps.h stm32f103x6.ld
	$(CC) $(CFLAGS) $(LDFLAGS) main.c -o $@

flash: canary-usb-adc.bin
	stm32flash -b 115200 -w $< -v $(PORT)

clean:
	rm -f canary-usb-adc.elf canary-usb-adc.bin

.PHONY: flash clean
