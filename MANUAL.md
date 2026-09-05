# MANUAL — Your Side of the Build

For you, not the agent. What you do, what to expect, and where to intervene.

**Reading order:** **M-0 first** (one-time environment setup) → the rest of this file → hand
`BLUEPRINT.md` + `BUILD_ORDER.md` to Claude Code → work one BO at a time.

---

## How to run this

Start each build order in a **fresh Claude Code session**, launched from your project directory
in the **Ubuntu (WSL2) terminal** — see M-0. Give it exactly this:

> **Scope.** Implement **BO-0X only**. Do not implement, stub, or scaffold components belonging
> to other build orders. If a BO-0X test appears to require a component from a later BO, say so
> and skip that test rather than building ahead.
>
> **Sources of truth.** `BUILD_ORDER.md` says what to build. `BLUEPRINT.md` is authoritative for
> every name, signature, and contract — follow it exactly. Check §1a (Type Index) before
> referencing any type; if one you need isn't listed, that's a spec gap: report it, don't invent
> it. Read `ARCHITECTURE.md` only for rationale when a contract looks arbitrary. Where the two
> disagree, BLUEPRINT wins and the disagreement is a defect worth reporting. Never implement
> anything from ARCHITECTURE's Appendix A — it documents rejected designs on purpose.
>
> **Depend on ports, not implementations.** To learn how another component behaves, read its
> contract in BLUEPRINT and the Protocol in `core/ports.py`. Do not read adapter source
> (`qdrant_store.py`, `neo4j_store.py`, `litellm_client.py`) to write a caller — if the contract
> is insufficient, that's a spec gap to report. Reading tooling (`Makefile`, `conftest.py`,
> `scripts/`, config) is fine and expected.
>
> **Read-only files.** `ARCHITECTURE.md`, `BLUEPRINT.md`, `BUILD_ORDER.md`, `MANUAL.md` and
> `config.example.yaml` must never be edited. Report errors, ambiguities and gaps; corrections
> are applied upstream and synced back.
>
> **Work in this checkout.** Do not create a git worktree or a new branch. Commit nothing and
> tag nothing — leave changes in the working tree for me to review and commit. (If you are a
> background session and the harness forces isolation, say so immediately rather than working
> around it — I'll decide whether to restart you in the foreground.)
>
> **Changing enforcement infrastructure** — `scripts/check_layering.py`, the `Makefile` lint
> target, ruff/mypy config in `pyproject.toml`, or fixtures in `tests/conftest.py` — requires
> stating up front what rule changed and why. Never loosen a check to make a build order pass.
>
> **Verify external facts.** Any image tag, package version, model name, or API endpoint must be
> confirmed to exist (registry, docs, or a live call) before you pin it. Do not write a version
> number from memory.
>
> **Finish with:** write the BO's tests after the components, then run `make lint && make test`.
> Report: the unit and integration test counts, each `[G]` gate test individually by name with
> its result, and every spec gap or judgment call you made. If a test fails for a reason outside
> this BO, say so and leave it — do not fix out-of-scope code.
>
> If a contract is ambiguous or a dependency is missing, stop and ask. Do not guess.

Fresh sessions matter: each BO is self-contained by design, and a long session accumulates stale
assumptions that quietly override the blueprint. "Fresh" means a **new conversation**, in the same
repo directory — Claude Code re-reads the files from disk, so it loses nothing that matters. If it
asks about a component from an earlier BO, point it at `BLUEPRINT.md` rather than re-explaining;
the contract there is the authority, and your paraphrase might not be.

**After every BO, before moving on:**
```bash
make lint && make test          # must be green
git add -A && git commit -m "BO-0X: <name>"
git tag bo-0X
```

Some terms used throughout, so nothing is implied:

| Term | What it means here |
|---|---|
| `make` | GNU Make, the task runner. **Ubuntu-only** — it does not exist in PowerShell, which is one reason M-0 has you work inside WSL2. Installed via `apt install make`. |
| `make up` / `make down` | Wrappers for `docker compose up -d` / `down`. You'll write them in BO-00; they exist so you never mistype a profile flag. |
| **profile** | A Docker Compose label that groups services. `--profile core` starts the databases and app; `--profile obs` adds Grafana and Phoenix. Services without a matching profile stay stopped. Lets you run a lean loop and a full demo from one file. |
| `make test` | `pytest -m "not integration and not eval"` — fast, no containers. `make test-int` adds integration tests, which need `make up` first; it also starts the isolated `neo4j-test` container they run against. |
| `[G]` in the build order | A **gate** test. It guards a failure that produces no error message. If a `[G]` test is red, stop; do not proceed to the next BO. |
| `[T]` / `[C]` | A test / a component to implement. |
| `X` next to a BO | Blocked on a task of yours (M-2, M-3, or M-5). Do it *before* starting that BO. |
| **SSE** | Server-Sent Events — a one-way HTTP stream. It's how `/v1/query/stream` pushes each pipeline step to the browser as it happens. That live view is your demo. |
| **golden set** | Your hand-written question/answer pairs with known-correct source chunks. Evaluation scores the system against these. |
| `git tag bo-0X` | A named bookmark for that commit. `git checkout bo-04` returns you to a known-good state. **You will use this at least once** — most likely when an agent refactors something that was already working.|

**Why the specs are read-only.** If the agent edits them, your repo and the reviewed copy drift
apart — the next upstream patch silently overwrites its changes, and neither version is
authoritative any more. Corrections flow one way: agent reports → you get a patched file → you
sync it in as its own commit (`docs: sync specs`), never mixed with code. That keeps `git log` on
those files a clean record of spec decisions.

**Syncing a corrected spec** — files downloaded on Windows carry CRLF, so normalize them:
```bash
DL=/mnt/c/Users/<YourName>/Downloads
cp "$DL"/{ARCHITECTURE,BLUEPRINT,BUILD_ORDER,MANUAL}.md "$DL/config.example.yaml" .
sed -i 's/\r$//' ARCHITECTURE.md BLUEPRINT.md BUILD_ORDER.md MANUAL.md config.example.yaml
git add -A && git commit -m "docs: sync specs"
```

**If a build order goes badly wrong:**
```bash
git reset --hard bo-0X          # back to the last good checkpoint
```
Then restart that BO in a new session. Re-running a BO from a clean checkpoint is almost always
faster than debugging a session that has lost the thread.

---

## Git, and the worktree trap

You don't need much git for this project. Six commands, plus one situation that will bite you.

### The normal loop, once per build order

```bash
make lint && make test            # you run it, not just the agent
git status                        # see what changed
git add -A                        # stage everything
git commit -m "BO-0X: <name>"
git tag bo-0X                     # a named bookmark for this commit
```

`git tag` is the important one. It gives you `git reset --hard bo-03` — an escape hatch back to a
known-good state. Re-running a build order from a clean checkpoint is almost always faster than
debugging a session that lost the thread.

Two more you'll want:

```bash
git log --oneline -5              # recent history
git diff bo-03 -- scripts/ Makefile pyproject.toml tests/conftest.py
```

### Run sessions in the foreground

**This is the root cause of every "the code vanished into a worktree" incident.**

Claude Code can run two ways:

| Mode | How you start it | Can it write to your checkout? |
|---|---|---|
| **Foreground** | Open a terminal in the repo, run `claude`, paste the prompt | **Yes** — edits land directly |
| **Background** | Delegated or detached job (async task, mobile hand-off) | **No** — the harness sandboxes it and forces a worktree |

A background session literally cannot edit your working tree; it refuses with *"This background
session hasn't isolated its changes yet. Call EnterWorktree first."* That is a harness guard, not
the agent ignoring you — telling it "don't use a worktree" cannot help, because it has no other
way to write anything.

**So: run every build order in the foreground.** Terminal, in the repo directory, `claude`. Then
the "no worktree" line in the standing prompt is actually followable, and you skip the merge
entirely.

If you're already in a background session and the task is small, just let it use the worktree and
merge afterwards — that's cheaper than restarting. Don't let it edit `.claude/settings.json` to
disable the guard.

### Worktrees — why your code can vanish

Claude Code sometimes creates a **git worktree**: a second directory, on its own branch, sharing
one `.git`. Typically at `.claude/worktrees/<branch-name>/`. It's a sensible isolation habit — the
agent's work can't corrupt your main checkout.

But it produces a failure mode that reads like nothing at all: **the agent reports success, and
your main checkout doesn't have the code.** Tests pass in the worktree; you run them at
`~/projects/GraphRAG` and get the previous build order's numbers. Nothing errors. The work is
real, it's just somewhere else.

**Two symptoms, both from `make test`:**

| Symptom | What it means |
|---|---|
| Test count didn't rise after a BO | The code is in a worktree, not your checkout |
| `git diff bo-0<prev> -- scripts/ ...` empty when the agent said it changed something | Same cause |

**Check first:**

```bash
git worktree list                 # more than one line = a worktree exists
git branch -a                     # look for worktree-* branches
```

**Bringing the work in:**

```bash
cd .claude/worktrees/<name>       # 1. commit it where it lives
git status                        # confirm what's there
git add -A && git commit -m "BO-0X: <name>"

cd ~/projects/GraphRAG            # 2. merge into your checkout
git status                        # must be clean first
git merge worktree-<name> --no-ff -m "BO-0X: <name>"

ls graphrag/adapters/<a new file>  # 3. verify it actually arrived
make test                          # test count MUST go up

git tag bo-0X                                    # 4. tag and tidy
git worktree remove .claude/worktrees/<name>
git branch -d worktree-<name>
```

Step 3 is not optional. A merge that silently no-ops leaves you tagging a commit that doesn't
contain the work.

### The other worktree trap: Docker

Compose derives its **project name from the directory name**. A worktree is a different directory,
so it gets its own container namespace: `bo-04-embeddings-vector-store-qdrant-1` instead of
`graphrag-qdrant-1`.

Integration tests then talk past each other — `docker compose stop qdrant` in the worktree stops a
container nothing is using, while the running `api` keeps talking to the original. The test fails
for a reason that looks like a code bug.

Fix either way:
```bash
export COMPOSE_PROJECT_NAME=graphrag     # in the worktree, before make up
docker compose ps --all                  # confirm the name prefix
docker rm -f $(docker ps -aq --filter "name=bo-0")   # clean up strays
```

### Two rules

- **Run `make test` in your main checkout after every build order.** The count going up is your
  proof the work landed. This is the single check that catches the whole class of problem.
- **Never `git add -A` while a merge is half-done.** If `git status` says you're mid-merge,
  finish or abort it (`git merge --abort`) before staging anything.


## Your Tasks (M-0 … M-7)

### M-0 · Windows: do the whole project inside WSL2  ⚠️ read before anything else

You are on Windows with the WSL2 backend. **Work inside the Linux distro, not from PowerShell.**
This is Docker's own recommendation, and it removes four separate problems at once:

| Problem if you work from Windows | Why WSL2 fixes it |
|---|---|
| `make`, `lsof`, `grep -r` don't exist | All standard in Ubuntu |
| CRLF line endings break shell scripts inside containers with `bad interpreter` errors | Linux filesystem writes LF natively |
| Bind mounts from `C:\` cross a 9P share and run **~10× slower** | Files in the distro share the same kernel and VFS cache as the containers |
| PowerShell 5.1 (the Windows default) doesn't support `&&`, so `make lint && make test` fails | bash supports it |

**One-time setup (~15 minutes):**

```powershell
# 1. PowerShell — install Ubuntu if you don't already have it
wsl --list --verbose            # is a distro listed with VERSION 2?
wsl --install -d Ubuntu         # only if none listed; sets a username/password
```

2. **Docker Desktop → Settings → Resources → WSL Integration** → toggle **Ubuntu** ON →
   *Apply & Restart*. Without this, `docker` isn't on your PATH inside the distro.
   *(If the WSL Integration tab is missing, you're in Windows-containers mode — right-click the
   tray whale → "Switch to Linux containers".)*

3. **Open Ubuntu** (Start menu → Ubuntu, or `wsl` from PowerShell) and install the toolchain:

```bash
sudo apt update && sudo apt install -y make git curl build-essential
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv installs Python 3.12 for you
exec $SHELL                                          # reload PATH
docker --version && docker compose version           # must both work; if not, redo step 2
```

4. **Put the repo in the Linux filesystem. This part matters:**

```bash
mkdir -p ~/projects && cd ~/projects
git clone <your-repo-url> graphrag-hybrid && cd graphrag-hybrid
pwd     # MUST print /home/<you>/projects/...  — if it prints /mnt/c/... you are in the slow path
```

> 🚫 **Never put the repo under `/mnt/c/`.** It works, so nothing warns you — it's just ~10× slower
> on every file operation, which turns a 40-second test run into six minutes and makes the whole
> build feel broken. If you already cloned there, move it: `cp -r /mnt/c/path/to/repo ~/projects/`.

5. **Force LF line endings** — do this before your first commit. A `.sh` file with CRLF fails
   inside a container with an error that names the wrong problem:

```bash
git config --global core.autocrlf input
printf '* text=auto eol=lf\n' > .gitattributes
```

6. **Editing files:** from the Ubuntu terminal, `code .` opens VS Code attached to WSL (install the
   *WSL* extension when prompted). From Windows Explorer, the path is `\\wsl$\Ubuntu\home\<you>\projects`.
   Run **Claude Code from the Ubuntu terminal**, in the repo directory.

**From here on, every command in this manual runs in the Ubuntu shell**, except the few explicitly
marked *PowerShell* — those configure Windows itself and cannot run inside the distro.

<details>
<summary><b>"Can't I just build this natively on Windows?"</b> — yes, and here's the honest trade-off</summary>

You can. Nothing here requires Linux. But two facts make WSL2 the better default *for this
specific project*:

**1. Your containers already run in WSL2.** Docker Desktop's WSL2 backend runs every container
inside a Linux VM — that's the backend you chose at install time. So the question was never
"Windows or Linux"; it's only *which side of that boundary your source files and shell sit on*.
Keeping them inside the distro removes a 9P filesystem hop that buys you nothing.

**2. This project is Linux-shaped.** Thirteen Linux containers, a bash-based Makefile, POSIX paths
in the Dockerfile, and shell entrypoints that break on CRLF. None of that is a Windows failing —
it's just what the stack is.

**The native-Windows path, if you want it.** It genuinely works; it's a papercut tax, not a wall:

```powershell
winget install --id Microsoft.PowerShell   # PowerShell 7 — 5.1 has no && and is in maintenance
winget install ezwinports.make             # or use Git Bash, which ships with Git for Windows
```
Then keep the repo on `C:\`, set `git config --global core.autocrlf input`, and run everything
from **PowerShell 7** (`pwsh`), not the default blue-icon PowerShell 5.1.

What you pay: bind-mounted file operations are ~10× slower via the 9P share (test runs and
`uv sync` feel sluggish), CRLF can still sneak into shell scripts, and every `make` recipe in this
project assumes POSIX tools. What you gain: nothing this project uses.

**Windows is a perfectly good development platform** — for .NET, desktop apps, and plenty of
Python. And WSL2 *is* Windows: a first-class Microsoft feature that ships with the OS, not a
workaround or a dual-boot. Using it isn't leaving Windows; it's using the part of Windows built
for exactly this.

**Recommendation:** WSL2. If you hit a wall in M-0 that costs more than ~30 minutes, take the
native path above and move on — a slower build that starts today beats a fast one that starts
Thursday.
</details>

---


### M-1 · Before BO-00 — Environment
- Docker with **≥ 8 GB** available to it. Too little RAM fails confusingly: containers get
  OOM-killed with no useful message, or Compose hangs on a healthcheck. **How you set this
  depends on your platform, and the paths differ.** See M-1a below.
- Python 3.12+, `uv`, `git`, `make` — **all installed inside Ubuntu in M-0**, not on Windows. If
  you also have Python on Windows, ignore it; it isn't used and mixing the two causes confusion.
- Free ports: 8000, 6333, 6334, 7474, 7687, 5432, 6379, 4000, 4317, 4318, 3000, 6006.
- ~15 GB free disk (container images alone are ~6 GB).
- Repo cloned to `~/projects/...` inside Ubuntu (M-0 step 4). Copy `.env.example` to `.env`.
  **Add `.env` to `.gitignore` before your first commit** — once a secret is in git history,
  removing it is genuinely painful.

Check ports are free first — a port already in use gives a bind error that doesn't name the
offender. Docker Desktop publishes ports on the **Windows host**, so a process inside WSL can't
see the conflict. **Run this in PowerShell, not Ubuntu:**

```powershell
8000,6333,6334,7474,7687,5432,6379,4000,4317,4318,3000,6006 | ForEach-Object {
  $c = Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue
  if ($c) {
    $p = Get-Process -Id $c[0].OwningProcess -ErrorAction SilentlyContinue
    "PORT $_ IN USE by $($p.ProcessName) (PID $($c[0].OwningProcess))"
  }
}
```

No output means you're clear. 5432 is the usual culprit — a local PostgreSQL service. Either stop
it (`Stop-Service postgresql*` as Administrator) or change the **host-side** port in
`docker-compose.yml` to `"5433:5432"`; the container-internal port stays 5432, so nothing in the
config changes.

#### M-1a · Giving Docker enough memory

**First, find out what you already have.** You may not need to change anything.

| Platform | Check |
|---|---|
| Windows (WSL2 backend) — **you** | PowerShell: `wsl free -h` — read the `Mem: total` column |
| Windows (Hyper-V backend) | Docker Desktop → Settings → Resources → Memory slider |
| macOS | Docker Desktop → Settings → Resources → Memory slider |
| Linux (Docker Engine) | `free -h` — containers use host RAM directly; nothing to configure |

Not sure which backend you're on? Docker Desktop → Settings → General → if **"Use the WSL 2 based
engine"** is ticked, you're on WSL2.

---

**Windows + WSL2 — your case, and the one with no slider.**

Docker Desktop's Resources page will just say *"Resource allocation is managed by WSL 2."* That is
not a bug. Windows owns the limit, via a file that **does not exist by default**.

When that file is absent, WSL2 takes **up to 50% of your total system RAM**. So:

| Your PC's RAM | WSL2 gets by default | Action |
|---|---|---|
| 32 GB | ~16 GB | Nothing to do |
| 16 GB | ~8 GB | Nothing to do |
| 8 GB | ~4 GB | Don't raise it — run core-only, see below |

Only if you need to raise it, create `C:\Users\<YourName>\.wslconfig`:

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

```ini
[wsl2]
memory=8GB
processors=4
swap=2GB
```

Apply it — **restarting Docker Desktop alone does nothing**; the WSL VM must be torn down:

```powershell
wsl --shutdown
```

Wait ~10 seconds, restart Docker Desktop, then confirm with `wsl free -h`.

Three things that trip people up here:
- **No spaces in the value.** `memory=8GB` works; `memory=8 GB` is silently ignored and you'll
  wonder why nothing changed.
- **The file has no extension.** Notepad will happily save `.wslconfig.txt`, which does nothing.
  In the Save dialog set *Save as type* → **All Files**.
- **Windows 11 has a GUI for this**: search **WSL Settings** in the Start menu → *Memory and
  processor*. Same fields, writes the same file, no text editor.

**Never allocate more than ~60% of physical RAM.** WSL2 memory is taken from Windows, not shared
with it. Setting `memory=8GB` on an 8 GB laptop starves the OS and everything thrashes.

---

**macOS or Windows + Hyper-V:** Docker Desktop → **Settings → Resources → Memory** → drag to
**8 GB** → **Apply & Restart**.

**Linux with Docker Engine (no Desktop):** nothing to configure. Containers use host RAM directly;
the `mem_limit` values in `docker-compose.yml` are the only caps.

---

**If you can't reach 8 GB — run core-only**

Perfectly workable. There is no special profile for this — you simply don't start `obs`:

```bash
# Ubuntu shell, in the repo directory
docker compose --profile core up -d      # ~4 GB: qdrant, neo4j, postgres, redis, litellm, api, worker
```

You lose Grafana and Phoenix, so BO-02's manual trace check (M-4) has to wait until you can run
`--profile obs` briefly — start it, take the screenshot, stop it. Everything else builds normally.
Tighten the per-service caps in `docker-compose.yml`: neo4j `1g` with
`NEO4J_server_memory_heap_max__size=512m`, and qdrant `768m` are enough for a 15-document corpus.
**Do not drop litellm below `1500m`** — it OOMKills, and the symptom (exit 137, restart loop, empty
logs) looks nothing like a memory problem.

**Verify before moving on:**
```bash
docker info --format "{{.MemTotal}}"     # bytes available to the daemon
docker compose --profile core up -d && docker compose ps
```
All services should reach `healthy` within about two minutes. Neo4j is the slowest — it takes
~30 s before it accepts connections, which is normal and not a failure.

### M-2 · Before BO-05 — Seed corpus
Put **10–15 documents** in `corpus/`. This drives every test from BO-05 onward, so build it
deliberately rather than dumping random PDFs.

Requirements:
- **2 documents must share an identical paragraph, verbatim.** Copy-paste it. This is the only
  way `test_shared_paragraph_two_sources` means anything.
- **3+ entities with deliberate surface variants** — `Acme Corp.` / `ACME Corporation` / `Acme`.
- **1 near-miss pair that must NOT merge** — e.g. `Acme Corp` (company) and `Acme` (product).
- **1 hub entity** appearing in most documents (exercises the degree cap).
- **Real multi-hop facts**: A relates to B, B relates to C, and no single document states A→C.
- Mixed formats: PDF, DOCX, TXT, MD.

#### What kind of documents — this matters more than it looks

This system answers **multi-hop questions about relationships between named things**. It is not a
table reader. Pick documents where the interesting facts are entities and the links between them,
in prose.

> 🚫 **Do not use SEC filings, 10-Qs, annual reports, or anything table-heavy.** Four reasons that
> compound: chunking splits on paragraph/sentence boundaries, which a table doesn't have, so rows
> get sliced arbitrarily; a PDF table flattens to one-dimensional text, so `Net sales 94,930
> 90,753` loses which column is which period; answering "Q3 services revenue" needs a *2D join*
> between the row header on the left and the column header on top, and neither survives chunking;
> and the graph schema models `Entity`/`Relation`, not `(measure, period, unit, value)`.
>
> The failure mode is the dangerous one. The system won't refuse — it will retrieve a chunk with
> the right words and plausible numbers and generate a fluent wrong answer. `verify_citations`
> won't catch it, because the chunk really was retrieved. You'd be demoing a hallucination.

**Best choice: press releases from one industry ecosystem** (12–15, from company newsrooms). They
satisfy the M-2 checklist naturally rather than by construction:

| Requirement | How press releases give it for free |
|---|---|
| Verbatim shared paragraph | The boilerplate "About Acme Corp" block repeats identically across every release from that company — authentic source retention, not a planted copy-paste |
| Entity surface variants | "Acme Corporation" in the legal footer, "Acme Corp." in the header, "Acme" in the body |
| Multi-hop facts | Release 1: Acme acquires Beta. Release 2: Beta partnered with Gamma. Nothing states Acme→Gamma — exactly what the graph path is for |
| Hub entity | The acquiring company appears in most of them |

**Also good:** Wikipedia articles on a connected domain (12 companies in a sector plus their
founders) — clean prose, dense entities, genuine multi-hop. Open-access papers from one research
group — authors and citations as edges, and related-work sections often reuse text.

**Avoid:** SEC filings and financial reports, spreadsheets exported to PDF, API documentation,
heavily clause-numbered contracts, and **anything confidential** — free LLM tiers may train on
prompts.

If a document is mostly prose with an occasional small table, that's fine; the prose carries the
answer. The problem is corpora where the *answers live in the tables*.

> This is the highest-leverage hour in the project. A weak corpus makes every downstream metric
> meaningless and you will not notice until BO-11.

### M-3 · Before BO-06 — API keys
Create free accounts and put keys in `.env`:
- **Groq** — console.groq.com. No card. Still publishes its rate-limit table.
- **Google AI Studio** — aistudio.google.com/apikey. **Check your actual limits in AI Studio**; Google no longer publishes free-tier numbers, so whatever any doc says is a guess.
- **OpenRouter** — openrouter.ai/keys. For `:free` models.

**Create two keys per provider where allowed** — key rotation is a feature you're claiming, and
`test_key_rotation_spreads_load` needs a second key to be real.

#### Which model names? Ask the provider, not the agent

**Claude Code cannot reliably name models.** Model IDs churn constantly and look memorable, so it
will produce a plausible one from stale training data — the same failure that gave us
`litellm:v1.98.0-stable`, an image tag that never existed. It can web-search, but that returns
blog posts. The provider's own API is authoritative and takes one command each. **You run these**;
they need your keys.

```bash
source .env

# Groq
curl -s https://api.groq.com/openai/v1/models \
  -H "Authorization: Bearer $GROQ_API_KEY_1" | jq -r '.data[].id' | sort

# Gemini
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY_1" \
  | jq -r '.models[] | select(.supportedGenerationMethods[]? == "generateContent") | .name' | sort

# OpenRouter — free variants only
curl -s https://openrouter.ai/api/v1/models \
  | jq -r '.data[] | select(.id | endswith(":free")) | .id' | sort
```

Paste the output into the BO-06 prompt so the agent chooses from a verified list. It writes
`litellm/config.yaml` with LiteLLM's provider prefixes: `groq/…`, `gemini/…`, `openrouter/…`.

**What each alias is selecting for** — the choice is about rate-limit shape, not benchmark scores:

| Alias | Used by | Pick |
|---|---|---|
| `fast-low-latency` | router, grader | Smallest fast model. Many tiny calls, latency-sensitive — Groq's strength |
| `bulk-high-tpm` | entity extraction | Highest token throughput, latency-tolerant. A Gemini Flash-Lite tier |
| `synth-quality` | answer generation | Best quality you can afford on a free tier. A Gemini Flash tier |
| `judge-alt-vendor` | groundedness, evals | **Must be a different provider from `synth`** — enforced at startup. Self-preference bias inflates every score otherwise |

Gemini names come back as `models/gemini-…`; strip the `models/` prefix and write
`gemini/gemini-…` in the config.

#### Adding a key — the two-file process

Keys live in exactly two places, and adding one always touches both. Nothing else in the codebase
changes: the app knows only role names (`synth`, `judge`, …), never a provider or a key.

**1. `.env`** — add a numbered variable. The name is yours to choose; only `litellm/config.yaml`
reads it:

```bash
GROQ_API_KEY_1=gsk_...
GROQ_API_KEY_2=gsk_...
GEMINI_API_KEY_1=AIza...
GEMINI_API_KEY_2=AIza...        # a second key on the same provider
OPENROUTER_API_KEY_1=sk-or-...
```

**2. `litellm/config.yaml`** — add a `model_list` entry. **The `model_name` is what makes it a
rotation**: two entries sharing one `model_name` become two deployments of the same alias, and
the router spreads load across them.

```yaml
model_list:
  # Two keys, one alias -> rotation. The router picks between them per request.
  - model_name: fast-low-latency
    litellm_params:
      model: groq/llama-3.3-70b-versatile
      api_key: os.environ/GROQ_API_KEY_1
      rpm: 30
  - model_name: fast-low-latency          # SAME alias
    litellm_params:
      model: groq/llama-3.3-70b-versatile
      api_key: os.environ/GROQ_API_KEY_2
      rpm: 30

  # A different provider under the same alias -> cross-provider failover
  - model_name: fast-low-latency
    litellm_params:
      model: gemini/gemini-flash-lite-latest
      api_key: os.environ/GEMINI_API_KEY_1
```

Then restart only the gateway — no rebuild, since the file is bind-mounted:

```bash
docker compose restart litellm
docker compose logs --tail 30 litellm     # confirm it loaded the new deployments
```

**Three things worth knowing:**

- **`os.environ/VAR` is LiteLLM's own syntax**, not shell expansion. LiteLLM reads the variable at
  startup. Putting the literal key in the YAML works and is a mistake — that file is committed.
- **Rate limits are usually per account, not per key.** Groq's are per organisation, so a second
  Groq key spreads load without raising your ceiling. Real headroom comes from a *different
  provider* under the same alias. Two keys still buy you the rotation mechanism and survival of
  one key being revoked.
- **`rpm`/`tpm` are per deployment.** Set them to what that key actually has. The router uses them
  to route away from a deployment near its limit, so wrong numbers make it route badly.

**Removing a key:** delete the `model_list` entry first, then the `.env` variable, then restart.
The other order leaves LiteLLM referencing a variable that no longer exists, and it fails at
startup rather than at first use — which is better, but confusing if you weren't expecting it.

**Verify a rotation is live:**
```bash
for i in $(seq 1 20); do
  curl -s http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
    -d '{"model":"fast-low-latency","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('model','?'))"
done | sort | uniq -c
```
More than one distinct line means the router is spreading. One line means both deployments
collapsed into one — usually a `model_name` mismatch or a missing env var.

Then:
```bash
# In the Ubuntu shell. Searches only tracked files, so .env (gitignored) is correctly skipped.
git grep -nE "sk-[A-Za-z0-9]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{30,}"
# Expected output: nothing at all. Any hit is a key about to be committed.

git check-ignore .env      # must print ".env" — if it prints nothing, .env is NOT ignored
gitleaks detect --no-git   # scans the working tree too
```

### M-4 · After BO-03 — Verify the trace tree yourself

> **Not after BO-02.** BO-02 builds the telemetry spine but there is no `api` service until BO-03,
> so nothing is listening on `:8000` and there is no realistic request to trace. Running M-4 early
> shows an empty Tempo and looks like a broken pipeline when nothing is wrong.
>
> If you want to confirm the spine works at BO-02, run `make test-int` — `test_trail_cli_roundtrip`
> emits a real trace through the collector. That proves the plumbing; the visual check below is
> still worth doing properly once BO-03 gives you an HTTP root span to hang it from.

Only you can do this; a passing test can't tell you a waterfall *looks* right.

1. Bring up both profiles and generate a real request:
   ```bash
   make up obs=1
   curl -X POST localhost:8000/v1/query \
     -H 'Content-Type: application/json' -d '{"question":"test"}'
   ```
   `/healthz` will not do — it's deliberately excluded from logging.
2. **Grafana → `localhost:3000`** (anonymous, no login). Left sidebar → **Explore** → choose
   **Tempo** in the datasource dropdown → **Search** tab → **Run query**. Click any trace.
3. **Set the time picker to "Last 15 minutes" first.** The default range often sits outside your
   run, which shows an empty result that looks identical to a broken exporter.
4. **What you're checking:** spans form a *tree* — an HTTP root with children indented beneath —
   not a flat list of equal-level rows. Flat means parent context isn't propagating.
5. **Phoenix → `localhost:6006`** → Traces → find the same trace → confirm **its parents are
   present**, not just LLM spans floating alone.

Disconnected fragments in Phoenix mean a span filter crept into the Collector config.

#### Reading the result — what "correct" actually looks like

**Grafana / Tempo.** In the waterfall, the root span sits flush left and children are indented
beneath it:

```
graphrag  GET /healthz (1.59ms)          <- root
  - GET /healthz http send (72.81µs)     <- indented = child
  - GET /healthz http send (13.97µs)
```

The two `http send` children are normal: FastAPI's ASGI instrumentation emits one for the response
start and one for the body. Seeing them is a sign the auto-instrumentation is working, not noise.
The Node graph view shows the same thing as a root node with arrows to two leaves.

A **flat list** — every span at the same indent level, no arrows in the Node graph — means parent
context isn't propagating. That's the failure this check exists to catch.

`/healthz` appears here even though it's excluded from the access log. The exclusion is
logging-only; tracing still covers it. Not a leak.

**Phoenix.** Search `trace_id == '<the id from Tempo>'` — it must be the *same* ID, since both
backends receive the same trace. You want:

- **All spans present**, root included. Three spans in Tempo must be three spans in Phoenix. Only
  the children arriving means a span filter is dropping parents in the Collector.
- **`app.correlation_id` in the Attributes table.** This is the one the trail CLI queries on; if
  it's missing, `graphrag trail <cid>` returns an empty bundle.
- `kind: unknown` and `Status: Unset` are **expected** on non-LLM spans. Phoenix classifies spans
  by OpenInference conventions, which HTTP spans don't carry. LLM spans from BO-06 onward will
  show `kind: LLM` and populate Cost. An empty Cost column at this stage is correct.

**Screenshot timing.** A `/healthz` waterfall is thin. Wait for BO-10, where the trace shows
`plan_route → retrieve_vector ∥ retrieve_graph → fuse → grade → generate → verify` — that one
displays the architecture and is the README asset worth having.

**If Tempo is empty, check these in order:**

| Cause | Check |
|---|---|
| Nothing emitted anything | Did the request actually reach an app? A connection-refused `curl` produces no span |
| Data was wiped | `otel-lgtm` stores traces **in memory only**. Any `make down` since the run clears everything — re-run, then look |
| Time range | Set the picker to "Last 15 minutes" |
| obs stack never started | `docker compose ps --all` — otel-collector, otel-lgtm and phoenix must all be listed and healthy. `make up obs=1` must pass `-f docker-compose.obs.yml` |
| Collector rejecting spans | `docker logs --tail 20 graphrag-otel-collector-1` — export errors appear here, not in the app |
| App pointed at the wrong endpoint | Host-run tools need `localhost:4317`; containerized services need `otel-collector:4317`. See the endpoint note in BLUEPRINT §5.1 |

### M-5 · Before BO-11 — Golden set
**50 items** in `evaluation/golden/`. Budget 2–3 hours. Do not let the agent generate these
unsupervised; an LLM-written golden set grades the system against its own assumptions.

| Category | Count | Notes |
|---|---|---|
| `single_hop` | 20 | Answer sits in one chunk |
| `multi_hop` | 15 | Requires ≥2 chunks or a graph traversal |
| `thematic` | 10 | Aggregative — "what themes recur across X" |
| `unanswerable` | 5 | **Plausible but genuinely absent from the corpus** |

Workflow that works: let the agent draft questions from the corpus, then **you** fill in
`gold_chunk_ids` and `gold_route` by hand. Wrong gold IDs silently cap Recall and you'll spend a
day blaming the retriever.

**How to find a `gold_chunk_id` in practice** — you do not read UUIDs off a screen. After BO-05,
the corpus is indexed, so query for the answer text and copy the ID of the chunk that contains it:

```bash
graphrag query "the exact sentence that answers the question" --show-chunk-ids
```

`gold_route` is your judgement call, using this rule:
- **vector** — the answer is stated in one passage; wording similarity is enough
- **graph** — it requires connecting two entities that no single passage connects
- **hybrid** — both, or you genuinely can't decide (this is a legitimate answer, not a cop-out)

The 5 unanswerable items are the most valuable in the set. Make them *plausible* — the topic
should feel like it belongs in the corpus. "What was Acme's 2019 revenue?" when the corpus only
covers 2021–2023 is a good one. "What is the capital of Mars?" is not.

### M-6 · Before BO-12 — Deployment target (optional)
Oracle Cloud **Always Free** ARM VM: 4 OCPU / 24 GB, genuinely free, fits the whole stack.

Two practical warnings. Sign-up requires a card for verification (not charged) and approval can
take a day, so start it during BO-09 if you want it. And the free ARM capacity is frequently
exhausted in popular regions — "out of host capacity" errors are common and may take several
attempts across different availability domains.

**Skipping this entirely is fine.** "Runs locally via `docker compose up`" is a completely
legitimate demo, and no interviewer will hold it against a free-tier project. Do not spend a
day of your build budget fighting cloud provisioning.

### M-7 · After BO-12 — README assets
1. 30-second GIF: query → SSE stream showing node transitions → cited answer.
2. The Grafana screenshot from M-4.
3. Eval table from `make eval`, **numbers as measured**.
4. A "Known Limitations" section.

On (4): write it honestly and specifically — no community detection, Neo4j CE is single-node and
GPLv3, ER gray band unresolved in T0, free-tier limits. Engineers who can enumerate what their
system doesn't do are the ones who get hired. This section will get you more interview traction
than any feature in the repo.

---

## What Each Build Order Gives You

| BO | What it does | You should see | Rough time |
|---|---|---|---|
| **00** | Repo, tooling, config module, infra containers | `make up` → 7 healthy containers; `python -m graphrag.config.validate` prints a hash | 2–3 h |
| **01** | Domain models, ports, errors, test fakes | Nothing runs. ~60 fast unit tests pass | 2–3 h |
| **02** | Observability spine | `make test-int` emits a real trace end-to-end; JSON logs carry `correlation_id`. No HTTP traces yet — there's no `api` service until BO-03 | 3–4 h |
| **03** | FastAPI app, Postgres, Redis, queue adapter | `curl localhost:8000/healthz` → 200; `/readyz` → 503 when you stop a backend. **Run M-4 now** — this is the first BO with a traceable HTTP request | 3–4 h |
| **04** | Embeddings + Qdrant | Collection exists **with the IDF modifier**; hybrid search returns ranked chunks | 3–4 h |
| **05** | Ingestion end to end | `graphrag ingest corpus/` → chunks in Qdrant; the shared paragraph has **2 sources** | 5–6 h |
| **06** | LLM gateway | Structured calls return validated objects; killing a provider fails over silently | 3–4 h |
| **07** | Extraction + entity resolution | Three `Acme` variants collapse to one canonical entity with alias edges | 5–6 h |
| **08** | Neo4j graph | Neo4j Browser shows the graph; **every edge carries `chunk_id`** | 4–5 h |
| **09** | Retrieval | Vector, graph, and fused results; hybrid beats either alone on exact IDs | 3–4 h |
| **10** | Orchestration + query API | **The demo.** `POST /v1/query` → routed, cited, verified answer | 6–8 h |
| **11** | Evaluation | `make eval` → metric table with pass/fail | 4–5 h |
| **12** | Hardening + deploy | Auth, rate limits, breakers, prod compose, CI, README | 4–5 h |

**How to tell a BO is genuinely done** (not just "the agent said so"):
1. `make lint && make test` is green **when you run it yourself**, not when the agent reports it.
2. Every `[G]` test for that BO passes — ask explicitly: *"list each [G] test and its result."*
3. The "You should see" column above actually happens when you try it by hand.
4. `git status` is clean after you commit, with no stray files in the working tree.

**Total ≈ 45–55 hours.** That is 6–7 focused days, not 4. Plan accordingly:

- **Absolute minimum for a defensible CV project:** BO-00 → BO-10. That's a working Hybrid GraphRAG with observability and self-correction.
- **If you're out of time at BO-10:** ship it. Write BO-11 and BO-12 into the README roadmap. A finished 11-BO system beats a broken 13-BO one, every time.
- **Do not skip BO-02.** It looks like overhead on day one and it is what makes days three through six survivable.

---

## DOs

- **DO commit and tag after every BO.** `git tag bo-0X`.
- **DO run `make test` yourself** after the agent says it's done. Trust, verify.
- **DO check the `[G]` gate tests specifically.** They guard silent failures — the bugs that pass every other test and surface mid-demo.
- **DO read the diff** on `config/*.yaml` and `core/ports.py`. Changes there ripple everywhere.
- **DO keep `docker compose logs -f` open** in a second terminal during BO-05 and BO-10.
- **DO write down the actual numbers** from `make eval`, whatever they are.
- **DO break things deliberately** — run the regression table at the end of `BUILD_ORDER.md` at least once before any demo.
- **DO ask the agent to explain a component** if you can't follow it. If you can't explain it in an interview, it isn't an asset.

## DON'Ts

- **DON'T let the agent implement across build orders.** "While I'm here I'll also add the retriever" is how you get an untested tangle. Stop it and re-scope.
- **DON'T change a threshold to make a test pass.** Thresholds encode intent. If `routing_accuracy` is 0.68 against a 0.75 gate, fix the prompt or record 0.68 honestly.
- **DON'T skip the IDF modifier check in BO-04.** It cannot be added later without recreating the collection and re-indexing everything.
- **DON'T reset your demo data before `make test-int`.** You no longer need to, and the reset is
  now the only real risk in the sequence. Integration tests cannot reach a real store: every
  backend they touch is namespaced per run by `tests/integration/namespaces.py` —
  Qdrant collections prefixed `test_<run>_`, a `graphrag_test_<run>` Postgres database the
  session creates and drops, a Redis db index in 1–15 (never 0, where the arq queue the worker
  consumes lives), and a **separate** `neo4j-test` container on port 7688 that `make test-int`
  starts and `make up` does not. Neo4j needs its own instance rather than its own database
  because Community Edition supports exactly one. `tests/integration/test_isolation_guard.py`
  asserts all four differ from what `config/base.yaml` and `.env` resolve to, and
  `tests/unit/test_integration_isolation.py` (no containers, so `make test` catches it too)
  feeds each destructive guard a fabricated production value and asserts it refuses — so the day
  someone points a fixture back at production, a test says so instead of a corpus quietly
  emptying. Verify those guards that way and only that way: aiming a live fixture at a real
  store to watch it refuse is how the real `chunks` and `entities` collections were once
  deleted.
  Run the suite through `make test-int`, not a bare `pytest -m integration`: the make target is
  what starts `neo4j-test`. (A bare run fails with a message telling you this, rather than
  falling back to 7687.)
- **DON'T commit `.env`.** Check `gitleaks` before every push. One leaked key in a public CV repo undoes the whole project.
- **DON'T put confidential or personal documents in `corpus/`.** Free tiers may train on prompts.
- **DON'T let enforcement infrastructure be edited to make a build order pass.** `tests/` is only
  half of it. `scripts/check_layering.py`, the `lint` target in the `Makefile`, ruff/mypy config in
  `pyproject.toml`, and fixtures in `tests/conftest.py` all decide whether a violation is even
  *detectable* — loosening one silently disables a whole class of checks, and no test goes red to
  tell you. Reading these files is fine and expected: the agent is checking a constraint it must
  satisfy, from the tool that enforces it. Changing them is what needs a reason. After each BO:
  ```bash
  git diff bo-0<previous> -- scripts/ Makefile pyproject.toml tests/conftest.py
  ```
  Empty is the expected result. Anything else, ask what rule changed and why. Legitimate reasons
  exist — BO-01 updated `check_layering.py` because the §0 table itself was corrected — but it
  should be stated up front, not discovered by you afterwards.
- **DON'T let the agent "fix" a failing test by weakening its assertion.** This is the single most common way an agentic build produces a green suite that proves nothing. Concretely, watch for: a `[G]` assertion changed to something looser, a test given `@pytest.mark.skip` or `xfail`, a threshold edited in `config/*.yaml` instead of the code, or an exact-value check turned into `assert result is not None`. Spot it with `git diff` on `tests/` — if a test file changed in the same commit that fixed the code it tests, read that diff before committing.
- **DON'T add features before BO-12 hardening.** A reranker is not worth an unauthenticated API.
- **DON'T use `latest` or `main-latest` image tags.** Pin by digest. There is a real 2026 incident behind this rule and an interviewer may ask about it.

---

## Fixing the libmagic startup failure (BO-08)

The `api` image crashes at import with `ImportError: failed to find libmagic`. `python-magic` is a
thin ctypes wrapper — it needs the system C library `libmagic1`, which the Dockerfile's runtime
stage never installs. The builder stage has it via build-essential, which is why this stayed hidden
until a rebuild. It causes two `make test-int` failures and blocks the `bo-08` tag.

**1.** Open the Dockerfile and find the runtime stage — the second `FROM`, the one that does not
install build tools.

**2.** Add an install line after that `FROM`, before the `COPY` of application code:

```dockerfile
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 \
 && rm -rf /var/lib/apt/lists/*
```

Put it before the `COPY` so Docker's layer cache keeps it across code edits. If the runtime stage
already has an `apt-get install` block, add `libmagic1` to that list instead of adding a second one.

**3.** Rebuild and restart just that service:

```bash
docker compose build api
docker compose up -d api
docker compose ps --all          # --all, or a crashed container won't appear
```

Wait for `api` to read healthy. If it doesn't, read the logs before changing anything else:

```bash
docker compose logs --tail=50 api
```

**4.** Confirm the fix landed:

```bash
make test-int                    # expect 48 passed, 0 failed
```

Both previously failing tests — `test_healthz_up_readyz_down` and `test_stack_healthy` — should
now pass. If the count is 48 and green, tag:

```bash
git add -A && git commit -m "BO-08: Neo4j graph store; fix missing libmagic1 in runtime image"
git tag bo-08
```

One thing worth noticing about this defect: a stale twelve-hour-old image was serving the whole
time, so every health check passed against a build that no longer matched the Dockerfile. That is
the same shape as the other blind spots in this project — the check ran, it just wasn't looking at
the thing you thought it was. After any Dockerfile change, rebuild before trusting a green run.

---

## The README and the 429 note

BO-07 was supposed to create `README.md` and it never happened, so the note owed since BO-06 has
been unowned across two stages. It's five minutes of work, and leaving it unowned a third time is
how it gets forgotten entirely.

Create `README.md` at the repo root with at least this much:

```markdown
# Hybrid GraphRAG

Dense vector search plus knowledge-graph traversal, with async ingestion, a self-hosted LLM
gateway, a self-correcting query pipeline, and full OpenTelemetry observability.

## Running the tests

    make test        # unit
    make test-int    # integration (quota-consuming tests excluded)
    make test-llm    # the three tests that make real provider calls

`make test-llm` calls live provider APIs on free tiers. **A 429 from it is a quota result, not a
defect** — the Groq free tier is 8000 TPM shared across the organisation. Re-run it later rather
than treating it as a failure.

## Corpus

The evaluation corpus is post-training-cutoff material, so a correct answer demonstrates
retrieval rather than recall.
```

Expand it later — the roadmap section, the architecture summary, and the T1/T2 upgrade path are
worth writing before you show this to anyone. But get the file into the repo now, in the same
commit as the libmagic fix, so the debt is closed.

---

## The `api` container is slow to become healthy (since BO-08)

### What changed and why it matters

BO-08 made `Container.create()` build the Neo4j driver and call `ensure_schema()` **at startup**,
before the app can serve traffic. That is deliberate — better to fail immediately than to serve a
process that will break on its first graph query. But it means `api` now has to wait for Neo4j to
accept connections before it reports healthy, and Neo4j is one of the slowest containers in this
stack to come up. So `api` got slower in this stage specifically. You noticed this; it is expected,
not a bug.

The risk is not the slowness. It's that `test_stack_healthy` will now pass or fail depending on
whether Neo4j happened to be warm. A test that passes on a warm stack and fails on a cold `make up`
is flaky, and flaky tests get ignored — which is how a real failure eventually slips through. So
either the healthcheck's grace period covers the real startup time, or it doesn't and you'll be
re-running tests to make them pass. Fix it once, now.

### Step 1 — Measure it cold

"Cold" means Neo4j starting from nothing, which is the worst case and the one the grace period has
to cover.

```bash
cd ~/projects/GraphRAG
docker compose down
docker compose up -d
```

Then watch until `api` reports healthy, timing it:

```bash
time until [ "$(docker inspect -f '{{.State.Health.Status}}' $(docker compose ps -q api))" = "healthy" ]; do sleep 2; done
```

That prints elapsed time once `api` goes healthy. **Write the number down.** If it never goes
healthy, stop and read `docker compose logs --tail=50 api` — that's a different problem.

### Step 2 — Compare it to the configured grace period

Open `docker-compose.yml`, find the `api` service, and look at its `healthcheck:` block:

```yaml
    healthcheck:
      test: [...]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 30s        # <- this is the one that matters
```

`start_period` is the grace window: failures during it don't count against `retries`. Compare it to
your measured time.

- Measured time is **comfortably under** `start_period` (say, under half) → nothing to do. Note the
  number in `HANDOFF.md` and move on.
- Measured time is **close to or above** `start_period` → go to step 3.

### Step 3 — Raise it, with a comment saying why

Set `start_period` to roughly **double** your measured cold-start time, rounded up. If `api` took 45
seconds, use 90s.

```yaml
    healthcheck:
      test: [...]
      interval: 10s
      timeout: 5s
      retries: 3
      # api calls ensure_schema() against Neo4j during Container.create(), so cold start
      # waits on Neo4j accepting connections. Measured ~45s cold; 90s leaves real headroom.
      start_period: 90s
```

Write the comment. Six months from now the number looks arbitrary without it, and someone will
"tidy" it back down.

Only raise `start_period`. Leave `interval`, `timeout`, and `retries` alone — those govern behaviour
*after* startup, and loosening them would mean a genuinely dead `api` takes longer to be noticed.
The point is to stop punishing a slow start, not to make the healthcheck less sensitive.

### Step 4 — Verify

```bash
docker compose down
docker compose up -d
docker compose ps --all          # --all, or a crashed container won't appear
make test-int
```

Two clean cold runs in a row is the bar. One could be luck.

```bash
git add docker-compose.yml && git commit -m "chore: raise api start_period for Neo4j-dependent startup"
```

### If it doesn't get better

If `api` takes much longer than a minute cold, the cause is probably Neo4j rather than `api` itself.
Check whether Neo4j has a healthcheck and whether `api` waits on it:

```bash
docker compose logs --tail=30 neo4j
```

If `api` has `depends_on: neo4j` with `condition: service_healthy`, it can't start until Neo4j is
ready, and the whole wait shows up as `api` slowness. That's correct behaviour — the number to fix
is still `api`'s `start_period`.

---

## Ingestion failing with `MAX_TOKENS` — diagnosed, BO-10 era

### What happened

The model hit its output ceiling and stopped mid-JSON. Not a provider error, not quota — the call
succeeded, the answer got cut off. For extraction that's worse than a clean failure: truncated JSON
is invalid, which triggers a repair attempt, which makes a **second** full-price call that truncates
the same way. Every failure cost roughly double.

### The evidence, and how to read it

From a Phoenix trace, `llm.gemini.usageMetadata` on a failing extraction:

```
promptTokenCount:     4828
candidatesTokenCount: 11985
totalTokenCount:      16813
```

Two things fall out of those three numbers.

**`4828 + 11985 = 16813` exactly.** The total is fully accounted for by prompt plus output, which
means `thoughtsTokenCount` was zero. Thinking was consuming nothing. `reasoning_effort: low` on
`bulk-high-tpm` had already done its job — that fix worked and is not the remaining problem.

**`candidatesTokenCount: 11985` against a 12000 ceiling** is a clean truncation. The model stopped
because it ran out of room, not because it finished. And since thinking was zero, all ~12k was
legitimate extraction JSON.

That's the diagnostic worth reusing: when a Gemini call fails this way, add prompt + candidates and
compare to total. If they match, thinking isn't your problem and the output is genuinely too big. If
total is larger, the difference is thinking and `reasoning_effort` is the lever.

### Why 12000 wasn't enough

`llm.batching.bulk_chunks_per_request` is 20, and one ceiling covers the JSON for the whole batch.
At ~600 tokens of output per chunk — driven mostly by `RelationOut.evidence_span`, capped at 500
characters, roughly 125 tokens each — twenty chunks is ~12,000 tokens of entirely correct output.
The batch size and the ceiling were set at different times and never reconciled.

### The fix

**You do not need to cap this.** `max_tokens` is a ceiling, not a reservation: you're billed for
tokens actually generated, so a request needing 3k costs the same whether the limit is 4k or 40k. A
low ceiling on the bulk role buys nothing and costs truncated responses plus doubled repair calls.

`gemini-3.5-flash` supports 65,536 output tokens. In `config/base.yaml`:

```yaml
    # bulk covers ALL bulk_chunks_per_request chunks of extraction JSON in ONE response.
    # Measured: 20 chunks produced 11,985 output tokens and truncated at a 12000 ceiling
    # (thinking was 0 — reasoning_effort: low is set on bulk-high-tpm in litellm/config.yaml).
    # 32000 leaves real headroom under the model's 65,536 max. max_tokens is a ceiling, not a
    # reservation — you are billed for what is generated, so a generous value costs nothing.
    bulk:    { model: bulk-high-tpm,    temperature: 0.1, max_tokens: 32000 }
```

Leave the other four roles alone. `router` at 400 and `grader` at 300 are correct — those return
tiny structured verdicts, and a low ceiling there is a genuine runaway guard.

**Mirror it into `config.example.yaml`**, which still says 2000. That file is the template every
future reader trusts; leaving it stale means the next person re-derives this from scratch.

### Which file, and why it matters

`max_tokens` belongs in `config/base.yaml`, app-side. `reasoning_effort` belongs in
`litellm/config.yaml`. They look similar and they are not:

- `reasoning_effort` is a property of the concrete provider deployment — same category as `model:`
  itself. It goes on the alias.
- `max_tokens` is a per-role semantic choice. Five roles share four aliases, so putting it on the
  alias would silently apply one role's ceiling to another role's calls.

`config/base.yaml` is baked into the app, so this needs a restart:

```bash
docker compose restart worker projection-worker api
docker compose ps --all
```

### Verify on one document first

```bash
docker compose logs -f worker
```

Re-run ingestion on a **single** document and confirm a clean extraction with no `MAX_TOKENS` before
re-running the corpus. A wrong value costs twelve documents of quota to discover.

### If it still truncates

Then the batch is genuinely too big for one response and the lever is batch size, not the ceiling.
Lower `bulk_chunks_per_request` from 20 to 10.

**Understand the trade-off first.** The bulk role is RPM-bound, not TPM-bound — the binding
constraint is requests per minute. Halving the batch doubles the request count, moving you toward
the ceiling batching existed to avoid. So raise `max_tokens` first, because it's free; cut batch size
only if that genuinely isn't enough.

A third option if both feel tight: shorten `RelationOut.evidence_span`'s 500-character cap, the
single largest contributor to output size. That's a BLUEPRINT change with real consequences for
citation quality — raise it with the reviewer, don't edit it directly.

### Watch afterwards

A higher ceiling means successful calls generate more tokens than before, so token throughput rises
even though request count doesn't. If Gemini free-tier TPM starts biting you'll see 429s where you
previously saw `MAX_TOKENS`. That's progress, not regression — but it's a different fix, and the
answer there is smaller batches, not a smaller ceiling.

One open question worth asking Claude Code: ~600 output tokens per chunk is high. It may be correct
for dense press releases, or it may mean the extraction prompt is over-extracting — pulling
low-confidence entities and relations that resolution will discard anyway. Worth a look before
BO-11, since it's paid for on every ingestion.

---

## Tuning judge (OpenRouter GLM) and synth (Gemini 3.1 Pro) before BO-11

Good instinct to tune this before M-5/BO-11: evaluating against a golden set while the judge role
is silently running on the wrong model, or synth is timing out on half its calls, would waste the
golden-set effort measuring noise instead of real quality. Two separate problems, two separate
fixes.

### Judge (OpenRouter GLM): reasoning tokens, same failure shape as the Gemini bulk fix

What you read off OpenRouter's logs — 500 configured, ~200 taken by "the provider," ~300 left —
is almost certainly **reasoning tokens**, the same mechanism that broke bulk extraction, just on a
different provider. OpenRouter normalizes a `reasoning` parameter across providers specifically
because many models (including GLM) default to a hidden "thinking" pass that draws from the same
token ceiling as the visible output. GLM models specifically expose this as a plain
**`reasoning.enabled` boolean** (not a graduated effort level like Gemini's) — confirmed on
OpenRouter's own model page for the GLM family. Unlike Gemini's fixed percentage, GLM's thinking
pass generates however many tokens the question seems to need, so "200 out of 500" is just what
happened on that particular call, not a fixed proportion — the next call could take more or less.

**The fix**, mirroring what worked for bulk:

```yaml
# litellm/config.yaml, on the judge-alt-vendor deployment's litellm_params
model_list:
  - model_name: judge-alt-vendor
    litellm_params:
      model: openrouter/<the exact model string configured>
      reasoning:
        enabled: false
```

Verify this actually lands — pass-through of provider-specific params through LiteLLM to
OpenRouter isn't guaranteed for every field, so confirm the same way you confirmed the Gemini fix:
send one call, check the response's `usage` for a `reasoning_tokens` field or gap between
`completion_tokens` and visible content. If `reasoning.enabled: false` isn't honored, raise
`max_tokens` for the judge role instead (same "it's a ceiling, not a reservation" logic — the judge
only returns a small `Entailment` verdict, so there's no downside to a generous ceiling here either,
unlike `router`/`grader` where a small ceiling is a genuine guard).

### The 429s — check the error body, and consider the mundane explanation first

A 429 from OpenRouter's own docs specifies which limit was hit — requests-per-minute,
requests-per-day, or tokens-per-minute — in the response body, not just the status code. Look at
the actual message before assuming which one it is:

```bash
docker compose logs --tail=100 litellm | grep -A5 "429"
```

Before reaching for a config fix: you've been manually firing test queries in quick succession
while debugging, and every truncation-triggered repair is a second full call. Rapid manual testing
plus doubled calls from the reasoning-token problem above is often enough to trip a low free-tier
RPM ceiling on its own, with nothing else wrong. Fix the reasoning-token issue first, then re-test
at a normal pace before concluding the rate limit itself needs a structural fix (spacing out calls,
or checking the specific GLM variant's actual free-tier RPM on OpenRouter's model page — free tiers
vary model to model, the same way Groq's is a flat 8000 TPM per organisation).

### Synth (`gemini-3.1-pro-preview`): this one may not be fixable from your side

This is different in kind from the judge issue. `gemini-3.1-pro-preview` has a well-documented
history of transient Google-side capacity rejections — `429 RESOURCE_EXHAUSTED`, `503` "experiencing
high demand" — recurring for months after release, independent of any caller's config, quota, or
key. Your own observation fits that pattern exactly: Google AI Studio's dashboard shows failed
requests but **zero usage** under this model for your dedicated key — consistent with requests
being rejected before the model ever starts, which is what capacity-based rejection looks like from
the outside. This is very likely not a bug in your setup.

Two real options, and this is your call, not a config tweak:

1. **Keep it and absorb the flakiness.** `num_retries: 2` plus the fallback to `judge-alt-vendor`
   already exist for exactly this. Once judge-alt-vendor's own reasoning-token issue is fixed, the
   fallback chain should work as designed when synth's primary is unavailable — you'd get a slightly
   lower-quality answer on those calls, but a working one.
2. **Switch synth to `gemini-3.5-flash`.** At least one independent model-status tracker has
   explicitly suggested exactly this swap when `gemini-3.1-pro-preview` shows concurrency problems.
   The real cost: `bulk` already uses a Gemini Flash-family model (§4 of `HANDOFF.md` — pending your
   flash-vs-flash-lite decision there too), so this would mean two roles sharing very similar
   underlying capability rather than a distinct "quality" tier for synthesis. That's a real
   trade-off on answer quality, not a free win — decide it deliberately, the same way you're holding
   off on the flash/flash-lite call for bulk.

If you decide to switch, say so and I'll fold it into the spec (`HANDOFF.md` §4 and the aliasing
note) rather than leaving the stale model name for the next person to trip on.

---

## Router chose "graph" for a narrative question — resolved with real evidence

### What you found, and where I was wrong

Earlier I offered three possible mechanisms for why `strategy: graph` missed the 1997-losses
narrative. You've now tested this directly, and the evidence changes the picture:

- **Only one document was ingested.** My "per-hop cap crowds it out on a busy 12-document hub"
  hypothesis cannot be the cause here — there's no hub to be crowded on a single document. That
  guess was wrong; I'm retracting it. (It's not permanently wrong — once the corpus scales back up
  to the full 12 documents, a busy Apple hub competing for the top-25-per-hop slots becomes a real
  risk again. Worth re-testing this specific query once more documents are back in, not assuming
  it's resolved for good.)
- **Forcing `strategy: vector` produced an excellent, well-cited answer** — Jobs' return, the NeXT
  acquisition, Microsoft's $150M investment, ending clone licensing, the Power Computing purchase,
  the online store, the product-line cuts, and the $309M year-end profit, cited to five real chunks.
  This directly confirms the mechanism that matters: choosing `graph` alone forecloses
  `retrieve_vector` entirely, and vector alone had everything needed. That part of the original
  diagnosis holds and is now proven, not just theorized.
- **`NeXT` is in the graph.** You found the edge yourself: *"Apple instead acquired NeXT, the
  company founded by Steve Jobs, for its NeXTSTEP operating system."* — a real, correctly-extracted
  relation. But its evidence text is narrowly about *why* NeXT was chosen (the NeXTSTEP OS), not the
  surrounding narrative about the 1997 losses or the comeback products. That's a **different
  sentence, a different chunk** than the one carrying the losses/iMac/iPod narrative. A graph
  traversal that finds this edge and hydrates its chunk gets the acquisition rationale, correctly —
  it was never going to reach the other chunk unless *that* chunk also produced an edge reachable
  from "Apple."

So the precise gap: the sentence *"Under his leadership, Apple returned to profitability through
the iMac, iPod, iPhone, and iPad…"* most likely never produced its own `Apple → CREATED/LAUNCHED →
{iMac, iPod, iPhone, iPad}` relations at all. And *"Apple was reporting major losses"* has no
natural two-entity relation to become — a loss is a fact about one entity, not a link between two.

### Your instinct about graph bloat is correct — don't fix this by extracting more

You asked: if every small fact got extracted as an entity/relation, wouldn't the graph get bloated?
Yes, and that's the right frame for the decision here. Forcing extraction to capture every
distributively-listed product mention or every monadic financial fact fights what a relational
graph is good at (crisp, binary, lookup-style facts) and duplicates what vector search already
does better, as your own test just proved. **Don't ask Claude Code to make extraction more
exhaustive to catch this class of fact.** It would cost more tokens per batch (the same ceiling
tension as bulk's `MAX_TOKENS` problem) for coverage vector search already provides directly from
raw text.

### The fix is on the router side — and given the evidence, worth doing now, not waiting for M-5

Last time I said not to patch the router prompt off one anecdote, and I'm updating that now that
you've actually established *why* it fails, not just *that* it failed once. This isn't corpus-
specific overfitting anymore — it's a general property: an open-ended "how/why did X do Y" question
about a process or history needs the connecting narrative prose, which lives in chunk text, not in
discrete relation triples. That reasoning holds regardless of which document or corpus you're
querying.

**Still add the query to the M-5 golden set** — as a regression check, not as the sole evidence for
the fix. And this belongs in a **Claude Code prompt**, not a config edit: prompt template wording is
a BO-06 build artifact Claude Code owns, not something to hand-edit directly. When you're ready, I
can draft a short, scoped prompt asking it to add router-prompt guidance and a contrasting few-shot
example (narrow relational lookup → graph; open-ended narrative/causal → hybrid), without touching
anything else in the orchestration pipeline.

### One thing that went right, worth noticing

When graph-only retrieval was inadequate, the pipeline didn't hallucinate — it returned "insufficient
context," the `insufficient` node's designed refusal path (HTTP 200, not an error). That's
`verify_grounded`/the self-correction design working as intended even while retrieval itself picked
the wrong strategy. The bug is in routing, not in groundedness — worth keeping that distinction
clear when you write this up for M-5 or a resume/portfolio description.

---

## Debugging one request end-to-end — you already have this tool

### Use the CLI, not the HTTP endpoint

`graphrag trail <correlation_id>` is the single point of source you're asking for. Built in BO-02,
gate-tested by `test_trail_cli_roundtrip`:

```bash
graphrag trail 01M1KKNWKA43CWBTHRTBH6Z4EA
# writes debug_bundle_01M1KKNWKA43CWBTHRTBH6Z4EA.md in the working directory
```

Every query response returns its `correlation_id` at the top level — the one in your last vector
query was `01M1KKNWKA43CWBTHRTBH6Z4EA`. The bundle contains spans and log lines **merged into one
timestamp-ordered sequence**, secrets redacted, each field truncated to
`observability.trail.truncate_field_chars`. That is the node-by-node story of one request: which
nodes ran, in what order, what each returned, where it failed.

**`GET /v1/debug/trail/{cid}` returns `{}`.** BO-10 implemented only its auth and env gating; the
lookup itself was left a stub. Don't use it — that's on me, I gave you that curl command in the
previous round knowing the stub was recorded. Wiring it to the existing `TrailBuilder` is a small
follow-up, not new work.

If the bundle comes back empty, that's a known-shape failure with a documented cause: the Loki
label is `service_name`, not `service` (Loki's OTLP path replaces the dot in `service.name` with an
underscore), and an empty bundle looks identical to "logs were never exported." BLUEPRINT §4.6
spells this out.

### What the trail can't tell you yet, and the fix

The trail answers "what happened in my app." It cannot answer **"why did the gateway fall back."**

Your app span records `llm.role`, `llm.alias`, `llm.model_served` — so you can see that a call meant
for `synth-quality` was served by Groq. But the *reason* (a 429 from Google, a 503, a timeout)
happens inside LiteLLM, and LiteLLM's logs aren't in the Loki/Tempo stream the trail queries. Two
systems, no join key. That's the gap you're feeling, and it's real — not you misreading the tools.

**The join key already exists in this codebase.** `x-litellm-trace-id` → `session_id` is the exact
mechanism that fixed `test_cost_tracked` at BO-06. Send the request's `correlation_id` as that
header on every gateway call, and LiteLLM stores it in the `session_id` column of
`LiteLLM_SpendLogs`. Then one ID retrieves both halves: the app's node sequence from the trail, and
every gateway attempt — including failed ones, with `attempted_fallbacks` and `original_model_group`
— from `/spend/logs`.

That is a genuine spec gap, not a Claude Code error. Nothing in BLUEPRINT ever asked for the
correlation ID to be propagated to the gateway. Worth fixing as a small scoped change alongside
wiring the endpoint: it converts fallback debugging from "dig through Phoenix attributes and guess"
to one lookup.

### Meanwhile, the fastest manual path

Until that lands, two commands answer most of it:

```bash
graphrag trail <cid>                       # what your app did
docker compose logs --tail=200 litellm | grep -i "fallback\|429\|503"   # why the gateway switched
```

---

## Why you can't test one module at a time — and what's missing

### The design supports it; the tooling was never specified

The architecture is genuinely componentised. Every orchestration node is
`async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]` — it takes state, returns a
partial update, mutates nothing. BLUEPRINT §6.6 states outright that each is independently
unit-testable with a hand-built state. Retrieval, resolution, and the graph store all sit behind
Protocol ports, so any one can be exercised against real backends without the rest of the pipeline.

So the separation you're asking for is already in the code. What's missing is an **entry point** —
a way for *you* to run one node, by hand, with your own question and context, and see what it
returns. Unit tests exercise these components, but they're written for CI, not for a human iterating
on a prompt.

**That's a real gap in my spec work, not a Claude Code failure.** I specified the components as
independently testable and never specified the harness that makes that property usable by a person.

### What to ask for

A `graphrag node` CLI command, added to the existing Typer app in `apps/cli/main.py` (which already
hosts `trail`, and which BO-05 and BO-11 were always going to extend):

```bash
graphrag node plan_route --question "How did Apple manage its losses in 1997?"
graphrag node grade_context --question "..." --chunks-from <cid>
graphrag node generate --question "..." --chunks-from <cid>
```

Each constructs a minimal `QueryState`, runs exactly one node against real `NodeDeps`, and prints
the returned partial update plus the LLM call it made. `--chunks-from <cid>` reuses the retrieved
context from a previous real query, so you can iterate on the `generate` prompt without re-running
retrieval and re-spending quota every time.

This is the tool that makes golden-set work tractable. Tuning a router prompt by running full
end-to-end queries costs four LLM calls and ~25 seconds each; running `plan_route` alone costs one
call and about a second. Worth building **before** M-5, not after — it's the difference between
tuning against 50 golden items being an afternoon or a week.

---

## Always-hybrid: an honest critique, and the version of it I'd actually ship

Your argument: making every query hybrid means that however dense or sparse the graph is, you never
lose vector search's advantages. That's largely right, and the failure you found is real evidence
for it. Here's the honest case against, so you can decide on the strongest version of both sides.

### Arguments against always-hybrid

**1. RRF dilution — the strongest objection.** `fuse` converts `GraphPath`s to `ScoredChunk`s and
applies reciprocal rank fusion. RRF is rank-based, so *every* list contributes ranks regardless of
quality. On a purely semantic question the graph will still return *something* — its top paths from
whatever entities got linked — and those ranks push genuinely good vector chunks down. Graph-only
fails loudly (you saw it: an honest refusal). Hybrid fails quietly, by making a good answer slightly
worse, which is much harder to detect and exactly what a golden set is for.

**2. Grader cost is real, unlike retrieval cost.** Retrieval itself is cheap — Qdrant and Neo4j, no
tokens, and `retrieve_vector`/`retrieve_graph` fan out in parallel, so wall time is roughly the max
of the two rather than the sum. But `grade_context` batches over the *fused* set at
`orchestration.grader.batch_size: 8`. A bigger fused set means more grader batches, and grading is
LLM calls. Always-hybrid raises token spend on every query, on free tiers you're already hitting
ceilings on.

**3. You lose the diagnostic signal.** If strategy is always hybrid, `plan_route`'s `strategy`
output becomes vestigial. Right now, when retrieval goes wrong, the plan tells you what the router
believed — that's how you found this problem in the first place. Hardwiring hybrid removes the
evidence that lets you notice the next miscalibration.

**4. It makes routing unmeasurable, right before you build the thing that measures it.** BO-11's
evaluation harness is meant to score retrieval quality. If every query takes the same path, there's
nothing left to evaluate about routing — you'd be removing a variable one stage before the tooling
that could tell you whether it was worth keeping.

**5. Portfolio framing.** "Self-correcting pipeline that routes queries by shape" is a stronger
claim in an interview than "we always run both and fuse." That's not a technical argument and
shouldn't outweigh one, but this is a CV project and it's worth naming rather than pretending it
isn't a consideration.

### What I'd actually ship: ban graph-only, keep the other two

The failure you hit was **graph-only**, not "insufficiently hybrid." Look at the asymmetry:

- `vector` alone: works well on this corpus, cheap, one backend. Your own test proved it produces a
  fully-cited, high-quality answer.
- `hybrid`: safe default, slightly more expensive, some dilution risk.
- `graph` alone: **can catastrophically fail** — it forecloses the retrieval path that had the
  answer, and the only recovery is an honest refusal.

Graph-only is the sole strategy with a catastrophic failure mode, because it's the only one that
excludes the general-purpose retrieval method. Vector-only is a *safe* narrowing; graph-only is a
*dangerous* one.

So: make `graph` unreachable as a terminal strategy — the router may choose `vector` or `hybrid`,
and anything it would have routed to `graph` becomes `hybrid`. You keep the routing signal, keep
cheap vector-only for straightforward semantic questions, and eliminate the one path that can lose
the answer outright. `orchestration.default_strategy` is already `hybrid`, so the fail-open behaviour
is unchanged.

This is a smaller change than always-hybrid, removes the same failure mode, and — unlike
always-hybrid — leaves BO-11 something to measure.

### Either way, measure it rather than deciding it permanently

Once the golden set exists, this is a directly measurable question: run the 50 items under
always-hybrid, under routed-with-graph-banned, and under the current routing, and compare retrieval
quality and token spend. That's a genuinely strong thing to have measured and written up — "we
tested three routing policies against a golden set and chose on evidence" is a much better story
than either policy chosen on argument alone.

Ban graph-only now, because the failure mode is proven and the fix is cheap. Hold always-hybrid as a
measured comparison for BO-11 rather than a decision made today.

---

## Known issue — the worker can wedge and stay wedged

Seen once during BO-06: `test_stack_healthy` failed because the `worker` container had been
unhealthy for **42 minutes**. Cause was a stale `delete_document` job raising `ConflictError:
cannot set status on unknown document`, which broke the worker's health-check sentinel. The
process never recovered on its own.

```bash
docker compose restart worker      # healthy again in ~15s
```

Two things to take from it. The restart is the workaround, not a fix — the underlying
ingestion/ledger behaviour (a job referencing a document the ledger no longer knows about) is
untouched, and it will recur. And a worker that sits dead for 42 minutes while you're working is
exactly the silent-blind-spot shape this project keeps getting bitten by: nothing shouted, the
failure only surfaced because an unrelated test happened to check.

**If an integration test fails for no reason you can connect to your change, check container
health before you debug the test.** `docker compose ps --all` — with `--all`, or crashed
containers don't appear at all.

Revisit properly at BO-12 (hardening) if it happens more than once.

---

## When Something Goes Wrong

**1. Get the correlation ID.** Every error response carries one. It also appears on the
`X-Correlation-ID` response header and in every log line for that request.

```json
{ "error": { "code": "RETRIEVAL_BACKEND_UNAVAILABLE",
             "correlation_id": "01J8XQ4ZK7M2N9P3R5T7V9W1XY", ... } }
```

**2. Dump the trail.** This is the CLI you build in BO-02 (`apps/cli`, backed by
`telemetry/trail.py`). Run it from the repo root:

```bash
graphrag trail 01J8XQ4ZK7M2N9P3R5T7V9W1XY
# or, if you haven't installed the package locally:
docker compose exec api python -m graphrag.apps.cli trail 01J8XQ4ZK7M2N9P3R5T7V9W1XY
```

It queries Loki and Tempo for everything tagged with that ID and writes a single file to your
working directory:

```
debug_bundle_01J8XQ4ZK7M2N9P3R5T7V9W1XY.md
```

Inside: the config hash, which route the query took, per-node timings, every LLM call with its
model and repair count, and the full stack for anything at ERROR — all in time order, secrets
redacted, long fields truncated.

**3. Hand that file to a fresh session**, with a line like:
*"BO-10, `test_grade_loop_bounded` fails. Trail bundle attached."*

The point is that one file replaces the guesswork. A stack trace shows you where it broke; the
bundle shows you the sequence that got it there — which node ran, what it retrieved, which model
answered, how many times it retried.

> **Before BO-02, this doesn't exist yet.** For BO-00 and BO-01 failures, `make test -x` output
> plus `docker compose logs` is all you have — and all you need, since nothing is distributed yet.

### Docker traps specific to this project

Four failure modes that cost real time. Each looks like something other than what it is.

**1. A bind-mount to a missing file silently creates a *directory*.**
If `docker-compose.yml` mounts `./litellm/config.yaml` and that file doesn't exist, Docker doesn't
error — it creates an empty **directory** at that path, owned by root. The container then
crash-loops with `IsADirectoryError`, which reads like a code bug rather than a missing file.

```bash
ls -la litellm/          # if config.yaml shows as 'd' (directory), this happened
sudo rm -rf litellm/config.yaml
# then create the real file BEFORE `make up` again
```
Root-owned because the Docker daemon created it, so plain `rm` gives permission denied — `sudo`
is correct and expected here, not a sign anything is wrong.

**2. `docker compose ps` hides crashed containers.**
It lists only *running* services by default. A container that started, crashed, and exited simply
isn't in the output — so a health check counting what it sees can report success on the survivors
while a service is dead. **Always use `docker compose ps --all`.** Any test that verifies stack
health must pass `--all`, or it can pass while the stack is broken.

**3. Services that can't be healthy until a later BO.**
`litellm` needs `litellm/config.yaml`, which BO-06 owns. BO-00 ships a minimal stub so the
container can start; without it, `test_stack_healthy` fails for a reason that has nothing to do
with your code.

**4. The litellm healthcheck endpoint matters.**
Use `/health/liveliness` (is the process up). Do **not** use `/health` — that actively probes
every configured provider and fails whenever keys are placeholders or a free tier is rate-limited,
turning an unrelated outage into a red stack.

**5. `mem_limit` too low reads as a crash, not a config error.**
A container killed for exceeding its limit exits with **137** and simply restarts — the logs show
no error, because the process was killed from outside. `docker inspect --format='{{.State.OOMKilled}}'
<container>` returns `true`. Measured minimums for this stack:

| Service | `mem_limit` | Note |
|---|---|---|
| qdrant | `1g` | |
| neo4j | `1500m` | plus `NEO4J_server_memory_heap_max__size=1g` |
| postgres | `512m` | |
| redis | `256m` | |
| litellm | `1500m` | Python app on a ~1.9 GB image; 512m OOMKills reliably |

Core total ≈ **4.75 GB**. Adding `obs` (otel-lgtm ~1.5 GB, phoenix ~1 GB, collector ~256 MB)
brings it to ≈ **7.5 GB** — which is why M-1a asks for 8 GB.

**Expected state at the end of BO-00:** five core services, all `healthy`, `litellm` included via
the stub. Neo4j takes ~30 s and is always the last to arrive.

```bash
make up
docker compose ps --all        # all five: healthy
make test-int
make down
```

### Pasting into WSL can inject CRLF

Copying a `cat > file <<'EOF'` block from a browser or chat window on Windows and pasting it into
the Ubuntu terminal carries `\r\n` through the clipboard. The heredoc writes those bytes
verbatim, so you get a CRLF file inside WSL — where you'd never expect one. `.gitattributes`
doesn't help: it normalizes on commit, not on paste.

The symptom is delayed and misleading. YAML and JSON tolerate CRLF, so the file *works*; then
`test_no_crlf_in_repo` fails later, or a shell script dies inside a container with
`bad interpreter: /bin/bash^M`.

```bash
file <path>                       # "with CRLF line terminators" = affected
sed -i 's/\r$//' <path>           # fix in place
grep -rlIU $'\r' . --exclude-dir=.git    # find every affected tracked file
```

Prefer creating files with an editor (`code .`) or letting Claude Code write them — both produce
LF. Reserve pasted heredocs for cases where you'll check with `file` afterwards.

**Common ones:**

| Symptom | Cause |
|---|---|
| Compose won't start, no clear error | RAM. See M-1a — on WSL2 the limit is in `.wslconfig`, not Docker's settings |
| Neo4j connection refused on first run | It takes ~30 s to accept Bolt. Check `depends_on: service_healthy` |
| Sparse retrieval is mediocre, no errors | **The IDF modifier.** Recreate the collection and re-index |
| `InvalidUpdateError` in the graph | A `QueryState` key written by parallel nodes is missing its reducer |
| Answers cite chunks that don't exist | `verify_citations` isn't wired, or `graded` isn't what's being checked against |
| Free-tier 429s during ingestion | RPM, not TPM. Raise `bulk_chunks_per_request` — batch harder, don't call more |
| Second eval run scores better than the first | Cache wasn't disabled. Check `disable_cache_during_run` |
| Traces appear in Grafana but not Phoenix | Collector's Phoenix exporter, or someone added a span filter |
| `make: command not found` | You're in PowerShell, or skipped `apt install make`. See M-0 |
| `docker: command not found` inside Ubuntu | Docker Desktop → Settings → Resources → **WSL Integration** → enable Ubuntu |
| `&&` throws a parser error | PowerShell 5.1 doesn't support it. Use the Ubuntu shell (M-0) |
| Tests take minutes instead of seconds | Repo is under `/mnt/c/`. `pwd` should start `/home/`. See M-0 step 4 |
| `docker compose restart <svc>` fails with a stale bind-mount error (WSL2) | Known flaky corner of Docker Desktop's WSL2 backend, not a config bug. Use `docker compose rm -f <svc> && docker compose up -d <svc>` instead — same effect, more reliable |
| `bad interpreter: /bin/bash^M` | CRLF line endings. `git config --global core.autocrlf input`, add `.gitattributes`, re-checkout |
| `test_no_crlf_in_repo` fails on a file you pasted | Windows clipboard carried `\r\n` into the heredoc. `sed -i 's/\r$//' <path>`. See the CRLF section above |
| Ports bound but nothing responds | Docker publishes to the Windows host; from Ubuntu use `localhost`, which WSL2 forwards. If it fails, check `localhostForwarding=true` in `.wslconfig` |
| `IsADirectoryError` on a mounted config | Docker created a directory because the file was missing. See Docker trap 1 above |
| A service is "healthy" but the app can't reach it | You ran `docker compose ps` without `--all` and a crashed container is invisible. See trap 2 |
| Phoenix exits with `PhoenixMigrationError` | A floating image tag moved under an existing schema. Pin by digest; give Phoenix its own database so it can be reset without touching LiteLLM's tables |
| A stack-health check passes while a service is dead | The check can't see it. Two causes seen so far: missing `--all` (crashed containers hidden) and missing `-f docker-compose.obs.yml` (obs services never enumerated) |
| A worker sits in `health: starting` forever, no errors | The healthcheck is probing something the process doesn't do. arq workers serve no HTTP — probe `arq --check`. And set `health_check_interval` (arq defaults to 3600s), shorter than the compose `interval` |
| `test_stack_healthy` fails only on litellm | Missing `litellm/config.yaml` stub, healthcheck hitting `/health` instead of `/health/liveliness`, or `mem_limit` under 1500m. See traps 3, 4 and 5 |
| Container restarts with empty logs, exit code 137 | OOMKilled. `docker inspect --format='{{.State.OOMKilled}}' <name>` confirms it. Raise `mem_limit`; see trap 5 |
| Integration test can't find `docker-compose.yml` | A test fixture chdir'd away from the repo root. Isolation fixtures must skip tests marked `integration` |
| Integration tests hit the wrong containers | **Git worktrees each get their own Compose project namespace**, derived from the directory name. `docker compose stop qdrant` in a worktree stops `<worktree>-qdrant-1`, not the `graphrag-qdrant-1` your running `api` is talking to — so the test "stops" a container nothing uses and the assertion fails for a reason that looks like a code bug. Run integration tests from the main checkout, or set `COMPOSE_PROJECT_NAME=graphrag` in the worktree. Check with `docker compose ps --all` and confirm the container name prefix |

## Command reference: docker and git

Reference only — for the *why* behind any of these, see the Docker traps and Git sections above.
These are the ones that have actually earned their keep during review, not a generic cheat sheet.

### Docker: inspecting a running or misbehaving stack

```bash
docker compose ps --all                          # ALWAYS --all; plain ps hides crashed containers
docker compose logs <service> --tail=80           # last N lines, no follow
docker compose logs <service> --tail=5 -f         # live tail — leave running ~30s to tell hung from quiet
docker compose exec <service> sh -c '<cmd>'        # run something inside a container
docker top <container-name>                        # host-side process list — works even with no shell tools installed in the image
docker stats --no-stream <container> [<container>...]   # one-shot CPU/mem snapshot, not the live dashboard
docker inspect <container> --format='{{json .State.Health}}' | python3 -m json.tool
                                                    # full healthcheck log: exit codes, timing, output —
                                                    # docker compose ps only shows the current status word
docker inspect --format='{{.State.OOMKilled}}' <container>   # true = killed for exceeding mem_limit, not a code crash
```

Timing a command inside a container — `time` often isn't installed in slim images; bracket with `date` instead:
```bash
docker compose exec <service> sh -c 'date +%s.%N; <cmd>; echo "exit:$?"; date +%s.%N'
```

### Docker: Redis, when debugging arq/queue state

```bash
docker compose exec redis redis-cli KEYS "<pattern>"       # exact pattern match — easy to guess wrong
docker compose exec redis redis-cli --scan --pattern "*<term>*"   # broader, safer first pass — use this before KEYS
docker compose exec redis redis-cli GET "<key>"
docker compose exec redis redis-cli TTL "<key>"             # -2 = key doesn't exist, -1 = exists with no expiry
docker compose exec redis redis-cli LLEN "<queue-key>"      # queue backlog depth
```

### Docker: recovering from a wedged service

```bash
docker compose restart <service> [<service>...]   # first thing to try
docker compose rm -f <service> && docker compose up -d <service>   # WSL2 fallback if restart hits a stale bind-mount error
```

### Git: history and diffing, beyond the basic loop in "Git, and the worktree trap" above

```bash
git log --oneline -5                                       # recent commits
git log -p -- <path>                                       # every commit that ever touched a file, with full diffs —
                                                              # THE tool for "did X always work this way?" — trust this
                                                              # over any prose claim about history
git log -p -L '/def <function>/,/return/:<path>'           # same, scoped to one function's line range
git diff <tag-or-commit> -- <paths>                         # diff against a checkpoint, not just working tree
git blame <path>                                            # who/when introduced a specific line
```

**Diffing untracked files.** Plain `git diff` is silent on files git has never seen — no error, just
nothing, which reads as "no changes" when it actually means "no baseline exists yet." This bit a
review mid-session: an empty `git diff` was mistaken for "nothing changed" when the real state was
"these files were never committed." Confirm which case you're in before trusting an empty diff:
```bash
git status                        # check for "Untracked files" first
git add -N .                      # "intent to add," stages no content — makes git diff show new files as additions
git diff                          # now renders untracked files in full instead of hiding them
git reset                         # un-stage the intent-marks — nothing gets committed by this
```

