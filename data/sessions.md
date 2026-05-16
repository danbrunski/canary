# Captured sessions — index

Running index of recording sessions (task 1.16). Raw captures live under
`data/sessions/<timestamp>/` (gitignored); per-session findings are in
`data/analyses/<timestamp>.md`. See `docs/data-collection-plan.md` for what's
still needed before the ML / behavior-model work.

OBD column: `Y` = recorded with `--obd-port` (has `pids.jsonl`); `—` = CAN-only.

| Session (UTC) | Engine state | OBD | Notes | Analysis |
|---|---|---|---|---|
| 2026-05-11T033338 | parked, key on | — | early dev capture | — |
| 2026-05-11T034516 | parked, NT510 screen-tagged | — | early NT510 session; tags were screen-selections (later found unreliable) | — |
| 2026-05-11T152820 | parked | — | early dev capture | — |
| 2026-05-11T152858 | rev protocol (cold→warm idle, blips) | — | first physical-action rev session; source of most ECM-byte candidates | [analysis](analyses/2026-05-11T152858.md) |
| 2026-05-11T154648 | partial drive | — | slcan parse crash mid-capture; motion-correlated candidates by inventory comparison | [analysis](analyses/2026-05-11T154648.md) |
| 2026-05-11T211230 | — | — | short capture preceding T211240 | — |
| 2026-05-11T211240 | tagged drive (reverse, A/C, clutch, coast-to-stop) | — | crashed but yielded gear/motion/AC candidates | [analysis](analyses/2026-05-11T211240.md) |
| 2026-05-11T213407 | short clean restart | — | reliability check after the slcan-tolerance fix | [analysis](analyses/2026-05-11T213407.md) |
| 2026-05-11T222447 | full drive (cold start → city → stop) | — | best/longest capture (~18 min); confirmed many engine-state findings | [analysis](analyses/2026-05-11T222447.md) |
| 2026-05-13T004818 | key on, engine off | Y | 3-device rig validation + KOEO broadcast baseline | [analysis](analyses/2026-05-13T004818.md) |
| 2026-05-13T005522 | rev protocol, engine running | Y | rev protocol with parallel ELM327 — pinned ECM_RPM (0x1F9:2-3) and ECM_COOLANT_TEMP against PIDs | [analysis](analyses/2026-05-13T005522.md) |
| 2026-05-13T011123 | NT510 active session | — | NT510 module scan + ABS live data (steering/brakes) + cooling-fan test; steering = 0x002, brake = 0x29E candidates | [analysis](analyses/2026-05-13T011123.md) |
| 2026-05-16T202800 | KOEO→crank→idle→blips→engine-off→restart→accessory sweep (rev + binding tags) | — | **reconnect-fix (7ff2a79) stability validation: 13.6 min, 1.13M frames, 0 parse / 0 reconnect, clean signal stop.** Canable-only (ELM327 dead). A/C on↔off + blower + headlights + brake + clutch + window binding tags. ⚠ no engine-restart marker before the accessory block — see analysis. | [analysis](analyses/2026-05-16T202800.md) |

## Still needed (see docs/data-collection-plan.md)

Cold-start · warm-idle baseline · city drive · highway drive · load-sweep
(grid-coverage) drive · off-road/4-Lo · A/C on↔off · Mode 22 DID exploration —
all with the expanded `--obd-pids` set and `guided_session.py --protocol drive`
tags.
