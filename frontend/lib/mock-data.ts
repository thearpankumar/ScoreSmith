import type {
  ChatMessage,
  ChatSession,
  Evaluation,
  EvaluationKpiResult,
  Guideline,
  KpiNode,
  Scorecard,
  ScorecardDraft,
  ScorecardVersion,
  User,
} from "./types";
import { computeKpiRollup, leafKpiNodes as leafKpiNodesOf } from "./kpi-tree";

/**
 * Realistic sample data standing in for the backend (Cycle 1 API is being
 * built in parallel and is not ready yet — see lib/api-client.ts). Shapes
 * mirror the Postgres schema sketch in the plan (scorecards /
 * scorecard_versions / kpi_nodes / kpi_guidelines / evaluations /
 * evaluation_kpi_results / chat_sessions / chat_messages) so this file can
 * be deleted wholesale once the real API client implementation lands,
 * without the components needing to change.
 */

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

export const MOCK_CURRENT_USER: User = {
  id: "user-1",
  name: "Arpan Kumar",
  email: "arpankumar1119@gmail.com",
  role: "user",
};

export const MOCK_USERS: User[] = [
  MOCK_CURRENT_USER,
  { id: "user-2", name: "Priya Nair", email: "priya.nair@talenciaglobal.example", role: "user" },
  { id: "user-3", name: "Marcus Chen", email: "marcus.chen@talenciaglobal.example", role: "user" },
  { id: "user-4", name: "Sofia Alvarez", email: "sofia.alvarez@talenciaglobal.example", role: "user" },
];

function userName(id: string): string {
  return MOCK_USERS.find((u) => u.id === id)?.name ?? "Unknown";
}

// ---------------------------------------------------------------------------
// Guideline text helpers
// ---------------------------------------------------------------------------

const LEVEL_TIER_LABEL: Record<number, string> = {
  0: "Absent / harmful",
  1: "Severely deficient",
  2: "Deficient",
  3: "Well below expectation",
  4: "Below expectation",
  5: "Borderline",
  6: "Meets expectation",
  7: "Solidly meets expectation",
  8: "Exceeds expectation",
  9: "Strongly exceeds expectation",
  10: "Exemplary",
};

/** Generic but KPI-specific guideline text for KPIs that aren't hand-authored below. */
function genericGuidelineText(kpiName: string, level: number): string {
  const tier = LEVEL_TIER_LABEL[level];
  if (level === 0) return `No evidence of ${kpiName.toLowerCase()} present; output fails this dimension entirely.`;
  if (level <= 2) return `${tier}: ${kpiName} falls far short of a usable standard; substantial rework required.`;
  if (level <= 4) return `${tier}: ${kpiName} shows real gaps that a reviewer would flag before this ships.`;
  if (level === 5) return `${tier}: ${kpiName} is passable but has at least one issue worth noting.`;
  if (level === 6) return `${tier}: ${kpiName} meets the baseline standard expected for this KPI.`;
  if (level === 7) return `${tier}: ${kpiName} clearly meets the standard with no notable issues.`;
  if (level === 8) return `${tier}: ${kpiName} is executed well above the baseline standard.`;
  if (level === 9) return `${tier}: ${kpiName} is executed at a near-ideal level with proactive extra value.`;
  return `${tier}: ${kpiName} represents best-in-class execution with no room identified for improvement.`;
}

let guidelineIdCounter = 0;
function makeGuidelines(kpiNodeId: string, kpiName: string, texts?: Partial<Record<number, string>>): Guideline[] {
  const levels = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  return levels.map((level) => ({
    id: `gl-${kpiNodeId}-${level}-${++guidelineIdCounter}`,
    kpiNodeId,
    scoreLevel: level,
    qualitativeText: texts?.[level] ?? genericGuidelineText(kpiName, level),
  }));
}

// ---------------------------------------------------------------------------
// Scorecard 1: Customer Support Email Response Quality (flat 6-KPI, hero scorecard)
// ---------------------------------------------------------------------------

const SC1_ID = "sc-1";
const SC1_VERSION_ID = "scv-1";

const sc1Kpi = {
  accuracy: "kpi-sc1-accuracy",
  tone: "kpi-sc1-tone",
  completeness: "kpi-sc1-completeness",
  timeliness: "kpi-sc1-timeliness",
  grammar: "kpi-sc1-grammar",
  compliance: "kpi-sc1-compliance",
};

