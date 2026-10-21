import assert from "node:assert/strict";
import test from "node:test";
// local imports
import { getProjectFileIssueHref, mergeProjectFilePages } from "./helpers.ts";

const file = (id) => ({
  id,
  name: `${id}.pdf`,
  type: "application/pdf",
  size: 1,
  url: `/api/assets/${id}/`,
  created_at: "2026-10-21T12:00:00Z",
  project_id: "project-id",
  project_identifier: "FILES",
  issue_id: "issue-id",
  issue_identifier: "FILES-1",
  issue_name: "Attachment issue",
});

test("merges file pages without duplicating a file", () => {
  assert.deepEqual(
    mergeProjectFilePages([file("one"), file("two")], [file("two"), file("three")]).map((item) => item.id),
    ["one", "two", "three"]
  );
});

test("builds issue links only when the file has an issue", () => {
  assert.equal(getProjectFileIssueHref("acme", "project-id", "issue-id"), "/acme/projects/project-id/issues/issue-id");
  assert.equal(getProjectFileIssueHref("acme", "project-id", null), null);
});
