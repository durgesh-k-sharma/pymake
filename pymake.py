#!/usr/bin/env python3
"""
pymake -- a small `make` built around ONE whole-project DAG.

Design follows Peter Miller, "Recursive Make Considered Harmful" (AUUGN 1997):

  * The Makefile is read into a single, complete dependency graph (a DAG),
    which is then walked in post-order.  `include` gives you modularity
    without splitting the graph into pieces.
  * Makefile text is a *macro* language: `=` is deferred, `:=` is immediate.
  * Included makefiles (e.g. generated `.d` dependency files) are themselves
    targets: they are remade first, then the Makefile is re-read.
  * Recipes that call $(MAKE) get a warning: that is the recursive pattern
    the paper argues against.

Extras beyond classic make:

  --hash    content-hash based rebuilds (instead of mtimes), with "early
            cutoff" and a content-addressed cache of build outputs in
            .pymake/ so switching git branches back and forth is free.
  -j N      parallel build over the DAG.
  --watch   rebuild automatically when a source file changes.
  --dot     print the whole-project DAG as Graphviz.
  --why     explain why each target is being rebuilt.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

sys.setrecursionlimit(20000)

_OUT = threading.Lock()


class MakeError(Exception):
    pass


def say(msg: str, err: bool = False) -> None:
    with _OUT:
        print(f"pymake: {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def mtime(path: str):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


# --------------------------------------------------------------------------
# Text helpers: patterns, argument splitting, built-in functions
# --------------------------------------------------------------------------

def pat_match(pat: str, word: str):
    """Match a make pattern ('%' wildcard). Returns the stem or None."""
    if "%" not in pat:
        return "" if pat == word else None
    pre, _, suf = pat.partition("%")
    if len(word) >= len(pre) + len(suf) and word.startswith(pre) and word.endswith(suf):
        return word[len(pre):len(word) - len(suf)]
    return None


def patsubst(pat: str, rep: str, text: str) -> str:
    out = []
    for w in text.split():
        stem = pat_match(pat, w)
        if stem is None:
            out.append(w)
        else:
            out.append(rep.replace("%", stem, 1) if "%" in rep else rep)
    return " ".join(out)


def split_args(s: str, n: int) -> list[str]:
    """Split on top-level commas (not inside nested $(...)), at most n parts."""
    args, cur, depth = [], [], 0
    for ch in s:
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        if ch == "," and depth == 0 and len(args) < n - 1:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    return args


def top_find(s: str, ch: str) -> int:
    depth = 0
    for i, c in enumerate(s):
        if c in "({":
            depth += 1
        elif c in ")}":
            depth -= 1
        elif c == ch and depth == 0:
            return i
    return -1


def _shell(cmd: str) -> str:
    p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, text=True)
    return p.stdout.rstrip("\n").replace("\n", " ")


def _error(msg: str):
    raise MakeError(msg)


def _dir(w: str) -> str:
    d = os.path.dirname(w)
    return d + "/" if d else "./"


# name -> (number of arguments, implementation on already-expanded args)
FUNCS = {
    "subst": (3, lambda a: a[2].replace(a[0], a[1])),
    "patsubst": (3, lambda a: patsubst(a[0], a[1], a[2])),
    "strip": (1, lambda a: " ".join(a[0].split())),
    "findstring": (2, lambda a: a[0] if a[0] in a[1] else ""),
    "filter": (2, lambda a: " ".join(w for w in a[1].split()
                                     if any(pat_match(p, w) is not None for p in a[0].split()))),
    "filter-out": (2, lambda a: " ".join(w for w in a[1].split()
                                         if not any(pat_match(p, w) is not None for p in a[0].split()))),
    "sort": (1, lambda a: " ".join(sorted(set(a[0].split())))),
    "word": (2, lambda a: (a[1].split() + [""] * int(a[0]))[int(a[0]) - 1]),
    "words": (1, lambda a: str(len(a[0].split()))),
    "firstword": (1, lambda a: (a[0].split() or [""])[0]),
    "lastword": (1, lambda a: (a[0].split() or [""])[-1]),
    "dir": (1, lambda a: " ".join(_dir(w) for w in a[0].split())),
    "notdir": (1, lambda a: " ".join(os.path.basename(w) for w in a[0].split())),
    "suffix": (1, lambda a: " ".join(os.path.splitext(w)[1] for w in a[0].split())),
    "basename": (1, lambda a: " ".join(os.path.splitext(w)[0] for w in a[0].split())),
    "addsuffix": (2, lambda a: " ".join(w + a[0] for w in a[1].split())),
    "addprefix": (2, lambda a: " ".join(a[0] + w for w in a[1].split())),
    "wildcard": (1, lambda a: " ".join(f for p in a[0].split() for f in sorted(glob.glob(p)))),
    "shell": (1, lambda a: _shell(a[0])),
    "abspath": (1, lambda a: " ".join(os.path.abspath(w) for w in a[0].split())),
    "info": (1, lambda a: (say(a[0]), "")[1]),
    "warning": (1, lambda a: (say(a[0], err=True), "")[1]),
    "error": (1, lambda a: _error(a[0])),
    "if": (3, None),        # lazy: handled in Database._call
    "foreach": (3, None),   # lazy: handled in Database._call
}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Var:
    value: str
    immediate: bool  # True for ':=' (value is already expanded)


@dataclass(eq=False)
class Rule:
    targets: list
    prereqs: list = field(default_factory=list)
    order_only: list = field(default_factory=list)
    recipe: list = field(default_factory=list)
    stem: str = ""


def logical_lines(text: str):
    """Yield (lineno, text, is_recipe), joining backslash continuations."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        start, line = i + 1, lines[i].rstrip("\r")
        i += 1
        if line.startswith("\t"):
            while line.endswith("\\") and i < len(lines):
                nxt = lines[i].rstrip("\r")
                i += 1
                line += "\n" + (nxt[1:] if nxt.startswith("\t") else nxt)
            yield start, line, True
        else:
            while line.endswith("\\") and i < len(lines):
                nxt = lines[i].rstrip("\r")
                i += 1
                line = line[:-1].rstrip() + " " + nxt.lstrip()
            yield start, line, False


