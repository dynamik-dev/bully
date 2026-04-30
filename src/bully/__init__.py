"""bully — agentic lint pipeline for Claude Code.

The package is organized as cohesive subpackages (config/, engines/, diff/,
semantic/, state/, runtime/, cli/, bench/). The names re-exported here are
the public surface used by tests and external callers; the underscore-
prefixed aliases preserve the historical names from the pre-restructure
single-file `pipeline.py`. Prefer the unprefixed names for new code.
"""

from __future__ import annotations

# Pinned by `release-bully` alongside .claude-plugin/plugin.json and
# pyproject.toml. Stamped into the `session_init` telemetry record so the
# analyzer can attribute records back to the producer version. Bump this
# whenever you bump the project version.
BULLY_VERSION = "0.8.2"
TELEMETRY_SCHEMA_VERSION = 1

from bully.cli import main
from bully.cli.args import normalize_argv as _normalize_argv
from bully.cli.doctor import check_python_version as _check_python_version
from bully.cli.session import (
    cmd_session_record as _cmd_session_record,
)
from bully.cli.session import (
    cmd_session_start as _cmd_session_start,
)
from bully.cli.stop import (
    cmd_stop as _cmd_stop,
)
from bully.cli.stop import (
    cmd_subagent_stop as _cmd_subagent_stop,
)
from bully.config.loader import parse_config, resolve_max_workers
from bully.config.parser import (
    ConfigError,
    Rule,
    Violation,
    _build_rule,
    _parse_scalar,
)
from bully.config.parser import parse_single_file as _parse_single_file
from bully.config.scope import filter_rules, scope_glob_matches
from bully.config.scope import scope_glob_matches as _scope_glob_matches
from bully.config.skip import (
    SKIP_PATTERNS,
    effective_skip_patterns,
    path_matches_skip,
)
from bully.config.skip import path_matches_skip as _path_matches_skip
from bully.diff.analysis import (
    can_match_diff,
    rule_add_perspective,
)
from bully.diff.analysis import can_match_diff as _can_match_diff
from bully.diff.analysis import rule_add_perspective as _rule_add_perspective
from bully.diff.context import (
    build_diff_context,
    cap_write_content,
    was_write_truncated,
    was_write_truncated_for_path,
)
from bully.diff.context import cap_write_content as _cap_write_content
from bully.diff.context import was_write_truncated as _was_write_truncated
from bully.diff.context import was_write_truncated_for_path as _was_write_truncated_for_path
from bully.engines.ast_grep import (
    ast_grep_available,
    execute_ast_rule,
    infer_ast_language,
    parse_ast_grep_json,
)
from bully.engines.ast_grep import infer_ast_language as _infer_ast_language
from bully.engines.ast_grep import parse_ast_grep_json as _parse_ast_grep_json
from bully.engines.output import parse_script_output
from bully.engines.script import (
    capability_env,
    combine_streams,
    execute_script_rule,
    tail_for_description,
)
from bully.engines.script import capability_env as _capability_env
from bully.engines.script import combine_streams as _combine_streams
from bully.engines.script import tail_for_description as _tail_for_description
from bully.runtime.hook_io import (
    build_semantic_prompt,
    format_blocked_stderr,
    read_stdin_payload,
)
from bully.runtime.hook_io import format_blocked_stderr as _format_blocked_stderr
from bully.runtime.runner import run_pipeline
from bully.semantic.payload import build_semantic_payload, build_semantic_payload_dict
from bully.state.baseline import (
    is_baselined,
    line_checksum,
    line_has_disable,
    parse_disable_directive,
)
from bully.state.baseline import is_baselined as _is_baselined
from bully.state.baseline import line_has_disable as _line_has_disable
from bully.state.baseline import parse_disable_directive as _parse_disable_directive
from bully.state.trust import (
    cmd_trust,
    config_checksum,
    trust_status,
    untrusted_stderr,
)
from bully.state.trust import cmd_trust as _cmd_trust
from bully.state.trust import config_checksum as _config_checksum
from bully.state.trust import trust_status as _trust_status

__all__ = [
    "BULLY_VERSION",
    "ConfigError",
    "Rule",
    "SKIP_PATTERNS",
    "TELEMETRY_SCHEMA_VERSION",
    "Violation",
    "ast_grep_available",
    "build_diff_context",
    "build_semantic_payload",
    "build_semantic_payload_dict",
    "build_semantic_prompt",
    "cap_write_content",
    "capability_env",
    "check_python_version",
    "cmd_trust",
    "combine_streams",
    "config_checksum",
    "effective_skip_patterns",
    "execute_ast_rule",
    "execute_script_rule",
    "filter_rules",
    "format_blocked_stderr",
    "infer_ast_language",
    "is_baselined",
    "line_checksum",
    "line_has_disable",
    "main",
    "parse_ast_grep_json",
    "parse_config",
    "parse_disable_directive",
    "parse_script_output",
    "path_matches_skip",
    "read_stdin_payload",
    "resolve_max_workers",
    "rule_add_perspective",
    "run_pipeline",
    "scope_glob_matches",
    "tail_for_description",
    "trust_status",
    "untrusted_stderr",
    "was_write_truncated",
    "was_write_truncated_for_path",
]


# ---------------------------------------------------------------------------
# Local helper used by `bully.cli.doctor.check_python_version` re-export. The
# import landed at module level above; this tiny re-export keeps the
# unprefixed name available alongside the underscored alias.
# ---------------------------------------------------------------------------
check_python_version = _check_python_version
