# CANary known OBD-II PIDs and Nissan DIDs

The following Mode 01 PIDs were reported as supported by the 2008 Nissan Xterra ECM in both KOEO and engine-running tests.

Mode 01 PID rows

0x01 - Monitor status, MIL state, and DTC count - count/bitmask
0x03 - Fuel-system status - bitmask
0x04 - Calculated engine load - percent
0x05 - Engine coolant temperature - degC
0x06 - Short-term fuel trim, bank 1 - percent
0x07 - Long-term fuel trim, bank 1 - percent
0x08 - Short-term fuel trim, bank 2 - percent
0x09 - Long-term fuel trim, bank 2 - percent
0x0C - Engine speed - rpm
0x0D - Vehicle speed - km/h
0x0E - Ignition timing advance - degrees BTDC
0x0F - Intake-air temperature - degC
0x10 - Mass airflow - g/s
0x11 - Absolute throttle position - percent
0x13 - Oxygen sensors present, two-bank layout - bitmask
0x15 - Narrowband oxygen sensor B1S2 - V
0x19 - Narrowband oxygen sensor B2S2 - V
0x1C - OBD standard to which the vehicle conforms - enum
0x1F - Engine run time since start - seconds
0x21 - Distance traveled with MIL on - km
0x24 - Wide-range oxygen sensor 1 equivalence ratio - ratio
0x28 - Wide-range oxygen sensor 5 equivalence ratio - ratio
0x2E - Commanded evaporative-purge duty - percent
0x2F - Fuel-tank level - percent
0x30 - Warm-up cycles since DTCs were cleared - count
0x31 - Distance since DTCs were cleared - km
0x32 - Evaporative-system vapor pressure - Pa
0x33 - Barometric pressure - kPa
0x3C - Catalyst temperature B1S1 - degC
0x3D - Catalyst temperature B2S1 - degC
0x41 - Monitor status during the current drive cycle - raw bitmask, not decoded yet
0x42 - ECM/control-module voltage - V
0x43 - Absolute engine load - percent
0x44 - Commanded fuel equivalence ratio - ratio
0x45 - Relative throttle position - percent
0x47 - Absolute throttle position B - percent
0x49 - Accelerator-pedal position D - percent
0x4A - Accelerator-pedal position E - percent
0x4C - Commanded throttle actuator position - percent
0x4D - Engine run time while MIL is on - minutes
0x4E - Time since DTCs were cleared - minutes

Nissan Mode 22 DID rows

0x1101 - Engine coolant temperature - raw minus 50 degC - high confidence
0x1219 - Engine speed - approximately raw rpm - high confidence
0x1208 - Bank 1 injector effective pulse width/quantity - millisecond scale not pinned - high confidence
0x1209 - Mass airflow - raw times 0.01 g/s - high confidence
0x1117 - Calculated engine load - raw times 100/256 percent - high confidence
0x1107 - A/F sensor heater duty - raw times 0.5 percent - high confidence
0x1225 - Bank 1 A/F sensor voltage - raw times 0.005 V - high confidence
0x1226 - Bank 2 A/F sensor voltage - raw times 0.005 V - high confidence
0x120F - Throttle-position sensor 1 voltage - raw times 0.005 V - high confidence
0x1210 - Throttle-position sensor 2 voltage - raw times 0.005 V - high confidence
0x113A - Commanded intake-cam advance - raw times 0.5 minus 64 degCA - high confidence
0x1135 - Measured intake-cam angle, one bank - raw times 0.5 minus 64 degCA - high confidence
0x1163 - Measured intake-cam angle, other bank - raw times 0.5 minus 64 degCA - high confidence
0x1110 - Intake VVT-solenoid duty - percentage scale not pinned - medium confidence
0x1206 - Intake/manifold-pressure candidate - raw 16-bit value, scale unknown - medium confidence
0x1207 - Second intake/manifold-pressure candidate - raw 16-bit value, scale unknown - medium confidence
0x1201 - Coarse duplicate engine speed - raw times 12.5 rpm - high confidence
0x121A - ECM-indicated vehicle speed - raw times 0.1 km/h - high confidence
0x122A - Internal vehicle speed with an approximately 30 km/h floor - raw times 0.05625 km/h - high confidence
0x1139 - Candidate bank-2 intake VVT-solenoid duty - scale and identity unconfirmed - low confidence
0x123F - Static or unrelated register previously called intake-air temperature - binding disproved, do not use

Notes