SPECIAL_TARGETS = {".SUFFIXES", ".SILENT", ".PRECIOUS", ".INTERMEDIATE", ".SECONDARY",
                   ".DELETE_ON_ERROR", ".ONESHELL", ".DEFAULT", ".IGNORE"}

ASSIGN = re.compile(r"^([\w.@%/\-$()]+?)\s*(::=|:=|\+=|\?=|=)\s*(.*)$", re.S)


class Database:
    """Everything read from the makefiles: variables, rules, includes."""

    def __init__(self, cmdvars: dict, cmdgoals: list, builtins: bool, make_cmd: str):
        self.vars: dict[str, Var] = {k: Var(v, False) for k, v in cmdvars.items()}
        self.cmdvars = cmdvars
        self.defaults = {
            "CC": Var("cc", True), "CXX": Var("c++", True), "RM": Var("rm -f", True),
            "MAKE": Var(make_cmd, True), "CURDIR": Var(os.getcwd(), True),
            "MAKECMDGOALS": Var(" ".join(cmdgoals), True),
        }
        self.rules: dict[str, Rule] = {}
        self.patterns: list[Rule] = []
        self.builtin_patterns: list[Rule] = []
        if builtins:
            for ext, cc in ((".c", "$(CC) $(CFLAGS) $(CPPFLAGS)"),
                            (".cc", "$(CXX) $(CXXFLAGS) $(CPPFLAGS)"),
                            (".cpp", "$(CXX) $(CXXFLAGS) $(CPPFLAGS)")):
                self.builtin_patterns.append(
                    Rule(["%.o"], ["%" + ext], [], [f"{cc} -c -o $@ $<"]))
        self.phony: set[str] = set()
        self.default_goal = None
        self.includes: list[tuple[str, bool]] = []
        self.makefiles: list[str] = []
        self._tl = threading.local()

    # ---- expansion -------------------------------------------------------

    def expand(self, s: str, autos: dict | None = None) -> str:
        if "$" not in s:
            return s
        out, i, n = [], 0, len(s)
        while i < n:
            j = s.find("$", i)
            if j < 0:
                out.append(s[i:])
                break
            out.append(s[i:j])
            i = j + 1
            if i >= n:
                out.append("$")
                break
            c = s[i]
            if c == "$":
                out.append("$")
                i += 1
            elif c in "({":
                close = ")" if c == "(" else "}"
                depth, k = 0, i
                while k < n:
                    if s[k] == c:
                        depth += 1
                    elif s[k] == close:
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                if k >= n:
                    raise MakeError(f"unterminated variable reference in: {s!r}")
                out.append(self._ref(s[i + 1:k], autos))
                i = k + 1
            else:
                out.append(self._lookup(c, autos))
                i += 1
        return "".join(out)

    def _ref(self, inner: str, autos) -> str:
        m = re.match(r"([\w-]+)\s+(.*)$", inner, re.S)
        if m and m.group(1) in FUNCS:
            return self._call(m.group(1), m.group(2), autos)
        m = re.match(r"^([^:]*):([^=]*)=(.*)$", inner, re.S)   # $(VAR:.c=.o)
        if m:
            name = self.expand(m.group(1), autos).strip()
            old, new = m.group(2), m.group(3)
            pat, rep = (old, new) if "%" in old else ("%" + old, "%" + new)
            return patsubst(pat, rep, self._lookup(name, autos))
        return self._lookup(self.expand(inner, autos).strip(), autos)

    def _call(self, name: str, raw: str, autos) -> str:
        n, fn = FUNCS[name]
        args = split_args(raw, n)
        if name == "if":
            if self.expand(args[0], autos).strip():
                return self.expand(args[1], autos)
            return self.expand(args[2], autos) if len(args) > 2 else ""
        if name == "foreach":
            var = self.expand(args[0], autos).strip()
            items = self.expand(args[1], autos).split()
            saved, res = self.vars.get(var), []
            for it in items:
                self.vars[var] = Var(it, True)
                res.append(self.expand(args[2] if len(args) > 2 else "", autos))
            if saved is None:
                self.vars.pop(var, None)
            else:
                self.vars[var] = saved
            return " ".join(res)
        a = [self.expand(x, autos) for x in args]
        a += [""] * (n - len(a))
        return fn(a)

    def _lookup(self, name: str, autos) -> str:
        if autos is not None:
            if name in autos:
                return autos[name]
            if len(name) == 2 and name[1] in "DF" and name[0] in autos:   # $(@D) $(<F)
                f = os.path.dirname if name[1] == "D" else os.path.basename
                return " ".join((f(w) or ".") if name[1] == "D" else f(w)
                                for w in autos[name[0]].split())
        v = self.vars.get(name)
        if v is None:
            if name in os.environ:
                return os.environ[name]
            v = self.defaults.get(name)
            if v is None:
                return ""
        if v.immediate:
            return v.value
        stack = getattr(self._tl, "stack", None)
        if stack is None:
            stack = self._tl.stack = set()
        if name in stack:
            raise MakeError(f"Recursive variable '{name}' references itself")
        stack.add(name)
        try:
            return self.expand(v.value, autos)
        finally:
            stack.discard(name)

    # ---- parsing ---------------------------------------------------------

    def _assign(self, name: str, op: str, value: str) -> None:
        name = self.expand(name).strip()
        if name in self.cmdvars:          # command line wins over the file
            return
        old = self.vars.get(name)
        if op in (":=", "::="):
            self.vars[name] = Var(self.expand(value), True)
        elif op == "?=":
            if old is None and name not in os.environ:
                self.vars[name] = Var(value, False)
        elif op == "+=":
            if old is None and name in os.environ:
                old = Var(os.environ[name], True)
            if old is None:
                self.vars[name] = Var(value, False)
            elif old.immediate:
                self.vars[name] = Var((old.value + " " + self.expand(value)).strip(), True)
            else:
                self.vars[name] = Var((old.value + " " + value).strip(), False)
        else:
            self.vars[name] = Var(value, False)

    def _cond(self, kw: str, rest: str) -> bool:
        rest = rest.strip()
        if kw in ("ifdef", "ifndef"):
            defined = bool(self._lookup(self.expand(rest).strip(), None))
            return defined if kw == "ifdef" else not defined
        if rest.startswith("(") and rest.endswith(")"):
            a, b = (x.strip() for x in split_args(rest[1:-1], 2)) if "," in rest else (rest[1:-1], "")
        else:
            m = re.match(r"""^(["'])(.*?)\1\s*(["'])(.*?)\3$""", rest)
            if not m:
                raise MakeError(f"invalid syntax in conditional: {rest!r}")
            a, b = m.group(2), m.group(4)
        eq = self.expand(a) == self.expand(b)
        return eq if kw == "ifeq" else not eq

    def _add_rule(self, rule: Rule) -> None:
        for t in rule.targets:
            old = self.rules.get(t)
            if old is None or old is rule:
                self.rules[t] = rule
            elif not rule.recipe:                      # extra prerequisites only
                old.prereqs += [p for p in rule.prereqs if p not in old.prereqs]
                old.order_only += [p for p in rule.order_only if p not in old.order_only]
            else:
                if old.recipe:
                    say(f"warning: overriding recipe for target '{t}'", err=True)
                rule.prereqs += [p for p in old.prereqs if p not in rule.prereqs]
                rule.order_only += [p for p in old.order_only if p not in rule.order_only]
                self.rules[t] = rule
            if self.default_goal is None and not t.startswith(".") and "%" not in t:
                self.default_goal = t

    def load(self, path: str) -> None:
        try:
            with open(path) as fh:
                text = fh.read()
        except OSError as e:
            raise MakeError(f"{path}: {e.strerror}")
        self.makefiles.append(path)
        pending, kind = None, "rule"
        stack: list[list[bool]] = []          # [taking, done] per open conditional
        active = lambda: all(c[0] for c in stack)

        def flush():
            nonlocal pending
            r, pending = pending, None
            if r is None or kind == "ignore":
                return
            if any("%" in t for t in r.targets):
                self.patterns.append(r)
            elif r.recipe or len(r.targets) == 1:
                self._add_rule(r)
            else:  # 'a b: deps' with no recipe: independent dependency lines
                for t in r.targets:
                    self._add_rule(Rule([t], list(r.prereqs), list(r.order_only)))

        for lineno, raw, is_recipe in logical_lines(text):
            where = f"{path}:{lineno}"
            if is_recipe:
                if not active() or not raw[1:].strip():
                    continue
                if pending is None:
                    raise MakeError(f"{where}: recipe commences before first target")
                pending.recipe.append(raw[1:])
                continue
            line = re.sub(r"(?<!\\)#.*$", "", raw).replace("\\#", "#").strip()
            if not line:
                continue
            flush()
            m = re.match(r"^(ifeq|ifneq|ifdef|ifndef|else|endif)\b\s*(.*)$", line)
            if m:
                kw, rest = m.groups()
                if kw in ("ifeq", "ifneq", "ifdef", "ifndef"):
                    parent = active()
                    t = self._cond(kw, rest) if parent else False
                    stack.append([t, t or not parent])
                elif kw == "else":
                    if not stack:
                        raise MakeError(f"{where}: extraneous 'else'")
                    top = stack[-1]
                    if top[1]:
                        top[0] = False
                    else:
                        m2 = re.match(r"^(ifeq|ifneq|ifdef|ifndef)\b\s*(.*)$", rest)
                        top[0] = self._cond(*m2.groups()) if m2 else True
                        top[1] = top[0]
                else:
                    if not stack:
                        raise MakeError(f"{where}: extraneous 'endif'")
                    stack.pop()
                continue
            if not active():
                continue
            m = re.match(r"^(-|s)?include\s+(.*)$", line)
            if m:
                for name in self.expand(m.group(2)).split():
                    self.includes.append((name, bool(m.group(1))))
                    if os.path.exists(name):
                        self.load(name)
                continue
            bare = re.sub(r"^(export|unexport|override)\s+", "", line)
            m = ASSIGN.match(bare)
            if m:
                self._assign(*m.groups())
                continue
            if bare != line and ":" not in bare and "=" not in bare:
                continue                                     # bare 'export VAR'
            semi = top_find(line, ";")
            left, inline = (line[:semi], line[semi + 1:]) if semi >= 0 else (line, None)
            left = self.expand(left)
            if ":" not in left:
                raise MakeError(f"{where}: missing separator")
            tpart, _, ppart = left.partition(":")
            ppart = ppart.lstrip(":")
            normal, _, oo = ppart.partition("|")
            targets = tpart.split()
            if not targets:
                continue
            if targets == [".PHONY"]:
                self.phony.update(normal.split())
                kind, pending = "ignore", Rule([".PHONY"])
                continue
            kind = "ignore" if all(t in SPECIAL_TARGETS for t in targets) else "rule"
            pending = Rule(targets, normal.split(), oo.split())
            if inline is not None and inline.strip():
                pending.recipe.append(inline.strip())
        flush()
        if stack:
            raise MakeError(f"{path}: missing 'endif'")

    def goal(self):
        v = self.vars.get(".DEFAULT_GOAL")
        return self.expand(v.value).strip() if v else self.default_goal


