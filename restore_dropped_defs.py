"""Restore top-level defs that a merge silently dropped from a module.

Appends any top-level def/class present in `--from` but absent from `path`,
plus any imports those definitions need that are not already present. Used at
integration after git auto-merged a file by taking one side wholesale, which
silently removed definitions the other side still depends on.
"""

import argparse
import ast
import subprocess


def show(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout


def names(tree: ast.Module) -> set[str]:
    out = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out.update(t.id for t in n.targets if isinstance(t, ast.Name))
    return out


def imports(src: str, tree: ast.Module) -> list[str]:
    lines = src.splitlines()
    return [
        "\n".join(lines[n.lineno - 1 : n.end_lineno])
        for n in tree.body
        if isinstance(n, ast.Import | ast.ImportFrom)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--from", dest="source_ref", required=True)
    args = ap.parse_args()

    with open(args.path, encoding="utf-8") as fh:
        cur_src = fh.read()
    src_src = show(args.source_ref, args.path)
    cur, src = ast.parse(cur_src), ast.parse(src_src)

    have = names(cur)
    cur_imports = set(imports(cur_src, cur))
    src_lines = src_src.splitlines()

    added, added_names = [], []
    for node in src.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name in have:
                continue
            start = min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1
            added.append("\n".join(src_lines[start : node.end_lineno]))
            added_names.append(node.name)

    new_imports = [i for i in imports(src_src, src) if i not in cur_imports]

    result = cur_src.rstrip("\n")
    if new_imports:
        lines = result.splitlines()
        end = max(
            (n.end_lineno for n in cur.body if isinstance(n, ast.Import | ast.ImportFrom)),
            default=0,
        )
        lines[end:end] = new_imports
        result = "\n".join(lines)
    if added:
        result += "\n\n\n" + "\n\n\n".join(added)
    result += "\n"

    ast.parse(result)
    with open(args.path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(result)
    print(f"{args.path}: restored {added_names}, +{len(new_imports)} imports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
