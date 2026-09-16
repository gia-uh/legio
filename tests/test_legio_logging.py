"""Tests for `legio.logging` — logging configuration helper.

legio is a library: modules provision their own loggers under the ``legio.*``
tree and never configure the root logger. The ``configure`` helper lets a
consumer opt in to structured verbosity without touching the root unless asked.
"""

from __future__ import annotations

import logging

from legio.logging import configure, get_legio_logger


def test_logger_names_are_nested_under_legio() -> None:
    logger = get_legio_logger("manager.client")
    assert logger.name == "legio.manager.client"


def test_configure_attaches_handler_to_legio_tree() -> None:
    tree = logging.getLogger("legio")
    tree.handlers.clear()

    configure(level="INFO")

    assert tree.handlers  # a handler was attached
    assert tree.propagate is False


def test_module_loggers_exist_under_legio_tree() -> None:
    for name in (
        "legio.manager.client",
        "legio.agents.tool_agent",
        "legio.manager",
        "legio.federation",
        "legio.security.middleware",
        "legio.naming",
        "legio.tools",
    ):
        logger = logging.getLogger(name)
        assert logger.name == name


def test_configure_does_not_touch_root_by_default() -> None:
    root = logging.getLogger()
    configure(level="DEBUG")
    assert root.level == logging.WARNING
