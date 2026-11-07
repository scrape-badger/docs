"""Reconcile Mintlify source with the live catalog, retaining reversible Git blobs."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def reconcile(root, entries):
    root = root.resolve()
    state_path = root / ".endpoint-lifecycle.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    # Restore only content last written by this reconciler; concurrent editorial changes win.
    for path, record in state.items():
        file = root / path
        current = hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None
        if current != record["generated"]:
            raise RuntimeError(f"Editorial change to managed file {path}; reconcile explicitly")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(git(root, "cat-file", "blob", record["original"]))
    changes = {}

    def write(file, content):
        original = git(root, "hash-object", "-w", str(file)).decode().strip()
        if content is None:
            file.unlink()
        else:
            file.write_bytes(content)
        changes[str(file.relative_to(root))] = {
            "original": original,
            "generated": hashlib.sha256(content).hexdigest() if content is not None else None,
        }

    def identity(method, path):
        return method.upper(), re.sub(r"\{[^}]+\}", "{}", path).rstrip("/")

    statuses = {identity(row["method"], row["path"]): row["status"] for row in entries}
    folders = {}
    known_apis = set()
    for page in root.rglob("*.mdx"):
        match = re.search(
            r'^openapi:\s*["\']?(?:GET|POST|PUT|PATCH|DELETE)\s+/v1/([^/\s]+)',
            page.read_text(),
            re.M,
        )
        if match and len(page.relative_to(root).parts) > 1:
            known_apis.add(match[1])
            for parent in page.relative_to(root).parents:
                if str(parent) != ".":
                    folders.setdefault(str(parent), set()).add(match[1])
    for api in known_apis:
        folders.setdefault(api, set()).add(api)
    folders.setdefault("web-scraping", {"web"})
    folders.setdefault("twitter-streams", {"twitter"})
    visible_apis = {row["api"] if "api" in row else row["path"].split("/")[2] for row in entries}
    removed_folders = {
        folder
        for folder, apis in folders.items()
        if not apis & visible_apis and not apis & {"account"}
    }
    for file in root.rglob("*.mdx"):
        if any(str(file.relative_to(root)).startswith(folder + "/") for folder in removed_folders):
            write(file, None)
            continue
        text = file.read_text()
        match = re.search(
            r'^openapi:\s*["\']?(GET|POST|PUT|PATCH|DELETE)\s+([^\s"\']+)', text, re.M
        )
        if not match or not match[2].startswith("/v1/") or match[2].startswith("/v1/account/"):
            continue
        status = statuses.get(identity(match[1], match[2]), "hidden")
        if status == "hidden":
            write(file, None)
        elif status != "active":
            label = (
                "Deprecated — returns HTTP 410."
                if status == "deprecated"
                else "Temporarily unavailable — returns HTTP 423."
            )
            end = text.find("\n---", 3)
            if end >= 0:
                text = text[: end + 4] + "\n\n<Warning>" + label + "</Warning>\n" + text[end + 4 :]
                write(file, text.encode())
    for file in root.glob("openapi*.json"):
        document = json.loads(file.read_text())
        for path, operations in list(document.get("paths", {}).items()):
            if not path.startswith("/v1/") or path.startswith("/v1/account/"):
                continue
            for method, operation in list(operations.items()):
                if method.upper() not in {
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "HEAD",
                    "OPTIONS",
                }:
                    continue
                status = statuses.get(identity(method, path), "hidden")
                if status == "hidden":
                    del operations[method]
                else:
                    operation["x-availability"] = status
                    if status != "active":
                        operation["description"] = (
                            status.replace("_", " ").capitalize()
                            + ". "
                            + operation.get("description", "")
                        )
                        operation["deprecated"] = status == "deprecated"
            if not any(
                method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
                for method in operations
            ):
                del document["paths"][path]
        components = document.get("components", {})
        seen = set()
        tags = set()

        def visit(value, tags=tags, seen=seen, components=components):
            if isinstance(value, dict):
                tags.update(value.get("tags", []))
                ref = value.get("$ref", "")
                if ref.startswith("#/components/") and ref not in seen:
                    seen.add(ref)
                    parts = ref.split("/")
                    if len(parts) == 4:
                        visit(components.get(parts[2], {}).get(parts[3], {}))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(document.get("paths", {}))
        for group, definitions in components.items():
            if group != "securitySchemes":
                components[group] = {
                    name: value
                    for name, value in definitions.items()
                    if f"#/components/{group}/{name}" in seen
                }
        if "tags" in document:
            document["tags"] = [tag for tag in document["tags"] if tag["name"] in tags]
        content = (json.dumps(document, indent=2) + "\n").encode()
        if content != file.read_bytes():
            write(file, content)
    file = root / "docs.json"
    document = json.loads(file.read_text())

    def navigation(value):
        if isinstance(value, list):
            items = [
                navigation(item)
                for item in value
                if not isinstance(item, str)
                or (root / (item + ".mdx")).exists()
                or item.startswith("http")
            ]
            return [
                item
                for item in items
                if not isinstance(item, dict)
                or all(item.get(key) != [] for key in ("pages", "groups", "tabs", "dropdowns"))
            ]
        if isinstance(value, dict):
            return {
                key: navigation(item)
                if key in {"pages", "groups", "tabs", "dropdowns", "anchors", "navigation"}
                else item
                for key, item in value.items()
            }
        return value

    document["navigation"] = navigation(document["navigation"])
    content = (json.dumps(document, indent=2) + "\n").encode()
    if content != file.read_bytes():
        write(file, content)
    state_path.write_text(json.dumps(changes, indent=2) + "\n")


if __name__ == "__main__":
    with urlopen("https://scrapebadger.com/api/public/endpoint-catalog", timeout=30) as response:
        entries = json.load(response)["endpoints"]
    reconcile(Path(sys.argv[1]), entries)
