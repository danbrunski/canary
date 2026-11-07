# shellcheck shell=bash
# The browser dashboard's bundle, src/dashboard/static/dist/ (gitignored): when
# it needs building, and the build itself.
#
# Sourced by scripts/install_pi.sh and scripts/update_canary.sh, so a fresh
# install and an update agree on whether the dashboard is there. Until
# 2026-11-06 only canary-update built it, so a Pi set up with install_pi.sh
# served no dashboard until its first `canary-update`. The daemon mounts dist/
# at startup only if it exists (src/dashboard/api.py).

# dashboard_stale_reason CHECKOUT -> prints why the bundle must be (re)built,
# or nothing when dist/ is current.
dashboard_stale_reason() {
  local home="$1"
  local dir="${home}/src/dashboard/static"
  local built="${dir}/dist/index.html"
  local newer
  if [[ ! -f "${built}" ]]; then
    echo "dist/ missing"
  elif newer="$(find "${dir}" \( -name node_modules -o -name dist \) -prune \
      -o -type f -newer "${built}" -print -quit)" && [[ -n "${newer}" ]]; then
    echo "dist/ older than ${newer#"${home}"/}"
  fi
}

# dashboard_build CHECKOUT USER -> pnpm install + pnpm build as USER, who owns
# the checkout. Fails if either fails, or if the build leaves no
# dist/index.html: what the daemon serves is the only success that counts.
dashboard_build() {
  local home="$1" user="$2"
  local dir="${home}/src/dashboard/static"
  if ! command -v pnpm >/dev/null 2>&1; then
    echo "pnpm is required to build the dashboard." >&2
    echo "sudo scripts/install_pi.sh installs the version package.json pins." >&2
    return 1
  fi
  runuser -u "${user}" -- bash -c \
    'cd "$1" && pnpm install --frozen-lockfile && pnpm build' bash "${dir}" || return $?
  if [[ ! -f "${dir}/dist/index.html" ]]; then
    echo "pnpm build exited 0 but wrote no ${dir#"${home}"/}/dist/index.html." >&2
    return 1
  fi
}
