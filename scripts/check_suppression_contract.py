from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter, defaultdict
from collections.abc import Generator, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

SCHEMA_VERSION = "research-assistant.suppression-contract.v1"
PYTHON_SUFFIXES = {".py", ".pyi"}
JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


@dataclass(frozen=True)
class Suppression:
    path: str
    kind: str
    scope: str
    reason: str


@dataclass(frozen=True)
class StructuralExclusion:
    path: str
    symbol: str
    start: int
    end: int


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def tracked_files(root: Path) -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=root,
    )
    return sorted(path for path in output.decode("utf-8").split("\0") if path)


def _strip_reason(value: str) -> str:
    return re.sub(r"^(?:--?|:)\s*", "", value.strip()).strip()


def _split_scope_reason(value: str) -> tuple[str, str]:
    match = re.match(r"^(?P<scope>.*?)(?:\s+--?\s+(?P<reason>.+))?$", value.strip())
    if match is None:
        return value.strip(), ""
    return match.group("scope").strip(), (match.group("reason") or "").strip()


def parse_python_comment(path: str, comment: str) -> list[Suppression]:
    entries: list[Suppression] = []
    pragma = re.search(r"pragma:\s*no\s*cover\b(?P<tail>.*)", comment, re.IGNORECASE)
    if pragma:
        entries.append(
            Suppression(path, "coverage-pragma", "", _strip_reason(pragma.group("tail")))
        )

    type_ignore = re.search(
        r"type:\s*ignore(?P<scope>\[[^\]]+\])?(?P<tail>.*)",
        comment,
        re.IGNORECASE,
    )
    if type_ignore:
        scope = (type_ignore.group("scope") or "").strip("[] \t")
        entries.append(
            Suppression(path, "type-ignore", scope, _strip_reason(type_ignore.group("tail")))
        )

    noqa = re.search(r"\bnoqa\b(?P<tail>.*)", comment, re.IGNORECASE)
    if noqa:
        tail = noqa.group("tail").strip()
        if tail.startswith(":"):
            scope, reason = _split_scope_reason(tail[1:])
        else:
            scope, reason = "", _strip_reason(tail)
        entries.append(Suppression(path, "noqa", scope, reason))
    return entries


def python_suppressions(root: Path, path: str) -> list[Suppression]:
    entries: list[Suppression] = []
    data = (root / path).read_bytes()
    for token in tokenize.tokenize(io.BytesIO(data).readline):
        if token.type == tokenize.COMMENT:
            entries.extend(parse_python_comment(path, token.string))
    return entries


def _javascript_comments(source: str) -> Iterable[str]:
    length = len(source)

    def scan_code(
        index: int,
        stop_at_brace: bool = False,
    ) -> Generator[tuple[str, int], None, int]:
        brace_depth = 1 if stop_at_brace else 0
        while index < length:
            char = source[index]
            next_char = source[index + 1] if index + 1 < length else ""
            if char in {"'", '"'}:
                quote = char
                index += 1
                while index < length:
                    if source[index] == "\\":
                        index += 2
                    elif source[index] == quote:
                        index += 1
                        break
                    else:
                        index += 1
                continue
            if char == "`":
                index += 1
                while index < length:
                    if source[index] == "\\":
                        index += 2
                    elif source[index] == "`":
                        index += 1
                        break
                    elif source[index : index + 2] == "${":
                        index += 2
                        index = yield from scan_code(index, stop_at_brace=True)
                    else:
                        index += 1
                continue
            if char == "/" and next_char == "/":
                end = source.find("\n", index + 2)
                if end == -1:
                    end = length
                yield source[index + 2 : end], end
                index = end
                continue
            if char == "/" and next_char == "*":
                end = source.find("*/", index + 2)
                if end == -1:
                    yield source[index + 2 :], length
                    return length
                yield source[index + 2 : end], end + 2
                index = end + 2
                continue
            if stop_at_brace:
                if char == "{":
                    brace_depth += 1
                elif char == "}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        return index + 1
            if char == "\\":
                index += 2
            else:
                index += 1
        return index

    for comment, _ in scan_code(0):
        yield comment


