"use client";

import { useEffect } from "react";

export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Research workbench render failed", error);
  }, [error]);

  return (
    <main className="route-error" role="alert">
      <h1>The research workbench could not load</h1>
      <p>The failure was recorded without exposing internal service details.</p>
      <button type="button" onClick={reset}>
        Try again
      </button>
    </main>
  );
}
