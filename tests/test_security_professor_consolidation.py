from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ORG_ROOT = REPO_ROOT / "organization"
ROLE_ROOT = ORG_ROOT / "roles" / "tech-security"
LEGACY_NAME = "security" + "-professor"


class SecurityProfessorConsolidationTest(unittest.TestCase):
    def test_active_organization_contracts_do_not_reference_legacy_role(self) -> None:
        failures = []
        for pattern in ("*.md", "*.json", "*.yaml", "*.yml"):
            for path in ORG_ROOT.rglob(pattern):
                if LEGACY_NAME in path.read_text(encoding="utf-8"):
                    failures.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual([], failures)

    def test_tech_security_owns_deep_web_review_capability(self) -> None:
        skill = (ROLE_ROOT / "skill.md").read_text(encoding="utf-8")
        checklist = (
            ROLE_ROOT / "references/web-security-review-checklist.md"
        ).read_text(encoding="utf-8")

        for marker in (
            "references/web-security-review-checklist.md",
            "Deep Web Security Review",
            "SEC-ISSUE-*",
            "BOLA",
            "SSRF",
            "AI features",
        ):
            self.assertIn(marker, skill)

        for marker in (
            "IPA 11脆弱性チェックリスト",
            "SQL インジェクション",
            "Server-Side Request Forgery",
            "BOLA",
            "JWT / OAuth",
            "Server Actions",
            "AI 固有のセキュリティ",
            "SEC-ISSUE-{番号}",
        ):
            self.assertIn(marker, checklist)

    def test_tech_security_has_deep_review_evals(self) -> None:
        evals = json.loads(
            (ROLE_ROOT / "evals/evals.json").read_text(encoding="utf-8")
        )
        self.assertEqual("tech-security", evals["skill_name"])
        self.assertGreaterEqual(len(evals["evals"]), 8)

        all_text = json.dumps(evals, ensure_ascii=False)
        for marker in (
            "SQL injection",
            "Server Actions",
            "DNS rebinding",
            "BOLA",
            "JWT",
            "XSS",
            "prompt injection",
            "Security Commit Review",
        ):
            self.assertIn(marker, all_text)

    def test_security_review_corrections_are_preserved(self) -> None:
        checklist = (
            ROLE_ROOT / "references/web-security-review-checklist.md"
        ).read_text(encoding="utf-8")
        evals = (ROLE_ROOT / "evals/evals.json").read_text(encoding="utf-8")

        for marker in (
            "Server Actions はCSRF tokenを使用しない",
            "serverActions.allowedOrigins",
            "IPv4-mapped IPv6",
            "検証済みIPへ接続先を固定",
            "nonce-{per-response-random}",
            "Next.js 15以降",
            "Tool authorization",
            "tagged templateまたはtrusted `Prisma.sql`",
        ):
            self.assertIn(marker, checklist)

        for marker in (
            "do not use CSRF tokens",
            "validated-IP connection pinning",
            "unique per response",
            "audit logging",
            "parameterized Prisma raw-query usage",
        ):
            self.assertIn(marker, evals)

    def test_role_index_tracks_role_owned_artifacts(self) -> None:
        index = json.loads((ORG_ROOT / "role-index.json").read_text(encoding="utf-8"))
        role = next(item for item in index["roles"] if item["role_id"] == "tech-security")
        artifact_paths = {item["path"] for item in role["artifacts"]}
        self.assertIn("organization/roles/tech-security/skill.md", artifact_paths)
        self.assertIn(
            "organization/roles/tech-security/references/web-security-review-checklist.md",
            artifact_paths,
        )
        self.assertIn("organization/roles/tech-security/evals/evals.json", artifact_paths)
        self.assertEqual("repository-native", role["compatibility_source"])
        self.assertEqual("role_authoritative", role["migration_stage"])


if __name__ == "__main__":
    unittest.main()
