# /loop prompt for the pull drive

The standing protocol lives in `.claude/commands/drive.md` (`/drive`). Fill in
DRIVE FACTS and paste the fenced block. Keep it this short: anything durable
belongs in `drive.md`, anything about this experiment belongs in
`data/analyses/2026-09-18-next-drive-deploy-plan.md`.

```
/loop /drive
OUTCOME: close the fuel workflow with one capture on fuel-pw-research —
0x1134 vs PID 0x44 (D03.8), 0x1206/0x1207 and 0x1208 on rich rows (D03.9),
the commanded-phi -> lambda transport delay (D04.3), coasts for D04.2.
Pass/fail criteria, sample target and drive order are in
data/analyses/2026-09-18-next-drive-deploy-plan.md; don't move them.
DRIVE FACTS: pulls possible: [yes/no] · driving time: [min] ·
elevation change >= 800 m: [yes/no] · UpRev along: [yes/no] ·
engine at start: [off/idling/driving]
THIS DRIVE ONLY: if D03.18 isn't fixed, ask for >= 10 s holds rather than
short pulls, and count stalls that land inside a hold.
```
