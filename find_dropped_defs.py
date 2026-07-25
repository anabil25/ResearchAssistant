"""Find top-level definitions that a merge silently dropped.

For every Python module in the working tree, compare its top-level def/class
names against the same module on each merged branch. Anything a branch defined
that the merged tree no longer defines was lost when git resolved that file by
taking one side wholesale -- a silent loss that no conflict marker records.
"""

import ast
import subprocess
import sys

BRANCHES = [
    "anabil25-fix-runtime-trust-clean",
    "anabil25-harden-provider-adapter",
    "anabil25-fix-dataset-approval-boundary",
    "anabil25-agent-studio-registry-workspace",
    "anabil25-agent-studio-platform-backend",
    "anabil25-agent-studio-integrations",
    "anabil25-animated-engine",
    "anabil25-coverage-release-gates",
    "anabil25-agent-harness-foundation",
    "refs/heads/main",
]
ROOTS = ("services/", "packages/", "agents/", "scripts/")


def names(src: str) -> set[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out.update(t.id for t in n.targets if isinstance(t, ast.Name))
    return out


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8"
    ).stdout


def main() -> int:
    tracked = [
        p for p in git("ls-files").splitlines()
        if p.endswith(".py") and p.startswith(ROOTS)
    ]
    head = {p: names(git("show", f"HEAD:{p}")) for p in tracked}

    losses: dict[str, dict[str, set[str]]] = {}
    for branch in BRANCHES:
        for p in tracked:
            src = git("show", f"{branch}:{p}")
            if not src:
                continue
            missing = names(src) - head.get(p, set())
            missing = {m for m in missing if not m.startswith("__")}
            if missing:
                losses.setdefault(p, {}).setdefault(branch, set()).update(missing)

    if not losses:
        print("no dropped definitions")
        return 0
    for path, per_branch in sorted(losses.items()):
        all_missing: set[str] = set()
        for m in per_branch.values():
            all_missing |= m
        print(f"\n{path}")
        print(f"    lost: {', '.join(sorted(all_missing))}")
        for branch, m in per_branch.items():
            print(f"      from {branch}: {', '.join(sorted(m))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
