"""The shared example tree: four self-contained, domain-free example nodes.

Each node under this directory is a copy-and-adapt skeleton of a real
deployment: its own ``patterns/`` (Schema 1 YAML in three per-type dirs), its
own ``tools.yaml`` (Schema 3) and its own ``legio.yaml`` (LEG-017). Every file
is exercised by the test suite — drift breaks the build (LEG-100, no bitrot).
"""

from __future__ import annotations