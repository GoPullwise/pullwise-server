import { readFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { buildV1Publication, buildV1TerminalPublication } from "../../../pullwise-worker/src/runtime/v1-publication.ts";
import { materializeReviewPayload } from "../../../pullwise-worker/src/runtime/review-evidence.ts";

const input = JSON.parse(readFileSync(0, "utf8"));
const finishedAt = Date.now();
const facts = { sessionId: "pi-session-fixture", model: { provider: "pullwise-gateway", id: "reviewer" },
  usage: { input: 10, output: 30, cacheRead: 0, cacheWrite: 0, total: 40, cost: 0.00007 },
  startedAt: finishedAt - 250, finishedAt };
const base = { workerId: input.workerId, workerVersion: "0.10.24", job: input.job };
if (input.mode === "failed") {
  process.stdout.write(JSON.stringify(buildV1TerminalPublication({ ...base, status: "failed", error: "budget exceeded", facts })));
} else {
  const workspace = await mkdtemp(path.join(tmpdir(), "pi-publication-contract-"));
  try {
    await writeFile(path.join(workspace, "app.js"), "broken();\n");
    const payload = await materializeReviewPayload(JSON.stringify({ summary: "Candidate", findings: [{
      path: "app.js", evidence_text: "broken();", title: "Candidate", category: "CORRECTNESS", severity: "HIGH",
      confidence_bps: 9000, explanation: "Possible failure", impact: "Requests fail", remediation: "Validate first",
      rule_id: null, validation_status: "UNVALIDATED",
    }], coverage: [{ path: "app.js", state: "REVIEWED", reason_code: null }] }), {
      attemptId: "attempt-fixture", workspace, provider: facts.model.provider, model: facts.model.id,
      thinkingLevel: "low", context: {}, budget: { wallTimeMs: 1000, inputTokens: 100, outputTokens: 100, cacheReadTokens: 100, cacheWriteTokens: 100 },
    });
    process.stdout.write(JSON.stringify(await buildV1Publication({ ...base, workspace,
      result: { ...facts, attemptId: "attempt-fixture", payload } })));
  } finally { await rm(workspace, { recursive: true, force: true }); }
}
