SET ROLE ip_owner;
CREATE TABLE IF NOT EXISTS business_private.regions (
 tenant_id uuid NOT NULL, dataset_version uuid NOT NULL, region_id int NOT NULL,
 region_name text NOT NULL, PRIMARY KEY(tenant_id,dataset_version,region_id));
CREATE TABLE IF NOT EXISTS business_private.channels (
 tenant_id uuid NOT NULL, dataset_version uuid NOT NULL, channel_id int NOT NULL,
 channel_name text NOT NULL, PRIMARY KEY(tenant_id,dataset_version,channel_id));
CREATE TABLE IF NOT EXISTS business_private.products (
 tenant_id uuid NOT NULL, dataset_version uuid NOT NULL, product_id int NOT NULL,
 product_name text NOT NULL, category text NOT NULL, PRIMARY KEY(tenant_id,dataset_version,product_id));
CREATE TABLE IF NOT EXISTS business_private.orders (
 tenant_id uuid NOT NULL, dataset_version uuid NOT NULL, order_id int NOT NULL,
 region_id int NOT NULL, channel_id int NOT NULL, customer_ref text NOT NULL,
 status text NOT NULL CHECK(status IN ('paid','cancelled')),
 paid_amount numeric(18,2) NOT NULL CHECK(paid_amount>=0),
 created_at timestamptz NOT NULL, paid_at timestamptz,
 PRIMARY KEY(tenant_id,dataset_version,order_id),
 FOREIGN KEY(tenant_id,dataset_version,region_id) REFERENCES business_private.regions,
 FOREIGN KEY(tenant_id,dataset_version,channel_id) REFERENCES business_private.channels,
 CHECK((status='paid' AND paid_at IS NOT NULL) OR (status='cancelled' AND paid_at IS NULL)));
CREATE TABLE IF NOT EXISTS business_private.order_items (
 tenant_id uuid NOT NULL, dataset_version uuid NOT NULL, item_id int NOT NULL,
 order_id int NOT NULL, product_id int NOT NULL, quantity int NOT NULL CHECK(quantity>0),
 line_paid_amount numeric(18,2) NOT NULL CHECK(line_paid_amount>=0),
 PRIMARY KEY(tenant_id,dataset_version,item_id),
 FOREIGN KEY(tenant_id,dataset_version,order_id) REFERENCES business_private.orders,
 FOREIGN KEY(tenant_id,dataset_version,product_id) REFERENCES business_private.products);
CREATE TABLE IF NOT EXISTS business_private.aftersales (
 tenant_id uuid NOT NULL, dataset_version uuid NOT NULL, aftersale_id int NOT NULL,
 order_id int NOT NULL, status text NOT NULL CHECK(status IN ('open','resolved','rejected')),
 refund_amount numeric(18,2) NOT NULL CHECK(refund_amount>=0),
 created_at timestamptz NOT NULL, resolved_at timestamptz,
 PRIMARY KEY(tenant_id,dataset_version,aftersale_id),
 FOREIGN KEY(tenant_id,dataset_version,order_id) REFERENCES business_private.orders,
 CHECK((status='resolved' AND resolved_at IS NOT NULL) OR (status<>'resolved' AND resolved_at IS NULL)));
CREATE INDEX IF NOT EXISTS orders_period ON business_private.orders(tenant_id,dataset_version,paid_at);
CREATE INDEX IF NOT EXISTS aftersales_period ON business_private.aftersales(tenant_id,dataset_version,resolved_at);
RESET ROLE;
