# MANUAL — Your Side of the Build

For you, not the agent. What you do, what to expect, and where to intervene.

**Reading order:** **M-0 first** (one-time environment setup) → the rest of this file → hand
`BLUEPRINT.md` + `BUILD_ORDER.md` to Claude Code → work one BO at a time.

---

## How to run this

Start each build order in a **fresh Claude Code session**, launched from your project directory
in the **Ubuntu (WSL2) terminal** — see M-0. Give it exactly this:

> `BUILD_ORDER.md` tells you what to build; `BLUEPRINT.md` is the authoritative spec for every
> name, signature and contract. Read `ARCHITECTURE.md` only for rationale when a contract looks
> arbitrary — where the two disagree, BLUEPRINT wins, and report the disagreement. Never
> implement anything from ARCHITECTURE's Appendix A: it documents rejected designs on purpose.
>
> Implement **BO-0X** only. Do not implement components from other build orders.
> Follow each component's contract in BLUEPRINT exactly. Consult BLUEPRINT §1a (Type Index)
> before referencing any type; if one you need isn't listed, that is a spec gap — report it
> rather than inventing it.
>
> **The spec files are read-only.** `ARCHITECTURE.md`, `BLUEPRINT.md`, `BUILD_ORDER.md`,
> `MANUAL.md` and `config.example.yaml` must never be edited. If you find an error, ambiguity
> or gap, report it and continue with the rest of the build order — or stop if it blocks you.
> Corrections are applied upstream and synced back into the repo.
>
> Write the BO's tests after the components. Run `make lint && make test`.
> Report which gate tests `[G]` pass, individually.
> If a contract is ambiguous or a dependency is missing, stop and ask — do not guess.

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
| `make test` | `pytest -m "not integration and not eval"` — fast, no containers. `make test-int` adds integration tests, which need `make up` first. |
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

Good sources: company annual reports, Wikipedia exports on a connected topic, open-access papers
from one research group. **Avoid anything confidential** — free LLM tiers may train on prompts.

> This is the highest-leverage hour in the project. A weak corpus makes every downstream metric
> meaningless and you will not notice until BO-11.

### M-3 · Before BO-06 — API keys
Create free accounts and put keys in `.env`:
- **Groq** — console.groq.com. No card. Still publishes its rate-limit table.
- **Google AI Studio** — aistudio.google.com/apikey. **Check your actual limits in AI Studio**; Google no longer publishes free-tier numbers, so whatever any doc says is a guess.
- **OpenRouter** — openrouter.ai/keys. For `:free` models.

**Create two keys per provider where allowed** — key rotation is a feature you're claiming, and
`test_key_rotation_spreads_load` needs a second key to be real.

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
- **DON'T run integration tests against your demo data** without a reset — several tests delete documents.
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
| `bad interpreter: /bin/bash^M` | CRLF line endings. `git config --global core.autocrlf input`, add `.gitattributes`, re-checkout |
| `test_no_crlf_in_repo` fails on a file you pasted | Windows clipboard carried `\r\n` into the heredoc. `sed -i 's/\r$//' <path>`. See the CRLF section above |
| Ports bound but nothing responds | Docker publishes to the Windows host; from Ubuntu use `localhost`, which WSL2 forwards. If it fails, check `localhostForwarding=true` in `.wslconfig` |
| `IsADirectoryError` on a mounted config | Docker created a directory because the file was missing. See Docker trap 1 above |
| A service is "healthy" but the app can't reach it | You ran `docker compose ps` without `--all` and a crashed container is invisible. See trap 2 |
| `test_stack_healthy` fails only on litellm | Missing `litellm/config.yaml` stub, healthcheck hitting `/health` instead of `/health/liveliness`, or `mem_limit` under 1500m. See traps 3, 4 and 5 |
| Container restarts with empty logs, exit code 137 | OOMKilled. `docker inspect --format='{{.State.OOMKilled}}' <name>` confirms it. Raise `mem_limit`; see trap 5 |
| Integration test can't find `docker-compose.yml` | A test fixture chdir'd away from the repo root. Isolation fixtures must skip tests marked `integration` |
| Integration tests hit the wrong containers | **Git worktrees each get their own Compose project namespace**, derived from the directory name. `docker compose stop qdrant` in a worktree stops `<worktree>-qdrant-1`, not the `graphrag-qdrant-1` your running `api` is talking to — so the test "stops" a container nothing uses and the assertion fails for a reason that looks like a code bug. Run integration tests from the main checkout, or set `COMPOSE_PROJECT_NAME=graphrag` in the worktree. Check with `docker compose ps --all` and confirm the container name prefix |
