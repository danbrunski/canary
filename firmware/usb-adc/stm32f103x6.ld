/* STM32F103x6 (low density, device ID 0x412): 32 KB flash, 10 KB RAM. */
MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 32K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 10K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > FLASH
    _sidata = LOADADDR(.data);
    .data : { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM AT > FLASH
    .bss : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
}
