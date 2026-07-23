import {
  expect,
  test as base,
  type ConsoleMessage,
  type Request,
} from "@playwright/test";

type ReleaseDiagnostics = {
  releaseDiagnostics: {
    expectConsoleError(pattern: RegExp): void;
    expectRequestFailure(pattern: RegExp): void;
  };
};

export const test = base.extend<ReleaseDiagnostics>({
  releaseDiagnostics: [
    async ({ page }, use, testInfo) => {
      const consoleErrors: string[] = [];
      const expectedConsoleErrors: RegExp[] = [];
      const requestFailures: string[] = [];
      const expectedRequestFailures: RegExp[] = [];
      const onConsole = (message: ConsoleMessage) => {
        if (message.type() === "error") {
          const text = message.text();
          const expectedIndex = expectedConsoleErrors.findIndex((pattern) => {
            pattern.lastIndex = 0;
            return pattern.test(text);
          });
          if (expectedIndex >= 0) {
            expectedConsoleErrors.splice(expectedIndex, 1);
          } else {
            consoleErrors.push(text);
          }
        }
      };
      const onRequestFailed = (request: Request) => {
        const text = `${request.method()} ${request.url()}: ${
          request.failure()?.errorText ?? "unknown failure"
        }`;
        const expectedIndex = expectedRequestFailures.findIndex((pattern) => {
          pattern.lastIndex = 0;
          return pattern.test(text);
        });
        if (expectedIndex >= 0) {
          expectedRequestFailures.splice(expectedIndex, 1);
        } else {
          requestFailures.push(text);
        }
      };
      page.on("console", onConsole);
      page.on("requestfailed", onRequestFailed);

      await use({
        expectConsoleError(pattern) {
          expectedConsoleErrors.push(pattern);
        },
        expectRequestFailure(pattern) {
          expectedRequestFailures.push(pattern);
        },
      });

      page.off("console", onConsole);
      page.off("requestfailed", onRequestFailed);
      if (testInfo.status === testInfo.expectedStatus) {
        expect(
          consoleErrors,
          "unexpected browser console errors",
        ).toEqual([]);
        expect(
          requestFailures,
          "unexpected browser request failures",
        ).toEqual([]);
        expect(
          expectedConsoleErrors,
          "expected browser console errors that did not occur",
        ).toEqual([]);
        expect(
          expectedRequestFailures,
          "expected browser request failures that did not occur",
        ).toEqual([]);
      }
    },
    { auto: true },
  ],
});

export { expect };
