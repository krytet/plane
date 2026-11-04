# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.core.management.base import BaseCommand, CommandError

# Module imports
from plane.db.models import JiraIssueBinding
from plane.utils.jira_integration.sync import poll_binding, process_pending_operations


class Command(BaseCommand):
    help = "Process queued Jira operations or poll active Jira bindings."

    def add_arguments(self, parser):
        parser.add_argument("--process", action="store_true", help="Process queued outbound operations.")
        parser.add_argument("--poll", action="store_true", help="Poll active bindings for inbound status changes.")
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        if not options["process"] and not options["poll"]:
            raise CommandError("Choose --process or --poll")
        if options["process"]:
            operations = process_pending_operations(limit=options["limit"])
            self.stdout.write(self.style.SUCCESS(f"Processed {len(operations)} operation(s)."))
        if options["poll"]:
            bindings = JiraIssueBinding.objects.filter(is_active=True, deleted_at__isnull=True)[: options["limit"]]
            results = [poll_binding(binding) for binding in bindings]
            applied = sum(1 for result in results if result.get("applied"))
            self.stdout.write(self.style.SUCCESS(f"Polled {len(results)} binding(s); applied {applied} change(s)."))
            for result in results:
                if result.get("reason") not in {"no_change", "verified"}:
                    self.stdout.write(f"{result.get('reason')}")
