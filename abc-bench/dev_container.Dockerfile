# abc_bench development container
# ===============================
#
# A batteries-included environment for *developing* abc_bench, as opposed to the
# layered `base -> harness -> task group` containers that abc_bench builds in
# order to *run* benchmarks. It contains everything needed to build those
# runtime containers and execute a benchmark batch:
#
#   * Charliecloud (ch-image / ch-run / ch-convert), built from source with
#     squashfuse, cJSON (CDI) and Boehm GC support
#   * squashfuse + squashfs-tools, for `.sqfs` task groups
#   * Open MPI + mpi4py, for `run-tasks --mpi`
#   * a Python venv with abc_bench's dev/docs/mpi dependencies preinstalled
#   * OpenCode, so an agent can drive development from inside the container
#
# The repo itself is NOT baked in: bind-mount your checkout and run
# `abc-bench-dev-install` to install it (plus sibling automaton) editable.
# Third-party dependencies are pre-baked, so that step is fast.
#
# This image primarily targets Docker (for less-supervised agent runs), but it
# is deliberately written in the Dockerfile subset Charliecloud's `ch-image`
# also accepts, so it can be exercised on an HPC system with no Docker daemon.
# Concretely that means: no heredocs, and no `#` comments inside line
# continuations, since ch-image's parser rejects both. `ch-image` also ignores
# `USER`, so a Charliecloud build of this image runs as root; that is a
# Charliecloud limitation, not a defect in the image.
#
# Build and run (Docker)
# ----------------------
#   docker build -f dev_container.Dockerfile -t abc-bench-dev .
#
#   docker run --rm -it \
#       --security-opt seccomp=unconfined \
#       --security-opt apparmor=unconfined \
#       --device /dev/fuse --cap-add SYS_ADMIN \
#       -v "$PWD/..:/workspace" \
#       abc-bench-dev
#
# Standard system CAs are used by default. On private networks, supply your own
# trusted CA bundle using SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE,
# and NODE_EXTRA_CA_CERTS; no organization-specific certificates are bundled.

FROM ubuntu:24.04

# Charliecloud release to build. Upstream moved from GitHub to GitLab; releases
# past v0.38 exist only on GitLab.
ARG CHARLIECLOUD_VERSION=0.45.1

# Unprivileged development user. Charliecloud is unprivileged by design and
# several of its operations misbehave (or flatten file ownership) under root, so
# this image does not default to root.
ARG DEV_USER=dev
ARG DEV_UID=1000
ARG DEV_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