def parse_javascript_comment(path: str, comment: str) -> list[Suppression]:
    entries: list[Suppression] = []
    patterns = [
        ("eslint-disable", r"\beslint-disable(?:-next-line|-line)?\b(?P<tail>.*)"),
        ("ts-ignore", r"@ts-ignore\b(?P<tail>.*)"),
        ("ts-expect-error", r"@ts-expect-error\b(?P<tail>.*)"),
        ("istanbul-ignore", r"\bistanbul\s+ignore(?:\s+\w+)?\b(?P<tail>.*)"),
        ("c8-ignore", r"\bc8\s+ignore(?:\s+\w+)?\b(?P<tail>.*)"),
    ]
    for kind, pattern in patterns:
        match = re.search(pattern, comment, re.IGNORECASE)
        if not match:
            continue
        scope, reason = _split_scope_reason(match.group("tail"))
        entries.append(Suppression(path, kind, scope, reason))
    return entries


def javascript_suppressions(root: Path, path: str) -> list[Suppression]:
    source = (root / path).read_text(encoding="utf-8")
    entries: list[Suppression] = []
    for comment in _javascript_comments(source):
        entries.extend(parse_javascript_comment(path, comment))
    return entries


def scan_suppressions(root: Path, paths: Iterable[str]) -> list[Suppression]:
    entries: list[Suppression] = []
    for path in paths:
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in PYTHON_SUFFIXES:
            entries.extend(python_suppressions(root, path))
        elif suffix in JAVASCRIPT_SUFFIXES:
            entries.extend(javascript_suppressions(root, path))
    return entries


def _tracked_python_under(paths: Iterable[str], prefix: str) -> list[str]:
    normalized = prefix.rstrip("/") + "/"
    return [
        path
        for path in paths
        if path.startswith(normalized) and PurePosixPath(path).suffix in PYTHON_SUFFIXES
    ]


def _add_evidence(
    evidence: dict[str, set[str]],
    paths: Iterable[str],
    label: str,
) -> None:
    for path in paths:
        evidence[path].add(label)


def _docker_copy_sources(dockerfile: Path) -> list[str]:
    sources: list[str] = []
    for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("COPY ") or "--from=" in line:
            continue
        tokens = line.split()[1:]
        tokens = [token for token in tokens if not token.startswith("--")]
        if len(tokens) >= 2:
            sources.extend(tokens[:-1])
    return sources


def production_evidence(root: Path, paths: list[str]) -> dict[str, list[str]]:
    evidence: dict[str, set[str]] = defaultdict(set)
    tracked = set(paths)

    pyproject_paths = [
        root / path for path in paths if PurePosixPath(path).name == "pyproject.toml"
    ]
    for pyproject_path in pyproject_paths:
        if pyproject_path == root / "pyproject.toml":
            continue
        config = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        if "project" not in config or "build-system" not in config:
            continue
        package_root = pyproject_path.parent.relative_to(root).as_posix()
        _add_evidence(
            evidence,
            _tracked_python_under(paths, f"{package_root}/src"),
            f"python-distribution:{package_root}",
        )

    azure = yaml.safe_load((root / "azure.yaml").read_text(encoding="utf-8"))
    for name, service in (azure.get("services") or {}).items():
        project = str(service.get("project", ".")).removeprefix("./").rstrip("/")
        if service.get("host") == "azure.ai.agent":
            _add_evidence(
                evidence,
                _tracked_python_under(paths, project),
                f"hosted-agent-project:{name}:{project}",
            )
        docker = service.get("docker")
        if not docker:
            continue
        context = str(docker.get("context", service.get("project", "."))).removeprefix("./")
        docker_relative = (
            PurePosixPath(project) / str(docker["path"]).removeprefix("./")
        ).as_posix()
        docker_path = root / docker_relative
        for source in _docker_copy_sources(docker_path):
            source_path = PurePosixPath(context) / source
            normalized = source_path.as_posix().lstrip("./")
            if normalized in tracked and PurePosixPath(normalized).suffix in PYTHON_SUFFIXES:
                _add_evidence(evidence, [normalized], f"container-image:{name}:{docker_path.relative_to(root)}")
            else:
                _add_evidence(
                    evidence,
                    _tracked_python_under(paths, normalized),
                    f"container-image:{name}:{docker_path.relative_to(root)}",
                )

    hook_module = re.compile(r"(?:^|\s)-m\s+(?P<module>scripts\.[A-Za-z0-9_.]+)")
    for hook_name, platforms in (azure.get("hooks") or {}).items():
        for platform, hook in (platforms or {}).items():
            run_path = str(hook.get("run", "")).removeprefix("./")
            wrapper = root / run_path
            if not wrapper.exists():
                continue
            for match in hook_module.finditer(wrapper.read_text(encoding="utf-8")):
                module = match.group("module")
                module_files = source_entry_files(paths, module)
                if module_files:
                    _add_evidence(
                        evidence,
                        module_files,
                        f"azure-hook:{hook_name}:{platform}:{run_path}",
                    )
    return {path: sorted(labels) for path, labels in sorted(evidence.items())}


