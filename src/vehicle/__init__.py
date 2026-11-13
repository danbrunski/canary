"""The vehicle's own records (layer 1, R09.5).

What CANary knows about the vehicle it is mounted in, as opposed to what it
measured on the bus: the per-VIN profile registry and the scoping that nests
every vehicle-specific path under the profile's slug
(:mod:`src.vehicle.profiles`, a package since R26.4), the ECU's
tune-revision history (:mod:`src.vehicle.tune_revisions`) and the flash history
read from it (:mod:`src.vehicle.flash_history`), and the operator's vehicle ledger of
maintenance, modifications and corpus boundaries
(:mod:`src.vehicle.vehicle_ledger`). The package sits beside ``storage`` and
imports only foundation units and ``storage``. Reading the VIN over the bus
is acquisition, so the probe lives in :mod:`src.collection.vehicle_probe`,
and its callers hand it to profile resolution.

This file re-exports nothing: import from the module that defines a name. (The
``profiles`` package re-exports what its parts define, for the import sites that
predate the split; new code imports from the part.)
"""
