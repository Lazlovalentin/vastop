"""Kopf handler registration entrypoint.

Importing this package loads all handler modules, which run the kopf decorators
at import time and register them with the operator.
"""

from __future__ import annotations

import logging
from typing import Any

import kopf
from kubernetes import config as k8s_config

from ..config import CONFIG
from . import alert, instance, order, template

__all__ = ["alert", "instance", "order", "template"]


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.persistence.finalizer = CONFIG.finalizer
    settings.posting.level = logging.INFO
    settings.watching.server_timeout = 270
    settings.execution.max_workers = 10
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
