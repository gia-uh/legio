# VALIDATIONS — release artifact 0.1.7 (LEG-102, in-repo harness)

Executable proof that the built artifact installs and runs as a consumer would
see it: built via `uv build`, installed into a throwaway virtualenv (deps from
the index), headless domain-free smoke against the **installed** package over
the `examples/` single source. The gate: `make validate-release`.

- Pinned version: `0.1.7`
- Artifact: `legio-0.1.7-py3-none-any.whl`
- Smoke: validate-release: import ok __version__=0.1.7;validate-release: round-trip ok task=validate-release@example:a2d7ad29-05a0-42c5-86ee-32ac41ebc030 output={'transform': {'transformed': 'HELLOHELLO'}};validate-release: artifact smoke passed
- Run at: 2026-09-29T23:50:42Z

The real external-consumer repository (an own repo pinning the release) is the
maintainer's follow-up; this record is the in-repo proof of the wheel.