def coverage_source_evidence(
    paths: list[str],
    source_entries: Iterable[str],
) -> dict[str, list[str]]:
    evidence: dict[str, set[str]] = defaultdict(set)
    for source_entry in source_entries:
        _add_evidence(
            evidence,
            source_entry_files(paths, source_entry),
            f"coverage-source:{source_entry}",
        )
    return {path: sorted(labels) for path, labels in sorted(evidence.items())}


def posture_partition(
    paths: list[str],
    packaging: Mapping[str, list[str]],
    coverage: Mapping[str, list[str]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    production: dict[str, list[str]] = {}
    unknown: list[dict[str, Any]] = []
    for path in sorted(
        path for path in paths if PurePosixPath(path).suffix in PYTHON_SUFFIXES
    ):
        packaging_labels = packaging.get(path, [])
        coverage_labels = coverage.get(path, [])
        if bool(packaging_labels) != bool(coverage_labels):
            unknown.append(
                {
                    "path": path,
                    "packagingEvidence": packaging_labels,
                    "coverageEvidence": coverage_labels,
                }
            )
        elif packaging_labels:
            production[path] = sorted([*packaging_labels, *coverage_labels])
    return production, unknown


def discover_source_roots(root: Path, paths: list[str]) -> list[str]:
    roots: set[str] = set()
    pyproject_paths = [
        root / path for path in paths if PurePosixPath(path).name == "pyproject.toml"
    ]
    for pyproject_path in pyproject_paths:
        if pyproject_path == root / "pyproject.toml":
            continue
        config = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        if "project" not in config:
            continue
        src = pyproject_path.parent / "src"
        if not src.exists():
            continue
        for child in src.iterdir():
            if child.is_dir() and any(child.rglob("*.py")):
                roots.add(child.relative_to(root).as_posix())

    azure = yaml.safe_load((root / "azure.yaml").read_text(encoding="utf-8"))
    for service in (azure.get("services") or {}).values():
        if service.get("host") != "azure.ai.agent":
            continue
        project = str(service.get("project", ".")).removeprefix("./").rstrip("/")
        top_level: set[str] = set()
        for path in _tracked_python_under(paths, project):
            relative = PurePosixPath(path).relative_to(project)
            if len(relative.parts) > 1:
                top_level.add((PurePosixPath(project) / relative.parts[0]).as_posix())
        roots.update(top_level)
    hook_module = re.compile(r"(?:^|\s)-m\s+(?P<module>scripts\.[A-Za-z0-9_.]+)")
    for platforms in (azure.get("hooks") or {}).values():
        for hook in (platforms or {}).values():
            run_path = str(hook.get("run", "")).removeprefix("./")
            wrapper = root / run_path
            if not wrapper.exists():
                continue
            for match in hook_module.finditer(wrapper.read_text(encoding="utf-8")):
                module = match.group("module")
                if source_entry_files(paths, module):
                    roots.add(module)
    return sorted(roots)


def module_name(source_root: str, path: str) -> str:
    root_path = PurePosixPath(source_root)
    relative = PurePosixPath(path).relative_to(root_path)
    parts = [root_path.name, *relative.parts]
    filename = parts.pop()
    stem = PurePosixPath(filename).stem
    if stem != "__init__":
        parts.append(stem)
    return ".".join(parts)


def source_entry_files(paths: list[str], source_entry: str) -> list[str]:
    normalized = source_entry.replace("\\", "/").rstrip("/")
    under_root = _tracked_python_under(paths, normalized)
    if under_root:
        return under_root
    module_path = normalized.replace(".", "/")
    module_file = module_path + ".py"
    if module_file in paths:
        return [module_file]
    return _tracked_python_under(paths, module_path)


def module_inventory(paths: list[str], source_roots: list[str]) -> tuple[list[str], list[str]]:
    files: list[str] = []
    modules: list[str] = []
    for source_root in source_roots:
        module_file = source_root.replace(".", "/") + ".py"
        for path in source_entry_files(paths, source_root):
            files.append(path)
            modules.append(
                source_root if path == module_file else module_name(source_root, path)
            )
    return sorted(set(files)), sorted(set(modules))


def coverage_configuration(root: Path) -> dict[str, Any]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    coverage = (config.get("tool") or {}).get("coverage")
    if not coverage:
        raise ValueError("[tool.coverage] configuration is missing")
    run = coverage.get("run") or {}
    report = coverage.get("report") or {}
    json_config = coverage.get("json") or {}
    xml_config = coverage.get("xml") or {}
    return {
        "run": {
            "branch": run.get("branch"),
            "relative_files": run.get("relative_files"),
            "source": run.get("source"),
            "omit": run.get("omit", []),
        },
        "report": {
            "fail_under": report.get("fail_under"),
            "precision": report.get("precision"),
            "show_missing": report.get("show_missing"),
            "skip_empty": report.get("skip_empty"),
            "exclude_lines": report.get("exclude_lines", []),
            "exclude_also": report.get("exclude_also", []),
        },
        "json": {"output": json_config.get("output"), "pretty_print": json_config.get("pretty_print")},
        "xml": {"output": xml_config.get("output")},
    }


def mypy_configuration(root: Path) -> dict[str, Any]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    mypy = (config.get("tool") or {}).get("mypy")
    if not mypy:
        raise ValueError("[tool.mypy] configuration is missing")
    return {
        "strict": mypy.get("strict"),
        "warn_unused_ignores": mypy.get("warn_unused_ignores"),
        "explicit_package_bases": mypy.get("explicit_package_bases"),
        "files": mypy.get("files"),
        "mypy_path": mypy.get("mypy_path"),
        "exclude": mypy.get("exclude", []),
    }


def mypy_roots(paths: list[str], source_roots: list[str]) -> list[str]:
    roots = {
        source_root.split(".", 1)[0]
        if "/" not in source_root
        else PurePosixPath(source_root).parts[0]
        for source_root in source_roots
    }
    for tooling_root in ("scripts", "tests"):
        if _tracked_python_under(paths, tooling_root):
            roots.add(tooling_root)
    return sorted(roots)


def mypy_paths(source_roots: list[str]) -> list[str]:
    paths: set[str] = set()
    for source_root in source_roots:
        if "/" not in source_root:
            continue
        parts = PurePosixPath(source_root).parts
        if "src" in parts:
            src_index = parts.index("src")
            paths.add(PurePosixPath(*parts[: src_index + 1]).as_posix())
        else:
            paths.add(PurePosixPath(source_root).parent.as_posix())
    return sorted(paths)


def mypy_file_inventory(paths: list[str], roots: list[str]) -> list[str]:
    return sorted(
        {
            path
            for root in roots
            for path in _tracked_python_under(paths, root)
        }
    )


def mypy_excluded_files(files: list[str], patterns: str | list[str]) -> list[str]:
    expressions = [patterns] if isinstance(patterns, str) else patterns
    return sorted(
        path
        for path in files
        if any(re.search(expression, path) for expression in expressions)
    )


def mypy_module_name(path: str, search_paths: list[str]) -> str:
    file_path = PurePosixPath(path)
    matching = [
        PurePosixPath(search_path)
        for search_path in search_paths
        if file_path.is_relative_to(PurePosixPath(search_path))
    ]
    relative = file_path.relative_to(max(matching, key=lambda item: len(item.parts))) if matching else file_path
    parts = list(relative.parts)
    stem = PurePosixPath(parts.pop()).stem
    if stem != "__init__":
        parts.append(stem)
    return ".".join(parts)


def reported_mypy_modules(report_path: Path) -> list[str]:
    modules: list[str] = []
    for line in report_path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields and fields[-1] != "total":
            modules.append(fields[-1])
    return sorted(modules)


def structural_exclusions(root: Path, files: Iterable[str]) -> list[StructuralExclusion]:
    exclusions: list[StructuralExclusion] = []
    for path in files:
        if PurePosixPath(path).suffix != ".py":
            continue
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)

        def visit(
            body: list[ast.stmt],
            parents: list[str],
            source_path: str = path,
        ) -> None:
            for node in body:
                next_parents = parents
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbol = ".".join([*parents, node.name])
                    next_parents = [*parents, node.name]
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) == 1:
                        expression = node.body[0]
                        if (
                            isinstance(expression, ast.Expr)
                            and isinstance(expression.value, ast.Constant)
                            and expression.value.value is Ellipsis
                        ):
                            exclusions.append(
                                StructuralExclusion(
                                    source_path,
                                    symbol,
                                    node.lineno,
                                    node.end_lineno or node.lineno,
                                )
                            )
                child_body = getattr(node, "body", None)
                if isinstance(child_body, list):
                    visit(child_body, next_parents)

        visit(tree.body, [])
    return sorted(exclusions, key=lambda item: (item.path, item.symbol))


