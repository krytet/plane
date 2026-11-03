# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib
import json
from dataclasses import dataclass

# Module imports
from .errors import JiraIntegrationError


@dataclass(frozen=True)
class JiraPolicyInput:
    jira_project_key: str
    jira_issue_type_id: str
    direction: str
    close_enabled: bool
    reopen_enabled: bool
    state_mappings: dict
    board_id: str = ""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_automation_scope_hash(scope):
    authority_fields = {
        "direction": scope.get("direction", "off"),
        "state_mappings": scope.get("state_mappings", {}),
        "close_enabled": bool(scope.get("close_enabled")),
        "reopen_enabled": bool(scope.get("reopen_enabled")),
        "allowed_events": sorted(scope.get("allowed_events", [])),
        "jira_project_key": scope.get("jira_project_key", ""),
        "jira_issue_type_id": scope.get("jira_issue_type_id", ""),
    }
    return hashlib.sha256(canonical_json(authority_fields).encode("utf-8")).hexdigest()


def validate_policy_input(value):
    if not isinstance(value, JiraPolicyInput):
        raise TypeError("policy must be a JiraPolicyInput")
    if not value.jira_project_key.strip():
        raise JiraIntegrationError("Jira project is required", field="jira_project_key")
    if not value.jira_issue_type_id.strip():
        raise JiraIntegrationError("Jira issue type is required", field="jira_issue_type_id")
    allowed_directions = {"off", "plane_to_jira", "jira_to_plane", "bidirectional"}
    if value.direction not in allowed_directions:
        raise JiraIntegrationError("Unsupported sync direction", field="direction")
    if not isinstance(value.state_mappings, dict):
        raise JiraIntegrationError("State mappings must be an object", field="state_mappings")
    for source_state, target in value.state_mappings.items():
        if not source_state or not isinstance(target, dict):
            raise JiraIntegrationError("Each mapping needs a state and object target", field="state_mappings")
        if value.direction in {"plane_to_jira", "bidirectional"} and not target.get("jira_status_id"):
            raise JiraIntegrationError(
                "Outbound mappings require a Jira status ID",
                field="state_mappings",
            )
        if value.direction in {"jira_to_plane", "bidirectional"} and not target.get("plane_state_id"):
            raise JiraIntegrationError(
                "Inbound mappings require a Plane state ID",
                field="state_mappings",
            )
    if value.reopen_enabled and not value.close_enabled:
        raise JiraIntegrationError("Reopen requires close to be enabled", field="reopen_enabled")
    return value
