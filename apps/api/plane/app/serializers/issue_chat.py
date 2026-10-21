# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import serializers

from plane.db.models import IssueChatBinding
from plane.utils.issue_chat import IssueChatInputError, normalize_chat_binding_url

from .base import BaseSerializer
from .issue import IssueLinkSerializer


class IssueChatProgressSerializer(serializers.Serializer):
    done = serializers.ListField(
        child=serializers.CharField(max_length=1000, trim_whitespace=True),
        max_length=50,
        required=False,
        default=list,
    )
    now = serializers.ListField(
        child=serializers.CharField(max_length=1000, trim_whitespace=True),
        max_length=50,
        required=False,
        default=list,
    )
    waiting = serializers.ListField(
        child=serializers.CharField(max_length=1000, trim_whitespace=True),
        max_length=50,
        required=False,
        default=list,
    )

    def validate(self, attrs):
        return {key: [item for item in attrs.get(key, []) if item.strip()] for key in ("done", "now", "waiting")}


class IssueChatBindingSyncSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=IssueChatBinding.Provider.choices)
    thread_id = serializers.CharField(max_length=255, trim_whitespace=True)
    url = serializers.CharField(max_length=2048, required=False, allow_blank=False)
    title = serializers.CharField(max_length=255, trim_whitespace=True, required=False, allow_blank=True)
    cursor = serializers.IntegerField(min_value=1, max_value=9223372036854775807)
    progress = IssueChatProgressSerializer()

    def validate_thread_id(self, value):
        if not value:
            raise serializers.ValidationError("Thread ID cannot be blank.")
        return value

    def validate(self, attrs):
        attrs["title"] = attrs.get("title") or None
        try:
            attrs["url"] = normalize_chat_binding_url(
                attrs["provider"],
                attrs["thread_id"],
                attrs.get("url"),
            )
        except IssueChatInputError as exc:
            raise serializers.ValidationError({exc.field: exc.message}) from exc
        return attrs


class IssueChatBindingSerializer(BaseSerializer):
    link = IssueLinkSerializer(read_only=True)

    class Meta:
        model = IssueChatBinding
        fields = [
            "id",
            "issue_id",
            "link",
            "provider",
            "thread_id",
            "sync_cursor",
            "progress",
            "last_synced_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
