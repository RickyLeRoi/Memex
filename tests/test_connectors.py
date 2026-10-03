import imaplib
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import httpx

from digest.config import Config, GmailConfig, JiraConfig, SlackConfig
from digest.sources.gmail import fetch_gmail, parse_message
from digest.sources.jira import JiraClient, adf_to_text, build_jql, fetch_jira
from digest.sources.slack import SlackClient, SlackError, clean_text, fetch_slack
from digest.state import State

SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
TS_OLD = "1790000000.000100"
TS_NEW = str(SINCE.timestamp() + 3600) + "00"


def json_ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **payload})


class TempState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = State(self.tmp / "state.sqlite")

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()


class SlackTests(TempState):
    def client(self, history: list[dict]) -> SlackClient:
        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            if method == "conversations.list":
                return json_ok({"channels": [{"id": "C1", "name": "support"}, {"id": "C2", "name": "random"}]})
            if method == "conversations.history":
                return json_ok({"messages": history})
            if method == "users.info":
                return json_ok({"user": {"real_name": "Mario Rossi"}})
            if method == "chat.getPermalink":
                return json_ok({"permalink": "https://x.slack.com/archives/C1/p1"})
            return httpx.Response(404)

        return SlackClient("xoxp-test", httpx.Client(transport=httpx.MockTransport(handler)))

    def config(self, **slack) -> Config:
        return Config(base_dir=self.tmp, slack=SlackConfig(enabled=True, token="t", **slack))

    def test_builds_one_doc_per_channel_with_msg_ids(self):
        history = [{"ts": "1791000000.000200", "user": "U1", "text": "Server down <@U2>"}]
        result = fetch_slack(self.client(history), self.config(channels=["#support"]), self.state, SINCE)
        self.assertEqual(len(result.docs), 1)
        doc = result.docs[0]
        self.assertEqual(doc.source, "slack")
        self.assertIn("Mario Rossi: Server down @Mario Rossi", doc.text)
        self.assertEqual(doc.meta["msg_ids"], ["C1:1791000000.000200"])
        self.assertEqual(doc.url, "https://x.slack.com/archives/C1/p1")

    def test_ticket_source_reads_ticket_channels_only(self):
        history = [{"ts": "1791000000.000200", "user": "U1", "text": "T-1 blocked"}]
        cfg = self.config(channels=["random"], ticket_channels=["support"])
        result = fetch_slack(self.client(history), cfg, self.state, SINCE, source="slack_tickets")
        self.assertEqual([d.title for d in result.docs], ["support"])
        self.assertEqual(result.docs[0].source, "slack_tickets")

    def test_seen_messages_and_join_events_are_skipped(self):
        self.state.mark_seen("slack", [("C1:1791000000.000200", None)])
        history = [
            {"ts": "1791000000.000200", "user": "U1", "text": "old"},
            {"ts": "1791000001.000200", "user": "U1", "text": "joined", "subtype": "channel_join"},
        ]
        result = fetch_slack(self.client(history), self.config(channels=["support"]), self.state, SINCE)
        self.assertEqual(result.docs, [])

    def test_api_error_is_raised(self):
        client = SlackClient("x", httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))))
        with self.assertRaises(SlackError):
            client.call("auth.test")

    def test_clean_text_unwraps_links(self):
        client = self.client([])
        self.assertEqual(clean_text(client, "<https://a.io|docs> &amp; more"), "docs (https://a.io) & more")


