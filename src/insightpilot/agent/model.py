"One durable reservation per actual provider attempt, without SDK retries."

import asyncio
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import openai
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from pydantic import ValidationError

from insightpilot.agent.prompts import messages
from insightpilot.agent.state import GeneratedQuery, PlannedAnalysis
from insightpilot.checkpoints import current_checkpoint_scope
from insightpilot.reports.models import ReportNarrative
from insightpilot.runs.repository import RunRepository, require_fence


@asynccontextmanager
async def unrestricted_slot():
    yield


class AnalysisModel:
    def __init__(self, settings, pool, admission=None):
        self.settings, self.pool, self.admission = settings, pool, admission
        self.http = openai.DefaultAsyncHttpxClient(trust_env=False, follow_redirects=False)
        self.llm = ChatOpenAI(
            model=settings.model_name,
            api_key=settings.model_api_key.get_secret_value(),
            base_url=settings.model_base_url,
            temperature=0,
            max_retries=0,
            http_async_client=self.http,
            use_responses_api=False,
            cache=False,
            # langchain-openai may rename its max_tokens field for OpenAI models.
            # Bailian accepts this explicit compatible-mode wire parameter.
            extra_body={"max_tokens": 2048, "enable_thinking": False},
        )

    async def aclose(self):
        await self.http.aclose()

    async def _call(self, kind, payload, dto):
        # A malformed response gets one bounded format correction. Each attempt
        # still reserves the persistent model budget and records actual usage.
        for correction in (False, True):
            request = {**payload, "format_correction": True} if correction else payload
            try:
                return await self._attempt(kind, request, dto)
            except RuntimeError as error:
                if error.args != ("model_output_invalid",) or correction:
                    raise
        raise RuntimeError("model_output_invalid")

    async def _attempt(self, kind, payload, dto):
        scope = current_checkpoint_scope()
        gate = self.admission.model_slot(scope.fence) if self.admission else unrestricted_slot()
        async with gate:
            budget = await RunRepository(self.pool).reserve(scope.fence, "model")
            identity = uuid4()
            async with self.pool.connection() as conn, conn.transaction():
                await require_fence(conn, scope.fence)
                await conn.execute(
                    """
                    INSERT INTO app_state.model_calls
                    (id,tenant_id,run_id,task_revision,execution_version,owner,kind,reservation,model_name)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                    (
                        identity,
                        scope.actor.tenant_id,
                        scope.fence.run_id,
                        scope.fence.task_revision,
                        scope.fence.execution_version,
                        scope.fence.owner,
                        kind,
                        budget.model_used,
                        self.settings.model_name,
                    ),
                )
            started, raw, result, code = time.monotonic(), None, None, None
            try:
                runnable = self.llm.with_structured_output(
                    dto, method="json_mode", include_raw=True
                )
                # Model/raw provider responses must never be exported to ambient
                # LangSmith tracing or persisted as graph state.
                with tracing_context(enabled=False):
                    async with asyncio.timeout(budget.model_timeout_seconds):
                        response = await runnable.ainvoke(
                            messages(kind, payload, dto.model_json_schema()),
                            config={"callbacks": []},
                            timeout=budget.model_timeout_seconds,
                        )
                raw = response.get("raw")
                if response.get("parsing_error") is not None or response.get("parsed") is None:
                    code = "model_output_invalid"
                else:
                    result = dto.model_validate(response["parsed"])
            except (TimeoutError, openai.APITimeoutError):
                code = "provider_timeout"
            except (openai.AuthenticationError, openai.PermissionDeniedError):
                code = "provider_authorization_denied"
            except openai.APIStatusError as error:
                code = (
                    "provider_unavailable"
                    if error.status_code >= 500 or error.status_code == 429
                    else "provider_request_rejected"
                )
            except openai.APIConnectionError:
                code = "provider_unavailable"
            except (ValidationError, ValueError, TypeError):
                code = "model_output_invalid"
            finally:
                metadata = getattr(raw, "response_metadata", {}) or {}
                usage = metadata.get("token_usage") or {}
                # SDK-normalized usage_metadata may invent zero for absent fields.
                counts = [
                    usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                ]
                counts = [
                    value if type(value) is int and 0 <= value < 10**12 else None
                    for value in counts
                ]
                usage_status = (
                    "reported"
                    if all(v is not None for v in counts)
                    else "partial"
                    if any(v is not None for v in counts)
                    else "unavailable"
                )
                provider_model = metadata.get("model_name")
                if not (
                    isinstance(provider_model, str)
                    and len(provider_model) <= 128
                    and provider_model.isascii()
                    and provider_model.isprintable()
                ):
                    provider_model = None
                # This is append-only cost history for this reserved attempt.
                # A late response may record usage but cannot mutate run/output.
                async with self.pool.connection() as conn:
                    await conn.execute(
                        """
                        UPDATE app_state.model_calls SET status=%s,usage_status=%s,
                        input_tokens=%s,output_tokens=%s,total_tokens=%s,elapsed_ms=%s,
                        error_code=%s,provider_model=%s,ended_at=clock_timestamp()
                        WHERE id=%s AND status='STARTED'
                    """,
                        (
                            "FAILED" if code or result is None else "SUCCEEDED",
                            usage_status,
                            *counts,
                            int((time.monotonic() - started) * 1000),
                            code,
                            provider_model,
                            identity,
                        ),
                    )
            if code:
                raise RuntimeError(code)
            return result

    async def plan(self, context):
        return await self._call("plan", context.model_dump(mode="json"), PlannedAnalysis)

    async def sql(self, step, context):
        return await self._call(
            "sql",
            {"step": step.model_dump(mode="json"), "context": context.model_dump(mode="json")},
            GeneratedQuery,
        )

    async def describe(self, facts):
        return await self._call("describe", facts.model_dump(mode="json"), ReportNarrative)
