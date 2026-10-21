# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import status
from rest_framework.response import Response

from plane.app.permissions import ROLE, ProjectEntityPermission, allow_permission
from plane.app.serializers import IssueChatBindingSerializer, IssueChatBindingSyncSerializer
from plane.utils.issue_chat import (
    IssueChatConflict,
    IssueChatInputError,
    IssueChatNotFound,
    list_issue_chat_bindings,
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
            bindings = list_issue_chat_bindings(slug, project_id, issue_id)
        except IssueChatNotFound:
            return Response(
                {"error": "The requested resource does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(self.serializer_class(bindings, many=True).data)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id):
        serializer = IssueChatBindingSyncSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = sync_issue_chat_binding(
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

        response_data = IssueChatBindingSerializer(result.binding).data
        response_data["applied"] = result.applied
        return Response(
            response_data,
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )
