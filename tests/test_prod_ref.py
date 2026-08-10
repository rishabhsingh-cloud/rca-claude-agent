"""Unit tests for prod_ref / PROD_BRANCHES (gitlab_client.py).

The agent does PRODUCTION RCA, but these repos' GitLab `default_branch` is each
team's active-dev branch (qa-master / pre-pro-master / qa), NOT prod. `prod_ref()`
must return the mapped PROD branch for known repos and fall back to `default_ref`
for everything else — reading the wrong branch fed the agent QA/pre-prod code.
"""
from __future__ import annotations

import pytest

from rca_agent.gitlab_client import PROD_BRANCHES, MockGitLabClient, RestGitLabClient


def test_the_prod_critical_repos_are_mapped_to_master():
    # Guard the map's contents: the RCA-critical services must resolve to prod.
    for repo in ("mastersindia/arap-auth-service", "mastersindia/gst-enterprise-service"):
        assert PROD_BRANCHES.get(repo) == "master"


@pytest.mark.parametrize("project,branch", sorted(PROD_BRANCHES.items()))
def test_prod_ref_returns_the_mapped_branch(project, branch):
    # prod_ref must actually consult the map (not ignore it) for known repos.
    assert MockGitLabClient().prod_ref(project) == branch


def test_prod_ref_falls_back_to_default_for_unmapped_repo():
    m = MockGitLabClient()
    unmapped = "acme/billing-service"
    assert unmapped not in PROD_BRANCHES
    # An unmapped repo must not crash — it defers to default_ref.
    assert m.prod_ref(unmapped) == m.default_ref(unmapped)


def test_both_clients_implement_prod_ref():
    # The GitLabClient protocol requires prod_ref; no implementation may miss it.
    assert callable(getattr(MockGitLabClient, "prod_ref", None))
    assert callable(getattr(RestGitLabClient, "prod_ref", None))


def test_rest_prod_ref_map_hit_needs_no_network():
    # A mapped repo short-circuits before default_ref, so the REST client returns
    # the prod branch with no HTTP call. __new__ skips __init__ so no live client
    # is built — we're testing the map-hit branch in isolation.
    inst = RestGitLabClient.__new__(RestGitLabClient)
    assert inst.prod_ref("mastersindia/gst-enterprise-service") == "master"
