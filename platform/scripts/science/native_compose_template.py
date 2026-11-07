"""Create the submitted native Codex archive; never launches Codex on this host."""
import io
import json
import zipfile

ADAPTER = '''#!/bin/sh
set -eu
# Original image supplies Codex 0.144.4. Native MCP config is provided by Harbor.
exec codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \\
  --json --model "$QIQC_MODEL" -C /app - < "$QIQC_TASK_INPUT"
'''

# Explicit infrastructure test submission: keep doing real task work after a
# successful CLI session. The trusted host, not this untrusted script, enforces
# the 300-second budget. A failed CLI exits immediately under `set -e`.
SUSTAINED_ADAPTER = '''#!/bin/sh
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
  codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \\
    --json --model "$QIQC_MODEL" -C /app - < "$prompt"
done
'''


def archive_bytes(*, sustained=False, reasoning_effort=None, agent_budget_seconds=300):
    if type(agent_budget_seconds) is not int or agent_budget_seconds not in (180, 300):
        raise ValueError("Invalid diagnostic agent budget")
    if reasoning_effort not in (None, "none"):
        raise ValueError("reasoning_effort must be omitted or explicitly none")
    manifest={'schema':'qiqc-harness/v1-demo','name':'QIQC native Compose Codex candidate','adapter':'codex','entrypoint':'adapter.sh',
              'runtime_kind':'native_cli','task_transport':'native-http/v1',
              'version':'native-compose-candidate/1','model_interfaces':['responses']}
    readme='Native Codex submission. Uses the original task HTTP/MCP interfaces and image CLI. No qsim file helper.\n'
    if sustained:
        manifest.update(name='QIQC native Compose Codex sustained acceptance',
                        version='native-compose-sustained-acceptance/2')
        readme += ('Infrastructure test Harness: successful CLI sessions are followed by real task rechecking. '
                   'Nonzero exits fail immediately. The trusted platform enforces the 300-second budget. '
                   'No sleeping or fabricated runtime, reward, or model evidence. This is not a competition baseline.\n')
        readme += ('Version 2 places a connectivity instruction before the unchanged task input: '
                   'the real Codex session must first call the registered qsim MCP list_devices tool, '
                   'wait for its actual successful response, then continue the original task. '
                   'The shell adapter never invokes qsim on behalf of the model and never writes evidence logs. '
                   'This guided infrastructure acceptance strategy does not measure baseline solution quality.\n')
    if sustained and agent_budget_seconds == 180:
        manifest["version"] += "-budget180"
        readme = readme.replace("300-second", "180-second")
    adapter = SUSTAINED_ADAPTER if sustained else ADAPTER
    if reasoning_effort is not None:
        adapter = adapter.replace('codex exec ', 'codex exec -c \'model_reasoning_effort="none"\' ')
        manifest.update(reasoning_effort='none', version=manifest['version'] + '-reasoning-none',
                        name=manifest['name'] + ' (explicit reasoning none)')
        readme += ('Organizer diagnostic variant: Codex CLI explicitly selects model_reasoning_effort="none". '
                   'This is a submitted Harness setting, not a gateway request override. '
                   'It does not establish that provider transport errors are resolved.\n')
    files={'adapter.sh':adapter,'harness.json':json.dumps(manifest,sort_keys=True,indent=2)+'\n',
           'README.md':readme}
    target=io.BytesIO()
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as output:
        for name,body in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.external_attr=0o100644<<16
            output.writestr(info,body)
    return target.getvalue()
