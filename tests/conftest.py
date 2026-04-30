"""Shared pytest fixtures.

The trust gate refuses to execute rules from an un-reviewed .bully.yml. Every
test exercising run_pipeline would otherwise short-circuit with status
'untrusted' -- so we unconditionally allow configs inside the test process
via the BULLY_TRUST_ALL env var. Tests that explicitly validate the trust
gate must override this by temporarily unsetting the var with monkeypatch.
"""

import os

os.environ.setdefault("BULLY_TRUST_ALL", "1")
