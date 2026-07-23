"use client";

import { FluentProvider, webLightTheme } from "@fluentui/react-components";

export function AppProviders({ children }: { children: React.ReactNode }) {
  return (
    <FluentProvider className="app-fluent-provider" theme={webLightTheme}>
      {children}
    </FluentProvider>
  );
}
