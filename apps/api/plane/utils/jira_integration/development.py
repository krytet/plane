# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib
import re

# Django imports
from django.utils.text import slugify

# Module imports
from plane.db.models import GithubRepositorySync


def _safe_slug(value):
    slug = slugify(value or "work", allow_unicode=False)
    slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
    return (slug or "work")[:80].rstrip("-")


def build_development_plan(*, issue, binding, policy, repository_sync=None):
    repository_sync = (
        repository_sync
        or GithubRepositorySync.objects.filter(
            project=issue.project,
            deleted_at__isnull=True,
        )
        .select_related("repository")
        .first()
    )
    summary = (issue.name or "work item").strip()
    slug = _safe_slug(summary)
    jira_key = binding.jira_issue_key
    branch_name = policy.branch_template.format(jira_key=jira_key, slug=slug)
    pr_title = policy.pr_title_template.format(jira_key=jira_key, summary=summary)
    pr_body = (
        f"JIRA ticket: [{jira_key} - {summary}]({binding.site.site_url}/browse/{jira_key})\n\n"
        "## Summary\n"
        "This change was prepared from the linked Plane work item. "
        "Confirm the implementation and verification evidence before creating a pull request.\n\n"
        "## QA Recommendations\n"
        "Review the linked Jira ticket and the affected user flow before merge.\n"
    )
    plan = {
        "ready": repository_sync is not None,
        "reason": None if repository_sync else "repository_not_configured",
        "issue_id": str(issue.id),
        "jira_issue_key": jira_key,
        "summary": summary,
        "repository": (
            {
                "id": repository_sync.repository.repository_id,
                "owner": repository_sync.repository.owner,
                "name": repository_sync.repository.name,
                "url": repository_sync.repository.url,
            }
            if repository_sync
            else None
        ),
        "branch_name": branch_name,
        "branch_source": policy.repository_base_ref,
        "pr_target": policy.repository_target_ref,
        "pr_title": pr_title,
        "pr_body": pr_body,
    }
    plan["plan_hash"] = hashlib.sha256(
        f"{branch_name}:{policy.repository_base_ref}:{policy.repository_target_ref}:{pr_title}".encode("utf-8")
    ).hexdigest()
    return plan
