import os
import json
import requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# -------------------------
# Config (Auto injected by GitHub Actions)
# -------------------------
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# -------------------------
# Load PR metadata
# -------------------------

with open(EVENT_PATH) as f:
    event = json.load(f)

pr_number = event["pull_request"]["number"]
commit_id = event["pull_request"]["head"]["sha"]

print(f"Reviewing PR #{pr_number}")

# -------------------------
# Get changed files
# -------------------------

files_url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/files"

response = requests.get(files_url, headers=headers)
files = response.json()

print(f"{len(files)} files changed")

# -------------------------
# Review each file
# -------------------------

comments = []

for file in files:

    filename = file["filename"]

    if "patch" not in file:
        continue

    patch = file["patch"]

    print(f"Reviewing {filename}")

    prompt = f"""
You are a senior software engineer reviewing a pull request.

Review the following git diff for bugs, security issues, or bad practices.

File: {filename}

Diff:
{patch}

Return JSON in this format:

{{
  "comments": [
    {{
      "line": <line number>,
      "message": "<review comment>"
    }}
  ]
}}

If there are no issues, return:

{{ "comments": [] }}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "You are a senior software engineer with extreme technical expertise."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )

        result = response.choices[0].message.content

        data = json.loads(result)

        for c in data["comments"]:
            comments.append({
                "path": filename,
                "line": c["line"],
                "body": c["message"]
            })

    except Exception as e:
        print(f"LLM review failed for {filename}: {e}")


# -------------------------
# Post comments to PR
# -------------------------


for comment in comments:
    print(f"Comment on {comment['path']}:{comment['line']}: {comment['body']}")
    continue

    """
    Right now we are just printing the comments to avoid spamming the PR with comments.
    TODO: Post comments to PR
    Use url https://api.github.com/repos/{REPO}/pulls/{pr_number}/comments with payload:
    {
        "body": body,
        "commit_id": commit_id,
        "path": path,
        "line": line
    }
    """

print("Review complete.")