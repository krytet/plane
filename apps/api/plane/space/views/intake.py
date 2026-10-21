# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json

# Django import
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, OuterRef, Func, F, Prefetch
from django.core.serializers.json import DjangoJSONEncoder

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from .base import BaseViewSet
from plane.db.models import IntakeIssue, Issue, IssueLink, FileAsset, DeployBoard, State, StateGroup
from plane.app.serializers import (
    IssueSerializer,
    IntakeIssueSerializer,
    IssueCreateSerializer,
    IssueStateIntakeSerializer,
)
from plane.utils.content_validator import validate_html_content
from plane.utils.issue_filters import issue_filters
from plane.utils.issue_chat import (
    IssueChatConflict,
    merge_latest_chat_description,
    redact_issue_description_fields,
    redact_chat_progress_html,
    redact_chat_progress_json,
    reject_managed_chat_progress,
)
from plane.bgtasks.issue_activities_task import issue_activity
from plane.db.models.intake import SourceType


class IntakeIssuePublicViewSet(BaseViewSet):
    serializer_class = IntakeIssueSerializer
    model = IntakeIssue

    filterset_fields = ["status"]

    def get_queryset(self):
        project_deploy_board = DeployBoard.objects.get(
            workspace__slug=self.kwargs.get("slug"),
            project_id=self.kwargs.get("project_id"),
        )
        if project_deploy_board is not None:
            return self.filter_queryset(
                super()
                .get_queryset()
                .filter(
                    Q(snoozed_till__gte=timezone.now()) | Q(snoozed_till__isnull=True),
                    project_id=self.kwargs.get("project_id"),
                    workspace__slug=self.kwargs.get("slug"),
                    intake_id=self.kwargs.get("intake_id"),
                )
                .select_related("issue", "workspace", "project")
            )
        return IntakeIssue.objects.none()

    def list(self, request, anchor, intake_id):
        project_deploy_board = DeployBoard.objects.get(anchor=anchor, entity_name="project")
        if project_deploy_board.intake is None:
            return Response(
                {"error": "Intake is not enabled for this Project Board"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = issue_filters(request.query_params, "GET")
        issues = (
            Issue.objects.filter(
                issue_intake__intake_id=intake_id,
                workspace_id=project_deploy_board.workspace_id,
                project_id=project_deploy_board.project_id,
            )
            .filter(**filters)
            .annotate(bridge_id=F("issue_intake__id"))
            .select_related("workspace", "project", "state", "parent")
            .prefetch_related("assignees", "labels")
            .order_by("issue_intake__snoozed_till", "issue_intake__status")
            .annotate(
                sub_issues_count=Issue.issue_objects.filter(parent=OuterRef("id"))
                .order_by()
                .annotate(count=Func(F("id"), function="Count"))
                .values("count")
            )
            .annotate(
                link_count=IssueLink.objects.filter(issue=OuterRef("id"))
                .order_by()
                .annotate(count=Func(F("id"), function="Count"))
                .values("count")
            )
            .annotate(
                attachment_count=FileAsset.objects.filter(
                    issue_id=OuterRef("id"),
                    entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
                )
                .order_by()
                .annotate(count=Func(F("id"), function="Count"))
                .values("count")
            )
            .prefetch_related(
                Prefetch(
                    "issue_intake",
                    queryset=IntakeIssue.objects.only("status", "duplicate_to", "snoozed_till", "source"),
                )
            )
        )
        issues_data = IssueStateIntakeSerializer(issues, many=True).data
        return Response(issues_data, status=status.HTTP_200_OK)

    def create(self, request, anchor, intake_id):
        project_deploy_board = DeployBoard.objects.get(anchor=anchor, entity_name="project")
        if project_deploy_board.intake is None:
            return Response(
                {"error": "Intake is not enabled for this Project Board"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Ensure the intake belongs to this board before writing: a
        # caller-supplied intake_id must be bound to the anchor.
        if str(intake_id) != str(project_deploy_board.intake_id):
            return Response(
                {"error": "Intake does not belong to this Project Board"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not request.data.get("issue", {}).get("name", False):
            return Response({"error": "Name is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Check for valid priority
        if request.data.get("issue", {}).get("priority", "none") not in [
            "low",
            "medium",
            "high",
            "urgent",
            "none",
        ]:
            return Response({"error": "Invalid priority"}, status=status.HTTP_400_BAD_REQUEST)

        # get the triage state
        triage_state = State.triage_objects.filter(
            project_id=project_deploy_board.project_id, workspace_id=project_deploy_board.workspace_id
        ).first()

        if not triage_state:
            triage_state = State.objects.create(
                name="Triage",
                group=StateGroup.TRIAGE.value,
                project_id=project_deploy_board.project_id,
                workspace_id=project_deploy_board.workspace_id,
                color="#4E5355",
                sequence=65000,
                default=False,
            )

        # Sanitize description_html before saving to prevent stored XSS
        issue_data = request.data.get("issue", {})
        raw_description_html = issue_data.get("description_html", "<p></p>")
        _, _, sanitized_description_html = validate_html_content(raw_description_html)
        safe_description_html = sanitized_description_html if sanitized_description_html is not None else "<p></p>"
        description_json = issue_data.get("description") or issue_data.get("description_json") or {}
        try:
            reject_managed_chat_progress(safe_description_html, description_json)
        except IssueChatConflict as exc:
            return Response(
                {"error": exc.message, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # create an issue
        issue = Issue.objects.create(
            name=issue_data.get("name"),
            description_json=description_json,
            description_html=safe_description_html,
            priority=issue_data.get("priority", "low"),
            project_id=project_deploy_board.project_id,
            state_id=triage_state.id,
        )

        # Create an Issue Activity
        issue_activity.delay(
            type="issue.activity.created",
            requested_data=json.dumps(request.data, cls=DjangoJSONEncoder),
            actor_id=str(request.user.id),
            issue_id=str(issue.id),
            project_id=str(project_deploy_board.project_id),
            current_instance=None,
            epoch=int(timezone.now().timestamp()),
        )
        # create an intake issue
        IntakeIssue.objects.create(
            intake_id=intake_id,
            project_id=project_deploy_board.project_id,
            issue=issue,
            source=SourceType.IN_APP,
        )

        serializer = IssueStateIntakeSerializer(issue)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def partial_update(self, request, anchor, intake_id, pk):
        project_deploy_board = DeployBoard.objects.get(anchor=anchor, entity_name="project")
        if project_deploy_board.intake is None:
            return Response(
                {"error": "Intake is not enabled for this Project Board"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        intake_issue = IntakeIssue.objects.get(
            pk=pk,
            workspace_id=project_deploy_board.workspace_id,
            project_id=project_deploy_board.project_id,
            intake_id=intake_id,
        )
        # Get the project member
        if str(intake_issue.created_by_id) != str(request.user.id):
            return Response(
                {"error": "You cannot edit intake issues"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get issue data
        issue_data = request.data.pop("issue", False)

        issue = Issue.objects.get(
            pk=intake_issue.issue_id,
            workspace_id=project_deploy_board.workspace_id,
            project_id=project_deploy_board.project_id,
        )
        # viewers and guests since only viewers and guests
        issue_data = {
            "name": issue_data.get("name", issue.name),
            "description_html": redact_chat_progress_html(issue_data.get("description_html", issue.description_html)),
            "description_json": redact_chat_progress_json(issue_data.get("description_json", issue.description_json)),
        }

        issue_serializer = IssueCreateSerializer(
            issue,
            data=issue_data,
            partial=True,
            context={"project_id": project_deploy_board.project_id, "allow_triage_state": True},
        )

        if issue_serializer.is_valid():
            current_instance = issue
            # Log all the updates
            requested_data = json.dumps(issue_data, cls=DjangoJSONEncoder)
            if issue is not None:
                issue_activity.delay(
                    type="issue.activity.updated",
                    requested_data=requested_data,
                    actor_id=str(request.user.id),
                    issue_id=str(issue.id),
                    project_id=str(project_deploy_board.project_id),
                    current_instance=json.dumps(IssueSerializer(current_instance).data, cls=DjangoJSONEncoder),
                    epoch=int(timezone.now().timestamp()),
                )
            try:
                with transaction.atomic():
                    locked_issue = (
                        Issue.objects.select_for_update(of=("self",))
                        .filter(
                            pk=intake_issue.issue_id,
                            workspace_id=project_deploy_board.workspace_id,
                            project_id=project_deploy_board.project_id,
                        )
                        .first()
                    )
                    if locked_issue is None:
                        return Response({"error": "Work item not found"}, status=status.HTTP_404_NOT_FOUND)
                    merged_description, merged_description_json = merge_latest_chat_description(
                        locked_issue,
                        issue_serializer.validated_data["description_html"],
                        issue_serializer.validated_data.get("description_json"),
                    )
                    issue_serializer.instance = locked_issue
                    issue_serializer.validated_data["description_html"] = merged_description
                    if merged_description_json is not None:
                        issue_serializer.validated_data["description_json"] = merged_description_json
                    issue_serializer.save()
            except IssueChatConflict as exc:
                return Response(
                    {"error": exc.message, "code": exc.code},
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(redact_issue_description_fields(issue_serializer.data), status=status.HTTP_200_OK)
        return Response(issue_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, anchor, intake_id, pk):
        project_deploy_board = DeployBoard.objects.get(anchor=anchor, entity_name="project")
        if project_deploy_board.intake is None:
            return Response(
                {"error": "Intake is not enabled for this Project Board"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        intake_issue = IntakeIssue.objects.get(
            pk=pk,
            workspace_id=project_deploy_board.workspace_id,
            project_id=project_deploy_board.project_id,
            intake_id=intake_id,
        )
        issue = Issue.objects.get(
            pk=intake_issue.issue_id,
            workspace_id=project_deploy_board.workspace_id,
            project_id=project_deploy_board.project_id,
        )
        serializer = IssueStateIntakeSerializer(issue)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def destroy(self, request, anchor, intake_id, pk):
        project_deploy_board = DeployBoard.objects.get(anchor=anchor, entity_name="project")
        if project_deploy_board.intake is None:
            return Response(
                {"error": "Intake is not enabled for this Project Board"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        intake_issue = IntakeIssue.objects.get(
            pk=pk,
            workspace_id=project_deploy_board.workspace_id,
            project_id=project_deploy_board.project_id,
            intake_id=intake_id,
        )

        if str(intake_issue.created_by_id) != str(request.user.id):
            return Response(
                {"error": "You cannot delete intake issue"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        intake_issue.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
