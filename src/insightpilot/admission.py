"Redis coordinates capacity; PostgreSQL execution fencing remains authoritative."

import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from redis.exceptions import RedisError

from insightpilot.errors import DomainError
from insightpilot.runs.repository import execution_row

TTL_MS = 60000
_EXECUTION = ContextVar("insightpilot_execution_permit", default=None)
ACQUIRE = """
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now)
if redis.call('ZCARD',KEYS[1])>=2 then return 0 end
if ARGV[2]=='BACKGROUND' and redis.call('ZCARD',KEYS[2])>=1 then return 0 end
local until_ms=now+tonumber(ARGV[3])
redis.call('ZADD',KEYS[1],until_ms,ARGV[1])
if ARGV[2]=='BACKGROUND' then redis.call('ZADD',KEYS[2],until_ms,ARGV[1]) end
redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[3])*2)
redis.call('PEXPIRE',KEYS[2],tonumber(ARGV[3])*2)
return 1
"""
RENEW = """
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
local score=redis.call('ZSCORE',KEYS[1],ARGV[1])
if not score or tonumber(score)<=now then return 0 end
if ARGV[2]=='BACKGROUND' then
 local bg=redis.call('ZSCORE',KEYS[2],ARGV[1]);if not bg or tonumber(bg)<=now then return 0 end
end
local until_ms=now+tonumber(ARGV[3])
redis.call('ZADD',KEYS[1],until_ms,ARGV[1])
if ARGV[2]=='BACKGROUND' then redis.call('ZADD',KEYS[2],until_ms,ARGV[1]) end
redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[3])*2)
redis.call('PEXPIRE',KEYS[2],tonumber(ARGV[3])*2)
return 1
"""
RELEASE = """
redis.call('ZREM',KEYS[2],ARGV[1]);return redis.call('ZREM',KEYS[1],ARGV[1])
"""
VALID = """
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
local score=redis.call('ZSCORE',KEYS[1],ARGV[1])
if not score or tonumber(score)<=now then return 0 end
if ARGV[2]=='BACKGROUND' then
 local bg=redis.call('ZSCORE',KEYS[2],ARGV[1]);if not bg or tonumber(bg)<=now then return 0 end
end
return 1
"""


@dataclass(frozen=True)
class Permit:
    token: str
    owner: str
    execution_class: str
    kind: str

    @property
    def member(self):
        return self.owner + ":" + self.token


@asynccontextmanager
async def execution_scope(admission, permit):
    token = _EXECUTION.set((admission, permit))
    try:
        yield
    finally:
        _EXECUTION.reset(token)


async def enforce_execution_permit():
    current = _EXECUTION.get()
    if current and not await current[0].valid(current[1]):
        raise DomainError("admission_lost", 503)


class Admission:
    def __init__(self, redis, pool=None):
        self.redis, self.pool = redis, pool

    def keys(self, kind):
        if kind not in ("execution", "model"):
            raise ValueError("admission_kind_invalid")
        return (
            f"insightpilot:admission:{{{kind}}}:all",
            f"insightpilot:admission:{{{kind}}}:background",
        )

    async def _eval(self, script, permit):
        try:
            return bool(
                await self.redis.eval(
                    script,
                    2,
                    *self.keys(permit.kind),
                    permit.member,
                    permit.execution_class,
                    TTL_MS,
                )
            )
        except RedisError:
            raise DomainError("admission_unavailable", 503) from None

    async def acquire(self, execution_class, owner, kind="execution"):
        if execution_class not in ("INTERACTIVE", "BACKGROUND"):
            raise ValueError("execution_class_invalid")
        permit = Permit(str(uuid4()), str(owner), str(execution_class), kind)
        return permit if await self._eval(ACQUIRE, permit) else None

    async def renew(self, permit):
        return await self._eval(RENEW, permit)

    async def valid(self, permit):
        return await self._eval(VALID, permit)

    async def release(self, permit):
        return await self._eval(RELEASE, permit)

    @asynccontextmanager
    async def model_slot(self, fence):
        permit = None
        while permit is None:
            await enforce_execution_permit()
            async with self.pool.connection() as conn, conn.transaction():
                row = await execution_row(conn, fence)
            permit = await self.acquire(row["scope"], fence.owner, kind="model")
            if permit is None:
                await asyncio.sleep(0.05)
        caller = asyncio.current_task()
        lost = asyncio.Event()

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(10)
                    if not await self.renew(permit):
                        lost.set()
                        caller.cancel()
                        return
            except DomainError:
                lost.set()
                caller.cancel()

        watcher = asyncio.create_task(heartbeat())
        try:
            yield permit
            if not await self.valid(permit):
                raise DomainError("admission_lost", 503)
        except asyncio.CancelledError:
            if lost.is_set():
                raise DomainError("admission_lost", 503) from None
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            with suppress(DomainError):
                await self.release(permit)