def _counter_records(
    counter: Mapping[Any, int],
    fields: list[str],
) -> list[dict[str, Any]]:
    records = []
    for key, count in sorted(counter.items()):
        record: dict[str, Any] = dict(zip(fields, key, strict=True))
        record["count"] = count
        records.append(record)
    return records


def report_exclusions(
    root: Path,
    coverage_json: Path,
    structural: list[StructuralExclusion],
) -> tuple[list[dict[str, Any]], list[str]]:
    report = json.loads(coverage_json.read_text(encoding="utf-8"))
    counter: Counter[tuple[str, ...]] = Counter()
    unknown: list[str] = []
    structural_by_path: dict[str, list[StructuralExclusion]] = defaultdict(list)
    for exclusion in structural:
        structural_by_path[exclusion.path].append(exclusion)

    for raw_path, payload in report.get("files", {}).items():
        path = PurePosixPath(raw_path.replace("\\", "/")).as_posix()
        lines = (root / path).read_text(encoding="utf-8").splitlines()
        pragma_lines = {
            token.start[0]
            for token in tokenize.generate_tokens(io.StringIO("\n".join(lines)).readline)
            if token.type == tokenize.COMMENT
            and re.search(r"pragma:\s*no\s*cover\b", token.string, re.IGNORECASE)
        }
        for line_number in payload.get("excluded_lines", []):
            text = lines[line_number - 1].strip()
            if line_number in pragma_lines:
                kind = "source-pragma"
            else:
                kind = "tool-default"
                if text and not any(
                    exclusion.start <= line_number <= exclusion.end
                    for exclusion in structural_by_path.get(path, [])
                ):
                    unknown.append(f"{path}:{line_number}:{text}")
            counter[(path, kind, text, "")] += 1
    return (
        _counter_records(counter, ["path", "kind", "source", "reason"]),
        sorted(unknown),
    )


