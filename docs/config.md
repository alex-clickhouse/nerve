# Configuration Reference

Nerve assembles configuration from up to three layers, **lowest precedence
first**:

1. `workspace/config/settings.yaml` — shareable, git-tracked settings that live
   inside the workspace (the portable surface you can sync from a remote repo).
2. `config.yaml` — machine-local base settings.
3. `config.local.yaml` — machine-local secrets and personal overrides (gitignored).

Each layer is deep-merged on top of the previous, so a machine can override
shared settings locally (until lockdown mode makes the workspace layer the only
source of truth). The workspace location itself is resolved from `config.yaml`
(or the default `~/nerve-workspace`) *before* `settings.yaml` is read, so a
`workspace:` key inside `settings.yaml` is ignored (it would be circular).

If `workspace/config/settings.yaml` is absent, behavior is exactly as before —
just `config.yaml` + `config.local.yaml`.

`nerve init` splits its answers across the first two layers rather than writing
everything to `config.yaml`. Only things that describe *this box* stay
machine-local:

| Layer | Gets |
|-------|------|
| `config.yaml` | `workspace`, `deployment`, `gateway.host`/`port`, `provider` (incl. the region-scoped Bedrock model IDs), `proxy`, `docker`, `telegram.enabled`, `external_agents` |
| `settings.yaml` | `timezone`, `agent.*`, `memory.*`, `sessions.*`, `sync.*`, `houseofagents.*`, quiet hours, `telegram.dm_policy`/`stream_mode` |

A key is written to exactly one of them. Writing a shared value to both would
make the tracked copy dead weight, since `config.yaml` shadows it.

Re-running `nerve init` regenerates `config.yaml` and `config.local.yaml`
wholesale. `settings.yaml` is git-tracked and may be shared, so it is handled
by ownership instead: the wizard rewrites the keys in the table above from
this run's answers and leaves every other key in the file untouched — a team
policy setting the wizard never emits survives. It prints what it added,
updated and removed. Each of the three files is copied to `*.bak` first if it
holds any setting at all; an empty or comments-only file is skipped, since
there is nothing in it to lose — that keeps a freshly scaffolded
`settings.yaml` from leaving a junk `.bak` in a git-tracked directory on every
install.

Two caveats. `settings.yaml` is rewritten with `yaml.safe_dump`, which cannot
round-trip comments, so a re-init that changes anything drops them (you are
warned, and the previous file is in `settings.yaml.bak`). And because the
wizard owns those keys, changing an answer *does* overwrite what someone else
put there — review the diff before committing.

Unknown keys are ignored but logged as warnings at startup (and shown by
`nerve doctor`) so typos don't fail silently.

Keys typed `path` below expand `~` and any environment variables that are set.
A blank value (`runs_dir:` with nothing after it, or `""`) means *unset*, so the
documented default applies. That includes `gateway.ssl.cert`/`key`: blank means
TLS is off, not TLS with an empty certificate path.

## Environment Variable References

Any string value in **any of the three layers** — `settings.yaml`,
`config.yaml`, `config.local.yaml` — may reference an environment variable, so
secrets can be supplied from the environment (or a secret store) instead of
being written into a file. Interpolation runs once, after all three are
merged:

```yaml
anthropic_api_key: ${ANTHROPIC_API_KEY}         # required — load fails if unset
gateway:
  host: ${BIND_HOST:-127.0.0.1}                 # optional — default when unset/empty
```

- `${VAR}` — **required**. If `VAR` is not set, config loading fails with a
  clear error listing every unresolved variable.
- `${VAR:-default}` — **optional**. Uses `default` when `VAR` is unset *or*
  empty (shell `:-` semantics).
- `$$` — an escaped literal `$` (so `$${X}` yields the literal text `${X}`).

Only the **braced** `${...}` form is interpolated. A bare `$` is never touched,
so bcrypt `password_hash` values (`$2b$...`), jwt secrets, and connection
strings are safe as-is. Interpolation runs after the local overlay is merged,
so `config.local.yaml` may reference env vars too. This is the recommended way
to keep secrets out of any file that will be committed to a shared/remote
workspace repo.

Notes:
- `${VAR}` treats only an **unset** variable as an error; `VAR=""` (set but
  empty) resolves to an empty string. Use `${VAR:-default}` if you want a
  fallback when the value is empty as well.
- Resolved values are always **strings**. `port: ${PORT}` arrives as `"8080"`,
  the same as if you had quoted it in YAML. Fields declared `int`, `float` or
  `bool` — including `int | None` and `list[int]` forms — are converted back
  to their declared type when the config object is built, so `port: ${PORT}`
  and `enabled: ${FEATURE}` behave the same as literal YAML values.