# TLS trust, mirrored from containers/base.Dockerfile.jinja2 so that everything
# consulting these variables (requests, curl, node) agrees on the CA bundle.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
# OpenCode ships as a Bun binary, and Bun honors NODE_EXTRA_CA_CERTS. Set it so
# an in-container agent reaching model endpoints trusts the same bundle
# (including any extra anchors added below) rather than only Bun's built-ins.
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# System packages. Grouped by purpose (comments kept outside the continuation,
# which ch-image's parser requires):
#   * autoconf/automake/libtool/pkg-config/build-essential - build Charliecloud
#     (and task code) from source
#   * libsquashfuse-dev + libfuse3-dev - ch-run internal SquashFS mounts
#   * libcjson-dev - Charliecloud --with-json (CDI support)
#   * libgc-dev - Charliecloud --with-gc
#   * squashfuse/squashfs-tools/fuse3 - abc_bench/squashfs.py shells out to
#     `squashfuse`; `mksquashfs` builds .sqfs task groups
#   * openmpi-bin/libopenmpi-dev - `run-tasks --mpi` (automaton MPIAgentPool)
#   * python3-lark/python3-requests - ch-image runs on the *system* interpreter
#     and needs these importable there
RUN apt-get update && apt-get install -y --no-install-recommends \
      autoconf \
      automake \
      build-essential \
      libtool \
      pkg-config \
      libcjson-dev \
      libfuse3-dev \
      libgc-dev \
      libsquashfuse-dev \
      fuse3 \
      squashfs-tools \
      squashfuse \
      libopenmpi-dev \
      openmpi-bin \
      python3 \
      python3-dev \
      python3-lark \
      python3-pip \
      python3-requests \
      python3-venv \
      python3-wheel \
      bash \
      bzip2 \
      ca-certificates \
      curl \
      git \
      jq \
      less \
      libstdc++6 \
      openssh-client \
      procps \
      ripgrep \
      rsync \
      sudo \
      unzip \
      vim \
      wget \
    && rm -rf /var/lib/apt/lists/*

# No private CA files are required in the build context.
# Refresh system trust, then normalize the paths
# various stacks look for (same treatment as the benchmark base image).
RUN set -eux; \
    update-ca-certificates; \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt; \
    mkdir -p /etc/pki/tls/certs; \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt

# Charliecloud from source, rather than apt: Ubuntu 24.04 ships 0.37, which
# predates behavior abc_bench/automaton depend on (notably `ch-image build
# --force` method selection). `--with-python=/usr/bin/python3` pins the shebang
# of ch-image and friends to the system interpreter, so activating the dev venv
# later cannot hide Charliecloud's own lark/requests dependencies. Note the
# option is `--disable-test`, singular.
RUN set -eux; \
    mkdir -p /tmp/cc-build; \
    curl -fsSL \
      "https://gitlab.com/charliecloud/charliecloud/-/archive/v${CHARLIECLOUD_VERSION}/charliecloud-v${CHARLIECLOUD_VERSION}.tar.gz" \
      -o /tmp/cc.tar.gz; \
    tar -xzf /tmp/cc.tar.gz -C /tmp/cc-build --strip-components=1; \
    cd /tmp/cc-build; \
    ./autogen.sh; \
    ./configure \
        --prefix=/usr/local \
        --with-python=/usr/bin/python3 \
        --with-squashfuse \
        --with-json \
        --with-gc \
        --disable-html \
        --disable-man \
        --disable-test; \
    make -j "$(nproc)"; \
    make install; \
    cd /; \
    rm -rf /tmp/cc-build /tmp/cc.tar.gz
RUN ch-image --version && ch-run --version && ch-convert --version

# Charliecloud image storage and abc_bench's container cache both live under
# /var/tmp; ensure it is world-writable so the unprivileged dev user can build.
RUN chmod 1777 /var/tmp

# Let the dev user mount FUSE filesystems (squashfuse) and let ch-run's nested
# mounts see them.
RUN sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf || true

# ubuntu:24.04 ships no /etc/mtab, but `fusermount3` insists on it and otherwise
# fails with "failed to open /etc/mtab" -> "mount failed: Operation not
# permitted", which breaks `.sqfs` task groups. The distro-standard symlink is
# the fix.
RUN [ -e /etc/mtab ] || ln -s ../proc/self/mounts /etc/mtab

# Dev user. ubuntu:24.04 ships an `ubuntu` account at uid/gid 1000, so drop it
# to free that id (which is also the uid automaton's ch-run maps to).
RUN set -eux; \
    if getent passwd "${DEV_UID}" >/dev/null; then \
        userdel -r "$(getent passwd "${DEV_UID}" | cut -d: -f1)" 2>/dev/null || true; \
    fi; \
    if getent group "${DEV_GID}" >/dev/null; then \
        groupdel "$(getent group "${DEV_GID}" | cut -d: -f1)" 2>/dev/null || true; \
    fi; \
    groupadd --gid "${DEV_GID}" "${DEV_USER}"; \
    useradd --uid "${DEV_UID}" --gid "${DEV_GID}" --create-home --shell /bin/bash "${DEV_USER}"; \
    echo "${DEV_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-${DEV_USER}"; \
    chmod 0440 "/etc/sudoers.d/90-${DEV_USER}"

ENV VIRTUAL_ENV=/opt/abc-bench-venv
ENV PATH="${VIRTUAL_ENV}/bin:/usr/local/bin:${PATH}"
ENV ABC_BENCH_REPO=/workspace/abc-bench

# Docker honors the ENV above, but `ch-run` does not: it passes the *host*
# environment straight through, so the venv would be invisible under
# Charliecloud. A profile.d snippet fixes that for any login shell, for any
# user, independent of $HOME.
RUN printf '%s\n' \
      'export VIRTUAL_ENV=/opt/abc-bench-venv' \
      'case ":$PATH:" in' \
      '  *":/opt/abc-bench-venv/bin:"*) ;;' \
      '  *) PATH="/opt/abc-bench-venv/bin:/usr/local/bin:$PATH" ;;' \
      'esac' \
      'export PATH' \
      'export ABC_BENCH_REPO="${ABC_BENCH_REPO:-/workspace/abc-bench}"' \
    > /etc/profile.d/abc-bench-dev.sh \
    && chmod 0644 /etc/profile.d/abc-bench-dev.sh

# Python dev environment in a venv rather than the system interpreter, so pip
# installs cannot break the distro Python that ch-image depends on. abc_bench
# and automaton are intentionally absent: the checkout is bind-mounted and
# installed editable by `abc-bench-dev-install`. Only third-party dependencies
# are baked in, which keeps that step quick.
RUN set -eux; \
    python3 -m venv "${VIRTUAL_ENV}"; \
    "${VIRTUAL_ENV}/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel; \
    "${VIRTUAL_ENV}/bin/pip" install --no-cache-dir \
        "pyyaml>=6.0" \
        "requests>=2.0" \
        "tqdm>=4.0" \
        "pytest>=7.0" \
        "black>=22.0" \
        "mypy>=0.990" \
        "ruff" \
        "mkdocs>=1.5" \
        "mkdocs-material>=9.0" \
        "mkdocstrings[python]>=0.25" \
        "mkdocstrings-python>=1.0"; \
    chmod -R a+rX "${VIRTUAL_ENV}"

# mpi4py compiles against the Open MPI installed above, so it gets its own step;
# it is the one dependency that needs a working MPI toolchain.
RUN set -eux; \
    "${VIRTUAL_ENV}/bin/pip" install --no-cache-dir "mpi4py>=3.1"; \
    "${VIRTUAL_ENV}/bin/python" -c "from mpi4py import MPI; print('mpi4py OK', MPI.Get_version())"

# Keep a containerized `mpiexec -n 2` usable out of the box: no ssh launcher, no
# shared-memory single-copy (which needs CAP_SYS_PTRACE), and allow
# oversubscription since a container often sees fewer cores than ranks.
ENV OMPI_MCA_plm_rsh_agent=false
ENV OMPI_MCA_btl_vader_single_copy_mechanism=none
ENV OMPI_MCA_rmaps_base_oversubscribe=1

# OpenCode, so an agent can be pointed at this container and develop abc_bench
# from inside it.
#
# STOP-GAP: opencode.ai/install currently redirects to an internal personal
# machine, so we pull the installer from the anomalyco fork instead. Revert to
# https://opencode.ai/install once the upstream redirect is fixed. Keep this in
# sync with containers/harnesses/*.Dockerfile.jinja2.
RUN set -eux; \
    curl -fsSL https://raw.githubusercontent.com/anomalyco/opencode/refs/heads/dev/install \
        | HOME=/opt/opencode bash -s -- --no-modify-path; \
    ln -s /opt/opencode/.opencode/bin/opencode /usr/local/bin/opencode; \
    chmod -R a+rX /opt/opencode; \
    opencode --version

# `abc-bench-dev-install`: editable install of the bind-mounted checkout.
RUN set -eux; \
    printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -euo pipefail' \
      'PIP=/opt/abc-bench-venv/bin/pip' \
      'REPO="${1:-${ABC_BENCH_REPO:-/workspace/abc-bench}}"' \
      'if [[ ! -f "$REPO/pyproject.toml" ]]; then' \
      '    echo "error: no abc_bench checkout at $REPO" >&2' \
      '    echo "hint: bind-mount your checkout at /workspace/abc-bench" >&2' \
      '    exit 1' \
      'fi' \
      'if [[ ! -w /opt/abc-bench-venv ]]; then' \
      '    echo "error: /opt/abc-bench-venv is not writable by $(id -un)" >&2' \
      '    exit 1' \
      'fi' \
      'if [[ ! -f "$REPO/../automaton/pyproject.toml" ]]; then' \
      '    echo "error: mount sibling automaton at $REPO/../automaton" >&2' \
      '    exit 1' \
      'fi' \
      'echo "==> installing automaton (editable)"' \
      '"$PIP" install -e "$REPO/../automaton"' \
      'echo "==> installing abc_bench (editable, dev+docs+mpi extras)"' \
      '"$PIP" install -e "$REPO[dev,docs,mpi]"' \
      '/opt/abc-bench-venv/bin/abc-bench --help >/dev/null && echo "==> abc_bench CLI OK"' \
      > /usr/local/bin/abc-bench-dev-install; \
    chmod 0755 /usr/local/bin/abc-bench-dev-install

# `abc-bench-dev-doctor`: report whether this container can actually do what
# abc_bench needs. This is the fastest way to distinguish "the image is broken"
# from "docker run was missing --security-opt seccomp=unconfined".
RUN set -eux; \
    printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -uo pipefail' \
      'VENV_PY=/opt/abc-bench-venv/bin/python' \
      'status=0' \
      'check() {' \
      '    local label="$1"; shift' \
      '    if "$@" >/tmp/.abc-bench-doctor.log 2>&1; then' \
      '        printf "ok    %s\n" "$label"' \
      '    else' \
      '        printf "FAIL  %s\n" "$label"' \
      '        sed "s/^/        /" /tmp/.abc-bench-doctor.log | head -5' \
      '        status=1' \
      '    fi' \
      '}' \
      'fuse_probe() {' \
      '    local d' \
      '    d=$(mktemp -d) || return 1' \
      '    mkdir -p "$d/src" "$d/mnt" || return 1' \
      '    echo probe > "$d/src/probe.txt" || return 1' \
      '    mksquashfs "$d/src" "$d/probe.sqfs" -no-progress >/dev/null 2>&1 || return 1' \
      '    squashfuse "$d/probe.sqfs" "$d/mnt" >/dev/null 2>&1 || return 1' \
      '    fusermount3 -u "$d/mnt" >/dev/null 2>&1 || true' \
      '    return 0' \
      '}' \
      'echo "-- tools --"' \
      'for t in ch-image ch-run ch-convert squashfuse mksquashfs mpiexec opencode git rsync; do' \
      '    check "found $t" command -v "$t"' \
      'done' \
      'echo "-- python --"' \
      'check "venv python exists" test -x "$VENV_PY"' \
      'check "venv python is on PATH" bash -c "[[ \"$(command -v python)\" == \"$VENV_PY\" ]]"' \
      'check "mpi4py imports" "$VENV_PY" -c "from mpi4py import MPI"' \
      'check "pytest available" "$VENV_PY" -m pytest --version' \
      'check "ch-image deps on system python3" /usr/bin/python3 -c "import lark, requests"' \
      'echo "-- charliecloud / mpi --"' \
      'check "ch-image --version" ch-image --version' \
      'check "mpiexec -n 2" mpiexec -n 2 "$VENV_PY" -c "from mpi4py import MPI; MPI.COMM_WORLD.barrier()"' \
      'echo "-- sandbox capabilities --"' \
      'check "nested user namespaces" unshare --user --map-root-user true' \
      'if [[ ! -e /dev/fuse ]]; then' \
      '    printf "WARN  /dev/fuse missing; .sqfs task groups will not mount\n"' \
      '    printf "        under Docker add: --device /dev/fuse --cap-add SYS_ADMIN\n"' \
      'elif fuse_probe; then' \
      '    printf "ok    squashfuse can mount a .sqfs\n"' \
      'else' \
      '    printf "WARN  squashfuse cannot mount; .sqfs task groups unavailable\n"' \
      '    printf "        under Docker: --device /dev/fuse --cap-add SYS_ADMIN\n"' \
      '    printf "        under ch-run this is expected (setuid fusermount3 is inert)\n"' \
      'fi' \
      'if [[ "$status" -ne 0 ]]; then' \
      '    printf "\nUnder Docker, Charliecloud needs:\n"' \
      '    printf "  --security-opt seccomp=unconfined --security-opt apparmor=unconfined\n"' \
      '    printf "  --device /dev/fuse --cap-add SYS_ADMIN\n"' \
      'fi' \
      'exit "$status"' \
      > /usr/local/bin/abc-bench-dev-doctor; \
    chmod 0755 /usr/local/bin/abc-bench-dev-doctor

RUN mkdir -p /workspace/abc-bench && chown -R "${DEV_UID}:${DEV_GID}" /workspace

# The dev user must be able to `pip install -e` into the venv, so give it
# ownership. Under Charliecloud this is moot (USER is ignored, everything runs as
# root), but under Docker it is what makes `abc-bench-dev-install` work.
RUN chown -R "${DEV_UID}:${DEV_GID}" "${VIRTUAL_ENV}"

USER ${DEV_USER}
ENV HOME=/home/${DEV_USER}
WORKDIR /workspace/abc-bench

# profile.d covers login shells; .bashrc covers non-login interactive ones such
# as `docker exec -it <ctr> bash`.
RUN printf '%s\n' \
      '. /etc/profile.d/abc-bench-dev.sh' \
    >> "${HOME}/.bashrc"

CMD ["/bin/bash", "-l"]
