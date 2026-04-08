from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import hmac
import hashlib
import os
import json
import threading
import requests
from e2b import Sandbox
from github_app import GitHubAppAuth
from review import process_review

app = FastAPI(title="PR Review Bot")
WEBHOOK_SECRET = os.environ.get('GITHUB_WEBHOOK_SECRET')

# Semaphore to limit concurrent E2B sandboxes (max 3)
e2b_semaphore = threading.Semaphore(3)

def verify_signature(request_body: bytes, signature: str) -> bool:
    """Verify webhook signature from GitHub."""
    if not signature or not WEBHOOK_SECRET:
        return False

    expected = 'sha256=' + hmac.new(
        WEBHOOK_SECRET.encode(),
        request_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected)

def run_review_in_e2b(repo: str, pr_number: int, installation_id: int):
    """
    Run PR review using E2B sandbox and Anthropic Agent SDK.
    Falls back to process_review if E2B fails.
    """
    print(f"[E2B] Starting review for PR #{pr_number} in {repo}")

    # Acquire semaphore to limit concurrent sandboxes
    acquired = False
    try:
        acquired = e2b_semaphore.acquire(blocking=True, timeout=60)
        if not acquired:
            print("[E2B] Timeout acquiring semaphore, falling back to local review")
            process_review(repo, pr_number, installation_id)
            return
    except Exception as e:
        print(f"[E2B] Semaphore error: {e}, falling back to local review")
        process_review(repo, pr_number, installation_id)
        return

    sandbox = None
    try:
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

        # Fetch PR data to get diff and commit SHA
        pr_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        try:
            pr_response = requests.get(pr_url, headers=headers)
            pr_response.raise_for_status()
            pr_data = pr_response.json()
            commit_sha = pr_data["head"]["sha"]
            branch_name = pr_data["head"]["ref"]
        except Exception as e:
            print(f"[E2B] Failed to fetch PR data: {e}, falling back to local review")
            process_review(repo, pr_number, installation_id)
            return

        # Get PR diff (unified diff format)
        diff_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
        try:
            diff_response = requests.get(diff_url, headers=headers)
            diff_response.raise_for_status()
            files_data = diff_response.json()

            # Build unified diff from files data
            diff_parts = []
            for file in files_data:
                if 'patch' in file:
                    diff_parts.append(f"--- a/{file['filename']}")
                    diff_parts.append(f"+++ b/{file['filename']}")
                    diff_parts.append(file['patch'])
            pr_diff = '\n'.join(diff_parts)
        except Exception as e:
            print(f"[E2B] Failed to fetch PR diff: {e}, falling back to local review")
            process_review(repo, pr_number, installation_id)
            return

        # Create E2B sandbox
        print("[E2B] Creating sandbox...")
        if not os.environ.get("E2B_API_KEY"):
            raise ValueError("E2B_API_KEY not set")

        sandbox = Sandbox(template="claude", timeout=600)
        print("[E2B] Sandbox created")

        # Upload agent.py to sandbox
        with open('/app/agent.py', 'r') as f:
            agent_code = f.read()
        sandbox.files.write('/app/agent.py', agent_code)
        print("[E2B] agent.py uploaded")

        # Write PR diff to a file to avoid env var size limits for large PRs
        sandbox.files.write('/app/pr.diff', pr_diff)
        print("[E2B] PR diff written to /app/pr.diff")

        # Run the agent with env vars passed directly to the command
        print("[E2B] Starting agent process...")
        agent_envs = {
            'REPO': repo,
            'COMMIT_SHA': commit_sha,
            'BRANCH': branch_name,
            'GITHUB_TOKEN': token,
            'ANTHROPIC_API_KEY': os.environ.get("ANTHROPIC_API_KEY", ""),
            'MAX_TOOL_CALLS': os.environ.get("MAX_TOOL_CALLS", "10"),
        }

        stdout_chunks = []
        sandbox.commands.run(
            "python /app/agent.py",
            envs=agent_envs,
            timeout=580,
            on_stdout=lambda data: stdout_chunks.append(data),
            on_stderr=lambda data: print(f"[Agent] {data}", end='', flush=True)
        )
        stdout = ''.join(stdout_chunks)

        print("[E2B] Agent process completed")

        # Parse the JSON output
        try:
            output = json.loads(stdout)
            findings = output.get('findings', [])

            # Cap at 3 findings
            findings = findings[:3]
            print(f"[E2B] Review completed: {len(findings)} findings")

            # Build set of files in this PR for validation of inline comments
            pr_files = {f['filename'] for f in files_data}

            for finding in findings:
                file_path = finding.get('file')
                line = finding.get('line')
                severity = finding.get('severity', 'medium')
                message = finding.get('message', '')

                if not message:
                    continue

                comment_body = f"**[{severity.upper()}]** {message}"

                if file_path and line:
                    # Inline comment — validate file is in the PR diff
                    if file_path not in pr_files:
                        print(f"[E2B] Skipping inline finding: {file_path} not in PR diff")
                        continue
                    comment_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
                    payload = {
                        "body": comment_body,
                        "commit_id": commit_sha,
                        "path": file_path,
                        "line": line
                    }
                    label = f"{file_path}:{line}"
                else:
                    # Overall assessment — post as a general PR comment
                    comment_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
                    payload = {"body": comment_body}
                    label = "overall"

                try:
                    response = requests.post(comment_url, headers=headers, json=payload)
                    response.raise_for_status()
                    print(f"[E2B] Posted comment ({label})")
                except requests.HTTPError as e:
                    if e.response.status_code == 422 and file_path and line:
                        # Line not in diff — fall back to general issue comment
                        print(f"[E2B] Inline comment rejected (line not in diff), posting as issue comment ({label})")
                        fallback_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
                        fallback_payload = {"body": f"**[{severity.upper()}]** `{file_path}` (line {line}): {message}"}
                        try:
                            requests.post(fallback_url, headers=headers, json=fallback_payload).raise_for_status()
                            print(f"[E2B] Posted fallback comment ({label})")
                        except Exception as e2:
                            print(f"[E2B] Failed to post fallback comment: {e2}")
                    else:
                        print(f"[E2B] Failed to post comment ({label}): {e}")

            if not findings:
                comment_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
                payload = {"body": "Review complete: No significant issues found. The changes look good."}
                try:
                    response = requests.post(comment_url, headers=headers, json=payload)
                    response.raise_for_status()
                    print("[E2B] Posted summary comment")
                except Exception as e:
                    print(f"[E2B] Failed to post summary comment: {e}")

        except json.JSONDecodeError as e:
            print(f"[E2B] Failed to parse agent output: {e}")
            print(f"[E2B] Agent output (first 500 chars): {stdout[:500]}")
            print("[E2B] Falling back to local review")
            process_review(repo, pr_number, installation_id)

    except Exception as e:
        print(f"[E2B] Error during review: {e}")
        print("[E2B] Falling back to local review")
        process_review(repo, pr_number, installation_id)

    finally:
        # Ensure sandbox is closed
        if sandbox:
            try:
                sandbox.close()
                print("[E2B] Sandbox closed")
            except Exception as e:
                print(f"[E2B] Error closing sandbox: {e}")

        # Release semaphore
        if acquired:
            e2b_semaphore.release()
        print("[E2B] Review task completed")

@app.post('/webhook')
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    # Verify signature
    signature = request.headers.get('X-Hub-Signature-256')
    body = await request.body()

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    event = json.loads(body)
    action = event.get('action')

    # Only process PR open/update events
    if action in ['opened', 'reopened', 'synchronize']:
        pr_number = event['pull_request']['number']
        repo = event['pull_request']['base']['repo']['full_name']
        installation_id = event['installation']['id']

        # Run review in E2B sandbox in background (don't block webhook response)
        background_tasks.add_task(
            run_review_in_e2b,
            repo=repo,
            pr_number=pr_number,
            installation_id=installation_id
        )

    # Respond quickly (GitHub expects <30 sec)
    return {'status': 'ok'}

@app.get('/health')
async def health_check():
    return {'status': 'healthy'}
