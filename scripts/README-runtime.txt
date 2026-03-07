1. Run: ./scripts/init_runtime.sh
2. Edit .env.runtime
3. Fill files under secrets/
4. Start production stack:
   docker compose -f docker-compose.prod.yml --env-file .env.runtime up --build