const sc1KpiNodes: KpiNode[] = [
  {
    id: sc1Kpi.accuracy,
    scorecardVersionId: SC1_VERSION_ID,
    parentId: null,
    path: "1",
    level: 1,
    name: "Response Accuracy",
    description: "Does the reply contain factually correct account, order, and policy information?",
    weight: 20,
    displayOrder: 1,
    includedInScoring: true,
    guidelines: makeGuidelines(sc1Kpi.accuracy, "Response Accuracy", {
      0: "Response contains factually incorrect information that could mislead the customer or cause harm.",
      1: "Response is almost entirely inaccurate; only a passing reference to the correct topic.",
      2: "Major inaccuracies dominate the response; customer would be misinformed on the core issue.",
      3: "Several material inaccuracies remain uncorrected; agent appears unfamiliar with the product.",
      4: "One or more inaccuracies affect the customer's ability to resolve their issue.",
      5: "Mostly accurate but contains a minor factual error that does not affect the outcome.",
      6: "Accurate on all material points; phrasing is imprecise in a way that could cause minor confusion.",
      7: "Fully accurate and addresses the customer's actual question.",
      8: "Fully accurate, addresses the question, and proactively confirms the correct account/order context.",
      9: "Fully accurate, precise, and anticipates a likely follow-up question with correct information.",
      10: "Fully accurate, precise, cites the exact policy/order detail relied on, and leaves no room for misinterpretation.",
    }),
  },
  {
    id: sc1Kpi.tone,
    scorecardVersionId: SC1_VERSION_ID,
    parentId: null,
    path: "2",
    level: 1,
    name: "Tone & Empathy",
    description: "Does the reply acknowledge how the customer feels and respond with appropriate warmth?",
    weight: 20,
    displayOrder: 2,
    includedInScoring: true,
    guidelines: makeGuidelines(sc1Kpi.tone, "Tone & Empathy", {
      0: "Response is rude, dismissive, or blames the customer.",
      1: "Tone is curt and impatient throughout; no acknowledgement of customer frustration.",
      2: "Tone is mechanical and cold; reads as a form response to a personal complaint.",
      3: "Minimal empathy; acknowledges the issue exists but not how the customer feels.",
      4: "Polite but generic; empathy statement feels templated and disconnected from specifics.",
      5: "Professional and polite; one genuine empathetic statement tied to the customer's situation.",
      6: "Warm and professional throughout; empathy is specific to what the customer described.",
      7: "Warm, specific empathy plus a clear statement of ownership for resolving the issue.",
      8: "Empathetic, personalized, and reassuring; tone matches the customer's emotional state.",
      9: "Empathetic and personalized; proactively reassures the customer about next steps and timing.",
      10: "Exceptional empathy that de-escalates a frustrated customer while remaining fully professional and solution-focused.",
    }),
  },
  {
    id: sc1Kpi.completeness,
    scorecardVersionId: SC1_VERSION_ID,
    parentId: null,
    path: "3",
    level: 1,
    name: "Resolution Completeness",
    description: "Does the reply fully resolve the customer's actual request, not just a symptom of it?",
    weight: 15,
    displayOrder: 3,
    includedInScoring: true,
    guidelines: makeGuidelines(sc1Kpi.completeness, "Resolution Completeness", {
      0: "Customer's issue is not addressed at all.",
      1: "Only tangentially related information is provided; core request ignored.",
      2: "Partial acknowledgement of the issue but no actionable resolution offered.",
      3: "Resolution addresses a symptom, not the underlying request.",
      4: "Resolution addresses the request but leaves an open question unanswered.",
      5: "Resolution addresses the primary request with minor gaps in detail.",
      6: "Resolution fully addresses the primary request.",
      7: "Resolution fully addresses the request and confirms the specific next step.",
      8: "Resolution fully addresses the request, confirms next steps, and sets a clear timeline.",
      9: "Resolution fully addresses the request, sets a timeline, and proactively prevents a likely follow-up issue.",
      10: "Resolution is complete, timely, proactive, and includes a documented fallback if the fix doesn't hold.",
    }),
  },
  {
    id: sc1Kpi.timeliness,
    scorecardVersionId: SC1_VERSION_ID,
    parentId: null,
    path: "4",
    level: 1,
    name: "Response Timeliness",
    description: "Was the reply sent within the support SLA window for this ticket priority?",
    weight: 15,
    displayOrder: 4,
    includedInScoring: true,
    guidelines: makeGuidelines(sc1Kpi.timeliness, "Response Timeliness", {
      0: "No response sent within the SLA window; case abandoned.",
      1: "Response sent more than 5x the SLA target with no earlier acknowledgement.",
      2: "Response sent well outside SLA (3-5x target) with no interim update.",
      3: "Response sent outside SLA (2-3x target); no interim acknowledgement sent.",
      4: "Response sent slightly outside SLA with an interim acknowledgement sent earlier.",
      5: "Response sent at the edge of the SLA window.",
      6: "Response sent within SLA.",
      7: "Response sent comfortably within SLA (within 75% of target window).",
      8: "Response sent well within SLA (within 50% of target window).",
      9: "Response sent well within SLA and an interim acknowledgement was sent within minutes.",
      10: "Response sent immediately with an interim acknowledgement, fully within SLA with margin to spare.",
    }),
  },
  {
    id: sc1Kpi.grammar,
    scorecardVersionId: SC1_VERSION_ID,
    parentId: null,
    path: "5",
    level: 1,
    name: "Grammar & Clarity",
    description: "Is the reply grammatically clean, well-structured, and easy to scan?",
    weight: 15,
    displayOrder: 5,
    includedInScoring: true,
    guidelines: makeGuidelines(sc1Kpi.grammar, "Grammar & Clarity", {
      0: "Response is largely unintelligible due to grammar/spelling errors.",
      1: "Frequent grammar and spelling errors obscure meaning in multiple sentences.",
      2: "Several grammar/spelling errors; readable but distracting.",
      3: "Noticeable grammar or structural issues that slow comprehension.",
      4: "Minor grammar issues; one sentence requires re-reading.",
      5: "Mostly clean; one or two minor typos with no impact on meaning.",
      6: "Clean grammar and spelling throughout.",
      7: "Clean and well-structured with clear paragraph breaks for scannability.",
      8: "Clean, well-structured, and concise; no filler.",
      9: "Clean, concise, and uses formatting (bullets/bold) to make next steps scannable.",
      10: "Publication-quality clarity: concise, well-formatted, and immediately scannable for the exact information the customer needs.",
    }),
  },
  {
    id: sc1Kpi.compliance,
    scorecardVersionId: SC1_VERSION_ID,
    parentId: null,
    path: "6",
    level: 1,
    name: "Policy Compliance",
    description: "Does the reply stay within refund, data-privacy, and disclosure policy?",
    weight: 15,
    displayOrder: 6,
    includedInScoring: true,
    guidelines: makeGuidelines(sc1Kpi.compliance, "Policy Compliance", {
      0: "Response violates company policy in a way that creates legal/financial risk.",
      1: "Response promises an outcome explicitly disallowed by policy.",
      2: "Response contradicts stated policy on a material point.",
      3: "Response is ambiguous about a policy-restricted item (e.g., refund eligibility).",
      4: "Response follows policy but omits a required disclosure.",
      5: "Response follows policy with all required disclosures, phrased informally.",
      6: "Response fully complies with policy and includes all required disclosures.",
      7: "Fully compliant, with disclosures phrased clearly in plain language.",
      8: "Fully compliant, clearly phrased, and cites the specific policy applied.",
      9: "Fully compliant, cites policy, and proactively flags a related policy the customer should know about.",
      10: "Fully compliant, precisely cited, and anticipates policy-adjacent questions before the customer has to ask.",
    }),
  },
];

