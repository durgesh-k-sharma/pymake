#!/usr/bin/env python3
"""End-to-end tests: run `pymake` on small throw-away projects."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PYMAKE = os.path.join(HERE, "pymake.py")
EXAMPLE = os.path.join(HERE, "examples", "whole-project")


class Project:
    """A temp directory with helpers to write files and run pymake."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="pymake-test-")

    def write(self, name, text=""):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text).lstrip("\n") if "\t" not in text else text)
        return path

    def read(self, name):
        with open(os.path.join(self.dir, name)) as fh:
            return fh.read()

    def exists(self, name):
        return os.path.exists(os.path.join(self.dir, name))

    def make(self, *args):
        return subprocess.run([sys.executable, PYMAKE, "-C", self.dir, *args],
                              capture_output=True, text=True)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.addCleanup(self.p.cleanup)


class BasicTests(Base):
    def test_build_then_up_to_date(self):
        self.p.write("in.txt", "hello\n")
        self.p.write("Makefile", "out.txt: in.txt\n\ttr a-z A-Z < in.txt > out.txt\n")
        r = self.p.make()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.p.read("out.txt"), "HELLO\n")
        self.assertIn("is up to date", self.p.make().stdout)

    def test_rebuild_when_prereq_newer(self):
        self.p.write("in.txt", "a\n")
        self.p.write("Makefile", "out.txt: in.txt\n\tcp in.txt out.txt\n")
        self.p.make()
        time.sleep(0.02)
        self.p.write("in.txt", "b\n")
        self.p.make()
        self.assertEqual(self.p.read("out.txt"), "b\n")

    def test_variables_deferred_vs_immediate(self):
        self.p.write("Makefile", (
            "A := $(shell echo x >> immediate.log; echo 1)\n"
            "B  = $(shell echo y >> deferred.log; echo 2)\n"
            "all: ; @echo $(A) $(A) $(B) $(B)\n"))
        r = self.p.make()
        self.assertEqual(r.stdout.strip().splitlines()[0], "1 1 2 2")
        self.assertEqual(len(self.p.read("immediate.log").split()), 1)   # ran once
        self.assertEqual(len(self.p.read("deferred.log").split()), 2)    # ran per use

    def test_functions_and_substitution_refs(self):
        self.p.write("Makefile", (
            "SRC = a.c dir/b.c\n"
            "all: ; @echo $(SRC:.c=.o) '|' $(patsubst %.c,%.h,$(SRC)) '|' "
            "$(notdir $(SRC)) '|' $(dir $(SRC)) '|' $(words $(SRC)) '|' "
            "$(filter %b.c,$(SRC)) '|' $(if $(SRC),yes,no)\n"))
        out = self.p.make().stdout.splitlines()[0]
        self.assertEqual(out, "a.o dir/b.o | a.h dir/b.h | a.c b.c | ./ dir/ | 2 | dir/b.c | yes")

    def test_conditionals(self):
        self.p.write("Makefile", (
            "MODE = fast\n"
            "ifeq ($(MODE),slow)\nX = 1\nelse ifeq ($(MODE),fast)\nX = 2\nelse\nX = 3\nendif\n"
            "ifdef NOPE\nY = set\nelse\nY = unset\nendif\n"
            "all: ; @echo $(X) $(Y)\n"))
        self.assertEqual(self.p.make().stdout.strip(), "2 unset")
        self.assertEqual(self.p.make("MODE=slow").stdout.strip(), "1 unset")   # command line wins

    def test_pattern_rule_with_extra_explicit_deps(self):
        self.p.write("a.in", "A")
        self.p.write("hdr", "1")
        self.p.write("Makefile", (
            "a.out: hdr\n"
            "%.out: %.in\n\tcat $< $^ > $@\n"))
        self.assertEqual(self.p.make("a.out").returncode, 0)
        self.assertEqual(self.p.read("a.out"), "AA1")         # cat $< $^  ==  a.in, a.in, hdr
        time.sleep(0.02)
        self.p.write("hdr", "2")                              # the explicit extra dep is honoured
        self.p.make("a.out")
        self.assertEqual(self.p.read("a.out"), "AA2")
        self.assertIn("is up to date", self.p.make("a.out").stdout)

    def test_multi_target_rule_runs_once(self):
        self.p.write("g.in", "x")
        self.p.write("Makefile", (
            "all: a.gen b.gen\n"
            "a.gen b.gen: g.in\n\techo run >> runs.log; cp g.in a.gen; cp g.in b.gen\n"))
        self.assertEqual(self.p.make("-j4").returncode, 0)
        self.assertEqual(self.p.read("runs.log").split(), ["run"])

    def test_failure_deletes_partial_target_and_keep_going(self):
        self.p.write("Makefile", (
            "all: bad.txt good.txt\n"
            "bad.txt:\n\techo partial > $@; false\n"
            "good.txt:\n\techo ok > $@\n"))
        r = self.p.make("-k")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.p.exists("bad.txt"))
        self.assertTrue(self.p.exists("good.txt"))
        self.assertIn("not remade because of errors", r.stderr)

    def test_missing_rule_message(self):
        self.p.write("Makefile", "all: nope.txt\n\ttrue\n")
        r = self.p.make()
        self.assertEqual(r.returncode, 2)
        self.assertIn("No rule to make target 'nope.txt', needed by 'all'", r.stderr)

    def test_recursive_make_warns(self):
        self.p.write("Makefile", "all:\n\t@$(MAKE) -C sub\n")
        self.p.write("sub/Makefile", "all: ; @echo inner\n")
        r = self.p.make()
        self.assertIn("inner", r.stdout)
        self.assertIn("Recursive Make Considered Harmful", r.stderr)

    def test_dry_run_prints_but_does_nothing(self):
        self.p.write("Makefile", "out: ; touch out\n")
        r = self.p.make("-n")
        self.assertIn("touch out", r.stdout)
        self.assertFalse(self.p.exists("out"))

    def test_dot_output(self):
        self.p.write("Makefile", "prog: a.o\n\ttouch prog\na.o: a.c\n\ttouch a.o\n")
        self.p.write("a.c")
        out = self.p.make("--dot", "prog").stdout
        self.assertIn('"prog" -> "a.o"', out)
        self.assertIn('"a.o" -> "a.c"', out)


