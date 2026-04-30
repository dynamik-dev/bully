"""Config schema, parser, loader, scope/skip matching."""

from bully.config.loader import parse_config, resolve_max_workers
from bully.config.parser import ConfigError, Rule, Violation
from bully.config.scope import filter_rules
from bully.config.skip import SKIP_PATTERNS, effective_skip_patterns

__all__ = [
    "ConfigError",
    "Rule",
    "SKIP_PATTERNS",
    "Violation",
    "effective_skip_patterns",
    "filter_rules",
    "parse_config",
    "resolve_max_workers",
]
