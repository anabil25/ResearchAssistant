import {
  computeExpectedOutcome,
  computeRuntimeCoverage,
  extractTokensFromTitle,
  hasWellFormedAttemptHistory,
  outcomeIsInternallyConsistent,
  passingProjectsForSpec,
  PLAYWRIGHT_ID_PATTERN,
  PLAYWRIGHT_STATE_TOKEN_PATTERN,
  resolveTestResults,
  specPassed,
  validateReportSchema,
  VIEWPORT_PROJECT_NAMES,
  type PlaywrightJsonReport,
  type PlaywrightJsonSpec,
  type PlaywrightJsonTest,
} from "./runtime-coverage-verifier";
import { REQUIRED_PLAYWRIGHT_PROJECT_NAMES } from "./playwright-projects";

describe("resolveTestResults", () => {
  it("returns the test entry's own results array when one is present", () => {
    const results = [{ status: "passed" }];
    expect(
      resolveTestResults({
        expectedStatus: "passed",
        status: "expected",
        results,
      }),
    ).toBe(results);
  });

  it("falls back to an empty array when results is missing entirely (no attempts recorded)", () => {
    expect(
      resolveTestResults({ expectedStatus: "passed", status: "expected" }),
    ).toEqual([]);
  });
});

describe("extractTokensFromTitle", () => {
  it("extracts a bare id token", () => {
    expect(extractTokensFromTitle("does a thing [pw.some-id]")).toEqual({
      bareIds: ["pw.some-id"],
      statePairs: [],
    });
  });

  it("extracts a state token", () => {
    expect(
      extractTokensFromTitle("does a thing [pw.some.interaction:ready]"),
    ).toEqual({
      bareIds: [],
      statePairs: ["some.interaction::ready"],
    });
  });

  it("extracts multiple tokens of both kinds from one title", () => {
    expect(
      extractTokensFromTitle(
        "[pw.group-a] [pw.group-b] covers two states [pw.thing:loading][pw.thing:error]",
      ),
    ).toEqual({
      bareIds: ["pw.group-a", "pw.group-b"],
      statePairs: ["thing::loading", "thing::error"],
    });
  });

  it("returns empty arrays for a title with no tokens", () => {
    expect(extractTokensFromTitle("a plain test title")).toEqual({
      bareIds: [],
      statePairs: [],
    });
  });
});

describe("computeExpectedOutcome", () => {
  it("returns 'skipped' for zero attempts, regardless of expectedStatus", () => {
    expect(computeExpectedOutcome("passed", [])).toBe("skipped");
    expect(computeExpectedOutcome(undefined, [])).toBe("skipped");
  });

  it("returns 'skipped' for a single genuinely-skipped attempt matching expectedStatus 'skipped'", () => {
    expect(computeExpectedOutcome("skipped", [{ status: "skipped" }])).toBe(
      "skipped",
    );
  });

  it("returns 'expected' for a single attempt whose status matches expectedStatus", () => {
    expect(computeExpectedOutcome("passed", [{ status: "passed" }])).toBe(
      "expected",
    );
  });

  it("returns 'unexpected' for a single attempt whose status does not match expectedStatus", () => {
    expect(computeExpectedOutcome("passed", [{ status: "failed" }])).toBe(
      "unexpected",
    );
  });

  it("returns 'unexpected' when every attempt (including exhausted retries) fails to match expectedStatus", () => {
    expect(
      computeExpectedOutcome("passed", [
        { status: "failed" },
        { status: "failed" },
      ]),
    ).toBe("unexpected");
  });

  it("returns 'flaky' for a genuine fail-then-pass retry history", () => {
    expect(
      computeExpectedOutcome("passed", [
        { status: "failed" },
        { status: "passed" },
      ]),
    ).toBe("flaky");
  });

  it("never returns 'flaky' for a single already-passing attempt, even with expectedStatus mismatched to something else", () => {
    // A single result can only ever produce "skipped", "expected", or
    // "unexpected" under the real algorithm -- "flaky" structurally
    // requires at least one genuinely unexpected attempt, which is
    // impossible with only one result in the history.
    expect(computeExpectedOutcome("passed", [{ status: "passed" }])).not.toBe(
      "flaky",
    );
    expect(computeExpectedOutcome("failed", [{ status: "passed" }])).not.toBe(
      "flaky",
    );
  });

  it("treats a missing/malformed per-attempt status as never matching expectedStatus", () => {
    expect(
      computeExpectedOutcome("passed", [{ status: undefined }]),
    ).toBe("unexpected");
    expect(
      computeExpectedOutcome("passed", [{ status: "bogus-status" }]),
    ).toBe("unexpected");
  });

  it("ignores 'interrupted' attempts entirely -- they contribute to neither the expected nor unexpected count, so a run with only an interrupted attempt is indistinguishable from having no attempts at all", () => {
    expect(computeExpectedOutcome("passed", [{ status: "interrupted" }])).toBe(
      "skipped",
    );
  });

  it("ignoring an 'interrupted' attempt lets a later genuine attempt in the same history determine the outcome on its own", () => {
    expect(
      computeExpectedOutcome("passed", [
        { status: "interrupted" },
        { status: "passed" },
      ]),
    ).toBe("expected");
  });

  it("treats a 'skipped' attempt as a no-op ('did not run') rather than 'unexpected' when expectedStatus is something other than 'skipped', letting a later genuine attempt determine the outcome", () => {
    expect(
      computeExpectedOutcome("passed", [
        { status: "skipped" },
        { status: "passed" },
      ]),
    ).toBe("expected");
  });

  it("returns 'skipped' when the only attempt is 'skipped' but expectedStatus does not itself equal 'skipped' -- distinct from the exact-match case above, since here the mismatch is deliberately not counted as unexpected", () => {
    expect(computeExpectedOutcome("passed", [{ status: "skipped" }])).toBe(
      "skipped",
    );
  });
});

