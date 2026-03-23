# Changelog

## [0.5.2](https://github.com/bogware/bog-agents/compare/bog-agents==0.5.1...bog-agents==0.5.2) (2026-03-23)


### Features

* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))
* **sdk:** implement real parallel execution in ParallelAgentsMiddleware ([6f6bdd2](https://github.com/bogware/bog-agents/commit/6f6bdd22995f953badfb0a4f2743021b09f0aabd))
* **sdk:** remove unused imports and dead code in middleware modules ([c638bb0](https://github.com/bogware/bog-agents/commit/c638bb0ba9620feda2843a231cd1ab917f429968))
* **sdk:** suppress ty unresolved-import errors for optional dependencies ([648a438](https://github.com/bogware/bog-agents/commit/648a4380bc316c0f239ecba87afad0042e74b7c1))

## [0.5.1](https://github.com/bogware/bog-agents/compare/bog-agents==0.5.0...bog-agents==0.5.1) (2026-03-23)


### Features

* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))
* **sdk:** implement real parallel execution in ParallelAgentsMiddleware ([6f6bdd2](https://github.com/bogware/bog-agents/commit/6f6bdd22995f953badfb0a4f2743021b09f0aabd))
* **sdk:** remove unused imports and dead code in middleware modules ([c638bb0](https://github.com/bogware/bog-agents/commit/c638bb0ba9620feda2843a231cd1ab917f429968))
* **sdk:** suppress ty unresolved-import errors for optional dependencies ([648a438](https://github.com/bogware/bog-agents/commit/648a4380bc316c0f239ecba87afad0042e74b7c1))

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
