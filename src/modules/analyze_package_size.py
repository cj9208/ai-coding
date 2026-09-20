# check uv.lock for package size
# and split into isolated groups to avoid installing all dependencies

import tomllib
from pathlib import Path
from typing import Any, Dict, List, Set


def load_uv_lock(lock_path: str = "uv.lock") -> Dict[str, Any]:
    """Load and parse the uv.lock file."""
    path = Path(lock_path)
    if not path.exists():
        raise FileNotFoundError(f"Lock file not found: {lock_path}")

    with open(path, "rb") as f:
        return tomllib.load(f)


def load_pyproject_toml(pyproject_path: str = "pyproject.toml") -> Set[str]:
    """Load pyproject.toml and extract explicit dependencies."""
    path = Path(pyproject_path)
    if not path.exists():
        raise FileNotFoundError(f"pyproject.toml not found: {pyproject_path}")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    explicit_deps = set()

    # Get project dependencies
    project = data.get("project", {})
    for dep in project.get("dependencies", []):
        # Extract package name (before any version specifier)
        pkg_name = (
            dep.split("[")[0]
            .split(">")[0]
            .split("<")[0]
            .split("=")[0]
            .split("!")[0]
            .split("~")[0]
            .strip()
        )
        explicit_deps.add(pkg_name.lower())

    # Get optional dependencies
    optional_deps = project.get("optional-dependencies", {})
    for group_deps in optional_deps.values():
        for dep in group_deps:
            pkg_name = (
                dep.split("[")[0]
                .split(">")[0]
                .split("<")[0]
                .split("=")[0]
                .split("!")[0]
                .split("~")[0]
                .strip()
            )
            explicit_deps.add(pkg_name.lower())

    return explicit_deps


def analyze_package_sizes(
    lock_data: Dict[str, Any], explicit_deps: Set[str]
) -> List[Dict[str, Any]]:
    """Extract package information and estimate sizes from lock data."""
    packages = []
    for package in lock_data.get("package", []):
        pkg_name = package.get("name", "").lower()

        # Skip the main project package
        if pkg_name == "ai-coding":
            continue

        # Get size from wheels (preferred) or sdist
        size = 0
        wheels = package.get("wheels", [])
        if wheels and len(wheels) > 0:
            # Use the first wheel's size as reference
            size = wheels[0].get("size", 0)
        else:
            # Fallback to sdist size if no wheels
            sdist = package.get("sdist", {})
            size = sdist.get("size", 0) if sdist else 0

        # Extract dependencies
        dependencies = set()
        for dep in package.get("dependencies", []):
            dep_name = dep.get("name", "").lower()
            if dep_name:
                dependencies.add(dep_name)

        pkg_info = {
            "name": package.get("name"),
            "version": package.get("version"),
            "size": size,
            "dependencies": dependencies,
            "is_explicit": pkg_name
            in explicit_deps,  # Track if this is an explicit dep
        }
        packages.append(pkg_info)
    return sorted(packages, key=lambda x: x["size"], reverse=True)


