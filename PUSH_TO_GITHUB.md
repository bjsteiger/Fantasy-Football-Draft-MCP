# Pushing this to your GitHub

The repo is already initialised with one commit, a `.gitignore` and an MIT `LICENSE`.
You only need to point it at your account and push.

## 1. Set your username in the docs

Badges and clone URLs contain an `OWNER` placeholder:

```bash
./scripts_set_owner.sh your-github-username
```

## 2. Set your identity on the commit

The commit was authored with a placeholder. Fix it before pushing:

```bash
cd ff-draft-mcp
git config user.name "Your Name"
git config user.email "your@email.com"
git commit --amend --reset-author --no-edit
```

## 3. Create the repo and push

**With the GitHub CLI** (easiest — handles auth and repo creation in one step):

```bash
gh auth login          # only if you haven't already
gh repo create ff-draft-mcp --private --source=. --remote=origin --push
```

Swap `--private` for `--public` if you want it visible.

**Without the CLI:** create an empty repo at https://github.com/new — no README,
no .gitignore, no license, since this repo already has them — then:

```bash
git remote add origin https://github.com/<your-username>/ff-draft-mcp.git
git branch -M main
git push -u origin main
```

Git will prompt for credentials. GitHub no longer accepts account passwords over
HTTPS: use a personal access token as the password
(https://github.com/settings/tokens, scope `repo`), or set up SSH keys and use the
`git@github.com:<your-username>/ff-draft-mcp.git` remote instead.

## 4. Check what you're publishing

```bash
git ls-files
```

Source, docs, tests, examples, CI workflow, issue templates, README, LICENSE,
SECURITY, CONTRIBUTING, CHANGELOG.

`.gitignore` deliberately excludes `*.parquet`, `cache/`, `data/`, `state/` and `.env`.
That keeps ~200 MB of cached nflverse data out of the repo — it rebuilds itself on first
run — and, more importantly, keeps your **ESPN cookies and any league state** out of it.
If you ever add credentials, put them in environment variables, never in a tracked file.

## A note on the data

The repo ships code only, no third-party data. It pulls nflverse, NGS and FantasyPros
consensus at runtime. If you make the repo public, the attribution section in the README
covers those sources — please leave it in place.
