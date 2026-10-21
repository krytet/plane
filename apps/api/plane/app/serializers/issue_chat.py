# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import serializers

from plane.db.models import IssueChatBinding
from plane.utils.issue_chat import (
    IssueChatInputError,
    binding_is_unread,
    get_current_user_seen,
    normalize_chat_binding_url,
)

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


class IssueChatBindingCreateSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=IssueChatBinding.Provider.choices)
    thread_id = serializers.CharField(max_length=255, trim_whitespace=True)
    url = serializers.CharField(max_length=2048, required=False, allow_blank=False)
    title = serializers.CharField(max_length=255, trim_whitespace=True, required=False, allow_blank=True)

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


class IssueChatManualAttentionSerializer(serializers.Serializer):
    manual_attention = serializers.ChoiceField(choices=("needs_action", "clear"))


class IssueChatBindingSerializer(BaseSerializer):
    link = IssueLinkSerializer(read_only=True)
    manual_attention = serializers.SerializerMethodField()
    attention = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()
    seen_at = serializers.SerializerMethodField()

    def _request_user(self):
        request = self.context.get("request")
        return getattr(request, "user", None)

    def _seen(self, obj):
        return get_current_user_seen(obj, self._request_user())

    def get_manual_attention(self, obj):
        return obj.is_manual_attention

    def get_attention(self, obj):
        return obj.has_attention

    def get_unread(self, obj):
        return binding_is_unread(obj, self._seen(obj))

    def get_seen_at(self, obj):
        seen = self._seen(obj)
        return seen.updated_at if seen is not None else None

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
            "observation_state",
            "observation_source",
            "observation_observed_at",
            "observation_turn_id",
            "manual_attention",
            "attention",
            "unread",
            "seen_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
