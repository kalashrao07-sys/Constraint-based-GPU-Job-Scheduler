# Constraint-Based GPU Job Scheduler

**KLE Technological University, Dr. M. S. Sheshagiri Campus, Belagavi · Mini-Project · Team 7**
Guide: **Prof. Amey Muchandi**

A scheduler that shares a limited pool of GPUs among many users, never breaking a rule, and runs their code on a real GPU. It ships as two portals on one engine: the **KLE Tech portal** (college GPU lab) and **SpareGPU** (a marketplace for idle GPUs).

---

## 1. Problem Statement

GPUs are expensive and scarce. In a college lab, many students need them at the same time, and today access is manual: people wait, ask around, or find a GPU already taken. Three problems follow.

- **Conflicts.** Two jobs can land on one GPU, or a job can be sent to a GPU without enough memory, so it crashes.
- **Unfair and wasteful use.** Nothing decides who goes first, and GPUs sit idle while others queue.
- **No safe, honest way to run code.** Running a stranger's code on a lender's or lab's machine is a security risk, and a portal that pretends a job ran when no GPU was available is worse than useless.

Outside the college, many people own capable GPUs that sit idle, while others cannot afford one. There is no simple way to connect them.

## 2. Solution

A **constraint-based scheduler** decides which job goes to which GPU. Hard constraints can never be violated; soft preferences only rank the valid options. Every assignment is saved in a single atomic database transaction, so crashes and simultaneous requests cannot leave the system inconsistent. A small **agent** on each GPU machine runs the job inside a locked-down Docker sandbox on that machine's own GPU and reports real output, errors and status back. A web portal with a VS Code-style editor lets users write, run and watch code. If no GPU is online, the portal says so and starts nothing.

## 3. Detailed Information

### Hard constraints (never broken)

| Code | Rule |
|---|---|
| HC1 | The GPU must have at least the memory the job asks for. |
| HC2 | One job per GPU at a time. |
| HC3 | Assignment is atomic: either everything is recorded or nothing is. |
| HC4 | User code runs only on machines whose sandbox is enabled. |
| HC5 | The job must fit inside the GPU owner's availability hours (SpareGPU). |
| HC6 | The GPU's price must not exceed the renter's maximum (SpareGPU). |
| HC7 | A private GPU serves only the people its owner trusts (SpareGPU). |

### Soft preferences (rank the valid GPUs, in this order)

1. **SP1:** a priority queue (heap) serves higher-priority jobs first, then earlier submissions.
2. Lowest price (SpareGPU).
3. **SP2:** the GPU that has been idle for the shortest time, which keeps GPUs warm and spreads work sensibly.
4. **SP3:** a GPU of the same type the user used before.
5. Lowest GPU id as the final tie-breaker.

### How a job flows

1. The user saves a script in the editor and presses **Run** with a memory and a time limit.
2. The portal checks GPU status first. With no suitable GPU online it shows a red error and creates no job.
3. The job is stored as *queued*. The scheduler picks it from the heap and filters the GPUs by the hard constraints. If none qualify, the job is *held* with a plain-language reason (for example "no GPU has enough memory" or "a suitable GPU exists but none is currently free").
4. For the best GPU, the assignment is committed in one transaction that re-checks every rule inside the transaction.
5. The agent on that GPU machine receives the job at its next heartbeat and starts it in a Docker sandbox pinned to that GPU.
6. Output streams back live. The user can press **Stop**, and the job ends on its own at its time limit.
7. The GPU is released and the next waiting job is assigned.

### Failure handling (honest, never fake)

- **No GPU online:** nothing starts, and the reason is shown.
- **Worker disconnects mid-run:** after 20 seconds without a heartbeat the job goes back to the queue and the offline worker is not used again.
- **Job exceeds its time:** the agent kills it, and the server closes it as a safety net if the agent cannot.
- **Cancel:** a queued job is cancelled at once; a running job is killed on the worker's next poll.
- **Server restart or database failure during assignment:** the transaction rolls back completely and the next pass succeeds.

### Sandbox and security

User code runs in Docker with no network, all Linux capabilities dropped, no privilege escalation, a read-only filesystem with a small temporary area, a non-root user, memory and CPU limits, and only the one allocated GPU visible. File paths are validated so code cannot escape the user's workspace. Agents authenticate with a secret token (stored only as a hash). Users cannot see each other's jobs, logs or files, and admin pages are checked on the server.

## 4. The Two Platforms

Both run on the same scheduler, agent and sandbox code, but with separate data, accounts and look.

### KLE Tech portal (`python run.py`, port 5000), the main demo

For students and the GPU lab admin.

- **Students:** a VS Code-style editor (syntax highlighting, folders, tabs, create, rename and delete files, line numbers), Run and Stop buttons, an Output panel with live logs, an interactive GPU terminal, and job history with queue position and status.
- **Admin:** register servers with their GPU model and memory, see each GPU as available, occupied, offline or disabled, watch live utilisation and memory use, edit, disable or enable GPUs, suspend or remove servers, and see held jobs and fleet status.
- Students never register GPUs; the admin adds the lab's machines.

