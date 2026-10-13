# H08 — HAT rev 2.1 ADC: ADS8688A driver, 12 V sense, fuel pressure, sensor ports

Part of [H00](H00-hat-integration-README.md). Rev 2.1 replaces rev 2.0's
ADS1015 with an 8-channel ADS8688A on SPI0 CE1. It carries the 12 V sense
(CH0), the 5 V sensor supply (CH3), the AEM fuel gauge (CH4) and the two
generic sensor ports (V1 on CH1, R1 on CH2). Design record:
`docs/CANary-HAT-Rev2.1.md` §13.5.3, §13.5.8, §13.5.11, §13.5.12.

## Where it stands

- **Written and tested on the desk, not run on a board.** No rev 2.1 board
  exists yet. Every desk item below is proven against a fake chip that models
  SBAS680's frame protocol (`tests/ads8688a_fake.py`). The Pi items are the
  first board's bring-up.
- **Rev 2.0 is untouched.** Its 12 V sense stays the ADS1015 collector. The
  ADS8688A collector starts only where the revision table says the board has
  one (`hat_revisions.json`, `supply_adc: ads8688a`).

## Rules

- The channel map, the population and every calibration number are data
  (`data/hat_adc_rev2_1.yaml`), never constants in the collector.
- Ports are named after the terminal (`PORT_V1_*`, `PORT_R1_*`), never after
  what the dev truck plugs into them (§13.5.11). A sensor binding is
  per-vehicle data, and it waits for that sensor's transfer function to be
  pinned.
- The gauge's sensor leg (transfer, window, trim) exists once in effect: the
  HAT map and `data/usb_adc_sensors.yaml` must agree, and a test holds them
  equal.
- The chip shares SPI0 with the vehicle CAN controller: bursts are short, one
  bus message each, and how fast they run is a measurement (H08.9), not a
  guess.

---

## Desk

- [x] **H08.1 SPI transport.** **Do:** a no-dependency `/dev/spidev` transport,
  the SPI twin of `i2c_dev.py`: each frame its own chip-select cycle, all frames
  in one `SPI_IOC_MESSAGE`. **Evidence (2026-10-12):**
  `src/collection/spi_dev.py`. Tests pin the ioctl numbers to `spidev.h` and
  the per-frame `cs_change` (`tests/test_ads8688a.py`).
- [x] **H08.2 ADS8688A driver.** **Do:** reset, per-channel ranges, power down
  unmapped channels, verify by readback; manual-mode bursts with the
  datasheet's one-frame latency; refuse an unmapped channel (open item 33).
  Catch a chip that reset (back to ±10.24 V) in the burst it would spoil: every
  burst ends with a read of one configured register, and a full verify runs
  each second. **Evidence (2026-10-12):** `src/collection/ads8688a.py`, 14
  tests. A mutation check showed the burst tests fail if the data bytes or the
  frame latency are wrong.
- [x] **H08.3 Board map.** **Do:** schema plus data for what each input is
  wired to, with every input accounted for, and the fuel gauge's leg shared
  with the USB path. **Evidence (2026-10-12):** `src/decode/hat_adc_board.py`,
  `data/hat_adc_rev2_1.yaml`. The map models U8's input (1 MΩ, leaking towards
  2.50 V per SBAS680) and each channel's series and match resistors. So the
  12 V rail, the port volts and the R1 solve are node values, with the leakage
  included: ignoring it read a cold NTC's 50 kΩ 2.2 % low. Tests:
  - every input mapped once (CH5-CH7 grounded);
  - every recorded channel name is one role's;
  - the fuel leg equals the USB catalog's;
  - a forward model of the circuit decodes back to the truth.
- [x] **H08.4 Collector.** **Do:** bursts on a thread, averaged into
  wall-clock windows. Records: `SUPPLY_VOLTAGE` (fed to vehicle power),
  `SENSOR_SUPPLY_VOLTAGE`, `FUEL_PRESSURE`, `PORT_V1_VOLTAGE`, `PORT_V1_RATIO`
  and `PORT_R1_RESISTANCE`, with `_FAULT` states for:
  - any input at full scale (the clipping rule, review item 11, for every input);
  - a low or off sensor supply;
  - an open R1;
  - the gauge's window.
  Also:
  - drive `SENS_PWR_EN` (GPIO19) once the chip has configured, low on stop;
  - configure a chip that reset again in place, escalating to the reconnect loop
    only after 3 resets in 10 s;
  - while a capture profile sets `analog_full_rate`, store every burst with its
    slot number (`adc_seq`) and `adc_clock: software`, which the pulsation
    analysis accepts.
  **Evidence (2026-10-12):** `src/collection/hat_adc_collector.py`,
  `hardware.hat_adc` (`HatAdcConfig`: `burst_hz` 500, `record_hz` 100,
  `slow_record_hz` 10, `sensor_power` on, disabled by default), and 36 tests
  (`tests/test_hat_adc.py`). The failure paths are tested: an absent chip never
  powers the sensor rail, and a bus failing mid-run drops the enable, closes
  the bus and leaves no reader thread.
