"""Channel physics and the per-vehicle signal definitions (layer 0, R09.4).

The formulas that turn telemetry channels into other channels -- derived
physics (:mod:`src.signals.derived`), measured and target AFR
(:mod:`src.signals.afr`), redundant-sensor committees
(:mod:`src.signals.committees`), curated physical limits
(:mod:`src.signals.fsm`) and same-instant pairing (:mod:`src.signals.pairing`)
-- and which channels each vehicle feeds them
(:mod:`src.signals.vehicle_signals`). Preprocessing, collection, the models
and the analysis workbench all use them, so they sit beneath every one of
those packages and import only other foundation units.

This file re-exports nothing: import from the module that defines a name.
"""
