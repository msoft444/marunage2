1. Run: ./scripts/init_runtime.sh
2. Edit .env.runtime
3. Fill files under secrets/ except GitHub token
4. Authenticate GitHub CLI:
   gh auth login
5. Confirm token retrieval:
   gh auth token
6. Start production stack:
   python scripts/gh_token_compose.py up --build