const sc1: Scorecard = {
  id: SC1_ID,
  name: "Customer Support Email Response Quality",
  domain: "Customer Support",
  ownerId: MOCK_CURRENT_USER.id,
  ownerName: MOCK_CURRENT_USER.name,
  purposeStatement:
    "Measure whether a support agent's email reply resolves the customer's issue accurately, empathetically, and within policy.",
  scope: "Applies to all outbound support email replies (excludes live chat and phone transcripts).",
  targetScore: 8.5,
  status: "published",
  currentVersionId: SC1_VERSION_ID,
  kpiCount: sc1KpiNodes.length,
  createdAt: "2026-08-04T09:12:00Z",
  updatedAt: "2026-09-20T14:05:00Z",
};

const sc1Version: ScorecardVersion = {
  id: SC1_VERSION_ID,
  scorecardId: SC1_ID,
  versionNumber: 2,
  guidelineNotes: "v2: tightened the Response Accuracy 8-10 band wording after calibration review; no weight changes.",
  createdBy: MOCK_CURRENT_USER.id,
  createdAt: "2026-09-20T14:05:00Z",
  isActive: true,
  scoringFormula: null,
  kpiNodes: sc1KpiNodes,
};

// ---------------------------------------------------------------------------
// Scorecard 2: Technical Documentation Review (Level 1-4 hierarchy)
// ---------------------------------------------------------------------------

const SC2_ID = "sc-2";
const SC2_VERSION_ID = "scv-2";

const sc2Ids = {
  contentQuality: "kpi-sc2-content-quality",
  technicalAccuracy: "kpi-sc2-technical-accuracy",
  codeSamples: "kpi-sc2-code-samples",
  codeSamplesRun: "kpi-sc2-code-samples-run",
  terminology: "kpi-sc2-terminology",
  completeness: "kpi-sc2-completeness",
  structure: "kpi-sc2-structure",
  headings: "kpi-sc2-headings",
  crosslinking: "kpi-sc2-crosslinking",
};

const sc2KpiNodes: KpiNode[] = [
  {
    id: sc2Ids.contentQuality,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: null,
    path: "1",
    level: 1,
    name: "Content Quality",
    weight: 60,
    displayOrder: 1,
    includedInScoring: true,
  },
  {
    id: sc2Ids.technicalAccuracy,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: sc2Ids.contentQuality,
    path: "1.1",
    level: 2,
    name: "Technical Accuracy",
    weight: 60,
    displayOrder: 1,
    includedInScoring: true,
  },
  {
    id: sc2Ids.codeSamples,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: sc2Ids.technicalAccuracy,
    path: "1.1.1",
    level: 3,
    name: "Code Sample Correctness",
    weight: 70,
    displayOrder: 1,
    includedInScoring: true,
  },
  {
    id: sc2Ids.codeSamplesRun,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: sc2Ids.codeSamples,
    path: "1.1.1.1",
    level: 4,
    name: "Runs Without Modification",
    weight: 100,
    displayOrder: 1,
    includedInScoring: true,
    guidelines: makeGuidelines(sc2Ids.codeSamplesRun, "Runs Without Modification"),
  },
  {
    id: sc2Ids.terminology,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: sc2Ids.technicalAccuracy,
    path: "1.1.2",
    level: 3,
    name: "Terminology Consistency",
    weight: 30,
    displayOrder: 2,
    includedInScoring: true,
    guidelines: makeGuidelines(sc2Ids.terminology, "Terminology Consistency"),
  },
  {
    id: sc2Ids.completeness,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: sc2Ids.contentQuality,
    path: "1.2",
    level: 2,
    name: "Completeness",
    weight: 40,
    displayOrder: 2,
    includedInScoring: true,
    guidelines: makeGuidelines(sc2Ids.completeness, "Completeness"),
  },
  {
    id: sc2Ids.structure,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: null,
    path: "2",
    level: 1,
    name: "Structure & Navigation",
    weight: 40,
    displayOrder: 2,
    includedInScoring: true,
  },
  {
    id: sc2Ids.headings,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: sc2Ids.structure,
    path: "2.1",
    level: 2,
    name: "Heading Hierarchy",
    weight: 50,
    displayOrder: 1,
    includedInScoring: true,
    guidelines: makeGuidelines(sc2Ids.headings, "Heading Hierarchy"),
  },
  {
    id: sc2Ids.crosslinking,
    scorecardVersionId: SC2_VERSION_ID,
    parentId: sc2Ids.structure,
    path: "2.2",
    level: 2,
    name: "Cross-linking",
    weight: 50,
    displayOrder: 2,
    includedInScoring: true,
    guidelines: makeGuidelines(sc2Ids.crosslinking, "Cross-linking"),
  },
];

const sc2: Scorecard = {
  id: SC2_ID,
  name: "Technical Documentation Review",
  domain: "Documentation",
  ownerId: "user-3",
  ownerName: userName("user-3"),
  purposeStatement: "Assess whether a technical doc page is accurate, complete, and easy to navigate before publishing.",
  scope: "Applies to public API reference and how-to guide pages; excludes internal design docs.",
  targetScore: 8.0,
  status: "published",
  currentVersionId: SC2_VERSION_ID,
  kpiCount: sc2KpiNodes.length,
  createdAt: "2026-07-22T11:00:00Z",
  updatedAt: "2026-09-15T16:30:00Z",
};

const sc2Version: ScorecardVersion = {
  id: SC2_VERSION_ID,
  scorecardId: SC2_ID,
  versionNumber: 1,
  guidelineNotes: "Initial version, imported from the docs team's existing review checklist.",
  createdBy: "user-3",
  createdAt: "2026-07-22T11:00:00Z",
  isActive: true,
  scoringFormula: null,
  kpiNodes: sc2KpiNodes,
};

// ---------------------------------------------------------------------------
// Scorecard 3: Sales Proposal Quality (draft status)
// ---------------------------------------------------------------------------

const SC3_ID = "sc-3";
const SC3_VERSION_ID = "scv-3";

const sc3Ids = {
  value: "kpi-sc3-value",
  pricing: "kpi-sc3-pricing",
  differentiation: "kpi-sc3-differentiation",
  personalization: "kpi-sc3-personalization",
  cta: "kpi-sc3-cta",
};