- For booleans the accepted spellings are `true/false`, `1/0`, `yes/no`,
  `on/off`, `y/n`, `t/f` (case-insensitive). **`enabled: ${FLAG}` with
  `FLAG=false` is off** — the string is parsed, not tested for truthiness.
  An empty value (`FLAG=`) and a bare `enabled:` are both **off**, so a
  blanked-out env var reliably disables a feature rather than falling back to
  whatever the tracked config said.
- An *unrecognized* value is logged (with the owning `Class.field`) and the
  field keeps its documented default, so a typo can't flip a flag to the
  opposite of both what it says and what the config declares. Integers are
  parsed with `int()`, so `"1.5"` and `"1e3"` are rejected rather than
  truncated.
- Defaults are **not** re-scanned: `${A:-${B}}` yields the literal `${B}` when
  `A` is unset; nest by using a single reference instead.

## Validating Configuration

`nerve config validate` checks the whole config bundle and exits non-zero on any
error, so it drops straight into CI on the config/workspace repo:

```bash
nerve config validate                 # validate the active install's config
nerve config validate --workspace .   # validate a checked-out config repo
nerve config validate --strict-keys   # also fail on unknown/misspelled keys
nerve config validate --strict-env    # also require every ${ENV_VAR} to be set
```

It fails on an unparseable or invalid cron file, a malformed `run_if` gate spec,
a bad spec for a built-in gate, and backend/codex misconfiguration. It runs even
when the config can't otherwise load (that's the point), so a missing required
secret won't stop it.

It also fails on a **schedule the daemon would not run as written** — both in
`cron/jobs.yaml` / `cron/system.yaml` and in `sync.<source>.schedule` — reporting
every offender in the bundle, by job id:

* a 5-field crontab the scheduler rejects, like `99 * * * *`. At run time the
  daemon refuses to schedule that job and logs it, but only after the change has
  merged and synced, leaving the instance on its old config.
* a schedule that is neither a crontab nor an interval, like `hourly`, `@daily`
  or `every day`. Nothing complains about this one at run time *ever*: it falls
  back to a fixed 2-hour default and the job runs, just not on the cadence
  anybody wrote down. Validation is the only place it can be caught.

Write a 5-field crontab (`*/15 * * * *`) or an interval (`4h`, `30m`, `1h30m`,
`90s`); those are the only two forms the scheduler understands.

It also fails on a **blank path setting**. `workspace`, `cron.jobs_file`,
`cron.system_file` and `cron.gate_plugins_dir` all read as "unset, use the
default" when left empty, but `Path("")` is `Path(".")` — they actually point at
whatever directory the daemon was started in. For `cron.gate_plugins_dir` that
is a code-execution footgun: every `.py` file in that directory is imported and
executed at startup and on every cron reload. Omit the key to get the default;
never set it to `''`, `.` or `./`.

**What it will not check: your gate plugins.** Validation never loads the
`.py` files in `<workspace>/config/cron/gates/` — importing one to check it
would mean the bundle had already run by the time validation decided it was
unfit, which is the one thing a gate on untrusted config cannot do. So a
`run_if` entry naming a gate type that isn't built in is reported as a
**warning**: a plugin may well provide it, but validation can neither confirm
the type exists nor check the spec's fields, because both answers live inside
code it declines to run. A plugin is code; test it the way you test code.

Two checks are deliberately lenient by default, so that validating a live
install doesn't cry wolf:

| Flag | Default | With the flag |
|------|---------|---------------|
| `--strict-keys` | An unknown or misspelled key is a **warning** — a config carrying a key from a newer nerve, or the shipped example, still passes. Covers config keys and the fields inside a built-in gate's `run_if` spec. | Unknown keys are errors. |
| `--strict-env` | An unset `${ENV_VAR}` is **info** — CI has no secrets to hand. | Every reference must resolve. |

**Turn `--strict-keys` on in CI.** A typo'd key is the most common config
mistake and the quietest: nothing loads it, and without the flag the check
exits 0. Pin the nerve version the workflow installs to the one you deploy, so a
key introduced by a newer nerve can't fail the check against an older validator.

One more flag controls *what* gets validated. `--portable-only` ignores this
machine's `config.yaml` and `config.local.yaml` and judges the portable
`<workspace>/config/settings.yaml` layer on its own. Use it when reviewing a
change headed for a shared repo: otherwise a local override can mask an invalid
shared value, and — more often — a broken *local* file fails a shared bundle
that has nothing wrong with it. Pass `--workspace` alongside it: with no machine
config left to read the workspace location from, it falls back to the default
one, and validating the wrong tree is how a CI gate ends up green and useless.
`--portable-only` fails outright if it opened no file at all under the
workspace's `config/` — an empty directory, a `settings.yml` typo or a
`settings.yaml` left at the repo root all mean the gate reviewed nothing. Every
run also names the layers it read, in absolute paths, so you can always see
which tree that was:

```
[info] portable layer: /home/you/config-repo/config/settings.yaml
[info] machine-local layers (config.yaml, config.local.yaml) not read: validating the portable workspace config on its own
```

