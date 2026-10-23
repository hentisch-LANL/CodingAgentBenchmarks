# Development Container

`dev_container.Dockerfile` builds a batteries-included environment for
*developing* ABC Bench. It is distinct from the layered
`base -> harness -> task group` images described in
[Container System](container-system.md): those exist to *run* benchmarks, while
this one exists to build and drive them.

The image contains:

- Charliecloud (`ch-image`, `ch-run`, `ch-convert`), built from source with
  squashfuse, cJSON (CDI) and Boehm GC support
- `squashfuse` + `squashfs-tools`, for `.sqfs` task groups
- Open MPI + `mpi4py`, for `run-tasks --mpi`
- a Python venv at `/opt/abc-bench-venv` with the dev/docs/mpi dependencies
- OpenCode, so an agent can drive development from inside the container

The repository itself is deliberately **not** baked in. Bind-mount a checkout and
install it with `abc-bench-dev-install`. Third-party dependencies are pre-baked,
so that step stays fast.

## Building

```bash
docker build -f dev_container.Dockerfile -t abc-bench-dev .
```

Standard system CAs are used by default; see [TLS trust](#tls-trust) for private networks.

## Running

```bash
docker run --rm -it \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    --device /dev/fuse --cap-add SYS_ADMIN \
    -v "$PWD/..:/workspace" \
    abc-bench-dev
```

Those flags are not optional decoration. Charliecloud needs unprivileged
*nested* user namespaces and squashfuse needs `/dev/fuse`, so the stock Docker
sandbox is too strict:

- `seccomp=unconfined` permits `unshare(CLONE_NEWUSER)`; without it `ch-run`
  cannot create its user namespace
- `--device /dev/fuse --cap-add SYS_ADMIN` are what let `squashfuse` mount a
  `.sqfs`

If the *host* forbids unprivileged user namespaces outright
(`/proc/sys/user/max_user_namespaces` is 0, or Ubuntu's
`kernel.apparmor_restrict_unprivileged_userns` is 1), that must be fixed on the
host. No in-container setting can grant the capability.

## Verifying

Two helper scripts are baked into the image:

```bash
abc-bench-dev-doctor      # checks tools, venv, mpi4py, and userns/FUSE access
abc-bench-dev-install     # editable install of the bind-mounted checkout
```

`abc-bench-dev-doctor` is the fastest way to tell "the image is broken" apart
from "`docker run` was missing a flag". With the flags above it reports `ok` for
every check, including `squashfuse can mount a .sqfs`.

It intentionally exits 0 on FUSE *warnings*, so read its output rather than
trusting only the exit code.

`abc-bench-dev-install` requires the sibling `automaton` component. Mount the
monorepo parent at `/workspace` so `/workspace/abc-bench` and
`/workspace/automaton` are both available. The helper installs both editable
and fails clearly if the sibling is missing; it never fetches a submodule.

## TLS trust

No private certificates are included or required. Standard system CAs are used
by default. For a private endpoint, configure the provider's `cert` field with
the path to your CA bundle, or set `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and
`SSL_CERT_FILE`. For Node-based clients also set `NODE_EXTRA_CA_CERTS`.
Mount the bundle into the container and use its container-visible path. If
image-build downloads require a private CA, configure your build environment's
trust store before running the build; do not disable TLS verification.

## Charliecloud compatibility

Although this image primarily targets Docker, it is written in the Dockerfile
subset `ch-image` also accepts, so it can be exercised on an HPC system with no
Docker daemon:

```bash
ch-image build --force -f dev_container.Dockerfile -t abc-bench-dev .
ch-convert abc-bench-dev /var/tmp/abc-bench-dev
ch-run --write-fake -b "$PWD/..:/workspace" /var/tmp/abc-bench-dev -- bash -l
```

Concretely, that constraint means **no heredocs** and **no `#` comments inside
line continuations**; `ch-image`'s parser rejects both fatally. It is why the two
helper scripts are generated with `printf '%s\n'` argument lists rather than the
heredocs that would read better. Check changes with:

```bash
ch-image build --parse-only -f dev_container.Dockerfile -t parsecheck .
```

Three caveats apply only to the Charliecloud path, and none indicates a problem
with the image:

- `ch-image` ignores `USER`, so the container runs as root rather than `dev`.
- setuid binaries are inert inside `ch-run`'s user namespace, so `fusermount3`
  cannot mount and the `.sqfs` tests in `tests/test_install_archives.py` fail.
  Under Docker, with `--device /dev/fuse --cap-add SYS_ADMIN`, they pass.
- `--write-fake` discards writes at exit, so `abc-bench-dev-install` does not
  persist between `ch-run` invocations. Chain it with whatever you want to run in
  a single `bash -lc`.

Because `ch-run` passes the *host* environment through and ignores image `ENV`,
the venv would otherwise be invisible under Charliecloud. `/etc/profile.d/
abc-bench-dev.sh` fixes that for any login shell; it is redundant under Docker
but harmless.

## Nested container builds

The container can build and run Charliecloud images *inside itself*, which is
what makes it usable for real work:

```bash
abc-bench build-container base
```

This requires the `seccomp`/`apparmor` flags above. Charliecloud pulls from
Docker Hub, so heavy use can hit anonymous pull rate limits.

## Notes

- Ubuntu 24.04 ships an `ubuntu` account at uid/gid 1000; the image removes it so
  the `dev` user can take that id, which is also the uid `ch-run` maps to.
- Ubuntu 24.04 ships no `/etc/mtab`, but `fusermount3` requires it. The image
  adds the distro-standard symlink to `/proc/self/mounts`.
- Charliecloud is built from source rather than apt because Ubuntu 24.04 ships
  0.37, which predates behavior abc_bench and automaton depend on. Upstream moved
  from GitHub to GitLab; releases past v0.38 exist only on GitLab.
- The configure flag is `--disable-test`, singular. `--disable-tests` aborts
  configure, and upstream's `doc/install.rst` gets this wrong.

## See Also

- [Container System](container-system.md)
- [Architecture](architecture.md)
- [Managing Containers](../user-guide/managing-containers.md)
