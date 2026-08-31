from __future__ import annotations

import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

import yaml

from scripts.sync_reviewing import (
    RULES_PATH,
    classify_eml,
    fetch_message_text,
    load_yaml,
    parse_header,
    search_uids,
    update_reviewing_venues,
)


class ReviewingSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_yaml(RULES_PATH)

    def sample(self, subject: str, body: str, sender: str = "editor@journal.example") -> Path:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = "reviewer@example.com"
        message["Subject"] = subject
        message.set_content(body)
        temp = tempfile.NamedTemporaryFile(suffix=".eml", delete=False)
        temp.write(message.as_bytes())
        temp.close()
        self.addCleanup(Path(temp.name).unlink, missing_ok=True)
        return Path(temp.name)

    def test_invitation_is_not_publishable(self) -> None:
        path = self.sample(
            "Invitation to review for Expert Systems with Applications",
            "Please accept or decline this review request.",
        )
        self.assertEqual(classify_eml(path, self.rules), ("invited", "ESWA"))

    def test_accepted_review_maps_venue(self) -> None:
        path = self.sample(
            "Review assignment confirmed",
            "Thank you for agreeing to review for Expert Systems with Applications.",
        )
        self.assertEqual(classify_eml(path, self.rules), ("accepted", "ESWA"))

    def test_completed_chinese_review_maps_venue(self) -> None:
        path = self.sample(
            "EAAI 审稿意见已提交",
            "感谢您完成审稿。Engineering Applications of Artificial Intelligence",
        )
        self.assertEqual(classify_eml(path, self.rules), ("completed", "EAAI"))

    def test_declined_review_is_not_accepted_from_quoted_invitation(self) -> None:
        path = self.sample(
            "Review invitation declined",
            "You have declined the invitation to review. Original: Please accept or decline.",
        )
        self.assertEqual(classify_eml(path, self.rules), ("rejected", None))

    def test_icic_alias(self) -> None:
        path = self.sample(
            "Thank you for agreeing to review for ICIC 2026",
            "International Conference on Intelligent Computing",
        )
        self.assertEqual(classify_eml(path, self.rules), ("accepted", "ICIC"))

    def test_new_journal_alias(self) -> None:
        path = self.sample(
            "Review submission confirmation - Scientific Reports",
            "We have received your review for Scientific Reports.",
        )
        self.assertEqual(classify_eml(path, self.rules), ("completed", "Scientific Reports"))

    def test_attachments_are_ignored(self) -> None:
        message = EmailMessage()
        message["From"] = "editor@journal.example"
        message["Subject"] = "Editorial message"
        message.set_content("This is not a review confirmation.")
        message.add_attachment(
            b"Thank you for submitting your review for Neurocomputing",
            maintype="application",
            subtype="octet-stream",
            filename="private.txt",
        )
        temp = tempfile.NamedTemporaryFile(suffix=".eml", delete=False)
        temp.write(message.as_bytes())
        temp.close()
        path = Path(temp.name)
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(classify_eml(path, self.rules), (None, None))

    def test_imap_attachment_payload_is_never_requested(self) -> None:
        class FakeIMAP:
            def __init__(self) -> None:
                self.requests: list[str] = []

            def uid(self, command: str, uid: str, query: str):
                self.requests.append(query)
                sections = {
                    "(BODY.PEEK[1.MIME])": b"Content-Type: text/plain; charset=utf-8\r\n",
                    "(BODY.PEEK[1]<0.262144>)": b"Thank you for submitting your review for Neurocomputing.",
                    "(BODY.PEEK[2.MIME])": (
                        b"Content-Type: application/pdf\r\n"
                        b"Content-Disposition: attachment; filename=private.pdf\r\n"
                    ),
                }
                payload = sections.get(query)
                return ("OK", [(b"FETCH", payload)]) if payload is not None else ("OK", [b"NIL"])

        raw_header = b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        record = parse_header(7, raw_header)
        client = FakeIMAP()
        text = fetch_message_text(client, record, 262144, 300000, 20)  # type: ignore[arg-type]
        self.assertIn("submitting your review", text)
        self.assertNotIn("(BODY.PEEK[2.MIME])", client.requests)
        self.assertNotIn("(BODY.PEEK[2]<0.262144>)", client.requests)

    def test_incremental_search_filters_server_range_edge_case(self) -> None:
        class FakeIMAP:
            def uid(self, *args):
                return "OK", [b"9 10 11"]

        self.assertEqual(search_uids(FakeIMAP(), 10), [10, 11])  # type: ignore[arg-type]

    def test_config_update_preserves_review_role_and_appends_only_new(self) -> None:
        original = {
            "reviewing": {
                "role": {"zh": "审稿服务", "en": "Reviewing Service"},
                "venues": ["AAAI"],
            },
            "other": "keep",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(original, allow_unicode=True, sort_keys=False), encoding="utf-8")
            additions = update_reviewing_venues(path, {"AAAI", "ICIC"}, dry_run=False)
            updated = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(additions, ["ICIC"])
        self.assertEqual(updated["reviewing"]["role"], original["reviewing"]["role"])
        self.assertEqual(updated["reviewing"]["venues"], ["AAAI", "ICIC"])
        self.assertEqual(updated["other"], "keep")


if __name__ == "__main__":
    unittest.main()
