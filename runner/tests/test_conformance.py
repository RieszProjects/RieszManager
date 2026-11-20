"""Every registered harness has gone through the whole checklist (drivers/README.md).

HarnessSpec checks its own tables when it is built (every universal command and every mode tier
filled in, methods and modes that exist, a known handoff strategy). This checks the rest, which
can only be seen once the harness is registered: its driver, routes, modes, tests and docs. A new
harness that skips a step fails here, naming the step.
"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path

from riesz_runner import routes
from riesz_runner.credentials import PROVIDERS
from riesz_runner.drivers.base import UNIVERSAL_COMMANDS, Capabilities, Driver, Mode
from riesz_runner.harnesses import HARNESSES, HarnessSpec
from riesz_runner.handoff import STRATEGIES, TARGETS
from riesz_runner.modes import CLAUDE_MODE_TABLE, CLAUDE_TIERS, TIERS

ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).parent
REQUIRED_METHODS = ("start", "send", "interrupt", "stop", "modes")


class ConformanceTest(unittest.TestCase):
    def specs(self) -> list[HarnessSpec]:
        return [s for s in HARNESSES.values() if s.id != "fake"]       # the test suite's own stand-in

    def test_driver_implements_the_contract(self):
        for spec in self.specs():
            with self.subTest(harness=spec.id):
                self.assertTrue(issubclass(spec.driver, Driver))
                self.assertEqual(spec.driver.harness, spec.id, "Driver.harness must be the spec id")
                self.assertIsInstance(spec.driver.capabilities, Capabilities)
                for name in REQUIRED_METHODS:
                    owner = next(c for c in spec.driver.__mro__ if name in c.__dict__)
                    self.assertIsNot(owner, Driver, f"{spec.driver.__name__} must implement {name}()")
                self.assertIn(spec.arg, inspect.signature(spec.driver.__init__).parameters,
                              f"{spec.driver.__name__}.__init__ must take {spec.arg}= (the executable)")

    def test_modes_and_their_table(self):
        for spec in self.specs():
            with self.subTest(harness=spec.id):
                driver = spec.driver(cwd="/", sink=None)
                modes = {m.id: m for m in driver.modes()}
                self.assertTrue(modes, "a harness lists its permission modes")
                self.assertIn(driver.mode, modes, "the default mode is one of them")
                self.assertTrue(all(isinstance(m, Mode) and m.tier in TIERS for m in modes.values()))
                for m in modes.values():
                    # A session in mode m moving to this harness lands in a mode of the same tier.
                    self.assertEqual(modes[spec.modes[m.tier]].tier, m.tier,
                                     f"modes[{m.tier!r}] should name a {m.tier} mode, like {m.id}")

    def test_commands_table(self):
        for spec in self.specs():
            with self.subTest(harness=spec.id):
                self.assertEqual(set(spec.commands), set(UNIVERSAL_COMMANDS))
                for name, how in spec.commands.items():
                    if how.startswith("none:"):
                        self.assertGreater(len(how), 8, f"/{name}: say why it is not supported")

    def test_routes(self):
        for spec in self.specs():
            with self.subTest(harness=spec.id):
                self.assertIn(spec.id, routes.ROUTES, "routes.ROUTES lists the APIs it can run on")
                listed = routes.ROUTES[spec.id]
                self.assertTrue(listed)
                for route in listed:
                    provider, auth = routes.split(route)
                    self.assertIn(provider, PROVIDERS)
                    self.assertIn(auth, ("subscription", "key"))
                self.assertIn(spec.native_provider, {routes.split(r)[0] for r in listed})

    def test_handoff_and_fork(self):
        for spec in self.specs():
            with self.subTest(harness=spec.id):
                self.assertIn(spec.id, TARGETS, "handoff.TARGETS lists it")
                self.assertIn(spec.handoff, STRATEGIES)
                self.assertIn(spec.fork, ("native", "copy"))
                if spec.resume_command is not None:
                    self.assertIn("{id}", spec.resume_command)

    def test_tests_and_docs(self):
        guide = {lang: (ROOT / f"docs/_src/{lang}/guide.html").read_text(encoding="utf-8") for lang in ("zh", "en")}
        deploy = {lang: (ROOT / f"docs/_src/{lang}/deploy.html").read_text(encoding="utf-8") for lang in ("zh", "en")}
        for spec in self.specs():
            with self.subTest(harness=spec.id):
                mentioned = any(spec.driver.__name__ in p.read_text(encoding="utf-8") for p in TESTS.glob("test_*.py")
                                if p.name != Path(__file__).name)
                self.assertTrue(mentioned, f"a test module exercises {spec.driver.__name__}")
                for lang in ("zh", "en"):
                    self.assertIn(spec.name, guide[lang], f"docs/_src/{lang}/guide.html describes {spec.name}")
                    self.assertTrue(re.search(rf"\b{re.escape(spec.bin)}\b", deploy[lang]),
                                    f"docs/_src/{lang}/deploy.html says how to install {spec.bin}")

    def test_claude_code_tables(self):
        self.assertEqual(set(CLAUDE_MODE_TABLE), set(TIERS))
        for tier, mode in CLAUDE_MODE_TABLE.items():
            self.assertEqual(CLAUDE_TIERS[mode], tier)


if __name__ == "__main__":
    unittest.main()
