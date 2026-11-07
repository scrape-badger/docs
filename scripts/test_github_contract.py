"""Offline GitHub documentation reference and schema checks.

Run: python3 scripts/test_github_contract.py
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "openapi-github.json").read_text())
PATH = "/v1/github/profiles/{username}"
PAGES = ["github/overview", "github/profiles", "api-reference/endpoint/github/get-profile"]


class GitHubDocsContract(unittest.TestCase):
    def test_explicit_operation_and_price(self):
        self.assertEqual(list(SPEC["paths"]), [PATH])
        operation = SPEC["paths"][PATH]["get"]
        self.assertEqual(operation["operationId"], "github_get_profile")
        self.assertEqual(operation["x-credit-cost"], 10)
        self.assertEqual(
            [p["name"] for p in operation["parameters"]],
            ["username", "tab", "year", "page", "next_page_token"],
        )
        self.assertEqual(
            operation["parameters"][1]["schema"]["enum"], ["overview", "repositories", "stars"]
        )
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/GitHubProfileResponse"},
        )

    def test_every_local_reference_resolves(self):
        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    self.assertTrue(value["$ref"].startswith("#/"))
                    target = SPEC
                    for part in value["$ref"][2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(SPEC)

    def test_navigation_openapi_and_page_links(self):
        config = json.loads((ROOT / "docs.json").read_text())
        self.assertIn("openapi-github.json", config["api"]["openapi"])
        tab = next(t for t in config["navigation"]["tabs"] if t["tab"] == "GitHub")
        registered = [p for g in tab["groups"] for p in g["pages"]]
        self.assertEqual(set(registered), set(PAGES))
        for page in PAGES:
            content = (ROOT / (page + ".mdx")).read_text()
            self.assertIn("representative", content.lower())
            self.assertIn("10", content)
            for link in re.findall(r"\]\((/[^)#?]+)(?:[?#][^)]*)?\)", content):
                self.assertTrue((ROOT / (link.lstrip("/") + ".mdx")).exists(), link)
        for page in PAGES[1:]:
            self.assertIn('openapi: "get ' + PATH + '"', (ROOT / (page + ".mdx")).read_text())

    def test_optional_source_sections_and_known_field_types(self):
        components = SPEC["components"]["schemas"]
        root = components["GitHubProfileResponse"]
        self.assertEqual(root["required"], ["profile"])
        self.assertIn("contributions", root["properties"])
        languages = root["properties"]["top_languages"]["anyOf"][0]["items"]
        self.assertEqual(languages["$ref"], "#/components/schemas/GitHubLanguage")
        profile = components["GitHubProfile"]["properties"]
        for field in [
            "pronouns",
            "local_time",
            "utc_offset",
            "description",
            "followers_display_count",
            "created_at",
            "created_utc",
            "archived_at",
            "archived_utc",
        ]:
            self.assertIn(field, profile)
        self.assertIn("from", components["GitHubContributions"]["properties"])
        self.assertIn("years", components["GitHubContributions"]["properties"])
        self.assertIn("count", components["GitHubActivityBlock"]["properties"])


if __name__ == "__main__":
    unittest.main()
