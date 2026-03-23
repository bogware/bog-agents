# Changelog

## [0.5.0]

### Features

- HTTP serve module with REST API, SSE streaming, and thread management
- Three-tier auth for serve mode (env var, config, auto-generate for non-localhost)
- Serve dependencies available as pip extra: `pip install 'bog-agents[serve]'`
- 88+ middleware modules with lazy loading for fast imports
- Security audit middleware with self-detection exclusion
- Scheduled runs store with robust JSON loading
- Self-improving middleware with auto-session initialization

### Bug Fixes

- Fixed `ScheduledRunsStore._load()` crash on empty JSON files
- Fixed `SelfImprovingMiddleware` NoneType error when no session started
- Fixed security audit scanner detecting its own patterns as findings
