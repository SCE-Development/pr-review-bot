# PR Review Bot

LLM-powered PR reviewer that analyzes diffs for bugs, security issues, and bad practices.

## Setup

```bash
pip install -r requirements.txt
```

Create `.env`:

```
GITHUB_TOKEN=ghp_xxx
OPENAI_API_KEY=sk-xxx
```

- `GITHUB_TOKEN`: Use `gh auth login` and the script will use `gh auth token`.
- `OPENAI_API_KEY`: OpenAI API key.

## Local testing

Run against any existing PR (prints comments to stdout; no comments posted):

```bash
./test_review.sh owner/repo pr_number
```

Example:

```bash
./test_review.sh SCE-Development/sce-tv 70
```