class JiraTests(TempState):
    ISSUE = {
        "key": "OPS-7",
        "fields": {
            "summary": "Fix login", "status": {"name": "In Progress"}, "issuetype": {"name": "Bug"},
            "priority": {"name": "High"}, "assignee": {"displayName": "Riccardo"}, "reporter": None,
            "duedate": "2026-10-10", "labels": ["prod"], "updated": "2026-10-02T10:00:00.000+0000",
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Users cannot log in"}]}]},
            "comment": {"comments": [{"created": "2026-10-02T09:00:00.000+0000", "author": {"displayName": "Anna"},
                                      "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                                          {"type": "text", "text": "Reproduced"}]}]}}]},
        },
    }

    def cfg(self, **jira) -> Config:
        return Config(base_dir=self.tmp, jira=JiraConfig(
            enabled=True, base_url="https://acme.atlassian.net", email="e", api_token="t", **jira))

    def client(self, seen: list[httpx.Request]) -> JiraClient:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"issues": [self.ISSUE], "isLast": True})

        return JiraClient("https://acme.atlassian.net", "e", "t", httpx.Client(transport=httpx.MockTransport(handler)))

    def test_issue_becomes_doc_with_description_and_comments(self):
        seen: list[httpx.Request] = []
        result = fetch_jira(self.client(seen), self.cfg(), self.state, SINCE)
        doc = result.docs[0]
        self.assertEqual(doc.id, "OPS-7")
        self.assertIn("Users cannot log in", doc.text)
        self.assertIn("Anna: Reproduced", doc.text)
        self.assertEqual(doc.url, "https://acme.atlassian.net/browse/OPS-7")
        self.assertIn("/rest/api/3/search/jql", str(seen[0].url))

    def test_unchanged_issue_is_skipped_by_fingerprint(self):
        first = fetch_jira(self.client([]), self.cfg(), self.state, SINCE).docs[0]
        self.state.mark_seen("jira", [(first.id, first.meta["fp"])])
        self.assertEqual(fetch_jira(self.client([]), self.cfg(), self.state, SINCE).docs, [])

    def test_jql_combines_projects_and_custom_clause(self):
        jql = build_jql(self.cfg(projects=["OPS", "DEV"], jql="assignee = currentUser()"), SINCE)
        self.assertIn('project in ("OPS", "DEV")', jql)
        self.assertIn("(assignee = currentUser())", jql)
        self.assertTrue(jql.endswith("ORDER BY updated ASC"))

    def test_adf_lists_are_flattened(self):
        node = {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]}
        self.assertIn("- one", adf_to_text(node))


class FakeImap:
    def __init__(self, raw_messages: list[bytes], login_ok: bool = True):
        self.raw = raw_messages
        self.login_ok = login_ok
        self.opened_readonly: bool | None = None

    def login(self, user, password):
        if not self.login_ok:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        self.opened_readonly = readonly
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raw)))]

    def fetch(self, num, spec):
        return "OK", [(b"1 (BODY[] {n}", self.raw[int(num) - 1]), b")"]

    def logout(self):
        pass


def make_mail(sender: str, subject: str, body: str, date: str = "Thu, 02 Oct 2026 10:00:00 +0000") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, "me@gmail.com", subject, date
    msg["Message-ID"] = f"<{subject.replace(' ', '')}@mail>"
    msg.set_content(body)
    return msg.as_bytes()


class GmailTests(TempState):
    def cfg(self) -> Config:
        return Config(base_dir=self.tmp, gmail=GmailConfig(enabled=True, user="me@gmail.com", app_password="x"))

    def test_fetch_builds_docs_skips_automated_senders_and_is_read_only(self):
        imap = FakeImap([
            make_mail("Anna <anna@acme.com>", "Budget", "Please approve the budget by Friday."),
            make_mail("no-reply@spam.com", "Promo", "Buy now"),
        ])
        result = fetch_gmail(self.cfg(), self.state, SINCE, connection=imap)
        self.assertEqual([d.title for d in result.docs], ["Budget"])
        self.assertIn("approve the budget", result.docs[0].text)
        self.assertEqual(len(result.skipped_ids), 1)
        self.assertTrue(imap.opened_readonly)

    def test_seen_message_is_not_returned_again(self):
        raw = make_mail("anna@acme.com", "Budget", "Hello")
        message_id, _, _ = parse_message(raw, self.cfg())
        self.state.mark_seen("gmail", [(message_id, None)])
        self.assertEqual(fetch_gmail(self.cfg(), self.state, SINCE, connection=FakeImap([raw])).docs, [])

    def test_login_failure_has_actionable_message(self):
        from digest.sources.gmail import GmailError

        with self.assertRaises(GmailError) as ctx:
            fetch_gmail(self.cfg(), self.state, SINCE, connection=FakeImap([], login_ok=False))
        self.assertIn("app password", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
