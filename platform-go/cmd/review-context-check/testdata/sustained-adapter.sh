#!/bin/sh
set -eu
prompt=/tmp/qiqc-sustained-task-input.txt
cat > "$prompt" <<'QIQC_CONNECTIVITY_FIRST'
This is an organizer infrastructure acceptance session, not a competition baseline.
Before solving the task, optimizing, or writing a solution, call the registered
qsim MCP tool list_devices with empty arguments. Discover the tool if necessary;
tool discovery alone does not confirm connectivity. Wait for its real successful
response, then continue working on the original task below. If the call fails,
investigate the actual reported error and do not claim connectivity succeeded.
Use the registered task tool. Do not call trusted host controls, fabricate tool
responses, or create or modify qsim evidence logs. The organizer enforces the
execution budget; keep doing genuine task work after the connectivity check.

Original task instructions follow unchanged:

QIQC_CONNECTIVITY_FIRST
cat "$QIQC_TASK_INPUT" >> "$prompt"
cat >> "$prompt" <<'QIQC_ACCEPTANCE_NOTE'

This is a bounded infrastructure acceptance session. Work on the task above.
If a candidate solution is already present, inspect it, use the task tools to
check it, and improve or verify it. Preserve useful work from earlier sessions.
Do not wait idly or use sleep to fill time. The organizer will stop execution
at the configured budget. A successful session may be followed by another
session that independently checks the current workspace and solution.
QIQC_ACCEPTANCE_NOTE
while :; do
  codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
    --json --model "$QIQC_MODEL" -C /app - < "$prompt"
done