class IncludeAndParallelTests(Base):
    def test_included_makefile_is_remade_then_reread(self):
        self.p.write("in.txt", "IN")
        self.p.write("extra.txt", "EXTRA")
        self.p.write("Makefile", (
            "all: out.txt\n"
            "include deps.mk\n"
            "deps.mk:\n\techo 'out.txt: extra.txt' > deps.mk\n"
            "out.txt: in.txt\n\tcat $^ > $@\n"))
        r = self.p.make()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.p.read("out.txt"), "INEXTRA")   # the generated dep was picked up

    def test_parallel_is_actually_parallel(self):
        rules = "".join(f"t{i}:\n\tsleep 0.5; touch $@\n" for i in range(4))
        self.p.write("Makefile", "all: t0 t1 t2 t3\n" + rules)
        t = time.time()
        r = self.p.make("-j4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t, 1.5)


class HashModeTests(Base):
    MAKEFILE = (
        "c.txt: b.txt\n\twc -c < b.txt > c.txt\n"
        "b.txt: a.txt\n\ttr a-z A-Z < a.txt > b.txt\n")

    def test_early_cutoff(self):
        """b.txt comes out identical, so c.txt must not be rebuilt (mtime mode would)."""
        self.p.write("a.txt", "abc\n")
        self.p.write("Makefile", self.MAKEFILE)
        self.assertEqual(self.p.make("--hash", "-s").returncode, 0)
        self.p.write("a.txt", "ABC\n")                     # different input, same b.txt
        r = self.p.make("--hash", "--why", "-s")
        self.assertIn("b.txt: content changed: a.txt", r.stdout)
        self.assertNotIn("c.txt:", r.stdout)

    def test_touch_without_change_is_free(self):
        self.p.write("a.txt", "abc\n")
        self.p.write("Makefile", self.MAKEFILE)
        self.p.make("--hash", "-s")
        time.sleep(0.02)
        os.utime(os.path.join(self.p.dir, "a.txt"))        # mtime bump only (e.g. git checkout)
        self.assertNotIn("built", self.p.make("--hash", "-s").stdout)

    def test_cache_restores_previous_outputs(self):
        self.p.write("a.txt", "one\n")
        self.p.write("Makefile", self.MAKEFILE)
        self.p.make("--hash", "-s")
        first = self.p.read("c.txt")
        self.p.write("a.txt", "longer input\n")
        self.p.make("--hash", "-s")
        self.assertNotEqual(self.p.read("c.txt"), first)
        self.p.write("a.txt", "one\n")                     # "switch branch back"
        r = self.p.make("--hash", "-s")
        self.assertIn("restored", r.stdout)
        self.assertIn("0 built", r.stdout)
        self.assertEqual(self.p.read("c.txt"), first)

    def test_modified_output_is_repaired(self):
        self.p.write("a.txt", "abc\n")
        self.p.write("Makefile", self.MAKEFILE)
        self.p.make("--hash", "-s")
        self.p.write("b.txt", "tampered")
        self.p.make("--hash", "-s")
        self.assertEqual(self.p.read("b.txt"), "ABC\n")


@unittest.skipUnless(shutil.which("gcc"), "needs gcc")
class PaperExampleTests(Base):
    """Miller's scenario: a generated header shared across modules."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.p.dir)
        shutil.copytree(EXAMPLE, self.p.dir)

    def run_prog(self):
        return subprocess.run([os.path.join(self.p.dir, "prog")], capture_output=True, text=True).stdout.strip()

    def test_generated_header_change_rebuilds_dependents_in_one_pass(self):
        for extra in ([], ["--hash"]):
            with self.subTest(mode=extra or "mtime"):
                r = self.p.make(*extra)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.run_prog(), "prog 1.0: 42")
                time.sleep(0.02)
                self.p.write("bee/VERSION", "2.0\n")
                self.assertEqual(self.p.make(*extra).returncode, 0)
                self.assertEqual(self.run_prog(), "prog 2.0: 42")   # main.o saw the new header
                self.p.write("bee/VERSION", "1.0\n")
                self.p.make(*extra)

    def test_clean_does_not_generate_dependencies_first(self):
        self.p.make()
        self.assertEqual(self.p.make("clean").returncode, 0)
        self.assertFalse(self.p.exists("prog"))
        self.assertFalse(self.p.exists("ant/main.d"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