const sc3KpiNodes: KpiNode[] = [
  { id: sc3Ids.value, scorecardVersionId: SC3_VERSION_ID, parentId: null, path: "1", level: 1, name: "Value Proposition Clarity", weight: 25, displayOrder: 1, includedInScoring: true, guidelines: makeGuidelines(sc3Ids.value, "Value Proposition Clarity") },
  { id: sc3Ids.pricing, scorecardVersionId: SC3_VERSION_ID, parentId: null, path: "2", level: 1, name: "Pricing Transparency", weight: 20, displayOrder: 2, includedInScoring: true, guidelines: makeGuidelines(sc3Ids.pricing, "Pricing Transparency") },
  { id: sc3Ids.differentiation, scorecardVersionId: SC3_VERSION_ID, parentId: null, path: "3", level: 1, name: "Competitive Differentiation", weight: 20, displayOrder: 3, includedInScoring: true, guidelines: makeGuidelines(sc3Ids.differentiation, "Competitive Differentiation") },
  { id: sc3Ids.personalization, scorecardVersionId: SC3_VERSION_ID, parentId: null, path: "4", level: 1, name: "Personalization", weight: 20, displayOrder: 4, includedInScoring: true, guidelines: makeGuidelines(sc3Ids.personalization, "Personalization") },
  { id: sc3Ids.cta, scorecardVersionId: SC3_VERSION_ID, parentId: null, path: "5", level: 1, name: "Call-to-Action Strength", weight: 15, displayOrder: 5, includedInScoring: true, guidelines: makeGuidelines(sc3Ids.cta, "Call-to-Action Strength") },
];

const sc3: Scorecard = {
  id: SC3_ID,
  name: "Sales Proposal Quality",
  domain: "Sales",
  ownerId: "user-4",
  ownerName: userName("user-4"),
  purposeStatement: "Score outbound sales proposals before they go to a prospect, to catch weak differentiation or unclear pricing.",
  scope: "Applies to formal written proposals for deals over $10k ACV.",
  targetScore: 7.5,
  status: "draft",
  currentVersionId: SC3_VERSION_ID,
  kpiCount: sc3KpiNodes.length,
  createdAt: "2026-09-18T10:00:00Z",
  updatedAt: "2026-09-24T08:45:00Z",
};

const sc3Version: ScorecardVersion = {
  id: SC3_VERSION_ID,
  scorecardId: SC3_ID,
  versionNumber: 1,
  guidelineNotes: "Draft — guidelines still being reviewed by the sales enablement team before publishing.",
  createdBy: "user-4",
  createdAt: "2026-09-18T10:00:00Z",
  isActive: true,
  scoringFormula: null,
  kpiNodes: sc3KpiNodes,
};

// ---------------------------------------------------------------------------
// Scorecard 4: Code Review Quality Gate (boundary case: 2 KPIs)
// ---------------------------------------------------------------------------

const SC4_ID = "sc-4";
const SC4_VERSION_ID = "scv-4";

const sc4Ids = {
  correctness: "kpi-sc4-correctness",
  readability: "kpi-sc4-readability",
};

const sc4KpiNodes: KpiNode[] = [
  {
    id: sc4Ids.correctness,
    scorecardVersionId: SC4_VERSION_ID,
    parentId: null,
    path: "1",
    level: 1,
    name: "Correctness & Test Coverage",
    weight: 60,
    displayOrder: 1,
    includedInScoring: true,
    guidelines: makeGuidelines(sc4Ids.correctness, "Correctness & Test Coverage", {
      0: "Change does not build, or tests are absent for new logic entirely.",
      6: "Change builds, core logic is covered by tests, and tests pass in CI.",
      10: "Change builds, edge cases are covered by tests, and CI includes a regression test for the specific bug fixed.",
    }),
  },
  {
    id: sc4Ids.readability,
    scorecardVersionId: SC4_VERSION_ID,
    parentId: null,
    path: "2",
    level: 1,
    name: "Readability & Maintainability",
    weight: 40,
    displayOrder: 2,
    includedInScoring: true,
    guidelines: makeGuidelines(sc4Ids.readability, "Readability & Maintainability", {
      0: "Code is unreadable; no reviewer could safely modify it later.",
      6: "Code is readable with reasonable names and no obvious duplication.",
      10: "Code is exceptionally clear, documented where non-obvious, and easier to extend than before the change.",
    }),
  },
];

const sc4: Scorecard = {
  id: SC4_ID,
  name: "Code Review Quality Gate",
  domain: "Engineering",
  ownerId: "user-3",
  ownerName: userName("user-3"),
  purposeStatement: "A lightweight two-KPI gate used to flag pull requests that need a closer human look before merge.",
  scope: "Applies to all backend service pull requests over 50 changed lines.",
  targetScore: 8.5,
  status: "published",
  currentVersionId: SC4_VERSION_ID,
  kpiCount: sc4KpiNodes.length,
  createdAt: "2026-06-10T09:00:00Z",
  updatedAt: "2026-09-01T12:00:00Z",
};

const sc4Version: ScorecardVersion = {
  id: SC4_VERSION_ID,
  scorecardId: SC4_ID,
  versionNumber: 1,
  guidelineNotes: "Deliberately minimal — two KPIs only, used as a fast pre-merge signal rather than a full review.",
  createdBy: "user-3",
  createdAt: "2026-06-10T09:00:00Z",
  isActive: true,
  scoringFormula: null,
  kpiNodes: sc4KpiNodes,
};

// ---------------------------------------------------------------------------
// Scorecard 5: Marketing Copy Brand Alignment (archived)
// ---------------------------------------------------------------------------

const SC5_ID = "sc-5";
const SC5_VERSION_ID = "scv-5";

const sc5Ids = {
  voice: "kpi-sc5-voice",
  clarity: "kpi-sc5-clarity",
  compliance: "kpi-sc5-compliance",
  cta: "kpi-sc5-cta",
};