Without `--portable-only` the second line instead names the machine-local files
that were overlaid, and the directory they came from.

Example GitHub Actions step for a config repo:

```yaml
jobs:
  validate-config:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v6
      # Pin to the nerve version you deploy, so the validator agrees with it.
      - run: uv pip install --system nerve   # or: pip install <your nerve dist>
      - run: nerve config validate --workspace . --portable-only --strict-keys
```

## Git-Backed Workspace Sync

The workspace can be a git repository whose remote (on GitHub) is a shared
**config repo**. Config changes are proposed as PRs, reviewed, and merged there;
the instance pulls the merged result and hot-reloads — no restart, no editing on
the box.

```bash
nerve config sync                 # git pull --ff-only the workspace, then validate
nerve config sync --branch main
nerve config sync --no-validate
nerve config sync --no-strict-env # tolerate ${VAR}s your shell doesn't have
```

Enable periodic pulls in the daemon (opt-in):

```yaml
workspace_sync:
  enabled: true          # off by default
  branch: main           # empty = current tracking branch
  interval_minutes: 5
  validate: true         # validate the pulled bundle before applying
  strict_env: true       # unset required ${VAR} in the bundle blocks the merge
```

When enabled, the daemon syncs on that cadence. Sync is **fetch → validate →
fast-forward merge**: it fetches the remote, validates the *fetched* bundle in a
throwaway git worktree, and only fast-forwards the live working tree if
validation passes. So an invalid bundle **never lands on disk** — nothing for the
file watcher or the next restart to pick up (`POST /api/config/sync` returns 400
and leaves the workspace untouched). On a successful, changed pull it reloads
cron and MCP config so the merged changes take effect immediately. CI
(`nerve config validate`) on the PR is still the first line of defense. The remote
and credentials come from git itself (configure `git remote` / auth in the
workspace as usual).

**Keep the config subtree clean.** Sync refuses to merge while
`<workspace>/config/` has local changes — an edited or deleted tracked file, a
staged change, an untracked file. Validation judges a clean checkout of the
fetched commit, but the merge lands in your working tree, and `--ff-only` only
refuses when the incoming commit touches the same path. Anything else survives
the merge without ever having been checked, so the bundle on disk would not be
the bundle that passed. An untracked `config/cron/gates/*.py` is the case that
matters most: the daemon imports and runs gate plugins, and validation
deliberately never loads them, so it would run unreviewed local code on a box
whose whole point is that it runs only reviewed remote config. Commit, discard or
push local edits; the failure message names the paths. Files matched by
`.gitignore` inside `config/` are reported as warnings, not refusals — they are
config the shared repo can never carry.

Sync validates **more strictly than CI**: an unset required `${VAR}` fails the
sync. CI has no secrets, so it reports those as info; the daemon does have them,
and a bundle with an unresolved required variable is one it will refuse to load
on its next restart — merging it would leave the box in a state that only breaks
later. If a shared change adds a `${VAR}` this particular box legitimately does
not set, relax it with `workspace_sync.strict_env: false` rather than letting
every sync fail. `nerve config sync` runs in your shell, which may not carry the
daemon's environment (systemd `EnvironmentFile`, docker `--env-file`); pass
`--no-strict-env` there for a one-off. Warnings — an unrecognized cron gate type,
an unknown key, a skipped validation — are reported but do not block the merge,
since validation deliberately does not load the bundle's gate plugins and cannot
tell a plugin's gate type from a typo.

`workspace_sync` changes need a **daemon restart**. The sync loop reads the
current config object on every cycle rather than a copy taken at startup, so it
adds no staleness of its own — but nothing refreshes that object while the
process runs, so an edit to `branch`, `interval_minutes`, `validate` or
`strict_env` does not reach a running daemon. Turning `enabled` on will need a
restart in any case: the sync task is only created at startup, and there is
nothing to re-read the flag if it was never started.

## Migrating an Existing Install

Installs from before the workspace-config layout are migrated automatically
(idempotently) on `nerve upgrade` and daemon start; you can also run it by hand:

```bash
nerve migrate --dry-run   # show what would change
nerve migrate             # apply
```

Migration is **non-destructive**:

- `config.yaml` → `workspace/config/settings.yaml` (git-tracked). Secret values
  are moved into machine-local `config.local.yaml` and replaced with `${ENV_VAR}`
  placeholders. Three things are treated as secret: values under secret-looking
  key names (`*api_key*`, `*api_hash*`, `api_id`, `*token*`, `*secret*`,
  `password*`, `jwt`, `authorization`, `bearer`, `oauth`, `*access_key*`,
  `*private_key*`, `dsn`, `pw`, `pat`, `session_string`, `webhook_url`), values
  whose *shape* is a credential whatever the key is called (`sk-…`, `ghp_…`,
  `xox…`, `user:password@host`, `?token=…`, `Bearer …`), and *every* value inside
  an `env` or `headers` block — including inside lists, where MCP `args` and
  `headers` entries live. The machine-local `workspace` path is also kept in
  `config.local.yaml`.
