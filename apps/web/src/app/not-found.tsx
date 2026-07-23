import Link from "next/link";

export default function NotFound() {
  return (
    <main className="route-error">
      <h1>Research workspace page not found</h1>
      <p>The requested page is not part of this accelerator.</p>
      <Link href="/">Return to the research workbench</Link>
    </main>
  );
}
