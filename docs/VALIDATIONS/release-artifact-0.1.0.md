# VALIDATIONS — release artifact 0.1.0 (LEG-102, in-repo harness)

Executable proof that the built artifact installs and runs as a consumer would
see it: built via `uv build`, installed into a throwaway virtualenv (deps from
the index), headless domain-free smoke against the **installed** package over
the `examples/` single source. The gate: `make validate-release`.

- Pinned version: `0.1.0`
- Artifact: `legio-0.1.0-py3-none-any.whl`
- Smoke: validate-release: import ok __version__=0.1.0;validate-release: round-trip ok task=validate-release@example:9e507531-0b97-46ce-95fa-8ebc32307c7e output={'transform': {'transformed': 'HELLOHELLO'}};validate-release: artifact smoke passed
- Run at: 2026-09-21T22:18:47Z

The real external-consumer repository (an own repo pinning the release) is the
maintainer's follow-up; this record is the in-repo proof of the wheel.