describe("outcomeIsInternallyConsistent", () => {
  it("is true for a genuine single-attempt pass", () => {
    const entry: PlaywrightJsonTest = {
      expectedStatus: "passed",
      status: "expected",
      results: [{ status: "passed" }],
    };
    expect(outcomeIsInternallyConsistent(entry)).toBe(true);
  });

  it("is true for a genuine fail-then-pass flaky retry history", () => {
    const entry: PlaywrightJsonTest = {
      expectedStatus: "passed",
      status: "flaky",
      results: [{ status: "failed" }, { status: "passed" }],
    };
    expect(outcomeIsInternallyConsistent(entry)).toBe(true);
  });

  it("is false for a claimed 'flaky' outcome backed by only one, already-passing attempt", () => {
    // The exact adversarial example from review: a single passing attempt
    // can never genuinely produce "flaky" (that requires at least one
    // truly unexpected attempt first); the real outcome for this history
    // is "expected", not "flaky".
    const entry: PlaywrightJsonTest = {
      expectedStatus: "passed",
      status: "flaky",
      results: [{ status: "passed" }],
    };
    expect(outcomeIsInternallyConsistent(entry)).toBe(false);
  });

  it("is false for a claimed 'expected' outcome backed by a genuine fail-then-pass history", () => {
    // The other exact adversarial example from review: a real
    // fail-then-pass retry history always computes to "flaky" under
    // Playwright's own algorithm, never "expected".
    const entry: PlaywrightJsonTest = {
      expectedStatus: "passed",
      status: "expected",
      results: [{ status: "failed" }, { status: "passed" }],
    };
    expect(outcomeIsInternallyConsistent(entry)).toBe(false);
  });

  it("is false for any non-'skipped' claim backed by zero attempts", () => {
    const entry: PlaywrightJsonTest = {
      expectedStatus: "passed",
      status: "expected",
      results: [],
    };
    expect(outcomeIsInternallyConsistent(entry)).toBe(false);
  });

  it("is true for a claimed 'skipped' outcome backed by zero attempts", () => {
    const entry: PlaywrightJsonTest = {
      expectedStatus: "skipped",
      status: "skipped",
      results: [],
    };
    expect(outcomeIsInternallyConsistent(entry)).toBe(true);
  });
});