- `~/.nerve/cron/*` → `workspace/config/cron/*` (the whole directory, including
  any `prompts/` referenced by `prompt_file`).
- Originals are renamed to `*.migrated` breadcrumbs, never deleted; an existing
  breadcrumb is never overwritten. The effective configuration is unchanged —
  values are only relocated. `config.local.yaml` and the breadcrumb both carry
  plaintext secrets, so both are written `0600`; `settings.yaml` gets the mode
  an ordinary write would have given it, so a restrictive `umask` is honored.

Two deliberate limits on the shape rules. They only fire when the *whole value*
is the credential, so a category description that mentions `postgres://user:pass@host`
stays put instead of becoming a `${VAR}` nobody can resolve. And a public
identifier is not a secret: `client_id` is left alone, `client_secret` is not.

If one item in a list is a secret, the **whole list** moves to
`config.local.yaml` — a merge replaces a list rather than combining it
element-wise, so there is no way to override one entry. Migration says so when
it happens; the copy left in `settings.yaml` no longer has any effect.

Secret detection is best-effort — **always review `workspace/config/settings.yaml`
before committing it to a shared repo** — then run `nerve config validate` to
confirm the bundle is well-formed. Migration also reports any value it left in
the tracked file that still looks like a credential (a long opaque string under
a key it has no opinion about); those are yours to judge.

Migration only runs on a pre-refactor `config.yaml` — one holding shareable
settings, not just this box's. A `config.yaml` written by `nerve init` under the
current layout contains only machine-local keys (workspace, bind address,
provider handles), so it is left exactly where it is even if the workspace has
no `settings.yaml` at all. Migration is likewise a no-op once `settings.yaml`
carries real keys: the `nerve init` scaffold is all comments and counts as
empty, but anything more does not. If a `config.yaml` with shareable keys is
still sitting next to a populated `settings.yaml`, migration says so — it is
overriding the tracked file — and you move the keys across by hand.

## Config Directory Resolution

`nerve` commands locate the config directory via a waterfall, so they work
from any working directory:

1. `--config-dir` / `-c` flag
2. `NERVE_CONFIG_DIR` environment variable
3. The current directory, if it contains `config.yaml` or `config.local.yaml`
4. The pointer file `~/.nerve/config_dir` (written by `nerve init` and on
   daemon start)
5. The current directory (fresh-install fallback)

`nerve doctor` reports which directory was used and how it was found.

## Core

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `workspace` | path | `~/nerve-workspace` | Path to workspace directory |
| `timezone` | string | `America/New_York` | Local timezone for scheduling |
| `deployment` | string | `server` | `server` (bare metal) or `docker`. Set during `nerve init`; determines whether CLI commands run directly or proxy to `docker compose`. |

> **Note:** The _mode_ (personal vs worker) is not a config field — it's determined at `nerve init` time and expressed through which workspace templates, cron jobs, and memory categories are active. There's no `mode` key in config.

## Agent

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `agent.model` | string | `claude-opus-4-8` | Primary model for conversations |
| `agent.cron_model` | string | `claude-sonnet-4-6` | Model for cron jobs (cheaper) |
| `agent.max_turns` | int | `50` | Max agentic turns per request |
| `agent.max_concurrent` | int | `32` | Max concurrent agent sessions |
| `agent.cache_ttl` | string | `"5m"` | Prompt-cache write TTL policy: `5m` (status quo), `1h` (always request the 1-hour TTL), or `auto` (per session at client-build time: sparse-cadence sessions — persistent crons, wakeup loops, spaced chats — get `1h`; dense sessions stay on `5m`). Per-cron-job override via `cache_ttl` in jobs.yaml. See `nerve/agent/cache_policy.py` |
| `agent.cache_ttl_excluded_models` | list | `[]` | Model-name substrings that never request the 1h TTL |
| `agent.prompt_rewrite.enabled` | bool | `true` | Offer the first-prompt rewrite feature in the web UI (per-user toggle lives in the composer) |
| `agent.prompt_rewrite.model` | string | `""` | Model for prompt rewriting (empty = `agent.model`, the chat model) |
| `agent.prompt_rewrite.max_tokens` | int | `1024` | Max tokens for the rewritten prompt |
| `agent.prompt_rewrite.timeout_seconds` | float | `45.0` | Rewrite API call timeout |

**Prompt rewrite:** when the ✨ toggle in the composer is on, the first prompt of a new chat is rewritten by a fast model to better express intent. The result is previewed (editable) and only sent after explicit approval — the user can always send the original instead. Trivial or already-clear prompts are sent unchanged without a preview.

