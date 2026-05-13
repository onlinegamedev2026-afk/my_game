Context:
	Ancestor folder using_claude_v2 contains the original working web application.
	Target folder is also using_claude_v3 (inside using_claude_v2)
	Ensure all existing logic remains intact unless explicitly required below.
    you can take reference from dev_helper/diagrams
Updates (Strict Requirements)
	Single Active Session (All Roles: Admin, Agent, User)
		Only one active login session per account is allowed.
		If already logged in:
			Block login from another device / browser / tab (same browser included)
		Enforce globally (backend-level, not UI-only).
	Agent and User Lifecycle Logic (Verify + Enforce Strictly)
		**Agent/User Deactivation & Removal Logic**
		1. **Deactivate (Agent/User)**
		* On clicking *Deactivate*, target entity is set inactive.
		* If Agent → all descendants (multi-level Agents + Users) also become inactive.
		* Show console message: `ID deactivated at HH:MM:SS`.
		* No page redirect for current Admin/Agent.
		**Session Handling:**
		* If in-game → allow game cycle + transactions to complete → then force logout → redirect to login.
		* If doing transactions → allow completion → then force logout → redirect to login.
		* Re-login blocked until parent/that Agent is reactivated.
		2. **Remove (Agent/User)**
		* On clicking *Remove*, target entity is permanently deleted.
		* If Agent → all descendants (multi-level Agents + Users) also deleted.
		* Show console message: `ID removed at HH:MM:SS`.
		* No page redirect for current Admin/Agent.
		**Session Handling:**
		* If in-game → complete cycle + transactions → then force logout → redirect to login.
		* If doing transactions → allow completion → then force logout → redirect to login.
		* Re-login permanently blocked (account deleted).
		**Note:**
		* Same logic applies to Users (no descendants, so only self affected).	
	Session Timeout (Configurable via ENV)
		Add environment variable:
			SESSION_TIMEOUT_HOUR
		Behavior:
			Value range: 1–10 → session expires after N hours
			Value -1 → no session timeout (infinite session)
		Applies to: Admin, Agent, User
		Must be enforced at backend (token/session validation layer)
Constraints
	Do NOT break existing working logic
	Do NOT modify unrelated code
	Implement changes in a scalable + concurrency-safe way
	Ensure compatibility with current architecture (FastAPI + Redis/PostgreSQL if used)