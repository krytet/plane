# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest

from types import SimpleNamespace

from plane.utils.jira_integration import (
    JiraApiError,
    JiraClient,
    JiraCredentials,
    JiraPolicyInput,
    build_english_draft,
    compute_automation_scope_hash,
    decrypt_credential,
    encrypt_credential,
    markdown_to_adf,
    normalize_site_url,
    validate_policy_input,
)
from plane.utils.jira_integration.development import build_development_plan


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


@pytest.mark.unit
def test_jira_site_url_is_strictly_allowlisted():
    assert normalize_site_url("https://example.atlassian.net") == "https://example.atlassian.net"
    for value in ("http://example.atlassian.net", "https://user:pass@example.atlassian.net", "https://example.com"):
        with pytest.raises(ValueError):
            normalize_site_url(value)


@pytest.mark.unit
def test_personal_credentials_are_encrypted_at_rest():
    encrypted = encrypt_credential("secret-token")
    assert "secret-token" not in encrypted
    assert decrypt_credential(encrypted) == "secret-token"


@pytest.mark.unit
def test_english_draft_requires_review_for_cyrillic():
    draft = build_english_draft(
        summary="Создать тикет",
        description_html="<p>Описание</p>",
        source_revision="rev-1",
    )
    assert not draft.is_valid
    assert draft.validation[0]["code"] == "english_required"
    assert "# Short summary" in draft.description_markdown
    assert draft.approval_hash


@pytest.mark.unit
def test_english_draft_has_jackrabbit_sections():
    draft = build_english_draft(
        summary="Reconcile the shipment status",
        description_html="<p>The carrier status is stale.</p>",
        source_revision="rev-2",
    )
    assert draft.is_valid
    assert "# Context" in draft.description_markdown
    assert "# Full description" in draft.description_markdown
    assert draft.description_markdown.index("# Short summary") < draft.description_markdown.index("# Full description")


@pytest.mark.unit
def test_adf_conversion_preserves_headings_and_paragraphs():
    adf = markdown_to_adf("# Title\n\nFirst paragraph.\n\nSecond paragraph.")
    assert adf["type"] == "doc"
    assert adf["content"][0]["type"] == "heading"
    assert adf["content"][0]["content"][0]["text"] == "Title"
    assert adf["content"][1]["type"] == "paragraph"


@pytest.mark.unit
def test_policy_requires_explicit_mapping_targets():
    value = JiraPolicyInput(
        jira_project_key="FP",
        jira_issue_type_id="10001",
        direction="plane_to_jira",
        close_enabled=True,
        reopen_enabled=False,
        state_mappings={"state-1": {}},
    )
    with pytest.raises(Exception) as error:
        validate_policy_input(value)
    assert "Jira status ID" in str(error.value)


@pytest.mark.unit
def test_automation_scope_hash_changes_on_authority_fields():
    first = {"direction": "plane_to_jira", "state_mappings": {"a": {"jira_status_id": "1"}}}
    second = {"direction": "plane_to_jira", "state_mappings": {"a": {"jira_status_id": "2"}}}
    assert compute_automation_scope_hash(first) != compute_automation_scope_hash(second)
    assert compute_automation_scope_hash(first) == compute_automation_scope_hash(dict(first))


@pytest.mark.unit
def test_client_sends_basic_auth_without_exposing_token():
    session = FakeSession(FakeResponse(payload={"accountId": "acc-1", "displayName": "Tester"}))
    client = JiraClient(
        JiraCredentials("https://example.atlassian.net", "tester@example.com", "secret-token"),
        session=session,
    )
    assert client.myself()["accountId"] == "acc-1"
    assert "secret-token" not in str(session.calls)
    assert session.calls[0][1] == "https://example.atlassian.net/rest/api/3/myself"


@pytest.mark.unit
def test_provider_error_is_sanitized_and_classified():
    session = FakeSession(FakeResponse(status_code=401, payload={"errorMessages": ["Authentication failed"]}))
    client = JiraClient(
        JiraCredentials("https://example.atlassian.net", "tester@example.com", "secret-token"),
        session=session,
    )
    with pytest.raises(JiraApiError) as error:
        client.myself()
    assert error.value.code == "jira_auth_error"
    assert "secret-token" not in str(error.value)


@pytest.mark.unit
def test_development_plan_uses_real_jira_key_and_repository_refs():
    issue = SimpleNamespace(id="issue-1", name="Fix label validation", project="project-1")
    binding = SimpleNamespace(jira_issue_key="FP-12345", site=SimpleNamespace(site_url="https://example.atlassian.net"))
    policy = SimpleNamespace(
        branch_template="{jira_key}-{slug}",
        pr_title_template="{jira_key}: {summary}",
        repository_base_ref="master",
        repository_target_ref="staging",
    )
    repository_sync = SimpleNamespace(
        repository=SimpleNamespace(
            repository_id=42,
            owner="floship",
            name="jackrabbit3",
            url="https://github.com/floship/jackrabbit3",
        )
    )
    plan = build_development_plan(
        issue=issue,
        binding=binding,
        policy=policy,
        repository_sync=repository_sync,
    )
    assert plan["ready"] is True
    assert plan["branch_name"] == "FP-12345-fix-label-validation"
    assert plan["pr_title"] == "FP-12345: Fix label validation"
    assert plan["pr_target"] == "staging"
    assert plan["plan_hash"]
