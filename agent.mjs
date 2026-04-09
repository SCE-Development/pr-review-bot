import { generateText, tool } from 'ai';
import { createAnthropic } from '@ai-sdk/anthropic';
import { createOpenAI } from '@ai-sdk/openai';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
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

const SYSTEM_PROMPT = `You are a senior software engineer reviewing a pull request.

The repository is already cloned and your working directory is the repo root. You have a bash tool with full shell access — use it however you see fit to understand the changes and their impact.

Return at most 3 findings as JSON — no other text. Each finding can be an inline comment (specific file + line) or an overall assessment (no file/line).

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

function getModel(modelId, anthropicApiKey, openaiApiKey, googleApiKey) {
  if (modelId.startsWith('gpt-') || modelId.startsWith('o1') || modelId.startsWith('o3')) {
    return createOpenAI({ apiKey: openaiApiKey })(modelId);
  }
  if (modelId.startsWith('gemini-')) {
    return createGoogleGenerativeAI({ apiKey: googleApiKey })(modelId);
  }
  return createAnthropic({ apiKey: anthropicApiKey })(modelId);
}

async function main() {
  const repoName = process.env.REPO;
  const branchName = process.env.BRANCH || 'main';
  const anthropicApiKey = process.env.ANTHROPIC_API_KEY;
  const openaiApiKey = process.env.OPENAI_API_KEY || '';
  const googleApiKey = process.env.GOOGLE_API_KEY || '';
  const githubToken = process.env.GITHUB_TOKEN;
  const maxSteps = parseInt(process.env.MAX_TOOL_CALLS || '10');
  const modelId = process.env.MODEL || 'claude-haiku-4-5-20251001';

  if (!repoName || !anthropicApiKey || !githubToken) {
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

  process.stderr.write('Running agent...\n');

  try {
    const model = getModel(modelId, anthropicApiKey, openaiApiKey, googleApiKey);
    let stepCount = 0;

    const { text } = await generateText({
      model,
      system: SYSTEM_PROMPT,
      prompt: `Please review this pull request:\n\n${prDiff}`,
      maxSteps,
      tools: {
        bash: tool({
          description: 'Run a shell command in the repository root.',
          parameters: z.object({ command: z.string() }),
          execute: async ({ command }) => {
            stepCount++;
            process.stderr.write(`Tool call ${stepCount}/${maxSteps}: bash(${JSON.stringify({ command })})\n`);
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
