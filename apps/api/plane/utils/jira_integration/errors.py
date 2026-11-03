# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.


class JiraIntegrationError(Exception):
    code = "jira_integration_error"
    status_code = 400

    def __init__(self, message, *, field=None, details=None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.details = details or {}
