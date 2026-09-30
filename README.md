# pymake

A small `make` in one Python file (stdlib only, Python 3.9+), built around the ideas in
Peter Miller's [*Recursive Make Considered Harmful*](https://aegis.sourceforge.net/auug97.pdf).

```
python3 pymake.py [-f Makefile] [-C dir] [-j N] [-n] [-k] [-B] [-s] [-r]
                  [--hash] [--watch] [--dot] [--why] [VAR=value ...] [targets ...]
python3 test_pymake.py          # 20 end-to-end tests
```

## How it maps to the paper

| Paper | pymake |
|---|---|
| §3.1 make = build a **DAG**, then **post-order traversal**, compare mtimes | `Builder.plan()` builds the graph (DFS, prerequisites first); `Builder.run()` walks it and rebuilds when a prerequisite is newer / was rebuilt |
| §3.2 recursion splits the DAG into incomplete pieces | pymake reads *one* DAG. `include` (with per-module `module.mk`) keeps modularity without splitting. A rule that calls `$(MAKE)` gets a warning citing the paper |
| §3.3 reshuffle / repeat / overkill hacks | not needed: order comes from the graph, not from the order of directories |
| §5.1-5.2 deferred (`=`) vs immediate (`:=`) evaluation | both implemented; `$(shell ...)` in a `=` variable really does run once per use |
| §5.4 `.d` include-dependency files, remade *as makefiles* | included files that are out of date are rebuilt, then everything is re-read, until stable |
| §7 whole-project Makefile with `module.mk` fragments | `examples/whole-project/` is that layout, including a generated header shared between modules |
| §7.1 `-j` and `-k` work better with a complete DAG | both implemented, over the single graph |

Try the paper's scenario:

```
cd examples/whole-project
python3 ../../pymake.py --why          # builds .d files, generates bee/version.h, compiles, links
echo 2.0 > bee/VERSION
python3 ../../pymake.py --why          # version.h -> main.d -> main.o -> prog, in one pass
./prog                                  # prog 2.0: 42
python3 ../../pymake.py --dot | dot -Tsvg > dag.svg    # the complete DAG
```

## Beyond classic make: caching, filesystem, VCS, automation

**`--hash`: content-based rebuilds + a build cache.** Instead of trusting mtimes, each target
gets a key = SHA-256 of (targets, expanded recipe, content hashes of all prerequisites).

* *Early cutoff*: if a rebuilt file comes out byte-identical, its dependents are **not** rebuilt.
* *Content-addressed cache*: every output is also stored under `.pymake/cas/<key>/`. If a key
  reappears (e.g. `git checkout other-branch && build && git checkout -`), outputs are
  **restored instead of recompiled**.
* *Tamper detection*: an output whose content differs from what was recorded is rebuilt/restored.
* *Version control friendly*: `git checkout` rewrites mtimes, which makes mtime-based make rebuild
  needlessly; hashes don't care. File hashes are cached by `(mtime, size)`, the same trick as git's
  index, so unchanged files aren't re-read. `.pymake/` writes its own `.gitignore`.

**`-j N`** runs independent rules in parallel (output of each job is kept together).
**`--watch`** polls the graph's source files and the makefiles, and rebuilds when any change.
**`--dot`** emits the whole-project DAG. **`--why`** prints the reason for every rebuild.

## Supported Makefile subset

* Rules: explicit, multi-target, pattern (`%.o: %.c`), order-only prereqs (`| dir`), inline
  `; recipe`, `.PHONY`, deps-only lines that add prerequisites to pattern/implicit rules.
  Built-in `%.o` from `.c`, `.cc`, `.cpp` (disable with `-r`).
* Automatic variables `$@ $< $^ $+ $* $|` and `$(@D) $(@F) $(<D) $(<F)`.
* Variables `= := ::= ?= +=`, `$(VAR:.c=.o)`, `$(VAR:%.c=%.o)`, command-line `VAR=x`, environment,
  `CC CXX RM MAKE CURDIR MAKECMDGOALS`, `.DEFAULT_GOAL`.
* Functions: `subst patsubst strip findstring filter filter-out sort word words firstword lastword
  dir notdir suffix basename addsuffix addprefix wildcard shell abspath if foreach info warning error`.
* `include`, `-include`, `ifeq ifneq ifdef ifndef else endif`, `\` line continuation.
* Recipe prefixes `@` `-` `+`; failed recipes delete half-written targets.

## Deliberate differences and limitations

* **Multi-target rules are grouped**: `parse.c parse.h: parse.y` runs its recipe once and produces
  both (GNU make's `&:`), which is what people intend and is safe under `-j`.
* Not implemented: `define/endef`, `export` semantics, target-specific variables, static pattern
  rules, `VPATH`, `$(call)`, `$(eval)`, secondary expansion, jobserver (a `$(MAKE)` child does not
  share `-j`). Filenames containing spaces are unsupported.
* Recipes run with `/bin/sh -c`, one shell per line (no `.ONESHELL`).
* `--hash` only tracks what is visible in the graph: if a tool reads a file that isn't listed as a
  prerequisite (e.g. an un-scanned `#include`), it won't be noticed, same as any make. That's what
  the `.d` file technique in the example is for.
* Delete `.pymake/` to reset the cache.
