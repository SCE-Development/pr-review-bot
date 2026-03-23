import os
import json
import time
import requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# -------------------------
# Configuration
# -------------------------

# File extensions to skip (binary files, generated code, etc.)
SKIP_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.webp',
    '.pdf', '.zip', '.tar', '.gz', '.tgz', '.rar',
    '.exe', '.dll', '.so', '.dylib', '.bin', '.obj',
    '.pyc', '.pyo', '.pyd', '.class', '.jar', '.war', '.ear',
    '.o', '.a', '.lib', '.dll', '.so', '.dylib',
    '.mo', '.pot', '.db', '.sqlite', '.sqlite3',
    '.woff', '.woff2', '.ttf', '.eot',
}

# Directory patterns to skip
SKIP_DIRS = {
    'node_modules', 'dist', 'build', '.git', 'vendor', 'Pods',
    '.gradle', 'target', '__pycache__', '.venv', 'venv', 'env',
    'bin', 'obj', '.idea', '.vscode', '.cache',
}

# Max number of comments to post (safety limit)
MAX_COMMENTS_PER_PR = 50

# GitHub API rate limit retry settings
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds (will be multiplied: 2, 4, 8)

# -------------------------
# Helper Functions
# -------------------------

def should_skip_file(filename):
    """Check if a file should be skipped based on extension or directory."""
    # Check extension
    ext = os.path.splitext(filename)[1].lower()
    if ext in SKIP_EXTENSIONS:
        return True

    # Check if any skip directory is in the path
    path_parts = filename.split('/')
    for part in path_parts:
        if part in SKIP_DIRS:
            return True

    # Skip common generated/minified patterns
    basename = os.path.basename(filename).lower()
    if any(pattern in basename for pattern in ['min.', '.min.', 'bundle.']):
        return True

    return False

def make_github_request(url, headers, params=None):
    """Make a request to GitHub API with rate limit handling and retry."""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, headers=headers, params=params)

            if response.status_code == 200:
                return response

            # Handle rate limiting
            if response.status_code in (403, 429):
                remaining = response.headers.get('X-RateLimit-Remaining', '1')
                reset_time = response.headers.get('X-RateLimit-Reset')

                if remaining == '0' or response.status_code == 429:
                    wait_time = RETRY_DELAY * (2 ** attempt)  # Exponential backoff
                    if reset_time:
                        # GitHub provides Unix timestamp when rate limit resets
                        reset_epoch = int(reset_time)
                        current_time = int(time.time())
                        wait_time = max(reset_epoch - current_time + 1, wait_time)

                    print(f"Rate limit hit (attempt {attempt + 1}/{MAX_RETRIES}). Waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue

            # Other errors
            response.raise_for_status()

        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait_time = RETRY_DELAY * (2 ** attempt)
            print(f"Request failed: {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)

    raise Exception(f"Failed after {MAX_RETRIES} retries")

# -------------------------
# Main Review Function
# -------------------------

def process_review(repo: str, pr_number: int, installation_id: int):
    """
    Process a PR review using GitHub App authentication.

    Args:
        repo: Repository name (e.g., "owner/repo")
        pr_number: Pull request number
        installation_id: GitHub App installation ID
    """
    from github_app import GitHubAppAuth

    print(f"Reviewing PR #{pr_number} in {repo}")

    # Get GitHub App auth token
    auth = GitHubAppAuth(
        app_id=os.environ["GITHUB_APP_ID"],
        private_key_path=os.environ["GITHUB_PRIVATE_KEY_PATH"],
        installation_id=installation_id
    )
    token = auth.get_installation_token()

    # Setup headers with installation token
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json"
    }

    # -------------------------
    # Get changed files
    # -------------------------

    files_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"

    try:
        response = make_github_request(files_url, headers)
        files = response.json()
    except Exception as e:
        print(f"Failed to fetch files from GitHub: {e}")
        return

    # Filter files before review
    filtered_files = []
    skipped_count = 0
    for file in files:
        filename = file["filename"]
        if should_skip_file(filename):
            skipped_count += 1
            print(f"Skipping {filename} (filtered)")
            continue
        filtered_files.append(file)

    total_files = len(filtered_files)
    print(f"{total_files} files to review (skipped {skipped_count})")

    # -------------------------
    # Review each file
    # -------------------------

    comments = []
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    for file in filtered_files:
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
    # Get commit SHA
    # -------------------------

    pr_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    try:
        pr_data = make_github_request(pr_url, headers).json()
        commit_id = pr_data["head"]["sha"]
    except Exception as e:
        print(f"Failed to fetch PR data: {e}")
        return

    # -------------------------
    # Post comments to PR
    # -------------------------

    for comment in comments:
        print(f"Comment on {comment['path']}:{comment['line']}: {comment['body']}")

        comment_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
        payload = {
            "body": comment["body"],
            "commit_id": commit_id,
            "path": comment["path"],
            "line": comment["line"]
        }

        try:
            response = requests.post(comment_url, headers=headers, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"Failed to post comment: {e}")

    print(f"Review complete: {len(comments)} comments posted.")