# --------------------------------------------------------------------------
# State: content hashes (like git's index stat-cache) + content-addressed cache
# --------------------------------------------------------------------------

class State:
    VERSION = 1

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, "state.json")
        self.cas = os.path.join(root, "cas")
        self.files: dict = {}      # path -> [mtime_ns, size, sha256]
        self.targets: dict = {}    # target -> {key, out, deps}
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            if d.get("version") == self.VERSION:
                self.files, self.targets = d["files"], d["targets"]
        except (OSError, ValueError, KeyError):
            pass

    def file_hash(self, path: str) -> str:
        try:
            st = os.stat(path)
            sig = [st.st_mtime_ns, st.st_size]
            ent = self.files.get(path)
            if ent and ent[:2] == sig:            # unchanged stat => skip re-reading
                return ent[2]
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return "unreadable"
        self.files[path] = sig + [h.hexdigest()]
        return h.hexdigest()

    def save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        gi = os.path.join(self.root, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w") as fh:
                fh.write("*\n")                   # never commit build state
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"version": self.VERSION, "files": self.files, "targets": self.targets}, fh)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------
# Builder: rule lookup, DAG planning, scheduling, execution
# --------------------------------------------------------------------------

class Builder:
    def __init__(self, db: Database, opts, state: State | None, counts: dict | None = None):
        self.db, self.opts, self.state = db, opts, state
        self.counts = counts if counts is not None else {"built": 0, "cached": 0}
        self.dry = opts.dry_run
        self.capture = False
        self.memo: dict[str, Rule | None] = {}
        self.inst: dict = {}
        self.volatile: set[str] = set()   # targets whose content must be treated as "new"
        self.ran: set[str] = set()        # targets whose recipe ran or were restored
        self.leaves: set[str] = set()
        self.uptodate = 0
        self.lock = threading.Lock()
        self.run_id = os.urandom(4).hex()
        self._warned = False

    # ---- rule lookup (explicit rules + pattern rules) ------------------------

    def resolve(self, name: str) -> Rule | None:
        if name in self.memo:
            return self.memo[name]
        rule = None
        ex = self.db.rules.get(name)
        if ex is not None and ex.recipe:
            rule = ex
        else:
            found = self._find_pattern(name, 0)
            if found:
                rule = self._instantiate(*found)
            elif ex is not None:
                rule = ex
        self.memo[name] = rule
        return rule

    def _find_pattern(self, name: str, depth: int):
        for prule in self.db.patterns + self.db.builtin_patterns:
            for tpat in prule.targets:
                stem = pat_match(tpat, name)
                if not stem:
                    continue
                if all(self._can_make(p.replace("%", stem, 1), depth + 1) for p in prule.prereqs):
                    return prule, stem
        return None

    def _can_make(self, name: str, depth: int) -> bool:
        return (os.path.exists(name) or name in self.db.rules
                or (depth < 4 and self._find_pattern(name, depth) is not None))

    def _instantiate(self, prule: Rule, stem: str) -> Rule:
        key = (id(prule), stem)
        if key in self.inst:
            return self.inst[key]
        targets = [t.replace("%", stem, 1) for t in prule.targets]
        prereqs = [p.replace("%", stem, 1) for p in prule.prereqs]
        oo = [p.replace("%", stem, 1) for p in prule.order_only]
        for t in targets:                       # merge explicit deps-only lines
            ex = self.db.rules.get(t)
            if ex is not None:
                prereqs += [p for p in ex.prereqs if p not in prereqs]
                oo += [p for p in ex.order_only if p not in oo]
        rule = Rule(targets, prereqs, oo, prule.recipe, stem)
        self.inst[key] = rule
        for t in targets:
            ex = self.db.rules.get(t)
            if t not in self.memo and (ex is None or not ex.recipe):
                self.memo[t] = rule
        return rule

    @staticmethod
    def autos(r: Rule) -> dict:
        ps = list(dict.fromkeys(r.prereqs))
        return {"@": r.targets[0], "<": ps[0] if ps else "", "^": " ".join(ps),
                "+": " ".join(r.prereqs), "*": r.stem, "|": " ".join(r.order_only)}

    # ---- planning ---------------------------------------------------------

    def plan(self, goals: list[str]):
        order: list[Rule] = []
        deps: dict[Rule, list[Rule]] = {}
        mark: dict[Rule, int] = {}

        def visit(name: str, parent):
            rule = self.resolve(name)
            if rule is None:
                if os.path.exists(name):
                    self.leaves.add(name)
                    return None
                need = f", needed by '{parent}'" if parent else ""
                raise MakeError(f"No rule to make target '{name}'{need}.")
            m = mark.get(rule)
            if m == 2:
                return rule
            if m == 1:
                say(f"Circular dependency dropped: {parent} <- {name}", err=True)
                return None
            mark[rule] = 1
            ds: list[Rule] = []
            for p in rule.prereqs + rule.order_only:
                d = visit(p, rule.targets[0])
                if d is not None and d is not rule and d not in ds:
                    ds.append(d)
            deps[rule], mark[rule] = ds, 2
            order.append(rule)          # post-order: prerequisites first
            return rule

        for g in goals:
            visit(g, None)
        return order, deps

    def dot(self, goals: list[str]) -> str:
        order, _ = self.plan(goals)
        out = ["digraph make {", "  node [fontname=monospace, shape=box];"]
        for r in order:
            for t in r.targets:
                shape = "octagon" if t in self.db.phony else "box, style=filled, fillcolor=lightgrey"
                out.append(f'  "{t}" [shape={shape}];')
                for p in r.prereqs:
                    out.append(f'  "{t}" -> "{p}";')
                for p in r.order_only:
                    out.append(f'  "{t}" -> "{p}" [style=dashed];')
        for leaf in sorted(self.leaves):
            out.append(f'  "{leaf}" [shape=plaintext];')
        out.append("}")
        return "\n".join(out)

    # ---- scheduling -------------------------------------------------------

    def run(self, goals: list[str], quiet: bool = False) -> bool:
        order, deps = self.plan(goals)
        jobs = max(1, self.opts.jobs)
        self.capture = jobs > 1
        pending, done, failed, running = list(order), set(), set(), {}
        stop = False
        with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
            while True:
                changed = True
                while changed and not stop:
                    changed = False
                    for r in list(pending):
                        if len(running) >= jobs:
                            break
                        ds = deps[r]
                        if any(d in failed for d in ds):
                            pending.remove(r)
                            failed.add(r)
                            changed = True
                            say(f"'{r.targets[0]}' not remade because of errors.", err=True)
                        elif all(d in done for d in ds):
                            pending.remove(r)
                            running[ex.submit(self.build_rule, r)] = r
                if not running:
                    break
                finished, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                for f in finished:
                    r = running.pop(f)
                    try:
                        f.result()
                        done.add(r)
                    except MakeError as e:
                        say(f"*** {e}", err=True)
                        failed.add(r)
                        if not self.opts.keep_going:
                            stop = True
        if not failed and not quiet:
            for g in goals:
                rule = self.resolve(g)
                if rule is not None and g not in self.ran:
                    say(f"Nothing to be done for '{g}'." if not rule.recipe
                        else f"'{g}' is up to date.")
        return not failed

    def remake_includes(self) -> bool:
        """Remake included makefiles; True if any changed (=> re-read everything)."""
        goals = []
        for path, optional in self.db.includes:
            if self.resolve(path) is None:
                if not os.path.exists(path) and not optional:
                    raise MakeError(f"{path}: No such file or directory (and no rule to make it)")
                continue
            goals.append(path)
        if not goals:
            return False
        saved, self.dry = self.dry, False
        try:
            ok = self.run(goals, quiet=True)
        finally:
            self.dry = saved
        if not ok:
            raise MakeError("failed to remake included makefiles")
        return any(g in self.ran for g in goals)

    # ---- one rule -----------------------------------------------------------

    def _fp(self, p: str) -> str:
        if p in self.volatile:
            return "volatile:" + self.run_id
        if os.path.isfile(p):
            return self.state.file_hash(p)
        return "absent" if not os.path.exists(p) else "dir"

    def _check(self, r: Rule, lines: list[str], phony: bool):
        """Returns (reason-to-rebuild or None, key, fingerprints)."""
        o = self.opts
        if self.state is None:                                   # classic: mtimes
            if o.always_make:
                return "forced (-B)", None, None
            if phony:
                return "phony target", None, None
            tm = [mtime(t) for t in r.targets]
            if None in tm:
                return "target does not exist", None, None
            oldest = min(tm)
            for p in r.prereqs:
                if p in self.volatile:
                    return f"'{p}' was rebuilt", None, None
                pm = mtime(p)
                if pm is not None and pm > oldest:
                    return f"'{p}' is newer", None, None
            return None, None, None
        fps = {p: self._fp(p) for p in r.prereqs}                # --hash: contents
        key = hashlib.sha256(json.dumps([r.targets, lines, list(fps.items())]).encode()).hexdigest()
        if o.always_make:
            return "forced (-B)", key, fps
        if phony:
            return "phony target", key, fps
        for t in r.targets:
            rec = self.state.targets.get(t)
            if mtime(t) is None:
                return "target does not exist", key, fps
            if rec is None:
                return "no build record", key, fps
            if rec["key"] != key:
                changed = [p for p, v in fps.items() if rec["deps"].get(p) != v]
                return ("content changed: " + ", ".join(changed)) if changed else "recipe changed", key, fps
            if self.state.file_hash(t) != rec["out"]:
                return "output was modified", key, fps
        return None, key, fps

    def build_rule(self, r: Rule) -> None:
        db, t0 = self.db, r.targets[0]
        autos = self.autos(r)
        lines = [db.expand(l, autos) for l in r.recipe]
        if not self._warned and any(re.search(r"\$[({]MAKE[)}]", l) for l in r.recipe):
            self._warned = True
            say(f"warning: recursive make in the rule for '{t0}': it splits the DAG into pieces "
                "(see Miller, 'Recursive Make Considered Harmful', AUUGN 1997).", err=True)
        phony = any(t in db.phony for t in r.targets)
        reason, key, fps = self._check(r, lines, phony)

        if not r.recipe:            # dependency-only rule: nothing to run, but it may be "updated"
            missing = any(mtime(t) is None for t in r.targets)
            if phony or missing or (self.state is None and reason):
                self.volatile.update(r.targets)
            if any(p in self.ran for p in r.prereqs):     # e.g. `all: prog` after prog was built
                with self.lock:
                    self.ran.update(r.targets)
            return
        if reason is None:
            with self.lock:
                self.uptodate += 1
            return
        if self.opts.why:
            say(f"{t0}: {reason}")

        if self.state and not self.dry and reason != "forced (-B)" and self._restore(r, key, fps):
            with self.lock:
                self.counts["cached"] += 1
                self.ran.update(r.targets)
            say(f"restored {', '.join(r.targets)} from cache")
            return

        if self.dry:
            for line in lines:
                self._sh(line, r)
            self.volatile.update(r.targets)
            self.ran.update(r.targets)
            return

        pre = {t: mtime(t) for t in r.targets}
        try:
            for line in lines:
                self._sh(line, r)
        except MakeError:
            for t in r.targets:             # don't leave half-written outputs behind
                m = mtime(t)
                if m is not None and m != pre[t] and t not in db.phony and os.path.isfile(t):
                    os.remove(t)
                    say(f"deleting '{t}' (its recipe failed)", err=True)
            raise
        with self.lock:
            self.counts["built"] += 1
            self.ran.update(r.targets)
        if self.state is None:
            self.volatile.update(r.targets)
        else:
            if phony or any(mtime(t) is None for t in r.targets):
                self.volatile.update(r.targets)
            else:
                self._record(r, key, fps)
                self._store(r, key)

    # ---- recipe execution ---------------------------------------------------

    def _sh(self, line: str, r: Rule) -> None:
        silent, ignore, s = self.opts.silent, False, line.lstrip()
        while s and s[0] in "@-+":
            silent |= s[0] == "@"
            ignore |= s[0] == "-"
            s = s[1:].lstrip()
        if not s:
            return
        echo = self.dry or not silent
        if self.dry:
            with _OUT:
                print(s, flush=True)
            return
        if self.capture:
            p = subprocess.run(["/bin/sh", "-c", s], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
            with _OUT:
                if echo:
                    print(s)
                if p.stdout:
                    sys.stdout.write(p.stdout)
                sys.stdout.flush()
        else:
            if echo:
                with _OUT:
                    print(s, flush=True)
            p = subprocess.run(["/bin/sh", "-c", s])
        if p.returncode:
            if ignore:
                say(f"[{r.targets[0]}] Error {p.returncode} (ignored)", err=True)
            else:
                raise MakeError(f"recipe for target '{r.targets[0]}' failed (exit status {p.returncode})")

    # ---- hash-mode bookkeeping ---------------------------------------------

    def _record(self, r: Rule, key: str, fps: dict) -> None:
        with self.lock:
            for t in r.targets:
                self.state.targets[t] = {"key": key, "out": self.state.file_hash(t), "deps": fps}

    def _store(self, r: Rule, key: str) -> None:
        ts = sorted(r.targets)
        if not all(os.path.isfile(t) for t in ts):
            return
        d = os.path.join(self.state.cas, key)
        os.makedirs(d, exist_ok=True)
        for i, t in enumerate(ts):
            tmp = os.path.join(d, f".{i}.tmp")
            shutil.copyfile(t, tmp)
            shutil.copymode(t, tmp)
            os.replace(tmp, os.path.join(d, str(i)))

    def _restore(self, r: Rule, key: str, fps: dict) -> bool:
        if any(t in self.db.phony for t in r.targets):
            return False
        ts = sorted(r.targets)
        d = os.path.join(self.state.cas, key)
        srcs = [os.path.join(d, str(i)) for i in range(len(ts))]
        if not all(os.path.isfile(s) for s in srcs):
            return False
        for t, s in zip(ts, srcs):
            os.makedirs(os.path.dirname(t) or ".", exist_ok=True)
            shutil.copyfile(s, t)
            shutil.copymode(s, t)
        self._record(r, key, fps)
        return True


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def prepare(opts, files, cmdvars, cmdgoals, state, counts):
    """Parse the makefiles; remake included makefiles and re-read until stable."""
    make_cmd = f"{sys.executable} {os.path.abspath(__file__)}"
    for _ in range(10):
        db = Database(cmdvars, cmdgoals, not opts.no_builtin_rules, make_cmd)
        for f in files:
            db.load(f)
        b = Builder(db, opts, state, counts)
        if not b.remake_includes():
            return db, b
    raise MakeError("included makefiles keep changing; giving up")


def once(opts, files, cmdvars, cmdgoals, state):
    try:
        counts = {"built": 0, "cached": 0}
        db, b = prepare(opts, files, cmdvars, cmdgoals, state, counts)
        goals = cmdgoals or ([db.goal()] if db.goal() else [])
        if not goals:
            raise MakeError("No targets.")
        if opts.dot:
            print(b.dot(goals))
            return 0, b
        ok = b.run(goals)
        if state and (counts["built"] or counts["cached"]):
            say(f"{counts['built']} built, {counts['cached']} restored from cache, "
                f"{b.uptodate} up to date")
        return (0 if ok else 2), b
    finally:
        if state:
            state.save()


def snapshot(paths):
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[p] = None
    return out


def watch(opts, files, cmdvars, cmdgoals, state):
    paths = list(files)
    while True:
        try:
            _, b = once(opts, files, cmdvars, cmdgoals, state)
            paths = sorted(b.leaves | set(b.db.makefiles))
        except MakeError as e:
            say(f"*** {e}", err=True)
        snap = snapshot(paths)
        say(f"watching {len(paths)} files (Ctrl-C to stop)")
        while snapshot(paths) == snap:
            time.sleep(0.3)
        time.sleep(0.1)          # let editors finish writing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pymake", description="A small whole-project make.")
    ap.add_argument("-f", "--file", action="append", help="makefile (default: Makefile)")
    ap.add_argument("-C", "--directory", help="change to DIR first")
    ap.add_argument("-j", "--jobs", type=int, default=1, help="parallel jobs")
    ap.add_argument("-n", "--dry-run", action="store_true", help="print commands, don't run them")
    ap.add_argument("-k", "--keep-going", action="store_true", help="continue after errors")
    ap.add_argument("-B", "--always-make", action="store_true", help="rebuild everything")
    ap.add_argument("-s", "--silent", action="store_true", help="don't echo commands")
    ap.add_argument("-r", "--no-builtin-rules", action="store_true")
    ap.add_argument("--hash", action="store_true", help="content hashes + output cache (in --state-dir)")
    ap.add_argument("--state-dir", default=".pymake")
    ap.add_argument("--watch", action="store_true", help="rebuild when sources change")
    ap.add_argument("--dot", action="store_true", help="print the DAG as Graphviz and exit")
    ap.add_argument("--why", action="store_true", help="explain each rebuild")
    ap.add_argument("args", nargs="*", help="targets and VAR=value overrides")
    ns = ap.parse_intermixed_args(argv)

    cmdvars, goals = {}, []
    for a in ns.args:
        if "=" in a and not a.startswith("-"):
            k, v = a.split("=", 1)
            cmdvars[k] = v
        else:
            goals.append(a)
    try:
        if ns.directory:
            os.chdir(ns.directory)
        files = ns.file or [f for f in ("Makefile", "makefile", "GNUmakefile") if os.path.exists(f)][:1]
        if not files:
            raise MakeError("No makefile found.")
        state = State(ns.state_dir) if ns.hash else None
        if ns.watch:
            watch(ns, files, cmdvars, goals, state)
            return 0
        rc, _ = once(ns, files, cmdvars, goals, state)
        return rc
    except MakeError as e:
        say(f"*** {e}", err=True)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
