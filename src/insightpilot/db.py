"Separate bounded pools for state, official checkpoints and readonly analytics."

import asyncio

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from insightpilot.config import Settings


class DatabasePools:
    def __init__(self, settings: Settings):
        self.postgres = {
            name: AsyncConnectionPool(
                secret.get_secret_value(),
                name=f"insightpilot-{name}",
                open=False,
                min_size=0,
                max_size=3,
                timeout=1,
                reconnect_timeout=1,
                kwargs={
                    "autocommit": True,
                    "row_factory": dict_row,
                    "connect_timeout": 1,
                    "prepare_threshold": 0,
                    "options": "-c timezone=UTC"
                    + (
                        " -c search_path=graph_checkpoint,pg_catalog"
                        if name == "checkpoint"
                        else ""
                    ),
                },
            )
            for name, secret in [
                ("state", settings.postgres_state_dsn),
                ("checkpoint", settings.postgres_checkpoint_dsn),
                ("query", settings.postgres_query_dsn),
            ]
        }
        self.redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )

    async def open(self) -> None:
        for pool in self.postgres.values():
            await pool.open(wait=False)

    async def close(self) -> None:
        await self.redis.aclose()
        await asyncio.gather(*(pool.close() for pool in self.postgres.values()))

    async def _postgres_ready(self, name: str, pool: AsyncConnectionPool) -> bool:
        try:
            async with pool.connection(timeout=1) as connection:
                probes = {
                    "state": [
                        "SELECT id,provider_model FROM app_state.model_calls LIMIT 0",
                        "SELECT id,execution_version FROM app_state.runs LIMIT 0",
                    ],
                    "checkpoint": [
                        "SELECT checkpoint_id FROM graph_checkpoint.checkpoints LIMIT 0",
                        "SELECT checkpoint_id FROM graph_checkpoint.checkpoint_writes LIMIT 0",
                    ],
                    "query": ["SELECT order_id,paid_amount FROM analysis.orders LIMIT 0"],
                }
                for statement in probes[name]:
                    await connection.execute(statement)
                if name == "state":
                    row = await (
                        await connection.execute(
                            "SELECT version FROM app_state.schema_version WHERE singleton"
                        )
                    ).fetchone()
                    return row == {"version": 10}
                return True
        except Exception:
            return False

    async def _redis_ready(self) -> bool:
        try:
            return await self.redis.ping() is True
        except Exception:
            return False

    async def health(self) -> dict[str, bool]:
        names = [*self.postgres, "redis"]
        results = await asyncio.gather(
            *(self._postgres_ready(name, pool) for name, pool in self.postgres.items()),
            self._redis_ready(),
        )
        return dict(zip(names, results, strict=True))
