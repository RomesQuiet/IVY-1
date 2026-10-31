"""Each part on its own: tools, router, senses, voice, wake, watcher, context, skills, MCP, UI."""

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from tests.support import Blocked, ROOT
from ivy import audio, config, context, mcp_client, router, senses, skills, toolkit, tools, tools_life, tools_media, watcher


class SafetyNet(unittest.TestCase):
    """If these fail, stop: the other tests could touch the real Mac."""

    def test_commands_are_blocked(self):
        for command in (["open", "-a", "Safari"], ["osascript", "-e", "beep"], ["say", "hi"], ["rm", "-rf", "/tmp/x"]):
            with self.assertRaises(Blocked):
                subprocess.run(command)

    def test_volume_and_browser_are_blocked(self):
        with self.assertRaises(Blocked):
            senses.set_volume(100)
        with self.assertRaises(Blocked):
            tools.open_in_browser("https://example.com")

    def test_real_memory_is_not_used(self):
        from ivy.memory import get_store
        self.assertNotEqual(get_store().path, ROOT / "memory.json")


class Tools(unittest.TestCase):
    def test_calculator(self):
        self.assertEqual(toolkit.run("calculate", {"expression": "2^10"}), "2^10 = 1024")
        self.assertIn("399.5", toolkit.run("calculate", {"expression": "0.17*2350"}))
        self.assertNotIn("000000", toolkit.run("calculate", {"expression": "0.17*2350"}))

    def test_calculator_cant_be_escaped(self):
        self.assertIn("failed", toolkit.run("calculate", {"expression": "().__class__.__base__.__subclasses__()"}))

    def test_runner_never_raises(self):
        self.assertIn("Missing required", toolkit.run("calculate", {}))
        self.assertIn("Unknown tool", toolkit.run("no_such_tool", {}))
        self.assertIn("failed", toolkit.run("read_file", {"path": "/no/such/file"}))

    def test_runner_coerces_and_drops_arguments(self):
        result = toolkit.run("calculate", {"expression": "1+1", "invented": True})
        self.assertIn("2", result)

    def test_slow_tool_times_out(self):
        toolkit.TOOLS["_slow"] = toolkit.Tool("_slow", lambda: time.sleep(3), {"function": {"parameters": {}}},
                                              "core", None, None, 0.1)
        try:
            self.assertIn("gave up", toolkit.run("_slow", {}))
        finally:
            del toolkit.TOOLS["_slow"]

    def test_long_results_are_cut(self):
        toolkit.TOOLS["_big"] = toolkit.Tool("_big", lambda: "x" * 9000, {"function": {"parameters": {}}},
                                             "core", None, None, 5)
        try:
            self.assertLess(len(toolkit.run("_big", {})), toolkit.MAX_RESULT_CHARS + 100)
        finally:
            del toolkit.TOOLS["_big"]

    def test_core_tools_stay_small(self):
        # every schema is re-sent each round against an 8k-per-minute limit
        self.assertLess(len(json.dumps(toolkit.schemas(set()))), 11000)

    def test_forget_all_works(self):
        # used to crash on a missing MemoryStore.count()
        from ivy.memory import get_store
        get_store().add("temporary fact")
        self.assertIn("Cleared", toolkit.run("forget_all", {}))
        self.assertEqual(get_store().all(), [])

    def test_launch_program_keeps_running_or_reports_the_crash(self):
        import tests.support as support
        support._ALLOWED_COMMANDS.add("zsh")
        try:
            running = toolkit.run("launch_program", {"command": "sleep 4"})
            self.assertIn("Running", running)
            crashed = toolkit.run("launch_program", {"command": "echo boom; exit 3"})
            self.assertIn("boom", crashed)
            self.assertIn("exit 3", crashed)
        finally:
            support._ALLOWED_COMMANDS.discard("zsh")

    def test_applescript_quoting(self):
        self.assertEqual(tools._as_str('a"b\\c'), '"a\\"b\\\\c"')

    def test_file_tools(self):
        with tempfile.TemporaryDirectory() as d:
            f = f"{d}/n.txt"
            toolkit.run("write_file", {"path": f, "content": "alpha\nbeta\n"})
            self.assertIn("Edited", toolkit.run("edit_file", {"path": f, "old": "beta", "new": "BETA"}))
            self.assertIn("Not changed", toolkit.run("edit_file", {"path": f, "old": "a", "new": "z"}))
            self.assertIn("2 BETA", toolkit.run("read_file", {"path": f}))

    def test_applescripts_compile(self):
        sent = []
        saved = tools_life._osa
        tools_life._osa = lambda s, timeout=15: sent.append(s) or "x: +351 900 000 000  "
        try:
            tools_life.find_contact("Mom")
            tools_life.send_imessage("+351900000000", "hi")
            tools_life.send_email("a@b.com", "s", "b")
            tools_life.reply_email("thanks")
            tools_life.unread_email()
            tools_life.calendar_events(2)
            tools_life.add_calendar_event("Dentist", "2026-11-03 15:30", 45)
            tools_life.add_reminder("call", "2026-11-01 10:00")
            tools_life.list_reminders()
            tools_life.create_note("T", "body")
        finally:
            tools_life._osa = saved
        sent += [context.MAIL_SCRIPT, context.FINDER_SCRIPT, context.SELECTION_SCRIPT.replace("{app}", "Notes"),
                 watcher.CALENDAR_SCRIPT.format(minutes=10)]
        for script in sent:
            with tempfile.NamedTemporaryFile(suffix=".scpt") as f:
                r = subprocess.run(["osacompile", "-o", f.name, "-e", script], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, script + r.stderr)