def build_dependency_graph(packages: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Build a dependency graph from package list."""
    pkg_names = {p["name"].lower() for p in packages}
    dep_graph = {}

    for pkg in packages:
        pkg_name = pkg["name"].lower()
        # Only include dependencies that are in our package list
        valid_deps = pkg["dependencies"] & pkg_names
        dep_graph[pkg_name] = valid_deps

    return dep_graph


def get_related_packages(
    packages: List[Dict[str, Any]], explicit_deps: Set[str]
) -> Set[str]:
    """Find all packages related to explicit dependencies."""
    dep_graph = build_dependency_graph(packages)

    # Start with explicit dependencies
    related = set()
    to_process = set()

    # Find explicit deps that exist in our package list
    for pkg in packages:
        pkg_name = pkg["name"].lower()
        if pkg_name in explicit_deps:
            to_process.add(pkg_name)
            related.add(pkg_name)

    # BFS to find all transitive dependencies
    while to_process:
        current = to_process.pop()
        deps = dep_graph.get(current, set())
        for dep in deps:
            if dep not in related:
                related.add(dep)
                to_process.add(dep)

    return related


def split_into_size_based_groups(
    packages: List[Dict[str, Any]],
    explicit_deps: Set[str],
    size_threshold_mb: int = 500,
) -> List[List[Dict[str, Any]]]:
    """Split packages into groups based on size threshold while respecting dependencies.
    If A depends on B, A cannot be in a group without B (B must be in same or earlier group).
    Each group's total size should not exceed the threshold.
    Each group must contain at least one explicit dependency."""
    if not packages:
        return []

    # Check if total size exceeds threshold
    total_size = sum(p["size"] for p in packages)
    size_threshold_bytes = size_threshold_mb * 1024 * 1024

    # If total size doesn't exceed threshold, no need to split
    if total_size <= size_threshold_bytes:
        return [packages]

    # Build dependency graph
    dep_graph = build_dependency_graph(packages)

    # Separate explicit deps and transitive deps
    explicit_packages = [p for p in packages if p["is_explicit"]]
    transitive_packages = [p for p in packages if not p["is_explicit"]]

    # Track which packages have been assigned to groups
    assigned = set()
    groups = []

    # Sort explicit packages by size (largest first)
    explicit_packages.sort(key=lambda x: x["size"], reverse=True)

    # Create groups, each starting with at least one explicit dependency
    for explicit_pkg in explicit_packages:
        pkg_name = explicit_pkg["name"].lower()
        if pkg_name in assigned:
            continue

        current_group = [explicit_pkg]
        current_size = explicit_pkg["size"]
        assigned.add(pkg_name)

        # Find transitive deps that can be added to this group
        candidates = []
        for pkg in transitive_packages:
            p_name = pkg["name"].lower()
            if p_name in assigned:
                continue

            deps = dep_graph.get(p_name, set())
            # Check if all dependencies are already assigned
            if deps <= assigned:
                candidates.append(pkg)

        # Sort candidates by size (largest first to fill groups efficiently)
        candidates.sort(key=lambda x: x["size"], reverse=True)

        # Add candidates to current group while staying under threshold
        for pkg in candidates:
            pkg_size = pkg["size"]
            if current_size + pkg_size <= size_threshold_bytes:
                current_group.append(pkg)
                current_size += pkg_size
                assigned.add(pkg["name"].lower())

        groups.append(current_group)

    # Handle any remaining unassigned packages
    remaining = [p for p in packages if p["name"].lower() not in assigned]
    if remaining:
        # Add remaining packages to the last group or create a new one
        if groups:
            # Try to add to existing groups
            for pkg in remaining:
                added = False
                for group in groups:
                    group_size = sum(p["size"] for p in group)
                    if group_size + pkg["size"] <= size_threshold_bytes:
                        group.append(pkg)
                        added = True
                        break
                if not added:
                    # Create new group for remaining package
                    groups.append([pkg])
        else:
            groups.append(remaining)

    return groups


def main():
    """Main entry point to analyze and group dependencies."""
    try:
        lock_data = load_uv_lock()
        explicit_deps = load_pyproject_toml()
        packages = analyze_package_sizes(lock_data, explicit_deps)

        # Filter to only include packages related to explicit dependencies
        related_pkg_names = get_related_packages(packages, explicit_deps)
        related_packages = [
            p for p in packages if p["name"].lower() in related_pkg_names
        ]
        remaining_packages = [
            p for p in packages if p["name"].lower() not in related_pkg_names
        ]

        groups = split_into_size_based_groups(
            related_packages, explicit_deps, size_threshold_mb=500
        )

        print(f"Total packages: {len(packages)}")
        print(f"Explicit dependencies (from pyproject.toml): {len(explicit_deps)}")
        print(f"Related packages (in dependency tree): {len(related_pkg_names)}")
        print(
            f"Remaining packages (not in dependency tree): "
            f"{len(remaining_packages)}"
        )
        print(f"Split into {len(groups)} groups for isolated installation.")
        print()

        for i, group in enumerate(groups):
            # Separate explicit deps and transitive deps
            explicit_in_group = [p for p in group if p["is_explicit"]]
            transitive_in_group = [p for p in group if not p["is_explicit"]]

            group_size_mb = sum(p["size"] for p in group) / (1024 * 1024)

            print("=" * 60)
            print(f"Group {i+1} (Size: {group_size_mb:.2f} MB)")
            print("=" * 60)

            # Part 1: Explicit dependencies from pyproject.toml
            print("\n[Part 1] Explicit Dependencies (from pyproject.toml):")
            if explicit_in_group:
                for p in explicit_in_group:
                    print(f"  - {p['name']}=={p['version']} ({p['size']/1024:.2f} KB)")
            else:
                print("  (none - ERROR: this should not happen)")

            # Part 2: All transitive dependencies from uv.lock
            print("\n[Part 2] Transitive Dependencies (from uv.lock):")
            if transitive_in_group:
                for p in transitive_in_group:
                    print(f"  - {p['name']}=={p['version']} ({p['size']/1024:.2f} KB)")
            else:
                print("  (none)")

            print()

        # Print remaining packages if any
        if remaining_packages:
            print("=" * 60)
            print("Remaining Packages (not dependencies of pyproject.toml)")
            print("=" * 60)
            remaining_size_mb = sum(p["size"] for p in remaining_packages) / (
                1024 * 1024
            )
            print(f"Total Size: {remaining_size_mb:.2f} MB")
            for p in remaining_packages:
                print(f"  - {p['name']}=={p['version']} ({p['size']/1024:.2f} KB)")
            print()

    except Exception as e:
        print(f"Error analyzing package size: {e}")


if __name__ == "__main__":
    main()
