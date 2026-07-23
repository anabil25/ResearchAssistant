import nextJest from "next/jest.js";
import type { Config } from "jest";

const createJestConfig = nextJest({
  dir: "./",
});

const config: Config = {
  clearMocks: true,
  collectCoverageFrom: [
    "src/**/*.{ts,tsx}",
    "!src/lib/generated-api.ts",
    "!src/lib/types.ts",
  ],
  coverageDirectory: "<rootDir>/coverage",
  coverageReporters: ["text", "json-summary", "lcov", "cobertura"],
  coverageThreshold: {
    global: {
      branches: 100,
      functions: 100,
      lines: 100,
      statements: 100,
    },
  },
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  modulePathIgnorePatterns: ["<rootDir>/.next/"],
  roots: ["<rootDir>/src"],
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  testEnvironment: "jsdom",
  testMatch: ["**/*.test.{ts,tsx}"],
};

export default createJestConfig(config);