- [x] **H08.5 Wiring by revision.** **Do:** on rev 2.1, `hardware.supply_voltage`
  reads CH0 through this collector. The startup VIN probe's one-shot reading
  goes to the fitted board's ADC. `hardware.hat_adc` on rev 2.0 is refused
  with a reason, and a channel two instruments would both write (the HAT's and
  the USB ADC's `FUEL_PRESSURE`) is refused at start. Rev 2.0's ADS1015
  settings set on a rev 2.1 board are named in a warning as ignored. **The
  reflash battery floor never trusts an uncalibrated rev 2.1 sense.**
  `gain_trim` is required and `null` until measured, and the gate's reader now
  takes its sources as a required argument, from `supply_voltage_sources`.
  That helper withholds the sense, naming why, and falls back to the ECM's own
  voltage. **Evidence (2026-10-12):** `_hat_adc_collectors`,
  `src/collection/supply_sense.py` and `src/control/nonvolatile_write.py`.
  Tests are in `tests/test_hat_adc.py` and `tests/test_supply_voltage.py`.
- [x] **H08.6 Install and bring-up.** **Do:** the service user joins `spi` (and
  `i2c`, `gpio`, which nothing granted before). The bring-up CLI is
  `python -m src.collection.hat_adc_collector`, and `checkout_hat.sh` reads the
  ADC on rev 2.1. **Evidence (2026-10-12):** `scripts/install_pi.sh` (tested in
  `tests/test_install_pi_script.py`) and `checkout_hat.sh` (dry-run with stubbed
  tools: the missing-device and non-spidev paths both fail with one readable
  line).

## Pi / board

- [ ] **H08.7 First boot (Pi, hardware).** `/dev/spidev0.1` exists. On a bare
  board, `checkout_hat.sh` shows `SUPPLY_VOLTAGE` near 0 V and
  `SENSOR_SUPPLY_VOLTAGE` near 5.0 V with GPIO19 high. With nothing on the
  ports, `PORT_V1_VOLTAGE` reads near 0 V, `PORT_R1_RESISTANCE_FAULT` reads
  open, and `FUEL_PRESSURE_FAULT` reads below_window.
- [ ] **H08.8 12 V sense against a meter (Pi, hardware).** Repeat rev 2.0's
  readings (0.000 V unplugged, then 12.68 V and 12.43 V) on this ADC and set
  `gain_trim` (open item 23). Until then the channel carries the nominal
  input-resistance correction. With 1 % divider parts it is good to about
  ±2 %: enough to tell parked from bench. The write gate withholds it (H08.5)
  until `gain_trim` is set.
- [ ] **H08.9 SPI contention (Pi, hardware).** Measure can0 frame loss and
  latency with the ADC idle, then collecting at `burst_hz` 500, under a busy
  bus (§13.5.3). Lower `burst_hz` if CAN suffers; the record rates follow.
- [ ] **H08.10 Reader cost and timing (Pi).** CPU time of the 500 Hz reader
  thread, and the jitter of its burst instants. A full-rate pulsation capture
  places samples by slot number, so its spectrum is only as good as that
  jitter. Record both; the first is a load decision.
- [ ] **H08.11 Fuel pressure on the HAT (truck).** Check `FUEL_PRESSURE` from CH4
  against the gauge face, and against the USB path's history at the same
  operating points. The +1.8 psi question (`usb_adc_sensors.yaml`) is the
  gauge's, so it carries over unchanged.

## Later

- [ ] **H08.12 TMAP decode.** MAP from `PORT_V1_RATIO` (`a`, `b`) and IAT from
  `PORT_R1_RESISTANCE` (a Steinhart–Hart fit) need the sensor's measured
  constants (§13.5.9 item 1), pinned as data, plus a per-vehicle binding of
  port to sensor. Until then the ports record what the board measures.
- [ ] **H08.13 Sensor supply off when parked.** §13.5.12 allows switching
  `SENS_PWR_EN` off while parked. Today it is on whenever the collector runs.
- [ ] **H08.14 Population from the EEPROM.** Read the board's fitted pull
  resistors from a custom HAT+ atom in U1 (§13.5.8) instead of the map's BOM
  values.
