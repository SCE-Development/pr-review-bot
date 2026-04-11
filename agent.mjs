import { generateText, tool, stepCountIs } from 'ai';
import { anthropic } from '@ai-sdk/anthropic';
import { openai } from '@ai-sdk/openai';
import { google } from '@ai-sdk/google';
import { z } from 'zod';
import { spawnSync } from 'child_process';
import { readFileSync } from 'fs';

function runBash(command, timeout = 30000) {
  const safeEnv = {
    PATH: process.env.PATH || '/usr/bin:/bin:/usr/local/bin',
    HOME: process.env.HOME || '/root',
    LANG: process.env.LANG || 'en_US.UTF-8',
  };

  const result = spawnSync('sh', ['-c', command], {
    cwd: '/tmp/repo',
    env: safeEnv,
    timeout,
    encoding: 'utf8',
  });

  if (result.error?.code === 'ETIMEDOUT') return `ERROR: Command timed out after ${timeout / 1000}s`;
  if (result.error) return `ERROR: ${result.error.message}`;

  let output = result.stdout || '';
  if (result.stderr) output += `\n[stderr]: ${result.stderr}`;
  if (!output.trim()) return `[exit code ${result.status}, no output]`;
  if (output.length > 8000) output = output.slice(0, 8000) + `\n...[truncated, ${output.length} total chars]`;
  return output;
}

const SYSTEM_PROMPT = `You are a senior software engineer doing a thorough review of a pull request.

The repository is already cloned at the repo root. You have full bash access — use it liberally, there is no cost to running many commands.

You MUST do all of the following before forming any conclusions:
1. Read each changed file in full, not just the diff
2. Find every caller and usage of any modified function, class, or symbol across the entire repo
3. Read related files — tests, configs, dependent modules, anything that could be affected
4. Check for edge cases: error handling, concurrency, security, null/undefined, type mismatches
5. Run any additional commands needed to fully understand the impact

Use as many bash calls as you need. Do not cut corners.

Only after thorough exploration, return at most 3 findings as JSON — no other text. Focus on real bugs, security issues, or broken logic. Skip style nits.

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

If there are no significant issues, return {"findings": []}.`;

function getModel(modelId) {
  if (modelId.startsWith('gpt-') || modelId.startsWith('o1') || modelId.startsWith('o3') || modelId.startsWith('o4')) {
    return openai(modelId);
  }
  if (modelId.startsWith('gemini-')) {
    return google(modelId);
  }
  return anthropic(modelId);
}

async function main() {
  const repoName = process.env.REPO;
  const branchName = process.env.BRANCH || 'main';
  const githubToken = process.env.GITHUB_TOKEN;
  const maxSteps = parseInt(process.env.MAX_TOOL_CALLS || '10');
  const modelId = process.env.MODEL || 'gemini-2.5-flash';

  if (!repoName || !githubToken) {
    console.log(JSON.stringify({ error: 'Missing required environment variables', findings: [] }));
    process.exit(0);
  }

  // Clone the repository
  process.stderr.write('Cloning repository...\n');
  const cloneDir = '/tmp/repo';
  const cloneUrl = `https://${githubToken}@github.com/${repoName}.git`;

  spawnSync('rm', ['-rf', cloneDir]);
  const cloneResult = spawnSync(
    'git',
    ['clone', '--depth=1', '--branch', branchName, cloneUrl, cloneDir],
    { encoding: 'utf8', env: { ...process.env, GIT_TERMINAL_PROMPT: '0' } }
  );

  if (cloneResult.status !== 0) {
    console.log(JSON.stringify({ error: `Failed to clone: ${cloneResult.stderr || 'unknown error'}`, findings: [] }));
    process.exit(0);
  }
  process.stderr.write(`Cloned ${repoName} branch ${branchName} (shallow)\n`);

  // Strip token from git config to prevent exfiltration via bash tool
  spawnSync('git', ['remote', 'set-url', 'origin', `https://github.com/${repoName}.git`], {
    cwd: cloneDir, encoding: 'utf8',
  });

  // Read PR diff
  let prDiff = '';
  try {
    prDiff = readFileSync('/app/pr.diff', 'utf8');
  } catch (e) {
    process.stderr.write(`Warning: Could not read PR diff (${e.message})\n`);
  }

  process.stderr.write(`Running agent with ${modelId}...\n`);

  try {
    let stepCount = 0;

    const { text } = await generateText({
      model: getModel(modelId),
      system: SYSTEM_PROMPT,
      prompt: `Please review this pull request:\n\n${prDiff}`,
      stopWhen: stepCountIs(maxSteps),
      tools: {
        bash: tool({
          description: 'Run a shell command in the repository root.',
          inputSchema: z.object({ command: z.string() }),
          execute: async ({ command }) => {
            stepCount++;
            process.stderr.write(`Tool call ${stepCount}: bash(${JSON.stringify({ command })})\n`);
            return runBash(command);
          },
        }),
      },
    });

    // Strip markdown fences if present
    let cleaned = text.trim();
    if (cleaned.startsWith('```')) {
      const lines = cleaned.split('\n').slice(1);
      if (lines.at(-1)?.trim() === '```') lines.pop();
      cleaned = lines.join('\n').trim();
    }

    const output = JSON.parse(cleaned);
    if (!Array.isArray(output.findings)) output.findings = [];
    output.findings = output.findings.slice(0, 3);

    console.log(JSON.stringify(output));
    process.exit(0);
  } catch (e) {
    console.log(JSON.stringify({ error: `Agent failed: ${e.message}`, findings: [] }));
    process.exit(0);
  }
}

main();