const sc5KpiNodes: KpiNode[] = [
  { id: sc5Ids.voice, scorecardVersionId: SC5_VERSION_ID, parentId: null, path: "1", level: 1, name: "Brand Voice Consistency", weight: 30, displayOrder: 1, includedInScoring: true, guidelines: makeGuidelines(sc5Ids.voice, "Brand Voice Consistency") },
  { id: sc5Ids.clarity, scorecardVersionId: SC5_VERSION_ID, parentId: null, path: "2", level: 1, name: "Message Clarity", weight: 30, displayOrder: 2, includedInScoring: true, guidelines: makeGuidelines(sc5Ids.clarity, "Message Clarity") },
  { id: sc5Ids.compliance, scorecardVersionId: SC5_VERSION_ID, parentId: null, path: "3", level: 1, name: "Compliance & Claims Accuracy", weight: 25, displayOrder: 3, includedInScoring: true, guidelines: makeGuidelines(sc5Ids.compliance, "Compliance & Claims Accuracy") },
  { id: sc5Ids.cta, scorecardVersionId: SC5_VERSION_ID, parentId: null, path: "4", level: 1, name: "Call-to-Action Effectiveness", weight: 15, displayOrder: 4, includedInScoring: true, guidelines: makeGuidelines(sc5Ids.cta, "Call-to-Action Effectiveness") },
];

const sc5: Scorecard = {
  id: SC5_ID,
  name: "Marketing Copy Brand Alignment",
  domain: "Marketing",
  ownerId: "user-4",
  ownerName: userName("user-4"),
  purposeStatement: "Check campaign copy against brand voice and regulatory claims guidelines before it ships.",
  scope: "Applies to paid ad copy and landing page hero sections.",
  targetScore: 7.0,
  status: "archived",
  currentVersionId: SC5_VERSION_ID,
  kpiCount: sc5KpiNodes.length,
  createdAt: "2026-03-02T09:00:00Z",
  updatedAt: "2026-06-30T09:00:00Z",
};

const sc5Version: ScorecardVersion = {
  id: SC5_VERSION_ID,
  scorecardId: SC5_ID,
  versionNumber: 1,
  guidelineNotes: "Archived after Q2 rebrand; superseded scorecard not yet rebuilt for the new brand guide.",
  createdBy: "user-4",
  createdAt: "2026-03-02T09:00:00Z",
  isActive: true,
  scoringFormula: null,
  kpiNodes: sc5KpiNodes,
};

// ---------------------------------------------------------------------------
// Aggregate scorecard exports
// ---------------------------------------------------------------------------

export const MOCK_SCORECARDS: Scorecard[] = [sc1, sc2, sc3, sc4, sc5];

export const MOCK_SCORECARD_VERSIONS: ScorecardVersion[] = [
  sc1Version,
  sc2Version,
  sc3Version,
  sc4Version,
  sc5Version,
];

// Tree helpers (buildKpiTree, leafKpiNodes, computeKpiRollup) now live in
// ./kpi-tree so they can be imported without pulling in this whole sample
// dataset. Re-exported here for any existing call sites.
export { buildKpiTree, leafKpiNodes, computeKpiRollup } from "./kpi-tree";

// ---------------------------------------------------------------------------
// Evaluations
// ---------------------------------------------------------------------------

function weightedScore(results: Array<{ score: number; weight: number }>): number {
  const total = results.reduce((sum, r) => sum + r.score * r.weight, 0);
  return Math.round((total / 100) * 100) / 100;
}

function ragBandForScore(score: number): Evaluation["ragBand"] {
  if (score >= 9) return "excellent";
  if (score >= 8) return "good";
  if (score >= 7) return "acceptable";
  if (score >= 6) return "needs-improvement";
  if (score >= 5) return "weak";
  if (score >= 4) return "poor";
  return "critical";
}

function guidelineText(kpiId: string, level: number): string {
  const node = sc1KpiNodes.find((n) => n.id === kpiId) ?? sc4KpiNodes.find((n) => n.id === kpiId);
  return node?.guidelines?.find((g) => g.scoreLevel === level)?.qualitativeText ?? "";
}

const eval1Results: EvaluationKpiResult[] = [
  {
    id: "ekr-1-1",
    evaluationId: "eval-1",
    kpiNodeId: sc1Kpi.accuracy,
    kpiName: "Response Accuracy",
    kpiPath: "1",
    level: 1,
    weight: 20,
    score: 9,
    matchedGuidelineLevel: 9,
    matchedGuidelineText: guidelineText(sc1Kpi.accuracy, 9),
    reasoningText:
      "The agent correctly identified the order (#48213) as eligible for a full refund under the 30-day policy, cited the exact order date, and correctly anticipated the customer's likely follow-up about refund timing by stating the 3-5 business day processing window.",
    evidenceQuotes: [
      "Your order #48213 from September 2nd qualifies for a full refund since it's within our 30-day window.",
      "You should see the refund back on your original payment method within 3-5 business days.",
    ],
  },
  {
    id: "ekr-1-2",
    evaluationId: "eval-1",
    kpiNodeId: sc1Kpi.tone,
    kpiName: "Tone & Empathy",
    kpiPath: "2",
    level: 1,
    weight: 20,
    score: 8,
    matchedGuidelineLevel: 8,
    matchedGuidelineText: guidelineText(sc1Kpi.tone, 8),
    reasoningText:
      "The reply opens with a specific acknowledgement of the customer's frustration about the delayed order (not a generic apology) and reassures them about next steps, matching the escalated tone of the original complaint.",
    evidenceQuotes: [
      "I completely understand how frustrating it is to wait two extra weeks for something you needed for a specific date — I'm sorry we let you down here.",
    ],
  },
  {
    id: "ekr-1-3",
    evaluationId: "eval-1",
    kpiNodeId: sc1Kpi.completeness,
    kpiName: "Resolution Completeness",
    kpiPath: "3",
    level: 1,
    weight: 15,
    score: 7,
    matchedGuidelineLevel: 7,
    matchedGuidelineText: guidelineText(sc1Kpi.completeness, 7),
    reasoningText:
      "The refund is processed and the next step (3-5 day timeline) is confirmed, but the agent did not proactively address whether the customer's linked loyalty points from the order would also be reversed, which the customer had asked about in a prior message.",
    evidenceQuotes: [
      "I've gone ahead and processed your refund — you'll see it in 3-5 business days.",
    ],
  },
  {
    id: "ekr-1-4",
    evaluationId: "eval-1",
    kpiNodeId: sc1Kpi.timeliness,
    kpiName: "Response Timeliness",
    kpiPath: "4",
    level: 1,
    weight: 15,
    score: 6,
    matchedGuidelineLevel: 6,
    matchedGuidelineText: guidelineText(sc1Kpi.timeliness, 6),
    reasoningText:
      "Reply was sent 7 hours 40 minutes after the ticket was opened, inside the 8-hour SLA for standard-priority tickets but without an earlier interim acknowledgement.",
    evidenceQuotes: ["[Ticket metadata] opened_at: 09:02, first_response_at: 16:41, sla_target_hours: 8"],
  },
  {
    id: "ekr-1-5",
    evaluationId: "eval-1",
    kpiNodeId: sc1Kpi.grammar,
    kpiName: "Grammar & Clarity",
    kpiPath: "5",
    level: 1,
    weight: 15,
    score: 9,
    matchedGuidelineLevel: 9,
    matchedGuidelineText: guidelineText(sc1Kpi.grammar, 9),
    reasoningText:
      "Reply is concise, uses a short bulleted list for the two action items, and has no grammar or spelling issues.",
    evidenceQuotes: ["Here's what happens next:\n- Refund processed today\n- Funds visible in 3-5 business days"],
  },
  {
    id: "ekr-1-6",
    evaluationId: "eval-1",
    kpiNodeId: sc1Kpi.compliance,
    kpiName: "Policy Compliance",
    kpiPath: "6",
    level: 1,
    weight: 15,
    score: 8,
    matchedGuidelineLevel: 8,
    matchedGuidelineText: guidelineText(sc1Kpi.compliance, 8),
    reasoningText:
      "Refund decision correctly cites the 30-day policy by name and stays within the disclosure requirements for refund communications.",
    evidenceQuotes: ["...qualifies for a full refund since it's within our 30-day window."],
  },
];