class Router(unittest.TestCase):
    def test_durations(self):
        for text, seconds in [("2m52.8s", 172.8), ("922ms", 0.922), ("50.467s", 50.467), ("1h2m3s", 3723)]:
            self.assertAlmostEqual(router.parse_duration(text), seconds, places=3)

    def endpoint(self, tier="main"):
        return router.Endpoint("groq", f"m/{tier}", tier, None, 8000, {})

    def test_budget_refills_continuously(self):
        e = self.endpoint()
        e.remaining, e.seen_at = 0, time.time() - 30
        self.assertAlmostEqual(e.budget(time.time()), 4000, delta=50)

    def test_picks_model_with_budget_then_waits(self):
        pool = router.ModelPool.__new__(router.ModelPool)
        main, fast = self.endpoint("main"), self.endpoint("fast")
        pool.endpoints = [fast, main]
        now = time.time()
        main.remaining, main.seen_at = 100, now
        self.assertIs(pool.pick("medium", 3000)[0], fast)
        fast.remaining, fast.seen_at = 100, now
        endpoint, wait, why = pool.pick("medium", 3000)
        self.assertGreater(wait, 0)
        self.assertEqual(why, "budget")

    def test_nothing_goes_out_while_blocked_even_if_our_estimate_is_low(self):
        e = self.endpoint()
        pool = router.ModelPool.__new__(router.ModelPool)
        pool.endpoints = [e]
        pool.rate_limited(e, "Limit 8000, Requested 7900. Please try again in 5s.")
        self.assertGreater(pool.pick("medium", 100)[1], 4)

    def test_daily_limit_benches(self):
        pool = router.ModelPool.__new__(router.ModelPool)
        e = self.endpoint()
        pool.endpoints = [e]
        pool.rate_limited(e, "Please try again in 21m36.5s")
        self.assertGreater(e.benched_until, time.time())

    def test_too_big_for_every_model(self):
        pool = router.ModelPool.__new__(router.ModelPool)
        pool.endpoints = [self.endpoint()]
        self.assertEqual(pool.pick("medium", 9000)[2], "too_big")

    def test_configured_models_arent_the_removed_one(self):
        self.assertNotIn("qwen3.6", json.dumps(config.PROVIDERS))


class Senses(unittest.TestCase):
    BLOCKS = [{"text": "Sign in", "x": 100, "y": 50, "w": 60}, {"text": "Play Queen on Spotify", "x": 300, "y": 90, "w": 210}]

    def test_find_text_exact_then_partial(self):
        self.assertEqual(senses.find_text(self.BLOCKS, "sign in")["x"], 100)
        self.assertIsNone(senses.find_text(self.BLOCKS, "subscribe"))

    def test_find_text_aims_inside_a_line(self):
        self.assertLess(senses.find_text(self.BLOCKS, "Queen")["x"], 300)

    def test_ocr_reads_and_positions_text(self):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (1680, 1050), (28, 28, 30))
        ImageDraw.Draw(img).text((800, 400), "Subscribe to GothamChess",
                                 fill=(240, 240, 240), font=ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28))
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            img.save(f.name)
            blocks = senses.ocr(Path(f.name), {"x": 0, "y": 0, "w": 1680, "h": 1050, "app": "t"})
        hit = senses.find_text(blocks, "Subscribe to GothamChess")
        self.assertIsNotNone(hit)
        self.assertLess(abs(hit["y"] - 415), 30)

    def test_blind_is_reported_not_faked(self):
        saved = senses.permissions
        senses.permissions = lambda: {"screen": False, "accessibility": False}
        try:
            self.assertIn("Screen Recording", toolkit.run("read_screen", {}))
        finally:
            senses.permissions = saved


