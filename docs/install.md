# Installation

Use `scripts/install_deps.py` for local development setup. It creates or updates
`.venv`, installs CANary's project dependencies, and installs PyTorch from a
wheel index selected for the machine's NVIDIA driver.

```bash
python3 scripts/install_deps.py
```

## What It Installs

The default command installs:

- editable CANary package
- development tools from `.[dev]`
- PyTorch from a selected PyTorch wheel index
- a final `pip check`

The script intentionally does not install the `ml` extra through the editable
project install. That extra currently contains Torch, and optional-extra
metadata cannot choose a CUDA wheel index. The script installs Torch directly
after detecting the local driver so pip does not pick an incompatible default
wheel.

## Torch Selection

With `--torch auto` (the default), the script runs `nvidia-smi`, parses the
reported CUDA capability, and selects the newest supported Torch wheel that is
not newer than the driver can run:

| Detected driver CUDA capability | Selected Torch index |
| --- | --- |
| 13.0 or newer | `cu130` |
| 12.8 or newer | `cu128` |
| 12.6 or newer | `cu126` |
| 11.8 or newer | `cu118` |
| unavailable / unsupported | `cpu` |

For example, a driver reporting `CUDA Version: 12.7` selects `cu126`.

## Overrides

Inspect the plan without installing:

```bash
python3 scripts/install_deps.py --dry-run
```

Force a specific Torch wheel source:

```bash
python3 scripts/install_deps.py --torch cu126
python3 scripts/install_deps.py --torch cpu
```

Skip Torch entirely:

```bash
python3 scripts/install_deps.py --torch none
```

Install only base project dependencies, without `dev`:

```bash
python3 scripts/install_deps.py --extras none
```

Pin a Torch version while still using the selected wheel index:

```bash
python3 scripts/install_deps.py --torch-package 'torch==2.11.0'
```

## Raspberry Pi Runtime Install

For the Pi service install, prefer the wrapper:

```bash
sudo scripts/install_pi.sh
```

That calls `scripts/install_deps.py` with runtime-only defaults:
`--extras none --torch none`. Use CPU Torch only when you are ready to run
LSTM inference on the Pi:

```bash
sudo CANARY_TORCH=cpu scripts/install_pi.sh
```

## Verifying CUDA

From a normal shell with GPU access:

```bash
.venv/bin/python -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```

If this returns `False` inside a sandbox or container but `nvidia-smi` works in a
normal terminal, the issue is device visibility in that environment. Run the
installer from a shell with GPU access, or pass an explicit override such as
`--torch cu126`.
