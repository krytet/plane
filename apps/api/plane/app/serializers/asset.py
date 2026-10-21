# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from plane.db.models import FileAsset
from rest_framework import serializers

from .base import BaseSerializer


class FileAssetSerializer(BaseSerializer):
    class Meta:
        model = FileAsset
        fields = "__all__"
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]


class ProjectFileOverviewSerializer(BaseSerializer):
    name = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    url = serializers.CharField(source="asset_url", read_only=True)
    project_identifier = serializers.CharField(source="project.identifier", read_only=True)
    issue_identifier = serializers.SerializerMethodField()
    issue_name = serializers.CharField(source="issue.name", read_only=True)

    class Meta:
        model = FileAsset
        fields = [
            "id",
            "name",
            "type",
            "size",
            "url",
            "created_at",
            "project_id",
            "project_identifier",
            "issue_id",
            "issue_identifier",
            "issue_name",
        ]
        read_only_fields = fields

    def get_name(self, obj):
        return obj.attributes.get("name") or str(obj.asset.name).rsplit("/", 1)[-1]

    def get_type(self, obj):
        return obj.attributes.get("type") or "application/octet-stream"

    def get_issue_identifier(self, obj):
        if obj.issue is None or obj.project is None:
            return None
        return f"{obj.project.identifier}-{obj.issue.sequence_id}"