### SpareGPU (`python run.py --portal exchange`, port 5100)

A marketplace for idle GPUs.

- **Lenders** register a laptop or PC, set a price per hour (or free), available hours, and whether it is private (trusted people only), and can pause or stop sharing at any time.
- **Renters** browse the market and rent a GPU; the editor appears only once a GPU has actually been allocated to them.
- The same constraints (memory, price, hours, trust, sandbox) decide every match.
- Wallet and credits exist in the code but are switched off (`CREDITS_ENABLED = False`).

## 5. Team

| Member | Engine | Contribution |
|---|---|---|
| **Kalash Rao** | **E1: Scheduling engine** (`e1_scheduling/`, `rules.py`) | Priority queue (heap), hard-constraint filters HC1 to HC7, soft-preference ranking SP1 to SP3, the deciding-rule explanation, hold reasons, the scheduler pass. Also project lead: portal split, integration, tests. |
| **Komal Suryavanshi** | **E2: Persistence engine** (`e2_persistence/`) | SQLite schema and migrations, the Repository, the atomic assignment commit with re-checks (HC3), job and GPU lifecycle, workspace files (create, rename, delete, folders), terminal queues, consistency checks. |
| **Omkar Naik** | **E3: Runtime engine** (`e3_runtime/`) | The agent on the GPU machine (Docker sandbox launch, live logs, stop and timeout, terminal relay), heartbeats and watchdog, recovery after restarts, GPU probing with `nvidia-smi`. Provides the RTX 2050 laptop used as the demo GPU. |
| **Bhoomika Kalal** | **E4: Web engine** (`e4_web/`) | Flask app, login and roles, CSRF and input validation, REST routes, the editor, admin and market pages, templates and styling. |

Guided by **Prof. Amey Muchandi**.

## 6. Technical Details

**Stack:** Python 3, Flask and Jinja templates, SQLite (WAL mode), Docker with the NVIDIA container toolkit, `nvidia-smi`, the Monaco editor (loaded from a CDN, with a plain-textarea fallback), `unittest`.

**Processes** (started by one supervisor): the web server, the scheduler loop, the provider monitor (watchdog), and, on each GPU machine, the agent.

**Key mechanisms**

- *Atomic commit:* `BEGIN IMMEDIATE` transaction that re-checks memory, one-job-per-GPU, sandbox, hours, price and trust before writing.
- *Heartbeat:* the agent reports every 2 s; silence beyond 20 s marks the worker offline and re-queues its jobs.
- *Live output:* logs are append-only files read by offset; the terminal uses database queues polled about every 0.3 s by the agent.
- *Safety:* JSON-only APIs, hashed tokens, login rate limiting, server-side role checks.

**Folder layout**

```
e1_scheduling/   heap_queue.py, selection.py, scheduler.py
e2_persistence/  schema.sql, repository.py, db.py, seed.py
e3_runtime/      agent.py, supervisor.py, provider_monitor.py, recovery.py, gpu_probe.py, jobs/
e4_web/          app.py, auth.py, validation.py, templates/, static/
rules.py  config.py  run.py  render.yaml
```

**Run it**

```bash
pip install -r requirements.txt
python run.py                      # KLE Tech portal  -> http://localhost:5000
python run.py --portal exchange    # SpareGPU         -> http://localhost:5100
```

On a GPU machine (Linux with Docker and the NVIDIA container toolkit), register the server in the admin page, copy its token, then:

```bash
python -m e3_runtime.agent --server http://SERVER_ADDRESS:5000 --token YOUR_TOKEN
```

**Tests:** about 98 automated tests cover each engine, the portals, and 12 end-to-end scenarios (concurrent submissions, contention for one GPU, queueing, assignment when a GPU frees, no-GPU runs, worker disconnect, cancel and timeout, admin GPU changes, editor file operations, successful runs, access control, and database failure with recovery).

```bash
python -m unittest discover -s . -t . -p "test_*.py"
```

**Deployment notes:** the app is Render-ready (`render.yaml`, `PORT`, proxy and cookie settings from environment variables). Free hosting wipes local disk on restart, so a hosted database (planned: Supabase Postgres) is the permanent-storage fix.

## 7. Conclusion

The project shows that a GPU lab can be run by explicit rules instead of ad-hoc requests: no job lands on a GPU that cannot hold it, no GPU runs two jobs, every decision can be explained, and a crash never leaves half-finished state. User code runs on the allocated GPU in a sandbox, and the portal reports the true state, including when nothing can run. The same engine powers both the college lab and the SpareGPU marketplace. Planned next steps are verified end-to-end runs on the RTX 2050 under Ubuntu and Docker, and moving storage to Supabase Postgres.