def _suppression_identity(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        record["path"],
        record["kind"],
        record["scope"],
        record["reason"],
        record["posture"],
    )


def suppression_records(
    suppressions: list[Suppression],
    production: dict[str, list[str]],
    unknown: set[str],
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str, str, str, str]] = Counter()
    for entry in suppressions:
        posture = (
            "unknown"
            if entry.path in unknown
            else "production"
            if entry.path in production
            else "test"
        )
        counter[(entry.path, entry.kind, entry.scope, entry.reason, posture)] += 1

    previous_metadata = {}
    if previous:
        previous_metadata = {
            _suppression_identity(record): (
                record.get("role", "standard"),
                record.get("protectedTest"),
                record.get("protectedControl"),
            )
            for record in previous.get("sourceSuppressions", [])
        }

    records = []
    for key, count in sorted(counter.items()):
        path, kind, scope, reason, posture = key
        role, protected_test, protected_control = previous_metadata.get(
            key,
            ("standard", None, None),
        )
        records.append(
            {
                "path": path,
                "kind": kind,
                "scope": scope,
                "reason": reason,
                "posture": posture,
                "role": role,
                "protectedTest": protected_test,
                "protectedControl": protected_control,
                "count": count,
            }
        )
    return records


def build_inventory(
    root: Path,
    coverage_json: Path,
    mypy_report: Path,
    previous: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    paths = tracked_files(root)
    suppressions = scan_suppressions(root, paths)
    config = coverage_configuration(root)
    packaging = production_evidence(root, paths)
    coverage_production = coverage_source_evidence(
        paths,
        config["run"]["source"] or [],
    )
    production, posture_unknown = posture_partition(
        paths,
        packaging,
        coverage_production,
    )
    discovered_roots = discover_source_roots(root, paths)
    source_files, modules = module_inventory(paths, discovered_roots)
    discovered_mypy_roots = mypy_roots(paths, discovered_roots)
    discovered_mypy_paths = mypy_paths(discovered_roots)
    mypy_files = mypy_file_inventory(paths, discovered_mypy_roots)
    mypy_config = mypy_configuration(root)
    mypy_modules = sorted(
        mypy_module_name(path, discovered_mypy_paths) for path in mypy_files
    )
    structural = structural_exclusions(root, source_files)
    excluded_lines, unknown = report_exclusions(root, coverage_json, structural)
    coverage_report = json.loads(coverage_json.read_text(encoding="utf-8"))
    reported_files = sorted(
        PurePosixPath(path.replace("\\", "/")).as_posix()
        for path in coverage_report["files"]
    )

    source_suppressions = suppression_records(
        suppressions,
        production,
        {record["path"] for record in posture_unknown},
        previous,
    )
    inventory = {
        "schemaVersion": SCHEMA_VERSION,
        "coverageConfig": config,
        "mypyConfig": mypy_config,
        "discoveredSourceRoots": discovered_roots,
        "discoveredMypyRoots": discovered_mypy_roots,
        "discoveredMypyPaths": discovered_mypy_paths,
        "sourceFiles": source_files,
        "moduleNames": modules,
        "mypyFiles": mypy_files,
        "mypyExcludedDomainFiles": mypy_excluded_files(
            mypy_files,
            mypy_config["exclude"],
        ),
        "mypyModuleNames": mypy_modules,
        "reportedMypyModules": reported_mypy_modules(mypy_report),
        "reportedCoverageFiles": reported_files,
        "packagingFiles": [
            {"path": path, "evidence": labels} for path, labels in packaging.items()
        ],
        "coverageProductionFiles": [
            {"path": path, "evidence": labels}
            for path, labels in coverage_production.items()
        ],
        "productionFiles": [
            {"path": path, "evidence": labels} for path, labels in production.items()
        ],
        "postureUnknownFiles": posture_unknown,
        "sourceSuppressions": source_suppressions,
        "coverageStructuralExclusions": [
            {
                "path": item.path,
                "kind": "coverage-default-ellipsis-stub",
                "symbol": item.symbol,
                "reason": "coverage.py excludes ellipsis-only function bodies by default",
            }
            for item in structural
        ],
        "coverageExcludedLines": excluded_lines,
    }
    return inventory, unknown


def validate_inventory(root: Path, inventory: dict[str, Any], unknown: list[str]) -> list[str]:
    errors: list[str] = []
    config = inventory["coverageConfig"]
    if config["run"]["branch"] is not True:
        errors.append("coverage branch measurement must remain enabled")
    if config["run"]["relative_files"] is not True:
        errors.append("coverage relative_files must remain enabled")
    if config["report"]["fail_under"] != 100:
        errors.append("coverage fail_under must remain exactly 100")
    if config["run"]["omit"]:
        errors.append("coverage run.omit must remain empty")
    if config["report"]["exclude_lines"] or config["report"]["exclude_also"]:
        errors.append("coverage exclusion configuration must remain empty")
    if sorted(config["run"]["source"] or []) != inventory["discoveredSourceRoots"]:
        errors.append("configured coverage source roots differ from packaging-derived roots")
    if inventory["sourceFiles"] != inventory["reportedCoverageFiles"]:
        errors.append("coverage JSON file set differs from the packaging-derived source file set")
    for record in inventory["postureUnknownFiles"]:
        errors.append(
            "production posture is unknown because packaging and coverage disagree: "
            f"{record['path']} "
            f"(packaging={record['packagingEvidence']}, "
            f"coverage={record['coverageEvidence']})"
        )
    errors.extend(f"unclassified coverage exclusion: {item}" for item in unknown)

    mypy = inventory["mypyConfig"]
    if mypy["strict"] is not True:
        errors.append("mypy strict mode must remain enabled")
    if mypy["warn_unused_ignores"] is not True:
        errors.append("mypy warn_unused_ignores must remain enabled")
    if mypy["explicit_package_bases"] is not True:
        errors.append("mypy explicit_package_bases must remain enabled")
    if sorted(mypy["files"] or []) != inventory["discoveredMypyRoots"]:
        errors.append("configured mypy roots differ from the packaging-derived domain")
    if sorted(mypy["mypy_path"] or []) != inventory["discoveredMypyPaths"]:
        errors.append("configured mypy search paths differ from packaging-derived import roots")
    if inventory["mypyExcludedDomainFiles"]:
        errors.append(
            "mypy exclude removes files from the packaging-derived domain: "
            + ", ".join(inventory["mypyExcludedDomainFiles"])
        )
    if inventory["mypyModuleNames"] != inventory["reportedMypyModules"]:
        errors.append("mypy report module set differs from the packaging-derived Python domain")

    forbidden_javascript = {
        "eslint-disable",
        "ts-ignore",
        "ts-expect-error",
        "istanbul-ignore",
        "c8-ignore",
    }
    for record in inventory["sourceSuppressions"]:
        kind = record["kind"]
        if kind in {"type-ignore", "noqa"} and not record["scope"] and not record["reason"]:
            errors.append(f"bare {kind} is forbidden; scope or reason required: {record['path']}")
        if kind == "coverage-pragma" and not record["reason"]:
            errors.append(f"coverage pragma requires a stated reason: {record['path']}")
        if kind in forbidden_javascript:
            errors.append(f"{kind} suppressions are pinned to zero: {record['path']}")
        if record["role"] not in {"standard", "load-bearing"}:
            errors.append(f"invalid suppression role for {record['path']}: {record['role']}")
        if record["role"] == "load-bearing":
            for field in ("protectedTest", "protectedControl"):
                link = record.get(field)
                if (
                    not isinstance(link, dict)
                    or not link.get("path")
                    or not link.get("anchor")
                    or not link.get("description")
                ):
                    errors.append(f"load-bearing suppression lacks {field}: {record['path']}")
                    continue
                linked_path = root / link["path"]
                if not linked_path.exists() or link["anchor"] not in linked_path.read_text(
                    encoding="utf-8"
                ):
                    errors.append(
                        f"load-bearing suppression has unresolved {field}: "
                        f"{record['path']} -> {link['path']}::{link['anchor']}"
                    )
    return errors


def census(inventory: dict[str, Any]) -> dict[str, Any]:
    kind_counts: Counter[str] = Counter()
    kind_files: dict[str, set[str]] = defaultdict(set)
    posture_counts: Counter[str] = Counter()
    bare = 0
    scoped = 0
    reasoned = 0
    load_bearing = 0
    for record in inventory["sourceSuppressions"]:
        count = record["count"]
        kind_counts[record["kind"]] += count
        kind_files[record["kind"]].add(record["path"])
        posture_counts[record["posture"]] += count
        bare += count if record["kind"] in {"type-ignore", "noqa"} and not record["scope"] else 0
        scoped += count if record["kind"] in {"type-ignore", "noqa"} and record["scope"] else 0
        reasoned += count if record["reason"] else 0
        load_bearing += count if record["role"] == "load-bearing" else 0
    return {
        "sourceSuppressions": sum(kind_counts.values()),
        "sourceSuppressionFiles": len(
            {record["path"] for record in inventory["sourceSuppressions"]}
        ),
        "byKind": {
            kind: {"occurrences": count, "files": len(kind_files[kind])}
            for kind, count in sorted(kind_counts.items())
        },
        "byPosture": dict(sorted(posture_counts.items())),
        "bare": bare,
        "scoped": scoped,
        "reasoned": reasoned,
        "reasonMissing": sum(
            record["count"] for record in inventory["sourceSuppressions"] if not record["reason"]
        ),
        "loadBearing": load_bearing,
        "coverageExcludedLines": sum(
            record["count"] for record in inventory["coverageExcludedLines"]
        ),
        "coverageExcludedFiles": len(
            {record["path"] for record in inventory["coverageExcludedLines"]}
        ),
        "coverageStructuralRoots": len(inventory["coverageStructuralExclusions"]),
        "coverageSourceRoots": len(inventory["discoveredSourceRoots"]),
        "coverageModules": len(inventory["moduleNames"]),
        "mypyFiles": len(inventory["mypyFiles"]),
        "mypyExcludedDomainFiles": len(inventory["mypyExcludedDomainFiles"]),
        "mypyModules": len(inventory["mypyModuleNames"]),
    }


def compare_inventory(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    if expected == actual:
        return []
    expected_text = json.dumps(expected, indent=2, sort_keys=True).splitlines()
    actual_text = json.dumps(actual, indent=2, sort_keys=True).splitlines()
    import difflib

    return list(
        difflib.unified_diff(
            expected_text,
            actual_text,
            fromfile="committed suppression contract",
            tofile="current suppression inventory",
            lineterm="",
        )
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce the exact suppression and coverage domain.")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(".github/suppression-contract.json"),
    )
    parser.add_argument(
        "--coverage-json",
        type=Path,
        default=Path("coverage/python/coverage.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("coverage/python/suppression-contract-report.json"),
    )
    parser.add_argument(
        "--mypy-report",
        type=Path,
        default=Path("coverage/python/mypy-domain/linecount.txt"),
    )
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    root = repository_root()
    baseline_path = args.baseline if args.baseline.is_absolute() else root / args.baseline
    coverage_json = (
        args.coverage_json if args.coverage_json.is_absolute() else root / args.coverage_json
    )
    mypy_report = args.mypy_report if args.mypy_report.is_absolute() else root / args.mypy_report
    report_path = args.report if args.report.is_absolute() else root / args.report
    previous = _load_json(baseline_path) if baseline_path.exists() else None

    try:
        inventory, unknown = build_inventory(
            root,
            coverage_json,
            mypy_report,
            previous,
        )
        errors = validate_inventory(root, inventory, unknown)
    except (OSError, ValueError, KeyError, tokenize.TokenError, SyntaxError) as exc:
        _write_json(
            report_path,
            {"schemaVersion": SCHEMA_VERSION, "ok": False, "errors": [str(exc)]},
        )
        print(f"suppression contract failed: {exc}", file=sys.stderr)
        return 1

    summary = census(inventory)
    if args.write_baseline:
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        _write_json(baseline_path, inventory)
        print(f"Wrote suppression contract baseline: {baseline_path}")
        return 0

    if previous is None:
        errors.append(f"suppression contract baseline is missing: {baseline_path}")
        differences: list[str] = []
    else:
        differences = compare_inventory(previous, inventory)
        if differences:
            errors.append("current suppression inventory differs from the committed exact set")

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "ok": not errors,
        "summary": summary,
        "errors": errors,
        "differences": differences,
        "inventory": inventory,
    }
    _write_json(report_path, report)

    print(json.dumps(summary, indent=2, sort_keys=True))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        for line in differences:
            print(line, file=sys.stderr)
        return 1
    print("Suppression contract exact set verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
