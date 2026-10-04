/* Host harness for filter.h (tests/test_usb_adc_firmware_filter.py): reads
 * 12-bit conversions as little-endian uint16 on stdin, writes each output
 * sample as a decimal line. Not part of the firmware build. */
#include <stdio.h>

#include "filter.h"

int main(void) {
    static filter_t filter;
    uint16_t conversion;
    while (fread(&conversion, sizeof conversion, 1, stdin) == 1) {
        uint32_t out;
        if (filter_push(&filter, conversion & 0xFFFu, &out)) {
            printf("%u\n", (unsigned)out);
        }
    }
    return 0;
}
