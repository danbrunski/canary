# Reversing ECU firmware / ROM tables

When the live stream isn't enough — you need the calibration maps, the meaning of
an OEM DID, or the seed-key algorithm to read protected engine data — you go into
the firmware. From *The Car Hacker's Handbook* Ch. 6 (by Dave Blundell).

**Reality check:** cold code analysis of a modern ECU is a hundreds-of-hours effort
(1–2 MB of machine code; even a 1995 32 KB ECU is ~10,000 instructions). Do it when
the payoff (understanding/changing what the ECU computes) justifies it; prefer
data-level wins (table finding, bin comparison) over full disassembly when possible.

## Getting the firmware: front door vs backdoor

**Front door** — the OEM's own reprogramming path through OBD-II:
- **J2534** (SAE J2534-1): the standardized Windows→vehicle API OEM tools use to
  flash/diagnose. Its value to you is *observation* — a J2534 **shim DLL**
  man-in-the-middles and logs every API call between the OEM app and the vehicle;
  or run the OEM tool and sniff the resulting bus traffic. Either way you learn how
  the OEM reads/reprograms, which is often the key to the "front door."
- **KWP2000 (ISO 14230)**: pre-J2534 application protocol over CAN or ISO-9141;
  huge ecosystem of serial/USB flasher tools.
- **Seed-key (SecurityAccess `0x27`)** is the lock. ECU sends a pseudo-random
  **seed**; tool must return the correct **key**. No standard algorithm — seed/key
  widths vary (16/32-bit) and the transform is per-platform, usually simple
  arithmetic over embedded constants. Recover it by: disassembling firmware to find
  the key routine; disassembling the legitimate PC tool (e.g. J2534 reflash sw);
  observing many seed/key pairs for patterns; or spoofing a real tool to harvest
  pairs for seeds you choose.

**Backdoor** — treat the ECU as the embedded system it is. Often *more* practical
than cracking the front-door lock: pull a dump directly. Start by analyzing the
board — largest chips first (CPU, memory), datasheet the part numbers, watch for
memory chips (EEPROM/FlashROM/serial flash/NVSRAM). Any nonvolatile chip can be
read (and replaced) off-board. Newer MCUs increasingly hold everything in internal
flash (no external memory to lift).

## Once you have a binary blob

You have raw machine code + parameters, 4 KB–4 MB. To make sense of it:

**Identify the chip/architecture first.** The disassembler and every address
assumption depend on it. Read the part markings or datasheet-match. Wrong arch ⇒
plausible-looking but nonsensical disassembly — "clean" disassembly (logical,
self-consistent) is the signal you picked right.

**Use the memory map.** From the datasheet: address width and the **interrupt
vector table** location. Worked example (1990 300ZX, HD6303, 32 KB EPROM):
addressable 64 KB, vector table at top of memory, reset vector at `0xFFFE/0xFFFF`
→ a 0x7FFF blob must load at `0x8000` to end at `0xFFFF`. The reset vector points
to the entry routine (often first thing it does is zero a RAM region).

## Finding the interesting data (without full code analysis)

- **Self-diagnostic routines.** Every ECU has fault-detection code that names its
  sensors. Find where a specific error code is set → you've found the internal
  variables/memory locations for that sensor (e.g. the IAT-sensor error handler
  leads to the IAT variable). An excellent first foothold.
- **Table-lookup library functions.** ECUs fetch calibration through a few shared
  lookup routines (1D byte arrays, 2D word maps, 3D short maps — each type tends to
  have its own reference function). They're called constantly (easy to spot), use
  program-memory access instructions (unique signature), cluster together in memory,
  and are heavy on interpolation math. Find one → find the rest → find every table
  and the variables that index them.
- **Physics-shaped tables.** A sensor transfer table has a shape physics dictates,
  so it's recognizable as raw values in ROM. The MAF VQ curve (voltage/frequency →
  airflow) follows King's Law — a characteristic nonlinear curve, similar across
  OEMs (Ford, Nissan). Visualizers (`analyze.exe`, WinOLS 2D/3D views) color cells
  by value so a curve or map stands out from surrounding code. WinOLS also has
  templates (mostly Bosch Motronic) that auto-identify parameters and manage
  checksums — the common tuning-oriented ROM tool.
- **Comparing bins.** Get multiple bins that run on the same ECU and hex-compare.
  Code usually stays fixed while parameters change: if **< ~5%** of bytes differ,
  the diffs are almost certainly parameters. Knowing *what* changed functionally
  between two bins + *which* bytes moved localizes those parameters in ROM. (Ex:
  a '96 V8 Mustang vs '97 V6 Thunderbird bin — 6,667/114,688 bytes ≈ 5.8% differ,
  same code, different calibration.)

## Disassembly starting points

Disassemblers: architecture-specific (Dis51, Dis66k, DASMx) or multi-target
(objdump/GNU binutils — rebuild with all architectures enabled; IDA Pro/Hopper for
the widest embedded coverage). Breadcrumbs to orient in the code:
- **OEM patents** — often a guided tour of the very algorithm you're staring at,
  and they lead production by 1–2 years.
- **Aftermarket tuning software** for that ECU — the tables it exposes tell you the
  structure and purpose of code segments.
- **Wiring diagram → MCU pins → interrupt/service routines** — trace a vehicle
  function (fuel, spark, crank/cam signal, idle) to the hardware pin, then to the
  code that services that pin via the interrupt vector table.

Divide and conquer — chase small routines, not the whole megabyte at once.
