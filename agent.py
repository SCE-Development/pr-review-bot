import os
import subprocess
import json
import sys
from anthropic import Anthropic


def run_bash(command: str, timeout: int = 30) -> str:
    """Run a shell command in the cloned repo directory with secrets stripped from env."""
    try:
        # Minimal env — no secrets accessible to shell commands (prevents prompt injection exfiltration)
        safe_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=timeout, cwd='/tmp/repo',
            env=safe_env
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]: {result.stderr}"
        if not output.strip():
            return f"[exit code {result.returncode}, no output]"
        if len(output) > 8000:
            output = output[:8000] + f"\n...[truncated, {len(output)} total chars]"
        return output
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {str(e)}"


TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the repository root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"}
            },
            "required": ["command"]
        }
    }
]

SYSTEM_PROMPT = """You are a senior software engineer reviewing a pull request.

The repository is already cloned and your working directory is the repo root. You have a bash tool with full shell access — use it however you see fit to understand the changes and their impact.

Return at most 3 findings as JSON — no other text. Each finding can be an inline comment (specific file + line) or an overall assessment (no file/line). Use whichever makes more sense for each issue.

{
  "findings": [
    {
      "file": "path/to/file.py",
      "line": 123,
      "severity": "critical" | "high" | "medium" | "low",
      "message": "..."
    },
    {
      "severity": "medium",
      "message": "Overall: ..."
    }
  ]
}

If there are no significant issues, return {"findings": []}.
"""


def run_agent(client: Anthropic, pr_diff: str, max_tool_calls: int) -> str:
    """Run the agentic loop using Anthropic's tool use API."""
    messages = [
        {
            "role": "user",
            "content": f"Please review this pull request:\n\n{pr_diff}"
        }
    ]
    tool_call_count = 0

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, 'text'):
                    return block.text
            return ""

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_call_count += 1
                    print(f"Tool call {tool_call_count}/{max_tool_calls}: {block.name}({json.dumps(block.input)})", file=sys.stderr)
                    result = run_bash(block.input["command"]) if block.name == "bash" else f"Unknown tool: {block.name}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            messages.append({"role": "user", "content": tool_results})

            if tool_call_count >= max_tool_calls:
                print(f"Tool call limit ({max_tool_calls}) reached, requesting final answer.", file=sys.stderr)
                response = client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=messages
                )
                for block in response.content:
                    if hasattr(block, 'text'):
                        return block.text
                return ""
        else:
            break

    return ""


def main():
    repo_name = os.environ.get('REPO')
    commit_sha = os.environ.get('COMMIT_SHA')
    branch_name = os.environ.get('BRANCH', 'main')
    anthropic_api_key = os.environ.get('ANTHROPIC_API_KEY')
    github_token = os.environ.get('GITHUB_TOKEN')
    max_tool_calls = int(os.environ.get('MAX_TOOL_CALLS', '10'))

    if not all([repo_name, commit_sha, anthropic_api_key, github_token]):
        print(json.dumps({"error": "Missing required environment variables", "findings": []}))
        sys.exit(1)

    # Clone the repository at the PR commit
    print("Cloning repository...", file=sys.stderr)
    clone_dir = '/tmp/repo'
    clone_url = f"https://{github_token}@github.com/{repo_name}.git"

    try:
        subprocess.run(['rm', '-rf', clone_dir], check=True)
        subprocess.run(
            ['git', 'clone', '--depth=1', '--branch', branch_name, clone_url, clone_dir],
            check=True, capture_output=True,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        print(f"Cloned {repo_name} branch {branch_name} (shallow)", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(json.dumps({"error": f"Failed to clone: {e.stderr.decode() if e.stderr else str(e)}", "findings": []}))
        sys.exit(1)

    os.chdir(clone_dir)

    # Read PR diff from file written by server.py
    try:
        with open('/app/pr.diff', 'r', encoding='utf-8') as f:
            pr_diff = f.read()
    except Exception as e:
        print(f"Warning: Could not read PR diff ({e})", file=sys.stderr)
        pr_diff = ""

    # Run the agentic review
    print("Running agent...", file=sys.stderr)
    client = Anthropic(api_key=anthropic_api_key)

    try:
        final_response = run_agent(client, pr_diff, max_tool_calls)

        if not final_response:
            raise ValueError("No response from agent")

        # Strip markdown fences if present
        cleaned = final_response.strip()
        if cleaned.startswith('```'):
            lines = cleaned.split('\n')[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            cleaned = '\n'.join(lines).strip()

        output = json.loads(cleaned)
        if not isinstance(output.get('findings'), list):
            output['findings'] = []
        output['findings'] = output['findings'][:3]

        print(json.dumps(output))
        sys.exit(0)

    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON from agent: {str(e)}", "findings": []}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"Agent failed: {str(e)}", "findings": []}))
        sys.exit(1)


if __name__ == '__main__':
    main()
