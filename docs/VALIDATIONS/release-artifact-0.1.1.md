# VALIDATIONS — release artifact 0.1.1 (LEG-102, in-repo harness)

Executable proof that the built artifact installs and runs as a consumer would
see it: built via `uv build`, installed into a throwaway virtualenv (deps from
the index), headless domain-free smoke against the **installed** package over
the `examples/` single source. The gate: `make validate-release`.

- Pinned version: `0.1.1`
- Artifact: `legio-0.1.1-py3-none-any.whl`
- Smoke: validate-release: import ok __version__=0.1.1;validate-release: round-trip ok task=validate-release@example:7f299c26-c3cd-47bc-b7bf-540a73340ac4 output={'transform': {'transformed': 'HELLOHELLO'}};validate-release: artifact smoke passed
- Run at: 2026-09-26T22:57:55Z

The real external-consumer repository (an own repo pinning the release) is the
maintainer's follow-up; this record is the in-repo proof of the wheel.
