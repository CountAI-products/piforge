# Contributing to PiForge

Thanks for considering a contribution. This project is maintained solo, so
the process is kept deliberately lightweight — but everything below is
enforced automatically, not just suggested.

## Dev setup

```bash
git clone https://github.com/gokul-hastrophil/piforge.git
cd piforge
./check-requirements.sh          # tells you what's missing
cp config.example.json config.json
# edit config.json with values you're comfortable testing with
sudo python3 server.py
```

Open `http://127.0.0.1:47823`. This is exactly the same code path the
packaged `.deb` uses — nothing behaves differently between "running from
source" and "installed app" except how it's launched.

## Before opening a PR

There's no formal test suite (this is a small, security-sensitive systems
tool — most of its value is verified by actually driving real hardware,
which CI can't do). What CI *can* and does check on every PR:

- Every `.sh` file parses (`bash -n`)
- Every `.py` file parses (`ast.parse`)
- `index.html`'s embedded `<script>` block parses as valid JS (`node -e`)
- `packaging/debian/piforge.desktop` validates (`desktop-file-validate`)
- CodeQL security analysis on the Python code

Run the same checks locally before pushing — they're fast:

```bash
bash -n *.sh vm-kernel/*.sh packaging/piforge packaging/*.sh
python3 -c "import ast; [ast.parse(open(f).read(), filename=f) for f in ['server.py','firstrun_gen.py','provision.py']]"
node -e "new Function(require('fs').readFileSync('index.html','utf8').match(/<script>([\s\S]*)<\/script>/)[1])"
```

If your change touches actual flashing logic (`server.py`'s `flash_device`,
`firstrun_gen.py`, the cancel/retry paths), please describe in the PR how
you tested it against real hardware or at minimum a loop device — CI
can't drive a card writer, so this is the one place manual verification
still matters most.

## Review process

Every PR gets:
1. **Automated CI** — the syntax/validation checks above, plus CodeQL.
   Must pass before merge (enforced by the branch ruleset on `main`).
2. **Two independent AI reviewers**, deliberately kept separate rather
   than relying on one:
   - **Claude** (`.github/workflows/ai-review.yml`, Anthropic's
     `claude-code-action`) — needs an `ANTHROPIC_API_KEY` repo secret,
     see "Maintainer setup" below.
   - **CodeRabbit** (`.coderabbit.yaml`) — a GitHub App, no key
     management needed. Install once at
     https://github.com/apps/coderabbitai for this repo. Tuned in
     `.coderabbit.yaml` with per-path instructions for the security-
     sensitive files (device detection, `firstrun_gen.py`'s shell
     substitution, the pkexec privilege boundary), plus shellcheck,
     ruff, and markdownlint enabled. Re-reviews automatically on every
     push to the PR. Chat with it inline in PR comments — no `@mention`
     needed (`auto_reply` is on).

   Neither is a substitute for maintainer review, especially for
   anything touching device I/O, privilege handling, or the
   `firstrun.sh` template — those get read carefully by a human before
   merge regardless of what either AI pass says.
3. **Maintainer review** — final pass before merge.

## Style

- No new runtime dependencies beyond what's already in `packaging/build-deb.sh`'s
  `Depends:` line unless discussed first — this project deliberately stays
  apt-installable with zero `pip install`s.
- Match the existing comment style: comments explain *why*, not *what*
  (see `CLAUDE.md`-style guidance embedded in the codebase's own tone if
  you're unsure — short, no restating the obvious).
- Shell scripts: `set -euo pipefail`, quote your variables.
- Python: stdlib only in `server.py`/`firstrun_gen.py`; the native app
  (`packaging/piforge`) is the one place `python3-gi`/WebKit2 are allowed
  since that's an apt dependency, not pip.

## Commit messages

Explain *why*, not just *what* — the diff already shows what changed.
Look at recent `git log` output for the tone this project uses.

## Maintainer setup: enabling the AI review

The AI review workflow (`.github/workflows/ai-review.yml`) needs an
Anthropic API key as a repo secret before it'll run — it skips cleanly
(doesn't fail CI) until this is set:

1. Get an API key from https://console.anthropic.com/ (Anthropic Console
   → API Keys).
2. In the repo: **Settings → Secrets and variables → Actions → New
   repository secret**, name it `ANTHROPIC_API_KEY`, paste the key.
3. That's it — the next PR triggers a review automatically.

This is a maintainer-only step; contributors never need this key.

## Reporting bugs / requesting features

Use the issue templates — they ask for the specific details that speed up
triage for this kind of tool (which distro, which Pi model, real or loop
device, etc).
