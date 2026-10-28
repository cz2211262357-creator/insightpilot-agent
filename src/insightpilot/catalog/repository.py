from insightpilot.catalog.models import Catalog
from insightpilot.migrations import load_manifest


class CatalogRepository:
    def __init__(self, pool):
        self.pool = pool

    async def list_datasets(self, actor):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT id,as_of FROM app_state.dataset_versions WHERE tenant_id=%s AND active",
                    (actor.tenant_id,),
                )
            ).fetchone()
        if row is None:
            raise LookupError("dataset_not_found")
        manifest = load_manifest()
        return Catalog(
            version=manifest["version"],
            dataset_version=row["id"],
            as_of=row["as_of"],
            views=manifest["views"],
            metrics=manifest["metrics"],
        )

    async def get_metric_definition(self, actor, metric_id):
        catalog = await self.list_datasets(actor)
        for metric in catalog.metrics:
            if metric.metric_id == metric_id:
                return metric
        raise LookupError("metric_not_found")
