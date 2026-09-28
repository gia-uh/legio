# VALIDATIONS — release artifact 0.1.2 (LEG-102, in-repo harness)

Executable proof that the built artifact installs and runs as a consumer would
see it: built via `uv build`, installed into a throwaway virtualenv (deps from
the index), headless domain-free smoke against the **installed** package over
the `examples/` single source. The gate: `make validate-release`.

- Pinned version: `0.1.2`
- Artifact: `legio-0.1.2-py3-none-any.whl`
- Smoke: validate-release: import ok __version__=0.1.2;validate-release: round-trip ok task=validate-release@example:47a6d9a7-8b5e-4319-a5c5-2fbd22be11f0 output={'transform': {'transformed': 'HELLOHELLO'}};validate-release: artifact smoke passed
- Run at: 2026-09-28T19:19:50Z

The real external-consumer repository (an own repo pinning the release) is the
maintainer's follow-up; this record is the in-repo proof of the wheel.
