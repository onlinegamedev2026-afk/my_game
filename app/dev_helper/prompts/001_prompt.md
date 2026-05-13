Parent folder: ./using_claude
Target folder: ./using_claude_v2
Reference prompt: ./using_claude/prompts/003_baseprompt.md

Task:
Read 003_baseprompt.md and inspect the existing working webapp in ./using_claude. Then fully verify and fix ./using_claude_v2 so it preserves all working logic from ./using_claude and satisfies the requirements from 003_baseprompt.md.

Main goals:
1. Compare ./using_claude and ./using_claude_v2 feature-by-feature.
2. Ensure no existing working logic from ./using_claude is broken in ./using_claude_v2.
3. Fix all errors in ./using_claude_v2: backend, frontend, templates, routes, services, database, Docker, env/config, WebSocket/game flow, wallet/transaction logic, auth, OTP, agent/user/admin flows.
4. Verify all important business logic:
   - Admin/Agent/User login
   - unique email across Admin/Agent/User
   - Agent/User activation and deactivation cascade
   - delete cascade for child agents/users
   - wallet funding/deduction
   - wallet transaction history
   - game betting
   - game settlement
   - winner calculation
   - OTP validation and expiry
   - password regeneration flow
   - dashboard child listing
   - WebSocket/live game updates
5. Keep all code consistent with the existing technology and structure unless a change is required to fix bugs.
6. Do not remove working logic.
7. Do not add unnecessary new architecture unless required by 003_baseprompt.md or existing broken code.
8. After fixing, run checks/tests/build commands and report:
   - what was broken
   - what was fixed
   - files changed
   - commands run
   - remaining issues, if any

Important:
- Treat ./using_claude as the source of currently working behavior.
- Treat ./using_claude/prompts/003_baseprompt.md as the requirement source.
- Treat ./using_claude_v2 as the folder to fix.
- Make changes only inside ./using_claude_v2.
- If any requirement conflicts with the working app, preserve the requirement from 003_baseprompt.md but do not break existing behavior unnecessarily.
- Ensure the app can run without obvious runtime/import/template/database/Docker errors.