const eval1: Evaluation = {
  id: "eval-1",
  scorecardId: SC1_ID,
  scorecardVersionId: SC1_VERSION_ID,
  scorecardName: sc1.name,
  domain: sc1.domain,
  name: "Ticket #48213 — refund escalation",
  evaluatedBy: MOCK_CURRENT_USER.id,
  evaluatedByName: MOCK_CURRENT_USER.name,
  inputSummary: "Support agent reply to a delayed-order refund escalation.",
  status: "completed",
  finalWeightedScore: weightedScore(eval1Results),
  targetScore: sc1.targetScore,
  ragBand: ragBandForScore(weightedScore(eval1Results)),
  submittedAt: "2026-09-22T17:05:00Z",
  kpiResults: eval1Results,
};

const eval2Results: EvaluationKpiResult[] = [
  { id: "ekr-2-1", evaluationId: "eval-2", kpiNodeId: sc1Kpi.accuracy, kpiName: "Response Accuracy", kpiPath: "1", level: 1, weight: 20, score: 10, matchedGuidelineLevel: 10, matchedGuidelineText: guidelineText(sc1Kpi.accuracy, 10), reasoningText: "Billing explanation cites the exact line item and proration formula, with no factual errors, and pre-empts the next likely question about next month's invoice.", evidenceQuotes: ["The $12.40 charge is the prorated difference for upgrading mid-cycle on Sept 14th.", "Your next invoice on Oct 1st will be the full new-plan amount, $49.00."] },
  { id: "ekr-2-2", evaluationId: "eval-2", kpiNodeId: sc1Kpi.tone, kpiName: "Tone & Empathy", kpiPath: "2", level: 1, weight: 20, score: 9, matchedGuidelineLevel: 9, matchedGuidelineText: guidelineText(sc1Kpi.tone, 9), reasoningText: "Warm, personalized opening referencing the customer's specific confusion, plus proactive reassurance about the next invoice.", evidenceQuotes: ["I can see why that extra charge looked confusing on your statement — let me walk you through exactly where it came from."] },
  { id: "ekr-2-3", evaluationId: "eval-2", kpiNodeId: sc1Kpi.completeness, kpiName: "Resolution Completeness", kpiPath: "3", level: 1, weight: 15, score: 9, matchedGuidelineLevel: 9, matchedGuidelineText: guidelineText(sc1Kpi.completeness, 9), reasoningText: "Fully explains the current charge and prevents a follow-up ticket by explaining the next invoice amount in advance.", evidenceQuotes: ["Your next invoice on Oct 1st will be the full new-plan amount, $49.00."] },
  { id: "ekr-2-4", evaluationId: "eval-2", kpiNodeId: sc1Kpi.timeliness, kpiName: "Response Timeliness", kpiPath: "4", level: 1, weight: 15, score: 9, matchedGuidelineLevel: 9, matchedGuidelineText: guidelineText(sc1Kpi.timeliness, 9), reasoningText: "First response sent 22 minutes after the ticket opened, well within the 8-hour SLA, with an automatic interim acknowledgement logged 2 minutes after ticket creation.", evidenceQuotes: ["[Ticket metadata] opened_at: 10:00, ack_sent_at: 10:02, first_response_at: 10:22, sla_target_hours: 8"] },
  { id: "ekr-2-5", evaluationId: "eval-2", kpiNodeId: sc1Kpi.grammar, kpiName: "Grammar & Clarity", kpiPath: "5", level: 1, weight: 15, score: 10, matchedGuidelineLevel: 10, matchedGuidelineText: guidelineText(sc1Kpi.grammar, 10), reasoningText: "Clean, well-formatted explanation with the exact dollar figures bolded for scannability.", evidenceQuotes: ["**$12.40** prorated charge · **$49.00** next full invoice on **Oct 1st**"] },
  { id: "ekr-2-6", evaluationId: "eval-2", kpiNodeId: sc1Kpi.compliance, kpiName: "Policy Compliance", kpiPath: "6", level: 1, weight: 15, score: 9, matchedGuidelineLevel: 9, matchedGuidelineText: guidelineText(sc1Kpi.compliance, 9), reasoningText: "Correctly cites the proration policy and proactively flags the billing-cycle policy the customer will hit next.", evidenceQuotes: ["This follows our standard mid-cycle proration policy."] },
];

