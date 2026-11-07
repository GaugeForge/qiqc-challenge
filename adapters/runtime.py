#!/usr/bin/env python3
"""QIQC real CLI adapter. Execute only inside the isolated agent sandbox."""

import json, os, pathlib, subprocess, sys


def main():
    manifest = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "harness.json").read_text()
    )
    # Minimal manifests name only "runtime"; legacy full manifests use "adapter".
    aliases = {"codex": "codex", "deepseek": "deepseek-harness", "pi": "pi-agent",
               "deepseek-harness": "deepseek-harness", "pi-agent": "pi-agent"}
    kind = aliases.get(manifest.get("runtime") or manifest.get("adapter"))
    if kind not in ("codex", "deepseek-harness", "pi-agent"):
        raise SystemExit("harness.json runtime must be codex, deepseek or pi")
    required = ("QIQC_TASK_INPUT", "QIQC_MODEL_URL", "QIQC_MODEL_TOKEN", "QIQC_MODEL")
    for name in required:
        if not os.environ.get(name):
            raise SystemExit(name + " is required")
    prompt = pathlib.Path(os.environ["QIQC_TASK_INPUT"]).read_text()
    url = os.environ["QIQC_MODEL_URL"].rstrip("/")
    for suffix in ("/responses", "/chat/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    state = (
        pathlib.Path(os.environ.get("QIQC_RUNTIME_STATE", "/tmp/qiqc-runtime")) / kind
    )
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    env = os.environ.copy()
    model = env["QIQC_MODEL"]
    protocol = env.get("QIQC_MODEL_PROTOCOL", "chat/completions")
    if kind != "codex" and json.loads(env.get("QIQC_MCP_SERVERS", "[]")):
        helper = str(pathlib.Path(__file__).with_name("qsim.py"))
        prompt += (
            "\n\nTask tools are the original qsim MCP service. From your shell tool, "
            "run `python3 " + helper + " list` to discover tool names and schemas. "
            "Call a tool with `python3 " + helper + " call TOOL_NAME 'JSON_OBJECT'`. "
            "This client reports the original service response. Follow the task's "
            "required answer and artifact format; a final chat message is not a submission.\n"
        )
    output = pathlib.Path(env.get("QIQC_OUTPUT", "/work/answer.txt"))
    trace = pathlib.Path(env.get("QIQC_TRAJECTORY", "/work/trajectory.jsonl"))
    output.parent.mkdir(parents=True, exist_ok=True)
    trace.parent.mkdir(parents=True, exist_ok=True)
    if kind == "codex":
        env["CODEX_HOME"] = env.get("CODEX_HOME", str(state))
        state = pathlib.Path(env["CODEX_HOME"])
        state.mkdir(parents=True, exist_ok=True)
        config = (
            "model = "
            + json.dumps(model)
            + '\nmodel_provider = "qiqc"\napproval_policy = "never"\nweb_search = "disabled"\n[model_providers.qiqc]\nname = "QIQC scoped gateway"\nbase_url = '
            + json.dumps(url)
            + '\nenv_key = "QIQC_MODEL_TOKEN"\nwire_api = "responses"\n'
        )
        # Preserve the organizer's native MCP configuration for this task.
        if not (state / "config.toml").exists():
            for server in json.loads(env.get("QIQC_MCP_SERVERS", "[]")):
                if server.get("transport") != "streamable-http":
                    raise SystemExit("Unsupported task transport")
                config += (
                    "[mcp_servers."
                    + json.dumps(server["name"])
                    + "]\nurl = "
                    + json.dumps(server["url"])
                    + "\n"
                )
            (state / "config.toml").write_text(config)
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--output-last-message",
            str(output),
            prompt,
        ]
    elif kind == "pi-agent":
        env["PI_CODING_AGENT_DIR"] = str(state)
        config = {
            "providers": {
                "qiqc": {
                    "baseUrl": url,
                    "apiKey": "$QIQC_MODEL_TOKEN",
                    "api": "openai-responses"
                    if protocol == "responses"
                    else "openai-completions",
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": 200000,
                            "maxTokens": int(env.get("QIQC_MODEL_MAX_OUTPUT_TOKENS", "16384")),
                            "cost": {
                                "input": 0,
                                "output": 0,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                            },
                        }
                    ],
                }
            }
        }
        (state / "models.json").write_text(json.dumps(config))
        cmd = [
            "pi",
            "--provider",
            "qiqc",
            "--model",
            model,
            "--mode",
            "json",
            "--no-session",
            "-p",
            prompt,
        ]
    else:
        env["DSH_HOME"] = str(state)
        profile = state / "profiles/headless"
        profile.mkdir(parents=True, exist_ok=True)
        # JSON is a subset of YAML. Use env reference so the token isn't copied into config.
        config = [
            {
                "id": "agent-default-model",
                "config": {"provider": "qiqc", "model": model},
            },
            {
                "id": "llm-pi-ai",
                "config": {
                    "providers": {
                        "qiqc": {
                            "apiKeyEnv": "QIQC_MODEL_TOKEN",
                            "api": "openai-responses"
                            if protocol == "responses"
                            else "openai-completions",
                            "baseURL": url,
                            "models": [{"id": model}],
                        }
                    }
                },
            },
        ]
        (profile / "cordis.patch.yml").write_text(json.dumps(config))
        cmd = ["dsh", "--profile", "headless", "--json", prompt]
    with trace.open("w") as stream:
        result = subprocess.run(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=sys.stderr,
            # The trusted runner stops the complete process group at the native
            # budget. Leave time for that stop instead of racing it and turning
            # a valid timeout into a Python adapter failure.
            timeout=int(env["QIQC_AGENT_TIMEOUT"]) + 10,
        )
    if kind != "codex":
        # Native runtime transcript remains authoritative; this file is only a transcript pointer.
        output.write_text(
            json.dumps({"trajectory": str(trace), "exit_code": result.returncode})
            + "\n"
        )
    if result.returncode == 0:
        from terminal import successful_terminal

        rows = []
        with trace.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except ValueError:
                    continue
        if not successful_terminal(kind, rows):
            # A clean CLI exit without a submission is a scientific rejection
            # and must reach Harbor's verifier.  A reported model/provider
            # error is different: it is an infrastructure failure and must
            # not be converted into reward 0 merely because the wrapper
            # process itself exited zero.
            if any(row.get("type") in ("turn.failed", "error") for row in rows):
                print("Native CLI reported a model/provider error", file=sys.stderr)
                return 1
            if kind == "deepseek-harness" and any(row.get("type") == "error" for row in rows):
                print("Native CLI reported a model/provider error", file=sys.stderr)
                return 1
            if kind == "pi-agent":
                for row in rows:
                    messages = [row.get("message")]
                    if isinstance(row.get("messages"), list):
                        messages.extend(row["messages"])
                    if any(isinstance(message, dict) and message.get("stopReason") in ("error", "aborted") for message in messages):
                        print("Native CLI reported a model/provider error", file=sys.stderr)
                        return 1
            print("Native CLI completed without a successful terminal event; verifier will score the scientific outcome", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