class Voice(unittest.TestCase):
    def test_exit_words(self):
        from ivy import voice
        for text in ("goodbye", "Stop.", "sleep", "standby", "stop listening", "goodbye Ivy"):
            self.assertTrue(voice.should_exit_voice(text), text)
        self.assertFalse(voice.should_exit_voice("stop the music and play jazz please now ok"))

    def test_short_exit_words_survive_transcription(self):
        # the "too short" filter silently ate them
        from types import SimpleNamespace as NS
        from ivy import voice
        saved = voice._client
        with tempfile.NamedTemporaryFile() as f:
            try:
                for heard, kept in [("Goodbye.", True), ("Stop.", True), ("Bye.", False), ("um", False), ("Hi.", False)]:
                    voice._client = NS(audio=NS(transcriptions=NS(create=lambda **k: NS(text=heard))))
                    self.assertEqual(bool(voice.transcribe(f.name)), kept, heard)
            finally:
                voice._client = saved

    def test_sentence_splitter(self):
        from ivy.brain import _split_sentences
        self.assertEqual(_split_sentences("One. Two"), (["One."], "Two"))
        self.assertEqual(_split_sentences("It costs 18.5 euros"), ([], "It costs 18.5 euros"))


class Wake(unittest.TestCase):
    def listen(self, frames):
        from ivy import wake

        class Stream:
            def __enter__(s): return s
            def __exit__(s, *a): pass
            def read(s, n):
                frame = next(frames, None)
                if frame is None:
                    audio.abort_wake()
                    return np.zeros((n, 1), np.int16), False
                return frame.reshape(-1, 1), False

        saved = wake.sd.InputStream
        wake.sd.InputStream = lambda **kw: Stream()
        try:
            # below the ~0.73 silence scores, so this tests the guard, not the current threshold
            return wake.WakeListener(config.WAKE_WORD, 0.5).wait_for_wake()
        finally:
            wake.sd.InputStream = saved

    def test_digital_silence_never_wakes(self):
        # the model scores all-zero audio ~0.73, which is what a denied mic delivers
        self.assertFalse(self.listen(iter([np.zeros(1280, np.int16)] * 60)))

    def test_room_noise_doesnt_wake(self):
        rng = np.random.default_rng(1)
        self.assertFalse(self.listen(iter([rng.normal(0, 200, 1280).astype(np.int16) for _ in range(50)])))

    def test_force_wake(self):
        audio.request_force_wake()
        self.assertTrue(self.listen(iter([])))


class Watcher(unittest.TestCase):
    BATT = "Now drawing from 'Battery Power'\n -InternalBattery-0\t%d%%; %s; 1:48 remaining"

    def setUp(self):
        watcher._battery_warned.clear()
        watcher._announced_events.clear()
        watcher._partials.clear()

    def test_battery_warns_once_per_threshold(self):
        seen = [watcher.battery_alert(self.BATT % r) for r in
                [(25, "discharging"), (20, "discharging"), (19, "discharging"), (50, "charging"), (20, "discharging")]]
        self.assertEqual([bool(s) for s in seen], [False, True, False, False, True])

    def test_calendar_doesnt_repeat(self):
        self.assertEqual(len(watcher.calendar_alerts("540|Dentist\n900|Later\nbad\n-5|Past", 10)), 1)
        self.assertEqual(watcher.calendar_alerts("535|Dentist", 10), [])

    def test_only_finished_downloads_count(self):
        watcher.finished_downloads({"movie.mp4.crdownload", "x.zip.part"})
        self.assertEqual(watcher.finished_downloads({"movie.mp4"}), ["movie.mp4"])     # x.zip was cancelled

    def test_quiet_hours_wrap_midnight(self):
        self.assertEqual([watcher.in_quiet_hours(h) for h in (22, 23, 2, 7)], [False, True, True, False])

    def test_failing_rule_stops_itself(self):
        rule = watcher.Rule(lambda: 1 / 0, 1)
        saved = dict(watcher._rules)
        watcher._rules.clear()
        watcher._rules["t.boom"] = rule
        try:
            for i in range(6):
                watcher.run_due(lambda m: None, now=time.time() + i * 2)
            self.assertEqual(rule.failures, 3)
        finally:
            watcher._rules.clear()
            watcher._rules.update(saved)