A Mode 01 PID request is 01 followed by the PID byte.
A Mode 22 DID request is 22 followed by the two-byte DID after entering Nissan diagnostic session 10 C0.
Mode 01 PID 0x0F is the valid intake-air-temperature source; DID 0x123F is not.
The bank assignment of DIDs 0x1135 and 0x1163 has not been pinned.
DIDs 0x1108 through 0x110A and 0x113C have been observed mirroring the 0x1107 heater-duty value but are not separate catalogue entries.

Passive broadcast CAN signal rows

Byte positions are zero-based. A range such as bytes 2-3 includes both endpoints. Multi-byte values are big-endian unless the row says otherwise.

0x551 byte 0 - ECM_COOLANT_TEMP - engine coolant temperature - raw minus 40 degC - confirmed - raw zero is an engine-off sentinel
0x1F9 bytes 2-3 - ECM_RPM - engine speed - raw times 0.125 rpm - confirmed
0x23D byte 1 - ECM_ACCEL_PEDAL_POS_V - accelerator-pedal position sensor voltage - raw times 0.01485 plus 0.795 V - confirmed
0x23D byte 2 - ECM_THROTTLE_SENSOR_V - electronic-throttle position sensor voltage - raw times 0.01341 plus 0.684 V - confirmed
0x233 byte 2 - ECM_AIRFLOW_233_B2 - coarse airflow-related value - scale and units unpinned - medium confidence - only about 11 distinct values across the tested airflow range
0x233 byte 1 - ECM_INTAKE_VACUUM_OR_IAC - likely idle-air-control quantity - scale and units unpinned - high confidence - earlier manifold-pressure interpretation retracted
0x233 byte 4 - ECM_CALC_LOAD_A - unknown engine quantity - scale and units unpinned - low confidence - calculated-load and injector-pulse interpretations rejected
0x23D byte 6 - ECM_THROTTLE_TRANSIENT - signed throttle demand/error term - signed int8, scale unpinned - low confidence - earlier transient-burst interpretation retracted
0x231 byte 0 - ECM_ENGINE_STATE_A - engine-state-related enum or bit field - raw value - high confidence
0x231 byte 3 - ECM_ENGINE_STATE_B - duplicate engine-state-related enum or bit field - raw value - high confidence
0x29E byte 0 - ECM_ENGINE_STATE_C - duplicate engine-state-related enum or bit field - raw value - high confidence
0x2D1 bytes 1-2 little-endian - VEHICLE_SPEED_2D1 - aggregate vehicle speed - raw times 0.1 km/h - high confidence
0x280 bytes 4-5 - cluster-speed candidate - Leaf and 2011 Xterra DBCs agree on the field and 0.01 km/h/count but disagree on endianness; the 2008 byte-5 motion result is consistent but not a scale validation - hypothesized
0x590 byte 2 - MOTION_SIGNAL_590_2 - slow body-bus motion or accumulated-value candidate - scale and identity unknown - hypothesized
0x590 byte 5 - MOTION_SIGNAL_590_5 - second slow body-bus motion or accumulated-value candidate - scale and identity unknown - hypothesized
0x354 bytes 0-1 - ABS/vehicle-speed candidate - Leaf and 2011 Xterra DBCs agree on a big-endian field; the Xterra file proposes 0.0066 mph/count, while the Leaf calls it unscaled pulses - hypothesized
0x354 byte 4 mask 0x40 - ESP/TCS-off candidate independently present in the Leaf and 2011 Xterra DBCs - hypothesized
0x284 bytes 1-2 - WHEEL_SPEED_PAIR_A - invalid legacy extraction - interpretation retracted, do not use as a wheel-speed signal
0x285 bytes 1-2 - WHEEL_SPEED_PAIR_B - invalid legacy extraction - interpretation retracted, do not use as a wheel-speed signal
0x002 byte 0 - STEERING_002_LOW - part of the suspected 16-bit steering-angle field at bytes 0-1 - scale unpinned - low confidence
0x002 byte 1 - STEERING_ANGLE_002 - part of the suspected 16-bit steering-angle field at bytes 0-1 - scale unpinned - low confidence
0x625 byte 1 - AC_COMPRESSOR_STATE - commanded A/C compressor state - zero off and approximately 128 engaged - high confidence
0x625 byte 3 - POWER_STATE_625_3 - ignition or vehicle power-state candidate - raw value - medium confidence
The `0x385` hypothesis below is now independently supported by a working [QM5/Koleos TPMS decoder](https://whoisit.tistory.com/523) and an [AZE0 Leaf CAR-CAN DBC](https://github.com/dalathegreat/leaf_can_bus_messages/blob/main/CAR-can_AZE0.dbc). Both use bytes 2-5 at 0.25 psi/count. Their tentative corner orders disagree, and matching CAN IDs plus plausible Xterra values are not substitutes for a controlled one-tire validation.
0x385 byte 2 - TPMS_FR_PRESSURE - candidate tire pressure - raw times 0.25 psi - hypothesized; QM5/Koleos and Leaf sources support the byte and scale but conflict on the corner, while the observed Xterra mean decodes to 36.9 psi
0x385 byte 3 - TPMS_FL_PRESSURE - candidate tire pressure - raw times 0.25 psi - hypothesized; corner unpinned and observed Xterra mean decodes to 38.8 psi
0x385 byte 4 - TPMS_RR_PRESSURE - candidate tire pressure - raw times 0.25 psi - hypothesized; corner unpinned and observed Xterra mean decodes to 38.4 psi
0x385 byte 5 - TPMS_RL_PRESSURE - candidate tire pressure - raw times 0.25 psi - hypothesized; corner unpinned and observed Xterra mean decodes to 37.5 psi
0x385 byte 6 - unidentified - previously grouped with bytes 2-5 in the retracted 40-bit odometer interpretation; not part of the published QM5/Koleos TPMS layout
0x002 byte 2 - STEERING_RATE_002 - steering-rate candidate associated with bytes 0-1 - sign and scale unpinned - low confidence
0x29E byte 5 - MOTION_29E_5 - engine-load or torque-request-related candidate - scale and identity unpinned - high confidence
0x29E byte 3 - BRAKE_PRESSURE_29E_3 - brake application or pressure magnitude - approximately zero released and 64 firmly applied, pressure scale unpinned - high confidence
0x29E byte 7 - BRAKE_ACTIVITY_29E_7 - brake-dominant driver-demand or pedal-activity composite - raw value - high confidence
0x358 byte 1 - HVAC_BLOWER_LIGHTING_358_1 - bit-packed status - medium confidence for the composite channel; Xterra evidence pins blower active to mask 0x40, while the Leaf DBC proposes headlights at independent mask 0x80 - headlight bit remains hypothesized pending an isolated off/on/off check
0x358 byte 2 - turn-signal candidate - Leaf DBC proposes 0=off, 4=left, 8=right, 12=hazards - hypothesized
0x5C5 bytes 1-3 - 24-bit big-endian odometer candidate - 2011 Xterra DBC names this message MILEAGE and Leaf DBC supplies the field; units/scale unpinned - hypothesized
0x5C5 byte 0 mask 0x04 - parking-brake candidate from Leaf DBC - hypothesized
0x60D bytes 0-2 - BCM body-state candidates from Leaf DBC: doors, locks, exterior lamps, turn-signal command/feedback, and vehicle state - all require isolated Xterra physical-action tags
0x1F9 byte 0 - COOLING_FAN_1F9_0 - cooling-fan and A/C clutch status flags - base 0x20, fan low adds 0x40, fan high adds 0x80, A/C clutch adds 0x08 - high confidence
0x6F7 bytes 0-1 - WHEEL_SPEED_6F7_A - aggregate vehicle-speed variant A, not an individual wheel - raw times 0.0625 km/h - high confidence - reports zero below approximately 4 km/h
0x6F7 bytes 2-3 - WHEEL_SPEED_6F7_B - aggregate vehicle-speed variant B, not an individual wheel - raw times 0.0625 km/h - high confidence
0x6F7 bytes 4-5 - WHEEL_SPEED_6F7_C - aggregate vehicle-speed variant C, not an individual wheel - raw times 0.0625 km/h - high confidence
0x23D bytes 3-4 - ECM_RPM_23D - redundant lower-resolution engine-speed value - little-endian raw times 3.125 rpm - high confidence
0x233 byte 0 - ECM_COOLANT_TEMP_233 - redundant engine coolant temperature - raw minus 50 degC - high confidence
0x23D byte 7 - ECM_COOLANT_TEMP_23D - redundant engine coolant temperature - raw minus 50 degC - high confidence

Passive-signal notes

The three 0x6F7 speed fields are vehicle-speed variants, not individual wheel speeds. No individual passive wheel-speed binding has been identified.
The 0x6F7 speeds agree with Mode 01 PID 0x0D but inherit that PID's approximately 8 percent under-read relative to measured ground speed on this vehicle. Do not use them for absolute distance or true road-speed calculations.
The 0x233 byte 0 and 0x23D byte 7 coolant copies use a minus-50 offset, while 0x551 byte 0 uses a minus-40 offset.
The names WHEEL_SPEED_PAIR_A and WHEEL_SPEED_PAIR_B remain only for compatibility with old model manifests; their wheel-speed interpretation was retracted.
Cross-vehicle evidence review for the body, TPMS, steering, and ABS candidates is in `data/analyses/2026-09-08-leaf-dbc-review.md`. No confidence was promoted from that evidence alone.
