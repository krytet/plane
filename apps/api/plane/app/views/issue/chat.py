# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import status
from rest_framework.response import Response

from plane.app.permissions import ROLE, ProjectEntityPermission, allow_permission
from plane.app.serializers import (
    IssueChatBindingCreateSerializer,
    IssueChatBindingSerializer,
    IssueChatBindingSyncSerializer,
    IssueChatManualAttentionSerializer,
)
from plane.utils.issue_chat import (
    IssueChatConflict,
    IssueChatInputError,
    IssueChatNotFound,
    acknowledge_issue_chat_binding,
    create_issue_chat_binding,
    list_issue_chat_bindings,
    set_issue_chat_manual_attention,
    sync_issue_chat_binding,
)

from ..base import BaseAPIView


class IssueChatBindingEndpoint(BaseAPIView):
    """Browser-facing chat bindings for one work item."""

    permission_classes = [ProjectEntityPermission]
    serializer_class = IssueChatBindingSerializer

    @staticmethod
    def _input_error(exc):
        return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)

    @staticmethod
    def _conflict(exc):
        return Response(
            {"error": exc.message, "code": exc.code},
            status=status.HTTP_409_CONFLICT,
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def get(self, request, slug, project_id, issue_id):
        try:
            bindings = list_issue_chat_bindings(slug, project_id, issue_id, user=request.user)
        except IssueChatNotFound:
            return Response(
                {"error": "The requested resource does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            self.serializer_class(
                bindings,
                many=True,
                context={"request": request},
            ).data
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id):
        has_sync_fields = "cursor" in request.data or "progress" in request.data
        serializer_class = (
            IssueChatBindingSyncSerializer if has_sync_fields else IssueChatBindingCreateSerializer
        )
        serializer = serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            if has_sync_fields:
                result = sync_issue_chat_binding(
                    workspace_slug=slug,
                    project_id=project_id,
                    issue_id=issue_id,
                    actor=request.user,
                    **serializer.validated_data,
                )
            else:
                result = create_issue_chat_binding(
                    workspace_slug=slug,
                    project_id=project_id,
                    issue_id=issue_id,
                    actor=request.user,
                    **serializer.validated_data,
                )
        except IssueChatNotFound:
            return Response(
                {"error": "The requested resource does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except IssueChatInputError as exc:
            return self._input_error(exc)
        except IssueChatConflict as exc:
            return self._conflict(exc)

        response_data = IssueChatBindingSerializer(
            result.binding,
            context={"request": request},
        ).data
        response_data["applied"] = getattr(result, "applied", False)
        return Response(
            response_data,
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )


class IssueChatBindingSeenEndpoint(BaseAPIView):
    """Acknowledge the current observation for the requesting Plane user."""

    permission_classes = [ProjectEntityPermission]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id, binding_id):
        try:
            result = acknowledge_issue_chat_binding(
                workspace_slug=slug,
                project_id=project_id,
                issue_id=issue_id,
                binding_id=binding_id,
                actor=request.user,
            )
        except IssueChatNotFound:
            return Response(
                {"error": "The requested resource does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )

        response_data = IssueChatBindingSerializer(
            result.binding,
            context={"request": request},
        ).data
        response_data["applied"] = result.applied
        response_data["acknowledgement_source"] = "plane"
        return Response(response_data, status=status.HTTP_200_OK)


class IssueChatManualAttentionEndpoint(BaseAPIView):
    """Set or clear a manual ChatGPT attention flag."""

    permission_classes = [ProjectEntityPermission]

    @staticmethod
    def _conflict(exc):
        return Response(
            {"error": exc.message, "code": exc.code},
            status=status.HTTP_409_CONFLICT,
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id, binding_id):
        serializer = IssueChatManualAttentionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = set_issue_chat_manual_attention(
                workspace_slug=slug,
                project_id=project_id,
                issue_id=issue_id,
                binding_id=binding_id,
                needs_action=serializer.validated_data["manual_attention"] == "needs_action",
            )
        except IssueChatNotFound:
            return Response(
                {"error": "The requested resource does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except IssueChatConflict as exc:
            return self._conflict(exc)

        response_data = IssueChatBindingSerializer(
            result.binding,
            context={"request": request},
        ).data
        response_data["applied"] = result.applied
        return Response(response_data, status=status.HTTP_200_OK)
