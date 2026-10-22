# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from collections.abc import Mapping

from plane.db.models import IssueChatBinding
from plane.utils.issue_chat import normalize_codex_thread_id

TOKEN_LABEL = "Plane local Codex progress sync"
BOT_USERNAME_PREFIX = "plane_codex_progress_sync_"
BINDING_ROUTE_NAMES = frozenset({"issue-chat-binding", "work-item-chat-binding"})
OBSERVATION_ROUTE_NAME = "work-item-chat-binding-observation"


def codex_sync_bot_username(workspace_id):
    return f"{BOT_USERNAME_PREFIX}{workspace_id.hex}"


def is_codex_sync_bot(user):
    if not user or not user.is_bot or not user.username.startswith(BOT_USERNAME_PREFIX):
        return False
    suffix = user.username.removeprefix(BOT_USERNAME_PREFIX)
    return len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix.lower())


def codex_sync_bot_request_allowed(request, api_token):
    resolver_match = getattr(request, "resolver_match", None)
    if resolver_match is None:
        resolver_match = getattr(getattr(request, "_request", None), "resolver_match", None)
    if (
        resolver_match is None
        or api_token.workspace is None
        or api_token.user_type != 1
        or api_token.label != TOKEN_LABEL
        or api_token.user.username != codex_sync_bot_username(api_token.workspace_id)
    ):
        return False

    method = request.method.upper()
    route_name = resolver_match.url_name
    kwargs = resolver_match.kwargs
    if method not in {"GET", "POST"}:
        return False
    if kwargs.get("slug") != api_token.workspace.slug:
        return False

    bindings = IssueChatBinding.objects.filter(
        workspace_id=api_token.workspace_id,
        project_id=kwargs.get("project_id"),
        issue_id=kwargs.get("issue_id"),
        provider=IssueChatBinding.Provider.CODEX,
        deleted_at__isnull=True,
    )

    if route_name in BINDING_ROUTE_NAMES:
        if method == "POST":
            data = request.data
            if not isinstance(data, Mapping):
                return False
            if data.get("provider") != IssueChatBinding.Provider.CODEX:
                return False
            thread_id = normalize_codex_thread_id(data.get("thread_id"))
            if thread_id is None:
                return False
            bindings = bindings.filter(thread_id=thread_id)
        return bindings.exists()

    if route_name == OBSERVATION_ROUTE_NAME:
        return method == "POST" and bindings.filter(pk=kwargs.get("binding_id")).exists()

    return False