class Context(unittest.TestCase):
    STATE = {"front": "Brave Browser", "title": "T", "tab": "Tab", "url": "https://example.com/a"}

    def setUp(self):
        self.saved = context._script, senses.read_text, senses._running

    def tearDown(self):
        context._script, senses.read_text, senses._running = self.saved

    def test_when_context_is_wanted(self):
        for text in ("summarise this page", "explain this", "reply to this email", "translate this to Portuguese",
                     "what am I looking at", "tldr", "fix this code", "what does this error mean?"):
            self.assertTrue(context.wanted(text), text)
        for text in ("what's the weather", "hi how are you", "is this a good idea to learn python"):
            self.assertFalse(context.wanted(text), text)

    def test_selection_beats_the_page(self):
        context._script = lambda src, t=5: "picked" if "AXSelectedText" in src else "PAGE"
        self.assertEqual(context.gather("explain this", self.STATE)["source"], "selected text")

    def test_page_when_nothing_selected(self):
        context._script = lambda src, t=5: "" if "AXSelected" in src else "PAGE"
        self.assertEqual(context.gather("explain this", self.STATE)["text"], "PAGE")

    def test_email_request_prefers_mail(self):
        context._script = lambda src, t=5: "From: a" if 'application "Mail"' in src else ""
        senses._running = lambda p: True
        self.assertIn("Mail", context.gather("reply to this email", self.STATE)["source"])

    def test_falls_back_to_the_window(self):
        context._script = lambda src, t=5: ""
        senses.read_text = lambda app: ([{"text": "visible"}], {})
        found = context.gather("explain this", {"front": "Notes", "title": "", "tab": "", "url": ""})
        self.assertIn("window", found["source"])

    def test_old_context_shrinks_and_large_context_is_cut(self):
        rendered = context.render({"source": "s", "app": "a", "title": "", "text": "z" * 9000})
        self.assertLess(len(rendered), context.MAX_CHARS + 500)
        self.assertLess(len(context.shorten("hi" + rendered)), 2000)
        self.assertEqual(context.shorten("plain"), "plain")


class Skills(unittest.TestCase):
    GOOD = 'from ivy.toolkit import tool\n\n@tool()\ndef t_hello() -> str:\n    "Says hi."\n    return "hi"\n'

    def test_learn_use_and_replace(self):
        self.assertIn("t_hello", toolkit.run("create_skill", {"name": "t_good", "code": self.GOOD}))
        self.assertEqual(toolkit.run("t_hello", {}), "hi")
        skills._unload("t_good")
        (config.SKILLS_DIR / "t_good.py").unlink()
        self.assertNotIn("t_hello", toolkit.TOOLS)

    def test_broken_skill_is_not_kept(self):
        result = toolkit.run("create_skill", {"name": "t_bad", "code": "@tool\n1/0\n"})
        self.assertIn("failed to load", result)
        self.assertFalse((config.SKILLS_DIR / "t_bad.py").exists())

    def test_background_rule_skill(self):
        code = "from ivy.watcher import watch\n\n@watch(every=30)\ndef t_ping():\n    return 'pong'\n"
        self.assertIn("t_ping", toolkit.run("create_skill", {"name": "t_rule", "code": code}))
        skills._unload("t_rule")
        (config.SKILLS_DIR / "t_rule.py").unlink()
        self.assertFalse(any("t_rule" in name for name in watcher._rules))

    def test_real_example_skill_loads(self):
        before = set(toolkit.TOOLS)
        path = ROOT / "skills" / "crypto_prices.py"
        skills._load(path)
        self.assertIn("crypto_prices", toolkit.TOOLS)
        skills._unload("crypto_prices")
        self.assertEqual(set(toolkit.TOOLS) - before, set())


class Mcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.config = Path(cls.dir.name) / "mcp.json"
        cls.config.write_text(json.dumps({"mcpServers": {
            "vault": {"command": sys.executable, "args": [str(ROOT / "tests" / "fake_mcp_server.py")]},
            "broken": {"command": "/nonexistent/server"}}}))
        cls.saved = mcp_client.CONFIG_PATH
        mcp_client.CONFIG_PATH = cls.config
        mcp_client.load_all()

    @classmethod
    def tearDownClass(cls):
        mcp_client.CONFIG_PATH = cls.saved
        for name in [n for n in toolkit.TOOLS if n.startswith("mcp_")]:
            del toolkit.TOOLS[name]
        for name in [g for g in toolkit.GROUPS if g.startswith("mcp_")]:
            del toolkit.GROUPS[name]
        cls.dir.cleanup()

    def test_tools_work_and_metadata_is_dropped(self):
        self.assertIn("heliotrope", toolkit.run("mcp_vault__get_secret", {"topic": "ivy"}))
        schema = toolkit.TOOLS["mcp_vault__get_secret"].schema["function"]["parameters"]
        self.assertNotIn("$schema", schema)

    def test_tool_without_required_list_doesnt_crash(self):
        self.assertIn("disk on fire", toolkit.run("mcp_vault__always_fails", {}))

    def test_a_broken_server_doesnt_stop_the_others(self):
        self.assertTrue(any("broken: failed" in line for line in mcp_client.status()))

    def test_untrusted_and_confirmed_by_default(self):
        t = toolkit.TOOLS["mcp_vault__get_secret"]
        self.assertEqual((t.confirm, t.untrusted), ("action", True))


class Interface(unittest.TestCase):
    HTML = (ROOT / "ivy" / "ui" / "index.html").read_text()
    GUI = (ROOT / "ivy_gui.py").read_text()

    def test_every_page_function_python_calls_exists(self):
        # pywebview._simulate_user silently did nothing because of a wrong name
        for name in set(re.findall(r"window\.(\w+)\(", self.GUI)) - {"evaluate_js", "show", "hide", "destroy", "create_confirmation_dialog"}:
            self.assertRegex(self.HTML, rf"window\.{name}\s*=", name)
        self.assertNotIn("pywebview._", self.GUI)

    def test_every_api_the_page_calls_exists(self):
        for name in set(re.findall(r"pywebview\.api\.(\w+)", self.HTML)):
            self.assertRegex(self.GUI, rf"def {name}\(", name)

    def test_no_old_names_or_colours(self):
        self.assertNotRegex(self.HTML, r"MAE-2|IVY-2|4dd0e1|qwen3\.6")

    def test_launcher_runs_the_real_entry_point(self):
        script = (ROOT / "IVY-1.command").read_text()
        entry = re.search(r"python\S*\s+(\S+\.py)", script).group(1)
        self.assertTrue((ROOT / entry).exists(), entry)

    def test_window_colour_is_always_valid_hex(self):
        # an rgb() value here would stop the app from opening
        import ivy_gui
        saved = ivy_gui.SETTINGS_PATH
        with tempfile.TemporaryDirectory() as d:
            ivy_gui.SETTINGS_PATH = Path(d) / "s.json"
            try:
                for content, expected in [('{"window_bg":"rgb(1,2,3)"}', "#1c1c1e"), ('{"window_bg":"#abcdef"}', "#abcdef"),
                                          ("{not json", "#1c1c1e"), ("[1]", "#1c1c1e")]:
                    ivy_gui.SETTINGS_PATH.write_text(content)
                    self.assertEqual(ivy_gui.window_colour(), expected)
            finally:
                ivy_gui.SETTINGS_PATH = saved

    def test_text_stays_selectable(self):
        self.assertIn("text_select=True", self.GUI)
        self.assertNotRegex(self.HTML, r"user-select:\s*none")

    def test_js_from_the_main_thread_cant_deadlock(self):
        # print() in menu-bar code froze the app: evaluate_js waited on its own thread
        import ivy_gui
        app = ivy_gui.IvyApp.__new__(ivy_gui.IvyApp)
        used = []

        class Window:
            def evaluate_js(self, code):
                used.append(threading.current_thread() is threading.main_thread())
        app.window, app._js_lock = Window(), threading.Lock()
        app._js("1")
        time.sleep(0.2)
        self.assertEqual(used, [False])


class CommandLine(unittest.TestCase):
    def test_starts_and_quits(self):
        r = subprocess.run([sys.executable, str(ROOT / "tests" / "fake_cli.py")], input="/model auto\n/quit\n",
                           capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("Goodbye", r.stdout)
