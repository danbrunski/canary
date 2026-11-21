/* STM32G031K8: 64 KB flash at 0x08000000, 8 KB SRAM at 0x20000000. */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 8K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);
_min_stack = 1024;

SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .fw_tag : { KEEP(*(.fw_tag)) } > FLASH
  ASSERT(ADDR(.fw_tag) == 0x080000C0, "the firmware tag must follow the 48-entry vector table")

  .text :
  {
    *(.text .text.*)
    *(.rodata .rodata.*)
    . = ALIGN(4);
  } > FLASH

  .ARM.exidx : { *(.ARM.exidx*) } > FLASH

  _sidata = LOADADDR(.data);
  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data .data.*)
    . = ALIGN(4);
    _edata = .;
  } > RAM AT > FLASH

  .bss (NOLOAD) :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss .bss.*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  .noinit (NOLOAD) :
  {
    . = ALIGN(4);
    *(.noinit .noinit.*)
    . = ALIGN(4);
    _end_static = .;
  } > RAM

  ASSERT(_end_static <= _estack - _min_stack, "RAM: less than 1 KB left for the stack")
}
