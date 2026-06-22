"""RCA Agent — root-cause-analysis over Jira tickets + self-hosted GitLab.

Accuracy-first: every conclusion is backed by deterministic evidence fetched
from live (or mocked) GitLab. The trace-first slice implemented here is the
highest-accuracy path: stack trace -> file:line -> blame -> introducing MR ->
regression verdict.
"""

__version__ = "0.1.0"
