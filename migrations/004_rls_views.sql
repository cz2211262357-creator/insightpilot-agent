SET ROLE ip_owner;
ALTER TABLE business_private.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_private.orders FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_version ON business_private.orders;
CREATE POLICY tenant_version ON business_private.orders USING (
 tenant_id = nullif(current_setting('insightpilot.tenant_id',true),'')::uuid
 AND dataset_version = nullif(current_setting('insightpilot.dataset_version',true),'')::uuid);
CREATE OR REPLACE VIEW analysis.orders WITH (security_invoker=true,security_barrier=true)
 AS SELECT order_id,region_id,channel_id,customer_ref,status,paid_amount,created_at,paid_at FROM business_private.orders;
ALTER TABLE business_private.order_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_private.order_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_version ON business_private.order_items;
CREATE POLICY tenant_version ON business_private.order_items USING (
 tenant_id = nullif(current_setting('insightpilot.tenant_id',true),'')::uuid
 AND dataset_version = nullif(current_setting('insightpilot.dataset_version',true),'')::uuid);
CREATE OR REPLACE VIEW analysis.order_items WITH (security_invoker=true,security_barrier=true)
 AS SELECT item_id,order_id,product_id,quantity,line_paid_amount FROM business_private.order_items;
ALTER TABLE business_private.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_private.products FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_version ON business_private.products;
CREATE POLICY tenant_version ON business_private.products USING (
 tenant_id = nullif(current_setting('insightpilot.tenant_id',true),'')::uuid
 AND dataset_version = nullif(current_setting('insightpilot.dataset_version',true),'')::uuid);
CREATE OR REPLACE VIEW analysis.products WITH (security_invoker=true,security_barrier=true)
 AS SELECT product_id,product_name,category FROM business_private.products;
ALTER TABLE business_private.channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_private.channels FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_version ON business_private.channels;
CREATE POLICY tenant_version ON business_private.channels USING (
 tenant_id = nullif(current_setting('insightpilot.tenant_id',true),'')::uuid
 AND dataset_version = nullif(current_setting('insightpilot.dataset_version',true),'')::uuid);
CREATE OR REPLACE VIEW analysis.channels WITH (security_invoker=true,security_barrier=true)
 AS SELECT channel_id,channel_name FROM business_private.channels;
ALTER TABLE business_private.regions ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_private.regions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_version ON business_private.regions;
CREATE POLICY tenant_version ON business_private.regions USING (
 tenant_id = nullif(current_setting('insightpilot.tenant_id',true),'')::uuid
 AND dataset_version = nullif(current_setting('insightpilot.dataset_version',true),'')::uuid);
CREATE OR REPLACE VIEW analysis.regions WITH (security_invoker=true,security_barrier=true)
 AS SELECT region_id,region_name FROM business_private.regions;
ALTER TABLE business_private.aftersales ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_private.aftersales FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_version ON business_private.aftersales;
CREATE POLICY tenant_version ON business_private.aftersales USING (
 tenant_id = nullif(current_setting('insightpilot.tenant_id',true),'')::uuid
 AND dataset_version = nullif(current_setting('insightpilot.dataset_version',true),'')::uuid);
CREATE OR REPLACE VIEW analysis.aftersales WITH (security_invoker=true,security_barrier=true)
 AS SELECT aftersale_id,order_id,status,refund_amount,created_at,resolved_at FROM business_private.aftersales;
GRANT SELECT ON ALL TABLES IN SCHEMA business_private,analysis TO ip_reader;
RESET ROLE;
