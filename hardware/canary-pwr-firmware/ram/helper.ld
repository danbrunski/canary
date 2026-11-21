/* The RAM helper runs where the bootloader writes it: above the 4 KB the
 * bootloader keeps for itself (AN2606 §47), below the status words. */
ENTRY(helper_main)

MEMORY
{
  HELPER (rwx) : ORIGIN = 0x20001000, LENGTH = 0xE00
}

_estack = 0x20002000;

SECTIONS
{
  .text :
  {
    KEEP(*(.vectors))
    *(.text .text.*)
    *(.rodata .rodata.*)
    *(.data .data.*)
    . = ALIGN(4);
  } > HELPER

  .bss (NOLOAD) : { *(.bss .bss.*) *(COMMON) } > HELPER

  ASSERT(SIZEOF(.bss) == 0, "the RAM helper cannot use .bss: nothing zeroes it")
  ASSERT(ADDR(.text) == 0x20001000, "HELPER_LOAD in helper.h has moved")
}
