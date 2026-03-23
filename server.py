from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import hmac
import hashlib
import os
import json
from github_app import GitHubAppAuth
from review import process_review

app = FastAPI(title="PR Review Bot")
WEBHOOK_SECRET = os.environ.get('GITHUB_WEBHOOK_SECRET')

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

        # Run review in background (don't block webhook response)
        background_tasks.add_task(
            process_review,
            repo=repo,
            pr_number=pr_number,
            installation_id=installation_id
        )

    # Respond quickly (GitHub expects <30 sec)
    return {'status': 'ok'}

@app.get('/health')
async def health_check():
    return {'status': 'healthy'}
