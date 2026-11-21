# CANary-PWR supervisor firmware. Builds on the Pi (Debian's gcc-arm-none-eabi;
# no newlib needed) or anywhere with an arm-none-eabi toolchain: `make`.
#
#   build/pwr.bin           the firmware, for pwr_tool.py flash
#   build/ram-boot-sel.bin  RAM helper: clear nBOOT_SEL (pwr_tool.py boot-sel)
#   build/ram-pins.bin      RAM helper: sample BOOT0 and the button (pwr_tool.py probe-pins)

CROSS   ?= arm-none-eabi-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
SIZE    := $(CROSS)size
BUILD   := build

# The checkout's commit, readable back from the running board (PWR_REG_BUILD_ID).
BUILD_ID ?= $(shell git rev-parse --short=8 HEAD 2>/dev/null || echo 0)

ARCH    := -mcpu=cortex-m0plus -mthumb
CFLAGS  := $(ARCH) -std=gnu11 -Os -g3 -Wall -Wextra -Werror \
           -ffreestanding -fno-common -ffunction-sections -fdata-sections \
           -fno-tree-loop-distribute-patterns \
           -DSTM32G031xx -Isrc -Iram -Ivendor/st -Ivendor/arm
LDFLAGS := $(ARCH) -nostdlib -Wl,--gc-sections

HEADERS := $(wildcard src/*.h ram/*.h)

.PHONY: all clean
all: $(BUILD)/pwr.bin $(BUILD)/ram-boot-sel.bin $(BUILD)/ram-pins.bin
	@$(SIZE) $(BUILD)/pwr.elf $(BUILD)/ram-boot-sel.elf $(BUILD)/ram-pins.elf

$(BUILD):
	mkdir -p $@

$(BUILD)/pwr.elf: src/startup.c src/main.c stm32g031k8.ld $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -DBUILD_ID=0x$(BUILD_ID)u src/startup.c src/main.c \
	    $(LDFLAGS) -T stm32g031k8.ld -Wl,-Map=$(BUILD)/pwr.map -lgcc -o $@

$(BUILD)/ram-boot-sel.elf: ram/helper.c ram/helper.ld $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -DHELPER_MODE=HELPER_MODE_BOOT_SEL ram/helper.c \
	    $(LDFLAGS) -Wl,--no-warn-rwx-segments -T ram/helper.ld -o $@

$(BUILD)/ram-pins.elf: ram/helper.c ram/helper.ld $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -DHELPER_MODE=HELPER_MODE_PINS ram/helper.c \
	    $(LDFLAGS) -Wl,--no-warn-rwx-segments -T ram/helper.ld -o $@

$(BUILD)/%.bin: $(BUILD)/%.elf
	$(OBJCOPY) -O binary $< $@

clean:
	rm -rf $(BUILD)
