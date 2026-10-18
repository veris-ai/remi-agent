# Contributing to Remi Agent

Thanks for helping. Issues and pull requests are welcome from anyone, especially
new voice-stack implementations and fixes to existing ones.

## Before you start

- **The published results are pinned.** The VAmoS Debt Collection results come
  from the tag
  [`vamos-debt-collection-2026-10`](https://github.com/veris-ai/remi-agent/tree/vamos-debt-collection-2026-10).
  Changes on `main` improve the agent for everyone, but they don't change those
  results. A new result needs a new benchmark run.
- **Every vendor runs the same Remi.** The prompt, tools, gates and policy are
  shared. A change to one of them changes the agent on every stack, so it needs
  a clear reason in the pull request.
- **For anything large, open an issue first** so we can agree on the approach
  before you write the code.

## What we're looking for

- **A new voice stack.** A new folder under `vamos_voice/providers/`, following
  the vendor checklist in [AGENTS.md](AGENTS.md#10-adding-a-vendor-checklist).
  Use the vendor's own turn-taking, voice-activity detection and noise handling.
  Don't add per-stack workarounds that change how the agent behaves.
- **Bug fixes** in an adapter, the framework or Remi's tools, with a test that
  fails before the fix.
- **Reliability fixes** (timeouts, retries, reconnects) that let a stack finish
  its calls.
- **Docs** that make the code easier to understand or run.

## Development setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker (Remi's tool
tests start a throwaway Postgres 16).

```bash
uv run --all-extras --no-extra livekit pytest -q
uv run --all-extras --no-extra pipecat --no-extra huggingface --no-extra grok-cascade pytest -q
uv run ruff check . && uv run ruff format --check .
```

The two test commands exist because LiveKit pins a different `openai` major
version from the other vendors. See [AGENTS.md](AGENTS.md#16-testing) for the
full testing guide.

## Pull requests

1. Fork the repo and branch from `main`. Name the branch `type/short-slug`, for
   example `feat/acme-voice` or `fix/vapi-reconnect`.
2. Keep each pull request to one change. Small pull requests are reviewed faster.
3. Add or update tests, and make sure the commands above pass.
4. In the description, say what changed, why, and how you tested it. For a new
   vendor, include the model it runs and a short sample call.
5. Never commit API keys or recordings of real people. Vendor keys come from
   the environment (see [AGENTS.md](AGENTS.md#11-config-contract-environment)).

Pull requests are squash-merged by the maintainer. Review comments must be
resolved before a merge.

## Reporting bugs and security issues

- **Bugs:** open an issue with the vendor, what you ran, what you expected and
  what happened.
- **Security issues:** don't open a public issue. Report them privately through
  [GitHub security advisories](https://github.com/veris-ai/remi-agent/security/advisories/new).

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
