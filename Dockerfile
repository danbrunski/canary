# CANary dev container (Task 0.8) — non-Pi development / simulation.
#
# Runs the full daemon (collection → Parquet → dashboard + WebSockets +
# control plane) against a python-can VirtualBus with OBD disabled, so the
# dashboard comes up on http://localhost:8000/ with no Canable and no
# vehicle. Not the production image — the Pi uses scripts/install_pi.sh +
# the systemd unit (CAP_NET_RAW for real SocketCAN).
#
#   docker compose up            # build + run the sim daemon
#   docker compose run --rm canary pytest -q   # run the suite in-container
#
# The 'ml' extra (Torch) is intentionally omitted: the sim config disables
# both models, so inference isn't exercised and the image stays lean. Train
# + mount artifacts under ./models and flip the models on in a copied config
# to exercise it.

FROM python:3.12-slim

# python-can / pyarrow / scikit-learn wheels are manylinux; only need a
# compiler if a sdist slips through. Keep the layer small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first against just the project metadata so the dependency
# layer caches across source edits.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip && pip install ".[dev]"

# The rest of the repo (scripts, configs, tests) is bind-mounted by
# docker-compose in dev; copy it too so the image is runnable standalone.
COPY . .

EXPOSE 8000

# Simulation by default; override the command to run tests / other configs.
CMD ["python", "-m", "src.main", "--config", "canary.sim.example.yaml"]
