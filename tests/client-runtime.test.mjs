import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../client.js", import.meta.url), "utf8");

function loadClientFunction(name) {
  const match = source.match(new RegExp(`    function ${name}\\([\\s\\S]*?\\n    \\}`));
  assert.ok(match, `client.js must define ${name}`);
  return vm.runInNewContext(`(${match[0]})`);
}

test("waiting for user input opens that node's workspace", () => {
  const runFollowup = loadClientFunction("runFollowup");
  const action = runFollowup({ run_id: "r1", status: "awaiting_input", nodes: [
    { id: "collect", status: "awaiting_input" },
  ] });
  assert.equal(action.type, "workspace");
  assert.equal(action.runId, "r1");
  assert.equal(action.nodeId, "collect");
});

test("waiting for an agent opens that node's workspace", () => {
  const runFollowup = loadClientFunction("runFollowup");
  const action = runFollowup({ run_id: "r1", status: "awaiting_agent", nodes: [
    { id: "draft", status: "awaiting_agent" },
  ] });
  assert.equal(action.type, "workspace");
  assert.equal(action.nodeId, "draft");
});

test("a failed run opens the failed node's workspace", () => {
  const runFollowup = loadClientFunction("runFollowup");
  const action = runFollowup({ run_id: "r1", status: "failed", nodes: [
    { id: "collect", status: "passed" },
    { id: "draft", status: "failed" },
  ] });
  assert.equal(action.type, "workspace");
  assert.equal(action.nodeId, "draft");
});

test("a completed run opens its results", () => {
  const runFollowup = loadClientFunction("runFollowup");
  const action = runFollowup({ run_id: "r1", status: "completed", nodes: [] });
  assert.equal(action.type, "results");
  assert.equal(action.runId, "r1");
});

test("a revised upstream exposes the results page to update stale downstream", () => {
  const runFollowup = loadClientFunction("runFollowup");
  const action = runFollowup({run_id:"r1",status:"creation_ready",nodes:[{id:"b",status:"stale"}]});
  assert.equal(action.type,"results");
});

test("an active run does not force a drawer open", () => {
  const runFollowup = loadClientFunction("runFollowup");
  assert.equal(runFollowup({ run_id: "r1", status: "running", nodes: [
    { id: "draft", status: "running" },
  ] }), null);
});

test("only a current failed node offers direct continuation", () => {
  const canContinueNode = loadClientFunction("canContinueNode");
  assert.equal(canContinueNode({ status: "failed" }, false), true);
  assert.equal(canContinueNode({ status: "failed" }, true), false);
  assert.equal(canContinueNode({ status: "passed" }, false), false);
});

test("the primary result prefers the declared primary artifact", () => {
  const primaryArtifact = loadClientFunction("primaryArtifact");
  const primary = primaryArtifact({
    primary: [{ path: "card.md", kind: "text" }],
    artifacts: [{ path: "other.md", kind: "text", is_primary: true }],
  });
  assert.equal(primary.path, "card.md");
});

test("only available text artifacts use inline text preview", () => {
  const isInlineTextArtifact = loadClientFunction("isInlineTextArtifact");
  assert.equal(isInlineTextArtifact({ kind: "text", status: "verified" }), true);
  assert.equal(isInlineTextArtifact({ kind: "text", status: "missing" }), false);
  assert.equal(isInlineTextArtifact({ kind: "image", status: "verified" }), false);
});

test("the results selector keeps the requested run during a history refresh", () => {
  const resultRunSelection = loadClientFunction("resultRunSelection");
  const runs = [{ run_id: "old-run" }, { run_id: "older-run" }];
  assert.equal(resultRunSelection(runs, "new-run"), "new-run");
  assert.equal(resultRunSelection(runs, ""), "old-run");
});