const eval2: Evaluation = {
  id: "eval-2",
  scorecardId: SC1_ID,
  scorecardVersionId: SC1_VERSION_ID,
  scorecardName: sc1.name,
  domain: sc1.domain,
  name: "Ticket #48339 — billing question",
  evaluatedBy: "user-2",
  evaluatedByName: userName("user-2"),
  inputSummary: "Support agent reply explaining a mid-cycle proration charge.",
  status: "completed",
  finalWeightedScore: weightedScore(eval2Results),
  targetScore: sc1.targetScore,
  ragBand: ragBandForScore(weightedScore(eval2Results)),
  submittedAt: "2026-09-24T11:30:00Z",
  kpiResults: eval2Results,
};

const eval3Results: EvaluationKpiResult[] = [
  {
    id: "ekr-3-1",
    evaluationId: "eval-3",
    kpiNodeId: sc4Ids.correctness,
    kpiName: "Correctness & Test Coverage",
    kpiPath: "1",
    level: 1,
    weight: 60,
    score: 6,
    matchedGuidelineLevel: 6,
    matchedGuidelineText: guidelineText(sc4Ids.correctness, 6),
    reasoningText:
      "The PR builds and the new retry-backoff function has a unit test covering the happy path, but there is no test for the max-retries-exceeded branch, which is the branch the linked bug report was about.",
    evidenceQuotes: ["def test_retry_backoff_succeeds_on_second_attempt(): ...", "# TODO: add coverage for max_retries exhausted"],
  },
  {
    id: "ekr-3-2",
    evaluationId: "eval-3",
    kpiNodeId: sc4Ids.readability,
    kpiName: "Readability & Maintainability",
    kpiPath: "2",
    level: 1,
    weight: 40,
    score: 5,
    matchedGuidelineLevel: 5,
    matchedGuidelineText: guidelineText(sc4Ids.readability, 5),
    reasoningText:
      "Function names are reasonable but the retry loop duplicates logic already present in `utils/retry.py` instead of reusing it, and there are no comments explaining the backoff multiplier choice.",
    evidenceQuotes: ["for attempt in range(1, max_retries + 1): time.sleep(2 ** attempt) ..."],
  },
];

const eval3: Evaluation = {
  id: "eval-3",
  scorecardId: SC4_ID,
  scorecardVersionId: SC4_VERSION_ID,
  scorecardName: sc4.name,
  domain: sc4.domain,
  name: "PR #1123 — payment retry logic",
  evaluatedBy: "user-3",
  evaluatedByName: userName("user-3"),
  inputSummary: "Pull request adding retry/backoff to the payment webhook handler.",
  status: "completed",
  finalWeightedScore: weightedScore(eval3Results),
  targetScore: sc4.targetScore,
  ragBand: ragBandForScore(weightedScore(eval3Results)),
  submittedAt: "2026-09-19T09:15:00Z",
  kpiResults: eval3Results,
};

// Evaluation 4: Technical Documentation Review (exercises the full
// Level1->Level4 hierarchy in the evaluation result page's tree table).
const eval4LeafScores: Record<string, number> = {
  [sc2Ids.codeSamplesRun]: 8,
  [sc2Ids.terminology]: 7,
  [sc2Ids.completeness]: 6,
  [sc2Ids.headings]: 9,
  [sc2Ids.crosslinking]: 7,
};

const eval4Rollup = computeKpiRollup(sc2KpiNodes, eval4LeafScores);

const eval4Results: EvaluationKpiResult[] = leafKpiNodesOf(sc2KpiNodes).map((kpi, idx) => ({
  id: `ekr-4-${idx + 1}`,
  evaluationId: "eval-4",
  kpiNodeId: kpi.id,
  kpiName: kpi.name,
  kpiPath: kpi.path,
  level: kpi.level,
  weight: kpi.weight ?? 0,
  score: eval4LeafScores[kpi.id],
  matchedGuidelineLevel: eval4LeafScores[kpi.id],
  matchedGuidelineText:
    kpi.guidelines?.find((g) => g.scoreLevel === eval4LeafScores[kpi.id])?.qualitativeText ?? "",
  reasoningText: `The submitted page's treatment of "${kpi.name.toLowerCase()}" matches level ${eval4LeafScores[kpi.id]} — see the cited section below.`,
  evidenceQuotes: [`[doc excerpt relevant to ${kpi.name}]`],
}));

const eval4: Evaluation = {
  id: "eval-4",
  scorecardId: SC2_ID,
  scorecardVersionId: SC2_VERSION_ID,
  scorecardName: sc2.name,
  domain: sc2.domain,
  name: "API Reference — /v2/payments page",
  evaluatedBy: "user-3",
  evaluatedByName: userName("user-3"),
  inputSummary: "Public API reference page for the /v2/payments endpoint.",
  status: "completed",
  finalWeightedScore: eval4Rollup.finalScore,
  targetScore: sc2.targetScore,
  ragBand: ragBandForScore(eval4Rollup.finalScore),
  submittedAt: "2026-09-16T13:20:00Z",
  kpiResults: eval4Results,
};

export const MOCK_EVALUATIONS: Evaluation[] = [eval1, eval2, eval3, eval4];

// ---------------------------------------------------------------------------
// Chat sessions / messages / drafts
// ---------------------------------------------------------------------------

