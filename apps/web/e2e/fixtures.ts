import {
  expect,
  test as base,
  type ConsoleMessage,
  type Page,
  type Request,
  type Response,
} from "@playwright/test";

type ReleaseDiagnostics = {
  releaseDiagnostics: {
    expectConsoleError(pattern: RegExp): void;
    expectRequestFailure(pattern: RegExp): void;
  };
};

const WORKSPACE_BOOTSTRAP_ENDPOINTS = [
  "workspace",
  "library",
  "runs",
  "approvals",
  "connectors",
  "settings",
  "agents",
  "workflows",
] as const;

type PatternMatcherQueue = {
  add(pattern: RegExp): void;
  consume(candidate: string): boolean;
  remaining(): RegExp[];
};

type DiagnosticsTracker = {
  expectedConsoleErrors: PatternMatcherQueue;
  expectedRequestFailures: PatternMatcherQueue;
  unexpectedConsoleErrors: string[];
  unexpectedRequestFailures: string[];
};

function matchesPattern(pattern: RegExp, candidate: string) {
  pattern.lastIndex = 0;
  return pattern.test(candidate);
}

function createPatternMatcherQueue(): PatternMatcherQueue {
  const queuedPatterns: RegExp[] = [];

  return {
    add(pattern) {
      queuedPatterns.push(pattern);
    },
    consume(candidate) {
      const matchedIndex = queuedPatterns.findIndex((pattern) =>
        matchesPattern(pattern, candidate),
      );

      if (matchedIndex < 0) {
        return false;
      }

      queuedPatterns.splice(matchedIndex, 1);
      return true;
    },
    remaining() {
      return queuedPatterns;
    },
  };
}

function isWorkspaceBootstrapResponse(response: Response, endpoint: string) {
  if (response.request().method() !== "GET") {
    return false;
  }

  const { pathname } = new URL(response.url());
  return pathname === `/api/backend/api/${endpoint}`;
}

function waitForWorkspaceBootstrapResponses(page: Page) {
  return WORKSPACE_BOOTSTRAP_ENDPOINTS.map((endpoint) =>
    page.waitForResponse((response) =>
      isWorkspaceBootstrapResponse(response, endpoint),
    ),
  );
}

function verifyWorkspaceBootstrapResponses(responses: Response[]) {
  responses.forEach((response, index) => {
    expect(
      response.status(),
      `GET /api/backend/api/${WORKSPACE_BOOTSTRAP_ENDPOINTS[index]}`,
    ).toBe(200);
  });
}

export async function completeWorkspaceRequests(
  page: Page,
  action: () => Promise<unknown>,
) {
  const pendingBootstrapResponses = waitForWorkspaceBootstrapResponses(page);
  await action();
  verifyWorkspaceBootstrapResponses(
    await Promise.all(pendingBootstrapResponses),
  );
}

function formatRequestFailure(request: Request) {
  const failureText = request.failure()?.errorText ?? "unknown failure";
  return `${request.method()} ${request.url()}: ${failureText}`;
}

function recordConsoleError(
  expectedConsoleErrors: PatternMatcherQueue,
  unexpectedConsoleErrors: string[],
  message: ConsoleMessage,
) {
  if (message.type() !== "error") {
    return;
  }

  const renderedMessage = message.text();
  if (!expectedConsoleErrors.consume(renderedMessage)) {
    unexpectedConsoleErrors.push(renderedMessage);
  }
}

function recordRequestFailure(
  expectedRequestFailures: PatternMatcherQueue,
  unexpectedRequestFailures: string[],
  request: Request,
) {
  const renderedFailure = formatRequestFailure(request);
  if (!expectedRequestFailures.consume(renderedFailure)) {
    unexpectedRequestFailures.push(renderedFailure);
  }
}

function attachReleaseDiagnostics(page: Page, tracker: DiagnosticsTracker) {
  const handleConsoleMessage = (message: ConsoleMessage) => {
    recordConsoleError(
      tracker.expectedConsoleErrors,
      tracker.unexpectedConsoleErrors,
      message,
    );
  };

  const handleRequestFailure = (request: Request) => {
    recordRequestFailure(
      tracker.expectedRequestFailures,
      tracker.unexpectedRequestFailures,
      request,
    );
  };

  page.on("console", handleConsoleMessage);
  page.on("requestfailed", handleRequestFailure);

  return () => {
    page.off("console", handleConsoleMessage);
    page.off("requestfailed", handleRequestFailure);
  };
}

function createReleaseDiagnosticsFixture(
  expectedConsoleErrors: PatternMatcherQueue,
  expectedRequestFailures: PatternMatcherQueue,
): ReleaseDiagnostics["releaseDiagnostics"] {
  return {
    expectConsoleError(pattern) {
      expectedConsoleErrors.add(pattern);
    },
    expectRequestFailure(pattern) {
      expectedRequestFailures.add(pattern);
    },
  };
}

function assertReleaseDiagnosticsSettled(tracker: DiagnosticsTracker) {
  expect(
    tracker.unexpectedConsoleErrors,
    "unexpected browser console errors",
  ).toEqual([]);
  expect(
    tracker.unexpectedRequestFailures,
    "unexpected browser request failures",
  ).toEqual([]);
  expect(
    tracker.expectedConsoleErrors.remaining(),
    "expected browser console errors that did not occur",
  ).toEqual([]);
  expect(
    tracker.expectedRequestFailures.remaining(),
    "expected browser request failures that did not occur",
  ).toEqual([]);
}

export const test = base.extend<ReleaseDiagnostics>({
  releaseDiagnostics: [
    async ({ page }, use, testInfo) => {
      const tracker: DiagnosticsTracker = {
        expectedConsoleErrors: createPatternMatcherQueue(),
        expectedRequestFailures: createPatternMatcherQueue(),
        unexpectedConsoleErrors: [],
        unexpectedRequestFailures: [],
      };

      const detachDiagnostics = attachReleaseDiagnostics(page, tracker);

      await use(
        createReleaseDiagnosticsFixture(
          tracker.expectedConsoleErrors,
          tracker.expectedRequestFailures,
        ),
      );

      detachDiagnostics();

      if (testInfo.status !== testInfo.expectedStatus) {
        return;
      }

      assertReleaseDiagnosticsSettled(tracker);
    },
    { auto: true },
  ],
});

export { expect };
