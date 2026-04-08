from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
import hmac
import hashlib
import os
import json
from review import process_review
from dashboard_state import dashboard_state

app = FastAPI(title="PR Review Bot")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET")


def verify_signature(request_body: bytes, signature: str) -> bool:
    """Verify webhook signature from GitHub."""
    if not signature or not WEBHOOK_SECRET:
        return False

    expected = (
        "sha256="
        + hmac.new(WEBHOOK_SECRET.encode(), request_body, hashlib.sha256).hexdigest()
    )

    return hmac.compare_digest(signature, expected)


@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    body = await request.body()

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    event = json.loads(body)
    action = event.get("action")

    # Only process PR open/update events
    if action in ["opened", "reopened", "synchronize"]:
        pr_number = event["pull_request"]["number"]
        repo = event["pull_request"]["base"]["repo"]["full_name"]
        installation_id = event["installation"]["id"]
        run_id = dashboard_state.create_run(
            repo=repo,
            pr_number=pr_number,
            installation_id=installation_id,
            action=action,
        )

        # Run review in background (don't block webhook response)
        background_tasks.add_task(
            process_review,
            repo=repo,
            pr_number=pr_number,
            installation_id=installation_id,
            run_id=run_id,
        )

    # Respond quickly (GitHub expects <30 sec)
    return {"status": "ok"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/api/dashboard")
async def dashboard_api():
    return dashboard_state.snapshot()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>PR Review Bot Dashboard</title>
  <style>
    :root {
      --bg: #f5f7f2;
      --bg-accent: #e8f0de;
      --text: #122117;
      --muted: #4a5c4f;
      --card: #ffffff;
      --border: #d8e1d2;
      --running: #a05e00;
      --done: #1a6f37;
      --failed: #8e1d1d;
      --shadow: 0 8px 24px rgba(18, 33, 23, 0.08);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", Tahoma, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 20% 0%, var(--bg-accent), transparent 45%),
        radial-gradient(circle at 100% 20%, #dff2ea, transparent 35%),
        var(--bg);
      min-height: 100vh;
      padding: 24px;
    }

    .container {
      max-width: 1200px;
      margin: 0 auto;
      display: grid;
      gap: 18px;
    }

    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }

    .title {
      margin: 0;
      font-size: clamp(24px, 4vw, 38px);
      letter-spacing: -0.02em;
    }

    .sub {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 15px;
    }

    .refresh {
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--card);
      padding: 10px 14px;
      color: var(--muted);
      font-size: 14px;
      box-shadow: var(--shadow);
    }

    .stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }

    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px 16px;
      box-shadow: var(--shadow);
      animation: reveal .35s ease;
    }

    .label {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .value {
      margin: 6px 0 0;
      font-size: 30px;
      font-weight: 700;
    }

    .layout {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 16px;
    }

    .runs {
      display: grid;
      gap: 12px;
    }

    .run-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .run-title {
      font-size: 16px;
      margin: 0;
    }

    .run-meta {
      font-size: 13px;
      color: var(--muted);
      margin-top: 4px;
    }

    .badge {
      border-radius: 999px;
      font-size: 12px;
      padding: 5px 10px;
      font-weight: 600;
      border: 1px solid;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .badge.running { color: var(--running); border-color: #efc282; background: #fff4e5; }
    .badge.completed { color: var(--done); border-color: #93d3a8; background: #eefbf2; }
    .badge.failed { color: var(--failed); border-color: #e6a1a1; background: #fff1f1; }

    .progress {
      margin-top: 10px;
      height: 8px;
      background: #eef2ea;
      border-radius: 999px;
      overflow: hidden;
    }

    .progress > div {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #5d8f52, #79b16c);
      transition: width .3s ease;
    }

    .summary {
      margin-top: 10px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }

    .events {
      display: grid;
      gap: 10px;
      max-height: 70vh;
      overflow: auto;
    }

    .event-item {
      border-left: 3px solid #90a998;
      padding: 8px 10px;
      background: #fafdfa;
      border-radius: 10px;
    }

    .event-time {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }

    .empty {
      color: var(--muted);
      padding: 16px;
      text-align: center;
    }

    @keyframes reveal {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @media (max-width: 960px) {
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
    }

    @media (max-width: 520px) {
      body { padding: 14px; }
      .stats { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class=\"container\">
    <header class=\"header\">
      <div>
        <h1 class=\"title\">PR Review Bot Dashboard</h1>
        <p class=\"sub\">Live status of webhook runs, review steps, and posted comments.</p>
      </div>
      <div class=\"refresh\" id=\"refresh\">Refreshing...</div>
    </header>

    <section class=\"stats\">
      <article class=\"card\"><p class=\"label\">Total Runs</p><p class=\"value\" id=\"stat-total\">0</p></article>
      <article class=\"card\"><p class=\"label\">Running</p><p class=\"value\" id=\"stat-running\">0</p></article>
      <article class=\"card\"><p class=\"label\">Completed</p><p class=\"value\" id=\"stat-completed\">0</p></article>
      <article class=\"card\"><p class=\"label\">Failed</p><p class=\"value\" id=\"stat-failed\">0</p></article>
    </section>

    <section class=\"layout\">
      <section class=\"card\">
        <h2 style=\"margin-top: 0;\">Current and Recent Runs</h2>
        <div id=\"runs\" class=\"runs\"></div>
      </section>

      <aside class=\"card\">
        <h2 style=\"margin-top: 0;\">Recent Events</h2>
        <div id=\"events\" class=\"events\"></div>
      </aside>
    </section>
  </div>

  <script>
    const stepOrder = ["queued", "auth", "fetch_files", "llm_review", "fetch_pr", "post_comments"];

    function fmtTime(ts) {
      return new Date(ts * 1000).toLocaleTimeString();
    }

    function badge(status) {
      const normalized = (status || "running").toLowerCase();
      return `<span class=\"badge ${normalized}\">${normalized}</span>`;
    }

    function progress(run) {
      if (run.status === "completed") return 100;
      const idx = Math.max(stepOrder.indexOf(run.current_step), 0);
      return Math.floor((idx / (stepOrder.length - 1)) * 100);
    }

    function renderRuns(runs) {
      const root = document.getElementById("runs");
      if (!runs.length) {
        root.innerHTML = `<div class=\"empty\">No runs yet. Send a pull_request webhook event to start.</div>`;
        return;
      }

      root.innerHTML = runs.map((run) => {
        const p = progress(run);
        const err = run.error ? `<div class=\"run-meta\" style=\"color: var(--failed);\">Error: ${run.error}</div>` : "";
        return `
          <article class=\"card\" style=\"padding: 12px;\">
            <div class=\"run-head\">
              <div>
                <h3 class=\"run-title\">${run.repo} - PR #${run.pr_number}</h3>
                <div class=\"run-meta\">Current step: ${run.current_step} | Started: ${fmtTime(run.started_at)} | Action: ${run.action}</div>
              </div>
              ${badge(run.status)}
            </div>
            <div class=\"progress\"><div style=\"width:${p}%\"></div></div>
            <div class=\"summary\">
              <span>files: ${run.summary.files_total}</span>
              <span>skipped: ${run.summary.files_skipped}</span>
              <span>reviewed: ${run.summary.files_reviewed}</span>
              <span>generated: ${run.summary.comments_generated}</span>
              <span>posted: ${run.summary.comments_posted}</span>
            </div>
            ${err}
          </article>
        `;
      }).join("");
    }

    function renderEvents(events) {
      const root = document.getElementById("events");
      if (!events.length) {
        root.innerHTML = `<div class=\"empty\">No events yet.</div>`;
        return;
      }
      root.innerHTML = events.slice().reverse().map((e) => `
        <div class=\"event-item\">
          <div class=\"event-time\">${fmtTime(e.at)} - ${e.repo || "system"}${e.pr_number ? ` PR #${e.pr_number}` : ""}</div>
          <div>${e.message}</div>
        </div>
      `).join("");
    }

    async function refresh() {
      try {
        const res = await fetch("/api/dashboard", { cache: "no-store" });
        const data = await res.json();

        document.getElementById("stat-total").textContent = data.overview.total;
        document.getElementById("stat-running").textContent = data.overview.running;
        document.getElementById("stat-completed").textContent = data.overview.completed;
        document.getElementById("stat-failed").textContent = data.overview.failed;
        document.getElementById("refresh").textContent = `Last refresh: ${new Date().toLocaleTimeString()}`;

        renderRuns(data.runs || []);
        renderEvents(data.events || []);
      } catch (err) {
        document.getElementById("refresh").textContent = `Refresh failed: ${err}`;
      }
    }

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
    """