**Note:** The engine uses a `can_use_tool` callback (not `bypassPermissions`) so that interactive tools (`AskUserQuestion`, `ExitPlanMode`, `EnterPlanMode`) can pause mid-turn for user input. All other tools are auto-approved. See [sdk-sessions.md](sdk-sessions.md#permissions--interactive-tools) for details.

## Agent Backends (claude / codex)

Nerve can run sessions on two agent runtimes. The backend is selected per
NEW session and is **sticky**: it's stamped into `sessions.backend` at first
client build and always wins over config afterwards, so flipping the
defaults never crosses an existing conversation (or its wakeups) onto a
runtime that can't resume it. See `docs/plans/codex-backend.md`.

```yaml
agent:
  backend: claude          # claude | codex — new interactive sessions
  cron_backend: null       # null → backend; new cron/hook sessions only

codex:                     # active when a codex backend is selected
  bin_path: codex          # tested: >= 0.144.1 and < 0.145.0
  min_version: 0.144.1
  max_version: 0.145.0
  home_dir: ~/.nerve/codex # isolated CODEX_HOME (auth, config, sessions)
  model: gpt-5.6-sol
  cron_model: null         # null → model
  auth: chatgpt            # chatgpt | api_key
  api_key: null            # config.local.yaml; or api_key_env: OPENAI_API_KEY
  sandbox: danger-full-access   # read-only | workspace-write | danger-full-access
  approval_policy: never        # never | on-request | untrusted
  web_search: true
  tool_timeout_sec: 3600        # nerve MCP calls may block on ask_user
  turn_idle_timeout_seconds: null  # null → agent.cli_idle_timeout_seconds
  pricing:                      # $/1M tokens — cost is None for unlisted models
    gpt-5.6-sol: {input: 5.0, cached_input: 0.5, output: 30.0}
  extra_config: {}              # arbitrary codex -c key=value passthrough
  ultracode:                    # optional managed third-party orchestrator
    enabled: false
    auto_install: true
    repository: https://github.com/just-every/plugin-ultracode.git
    revision: 9dde0086e983413016bf62ab96ba6bb17b599fae
    version: 0.3.0+codex.20260601143116
    dashboard: false            # authenticated read-only Nerve UI
    ui: false                   # detached upstream server; keep disabled
    default_transport: exec
    max_concurrency: 2          # hard cap, even if a workflow asks for more
    default_token_budget: 250000 # default and maximum per workflow
    max_agents: 8               # lifetime worker cap per workflow
```

`codex.ultracode.dashboard` exposes run journals in Nerve's authenticated UI.
It does not start Ultracode's detached dashboard process. Keep
`codex.ultracode.ui: false`: the upstream process serves unauthenticated
mutation and execution endpoints and is not safe to expose through Nerve.

Setup for `auth: chatgpt`: run `CODEX_HOME=~/.nerve/codex codex login` once,
then `nerve codex doctor` to verify CLI version, authentication, the live
model list, protocol, and managed plugin state before flipping any backend
default. Codex sessions reach Nerve tools through a dedicated plaintext ASGI
listener bound to an ephemeral `127.0.0.1` port. Its bearer token is scoped to
the owning session, expires after eight hours, and exists only in the spawned
process environment. `mcp_endpoint.enabled` must stay on; the public gateway
mount and the loopback listener share the same authenticated MCP manager.

External/user-launched Codex uses `bearer_token_env_var = "NERVE_MCP_TOKEN"`
instead of storing a credential in TOML. Refresh it with:

```bash
export NERVE_MCP_TOKEN="$(nerve codex token)"
```

Billing follows the effective account reported by `account/read` (and preflight
flags a mismatch with `codex.auth`). With ChatGPT authentication, token counts and rate/credit events are retained,
but `cost_usd` is null: any API-price calculation is stored separately as an
`api_equivalent_estimate`. API-key sessions use `cost_basis: api_billed` when a
known price is available.

Ultracode is installed only into the isolated Nerve Codex home at the exact
configured revision. A hash-verified Nerve policy overlay hard-enforces the
configured concurrency, token, lifetime-agent, and dashboard caps even when a
workflow requests looser values. Autonomous marketplace updates and its
dashboard are off by default. Workers inherit stable MCP definitions through a Nerve wrapper,
exchange the parent credential for two-hour worker-scoped tokens, report usage
back into the parent turn, and run read-only unless a workflow explicitly asks
for a writable sandbox. `GET /api/codex/status` exposes preflight state and
non-terminal journals available for recovery.

Notes: prompt-cache TTL policy, Claude Code plugins, and Langfuse tracing are
claude-only. PDF attachments are surfaced to Codex as explicit path/context
notes rather than silently dropped. With the
default `approval_policy: never` + full-access sandbox, codex sessions
behave like claude's auto-approved tools; tightening the policy surfaces
Approve/Decline cards in the web UI.

## Gateway

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `gateway.host` | string | `0.0.0.0` | Bind address |
| `gateway.port` | int | `8900` | Port number |
| `gateway.ssl.cert` | path | - | SSL certificate path |
| `gateway.ssl.key` | path | - | SSL private key path |

## Telegram

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `telegram.enabled` | bool | `true` | Enable Telegram bot |
| `telegram.bot_token` | string | - | Bot token from @BotFather |
| `telegram.dm_policy` | string | `pairing` | `pairing` (allowlist + one-time pairing codes) or `open` (anyone — dangerous) |
| `telegram.allowed_users` | list[int] | `[]` | Telegram user IDs allowed to DM the bot |
| `telegram.stream_mode` | string | `partial` | `partial` (edit msgs) or `full` |

### Pairing

With `dm_policy: pairing` (the default), the bot only talks to users in
`allowed_users` and rejects everyone else. To authorize a user without
editing config files:

1. Run `nerve pair` on the server — it prints a one-time 6-digit code
   (valid 1 hour). On a fresh install with no `allowed_users`, a code is
   also generated automatically at startup and printed to the log.
2. Send the bot `/pair <code>` from the Telegram account to authorize.
3. The user ID is appended to `telegram.allowed_users` in
   `config.local.yaml` and takes effect immediately.

An unauthorized `/start` gets a reply with the sender's numeric ID and
pairing instructions (rate-limited); all other messages from unauthorized
users are ignored.

## Quiet Hours

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `quiet_start` | string | `02:00` | HH:MM — start of quiet period (local timezone) |
| `quiet_end` | string | `08:00` | HH:MM — end of quiet period (local timezone) |

## Sources (sync)

Sources pull data from external services on a schedule. See [sources.md](sources.md) for full details.

**Common fields** (available on all sources):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sync.<source>.enabled` | bool | `true` | Enable/disable this source |
| `sync.<source>.schedule` | cron/interval | varies | Fetch frequency (crontab or interval like `2h`) |
| `sync.<source>.processor` | string | `agent` | `agent` (LLM review), `memorize` (direct memU), `notify` (channel forward), `none` |
| `sync.<source>.batch_size` | int | `50` | Max records per fetch cycle |
| `sync.<source>.prompt_hint` | string | `""` | Extra instructions for the agent prompt |
| `sync.<source>.model` | string | `""` | Override model (empty = `agent.cron_model`) |
| `sync.<source>.condense` | bool | `false` | LLM-condense long records via `memory.fast_model` before processing |

**Telegram-specific:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sync.telegram.api_id` | int | - | Telethon API ID (from my.telegram.org) |
| `sync.telegram.api_hash` | string | - | Telethon API hash |
| `sync.telegram.schedule` | cron | `*/5 * * * *` | Fetch frequency |
| `sync.telegram.exclude_chats` | list[int] | `[]` | Chat IDs to skip |
| `sync.telegram.monitored_folders` | list | `[]` | Telegram folder names to filter |

**Gmail-specific:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sync.gmail.accounts` | list | `[]` | Gmail accounts to sync |
| `sync.gmail.schedule` | cron | `*/15 * * * *` | Fetch frequency |
| `sync.gmail.keyring_password` | string | - | gog keyring password |

**GitHub-specific:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sync.github.schedule` | cron | `*/15 * * * *` | Fetch frequency |

## Memory (memU)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `memory.recall_model` | string | `claude-sonnet-4-6` | Model for recall routing |
| `memory.memorize_model` | string | `claude-sonnet-4-6` | Model for extraction & preprocessing |
| `memory.fast_model` | string | `claude-haiku-4-5-20251001` | Model for categorization, date resolution, knowledge filtering |
| `memory.embed_model` | string | *(empty)* | Embedding model (only used when `openai_api_key` is set, e.g. `text-embedding-3-small`) |
| `memory.semantic_dedup_threshold` | float | `0.85` | Cosine similarity threshold for semantic deduplication (0 to disable) |
| `memory.knowledge_filter` | bool | `false` | Post-extraction LLM filter that deletes generic knowledge items (extra Haiku API call per memorize) |
| `memory.categories` | list | `[]` | Seed categories — each entry has `name` and `description` fields. Used for semantic routing when memorizing and recalling facts. `nerve init` populates mode-appropriate defaults (personal: relationships, finances, health, etc.; worker: patterns, procedures, approvals, etc.). |

## xmemory (optional, alongside memU)

[xmemory.ai](https://xmemory.ai) is an optional schema-backed memory layer that runs **alongside** memU — it never replaces it. Activated only when both `xmemory.api_key` and `xmemory.instance_id` are set (put them in `config.local.yaml`); otherwise it is completely inert (no SDK calls, zero overhead). The instance and its schema are created out of band on xmemory's side.

When active:
- The `memorize` tool **dual-writes**: memU (as always) plus an async `write_async` to xmemory. Failures on the xmemory side never fail the tool.
- `memory_recall` appends xmemory's read result (serialized as JSON) to memU's N items, run concurrently so the dual lookup is one round-trip. Read behavior is controlled via `xmemory.read_mode` (defaults to `single-answer`).
- The memorization **sweep** (session-close / cron) stays memU-only — it does not go through the `memorize` tool handler.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `xmemory.api_key` | string | *(empty)* | xmemory bearer token (invite-only). Secret → `config.local.yaml`. |
| `xmemory.instance_id` | string | *(empty)* | The xmemory instance to bind. Both this and `api_key` are required to activate. |
| `xmemory.api_url` | string | `https://api.xmemory.ai` | API base URL. |
| `xmemory.extraction_logic` | string | `deep` | Write extraction mode: `deep` (accurate) or `fast` (high-volume). |
| `xmemory.read_mode` | string | `single-answer` | Read mode for recall, whose result is appended as JSON: `single-answer` (synthesized answer envelope), `raw-tables` (table columns + rows), or `xresponse` (objects + relations). |
| `xmemory.timeout` | float | `60.0` | Per-request timeout in seconds. |

## Docker

Configuration for Docker deployment. Only relevant when `deployment: docker`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `docker.extra_mounts` | list[string] | `[]` | Additional host:container mount pairs to add to `docker-compose.yml`. Example: `["~/code:/code", "~/projects:/projects"]` |

The core Docker mounts (source code, `~/.nerve`, workspace) are always included. GitHub CLI (`~/.config/gh`) and Gmail CLI (`~/.config/gog`) auth directories are mounted automatically if they exist on the host.

## MCP Servers

External MCP servers can be added via config without code changes or restarts. The agent picks up new servers on the next session creation, or immediately via the "Reload" button in the UI / `mcp_reload` tool.

Config uses a **dict format** so `_deep_merge` correctly overlays secrets from `config.local.yaml`:

```yaml
# config.yaml — server definitions
mcp_servers:
  filesystem:
    type: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

  remote-api:
    type: http
    url: https://mcp.example.com/v1
```

```yaml
# config.local.yaml — secrets merge on top
mcp_servers:
  remote-api:
    headers:
      Authorization: "Bearer sk-secret-token"
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mcp_servers.<name>.type` | string | `stdio` | Transport: `stdio`, `sse`, or `http` |
| `mcp_servers.<name>.enabled` | bool | `true` | Enable/disable this server |
| `mcp_servers.<name>.command` | string | - | Command to run (stdio only) |
| `mcp_servers.<name>.args` | list | `[]` | Command arguments (stdio only) |
| `mcp_servers.<name>.env` | dict | `{}` | Environment variables (stdio only) |
| `mcp_servers.<name>.url` | string | - | Server URL (sse/http only) |
| `mcp_servers.<name>.headers` | dict | `{}` | HTTP headers (sse/http only) |

The built-in `nerve` server (SDK type, in-process) is always present and cannot be overridden.

### Claude Code Plugins

Nerve automatically discovers MCP servers from Claude Code's enabled plugins. Any plugin enabled in `~/.claude/settings.json` is loaded via the SDK's `--plugin-dir` flag, so the CLI handles OAuth, credentials, and plugin lifecycle natively.

- **No config needed** — just enable a plugin in Claude Code and restart Nerve.
- **OAuth works** — the CLI uses cached tokens from `~/.claude/.credentials.json`.
- **Auto-registered in UI** — plugin MCP servers appear in the MCP Servers page on first tool invocation (type: `plugin`).
- **No conflicts** — Nerve-configured MCPs (from `config.yaml`) and Claude Code plugin MCPs coexist; they use separate mechanisms (`--mcp-config` vs `--plugin-dir`).

## Auth

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `auth.password_hash` | string | - | bcrypt hash for login |
| `auth.jwt_secret` | string | - | JWT signing secret |

## API Keys (config.local.yaml)

| Key | Type | Description |
|-----|------|-------------|
| `anthropic_api_key` | string | Anthropic API key (agent + memU chat). Not required when proxy is enabled. |
| `openai_api_key` | string | OpenAI API key (optional — enables vector-based memory search via embeddings; without it, LLM-based recall is used) |
| `brave_search_api_key` | string | Brave Search API key (optional) |

## Proxy (CLIProxyAPI)

Optional local proxy that routes Anthropic API calls through Claude Code's OAuth authentication instead of a direct API key. When enabled, the API key is not required — all API calls go through the proxy at `localhost`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `proxy.enabled` | bool | `false` | Enable CLIProxyAPI proxy |
| `proxy.port` | int | `8317` | Proxy listen port |
| `proxy.host` | string | `127.0.0.1` | Proxy bind address |
| `proxy.binary_path` | path | `~/.nerve/bin/cli-proxy-api` | Path to CLIProxyAPI binary (auto-downloaded if missing) |
| `proxy.auth_dir` | path | `~/.nerve/cli-proxy-auth` | Directory for OAuth token storage |
| `proxy.api_key` | string | `sk-nerve-local-proxy` | Local auth key between Nerve and the proxy |
| `proxy.log_file` | path | `~/.nerve/proxy.log` | Proxy log file |

**Setup:**
```bash
# During nerve init, choose "Claude Code proxy" at the API configuration step.
# Or enable manually:
```

```yaml
# config.yaml
proxy:
  enabled: true
  port: 8317
```

```bash
# Authenticate with Claude (one-time):
~/.nerve/bin/cli-proxy-api --claude-login --no-browser \
  --config ~/.nerve/cli-proxy-config.yaml
```

The proxy binary is automatically downloaded from [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) on first start if not present. OAuth tokens are refreshed automatically.

## Sessions

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sessions.archive_after_days` | int | `30` | Auto-archive idle/stopped sessions older than this |
| `sessions.interactive_archive_after_hours` | int | `0` | Auto-close interactive (web/telegram/…) sessions after this many idle hours (`0` = disabled; opt-in). Cron/persistent sessions are unaffected. |
| `sessions.max_sessions` | int | `500` | Max active (non-archived) sessions before cleanup |
| `sessions.cron_session_mode` | string | `per_run` | `per_run` (unique session per cron run) or `reuse` (shared session per job) |

**Starred sessions are exempt from all auto-archival.** A session starred via
the star toggle (web sidebar, or the Telegram `/sessions` list / `/star`) is
never auto-closed: it is skipped by the idle cutoff and the
`archive_after_days` backstop, and is off-budget for `max_sessions` — neither
counted toward the cap nor evicted. It stays resumable until explicitly
unstarred, archived, or deleted.

## Retention

Opt-in `nerve.db` maintenance. Disabled by default. When enabled, a background
pass every `interval_hours` drops the verbose `blocks`/`thinking` JSON of old,
already-memorized messages (keeping the rendered `content`), prunes append-only
telemetry and file snapshots older than `retention_days`, and checkpoints the
WAL. This frees space inside the database but does not shrink the file on disk;
run `nerve db vacuum` once (with the daemon stopped) to reclaim it.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `retention.enabled` | bool | `false` | Master switch for the background retention pass |
| `retention.retention_full_days` | int | `30` | Compact `blocks`/`thinking` of memorized messages older than this |
| `retention.retention_days` | int | `90` | Prune telemetry and file snapshots older than this |
| `retention.interval_hours` | int | `24` | How often the background pass runs |

Manual commands (run regardless of `enabled`):

- `nerve db prune [--dry-run]` runs one pass immediately. `--dry-run` reports
  what would change without mutating.
- `nerve db vacuum` rewrites the file to reclaim freed pages. It takes a write
  lock, so stop the daemon first.

## Cron

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `cron.system_file` | path | `<workspace>/config/cron/system.yaml` (falls back to `~/.nerve/cron/system.yaml` for un-migrated installs) | System cron jobs (managed by `nerve init`) |
| `cron.jobs_file` | path | `<workspace>/config/cron/jobs.yaml` (falls back to `~/.nerve/cron/jobs.yaml`) | User-defined custom cron jobs |
| `cron.gate_plugins_dir` | path | `<workspace>/config/cron/gates` (falls back to `~/.nerve/cron/gates`) | Drop-in custom gate plugin directory |
| `cron.auto_reload` | bool | `true` | Watch the cron directory and hot-reload jobs on change (no restart) |

## Workflow Runs

Budget-capped multi-agent jobs (Claude harness `Workflow` tool or Codex
Ultracode) in dedicated tracked sessions. Nerve meters real dollar spend from
its own usage accounting, warns at `warn_fraction`, and terminates the run at
100% of budget — the kill is scoped to the run's own session/subprocess. Each
run keeps a journal under `runs_dir` (`<run-id>/{run.json,events.ndjson,result.md}`).
Runs do not survive a daemon restart: a startup recovery pass marks orphaned
active runs `failed` and notifies. See [workflow-runs.md](workflow-runs.md).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `workflows.enabled` | bool | `true` | Master switch — service, MCP tools, and API |
| `workflows.runs_dir` | path | `~/.nerve/workflow-runs` | Root for per-run journal directories |
| `workflows.poll_interval_seconds` | int | `60` | Budget monitor cadence — spend is re-metered (recorded turn costs + live in-flight estimate) every interval (min 5s) |
| `workflows.warn_fraction` | float | `0.8` | Fraction of `budget_usd` at which the one-time warning notification fires |
| `workflows.kill_grace_seconds` | int | `30` | After the graceful stop at 100% budget, how long to wait before force-discarding the session's client (kills its subprocess) |
| `workflows.max_concurrent_runs` | int | `2` | Runs dispatched concurrently; excess queues in status `pending`. Each running workflow occupies one `agent.max_concurrent` slot for its whole turn — keep this well below that limit |
| `workflows.allow_unbudgeted` | bool | `false` | Permit starting runs without `budget_usd`. Budget enforcement is the point of this surface, so off by default |
