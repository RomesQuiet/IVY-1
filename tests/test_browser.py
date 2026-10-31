"""Ivy's browser, driving headless Brave on a throwaway profile against a local test site."""

import http.server
import subprocess
import threading
import time
import unittest
from pathlib import Path

from tests.support import call, fake_pool, FakeModel, say
from ivy import browser, config, toolkit
from ivy.brain import Brain

PAGES = {
    "/": """<title>Shop</title><h1>Welcome</h1><a href="/form">Contact us</a>
        <a href="/new" target="_blank">Open new tab</a><button onclick="document.title='Pressed'">Press me</button>
        <button style="display:none">Hidden thing</button><button>Buy now</button><button>Delete account</button>""",
    "/form": """<title>Form</title><form action="/thanks"><input name="fullname" placeholder="Your name">
        <select name="city"><option>Lisbon</option><option>Porto</option></select>
        <input type="password" name="pw" placeholder="Password"><input name="cardnumber" placeholder="Card">
        <button>Continue</button></form>""",
    "/thanks": "<title>Thanks</title>Thanks for your message",
    "/new": "<title>New tab page</title>Second tab",
}


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "<title>404</title>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(any(Path(b).exists() for b in browser._BINARIES), "needs Brave or Chrome")
class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        browser.close()
        cls.server.shutdown()
        time.sleep(1)
        left = subprocess.run(["pgrep", "-f", f"remote-debugging-port={config.BROWSER_PORT}"], capture_output=True)
        assert not left.stdout, "the test browser was left running"

    def open(self, path="/"):
        return toolkit.run("browser_open", {"url": self.base + path})

    def ref(self, label):
        page = toolkit.run("browser_read", {})
        for line in page.splitlines():
            if label in line and line.startswith("["):
                return int(line[1:line.index("]")])
        self.fail(f"{label} not in: {page[:400]}")

    def test_opens_and_lists_controls(self):
        self.assertIn('"Shop"', self.open())
        page = toolkit.run("browser_read", {})
        self.assertIn('button "Press me"', page)
        self.assertNotIn("Hidden thing", page)

    def test_click_works_and_reports_what_happened(self):
        self.open()
        self.assertIn("didn't change", toolkit.run("browser_click", {"ref": self.ref("Press me")}))
        self.assertIn("Pressed", toolkit.run("browser_read", {}))
        self.assertIn("/form", toolkit.run("browser_click", {"ref": self.ref("Contact us")}))

    def test_follows_a_new_tab(self):
        self.open()
        self.assertIn("New tab page", toolkit.run("browser_click", {"ref": self.ref("Open new tab")}))

    def test_fills_and_submits_a_form(self):
        self.open("/form")
        self.assertIn("holds", toolkit.run("browser_type", {"ref": self.ref("Your name"), "text": "Romeu"}))
        self.assertIn("Porto", toolkit.run("browser_select", {"ref": self.ref("select"), "option": "porto"}))
        result = toolkit.run("browser_type", {"ref": self.ref("Your name"), "text": "Romeu V", "submit": True})
        self.assertIn("Thanks", result)

    def test_never_types_passwords_or_cards(self):
        self.open("/form")
        for label in ("Password", "Card"):
            ref = self.ref(label)
            self.assertIn("NOT DONE", toolkit.run("browser_type", {"ref": ref, "text": "secret"}))
        self.assertEqual(browser._tab().run("document.querySelector('[name=pw]').value"), "")

    def test_stale_numbers_are_reported(self):
        self.open()
        toolkit.run("browser_read", {})
        self.open("/thanks")
        self.assertIn("isn't on the page", toolkit.run("browser_click", {"ref": 1}))

    def test_local_files_cant_be_opened(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"):
            self.assertIn("NOT DONE", toolkit.run("browser_open", {"url": url}))

    def test_buying_always_asks_even_when_approved(self):
        self.open()
        asked = []
        b = Brain(confirm=lambda title, detail: (asked.append(title), False)[1])
        b.actions_approved = True
        tool = toolkit.TOOLS["browser_click"]
        for label in ("Buy now", "Delete account"):
            self.assertFalse(b._allowed(tool, {"ref": self.ref(label)}))
        self.assertTrue(b._allowed(tool, {"ref": self.ref("Press me")}))
        self.assertEqual(len(asked), 2)

    def test_agent_loop_through_the_real_browser(self):
        model = FakeModel([call("browser_open", url=self.base + "/form"), call("browser_read"), say("It's a form.")])
        fake_pool(main=model, fast=model)
        self.assertEqual(Brain(confirm=lambda t, d: False).chat("what does this website form ask for"), "It's a form.")
        self.assertIn("Your name", model.requests[2]["messages"][-1]["content"])

    def test_tasks_cant_use_it(self):
        b = Brain(background=True)
        names = {s["function"]["name"] for s in b._schemas({"browser"})}
        self.assertFalse(any(n.startswith("browser_") for n in names))