describe("specPassed", () => {
  it("is true when the only test's only result passed and it was expected to pass", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          projectName: "chromium",
          expectedStatus: "passed",
          status: "expected",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(true);
  });

  it("is false when the spec has no tests", () => {
    const spec: PlaywrightJsonSpec = { title: "x" };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false when the only test has no results", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [{ expectedStatus: "passed", status: "expected" }],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false when the final result is skipped", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "skipped",
          status: "skipped",
          results: [{ status: "skipped" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false when every attempt failed, including retries", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          status: "unexpected",
          results: [{ status: "failed" }, { status: "failed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is true for a flaky test whose final retry passed", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          projectName: "chromium",
          expectedStatus: "passed",
          status: "flaky",
          results: [{ status: "failed" }, { status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(true);
  });

  it("is false for an otherwise-passing test entry that carries no projectName", () => {
    // Evidence that cannot be attributed to a project cannot be counted:
    // per-project accounting is what proves each viewport genuinely covered
    // something, and an unattributable entry would silently inflate the
    // global totals without belonging to any project's column.
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          status: "expected",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is true when any one of several project executions passed", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          projectName: "mobile-chromium",
          expectedStatus: "skipped",
          status: "skipped",
          results: [{ status: "skipped" }],
        },
        {
          projectName: "chromium",
          expectedStatus: "passed",
          status: "expected",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(true);
    expect([...passingProjectsForSpec(spec)]).toEqual(["chromium"]);
  });

  it("is false for a test.fail-marked test that unexpectedly passed (expectedStatus 'failed')", () => {
    // This is the exact loophole reviewer blocker 2 identifies: a
    // test.fail()-marked test that unexpectedly passes has expectedStatus
    // "failed" and outcome "unexpected", even though its final result
    // status is literally "passed". An unexpected pass is not evidence the
    // behavior genuinely works and must never satisfy required coverage.
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "failed",
          status: "unexpected",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false for a normal test that unexpectedly failed, even though expectedStatus is 'passed'", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          status: "unexpected",
          results: [{ status: "failed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false when expectedStatus is missing entirely (untrusted/legacy report shape)", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [{ results: [{ status: "passed" }] }],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false for a bogus/fabricated top-level status even with expectedStatus 'passed' and a passing final result", () => {
    // Regression for the exact gap identified in review: the previous
    // implementation only excluded `status === "unexpected"` rather than
    // requiring one of the genuine passing outcomes, so a hand-crafted or
    // corrupted report entry with an unrecognized `status` string could
    // still slip past as a "genuine pass" whenever expectedStatus/the final
    // result happened to look right. Reproduced `true` under the old logic
    // before this test/fix; must be `false` now.
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          status: "some-bogus-fabricated-status",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false when the top-level status is missing entirely but expectedStatus/results look like a genuine pass", () => {
    // Same gap, different shape: an omitted `status` field (rather than an
    // unrecognized string) must also be rejected, not just an explicit
    // `"unexpected"`.
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false when the top-level status is 'skipped' despite a fabricated passing result (self-contradictory report)", () => {
    // A real Playwright report never actually pairs status: "skipped" with
    // a "passed" result, but a fabricated/corrupted one could -- this must
    // still be rejected rather than trusted because expectedStatus/the
    // final result happen to look right.
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          status: "skipped",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false for a claimed 'flaky' outcome backed by only one, already-passing attempt (adversarial history/status mismatch)", () => {
    // Reproduced this exact gap before fixing it: `PASSING_OUTCOME_STATUSES`
    // alone accepted a bare claim of "flaky" as long as the final result
    // passed, without checking whether "flaky" was even a possible outcome
    // of the attached `results` history. A single passing attempt can never
    // genuinely produce "flaky" (see `computeExpectedOutcome`); the real
    // outcome for this exact history is "expected", not "flaky".
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          status: "flaky",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });

  it("is false for a claimed 'expected' outcome backed by a genuine fail-then-pass retry history (adversarial history/status mismatch)", () => {
    // The mirror-image adversarial case: a real fail-then-pass retry
    // history always computes to "flaky" under Playwright's own algorithm,
    // never "expected" -- a report entry claiming "expected" here is
    // self-contradictory and must not count, even though
    // `PASSING_OUTCOME_STATUSES` alone would have accepted "expected" and
    // the final attempt did genuinely pass.
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "passed",
          status: "expected",
          results: [{ status: "failed" }, { status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(false);
  });
});

describe("VIEWPORT_PROJECT_NAMES", () => {
  it("maps every manifest viewport onto a real configured Playwright project", () => {
    // This mapping is the join between two independently-maintained
    // vocabularies: the manifest's viewport names and the Playwright project
    // names. A drift between them would not fail loudly -- viewport
    // attribution would just silently find no evidence for the renamed
    // viewport and report it as unproven forever. `runtime-coverage-verifier`
    // is deliberately import-free so it cannot reference the project list
    // directly, which is exactly why the agreement needs asserting here.
    expect(Object.keys(VIEWPORT_PROJECT_NAMES).sort()).toEqual([
      "desktop",
      "mobile",
      "tablet",
    ]);
    for (const projectName of Object.values(VIEWPORT_PROJECT_NAMES)) {
      expect(REQUIRED_PLAYWRIGHT_PROJECT_NAMES).toContain(projectName);
    }
    // Every configured project is reachable from some viewport, so no
    // project can execute tests that no viewport scope can ever credit.
    expect(Object.values(VIEWPORT_PROJECT_NAMES).sort()).toEqual(
      [...REQUIRED_PLAYWRIGHT_PROJECT_NAMES].sort(),
    );
  });
});

describe("hasWellFormedAttemptHistory", () => {
  it("rejects each disqualifying condition independently while the entry is otherwise attributable", () => {
    // Every rejection reason in `passingProjectsForSpec` exercised with a
    // `projectName` present, so each one is reached on its own merits rather
    // than short-circuiting on attribution first.
    const cases: Array<[string, PlaywrightJsonTest]> = [
      [
        "expectedStatus is not 'passed' (a test.fail-marked test)",
        {
          projectName: "chromium",
          expectedStatus: "failed",
          status: "unexpected",
          results: [{ status: "passed" }],
        },
      ],
      [
        "outcome is not a passing one",
        {
          projectName: "chromium",
          expectedStatus: "passed",
          status: "unexpected",
          results: [{ status: "failed" }],
        },
      ],
      [
        "outcome field is missing entirely",
        {
          projectName: "chromium",
          expectedStatus: "passed",
          results: [{ status: "passed" }],
        },
      ],
      [
        "attempt history is malformed",
        {
          projectName: "chromium",
          expectedStatus: "passed",
          status: "expected",
          results: [{ status: "not-a-real-status" }],
        },
      ],
      [
        "claimed outcome contradicts its own history",
        {
          projectName: "chromium",
          expectedStatus: "passed",
          status: "flaky",
          results: [{ status: "passed" }],
        },
      ],
      [
        "final attempt did not pass",
        {
          projectName: "chromium",
          expectedStatus: "passed",
          status: "flaky",
          results: [{ status: "passed" }, { status: "failed" }],
        },
      ],
    ];

    for (const [reason, testEntry] of cases) {
      const spec: PlaywrightJsonSpec = { title: "x", tests: [testEntry] };
      expect([reason, [...passingProjectsForSpec(spec)]]).toEqual([reason, []]);
    }
  });

  it("accepts a recognized expectedStatus with a recognized non-empty attempt history", () => {
    expect(
      hasWellFormedAttemptHistory({
        expectedStatus: "passed",
        status: "flaky",
        results: [{ status: "failed" }, { status: "passed" }],
      }),
    ).toBe(true);
  });

  it("rejects an unrecognized expectedStatus", () => {
    // The load-bearing case. `computeExpectedOutcome` classifies every
    // attempt that does not match `expectedStatus` as "unexpected", so an
    // unrecognized expectedStatus makes *every* attempt unexpected. The entry
    // then recomputes to exactly "unexpected", agrees with a claimed
    // `status: "unexpected"`, and -- because "unexpected" is a genuinely
    // executed outcome -- was credited as proof its project ran a test.
    expect(
      hasWellFormedAttemptHistory({
        expectedStatus: "definitely-not-a-status",
        status: "unexpected",
        results: [{ status: "failed" }],
      }),
    ).toBe(false);
  });

  it("rejects a missing expectedStatus", () => {
    expect(
      hasWellFormedAttemptHistory({
        status: "unexpected",
        results: [{ status: "failed" }],
      }),
    ).toBe(false);
  });

  it("rejects an empty attempt history", () => {
    expect(
      hasWellFormedAttemptHistory({
        expectedStatus: "passed",
        status: "expected",
        results: [],
      }),
    ).toBe(false);
  });

  it("rejects a missing attempt history", () => {
    expect(
      hasWellFormedAttemptHistory({
        expectedStatus: "passed",
        status: "expected",
      }),
    ).toBe(false);
  });

  it("rejects an attempt with no status field at all", () => {
    // `results: [{}]` is the minimal fabrication: an object-shaped attempt
    // carrying no outcome. It matches nothing, lands in the unexpected
    // bucket, and manufactures an "unexpected" outcome out of nothing.
    expect(
      hasWellFormedAttemptHistory({
        expectedStatus: "passed",
        status: "unexpected",
        results: [{}],
      }),
    ).toBe(false);
  });

  it("rejects an attempt with an unrecognized status, even alongside valid attempts", () => {
    expect(
      hasWellFormedAttemptHistory({
        expectedStatus: "passed",
        status: "flaky",
        results: [{ status: "failed" }, { status: "invented" }],
      }),
    ).toBe(false);
  });

  it("accepts every status in Playwright's real vocabulary", () => {
    for (const status of [
      "passed",
      "failed",
      "timedOut",
      "skipped",
      "interrupted",
    ]) {
      expect(
        hasWellFormedAttemptHistory({
          expectedStatus: status,
          status: "expected",
          results: [{ status }],
        }),
      ).toBe(true);
    }
  });
});

describe("computeRuntimeCoverage", () => {
  const manifest = [
    {
      id: "alpha",
      states: ["ready", "loading"],
      playwrightTestIds: ["pw.alpha-group"],
    },
    {
      id: "beta",
      states: ["ready"],
      playwrightTestIds: ["pw.beta-group"],
    },
  ];

  it("reports full coverage when every required id/state has a passed execution", () => {
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest);

    expect(result).toEqual({
      interactionCount: 2,
      requiredIdCount: 2,
      passedIdCount: 2,
      requiredStateCount: 3,
      passedStateCount: 3,
      missingIds: [],
      missingStates: [],
      idsPresentButNeverPassed: [],
      statesPresentButNeverPassed: [],
      perProject: [
        { projectName: "chromium", passedIdCount: 2, passedStateCount: 3 },
      ],
      projectsWithoutEvidence: [],
      viewportStatesWithoutProjectEvidence: [],
      requiredViewportStateCount: 3,
      passedViewportStateCount: 3,
      unknownEvidenceProjects: [],
    });
  });

  it("reports a missing id and missing states when nothing in the report covers them", () => {
    const report: PlaywrightJsonReport = { suites: [] };

    const result = computeRuntimeCoverage(report, manifest);

    expect(result.missingIds).toEqual(["pw.alpha-group", "pw.beta-group"]);
    expect(result.missingStates).toEqual([
      "alpha:loading",
      "alpha:ready",
      "beta:ready",
    ]);
    expect(result.idsPresentButNeverPassed).toEqual([]);
    expect(result.statesPresentButNeverPassed).toEqual([]);
  });

  it("distinguishes 'present but never passed' from fully missing", () => {
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              // Present in the report, but every execution was skipped --
              // this is the exact runtime-only loophole the module exists
              // to catch: the static scan would call this trusted, but it
              // never genuinely ran and passed in this run.
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  expectedStatus: "skipped",
                  status: "skipped",
                  results: [{ status: "skipped" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest);

    expect(result.missingIds).toEqual(["pw.alpha-group", "pw.beta-group"]);
    expect(result.missingStates).toEqual([
      "alpha:loading",
      "alpha:ready",
      "beta:ready",
    ]);
    expect(result.idsPresentButNeverPassed).toEqual(["pw.alpha-group"]);
    expect(result.statesPresentButNeverPassed).toEqual([
      "alpha:loading",
      "alpha:ready",
    ]);
  });

  it("recurses into nested describe suites", () => {
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          suites: [
            {
              title: "a describe group",
              specs: [
                {
                  title:
                    "[pw.alpha-group] alpha ready [pw.alpha:ready]",
                  tests: [
                    {
                      projectName: "chromium",
                      expectedStatus: "passed",
                      status: "expected",
                      results: [{ status: "passed" }],
                    },
                  ],
                },
              ],
              suites: [
                {
                  title: "a nested describe group",
                  specs: [
                    {
                      title:
                        "[pw.alpha-group] alpha loading [pw.alpha:loading]",
                      tests: [
                        {
                          projectName: "chromium",
                          expectedStatus: "passed",
                          status: "expected",
                          results: [{ status: "passed" }],
                        },
                      ],
                    },
                    {
                      title: "[pw.beta-group] beta ready [pw.beta:ready]",
                      tests: [
                        {
                          projectName: "mobile-chromium",
                          expectedStatus: "passed",
                          status: "expected",
                          results: [{ status: "passed" }],
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest);

    expect(result).toEqual({
      interactionCount: 2,
      requiredIdCount: 2,
      passedIdCount: 2,
      requiredStateCount: 3,
      passedStateCount: 3,
      missingIds: [],
      missingStates: [],
      idsPresentButNeverPassed: [],
      statesPresentButNeverPassed: [],
      // Attribution survives the recursion: chromium proved the two alpha
      // states, mobile-chromium proved the single beta state. A global-only
      // count would report "3/3 states" and hide that split entirely.
      perProject: [
        { projectName: "chromium", passedIdCount: 1, passedStateCount: 2 },
        {
          projectName: "mobile-chromium",
          passedIdCount: 1,
          passedStateCount: 1,
        },
      ],
      projectsWithoutEvidence: [],
      // The fixture manifest declares no viewports, so every state is
      // desktop-scoped: three triples, and the beta state is proven only by
      // mobile-chromium, so it has no desktop-attributable evidence.
      viewportStatesWithoutProjectEvidence: ["beta:ready@desktop"],
      requiredViewportStateCount: 3,
      passedViewportStateCount: 2,
      unknownEvidenceProjects: [],
    });
  });

  it("handles a report with no suites at all", () => {
    const result = computeRuntimeCoverage({}, manifest);
    expect(result.missingIds).toEqual(["pw.alpha-group", "pw.beta-group"]);
  });

  it("treats a non-array `tests` field as no tests instead of throwing", () => {
    // The report is untrusted input. `spec.tests ?? []` only guards
    // null/undefined, so a string/object/number fell through to `for...of`
    // and threw -- fail-closed by accident. `Array.isArray` states it.
    const spec = { title: "[pw.beta-group] beta ready [pw.beta:ready]", tests: "nope" };
    expect(() =>
      passingProjectsForSpec(spec as unknown as PlaywrightJsonSpec),
    ).not.toThrow();
    expect([
      ...passingProjectsForSpec(spec as unknown as PlaywrightJsonSpec),
    ]).toEqual([]);
  });

  it("refuses evidence from a project name outside the required project list", () => {
    // A fabricated or mis-merged report could invent a projectName, or carry
    // one in from an unrelated Playwright configuration. Without an
    // allowlist its tokens counted, and `projectsWithoutEvidence` still
    // looked clean because that check runs over the required list rather
    // than over the set that actually produced the evidence.
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  projectName: "some-other-config-project",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest, ["chromium"]);

    expect(result.passedStateCount).toBe(0);
    expect(result.perProject).toEqual([]);
    expect(result.projectsWithoutEvidence).toEqual(["chromium"]);
  });

  it("requires per-viewport evidence for an interaction that declares multiple viewports", () => {
    // Viewport scope in action: a viewport-sensitive interaction needs one
    // triple per declared viewport, so desktop evidence alone no longer
    // satisfies it and the required denominator grows accordingly.
    const scopedManifest = [
      {
        id: "alpha",
        states: ["ready"],
        playwrightTestIds: ["pw.alpha-group"],
        viewports: ["desktop", "tablet", "mobile"],
      },
      {
        id: "beta",
        states: ["ready"],
        playwrightTestIds: ["pw.beta-group"],
        viewports: ["desktop"],
      },
    ];
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title: "[pw.alpha-group] alpha ready [pw.alpha:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
                {
                  projectName: "tablet-chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, scopedManifest, [
      "chromium",
      "tablet-chromium",
    ]);

    // Flat view is complete -- which is exactly why it cannot be the whole
    // story.
    expect(result.missingStates).toEqual([]);
    expect(result.passedStateCount).toBe(2);
    // Viewport-scoped view: alpha needs three triples and has two.
    expect(result.requiredViewportStateCount).toBe(4);
    expect(result.passedViewportStateCount).toBe(3);
    expect(result.viewportStatesWithoutProjectEvidence).toEqual([
      "alpha:ready@mobile",
    ]);
  });

  it("treats an interaction with no declared viewports as desktop-scoped", () => {
    const result = computeRuntimeCoverage(
      {
        suites: [
          {
            title: "a.spec.ts",
            specs: [
              {
                title: "[pw.beta-group] beta ready [pw.beta:ready]",
                tests: [
                  {
                    projectName: "chromium",
                    expectedStatus: "passed",
                    status: "expected",
                    results: [{ status: "passed" }],
                  },
                ],
              },
            ],
          },
        ],
      },
      [
        {
          id: "beta",
          states: ["ready"],
          playwrightTestIds: ["pw.beta-group"],
          viewports: [],
        },
      ],
      ["chromium"],
    );

    expect(result.requiredViewportStateCount).toBe(1);
    expect(result.passedViewportStateCount).toBe(1);
    expect(result.viewportStatesWithoutProjectEvidence).toEqual([]);
  });

  it("does not credit a project whose only 'execution' is a malformed attempt history", () => {
    // End-to-end form of the fabrication `hasWellFormedAttemptHistory`
    // blocks. Every entry here claims a genuinely-executed outcome that its
    // own (garbage) history recomputes to exactly, so the internal-consistency
    // check alone waves all of them through. None of them is evidence of
    // anything.
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "unexpected",
                  results: [{}],
                },
              ],
            },
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "not-a-real-expected-status",
                  status: "unexpected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest, ["chromium"]);

    expect(result.passedIdCount).toBe(0);
    expect(result.passedStateCount).toBe(0);
    expect(result.perProject).toEqual([]);
    expect(result.projectsWithoutEvidence).toEqual(["chromium"]);
  });

  it("flags a required project that executed tests but proved no coverage token", () => {
    // The per-viewport gap: chromium covers the whole manifest, so every
    // global count reads as complete, while mobile-chromium contributes
    // nothing at all. Attributing evidence per project is the only way that
    // shows up -- a union of tokens cannot distinguish "all three projects
    // proved this" from "chromium proved everything".
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
                {
                  projectName: "mobile-chromium",
                  expectedStatus: "passed",
                  status: "unexpected",
                  results: [{ status: "failed" }],
                },
              ],
            },
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest, [
      "chromium",
      "mobile-chromium",
    ]);

    // Globally indistinguishable from a healthy run:
    expect(result.missingIds).toEqual([]);
    expect(result.missingStates).toEqual([]);
    expect(result.passedIdCount).toBe(2);
    expect(result.passedStateCount).toBe(3);
    // Per project, the empty viewport is exposed:
    expect(result.perProject).toEqual([
      { projectName: "chromium", passedIdCount: 2, passedStateCount: 3 },
    ]);
    expect(result.projectsWithoutEvidence).toEqual(["mobile-chromium"]);
  });

  it("reports no missing projects when every required project proves at least one token", () => {
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
                {
                  projectName: "mobile-chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest, [
      "chromium",
      "mobile-chromium",
    ]);

    expect(result.projectsWithoutEvidence).toEqual([]);
    expect(result.perProject).toEqual([
      { projectName: "chromium", passedIdCount: 2, passedStateCount: 3 },
      { projectName: "mobile-chromium", passedIdCount: 1, passedStateCount: 1 },
    ]);
  });

  it("ignores unattributed evidence when computing per-project coverage but still reports it globally as missing", () => {
    // An entry with no projectName cannot be filed under any project, so it
    // proves nothing for anyone -- including globally.
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest, ["chromium"]);

    expect(result.passedStateCount).toBe(0);
    expect(result.statesPresentButNeverPassed).toEqual(["beta:ready"]);
    expect(result.projectsWithoutEvidence).toEqual(["chromium"]);
  });

  it("flags a project that passed only specs carrying tokens outside the manifest", () => {
    // A project can genuinely pass tests and still prove nothing *required*:
    // its evidence entry exists, but every token it proved is an orphan the
    // manifest never declared, so both of its counts are zero. Distinct from
    // the "no evidence entry at all" case above, and it must fail the gate
    // just the same.
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
            {
              title: "[pw.gamma-group] gamma something [pw.gamma:undeclared]",
              tests: [
                {
                  projectName: "mobile-chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest, [
      "chromium",
      "mobile-chromium",
    ]);

    expect(result.missingStates).toEqual([]);
    expect(result.perProject).toEqual([
      { projectName: "chromium", passedIdCount: 2, passedStateCount: 3 },
      { projectName: "mobile-chromium", passedIdCount: 0, passedStateCount: 0 },
    ]);
    expect(result.projectsWithoutEvidence).toEqual(["mobile-chromium"]);
  });

  it("does not count a test.fail-marked test's unexpected pass toward required coverage", () => {
    // End-to-end proof of blocker 2's fix at the computeRuntimeCoverage
    // level: a token that appears in the report with an "unexpected pass"
    // (expectedStatus "failed", actual result "passed") must still be
    // reported as missing/never-passed, not as covered.
    const report: PlaywrightJsonReport = {
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title:
                "[pw.alpha-group] alpha ready and loading [pw.alpha:ready][pw.alpha:loading]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "failed",
                  status: "unexpected",
                  results: [{ status: "passed" }],
                },
              ],
            },
            {
              title: "[pw.beta-group] beta ready [pw.beta:ready]",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = computeRuntimeCoverage(report, manifest);

    expect(result.missingIds).toEqual(["pw.alpha-group"]);
    expect(result.missingStates).toEqual(["alpha:loading", "alpha:ready"]);
    expect(result.idsPresentButNeverPassed).toEqual(["pw.alpha-group"]);
    expect(result.statesPresentButNeverPassed).toEqual([
      "alpha:loading",
      "alpha:ready",
    ]);
  });

  it("tolerates a spec with a missing title, treating it as contributing no tokens", () => {
    // Playwright's JSON report always includes a title in practice, but the
    // schema is untrusted external input at runtime; a malformed/partial
    // report entry must not throw, and must simply contribute zero tokens.
    const malformedSpec = {
      tests: [{ results: [{ status: "passed" }] }],
    } as unknown as PlaywrightJsonSpec;
    const report: PlaywrightJsonReport = {
      suites: [{ title: "a.spec.ts", specs: [malformedSpec] }],
    };

    const result = computeRuntimeCoverage(report, manifest);

    expect(result.missingIds).toEqual(["pw.alpha-group", "pw.beta-group"]);
    expect(result.missingStates).toEqual([
      "alpha:loading",
      "alpha:ready",
      "beta:ready",
    ]);
  });
});

describe("validateReportSchema", () => {
  const requiredProjects = ["chromium", "tablet-chromium", "mobile-chromium"];

  function validReport(): PlaywrightJsonReport {
    return {
      config: {
        projects: [
          { name: "chromium" },
          { name: "tablet-chromium" },
          { name: "mobile-chromium" },
        ],
      },
      stats: { expected: 3, unexpected: 0, flaky: 0, skipped: 0 },
      errors: [],
      suites: [
        {
          title: "a.spec.ts",
          specs: [
            {
              title: "does something",
              tests: [
                {
                  projectName: "chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
                {
                  projectName: "tablet-chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
                {
                  projectName: "mobile-chromium",
                  expectedStatus: "passed",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
              ],
            },
          ],
        },
      ],
    };
  }

  it("returns no problems for a structurally complete report covering every required project", () => {
    expect(validateReportSchema(validReport(), requiredProjects)).toEqual([]);
  });

  it("fails closed with a single problem when the report is not a JSON object at all", () => {
    // Guards against a fabricated/corrupted report.json whose top level is
    // not even an object (e.g. `null`, a bare array, or a primitive).
    expect(validateReportSchema(null as never, requiredProjects)).toEqual([
      "Report is not a JSON object.",
    ]);
  });

  it("flags a report missing the stats block", () => {
    const report = validReport();
    delete report.stats;
    const problems = validateReportSchema(report, requiredProjects);
    expect(problems.some((p) => p.includes("stats"))).toBe(true);
  });

  it("flags a report with a stats block missing a required numeric field", () => {
    const report = validReport();
    report.stats = { expected: 3, unexpected: 0, flaky: 0 } as never;
    const problems = validateReportSchema(report, requiredProjects);
    expect(problems.some((p) => p.includes("stats"))).toBe(true);
  });

  it("flags a report with no suites at all", () => {
    const report = validReport();
    report.suites = [];
    const problems = validateReportSchema(report, requiredProjects);
    expect(problems.some((p) => p.includes("no top-level suites"))).toBe(
      true,
    );
  });

  it("flags a report missing the top-level errors array", () => {
    const report = validReport();
    delete report.errors;
    const problems = validateReportSchema(report, requiredProjects);
    expect(problems.some((p) => p.includes("errors"))).toBe(true);
  });

  it("flags a report with non-empty global errors", () => {
    const report = validReport();
    report.errors = [{ message: "global setup crashed" }];
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some((p) => p.includes("global setup/teardown error")),
    ).toBe(true);
  });

  it("flags a report whose config.projects omits a required project", () => {
    const report = validReport();
    report.config = { projects: [{ name: "chromium" }] };
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some((p) => p.includes('"tablet-chromium"') && p.includes("config.projects")),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"mobile-chromium"') && p.includes("config.projects")),
    ).toBe(true);
  });

  it("treats a missing config as declaring zero projects (still flags all required projects as absent from config)", () => {
    const report = validReport();
    delete report.config;
    const problems = validateReportSchema(report, requiredProjects);
    for (const required of requiredProjects) {
      expect(
        problems.some(
          (p) => p.includes(`"${required}"`) && p.includes("config.projects"),
        ),
      ).toBe(true);
    }
  });

  it("ignores a config.projects entry with no name without throwing", () => {
    const report = validReport();
    report.config = { projects: [{}, { name: "chromium" }] } as never;
    const problems = validateReportSchema(report, requiredProjects);
    // "chromium" still has a named entry, so it must not be flagged as
    // absent from config.projects; the unnamed entry is simply ignored.
    expect(
      problems.some((p) => p.includes('"chromium"') && p.includes("config.projects")),
    ).toBe(false);
  });

  it("ignores a test entry with no projectName when collecting executed projects", () => {
    const report = validReport();
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [
          {
            title: "does something anonymous",
            tests: [{ results: [{ status: "passed" }] }],
          },
          {
            title: "does something",
            tests: [
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
              {
                projectName: "tablet-chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
              {
                projectName: "mobile-chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
            ],
          },
        ],
      },
    ];
    expect(validateReportSchema(report, requiredProjects)).toEqual([]);
  });

  it("rejects a required project whose every test entry was skipped, even though the project name is present (skipped-only project completeness)", () => {
    // Real gap fix: previously any test entry with a matching projectName
    // counted as "executed" regardless of outcome, so a tablet/mobile
    // project whose entire suite was runtime-skipped (e.g. an env-gated
    // `test.skip` applied project-wide) would incorrectly satisfy
    // project-completeness -- a stale/partial report could exploit this to
    // look like every project genuinely ran when tablet/mobile never did.
    const report = validReport();
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [
          {
            title: "does something",
            tests: [
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
              {
                projectName: "tablet-chromium",
                status: "skipped",
                results: [],
              },
              {
                projectName: "mobile-chromium",
                status: "skipped",
                results: [],
              },
            ],
          },
        ],
      },
    ];
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some((p) => p.includes('"tablet-chromium"') && p.includes("actually executed")),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"mobile-chromium"') && p.includes("actually executed")),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"chromium"') && p.includes("actually executed")),
    ).toBe(false);
  });

  it("rejects a required project whose test entries all have a missing or malformed status (adversarial/fabricated report), rather than crediting them as executed", () => {
    // Reviewer-identified gap: `!== "skipped"` alone treats *anything* that
    // isn't literally the string "skipped" as evidence of genuine
    // execution -- including `undefined` (a missing field), an empty
    // string, or a garbage/unrecognized value. A hand-crafted or corrupted
    // report.json could exploit exactly this to satisfy project
    // completeness for a project that never actually ran a single genuine
    // test. Only the known real outcome values
    // ("expected"/"unexpected"/"flaky") may count.
    const report = validReport();
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [
          {
            title: "does something",
            tests: [
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
              // Missing `status` entirely.
              {
                projectName: "tablet-chromium",
                results: [{ status: "passed" }],
              } as never,
              // Malformed/unrecognized `status` value.
              {
                projectName: "mobile-chromium",
                status: "bogus-status",
                results: [{ status: "passed" }],
              } as never,
            ],
          },
        ],
      },
    ];
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some((p) => p.includes('"tablet-chromium"') && p.includes("actually executed")),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"mobile-chromium"') && p.includes("actually executed")),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"chromium"') && p.includes("actually executed")),
    ).toBe(false);
  });

  it("treats a spec with no tests array as contributing zero executed projects", () => {
    const report = validReport();
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [{ title: "not yet run" } as never],
      },
    ];
    // No project actually executed a test here, so every required project
    // must be reported as missing from the executed set.
    const problems = validateReportSchema(report, requiredProjects);
    for (const required of requiredProjects) {
      expect(problems.some((p) => p.includes(`"${required}"`) && p.includes("actually executed"))).toBe(
        true,
      );
    }
  });

  it("treats a report with no suites at all as contributing zero executed projects", () => {
    const report = validReport();
    delete (report as { suites?: unknown }).suites;
    const problems = validateReportSchema(report, requiredProjects);
    expect(problems.some((p) => p.includes("no top-level suites"))).toBe(true);
    for (const required of requiredProjects) {
      expect(
        problems.some((p) => p.includes(`"${required}"`) && p.includes("actually executed")),
      ).toBe(true);
    }
  });

  it("falls back to a placeholder message for a global error entry with no message", () => {
    const report = validReport();
    report.errors = [{}];
    const problems = validateReportSchema(report, requiredProjects);
    expect(problems.some((p) => p.includes("(no message)"))).toBe(true);
  });

  it("flags a Chromium-only run even when config.projects declares all three (partial-run detection)", () => {
    // The exact loophole blocker 3 identifies: a report can *declare* all
    // three configured projects while only Chromium actually executed any
    // test (e.g. a run scoped to a single project). config completeness
    // alone is not enough; execution completeness must also be checked.
    const report = validReport();
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [
          {
            title: "does something",
            tests: [
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
            ],
          },
        ],
      },
    ];
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some(
        (p) =>
          p.includes('"tablet-chromium"') && p.includes("actually executed"),
      ),
    ).toBe(true);
    expect(
      problems.some(
        (p) => p.includes('"mobile-chromium"') && p.includes("actually executed"),
      ),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"chromium"') && p.includes("actually executed")),
    ).toBe(false);
  });

  it("finds executed project names nested arbitrarily deep across describe suites", () => {
    const report = validReport();
    report.suites = [
      {
        title: "a.spec.ts",
        suites: [
          {
            title: "outer",
            suites: [
              {
                title: "inner",
                specs: [
                  {
                    title: "does something",
                    tests: [
                      {
                        projectName: "chromium",
                        expectedStatus: "passed",
                        status: "expected",
                        results: [{ status: "passed" }],
                      },
                      {
                        projectName: "tablet-chromium",
                        expectedStatus: "passed",
                        status: "expected",
                        results: [{ status: "passed" }],
                      },
                      {
                        projectName: "mobile-chromium",
                        expectedStatus: "passed",
                        status: "expected",
                        results: [{ status: "passed" }],
                      },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      },
    ];
    expect(validateReportSchema(report, requiredProjects)).toEqual([]);
  });

  it("rejects a required project whose only test entries claim a genuine outcome despite zero recorded attempts (fabricated 'project executed with no attempts' report)", () => {
    // Reviewer-identified gap: a test entry with a matching projectName and
    // a recognized top-level status ("expected"/"unexpected"/"flaky") used
    // to count as "genuinely executed" regardless of whether its `results`
    // array actually contained any attempts at all. A hand-crafted entry
    // claiming `status: "expected"` with `results: []` -- which can only
    // ever genuinely recompute to "skipped" -- must not satisfy project
    // completeness.
    const report = validReport();
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [
          {
            title: "does something",
            tests: [
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
              {
                projectName: "tablet-chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [],
              },
              {
                projectName: "mobile-chromium",
                expectedStatus: "passed",
                status: "flaky",
                results: [{ status: "passed" }],
              },
            ],
          },
        ],
      },
    ];
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some((p) => p.includes('"tablet-chromium"') && p.includes("actually executed")),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"mobile-chromium"') && p.includes("actually executed")),
    ).toBe(true);
    expect(
      problems.some((p) => p.includes('"chromium"') && p.includes("actually executed")),
    ).toBe(false);
  });

  it("flags a report whose top-level stats block does not match an independent recount of every test entry's status", () => {
    // Reviewer-identified gap: the stats block's presence/numeric-ness was
    // checked, but its actual values were never cross-checked against the
    // report's own suite tree -- a stale report spliced with new suites, or
    // one with a hand-edited stats block, could disagree with itself and
    // still pass.
    const report = validReport();
    report.stats = { expected: 3, unexpected: 0, flaky: 0, skipped: 0 };
    // Only 2 of the 3 declared "expected" test entries actually exist in
    // this (deliberately truncated) suite tree.
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [
          {
            title: "does something",
            tests: [
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
              {
                projectName: "tablet-chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
            ],
          },
        ],
      },
    ];
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some(
        (p) =>
          p.includes("top-level `stats` block") &&
          p.includes("expected: header 3 vs recounted 2"),
      ),
    ).toBe(true);
  });

  it("does not flag a stats mismatch when the recount genuinely matches (no false positive on a real, internally consistent report)", () => {
    expect(validateReportSchema(validReport(), requiredProjects)).toEqual([]);
  });

  it("flags a header/recount disagreement specifically on the 'unexpected' bucket, and correctly tallies a genuinely 'unexpected' test entry in the recount", () => {
    const report = validReport();
    report.stats = { expected: 1, unexpected: 5, flaky: 0, skipped: 0 };
    report.suites = [
      {
        title: "a.spec.ts",
        specs: [
          {
            title: "does something",
            tests: [
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed" }],
              },
              // Only one genuinely "unexpected" test entry actually exists
              // in this suite tree, disagreeing with the header's claimed 5.
              {
                projectName: "chromium",
                expectedStatus: "passed",
                status: "unexpected",
                results: [{ status: "failed" }],
              },
            ],
          },
        ],
      },
    ];
    const problems = validateReportSchema(report, requiredProjects);
    expect(
      problems.some(
        (p) =>
          p.includes("top-level `stats` block") &&
          p.includes("unexpected: header 5 vs recounted 1"),
      ),
    ).toBe(true);
  });
});

describe("exported token regex patterns", () => {
  it("PLAYWRIGHT_ID_PATTERN matches a bare token and agrees with extractTokensFromTitle", () => {
    const matches = [
      ..."[pw.some-id] does a thing".matchAll(PLAYWRIGHT_ID_PATTERN),
    ];
    expect(matches).toHaveLength(1);
    expect(matches[0][1]).toBe("pw.some-id");
  });

  it("PLAYWRIGHT_STATE_TOKEN_PATTERN matches a state token and agrees with extractTokensFromTitle", () => {
    const matches = [
      ..."[pw.some.interaction:ready] is ready".matchAll(
        PLAYWRIGHT_STATE_TOKEN_PATTERN,
      ),
    ];
    expect(matches).toHaveLength(1);
    expect(matches[0][1]).toBe("some.interaction");
    expect(matches[0][2]).toBe("ready");
  });
});
