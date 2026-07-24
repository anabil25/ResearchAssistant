import {
  computeRuntimeCoverage,
  extractTokensFromTitle,
  PLAYWRIGHT_ID_PATTERN,
  PLAYWRIGHT_STATE_TOKEN_PATTERN,
  specPassed,
  validateReportSchema,
  type PlaywrightJsonReport,
  type PlaywrightJsonSpec,
} from "./runtime-coverage-verifier";

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

describe("specPassed", () => {
  it("is true when the only test's only result passed and it was expected to pass", () => {
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
          expectedStatus: "passed",
          status: "flaky",
          results: [{ status: "failed" }, { status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(true);
  });

  it("is true when any one of several project executions passed", () => {
    const spec: PlaywrightJsonSpec = {
      title: "x",
      tests: [
        {
          expectedStatus: "skipped",
          status: "skipped",
          results: [{ status: "skipped" }],
        },
        {
          expectedStatus: "passed",
          status: "expected",
          results: [{ status: "passed" }],
        },
      ],
    };
    expect(specPassed(spec)).toBe(true);
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
    });
  });

  it("handles a report with no suites at all", () => {
    const result = computeRuntimeCoverage({}, manifest);
    expect(result.missingIds).toEqual(["pw.alpha-group", "pw.beta-group"]);
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
                  status: "expected",
                  results: [{ status: "passed" }],
                },
                {
                  projectName: "tablet-chromium",
                  status: "expected",
                  results: [{ status: "passed" }],
                },
                {
                  projectName: "mobile-chromium",
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
                status: "expected",
                results: [{ status: "passed" }],
              },
              {
                projectName: "tablet-chromium",
                status: "expected",
                results: [{ status: "passed" }],
              },
              {
                projectName: "mobile-chromium",
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
                        status: "expected",
                        results: [{ status: "passed" }],
                      },
                      {
                        projectName: "tablet-chromium",
                        status: "expected",
                        results: [{ status: "passed" }],
                      },
                      {
                        projectName: "mobile-chromium",
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
