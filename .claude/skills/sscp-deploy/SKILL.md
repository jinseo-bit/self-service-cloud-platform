---
name: sscp-deploy
description: Deploy the developer's repository to the Self-Service Cloud Platform through the `sscp` MCP server. Use when the user asks to deploy, host, put online, or "배포해줘" an app/repo/site, asks which AWS setup fits their app, or asks why a platform environment failed. The agent drafts; a person approves in the web UI.
---

# Deploy a repository with the Self-Service Cloud Platform

You turn a repository into a platform environment **draft**. You never create AWS resources yourself: the platform
builds nothing until the developer presses **승인하고 만들기** in the web UI. The API refuses approve, delete, retry,
redeploy, extend and budget overrides from your token, so do not try them or suggest workarounds.

If a tool returns "로그인이 필요해요", tell the developer to run `python -m agent.login` **in their own terminal**
(it asks for the password there). Never ask for a password, token or key in the chat.

## Steps

1. **Find what to deploy.** Usually the current repo. For a static site that has a build step, the thing to upload is
   the build output folder (e.g. `dist/`, `build/`). Ask the developer to build it if it does not exist yet.

2. **Analyze**: call `analyze_repo(path)`.
   - If `unsupported` is not empty, explain each `why` and stop. These are outside the platform's scope:
     several services, a non-HTTP protocol, or a port that can only be set by a command-line flag.
   - Show the developer the draft as a short table: answer, confidence, evidence file.
   - For every key in `confirm`, ask the developer to confirm or correct it. Quote the evidence. These answers change
     which setup is chosen, for example a WebSocket library that is never used.
   - Ask every question in `questions` in plain words. Do not pick answers for them; traffic, downtime tolerance,
     audience and budget are the developer's decisions.
   - If `port_env` is not `PORT`, the app reads its port from another variable. Plan to pass it as an env var set to
     `8080`, for example `WHOAMI_PORT_NUMBER=8080`.

3. **Recommend**: call `recommend(requirements)` with the draft completed by the developer's answers. Explain:
   - `run_model` (정적 호스팅 / 요청 단위 실행 / 상시 실행 컨테이너) and `reasons`
   - `cost_usd_month` (typical and max)
   - `warnings` and what was `not_chosen` and why

   If `over_budget`, offer to change requirements; agreeing to exceed the budget is the developer's click in the UI.
   If `unsupported`, explain it and the `nearest_alternative`.

4. **Draft**: only after the developer says to go ahead, call `create_pending_environment(name, requirements, ...)`.
   - Image apps (`artifact` = IMAGE): pass `image` (with a tag or digest; docker.io, public.ecr.aws, ghcr.io or quay.io),
     `health_path`, and non-secret `env_vars`.
   - Static sites (`needs_site_upload`): call `upload_site(env_id, build_folder)`.

5. **Hand over**: give the developer the `approve_here` link. Tell them to check the setup and cost there and press
   **승인하고 만들기** themselves. Then poll `get_environment(env_id)` every ~20 s until it is READY, DEGRADED or FAILED.
   A first container environment can take ~20 minutes.

6. **Report**:
   - READY: give the `url`.
   - FAILED or DEGRADED: call `explain_failure(env_id)` and relay `what_to_do`. Retry and delete are the developer's
     buttons in the UI. If the app or settings must change, fix them and create a new draft.

## Example opening

> 저장소를 분석했어요. Express + socket.io가 있어서 실시간 연결이 필요해 보여요 (`package.json`: socket.io).
> 실시간 기능을 실제로 쓰시나요? 그리고 하루 사용자 수와, 잠깐 멈춰도 되는지 알려 주세요.

## After deployment (Day-2)

If the developer asks whether an environment is still OK, or `get_environment` shows status **DRIFTED**:
- call `recheck_environment(env_id)` to run the checks now (read-only), then `get_environment` again;
- for DRIFTED, call `explain_failure(env_id)` and relay what changed, who changed it and when (`drift`), whether the
  endpoint was blocked, and the prepared repair plan (the resources it will change back);
- the repair is the developer's click on **원래 구성으로 복구** in the web UI. If the change was intended, it should be
  made as a requirement change instead, or it will be detected again.
