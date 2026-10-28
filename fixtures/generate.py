"Deterministic fictional retail data only. No user uploads or production records."

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_DNS, uuid5


def build_synthetic(seed=20261025):
    rng = random.Random(seed)
    tenants = tuple(uuid5(NAMESPACE_DNS, f"insightpilot.synthetic.{seed}.{x}") for x in ("A", "B"))
    data = []
    for tenant, label, count in zip(tenants, ("A", "B"), (96, 60), strict=True):
        records = {
            name: []
            for name in ("regions", "channels", "products", "orders", "order_items", "aftersales")
        }
        records["regions"] = [
            dict(region_id=i, region_name=n) for i, n in enumerate(("华东", "华南", "华北"), 1)
        ]
        records["channels"] = [
            dict(channel_id=i, channel_name=n) for i, n in enumerate(("商城", "直播", "门店"), 1)
        ]
        records["products"] = [
            dict(product_id=i, product_name=f"虚构商品{i}", category=c)
            for i, c in enumerate(("数码", "家居", "运动", "数码", "家居", "运动"), 1)
        ]
        for order_id in range(1, count + 1):
            # Three deliberate local-midnight boundaries plus paid/cancelled/zero amounts.
            instant = datetime(2026, 7, 15, tzinfo=UTC) + timedelta(hours=order_id * 20)
            if order_id in (1, 2, 3):
                instant = datetime(2026, 8, 31, 16, tzinfo=UTC) + timedelta(
                    microseconds=order_id - 2
                )
            cancelled = order_id % 13 == 0
            amount = (
                Decimal("0.00")
                if cancelled or order_id == 7
                else Decimal(rng.randint(100, 90000)) / 100
            )
            records["orders"].append(
                dict(
                    order_id=order_id,
                    region_id=order_id % 3 + 1,
                    channel_id=(order_id // 3) % 3 + 1,
                    customer_ref=f"SYN-{label}-{order_id:04}",
                    status="cancelled" if cancelled else "paid",
                    paid_amount=amount,
                    created_at=instant - timedelta(hours=2),
                    paid_at=None if cancelled else instant,
                )
            )
            first = (amount * Decimal("0.60")).quantize(Decimal("0.01"))
            for part, value in enumerate((first, amount - first)):
                records["order_items"].append(
                    dict(
                        item_id=2 * order_id - 1 + part,
                        order_id=order_id,
                        product_id=(order_id + part) % 6 + 1,
                        quantity=1 + order_id % 3,
                        line_paid_amount=value,
                    )
                )
            if not cancelled and order_id % 5 == 0:
                status = (
                    "open"
                    if order_id % 20 == 0
                    else ("rejected" if order_id % 15 == 0 else "resolved")
                )
                created = instant + timedelta(days=1)
                records["aftersales"].append(
                    dict(
                        aftersale_id=order_id,
                        order_id=order_id,
                        status=status,
                        refund_amount=(amount / 2).quantize(Decimal("0.01")),
                        created_at=created,
                        resolved_at=created + timedelta(hours=24 + order_id % 48)
                        if status == "resolved"
                        else None,
                    )
                )
        data.append((tenant, label, records))
    serial = json.dumps(
        data, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(serial.encode()).hexdigest()
    version = uuid5(NAMESPACE_DNS, f"insightpilot.dataset.{seed}.{digest}")
    return version, tenants, digest, data