export const MOCK_CHAT_SESSIONS: ChatSession[] = [
  {
    id: "chat-1",
    userId: MOCK_CURRENT_USER.id,
    title: "Customer Support Email Response Quality",
    status: "completed",
    contextSummary: "Completed — saved as a published scorecard.",
    targetScorecardId: SC1_ID,
    createdAt: "2026-08-04T08:50:00Z",
    lastActivityAt: "2026-08-04T09:12:00Z",
  },
  {
    id: "chat-2",
    userId: MOCK_CURRENT_USER.id,
    title: "Vendor Onboarding Checklist Quality",
    status: "active",
    contextSummary: "Purpose and scope captured. KPIs not yet proposed.",
    targetScorecardId: null,
    createdAt: "2026-09-27T15:10:00Z",
    lastActivityAt: "2026-09-27T15:22:00Z",
  },
  {
    id: "chat-3",
    userId: MOCK_CURRENT_USER.id,
    title: "Support Chat Transcript Quality",
    status: "active",
    contextSummary: "Waiting on: which channels this scorecard should cover.",
    targetScorecardId: null,
    createdAt: "2026-09-26T10:00:00Z",
    lastActivityAt: "2026-09-26T10:18:00Z",
  },
  {
    id: "chat-4",
    userId: "user-3",
    title: "Code Review Quality Gate",
    status: "completed",
    contextSummary: "Completed — saved as a published scorecard.",
    targetScorecardId: SC4_ID,
    createdAt: "2026-06-10T08:40:00Z",
    lastActivityAt: "2026-06-10T09:00:00Z",
  },
];

export const MOCK_CHAT_MESSAGES: Record<string, ChatMessage[]> = {
  "chat-1": [
    { id: "m-1-1", sessionId: "chat-1", role: "user", content: "I need a scorecard for grading our support team's email replies.", createdAt: "2026-08-04T08:50:00Z" },
    { id: "m-1-2", sessionId: "chat-1", role: "assistant", content: "Got it. A couple of quick questions before I draft this.", createdAt: "2026-08-04T08:50:30Z" },
    {
      id: "m-1-3",
      sessionId: "chat-1",
      role: "assistant",
      content: "What should this scorecard mainly optimize for?",
      createdAt: "2026-08-04T08:50:45Z",
      clarifyingQuestion: {
        id: "cq-1-3",
        question: "What should this scorecard mainly optimize for?",
        options: [
          { id: "opt-accuracy-tone", label: "Accuracy + tone" },
          { id: "opt-speed", label: "Speed of resolution" },
          { id: "opt-policy", label: "Policy compliance" },
        ],
        allowOther: true,
        missingFields: ["purposeStatement"],
      },
    },
    { id: "m-1-4", sessionId: "chat-1", role: "user", content: "Accuracy + tone", createdAt: "2026-08-04T08:51:10Z" },
    { id: "m-1-5", sessionId: "chat-1", role: "assistant", content: "Great — I've drafted 6 KPIs covering accuracy, tone, completeness, timeliness, clarity, and compliance, each weighted and with 11-level guidelines. Saved as a published scorecard.", createdAt: "2026-08-04T09:12:00Z" },
  ],
  "chat-2": [
    { id: "m-2-1", sessionId: "chat-2", role: "user", content: "We need a quality scorecard for how well new vendors are onboarded.", createdAt: "2026-09-27T15:10:00Z" },
    { id: "m-2-2", sessionId: "chat-2", role: "assistant", content: "Sounds good. What's the primary purpose — compliance checking, or overall onboarding experience?", createdAt: "2026-09-27T15:10:20Z" },
    { id: "m-2-3", sessionId: "chat-2", role: "user", content: "Mostly making sure required compliance documents are collected on time, but also that the vendor contact felt supported.", createdAt: "2026-09-27T15:21:00Z" },
    { id: "m-2-4", sessionId: "chat-2", role: "assistant", content: "Captured. I've set the purpose and scope — next I'll propose KPIs covering document completeness, timeliness, and vendor experience.", createdAt: "2026-09-27T15:22:00Z" },
  ],
  "chat-3": [
    { id: "m-3-1", sessionId: "chat-3", role: "user", content: "Build me a scorecard for rating support chat transcripts.", createdAt: "2026-09-26T10:00:00Z" },
    {
      id: "m-3-2",
      sessionId: "chat-3",
      role: "assistant",
      content: "Which channels should this cover?",
      createdAt: "2026-09-26T10:18:00Z",
      clarifyingQuestion: {
        id: "cq-3-2",
        question: "Which channels should this scorecard cover?",
        options: [
          { id: "opt-live-chat-only", label: "Live chat only" },
          { id: "opt-chat-and-inapp", label: "Live chat + in-app messaging" },
          { id: "opt-all-async", label: "All async channels (chat, in-app, SMS)" },
        ],
        allowOther: true,
        missingFields: ["scope"],
      },
    },
  ],
  "chat-4": [
    { id: "m-4-1", sessionId: "chat-4", role: "user", content: "Quick 2-KPI gate for pre-merge PR checks — correctness and readability, that's it.", createdAt: "2026-06-10T08:40:00Z" },
    { id: "m-4-2", sessionId: "chat-4", role: "assistant", content: "Understood — keeping it to exactly those two KPIs, weighted 60/40 toward correctness. Saved as a published scorecard.", createdAt: "2026-06-10T09:00:00Z" },
  ],
};

export const MOCK_SCORECARD_DRAFTS: Record<string, ScorecardDraft> = {
  "chat-2": {
    sessionId: "chat-2",
    name: "Vendor Onboarding Checklist Quality",
    domain: "Procurement",
    purposeStatement: "Make sure required compliance documents are collected on time and the vendor felt supported during onboarding.",
    scope: "Applies to new third-party vendor onboarding cases handled by procurement ops.",
    targetScore: null,
    kpis: [],
    scoringFormula: null,
  },
  "chat-3": {
    sessionId: "chat-3",
    name: "Support Chat Transcript Quality",
    domain: "Customer Support",
    purposeStatement: "Rate the quality of a full support chat transcript from open to close.",
    scope: null,
    targetScore: null,
    kpis: [],
    scoringFormula: null,
  },
};

// ---------------------------------------------------------------------------
// Lookups
// ---------------------------------------------------------------------------

export function getScorecardById(id: string): Scorecard | undefined {
  return MOCK_SCORECARDS.find((s) => s.id === id);
}

export function getScorecardVersionById(id: string): ScorecardVersion | undefined {
  return MOCK_SCORECARD_VERSIONS.find((v) => v.id === id);
}

export function getEvaluationsForScorecard(scorecardId: string): Evaluation[] {
  return MOCK_EVALUATIONS.filter((e) => e.scorecardId === scorecardId);
}
